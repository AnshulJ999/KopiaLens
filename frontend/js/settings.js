// Settings modal: every control in it saves itself on change (one key per save, never a full
// payload of browser-side defaults - that is what stamped values the user never chose).
const settingsPart = {

    async openSettings() {  // re-read the live settings file each open so the modal can't show stale state
      try {
        const data = await getJson("/api/repos");
        const kl = data.settings?.kopialens || {};
        this.backend = data.settings?.listing_backend || "subprocess";
        this.reindexInterval = Number(kl.reindex_interval || 1800);
        this.groupDepth = Number(kl.group_depth || 3);
        this.churnDays = String(kl.churn_days || 30);
        this.churnDepth = Number(kl.churn_depth || 4);
        this.churnView = kl.churn_view || "source";
        this.indexMode = kl.index_mode || "daily";
        this.indexTime = kl.index_time || "23:00";
        this.indexInterval = Number(kl.index_interval || 86400);
        this.dashboardMode = kl.dashboard_mode || "nightly";
        this.dashRefresh = Number(kl.dashboard_refresh ?? 900);
        this.excludedTolerance = Number(kl.excluded_tolerance ?? 0.05);
        this.esPath = kl.es_path || "";
        this.repoLabels = kl.repo_labels === "full" ? "full" : "short";
        this.diskLookupMode = ["auto", "everything", "scan", "off"].includes(kl.disk_lookup) ? kl.disk_lookup : "auto";
        this.diskScanBudget = Number(kl.disk_scan_budget || 15);
        getJson("/api/disk-lookup?refresh=true").then((s) => { this.diskLookup = s; }).catch(() => {});
        this.authProtected = !!kl.auth_protected;  // the hash itself never reaches the browser
        this.newPassword = "";
        this.panelLayout = kl.panel_layout === "tabbed" ? "tabbed" : "classic";  //
        this._readAdvanced(kl);
        this._readBackblaze(data.settings?.backblaze || {});
      } catch (e) { /* keep last-known state on fetch failure */ }
      this.showSettings = true;
    },
    _readAdvanced(kl) {
      this.lightInterval = Number(kl.light_interval || 1800);
      this.dashboardInterval = Number(kl.dashboard_interval || 14400);
      this.indexWorkerTimeout = Number(kl.index_worker_timeout || 14400);
      this.indexRetryDelay = Number(kl.index_retry_delay || 900);
      this.reindexAfterDeleteDelay = Number(kl.reindex_after_delete_delay ?? 300);
      this.indexMaxRetries = Number(kl.index_max_retries ?? 1);
      this.quitIndexing = kl.quit_indexing === "stop" ? "stop" : "finish";
      // ?? not ||: 0 is a meaningful value here (disables the stage) and || would silently
      // rewrite it to the default on every read and every save.
      this.folderPrewarmBudget = Number(kl.folder_prewarm_budget ?? 900);
      this.startupDelay = Number(kl.startup_delay ?? 300);
      this.debugLog = kl.debug_log === true;
    },
    _readBackblaze(bz) {
      this.bzDataPath = bz.bzdata_path || "C:\\ProgramData\\Backblaze\\bzdata";
      this.bzReindexMinGap = Number(bz.bz_reindex_min_gap || 21600);
      this.bzHistoryDays = Number(bz.history_days || 90);
      this.bzCollapsePrefixes = (bz.collapse_prefixes || []).join(", ");
      this.bzEventlogArchive = bz.eventlog_archive_path || "";
      this.bzStore = bz.store === "sqlite" ? "sqlite" : "parquet";
    },
    // One row per control in the modal: which state field it mirrors, how the value is written,
    // and any bound. `restart` = the app reads it once at startup. A control missing from here
    // saves nothing - that is the whole "silently dropped edit" class gone.
    KL_FIELDS: {
      listing_backend: { s: "backend", top: true, t: "choice", of: ["subprocess", "server"], restart: true },
      disk_lookup: { s: "diskLookupMode", t: "choice", of: ["auto", "everything", "scan", "off"] },
      disk_scan_budget: { s: "diskScanBudget", t: "int", min: 1 },
      es_path: { s: "esPath", t: "str" },
      excluded_tolerance: { s: "excludedTolerance", t: "num" },
      index_mode: { s: "indexMode", t: "choice", of: ["daily", "interval"] },
      index_time: { s: "indexTime", t: "str" },
      index_interval: { s: "indexInterval", t: "int", min: 3600 },
      dashboard_mode: { s: "dashboardMode", t: "choice", of: ["nightly", "interval"] },
      light_interval: { s: "lightInterval", t: "int", min: 60 },
      dashboard_refresh: { s: "dashRefresh", t: "int", min: 0 },
      dashboard_interval: { s: "dashboardInterval", t: "int", min: 900 },
      index_worker_timeout: { s: "indexWorkerTimeout", t: "int", min: 600 },
      reindex_after_delete_delay: { s: "reindexAfterDeleteDelay", t: "int", min: 0 },
      index_retry_delay: { s: "indexRetryDelay", t: "int", min: 60 },
      index_max_retries: { s: "indexMaxRetries", t: "int", min: 0, max: 5 },
      quit_indexing: { s: "quitIndexing", t: "choice", of: ["finish", "stop"] },
      folder_prewarm_budget: { s: "folderPrewarmBudget", t: "int", min: 0 },
      reindex_interval: { s: "reindexInterval", t: "int", min: 300 },
      startup_delay: { s: "startupDelay", t: "int", min: 0, restart: true },
      debug_log: { s: "debugLog", t: "bool" },
      repo_labels: { s: "repoLabels", t: "choice", of: ["short", "full"] },
      bzdata_path: { s: "bzDataPath", t: "str", block: "backblaze", restart: true },
      store: { s: "bzStore", t: "choice", of: ["parquet", "sqlite"], block: "backblaze" },
      bz_reindex_min_gap: { s: "bzReindexMinGap", t: "int", min: 300, block: "backblaze" },
      history_days: { s: "bzHistoryDays", t: "int", min: 7, block: "backblaze" },
      collapse_prefixes: { s: "bzCollapsePrefixes", t: "list", block: "backblaze" },
      eventlog_archive_path: { s: "bzEventlogArchive", t: "str", block: "backblaze" },
    },

    // Save ONE key on change. The payload never carries a value the user did not touch, so a
    // save cannot stamp a browser-side default over a setting that was resolved elsewhere.
    saveKl(key) {
      const f = this.KL_FIELDS[key];
      if (!f) return;
      let v = this[f.s];
      if (f.t === "int") {
        v = Math.round(Number(v));
        if (!isFinite(v)) v = f.min != null ? f.min : 0;
        if (f.min != null) v = Math.max(f.min, v);
        if (f.max != null) v = Math.min(f.max, v);
        this[f.s] = v;  // show the clamped value the backend will actually store
      } else if (f.t === "num") {
        v = Number(v) || 0;
      } else if (f.t === "bool") {
        v = !!v;
      } else if (f.t === "list") {
        v = String(v || "").split(",").map((p) => p.trim()).filter(Boolean);
      } else if (f.t === "choice") {
        v = f.of.includes(v) ? v : f.of[0];
        this[f.s] = v;
      } else {
        v = v == null ? "" : String(v);
      }
      const body = f.top ? { [key]: v }
        : f.block === "backblaze" ? { backblaze: { [key]: v } }
        : { kopialens: { [key]: v } };
      getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(() => {
          this.klNotify(f.restart ? "Saved - this setting needs a restart" : "Saved");
          if (key === "dashboard_refresh") this.armAutoRefresh();
        })
        .catch((e) => { this.error = e.message || String(e); });
    },
    // Auth is on BECAUSE a password exists - set/remove through the dedicated routes,
    // never through saveSettings: the hash is PBKDF2'd server-side and never round-trips here.
    async setPassword() {
      if (!this.newPassword || this.newPassword.length < 8) { this.klNotify("Use at least 8 characters.", "warn"); return; }
      const r = await getJson("/api/auth/set-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: this.newPassword }) }).catch((e) => { this.error = e.message; return null; });
      if (r && r.ok) { this.authProtected = true; this.newPassword = ""; this.klNotify("Password set - the app now asks for it."); }
    },
    async removePassword() {
      const ok = await this.klConfirm({
        title: "Remove password protection?",
        callout: "Anyone who can reach this address will be able to browse and act on the repositories.",
        confirmLabel: "Remove password", danger: true,
      });
      if (!ok) return;
      const r = await getJson("/api/auth/remove-password", { method: "POST" }).catch((e) => { this.error = e.message; return null; });
      if (r && r.ok) { this.authProtected = false; this.klNotify("Password removed - the app is open again."); }
    },
};
