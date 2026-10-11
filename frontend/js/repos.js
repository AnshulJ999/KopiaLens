// Repositories: the connect form shared by onboarding and "Add repository" (cx*), the repo menu in
// the top bar, and Settings -> Repositories (ra*). Backend: app/api/setup.py.
const _setupPost = (url, body, method = "POST") => getJson(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

// Where a repository lives, from its scrubbed storage config.
const repoWhereOf = (type, c) => {
  c = c || {};
  const join = (...xs) => xs.filter(Boolean).join("/");
  switch (type) {
    case "filesystem": return c.path || "";
    case "sftp": return `${c.host || ""}${c.path ? ":" + c.path : ""}`;
    case "s3": case "b2": case "gcs": return join(c.bucket, c.prefix);
    case "azureBlob": return join(c.container, c.prefix);
    case "webdav": return c.url || "";
    case "rclone": return c.remotePath || "";
    case "gdrive": return c.folderID || "";
    case "server": return c.url || "";
    default: return "";
  }
};
const REPO_SHORT_TYPES = {
  filesystem: "Folder", sftp: "SFTP", s3: "S3", b2: "B2", gcs: "GCS", azureBlob: "Azure",
  webdav: "WebDAV", rclone: "Rclone", gdrive: "Drive", server: "Server",
};
const REPO_TYPE_LABELS = {
  filesystem: "Folder", sftp: "SFTP", s3: "S3", b2: "Backblaze B2", gcs: "Google Cloud Storage",
  azureBlob: "Azure", webdav: "WebDAV", rclone: "Rclone", gdrive: "Google Drive", server: "Kopia server",
};

const reposPart = {
    sampleBusy: false,

    // ---- Sample data: an invented repository made on this computer (backend: sampledata.py) --
    hasSample() { return (this.repos || []).some((r) => r.sample); },
    isSample(id) {
      const r = (this.repos || []).find((x) => x.id === (id || this.repo));
      return !!(r && r.sample);
    },
    async addSampleData() {
      await _setupPost("/api/setup/sample");
      for (;;) {
        await new Promise((ok) => setTimeout(ok, 1500));
        const st = await getJson("/api/setup/sample");
        if (st.state === "error") throw new Error(st.error || "The sample data couldn't be made.");
        if (st.state === "done") {
          await this._cxRefreshRepos();
          return (this.repos || []).find((r) => r.id === st.repo) || { id: st.repo, name: st.name, sample: true };
        }
      }
    },
    async raAddSample() {
      if (this.sampleBusy) return;
      this.sampleBusy = true;
      try {
        const r = await this.addSampleData();
        this.klNotify(`${r.name} added`);
        this.raLoad();
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
      this.sampleBusy = false;
    },

    // ---- The connect form (partials/connectform.html) ---------------------------------------
    async cxOpen(existing) {
      this.cx = {
        loading: true, error: "", busy: false,
        detected: [], picked: {}, providers: [],
        mode: "", provider: "", fields: {}, password: "", name: "", serverOk: false,
        added: (existing || []).map((r) => this._cxEntry(r)),
      };
      try {
        const [d, p] = await Promise.all([getJson("/api/setup/detect-configs"), getJson("/api/setup/providers")]);
        this.cx.detected = d.configs || [];
        this.cx.providers = p.providers || [];
      } catch (e) { this.cx.error = e.message || String(e); }
      this.cx.loading = false;
      for (const a of this.cx.added) this.cxSummary(a);
    },
    _cxEntry(r) {
      const st = r.storage || {};
      return { id: r.id, name: r.name || "", short_name: r.short_name || "", type: st.type || "",
               where: repoWhereOf(st.type, st.config), summary: null, loading: false, error: "",
               pw: "", unlocking: false, managed: r.source === "connected" || r.source === "created",
               server: !!r.via_server || st.type === "server", sample: !!r.sample };
    },
    cxCandidates() { return ((this.cx && this.cx.detected) || []).filter((c) => !c.in_store); },
    cxPickedCount() { return this.cxCandidates().filter((c) => this.cx.picked[c.config_file]).length; },
    cxTypeLabel(type) { return REPO_TYPE_LABELS[type] || type || "Unknown storage"; },
    cxWhere(c) { return repoWhereOf(c.type, c.config); },
    cxDefaultName(type, config, fallback) {
      const where = repoWhereOf(type, config);
      const tail = where.replace(/[\\/:]+$/, "").split(/[\\/:]/).filter(Boolean).pop();
      const taken = new Set((this.repos || []).map((r) => r.name).concat(((this.cx && this.cx.added) || []).map((a) => a.name)));
      let name = tail || fallback || this.cxTypeLabel(type);
      for (let i = 2; taken.has(name); i++) name = `${tail || fallback} ${i}`;
      return name;
    },
    // Suggested chip label: the folder or bucket name when it fits in 12 characters, else the storage type.
    cxDefaultShort(type, config) {
      // A URL's port is not a name: "https://nas:51515" suggests "nas".
      const where = repoWhereOf(type, config).replace(/[\\/:]+$/, "").replace(/:\d+$/, "");
      const tail = where.split(/[\\/:]/).filter(Boolean).pop() || "";
      const base = tail && tail.length <= 12 ? tail : (REPO_SHORT_TYPES[type] || "");
      if (!base) return "";
      const taken = new Set((this.repos || []).map((r) => r.short_name).concat(((this.cx && this.cx.added) || []).map((a) => a.short_name)));
      let label = base;
      for (let i = 2; taken.has(label); i++) label = `${base.slice(0, 12 - String(i).length - 1)} ${i}`;
      return label;
    },
    async cxUseSelected() {
      const cx = this.cx;
      const picked = this.cxCandidates().filter((c) => cx.picked[c.config_file]);
      const vault = picked.find((c) => c.folder === "blinkdisk");
      const chosen = picked.filter((c) => c.folder !== "blinkdisk");
      if (vault && !chosen.length) return this.cxFromVault(vault);
      if (!chosen.length || cx.busy) return;
      cx.busy = true; cx.error = "";
      for (const c of chosen) {
        try {
          const name = this.cxDefaultName(c.type, c.config, c.filename.replace(/\.config$/, ""));
          const r = await _setupPost("/api/setup/attach", { config_file: c.config_file, name, short_name: this.cxDefaultShort(c.type, c.config) });
          cx.added.push(this._cxEntry(r.repo));
          c.in_store = true;
          delete cx.picked[c.config_file];
          this.cxSummary(cx.added[cx.added.length - 1]);   // the tracked copy, so the card updates
        } catch (e) { cx.error = `${c.filename}: ${e.message || e}`; }
      }
      cx.busy = false;
      await this._cxRefreshRepos();
      if (vault) this.cxFromVault(vault);
    },
    // A BlinkDisk vault: KopiaLens makes its own connection (the vault password lives in BlinkDisk's
    // store), so the connect form opens with the vault's location filled in.
    cxFromVault(c) {
      const cx = this.cx;
      delete cx.picked[c.config_file];
      this.cxStart("connect");
      this.cxSetProvider(c.type);
      for (const [k, v] of Object.entries(c.config || {})) if (k in cx.fields && v !== "***") cx.fields[k] = v;
      cx.name = "BlinkDisk";
    },
    cxStart(mode) {
      const cx = this.cx;
      cx.mode = mode; cx.error = "";
      const list = this.cxProviderList();
      if (!list.some((p) => p.key === cx.provider)) this.cxSetProvider(list.length ? list[0].key : "");
    },
    cxProviderList() {
      const all = (this.cx && this.cx.providers) || [];
      return this.cx.mode === "create" ? all.filter((p) => p.can_create) : all.filter((p) => p.key !== "token");
    },
    cxSpec() { return ((this.cx && this.cx.providers) || []).find((p) => p.key === this.cx.provider) || null; },
    cxSetProvider(key) {
      const cx = this.cx;
      cx.provider = key; cx.error = ""; cx.serverOk = false;
      const spec = this.cxSpec();
      cx.fields = {};
      for (const f of (spec ? spec.fields : [])) cx.fields[f.key] = f.default == null ? (f.kind === "bool" ? false : "") : f.default;
    },
    cxFieldList(advanced) { const s = this.cxSpec(); return s ? s.fields.filter((f) => !!f.advanced === advanced) : []; },
    cxPasswordNote() {
      if (this.cx.provider === "server") return "Your account's password on the kopia server. Not your KopiaLens password.";
      return this.cx.mode === "create"
        ? "kopia encrypts the repository with this password. Without it the backups can't be read, so keep it safe. It's not your KopiaLens password."
        : "kopia's own password for this repository. Not your KopiaLens password.";
    },
    cxInputType(f) { return f.kind === "password" ? "password" : f.kind === "int" ? "number" : "text"; },
    async cxSubmit() {
      const cx = this.cx;
      const spec = this.cxSpec();
      if (!spec || cx.busy || (spec.key === "server" && !cx.serverOk)) return;
      const fields = {};
      for (const f of spec.fields) {
        const v = cx.fields[f.key];
        if (f.kind === "bool") { if (v !== (f.default || false)) fields[f.key] = !!v; continue; }
        if (v === "" || v == null) continue;
        fields[f.key] = f.kind === "int" ? Number(v) : v;
      }
      const cfg = { path: fields.path, bucket: fields.bucket, prefix: fields.prefix, host: fields.host, url: fields.url,
                    container: fields.container, remotePath: fields.remote_path, folderID: fields.folder_id };
      const name = cx.name.trim() || this.cxDefaultName(spec.key === "azure" ? "azureBlob" : spec.key, cfg, spec.label);
      cx.busy = true; cx.error = "";
      try {
        const url = cx.mode === "create" ? "/api/setup/create" : "/api/setup/connect";
        const short_name = this.cxDefaultShort(spec.key === "azure" ? "azureBlob" : spec.key, cfg);
        const r = await _setupPost(url, { provider: spec.key, fields, password: cx.password, name, short_name });
        cx.added.push({ ...this._cxEntry(r.repo), managed: true });
        cx.mode = ""; cx.password = ""; cx.name = "";
        this.cxSummary(cx.added[cx.added.length - 1]);   // the tracked copy, so the card updates
        await this._cxRefreshRepos();
      } catch (e) { cx.error = e.message || String(e); }
      cx.busy = false;
    },
    async cxSummary(a) {
      a.loading = true; a.error = "";
      try { a.summary = await getJson(`/api/setup/repos/${encodeURIComponent(a.id)}/summary`); }
      catch (e) { a.error = e.message || String(e); }
      a.loading = false;
    },
    cxSummaryLine(a) {
      const s = a.summary;
      if (!s) return "";
      const parts = [countOf(s.sources, "source")];
      if (s.hosts > 1) parts[0] += ` (${countOf(s.hosts, "computer")})`;
      parts.push(countOf(s.snapshots, "snapshot"));
      if (s.newest) parts.push(`newest ${fmtRel(s.newest)}`);
      return parts.join(" · ");
    },
    cxEstimateLine(a) {
      const e = a.summary && a.summary.estimate;
      if (!e || a.server || a.sample || !a.summary.snapshots) return "";
      const cloud = a.type && a.type !== "filesystem" ? " Cloud storage can take longer." : "";
      return `First index: ${fmtDurRange(e.seconds)}, ${fmtBytesRange(e.bytes)} of disk space.${cloud}`;
    },
    async cxUnlock(a) {
      if (!a.pw || a.unlocking) return;
      a.unlocking = true; a.error = "";
      try {
        await _setupPost(`/api/setup/repos/${encodeURIComponent(a.id)}/unlock`, { password: a.pw });
        a.pw = "";
        await this.cxSummary(a);
      } catch (e) { a.error = e.message || String(e); }
      a.unlocking = false;
    },
    async cxRename(a) {
      try {
        await _setupPost(`/api/setup/repos/${encodeURIComponent(a.id)}`, { name: a.name.trim() || a.id, short_name: a.short_name.trim() }, "PATCH");
        await this._cxRefreshRepos();
      } catch (e) { a.error = e.message || String(e); }
    },
    async cxRemoveAdded(a) {
      const ok = await this.klConfirm({
        title: `Remove ${a.name} from KopiaLens?`,
        body: "The kopia repository itself isn't touched.",
        confirmLabel: "Remove",
      });
      if (!ok) return;
      try {
        await getJson(`/api/setup/repos/${encodeURIComponent(a.id)}?confirm=true&delete_config=${a.managed ? "true" : "false"}`, { method: "DELETE" });
        this.cx.added = this.cx.added.filter((x) => x !== a);
        this.cx.detected.forEach((c) => { if (c.repo_id === a.id) c.in_store = false; });
        await this._cxRefreshRepos();
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    async _cxRefreshRepos() {
      try { this.repos = (await getJson("/api/repos")).repos || []; } catch (e) { /* the list refreshes on the next load */ }
    },

    // ---- Top-bar repo menu ------------------------------------------------------------------
    pickRepo(id) {
      this.repoMenu = false;
      if (id === this.repo) return;
      this.repo = id;
      this.repoChanged();
    },
    openRepoAdmin() {
      this.repoMenu = false;
      this.openSettings();
      this.settingsTab = "repositories";
    },
    async openAddRepo() {
      this.repoMenu = false;
      this.repoAdd = true;
      await this.cxOpen();
    },
    async closeAddRepo() {
      this.repoAdd = false;
      const added = (this.cx && this.cx.added) || [];
      this.cx = null;
      if (added.length) {
        await this._cxRefreshRepos();
        this.klNotify(added.length === 1 ? `${added[0].name} added` : `${added.length} repositories added`);
        this.raLoad();
      }
    },

    // ---- Settings -> Repositories -----------------------------------------------------------
    async raLoad() {
      this.loadFreshness();
      try { this.kopiaInfo = await getJson("/api/setup/kopia"); } catch (e) { this.kopiaInfo = null; }
    },
    raIndexLine(r) {
      if (r.kind === "backblaze") return "Backblaze's own index, read-only";
      if (r.sample) return "Sample data, not a backup";
      if (r.via_server) return "No index through a kopia server";
      const f = (this.freshness || []).find((x) => x.repo === r.id);
      if (!f) return "";
      if (f.running) return "Indexing now";
      if (f.error) return "Last index failed";
      return f.last_index ? `Indexed ${fmtRel(f.last_index)}` : "Not indexed yet";
    },
    raWhere(r) {
      if (r.sample) return "Made on this computer";
      const st = r.storage || {};
      const where = repoWhereOf(st.type, st.config);
      return [this.cxTypeLabel(st.type), where].filter(Boolean).join(" · ");
    },
    raPill(r) {
      const chk = this.repoChecks[r.id];
      if (chk && chk.busy) return ["Checking…", ""];
      if (r.via_server || (r.storage || {}).type === "server") return ["Browse-only", "warn"];
      if (chk && chk.ok) return ["Connected", "ok"];
      if (chk && chk.error) return ["Can't connect", "bad"];
      return null;
    },
    async raCheck(r) {
      this.repoChecks = { ...this.repoChecks, [r.id]: { busy: true, pw: "" } };
      let res;
      try { res = await _setupPost("/api/setup/probe", { repo: r.id }); }
      catch (e) { res = { ok: false, error: e.message || String(e) }; }
      this.repoChecks = { ...this.repoChecks, [r.id]: { busy: false, ok: res.ok, error: res.error || "", pw: "" } };
    },
    async raUnlock(r) {
      const chk = this.repoChecks[r.id];
      if (!chk || !chk.pw) return;
      try {
        await _setupPost(`/api/setup/repos/${encodeURIComponent(r.id)}/unlock`, { password: chk.pw });
        this.klNotify("Password saved. kopia remembers it now.");
        await this.raCheck(r);
      } catch (e) { this.repoChecks = { ...this.repoChecks, [r.id]: { ...chk, error: e.message || String(e) } }; }
    },
    async raIndex(r) {
      if (r.id === this.repo) return this.buildIndex();
      try {
        const res = await getJson(`/api/index/build?repo=${encodeURIComponent(r.id)}`, { method: "POST" });
        if (res && res.started === false) throw new Error(res.reason || "The index didn't start.");
        this.klNotify(`Indexing ${r.name}`);
        this.loadFreshness();
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    async raCountsAsBackup(r, on) {
      try {
        await _setupPost(`/api/setup/repos/${encodeURIComponent(r.id)}`, { counts_as_backup: on }, "PATCH");
        r.counts_as_backup = on;
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    raMove(i, dir) {
      const j = i + dir;
      if (j < 0 || j >= this.repos.length) return;
      const list = this.repos.slice();
      [list[i], list[j]] = [list[j], list[i]];
      this.repos = list;
      this.klSet({ repo_order: list.map((r) => r.id) });
    },
    raEdit(r) { this.repoDlg = { mode: "edit", repo: r, name: r.name, short_name: r.short_name || "", busy: false, error: "" }; },
    async raRemove(r) {
      let p;
      try { p = await getJson(`/api/setup/repos/${encodeURIComponent(r.id)}/remove-preview`); }
      catch (e) { this.klNotify(e.message || String(e), "warn"); return; }
      this.repoDlg = { mode: "remove", repo: r, preview: p, deleteConfig: false, busy: false, error: "" };
    },
    raRemoveFacts() {
      const d = this.repoDlg;
      if (!d || !d.preview) return [];
      const p = d.preview;
      return [
        ["KopiaLens data", (p.files || []).length ? `${fmtBytes(p.bytes || 0)} in ${countOf(p.files.length, "item")}` : "None yet"],
        ["Kopia repository", "Not touched"],
        ["Connection file", p.managed_config ? p.config_file : (p.config_file ? "Kept (KopiaLens didn't create it)" : "kopia's default, kept")],
      ];
    },
    async repoDlgSubmit() {
      const d = this.repoDlg;
      if (!d || d.busy) return;
      d.busy = true; d.error = "";
      try {
        if (d.mode === "edit") {
          if (!d.name.trim()) throw new Error("Enter a name.");
          await _setupPost(`/api/setup/repos/${encodeURIComponent(d.repo.id)}`, { name: d.name.trim(), short_name: d.short_name.trim() }, "PATCH");
          await this._cxRefreshRepos();
          this.repoDlg = null;
          this.klNotify("Saved");
          return;
        }
        const res = await getJson(`/api/setup/repos/${encodeURIComponent(d.repo.id)}?confirm=true&delete_config=${d.deleteConfig ? "true" : "false"}`, { method: "DELETE" });
        this.repoDlg = null;
        await this._cxRefreshRepos();
        const left = (res.failed || []).length;
        this.klNotify(left ? `${d.repo.name} removed. ${countOf(left, "file")} couldn't be deleted; see the log.` : `${d.repo.name} removed`, left ? "warn" : "");
        if (!this.repos.some((r) => r.kind !== "backblaze")) { location.reload(); return; }
        if (!this.repos.some((r) => r.id === this.repo)) this.pickRepo((this.repos.find((r) => r.kind !== "backblaze") || this.repos[0]).id);
      } catch (e) {
        d.error = e.message || String(e);
      } finally {
        if (this.repoDlg) this.repoDlg.busy = false;
      }
    },
    raKopiaLine() {
      const k = this.kopiaInfo;
      if (!k) return "";
      if (!k.available) return "Not found";
      const src = { bundled: "bundled", setting: "chosen here", environment: "set by environment", PATH: "on PATH", "install location": "installed", KopiaUI: "from KopiaUI" }[k.source] || k.source || "";
      return [k.version ? `kopia ${k.version}` : "kopia", src].filter(Boolean).join(" · ");
    },
    async raKopiaSave(path) {
      try {
        this.kopiaInfo = { ...(await _setupPost("/api/setup/kopia", { path: (path || "").trim() })), found: (this.kopiaInfo || {}).found };
        this.repoKopiaEdit = null;
        this.klNotify("Saved");
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
};
