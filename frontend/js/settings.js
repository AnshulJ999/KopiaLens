// Settings modal: every control in it saves itself on change (one key per save, never a full
// payload of browser-side defaults - that is what stamped values the user never chose).
const MEMORY_CHOICES = ["512MB", "1GB", "2GB", "3GB", "4GB", "6GB", "8GB"];
// Tray icon choices (the desktop app draws them from shell/src-tauri/icons/tray/). First = default.
const TRAY_ICONS = [
  ["k3g", "Disc"], ["k6s", "K"], ["k3a", "Ring"], ["tile", "Tile"], ["mono", "K in one colour"], ["q3", "Q with a glint"],
  ["q1", "Lens with a tail"], ["s3", "Ring with a slice out"], ["r4", "Bold lens"], ["slice", "Slice pulled out"], ["q", "Simple Q"],
];

// A duration setting shown as a free number plus a unit; the setting itself stays in seconds.
// Changing the unit converts the shown number, it never changes the stored value.
function klDur(prop, key) {
  return {
    n: 0, unit: "1",
    init() { this._show(true); this.$watch(prop, () => this._show(true)); },
    _show(pickUnit) {
      const v = Number(this[prop]) || 0;
      if (pickUnit) this.unit = v >= 3600 && v % 3600 === 0 ? "3600" : v >= 60 && v % 60 === 0 ? "60" : "1";
      this.n = Math.round((v / Number(this.unit)) * 100) / 100;
    },
    relabel() { this._show(false); },
    commit() {
      this[prop] = Math.round(Number(this.n || 0) * Number(this.unit));
      this.saveKl(key);
    },
  };
}

