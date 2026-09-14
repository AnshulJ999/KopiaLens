// Settings modal: read/save scheduling, storage and advanced settings, auth password.
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
        this.reclaimInterval = Number(kl.reclaim_interval || 10800);
        this.excludedTolerance = Number(kl.excluded_tolerance ?? 0.05);
        this.esPath = kl.es_path || "";
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
      this.autoCompact = kl.auto_compact !== false;
      this.compactRatio = Number(kl.compact_ratio || 1.6);
      this.compactMinInterval = Number(kl.compact_min_interval || 86400);
      this.indexWorkerTimeout = Number(kl.index_worker_timeout || 14400);
      this.indexDrainTimeout = Number(kl.index_drain_timeout || 300);
      this.indexRetryDelay = Number(kl.index_retry_delay || 900);
      this.reindexAfterDeleteDelay = Number(kl.reindex_after_delete_delay ?? 300);
      this.indexMaxRetries = Number(kl.index_max_retries ?? 1);
      // ?? not ||: 0 is a meaningful value here (disables the stage) and || would silently
      // rewrite it to the default on every read and every save.
      this.folderPrewarmBudget = Number(kl.folder_prewarm_budget ?? 900);
      this.startupDelay = Number(kl.startup_delay ?? 300);
      this.dirMemoMaxMb = Number(kl.dir_memo_max_mb || 1500);
      this.refcountWriteInterval = Number(kl.refcount_write_interval || 21600);
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
    async saveSettings() {
      await this.wrap(async () => {
        // The backend MERGES this sub-dict rather than replacing it, so sending a subset is safe -
        // but anything the modal edits must be listed here or the edit is silently dropped.
        const kopialens = {
          reindex_interval: Number(this.reindexInterval) || 1800,
          dashboard_refresh: Number(this.dashRefresh) || 0,
          reclaim_interval: Number(this.reclaimInterval) || 10800,
          excluded_tolerance: Number(this.excludedTolerance),
          es_path: this.esPath || "",
          panel_layout: this.panelLayout === "tabbed" ? "tabbed" : "classic",
          index_mode: this.indexMode,
          index_time: this.indexTime,
          index_interval: Number(this.indexInterval) || 86400,
          dashboard_mode: this.dashboardMode,
          light_interval: Number(this.lightInterval) || 1800,
          dashboard_interval: Number(this.dashboardInterval) || 14400,
          auto_compact: !!this.autoCompact,
          compact_ratio: Number(this.compactRatio) || 1.6,
          compact_min_interval: Number(this.compactMinInterval) || 86400,
          index_worker_timeout: Number(this.indexWorkerTimeout) || 14400,
          index_drain_timeout: Number(this.indexDrainTimeout) || 300,
          index_retry_delay: Number(this.indexRetryDelay) || 900,
          reindex_after_delete_delay: Math.max(0, Number(this.reindexAfterDeleteDelay) || 0),
          index_max_retries: Math.max(0, Number(this.indexMaxRetries) || 0),
          folder_prewarm_budget: Number.isFinite(Number(this.folderPrewarmBudget))
            ? Math.max(0, Number(this.folderPrewarmBudget)) : 900,   // 0 = disable the stage
          startup_delay: Number(this.startupDelay) || 0,
          dir_memo_max_mb: Number(this.dirMemoMaxMb) || 1500,
          refcount_write_interval: Number(this.refcountWriteInterval) || 21600,
          debug_log: !!this.debugLog,
        };
        const backblaze = {
          bzdata_path: this.bzDataPath || "",
          bz_reindex_min_gap: Number(this.bzReindexMinGap) || 21600,
          history_days: Math.max(1, Number(this.bzHistoryDays) || 90),
          collapse_prefixes: this.bzCollapsePrefixes.split(",").map((p) => p.trim()).filter(Boolean),
          eventlog_archive_path: this.bzEventlogArchive || "",
          store: this.bzStore === "parquet" ? "parquet" : "sqlite",
        };
        await getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ listing_backend: this.backend, kopialens, backblaze }) });
        this.armAutoRefresh();  // apply the new dashboard auto-refresh interval immediately
        this.showSettings = false;
        window.alert("Saved. Scheduling and interval changes are picked up on the next tick (within a minute). The listing backend, directory-cache budget, startup delay and Backblaze data folder need a restart.");
      });
    },
    // Auth is on BECAUSE a password exists - set/remove through the dedicated routes,
    // never through saveSettings: the hash is PBKDF2'd server-side and never round-trips here.
    async setPassword() {
      if (!this.newPassword || this.newPassword.length < 8) { window.alert("Use at least 8 characters."); return; }
      const r = await getJson("/api/auth/set-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: this.newPassword }) }).catch((e) => { this.error = e.message; return null; });
      if (r && r.ok) { this.authProtected = true; this.newPassword = ""; window.alert("Password set - auth is on."); }
    },
    async removePassword() {
      if (!window.confirm("Remove password protection? Anyone who can reach this address will be able to use the app.")) return;
      const r = await getJson("/api/auth/remove-password", { method: "POST" }).catch((e) => { this.error = e.message; return null; });
      if (r && r.ok) { this.authProtected = false; window.alert("Password removed - auth is off."); }
    },
    async compactNow() {
      if (!window.confirm(`Compact repo ${this.repo} DuckDB now? Rewrites the file to reclaim dead space (no kopia rewalk); briefly pauses DB access during the swap.`)) return;
      await this.wrap(async () => {
        const r = await getJson("/api/actions/compact", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo }) });
        this.actionText = r.ok
          ? `Compacted repo ${this.repo}: ${fmtBytes(r.before)} → ${fmtBytes(r.after)} (saved ${fmtBytes(Math.max(0, r.before - r.after))}).`
          : `Compaction skipped: ${r.reason || "unknown"}.`;
      }, "Compacting DuckDB…");
    },
};
