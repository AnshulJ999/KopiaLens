// Settings backups (copies, restore, export/import), the damaged-file notice, pausing indexing,
// and the About section. Routes: app/api/appconfig.py.
const _cfgPost = (url, body) => getJson(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

const CFG_GROUPS = [
  ["settings", "Settings"],
  ["repos", "Repository list"],
  ["accounts", "Sign-in accounts"],
  ["keys", "Encryption key files"],
];
const CFG_GROUP_NOTE = {
  accounts: "Usernames and passwords go back to what they were then.",
  keys: "Only if the key files are damaged. Keys from before an encryption reset can't open the current index.",
};
const CFG_KIND = { change: "Before changes", weekly: "Weekly", upgrade: "Before upgrade", manual: "Manual", safety: "Before restore" };
// Names for the settings a restore preview lists most (the rest show their key in words).
const CFG_SETTING_LABELS = {
  "kopialens.write_mode": "Permissions", "kopialens.auth_enabled": "Sign-in", "kopialens.bind_host": "Listens on",
  "kopialens.access": "Who can connect", "kopialens.access_addresses": "Allowed addresses", "kopialens.allowed_hosts": "Allowed host names",
  "kopialens.https": "HTTPS", "kopialens.https_cert": "HTTPS certificate", "kopialens.https_key": "HTTPS key",
  "kopialens.trusted_proxies": "Trusted proxies", "kopialens.encryption": "Encryption", "kopialens.encryption_level": "Encryption level",
  "kopialens.kopia_path": "Kopia program", "kopialens.theme": "Theme", "kopialens.density": "Density", "last_repo": "Last repository",
};
const CFG_REPO_FIELDS = { name: "name", short_name: "short name", config_file: "connection file", write_mode: "permissions",
  engine: "who runs backups", engine_moved_from: "moved KopiaUI file", counts_as_backup: "counts as a backup", kind: "kind" };
const CFG_FILE = { "settings.json": "settings file", "repos.json": "repository list", "users.json": "sign-in accounts file" };

const appConfigPart = {
    CFG_GROUPS, CFG_GROUP_NOTE,

    // ---- backups --------------------------------------------------------------------------
    async loadCfg() {
      this.cfgError = false;
      try {
        const d = await getJson("/api/config-backups");
        this.cfg = d;
        this.cfgNotices = d.notices || [];
      } catch (e) { this.cfgError = true; }
    },
    cfgKind(b) {
      if (b.kind === "safety" && /import/.test(b.note || "")) return "Before import";
      return CFG_KIND[b.kind] || b.kind;
    },
    cfgGroupsLabel(groups) {
      return CFG_GROUPS.filter((x) => (groups || []).includes(x[0])).map((x) => x[1]).join(", ");
    },
    async cfgBackUpNow() {
      if (this.cfgBusy) return;
      this.cfgBusy = "backup";
      try {
        await _cfgPost("/api/config-backups");
        this.klNotify("Backup made");
        await this.loadCfg();
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
      finally { this.cfgBusy = ""; }
    },
    async cfgOpenRestore(b) {
      const groups = {};
      for (const [g] of CFG_GROUPS) groups[g] = b.groups.includes(g) && (g === "settings" || g === "repos");
      this.cfgDlg = { mode: "restore", backup: b, available: b.groups, groups, done: null, busy: false, error: "",
                      plan: null, planError: "", keep: {} };
      try {
        const plan = await getJson(`/api/config-backups/${encodeURIComponent(b.id)}/preview`);
        if (this.cfgDlg && this.cfgDlg.backup && this.cfgDlg.backup.id === b.id) this._cfgSetPlan(plan);
      } catch (e) { if (this.cfgDlg) this.cfgDlg.planError = e.message || String(e); }
    },
    // Risky items start as "keep current": nothing that changes who can get in or what may change
    // happens unless it is unticked.
    _cfgSetPlan(plan) {
      const d = this.cfgDlg;
      d.plan = plan;
      d.keep = Object.fromEntries((plan.risky || []).map((id) => [id, true]));
    },
    // The preview's items for the groups ticked, in sections.
    cfgSections() {
      const d = this.cfgDlg;
      if (!d || !d.plan) return [];
      const items = d.plan.items.filter((i) => d.groups[i.group]);
      const out = [];
      const sec = (title, list) => { if (list.length) out.push({ title, items: list }); };
      sec("Settings", items.filter((i) => i.kind === "setting" || i.id === "ui_state"));
      sec("Repositories", items.filter((i) => i.kind.startsWith("repo")));
      sec("Accounts and keys", items.filter((i) => i.id === "accounts" || i.id === "keys"));
      return out;
    },
    cfgChangeCount() { return this.cfgSections().reduce((n, s) => n + s.items.length, 0); },
    cfgKeptCount() {
      const d = this.cfgDlg;
      return this.cfgSections().reduce((n, s) => n + s.items.filter((i) => i.risky && d.keep[i.id]).length, 0);
    },
    cfgItemLabel(i) {
      if (i.kind === "setting") return CFG_SETTING_LABELS[i.label] || capFirst(i.label.split(".").pop().replace(/_/g, " "));
      if (i.kind === "repo-added") return "Adds " + i.label;
      if (i.kind === "repo-removed") return "Removes " + i.label;
      if (i.kind === "repo-field") return i.repo_name + ": " + (CFG_REPO_FIELDS[i.label] || i.label);
      return i.label;
    },
    cfgVal(v, i) {
      if (v === null || v === undefined || v === "") return "default";
      if (i && /write_mode$/.test(i.label) && typeof v === "string") return this.wmLabel(v);
      if (v === true) return "On";
      if (v === false) return "Off";
      if (Array.isArray(v)) return v.length ? v.join(", ") : "none";
      if (typeof v === "object") return JSON.stringify(v);
      return String(v);
    },
    cfgItemChange(i) {
      if (i.id === "ui_state") return countOf(i.backup, "table") + " laid out differently";
      if (i.id === "keys") return "The backup's key files replace today's";
      if (i.kind === "repo-added" || i.kind === "repo-removed") return "";
      return [this.cfgVal(i.now, i), this.cfgVal(i.backup, i)];
    },
    cfgChosen() {
      const d = this.cfgDlg;
      return d ? CFG_GROUPS.map((x) => x[0]).filter((g) => d.groups[g] && d.available.includes(g)) : [];
    },
    async cfgApply() {
      const d = this.cfgDlg;
      if (!d || d.busy || !this.cfgChosen().length) return;
      d.busy = true; d.error = "";
      try {
        const keep = Object.keys(d.keep || {}).filter((id) => d.keep[id]);
        const out = d.mode === "restore"
          ? await _cfgPost(`/api/config-backups/${encodeURIComponent(d.backup.id)}/restore`, { groups: this.cfgChosen(), keep })
          : await _cfgPost("/api/config-backups/import/apply", { token: d.preview.token, groups: this.cfgChosen(), keep });
        const names = this.cfgChosen().map((g) => CFG_GROUPS.find((x) => x[0] === g)[1].toLowerCase());
        d.done = { ...out, label: capFirst(names.length > 1 ? names.slice(0, -1).join(", ") + " and " + names.at(-1) : names[0]) };
        this.loadCfg();
      } catch (e) { d.error = e.message || String(e); }
      finally { d.busy = false; }
    },
    // Settings' Restart: confirm with what happens on this install, then reload once it's back.
    async appRestart() {
      const kind = this.host && this.host.restart;
      if (!kind) return;
      const running = (this.runningJobs || []).filter((j) => j.state === "running").length;
      const how = {
        desktop: "KopiaLens closes and starts again.",
        addon: "KopiaLens stops, and Home Assistant starts it again.",
        container: "KopiaLens stops, and Docker starts it again. That needs a restart rule on the container, such as restart: unless-stopped.",
      }[kind];
      const ok = await this.klConfirm({
        title: "Restart KopiaLens?", body: `${how} This page reconnects by itself.`,
        callout: running ? `${countOf(running, "running job")} will stop.` : "",
        confirmLabel: "Restart", danger: running > 0,
      });
      if (!ok) return;
      let r = null;
      try { r = await getJson("/api/app/restart", { method: "POST" }); } catch (e) { this.klNotify(e.message || String(e), "error"); return; }
      if (!r || !r.ok) { this.klNotify("Restart KopiaLens from the service that runs it.", "warn"); return; }
      this.showSettings = false;
      this.klNotify("Restarting KopiaLens…");
      this._reloadWhenBack();
    },
    // Wait for the server to go away (or 15 s), then for it to answer again, then reload.
    async _reloadWhenBack() {
      const up = async () => { try { await getJson("/health", { cache: "no-store" }); return true; } catch (e) { return false; } };
      const wait = (ms) => new Promise((res) => setTimeout(res, ms));
      const start = Date.now();
      while (Date.now() - start < 15000 && await up()) await wait(1000);
      while (Date.now() - start < 120000) {
        if (await up()) { location.reload(); return; }
        await wait(1500);
      }
      this.klNotify("KopiaLens hasn't come back yet. Reload the page in a moment.", "warn");
    },

    // Start at logon (Settings > Desktop app): the desktop app holds it; the server passes it on.
    async loadAutostart() {
      try { this.autostart = await getJson("/api/app/autostart"); } catch (e) { this.autostart = null; }
    },
    async setAutostart(on) {
      if (this.autostartBusy) return;
      this.autostartBusy = true;
      try {
        let s = await getJson("/api/app/autostart", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on }) });
        for (let i = 0; i < 20 && s.pending; i++) {
          await new Promise((res) => setTimeout(res, 500));
          s = await getJson("/api/app/autostart");
        }
        this.autostart = s;
        if (s.pending) this.klNotify("The desktop app didn't answer. Try again in a moment.", "warn");
        else if (s.on !== on) this.klNotify("Couldn't change Start at logon.", "warn");
      } catch (e) {
        this.klNotify(e.message || String(e), "error");
      } finally {
        this.autostartBusy = false;
      }
    },

    async cfgRestart() {
      try {
        const r = await _cfgPost("/api/app/restart");
        if (r.ok) {
          this.cfgDlg = null;
          this.klNotify("Restarting KopiaLens…");
          this._reloadWhenBack();
        } else {
          this.cfgDlg.error = "Restart KopiaLens from its tray icon or the service that runs it.";
        }
      } catch (e) { this.cfgDlg.error = e.message || String(e); }
    },

    // ---- export / import ------------------------------------------------------------------
    cfgExportUrl() {
      const g = ["settings", "repos"];
      if (this.cfgExportAccounts) g.push("accounts");
      if (this.cfgExportKeys) g.push("keys");
      return "/api/config-backups/export?groups=" + g.join(",");
    },
    cfgPickImport() { this.$refs.cfgImportFile && this.$refs.cfgImportFile.click(); },
    async cfgImportFile(ev) {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = "";
      if (!file) return;
      this.cfgBusy = "import";
      try {
        const preview = await getJson("/api/config-backups/import", { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
        const groups = {};
        for (const [g] of CFG_GROUPS) groups[g] = preview.groups.includes(g) && (g === "settings" || g === "repos");
        this.cfgDlg = { mode: "import", preview, available: preview.groups, groups, done: null, busy: false, error: "",
                        plan: null, planError: "", keep: {} };
        this._cfgSetPlan(preview.plan || { items: [], risky: [] });
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
      finally { this.cfgBusy = ""; }
    },

    // ---- damaged-file notice ----------------------------------------------------------------
    cfgNoticeText(n) {
      const what = CFG_FILE[n.file] || n.file;
      return n.kind === "recovered"
        ? `Your ${what} was damaged. KopiaLens restored the last good copy, from ${fmtAbs(n.backup_at)}.`
        : `Your ${what} was damaged and no backup had a good copy, so it started from defaults.`;
    },
    async cfgDismiss(n) {
      this.cfgNotices = this.cfgNotices.filter((x) => x.id !== n.id);
      _cfgPost(`/api/config-backups/notices/${encodeURIComponent(n.id)}/dismiss`).catch(() => {});
    },
    // ---- index backups (Settings > Indexing) and the damaged-index notice ----------------------
    async loadIxNotices() {
      try { this.ixNotices = (await getJson("/api/index/notices")).notices || []; } catch (e) { /* keep */ }
    },
    ixNoticeText(n) {
      const from = n.version_at ? `the copy from ${fmtAbs(n.version_at)}` : "the last good copy";
      return `The index for ${this.repoLabel(n.repo)} was damaged. KopiaLens switched to ${from}.`;
    },
    ixDismiss(n) {
      this.ixNotices = this.ixNotices.filter((x) => x.id !== n.id);
      _cfgPost(`/api/index/notices/${encodeURIComponent(n.id)}/dismiss`).catch(() => {});
    },
    async loadIxBackups() {
      try { this.ixBackups = await getJson("/api/index/backups"); } catch (e) { this.ixBackups = null; }
    },
    ixVersionsText() {
      const repos = (this.ixBackups && this.ixBackups.repos) || [];
      const bytes = repos.reduce((t, r) => t + (r.versions || []).reduce((s, v) => s + (v.bytes || 0), 0), 0);
      return bytes ? `Older versions use ${fmtBytes(bytes)} now.` : "";
    },
    ixCopyLines() {
      return ((this.ixBackups && this.ixBackups.repos) || []).filter((r) => r.copy).map((r) => {
        const c = r.copy;
        const text = c.ok
          ? `${this.repoLabel(r.repo)}: copied ${fmtAbs(c.at)}` + (c.bytes ? `, ${fmtBytes(c.bytes)}` : "")
          : `${this.repoLabel(r.repo)}: couldn't copy. ${c.error || ""}`;
        return { repo: r.repo, ok: !!c.ok, text };
      });
    },
    ixCopyBusy() { return this.ixCopyRunning || !!(this.ixBackups && this.ixBackups.copying); },
    async ixCopyNow() {
      if (this.ixCopyBusy()) return;
      try {
        await this.saveKl("index_backup_dir");   // a folder typed just now, before the click
        await _cfgPost("/api/index/backups/copy");
        this.klNotify("Copying the index…");
      } catch (e) { this.klNotify(`Couldn't start the copy: ${e.message || e}`, "warn"); return; }
      this.ixCopyRunning = true;
      while (this.ixCopyRunning) {
        await new Promise((r) => setTimeout(r, 2000));
        await this.loadIxBackups();
        if (!(this.ixBackups && this.ixBackups.copying)) this.ixCopyRunning = false;
      }
    },
    cfgOpenBackups() {
      this.openSettings();
      this.settingsTab = "backups";
    },

    // ---- pause indexing -------------------------------------------------------------------
    pauseText() {
      const p = this.indexPause;
      if (!p || !p.paused) return "";
      return p.until ? `Indexing paused until ${fmtAbs(p.until)}` : "Indexing paused until you resume";
    },
    async pauseIndexing(hours) {
      if (hours === "custom") { hours = await this.pauseCustomHours("Pause indexing"); if (hours == null) return; }
      try { this.indexPause = await _cfgPost("/api/index/pause", { hours }); }
      catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    async resumeIndexing() {
      try {
        this.indexPause = await _cfgPost("/api/index/resume");
        this.klNotify("Indexing resumed");
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },

    // ---- about ----------------------------------------------------------------------------
    async loadAbout() {
      try { this.about = await getJson("/api/app/about"); } catch (e) { /* section shows Loading… */ }
    },
    aboutText() {
      const a = this.about;
      if (!a) return "";
      const k = a.kopia || {};
      return [`KopiaLens ${a.version}${a.commit ? ` (${a.commit})` : ""}`, `kopia ${k.version || "unknown"} (${k.path || "not found"})`,
        `Data format ${a.data_format}`, `${a.os} · Python ${a.python}${a.compiled ? " · compiled" : ""}`,
        `Running as ${a.profile === "desktop" ? "desktop app" : "server"}`, `Data folder ${a.data_dir}`,
        `HTTPS certificates: ${a.certificates || "unknown"}`].join("\n");
    },
    async copyAbout() {
      try { await navigator.clipboard.writeText(this.aboutText()); this.klNotify("Copied"); }
      catch (e) { this.klNotify("Couldn't copy. Select the text instead.", "warn"); }
    },
};
