// KopiaLens frontend entry: composes the single Alpine `kopiaLens` component from the part files.
// Load order (script tags in index.html): util.js first (shared helpers), then the part files,
// then this file LAST. Alpine is deferred in <head> and runs after every classic script, so the
// alpine:init listener below is registered in time. All state lives here (approach A); part
// files contribute methods only. treeWeekOptions stays in core because Object.assign would
// flatten a getter passed as a source argument into a one-time value.
document.addEventListener("alpine:init", () => {
  Alpine.data("kopiaLens", () => {
    const core = {
    repos: [], repo: "1", repoKind: "kopia",
    tabs: [
      { id: "dashboard", label: "Dashboard" }, { id: "snapshots", label: "Snapshots" },
      { id: "tree", label: "Tree" }, { id: "search", label: "Search" },
      { id: "churn", label: "Churn" }, { id: "simulate", label: "Simulate" },
      { id: "stats", label: "Stats" }, { id: "excluded", label: "Excluded" },
      { id: "policies", label: "Policies" }, { id: "actions", label: "Actions" },
    ],
    active: "dashboard",
    currentTabs() { return this.repoKind === "backblaze" ? this.bzTabs : this.tabs; },
    get dark() {  // live: theme/dark-mode state lives in themePart; "system" defers to the OS
      if (this.theme === "system") return window.matchMedia("(prefers-color-scheme: dark)").matches;
      return this.themeIsDark();
    },
    timeFmt: localStorage.getItem("kl-timefmt") || "absolute",
    indexStatus: {},
    sources: [], selectedSource: "",
    snapshots: [], selectedSnapshot: "",
    showChurn: localStorage.getItem("kl-churn") === "1",
    analyzing: false,
    snapStaleAnalysis: false,  // analysis rows exist but belong to the previous index run
    treeMode: "full",
    treeWeek: "",
    treeDeletedOnly: false, treeOverGb: false, treeExt: "", treeFlat: false,
    query: "", deletedOnly: false, overOneGb: false, searchSource: "", searchExt: "", searchExtExclude: "", omniMeta: "",
    groupByFolder: false, groupDepth: 3, groupMinGb: 0, groupMinFiles: 0,
    churnDays: "30", churnDepth: 4, churnView: "source", churnData: null, churnMeta: "", _churnSeq: 0,
    indexMode: "daily", indexTime: "23:00", indexInterval: 86400, dashboardMode: "nightly",
    freshness: [], _freshTimer: null,
    searchTotal: 0, searchPage: 1, searchPageSize: "500", searchSort: "size", searchDir: "desc",
    pageSizes: ["100", "200", "500", "1000", "2000", "5000", "10000", "Infinite"],
    showFilters: false, minGb: "", maxGb: "", dateFrom: "", dateTo: "", minVersions: "", sharedMin: "", recentBackup: "",
    dedupe: false, treeDedupe: false, reclaimMap: {}, reclaimComputing: false,
    showRestore: false, restore: null, showDiff: false, diffText: "", diffBusy: false,
    authProtected: false, newPassword: "",
    policyKeys: ["latest", "hourly", "daily", "weekly", "monthly", "annual"],
    policy: { latest: 100, hourly: 96, daily: 30, weekly: 52, monthly: 36, annual: 5 },
    simMode: "all", simMeta: "", simText: "", actionText: "",
    mounts: [],
    statsData: null, blobStats: "", blobLoading: false, _statsLoading: false,
    auditRows: [], auditSize: 0,
    groupReclaim: {},  // "repo:folder" -> {v: freeable, capped} - keyed by repo so one repo's
                       // numbers can never render in another repo's table
    mountSnapshot: "", mountPoint: "", restoreSource: "", restoreDest: "",
    dashboardMeta: "", busy: "", error: "", detail: null, batch: null, showBatch: false, snapSource: "*ALL*", _poll: null, _searchSeq: 0, _detailSeq: 0,
    _dashSeq: 0, _snapSeq: 0, _treeSeq: 0, _batchSeq: 0, _batchCmdSeq: 0, _removePlanSeq: 0, _removeDetailSeq: 0, _rfPlanSeq: 0,
    showRemove: false, removePlan: null, removeJobPoll: null, _removeBase: null,
    // Job runner (shared for every long kopia action): panel + follow state + header chip
    showJob: false, jobPanel: null, jobLines: [], jobEarly: 0, jobCursor: 0, jobFollow: true,
    jobMode: localStorage.getItem("kl-jobmode") || "clean", jobExpanded: false, runningJobs: [],
    _jobPoll: null, _jobChipPoll: null, _jobOpenSeq: 0, _handledJobs: null, _reindexPoll: null,
    excludedDrives: [], excludedOk: true, excludedPath: "", excludedFilter: "excluded", excludedMeta: "", _excludedItems: [], _excludedSeq: 0, excludedDetail: null, showExcludedDetail: false,
    showSettings: false, settingsTab: "general", backend: "subprocess", repoPinned: false, reindexInterval: 1800, dashRefresh: 900,
    reclaimInterval: 10800, excludedTolerance: 0.05, esPath: "",
    // Advanced settings. These existed in settings.json all along and had no UI, so the only way to
    // change them was to edit the file by hand. Defaults here mirror app/settings.py.
    lightInterval: 1800, dashboardInterval: 14400,
    autoCompact: true, compactRatio: 1.6, compactMinInterval: 86400,
    indexWorkerTimeout: 14400, indexDrainTimeout: 300, indexRetryDelay: 900, indexMaxRetries: 1,
    folderPrewarmBudget: 900, startupDelay: 300, dirMemoMaxMb: 1500, refcountWriteInterval: 21600,
    analyzingAll: false, showDelete: false, deletePlan: null, deleteConfirm: "", _autoRefresh: null,
    // detail-panel layout ("classic" | "tabbed"), persisted server-side, default Classic.
    panelLayout: "classic",
    confirmBox: null,  // the shared confirm dialog; truthy while it is open
    // Policies tab: overview + per-target detail from /api/policies/*; edits stage in
    // polStaged and reach kopia only through Review + save (sync POST, fail-closed backup
    // server-side). polAddPrices / polRowPrices cache "keeping out" pricing per pattern.
    polData: null, polSel: "global", polFilter: "", polStaged: {}, polAddText: "", polAddPrice: null,
    polAddPrices: {}, polRowPrices: {}, _polAddTimer: null, _polSeq: 0, _polRowPriceSeq: 0, _polAddPriceSeq: 0,
    polDetail: null, polDetailCache: {}, _polDetailSeq: 0,
    showPolDiff: false, polDiff: null, polDiffBusy: false, polDiffMode: "save",
    polRestoreName: "", polRestoreMode: "merge", polRestoreCmd: null, polRestoreParsed: null,
    showPolBackups: false, polBackups: null, _polBackupSeq: 0,
    // exclusion dialog: priced-before-commit pattern adder over the file/folder panels.
    exDialog: null, showExclude: false, _exPriceSeq: 0, _exCustomTimer: null, _exPriceTimer: null,

    fmtB(v) { return fmtBytes(v); },
    fmtPhys(v) { return fmtPhys(v); },
    humanTime(s) { return humanTime(s); },
    shortSource(s) { return sourcePath(s); },
    sourceRoot() { return this.selectedSource === "*ALL*" ? "" : sourcePath(this.selectedSource); },
    treePrefix: "",

    async init() {
      await this.wrap(async () => {
        TABLE_PREFS = await getJson("/api/settings/tables").catch(() => ({})) || {};
        const data = await getJson("/api/repos");
        this.repos = data.repos;
        const kl = data.settings?.kopialens || {};
        // URL ?repo= pins this tab to a repo (survives refresh, never writes last_repo) -> two
        // browser tabs can hold different repos without the last_repo setting flip-flopping.
        const urlRepo = new URLSearchParams(location.search).get("repo");
        if (urlRepo && data.repos.some((r) => r.id === urlRepo)) { this.repo = urlRepo; this.repoPinned = true; }
        else this.repo = data.settings?.last_repo || "1";
        this.repoKind = (data.repos.find((r) => r.id === this.repo) || {}).kind || "kopia";
        this.backend = data.settings?.listing_backend || "subprocess";
        const _ps = String(kl.page_size || 500);  // snap to a valid <option> so the select can't visually revert to 100
        this.searchPageSize = this.pageSizes.includes(_ps) ? _ps : "500";
        this.snapSource = kl.snap_source || "*ALL*";  // snapshots-view scope (persisted, default All sources)
        this.panelLayout = kl.panel_layout === "tabbed" ? "tabbed" : "classic";  //
        this.searchSort = kl.search_sort || "size";
        this.searchDir = kl.search_dir || "desc";
        this.dedupe = !!kl.search_dedupe;
        this.recentBackup = kl.recent_backup || "";  // "recently backed up" preset (days; "" = Any)
        this.treeDedupe = !!kl.tree_dedupe;
        this.treeMode = kl.tree_mode || "full";
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
        // Also at startup, not only in openSettings: if that re-fetch ever fails, the advanced
        // fields would still hold constructor defaults and a Save would write those over the real
        // values.
        this._readAdvanced(kl);
        this.initTheme(kl);  // sync theme/density state (server stamp already painted)
        if (kl.time_format) { TIME_FMT = kl.time_format; this.timeFmt = kl.time_format; }
        if (this.bzApplySettings) this.bzApplySettings(kl);  // restore persisted BZ prefs (tree drive, depths, toggles)
        if (this.repoKind === "backblaze") {
          this.active = "bz-dashboard";
          await this.bzInit();
        } else {
          await this.refreshBase();
          if (this.indexStatus.building) this.pollIndex();  // show progress for the auto-triggered (migration) rebuild
          await this.loadDashboard();
        }
      });
      this.armAutoRefresh();
      this.armFreshness();
      this.armJobChip();
    },

    armFreshness() {
      // One cached call covers all three repos. Skipped while the tab is hidden so a background
      // window is not polling all night.
      if (this._freshTimer) { clearInterval(this._freshTimer); this._freshTimer = null; }
      this.loadFreshness();
      this._freshTimer = setInterval(() => { if (!document.hidden) this.loadFreshness(); }, 30000);
    },

    armAutoRefresh() {
      // Re-fetch the dashboard from the (background-recomputed) cache on an interval so an open
      // tab never silently shows stale numbers. The signature guard skips re-render when unchanged.
      if (this._autoRefresh) { clearInterval(this._autoRefresh); this._autoRefresh = null; }
      const sec = Number(this.dashRefresh) || 0;
      if (sec > 0) this._autoRefresh = setInterval(() => { if (this.active === "dashboard" && !this.busy) this.loadDashboard(); }, sec * 1000);
    },

    klSet(patch) {  // persist a kopialens settings patch in the backend (all prefs are permanent)
      getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kopialens: patch }) }).catch(() => {});
    },

    async wrap(fn, label = "") {
      this.error = ""; this.busy = label;
      try { return await fn(); } catch (err) { this.error = err.message || String(err); } finally { this.busy = ""; }
    },

    toggleTimeFmt() {
      TIME_FMT = TIME_FMT === "relative" ? "absolute" : "relative";
      this.timeFmt = TIME_FMT;
      localStorage.setItem("kl-timefmt", TIME_FMT);
      this.klSet({ time_format: TIME_FMT });  // permanent in backend too
      this.activate();
    },
    copyText(t) { try { navigator.clipboard.writeText(t); } catch (e) {} },
    _downloadText(name, text) {  // trigger a browser download of generated text (e.g. a .bat)
      const blob = new Blob([text], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = name; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },

    indexLine() {
      const s = this.indexStatus;
      // The nightly worker holds the database, so this repo's tabs are briefly unavailable.
      // Say so plainly rather than letting the tabs just error.
      if (s.offline) return `Indexing (${s.stage || "working"}) ${s.indexed_snapshots || 0}/${s.snapshots || "?"}`;
      if (s.building) return `Indexing ${s.indexed_snapshots || 0}/${s.snapshots || "?"}...`;
      if (s.snapshots === undefined) return "Index: unknown";
      return `Index ${s.indexed_snapshots}/${s.snapshots}`;
    },

    // ---- Freshness strip: how current each repo's data is (one call covers all three) ----
    async loadFreshness() {
      try { this.freshness = (await getJson("/api/index/freshness")).repos || []; } catch (e) { /* non-critical */ }
    },
    freshAge(row) {
      if (row.running) return "indexing now";
      if (!row.last_index) return "never";
      const mins = Math.round((Date.now() / 1000 - row.last_index) / 60);
      if (mins < 60) return `${mins}m ago`;
      const hrs = Math.round(mins / 60);
      return hrs < 36 ? `${hrs}h ago` : `${Math.round(hrs / 24)}d ago`;
    },
    freshStale(row) {  // amber past a day and a half - long enough that a missed run is the reason
      return !row.running && (!row.last_index || (Date.now() / 1000 - row.last_index) > 36 * 3600);
    },

    async repoChanged() {
      this.selectedSnapshot = ""; this.snapshots = [];
      this.statsData = null; this.blobStats = "";  // stats are per-repo; force reload
      this.polData = null; this.polBackups = null; this.polDiscard();  // policies are per-repo too
      this.repoKind = (this.repos.find((r) => r.id === this.repo) || {}).kind || "kopia";
      if (this.repoPinned) {  // pinned tab: keep the choice in the URL, don't touch shared last_repo
        const u = new URL(location.href); u.searchParams.set("repo", this.repo); history.replaceState(null, "", u);
      } else {
        getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ last_repo: this.repo }) }).catch(() => {});
      }
      await this.wrap(async () => {
        if (this.repoKind === "backblaze") {  // BZ repo selected -> its own tabs + loaders
          if (!String(this.active).startsWith("bz-")) this.active = "bz-dashboard";
          return this.bzInit();
        }
        if (String(this.active).startsWith("bz-")) this.active = "dashboard";  // coming back from BZ
        await this.refreshBase(true);
        this.selectedSource = this.sources[0] || "";
        await this.activate();
      });
    },

    async refreshBase(force = false) {
      const [status, sources] = await Promise.all([
        getJson(`/api/index/status?repo=${encodeURIComponent(this.repo)}`),
        getJson(`/api/sources?repo=${encodeURIComponent(this.repo)}&force=${force}`),
      ]);
      this.indexStatus = status;
      this.sources = sources.sources || [];
      if (this.selectedSource !== "*ALL*" && !this.sources.includes(this.selectedSource)) this.selectedSource = this.sources[0] || "";
    },

    activate() {
      if (this.repoKind === "backblaze") return this.bzActivate();
      if (this.active === "dashboard") return this.loadDashboard();
      if (this.active === "snapshots") return this.loadSnapshots();
      if (this.active === "tree") return this.openTree();
      if (this.active === "search") return this.loadSearch();
      if (this.active === "churn") return this.loadChurn();
      if (this.active === "simulate") return;  // heavy - user clicks Run
      if (this.active === "stats") return this.statsData ? null : this.loadStats();
      if (this.active === "actions") return this.loadMounts();
      if (this.active === "excluded") return this.loadExcluded();
      if (this.active === "policies") return this.loadPolicies();
    },

    headerSearch() {  // top search box (Option B): drives BZ search when a Backblaze repo is active
      if (this.repoKind === "backblaze") { this.active = "bz-search"; this.bzLoadSearch(); }
      else { this.active = "search"; this.loadSearch(); }
    },

    async refreshAll() {
      await this.wrap(async () => {
        if (this.repoKind === "backblaze") { await this.bzRefreshStatus(); return this.bzActivate(); }
        await this.refreshBase(true);
        if (this.active === "dashboard") return this.loadDashboard(true);
        if (this.active === "snapshots") return this.loadSnapshots(true);
        if (this.active === "tree") return this.loadTree();
        if (this.active === "search") return this.loadSearch();
        if (this.active === "churn") return this.loadChurn(true);
        if (this.active === "stats") return this.loadStats(true);
        if (this.active === "actions") return this.loadMounts();
        if (this.active === "policies") return this.loadPolicies(true);
      }, "Refreshing...");
    },

    async buildIndex() {
      await this.wrap(async () => {
        const r = await getJson(`/api/index/build?repo=${encodeURIComponent(this.repo)}`, { method: "POST" });
        // The backend refuses a 2nd repo while one is indexing: {"started": false, "reason": ...}
        if (r && r.started === false) throw new Error(r.reason || "Index did not start.");
        this.pollIndex();
      }, "Index update started...");
    },
    async rebuildIndex() {
      await this.wrap(async () => {
        const r = await getJson(`/api/index/build?repo=${encodeURIComponent(this.repo)}&full=true`, { method: "POST" });
        if (r && r.started === false) throw new Error(r.reason || "Rebuild did not start.");
        this.pollIndex();
      }, "Full rebuild started...");
    },
    pollIndex() {
      if (this._poll) clearInterval(this._poll);
      this._poll = setInterval(async () => {
        try {
          this.indexStatus = await getJson(`/api/index/status?repo=${encodeURIComponent(this.repo)}`);
          if (!this.indexStatus.building) { clearInterval(this._poll); this._poll = null; }
        } catch (e) { clearInterval(this._poll); this._poll = null; }
      }, 3000);
    },
    // Week options: the view lists what the snapshots taken INSIDE that week recorded as
    // changed - the delta, not a full point-in-time tree. Value = the week's last-day ISO date.
    get treeWeekOptions() {
      const iso = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      const lbl = (d) => d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      const out = [];
      const end = new Date();
      end.setDate(end.getDate() + ((7 - end.getDay()) % 7));  // this week's Sunday
      for (let i = 0; i < 12; i++) {
        const e = new Date(end); e.setDate(end.getDate() - 7 * i);
        const s = new Date(e); s.setDate(e.getDate() - 6);
        out.push({ v: iso(e), label: lbl(s) + " - " + lbl(e) + (i === 0 ? " (current)" : "") });
      }
      return out;
    },

    // ---- Multi-select batch actions (Search + Tree) ----
    selectedRows(scope) {
      const el = document.querySelector(scope === "tree" ? "#treeTable" : "#searchTable");
      return (el && el._tabulator) ? el._tabulator.getSelectedData() : [];
    },
    _selFiles(scope) {
      return this.selectedRows(scope).filter((r) => r.type !== "folder")
        .map((r) => ({ source: r.source || this.selectedSource, full_path: r.full_path || r.path }))
        .filter((f) => f.source && f.full_path);
    },

    // ---- Range select (Shift+Click) — shared by search + snapshots tables ----
    _rangeClick(table, e, row) {
      const rows = table.getRows("active");                              // current display order
      const idx = rows.findIndex((r) => r.getData() === row.getData());  // identity match (indexOf was unreliable)
      if (idx < 0) return;
      if (e.shiftKey && table._rangeAnchor != null && table._rangeAnchor >= 0) {
        const a = Math.min(table._rangeAnchor, idx), b = Math.max(table._rangeAnchor, idx);
        for (let k = a; k <= b; k++) rows[k].select();
      }
      table._rangeAnchor = idx;
    },

    csv(id, name) { downloadTable(id, name); },
    };
    // Duplicate-key guard: two parts declaring the same key would compose silently last-wins.
    const parts = [["core", core], ["bzIndexPart", bzIndexPart], ["bzDashboardPart", bzDashboardPart], ["bzSearchPart", bzSearchPart], ["bzTreePart", bzTreePart], ["bzExcludedPart", bzExcludedPart], ["bzExclusionsPart", bzExclusionsPart], ["bzChurnPart", bzChurnPart], ["bzHealthPart", bzHealthPart], ["dashboardPart", dashboardPart], ["snapshotsPart", snapshotsPart], ["treePart", treePart], ["searchPart", searchPart], ["churnPart", churnPart], ["simulatePart", simulatePart], ["statsPart", statsPart], ["excludedPart", excludedPart], ["actionsPart", actionsPart], ["confirmPart", confirmPart], ["runbarPart", runbarPart], ["detailPart", detailPart], ["excludePart", excludePart], ["jobsPart", jobsPart], ["settingsPart", settingsPart], ["themePart", themePart], ["policiesPart", policiesPart]];
    const seen = new Map();
    for (const [owner, part] of parts)
      for (const k of Object.keys(part)) {
        if (seen.has(k)) console.error(`kopiaLens: duplicate key "${k}" declared in both ${seen.get(k)} and ${owner}`);
        else seen.set(k, owner);
      }
    return Object.assign(core, bzIndexPart, bzDashboardPart, bzSearchPart, bzTreePart, bzExcludedPart, bzExclusionsPart, bzChurnPart, bzHealthPart, dashboardPart, snapshotsPart, treePart, searchPart, churnPart, simulatePart, statsPart, excludedPart, actionsPart, confirmPart, runbarPart, detailPart, excludePart, jobsPart, settingsPart, themePart, policiesPart);
  });
});
