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
const REPO_TYPE_LABELS = {
  filesystem: "Folder", sftp: "SFTP", s3: "S3", b2: "Backblaze B2", gcs: "Google Cloud Storage",
  azureBlob: "Azure", webdav: "WebDAV", rclone: "Rclone", gdrive: "Google Drive", server: "Kopia server",
};

const reposPart = {
    sampleBusy: false,
    sampleProg: null,   // {step, started} while the sample is being made

    // ---- Sample data: an invented repository made on this computer (backend: sampledata.py) --
    hasSample() { return (this.repos || []).some((r) => r.sample); },
    sampleProgText() {
      const p = this.sampleProg;
      if (!p) return "";
      const step = { making: "Making the sample", checking: "Checking it", adding: "Adding it" }[p.step] || "Starting";
      return `${step}… · ${fmtDur((Date.now() - p.started) / 1000)}`;
    },
    hasKopiaRepo() { return (this.repos || []).some((r) => r.kind !== "backblaze"); },
    isSample(id) {
      const r = (this.repos || []).find((x) => x.id === (id || this.repo));
      return !!(r && r.sample);
    },
    async addSampleData() {
      const started = Date.now();
      this.sampleProg = { step: "", started };
      try {
        await _setupPost("/api/setup/sample");
        for (;;) {
          await new Promise((ok) => setTimeout(ok, 1000));
          const st = await getJson("/api/setup/sample");
          if (st.state === "error") throw new Error(st.error || "The sample data couldn't be made.");
          if (st.state === "done") {
            await this._cxRefreshRepos();
            return (this.repos || []).find((r) => r.id === st.repo) || { id: st.repo, name: st.name, sample: true };
          }
          this.sampleProg = { step: st.step || "", started };
        }
      } finally {
        this.sampleProg = null;
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
        detected: [], container: null, picked: {}, providers: [], bz: null, bzPicked: false, confirm: "", format: {}, formatChoices: {},
        mode: "", provider: "", fields: {}, password: "", name: "", serverOk: false,
        cfgLoc: "data", cfgFolder: "", kopiaFolderOk: true,
        added: (existing || []).map((r) => this._cxEntry(r)),
      };
      try {
        const [d, p, bz] = await Promise.all([getJson("/api/setup/detect-configs"), getJson("/api/setup/providers"),
                                              getJson("/api/setup/backblaze").catch(() => null)]);
        this.cx.detected = d.configs || [];
        this.cx.container = d.container || null;
        this.cx.providers = p.providers || [];
        this.cx.cfgLoc = p.config_location || "data";
        this.cx.kopiaFolderOk = p.kopia_folder_ok !== false;
        this.cx.formatChoices = p.format_choices || {};
        this.cx.bz = bz;
        this.cx.bzPicked = !!(bz && bz.found && !bz.added);   // found = offered ticked
        Object.assign(this.cx, { bzOther: false, bzFolder: "", bzFolderError: "", bzFolderBusy: false });
      } catch (e) { this.cx.error = e.message || String(e); }
      this.cx.loading = false;
      for (const a of this.cx.added) this.cxSummary(a);
    },
    _cxEntry(r) {
      const st = r.storage || {};
      return { id: r.id, name: r.name || "", short_name: r.short_name || "", type: st.type || "",
               where: repoWhereOf(st.type, st.config), summary: null, loading: false, error: "",
               pw: "", unlocking: false, managed: r.source === "connected" || r.source === "created",
               created: r.source === "created", server: !!r.via_server || st.type === "server",
               sample: !!r.sample, backblaze: r.kind === "backblaze" };
    },
    // Setup without kopia: only Backblaze can be added until kopia is found.
    cxKopiaOk() { return !(this.ob && this.obKopiaMissing()); },
    cxCandidates() { return this.cxKopiaOk() ? ((this.cx && this.cx.detected) || []).filter((c) => !c.in_store) : []; },
    cxBzOffered() { const b = this.cx && this.cx.bz; return !!(b && b.found && !b.added); },
    // Docker: kopia repositories found in the folders attached to the container.
    cxFolders() {
      const c = this.cx && this.cx.container;
      return this.cxKopiaOk() && c ? (c.folders || []).filter((f) => !f.in_store) : [];
    },
    cxNothingAttached() {
      const c = this.cx && this.cx.container;
      return !!(c && c.docker && !(c.attached || []).length) && !this.cxCandidates().length
        && !this.cx.added.some((a) => !a.backblaze);
    },
    // A found folder needs its password, so the connect form opens with the folder filled in.
    cxFromFolder(f) {
      this.cxStart("existing");
      this.cxSetProvider("filesystem");
      this.cx.fields.path = f.path;
      setTimeout(() => { const el = document.querySelector('[data-testid="cx-password"]'); if (el) el.focus(); }, 50);
    },
    cxPickedCount() {
      return this.cxCandidates().filter((c) => this.cx.picked[c.config_file]).length + (this.cxBzOffered() && this.cx.bzPicked ? 1 : 0);
    },
    async cxBzFolder() {   // Backblaze on another computer: its shared data folder
      const cx = this.cx;
      cx.bzFolderBusy = true; cx.bzFolderError = "";
      try {
        const bz = await _setupPost("/api/setup/backblaze/folder", { path: (cx.bzFolder || "").trim() });
        cx.bz = bz; cx.bzPicked = !!(bz.found && !bz.added); cx.bzOther = false;
      } catch (e) { cx.bzFolderError = e.message || String(e); }
      cx.bzFolderBusy = false;
    },
    async cxBzRecheck() {   // after the user allows access to a blocked Backblaze folder
      const cx = this.cx;
      cx.bzFolderBusy = true;
      try {
        const bz = await getJson("/api/setup/backblaze");
        cx.bz = bz; cx.bzPicked = !!(bz.found && !bz.added);
      } catch (e) { cx.bzFolderError = e.message || String(e); }
      cx.bzFolderBusy = false;
    },
    async cxAddBackblaze() {
      const cx = this.cx;
      const r = await _setupPost("/api/setup/backblaze", { index: !this.ob });
      cx.bz = { ...cx.bz, added: true };
      cx.bzPicked = false;
      cx.added.push({ ...this._cxEntry(r.repo), summary: { drives: r.drives } });
    },
    cxTypeLabel(type) { return REPO_TYPE_LABELS[type] || type || "Unknown storage"; },
    cxWhere(c) { return repoWhereOf(c.type, c.config); },
    // A new repository's name: kopia's own description when the connection has one, else the
    // storage type and where it is ("Local folder (E:\Kopia)", "S3 (my-bucket)").
    cxDefaultName(type, config, fallback, description = "") {
      const where = repoWhereOf(type, config);
      const kind = type === "filesystem" ? (isUnc(where) ? "Network folder" : "Local folder") : this.cxTypeLabel(type);
      const base = (description || "").trim() || (where ? `${kind} (${where})` : (fallback || kind));
      const taken = new Set((this.repos || []).map((r) => r.name).concat(((this.cx && this.cx.added) || []).map((a) => a.name)));
      let name = base;
      for (let i = 2; taken.has(name); i++) name = `${base} ${i}`;
      return name;
    },
    async cxUseSelected() {
      const cx = this.cx;
      const picked = this.cxCandidates().filter((c) => cx.picked[c.config_file]);
      const vault = picked.find((c) => c.folder === "blinkdisk");
      const chosen = picked.filter((c) => c.folder !== "blinkdisk");
      const bz = this.cxBzOffered() && cx.bzPicked;
      if (vault && !chosen.length && !bz) return this.cxFromVault(vault);
      if ((!chosen.length && !bz) || cx.busy) return;
      cx.busy = true; cx.error = "";
      if (bz) {
        try { await this.cxAddBackblaze(); } catch (e) { cx.error = `Backblaze: ${e.message || e}`; }
      }
      for (const c of chosen) {
        try {
          const name = this.cxDefaultName(c.type, c.config, c.filename.replace(/\.config$/, ""), c.description);
          const body = { config_file: c.config_file, name };   // the server names it "Repo <id>"
          const r = await this._cxAdd("/api/setup/attach", body);
          if (!r) continue;
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
      cx.mode = mode; cx.error = ""; cx.confirm = "";
      cx.format = Object.fromEntries(Object.entries(cx.formatChoices || {}).map(([k, v]) => [k, v[0]]));
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
    // Only choices that differ from kopia's defaults are sent; kopia applies its own for the rest.
    cxFormatOptions() {
      const out = {};
      for (const [k, v] of Object.entries(this.cx.format || {})) {
        if (v === ((this.cx.formatChoices || {})[k] || [])[0]) continue;
        out[k] = ["ecc_overhead_percent", "format_version"].includes(k) ? Number(v) : v;
      }
      return out;
    },
    cxFormatLabel(key, v) {
      if (key === "ecc_overhead_percent") return v === "0" ? "Off" : `${v}% extra space`;
      if (key === "format_version") return v === "0" ? "Latest" : `Version ${v}`;
      return v;
    },
    cxFieldList(advanced) {
      const s = this.cxSpec();
      return s ? s.fields.filter((f) => !!f.advanced === advanced && !f.identity && !(f.connect_only && this.cx.mode === "create")) : [];
    },
    // "Connect as": the user and computer names kopia connects as, shown side by side.
    cxIdentityFields() {
      const s = this.cxSpec();
      return s ? s.fields.filter((f) => f.identity) : [];
    },
    cxPasswordNote() {
      if (this.cx.provider === "server") return "Your account's password on the kopia server. Not your KopiaLens password.";
      return this.cx.mode === "create"
        ? "kopia encrypts the repository with this password. Without it the backups can't be read, so keep it safe. It's not your KopiaLens password."
        : "kopia's own password for this repository. Not your KopiaLens password.";
    },
    cxInputType(f) { return f.kind === "password" ? "password" : f.kind === "int" ? "number" : "text"; },
    async cxBrowse(key) {
      const path = await this.pickFolder(this.cx && this.cx.fields[key] || null);
      if (path && this.cx) this.cx.fields[key] = path;
    },
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
      if (cx.mode === "create" && spec.password && cx.password !== cx.confirm) { cx.error = "The passwords don't match."; return; }
      cx.busy = true; cx.error = "";
      try {
        const url = cx.mode === "create" ? "/api/setup/create" : "/api/setup/connect";
        const r = await this._cxAdd(url, { provider: spec.key, fields, password: cx.password, name,
                                           config_location: cx.cfgLoc, config_folder: cx.cfgFolder.trim(),
                                           ...(cx.mode === "create" ? { options: this.cxFormatOptions() } : {}) });
        if (!r) { cx.busy = false; return; }
        cx.added.push({ ...this._cxEntry(r.repo), managed: true });
        for (const f of (cx.container && cx.container.folders) || []) if (f.path === fields.path) f.in_store = true;
        cx.mode = ""; cx.password = ""; cx.confirm = ""; cx.name = "";
        this.cxSummary(cx.added[cx.added.length - 1]);   // the tracked copy, so the card updates
        await this._cxRefreshRepos();
      } catch (e) { cx.error = e.message || String(e); }
      cx.busy = false;
    },
    // Connect or attach; when it's a repository KopiaLens already has, ask before adding it again.
    // Returns null when the user says no.
    async _cxAdd(url, body) {
      try {
        return await _setupPost(url, body);
      } catch (e) {
        if (e.status !== 409 || !e.data || !e.data.duplicate_of) throw e;
        const ok = await this.klConfirm({
          title: "Add this repository again?",
          body: `It's the same repository as ${e.data.duplicate_name}. Adding it again indexes it twice and shows everything twice.`,
          confirmLabel: "Add anyway", danger: false,
        });
        return ok ? _setupPost(url, { ...body, allow_duplicate: true }) : null;
      }
    },
    async cxSummary(a) {
      if (a.backblaze) {
        if (!a.summary) { try { a.summary = { drives: (await getJson("/api/setup/backblaze")).drives }; } catch (e) { a.summary = {}; } }
        return;
      }
      a.loading = true; a.error = "";
      try { a.summary = await getJson(`/api/setup/repos/${encodeURIComponent(a.id)}/summary`); }
      catch (e) { a.error = e.message || String(e); }
      a.loading = false;
    },
    cxSummaryLine(a) {
      const s = a.summary;
      if (!s) return "";
      if (a.backblaze) return s.drives ? countOf(s.drives, "drive") + " backed up" : "";
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
        if (a.backblaze && this.cx.bz) this.cx.bz = { ...this.cx.bz, added: false };
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
        if (this.noRepos && this.repos.length) { location.reload(); return; }
        this.klNotify(added.length === 1 ? `${added[0].name} added` : `${added.length} repositories added`);
        this.raLoad();
      }
    },

    // ---- Settings -> Repositories -----------------------------------------------------------
    async raLoad() {
      this.loadFreshness();
      getJson("/api/setup/backblaze").then((b) => { this.raBz = b; }).catch(() => { this.raBz = null; });
      try { this.kopiaInfo = await getJson("/api/setup/kopia"); } catch (e) { this.kopiaInfo = null; }
    },
    async raAddBackblaze() {
      if (!this.raBz || this.raBz.busy) return;
      this.raBz = { ...this.raBz, busy: true };
      try {
        await _setupPost("/api/setup/backblaze", { index: true });
        await this._cxRefreshRepos();
        this.klNotify("Backblaze added");
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
      this.raLoad();
    },
    async raRunSetup() {
      const sample = (this.repos || []).find((r) => r.sample);
      const ok = await this.klConfirm({
        title: "Run setup again?",
        body: "Your repositories, sign-in and encryption stay as they are. You can change any of them on the way.",
        note: sample ? "You can remove the sample data there too." : "",
        confirmLabel: "Run setup",
      });
      if (!ok) return;
      try {
        await _setupPost("/api/onboarding/restart");
        location.reload();
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
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
      this.repoChecks = { ...this.repoChecks, [r.id]: { busy: false, ok: res.ok, error: res.error || "", pw: "", missing: !!res.missing_config,
        reconnect: res.reconnect || { possible: true, keys: [] }, keys: {} } };
    },
    async raReconnect(r) {
      const chk = this.repoChecks[r.id];
      if (!chk || !chk.pw || chk.busy) return;
      this.repoChecks = { ...this.repoChecks, [r.id]: { ...chk, busy: true } };
      try {
        await _setupPost(`/api/setup/repos/${encodeURIComponent(r.id)}/reconnect`, { password: chk.pw, keys: chk.keys || {} });
        this.klNotify(`${r.name} is connected again.`);
        await this.raCheck(r);
      } catch (e) { this.repoChecks = { ...this.repoChecks, [r.id]: { ...chk, busy: false, error: e.message || String(e) } }; }
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
    async raKopiaReadonly(r, on, sharedOk = false, el = null) {
      try {
        const res = await _setupPost(`/api/setup/repos/${encodeURIComponent(r.id)}/readonly`, { on, shared_ok: sharedOk });
        r.kopia_readonly = !!(res.repo && res.repo.kopia_readonly);
        if (el) el.checked = r.kopia_readonly;
        this.klNotify(on ? `${r.name} is read-only now` : `${r.name} can be changed again`);
        this.wmLoad && this.wmLoad();
      } catch (e) {
        if (e.status === 409 && e.data && e.data.shared_with) {
          const app = e.data.shared_with;
          const ok = await this.klConfirm({ title: `Make ${app}'s connection read-only too?`,
            body: `${app} uses the same connection to this repository. Read-only applies to both, so ${app} can't back up to it until you turn this off.`,
            confirmLabel: "Make read-only", danger: true });
          if (ok) return this.raKopiaReadonly(r, on, true, el);
        } else {
          this.klNotify(e.message || String(e), "warn");
        }
        // The box was already toggled by the click; the model didn't change, so set it back by hand.
        r.kopia_readonly = !on;
        if (el) el.checked = !on;
        this.repos = this.repos.slice();
      }
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
      if (d.repo.kind === "backblaze") {
        return [["Backblaze", "Not touched"], ["What KopiaLens read", "Kept, so adding it back picks up where it left off"]];
      }
      return [
        ["KopiaLens data", (p.files || []).length ? `${fmtBytes(p.bytes || 0)} in ${countOf(p.files.length, "item")}` : "None yet"],
        ["Kopia repository", "Not touched"],
        ["Connection file", p.managed_config ? p.config_file : (p.config_file ? "Kept (KopiaLens didn't create it)" : "kopia's default, kept")],
      ];
    },
    // Move (or copy) a repository's connection file into KopiaLens's folder.
    async openTakeConnection(id) {
      this.repoMenu = false;
      const r = this.repos.find((x) => x.id === id);
      if (!r) return;
      this.tcDlg = { repo: r, preview: null, pw: "", busy: "", error: "" };
      try { this.tcDlg.preview = await getJson(`/api/setup/repos/${encodeURIComponent(id)}/take-connection`); }
      catch (e) { if (this.tcDlg) this.tcDlg.error = e.message || String(e); }
    },
    tcWarnings() {
      const p = this.tcDlg && this.tcDlg.preview;
      if (!p || p.ours) return [];
      const out = [];
      if (p.used_by.length) out.push(`${p.used_by.join(" and ")} uses this file. After a move, it stops connecting to this repository the next time it starts.`);
      if (p.default) out.push("This is kopia's default connection. After a move, kopia commands typed in a terminal no longer find this repository.");
      return out;
    },
    async tcSubmit(mode) {
      const d = this.tcDlg;
      if (!d || d.busy || !d.pw) return;
      d.busy = mode; d.error = "";
      try {
        await _setupPost(`/api/setup/repos/${encodeURIComponent(d.repo.id)}/take-connection`, { mode, password: d.pw });
        this.tcDlg = null;
        await this._cxRefreshRepos();
        this.klNotify(mode === "move" ? "Connection moved into KopiaLens's folder" : "Connection copied into KopiaLens's folder");
      } catch (e) {
        d.busy = ""; d.error = e.message || String(e);
      }
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
        this.klNotify(left ? `${d.repo.name} removed. ${countOf(left, "file")} couldn't be deleted; see the log.` : `${d.repo.name} removed`, left ? "warn" : "ok");
        if (!this.repos.length) { location.reload(); return; }
        if (d.repo.kind === "backblaze") this.raLoad();
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
      const fit = { older: `older than KopiaLens supports (${k.minimum} or newer)`, behind: `older than tested (${k.tested})`, newer: `newer than tested (${k.tested})` }[k.compat] || "";
      return [k.version ? `kopia ${k.version}` : "kopia", src, fit].filter(Boolean).join(" · ");
    },
    // Once per kopia version: a kopia newer or older than tested gets a note; one older than
    // supported gets a warning on every start (some screens can't work with it).
    kopiaCompatNote(k) {
      if (!k || !k.version || !["older", "behind", "newer"].includes(k.compat)) return;
      if (k.compat === "older") {
        this.klNotify(`kopia ${k.version} is older than KopiaLens supports (${k.minimum} or newer). Policies and notifications won't work. Update kopia, or pick another kopia in Settings → Repositories.`, "warn");
        return;
      }
      const key = "kl-kopia-newer-seen";
      try { if (localStorage.getItem(key) === k.version) return; localStorage.setItem(key, k.version); } catch (e) { /* show it anyway */ }
      const side = k.compat === "behind" ? "older" : "newer";
      this.klNotify(`kopia ${k.version} is ${side} than the version KopiaLens was tested with (${k.tested}). If something doesn't work, report it from Help.`);
    },
    async raKopiaSave(path) {
      try {
        this.kopiaInfo = { ...(await _setupPost("/api/setup/kopia", { path: (path || "").trim() })), found: (this.kopiaInfo || {}).found };
        this.repoKopiaEdit = null;
        this.klNotify("Saved");
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
};