const settingsPart = {

    async openSettings() {  // re-read the live settings file each open so the modal can't show stale state
      this.acc = null; this.accErr = "";
      try {
        const data = await getJson("/api/repos");
        const kl = data.settings?.kopialens || {};
        this._klSettings = kl;
        this.notifyHook = null; this.notifyForm = null;
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
        this.showProBadge = kl.show_pro_badge !== false;
        this._readLook(kl);
        this.updateCheck = kl.update_check !== false;
        this.usageStats = kl.usage_stats !== false;
        this.diskLookupMode = ["auto", "everything", "scan", "off"].includes(kl.disk_lookup) ? kl.disk_lookup : "auto";
        this.diskScanBudget = Number(kl.disk_scan_budget || 15);
        this.folderPicker = ["auto", "native", "browser"].includes(kl.folder_picker) ? kl.folder_picker : "auto";
        this.mountDir = kl.mount_dir || "";
        this.mountHostPath = kl.mount_host_path || "";
        getJson("/api/disk-lookup?refresh=true").then((s) => { this.diskLookup = s; }).catch(() => {});
        this.sessionDays = Number(kl.session_days || 30);
        this.loadAccount();
        this.raLoad();
        this.panelLayout = kl.panel_layout === "tabbed" ? "tabbed" : "classic";  //
        this.docsView = kl.docs_view === "side" ? "side" : "popup";
        this._readAdvanced(kl);
        this._readBackblaze(data.settings?.backblaze || {});
        this.loadCfg();
        this.loadIxBackups();
        this.loadAbout();
      } catch (e) { /* keep last-known state on fetch failure */ }
      this.showSettings = true;
    },
    _readAdvanced(kl) {
      this.lightInterval = Number(kl.light_interval || 1800);
      this.dashboardInterval = Number(kl.dashboard_interval || 14400);
      this.indexWorkerTimeout = Number(kl.index_worker_timeout || 14400);
      this.indexFirstTimeout = Number(kl.index_first_timeout || 86400);
      this.indexWaitUnreachable = Number(kl.index_wait_unreachable || 1800);
      this.indexStallTimeout = Number(kl.index_stall_timeout ?? 3600);
      this.indexKeepVersions = Number(kl.index_keep_versions ?? 1);
      this.indexBackupDir = kl.index_backup_dir || "";
      this.removePlanCap = Number(kl.remove_plan_cap || 200000);
      this.indexRetryDelay = Number(kl.index_retry_delay || 900);
      this.reindexAfterDeleteDelay = Number(kl.reindex_after_delete_delay ?? 300);
      this.indexMaxRetries = Number(kl.index_max_retries ?? 1);
      this.quitIndexing = kl.quit_indexing === "stop" ? "stop" : "finish";
      // ?? not ||: 0 is a meaningful value here (disables the stage) and || would silently
      // rewrite it to the default on every read and every save.
      this.folderPrewarmBudget = Number(kl.folder_prewarm_budget ?? 900);
      this.startupDelay = Number(kl.startup_delay ?? 300);
      this.debugLog = kl.debug_log === true;
      this.controlApi = kl.control_api !== false;
      this.appPort = Number(kl.port || 7123);
      this.controlPort = Number(kl.control_port || 7124);
      this.trayClick = kl.tray_click === "menu" ? "menu" : "window";
      this.trayDoubleClick = kl.tray_double_click === "none" ? "none" : "window";
      this.notifyLevel = ["all", "off"].includes(kl.notifications) ? kl.notifications : "warnings";
      this.logCleanupDays = Number(kl.log_cleanup_days ?? 90);
      this.taskHistoryKeep = Number(kl.task_history_days ?? 90);
      this.engineTakeback = kl.engine_takeback === "wait" ? "wait" : "auto";
      this.rankCount = Number(kl.rank_count ?? 10000);
      this.duckdbMemory = String(kl.duckdb_memory || "").toUpperCase();
      this.memoryAuto = kl.memory_auto || "";
    },
    // Logo, retention chips and the Snapshot now confirm: read at startup and on each Settings open.
    _readLook(kl) {
      this.logo = ["k3g", "k6s", "k3a", "tile", "none"].includes(kl.logo) ? kl.logo : "k3g";
      this.trayIcon = TRAY_ICONS.some((t) => t[0] === kl.tray_icon) ? kl.tray_icon : "k3g";
      this.askSnapshotNow = kl.ask_snapshot_now !== false;
      this.taskView = kl.task_view === "panel" ? "panel" : "popup";
      this.retentionChips = ["kopiaui", "custom"].includes(kl.retention_chips) ? kl.retention_chips : "uniform";
      this.retentionChipColor = /^#[0-9a-f]{6}$/i.test(kl.retention_chip_color || "") ? kl.retention_chip_color : "#2563eb";
      this.applyFavicon();
    },
    // The browser tab icon follows the header logo (the Disc when the logo is hidden).
    applyFavicon() {
      const el = document.querySelector('link[rel="icon"]');
      if (el) el.href = appUrl(`/static/logos/${this.logo === "none" ? "k3g" : this.logo}.svg`);
    },
    TRAY_ICONS,
    MEMORY_CHOICES,
    fmtLimit(v) { return String(v || "").replace(/(\d)\s*([KMGT]B)$/i, "$1 $2"); },
    _readBackblaze(bz) {
      this.bzDataPath = bz.bzdata_path || "";
      this.bzUpdates = bz.updates === "off" ? "off" : "auto";
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
      folder_picker: { s: "folderPicker", t: "choice", of: ["auto", "native", "browser"] },
      mount_dir: { s: "mountDir", t: "str" },
      mount_host_path: { s: "mountHostPath", t: "str" },
      es_path: { s: "esPath", t: "str" },
      excluded_tolerance: { s: "excludedTolerance", t: "num" },
      index_mode: { s: "indexMode", t: "choice", of: ["daily", "interval", "off"] },
      index_time: { s: "indexTime", t: "str" },
      index_interval: { s: "indexInterval", t: "int", min: 3600 },
      dashboard_mode: { s: "dashboardMode", t: "choice", of: ["nightly", "interval"] },
      light_interval: { s: "lightInterval", t: "int", min: 60 },
      dashboard_refresh: { s: "dashRefresh", t: "int", min: 0 },
      dashboard_interval: { s: "dashboardInterval", t: "int", min: 900 },
      index_worker_timeout: { s: "indexWorkerTimeout", t: "int", min: 600 },
      index_first_timeout: { s: "indexFirstTimeout", t: "int", min: 600 },
      index_wait_unreachable: { s: "indexWaitUnreachable", t: "int", min: 60 },
      index_stall_timeout: { s: "indexStallTimeout", t: "int", min: 0 },
      index_keep_versions: { s: "indexKeepVersions", t: "int", min: 0, max: 3 },
      index_backup_dir: { s: "indexBackupDir", t: "str" },
      remove_plan_cap: { s: "removePlanCap", t: "int", min: 1000 },
      reindex_after_delete_delay: { s: "reindexAfterDeleteDelay", t: "int", min: 0 },
      index_retry_delay: { s: "indexRetryDelay", t: "int", min: 60 },
      index_max_retries: { s: "indexMaxRetries", t: "int", min: 0, max: 5 },
      quit_indexing: { s: "quitIndexing", t: "choice", of: ["finish", "stop"] },
      folder_prewarm_budget: { s: "folderPrewarmBudget", t: "int", min: 0 },
      reindex_interval: { s: "reindexInterval", t: "int", min: 300 },
      startup_delay: { s: "startupDelay", t: "int", min: 0, restart: true },
      debug_log: { s: "debugLog", t: "bool" },
      control_api: { s: "controlApi", t: "bool", restart: true },
      port: { s: "appPort", t: "int", min: 1024, max: 65535, restart: true },
      control_port: { s: "controlPort", t: "int", min: 1024, max: 65535, restart: true },
      tray_click: { s: "trayClick", t: "choice", of: ["window", "menu"] },
      tray_double_click: { s: "trayDoubleClick", t: "choice", of: ["window", "none"] },
      notifications: { s: "notifyLevel", t: "choice", of: ["all", "warnings", "off"] },
      duckdb_memory: { s: "duckdbMemory", t: "choice", of: ["", ...MEMORY_CHOICES], restart: true },
      log_cleanup_days: { s: "logCleanupDays", t: "int", min: 0 },
      task_history_days: { s: "taskHistoryKeep", t: "int", min: 0 },
      engine_takeback: { s: "engineTakeback", t: "choice", of: ["auto", "wait"] },
      rank_count: { s: "rankCount", t: "int", min: 0 },
      repo_labels: { s: "repoLabels", t: "choice", of: ["short", "full"] },
      show_pro_badge: { s: "showProBadge", t: "bool" },
      logo: { s: "logo", t: "choice", of: ["k3g", "k6s", "k3a", "tile", "none"] },
      tray_icon: { s: "trayIcon", t: "choice", of: TRAY_ICONS.map((t) => t[0]) },
      ask_snapshot_now: { s: "askSnapshotNow", t: "bool" },
      retention_chips: { s: "retentionChips", t: "choice", of: ["uniform", "kopiaui", "custom"] },
      retention_chip_color: { s: "retentionChipColor", t: "str" },
      update_check: { s: "updateCheck", t: "bool" },
      usage_stats: { s: "usageStats", t: "bool" },
      bzdata_path: { s: "bzDataPath", t: "str", block: "backblaze", restart: true },
      store: { s: "bzStore", t: "choice", of: ["parquet", "sqlite"], block: "backblaze" },
      updates: { s: "bzUpdates", t: "choice", of: ["auto", "off"], block: "backblaze" },
      bz_reindex_min_gap: { s: "bzReindexMinGap", t: "int", min: 300, block: "backblaze" },
      history_days: { s: "bzHistoryDays", t: "int", min: 7, block: "backblaze" },
      collapse_prefixes: { s: "bzCollapsePrefixes", t: "list", block: "backblaze" },
      eventlog_archive_path: { s: "bzEventlogArchive", t: "str", block: "backblaze" },
      session_days: { s: "sessionDays", t: "int", min: 1, max: 365 },
      number_style: { s: "numberStyle", t: "choice", of: ["system", "en-US", "de-DE", "fr-FR", "en-IN"] },
      byte_units: { s: "byteUnits", t: "choice", of: ["decimal", "binary"] },
      date_style: { s: "dateStyle", t: "choice", of: ["system", "dmy", "mdy", "iso"] },
      clock: { s: "clock", t: "choice", of: ["system", "12", "24"] },
      search_dedupe_by: { s: "dedupeBy", t: "choice", of: ["path", "content"] },
      header_search_only: { s: "headerSearchOnly", t: "bool" },
    },

    setTimeFmt(v) { if (v !== TIME_FMT) this.toggleTimeFmt(); },

    // ---- Remote access (read at start: a change needs a restart) ----
    async loadAccess() {
      this.accErr = "";
      try { this.acc = await getJson("/api/access"); } catch (e) { this.accErr = e.message || String(e); }
    },
    async saveAccess(change) {
      this.accErr = "";
      try {
        this.acc = await getJson("/api/access", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(change) });
        this.klNotify("Saved - restart KopiaLens to apply");
      } catch (e) {
        this.accErr = e.message || String(e);
        this.loadAccess();   // put the form back to what is stored
      }
    },
    accessLabel(m) {
      return { window: "This window only", computer: "This computer", addresses: "These addresses",
               network: "Anyone on the network" }[m] || m;
    },
    accessHint(m) {
      return { window: "Only the KopiaLens app window. Browsers, even on this computer, are turned away.",
               computer: "Any browser on this computer.",
               addresses: "This computer, plus the addresses and networks you list.",
               network: "Anyone who can reach this computer on the network. Turn on sign-in." }[m] || "";
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
      if (key === "number_style" || key === "date_style" || key === "clock" || key === "byte_units") this.applyFormats();
      if (key === "logo") this.applyFavicon();
      if ((key === "retention_chips" || key === "retention_chip_color") && this.active === "snapshots") this.loadSnapshots(true);
      const body = f.top ? { [key]: v }
        : f.block === "backblaze" ? { backblaze: { [key]: v } }
        : { kopialens: { [key]: v } };
      return getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(() => {
          this.klNotify(f.restart ? "Saved - this setting needs a restart" : "Saved");
          if (key === "dashboard_refresh") this.armAutoRefresh();
        })
        .catch((e) => { this.error = e.message || String(e); });
    },
};
