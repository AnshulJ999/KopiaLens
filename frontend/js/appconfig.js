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
    cfgOpenRestore(b) {
      const groups = {};
      for (const [g] of CFG_GROUPS) groups[g] = b.groups.includes(g) && (g === "settings" || g === "repos");
      this.cfgDlg = { mode: "restore", backup: b, available: b.groups, groups, done: null, busy: false, error: "" };
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
        const out = d.mode === "restore"
          ? await _cfgPost(`/api/config-backups/${encodeURIComponent(d.backup.id)}/restore`, { groups: this.cfgChosen() })
          : await _cfgPost("/api/config-backups/import/apply", { token: d.preview.token, groups: this.cfgChosen() });
        const names = this.cfgChosen().map((g) => CFG_GROUPS.find((x) => x[0] === g)[1].toLowerCase());
        d.done = { ...out, label: capFirst(names.length > 1 ? names.slice(0, -1).join(", ") + " and " + names.at(-1) : names[0]) };
        this.loadCfg();
      } catch (e) { d.error = e.message || String(e); }
      finally { d.busy = false; }
    },
    async cfgRestart() {
      try {
        const r = await _cfgPost("/api/app/restart");
        if (r.ok) {
          this.cfgDlg = null;
          this.klNotify("Restarting KopiaLens…");
          setTimeout(() => location.reload(), 6000);
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
        this.cfgDlg = { mode: "import", preview, available: preview.groups, groups, done: null, busy: false, error: "" };
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
      finally { this.cfgBusy = ""; }
    },
    cfgFileSummary(f) {
      if (f.file === "settings.json") return f.changed && f.changed.length ? countOf(f.changed.length, "setting") + " differ" : "Same as now";
      if (f.file === "repos.json") {
        const parts = [];
        if (f.added && f.added.length) parts.push("adds " + f.added.join(", "));
        if (f.removed && f.removed.length) parts.push("removes " + f.removed.join(", "));
        return parts.length ? capFirst(parts.join("; ")) : "Same repositories as now";
      }
      if (f.file === "users.json") return "Accounts: " + (f.accounts || []).join(", ");
      return "Replaces the current file";
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
      return [`KopiaLens ${a.version}`, `kopia ${k.version || "unknown"} (${k.path || "not found"})`,
        `Data format ${a.data_format}`, `${a.os} · Python ${a.python}${a.compiled ? " · compiled" : ""}`,
        `Running as ${a.profile === "desktop" ? "desktop app" : "server"}`, `Data folder ${a.data_dir}`].join("\n");
    },
    async copyAbout() {
      try { await navigator.clipboard.writeText(this.aboutText()); this.klNotify("Copied"); }
      catch (e) { this.klNotify("Couldn't copy. Select the text instead.", "warn"); }
    },
};
