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
    // The default order; people drag tabs into their own (tab_order, Settings > Appearance resets it).
    tabs: [
      { id: "dashboard", label: "Dashboard", tip: "Every backup source in this repo" },
      { id: "snapshots", label: "Snapshots", tip: "Every snapshot of a source" },
      { id: "tree", label: "Tree", tip: "Browse backed-up snapshots" },
      { id: "search", label: "Search", tip: "Find files across your backups" },
      { id: "churn", label: "Churn", tip: "How much new data each source adds" },
      { id: "excluded", label: "Excluded", tip: "Browse excluded files and folders" },
      { id: "stats", label: "Repository", tip: "Storage, maintenance and health" },
      { id: "policies", label: "Policies", tip: "Backup rules and schedules" },
      { id: "tasks", label: "Tasks", tip: "Backups and jobs, running and finished" },
      { id: "logs", label: "Logs", tip: "Audit trail, activity and app logs" },
    ],
    tabOrder: [], tabDrag: "", tabOver: "", tabMenu: false,
    // Reached through a kopia server: kopia refuses the reads an index needs, so only the views
    // that work straight from kopia are offered.
    serverTabs: [
      { id: "snapshots", label: "Snapshots" }, { id: "policies", label: "Policies" }, { id: "tasks", label: "Tasks" }, { id: "logs", label: "Logs" },
    ],
    active: "dashboard",
    currentTabs() { return this.repoKind === "backblaze" ? this.bzTabs : (this.repoKind === "server" ? this.serverTabs : this.orderedTabs()); },
    orderedTabs() {
      const at = (id) => { const i = this.tabOrder.indexOf(id); return i < 0 ? 1000 + this.tabs.findIndex((t) => t.id === id) : i; };
      return [...this.tabs].sort((a, b) => at(a.id) - at(b.id));
    },
    // Drag a tab onto another to put it there; the order is saved (tab_order).
    tabDrop(target) {
      const from = this.tabDrag;
      this.tabDrag = ""; this.tabOver = "";
      if (!from || from === target || this.repoKind !== "kopia") return;
      const ids = this.orderedTabs().map((t) => t.id).filter((id) => id !== from);
      const at = ids.indexOf(target);
      const after = this.orderedTabs().findIndex((t) => t.id === from) < this.orderedTabs().findIndex((t) => t.id === target);
      ids.splice(after ? at + 1 : at, 0, from);
      this.tabOrder = ids;
      this.klSet({ tab_order: ids });
    },
    resetTabOrder() {
      this.tabMenu = false;
      this.tabOrder = [];
      this.klSet({ tab_order: [] });
      this.klNotify("Tab order reset");
    },
    kindOf(id) {
      const r = (this.repos || []).find((x) => x.id === id) || {};
      if (r.kind === "backblaze") return "backblaze";
      return r.via_server ? "server" : (r.kind || "kopia");
    },
    get dark() {  // live: theme/dark-mode state lives in themePart; "system" defers to the OS
      if (this.theme === "system") return window.matchMedia("(prefers-color-scheme: dark)").matches;
      return this.themeIsDark();
    },
    timeFmt: localStorage.getItem("kl-timefmt") || "auto",
    polRailW: Number(localStorage.getItem("kl-pol-rail")) || 288,   // Policies list width (drag handle)
    numberStyle: "system", dateStyle: "system", clock: "system", byteUnits: "decimal",   // Settings > General (util.js setFormatStyles)
    indexStatus: {},
    sources: [], selectedSource: "",
    // Each tab remembers its own source per repo; "*ALL*" = all sources.
    snapSourceByRepo: {}, treeSourceByRepo: {},
    ownerFilterByRepo: {}, clientByRepo: {},   // owner filter (all / this computer / one user@host) + this computer's kopia name
    searchSelCount: 0, treeSelCount: 0, snapSelCount: 0,   // rows selected in Search / Tree (their selection bars)
    snapshots: [], selectedSnapshot: "",
    showChurn: localStorage.getItem("kl-churn") === "1",
    analyzing: false,
    snapStaleAnalysis: false,  // analysis rows exist but belong to the previous index run
    treeMode: "full",
    treeWeek: "",
    treeDeletedOnly: false, treeOverGb: false, treeExt: "", treeFlat: false, treeView: "list",
    query: "", deletedOnly: false, overOneGb: false, searchSource: "", searchExt: "", searchExtExclude: "", omniMeta: "",
    searchView: "", searchViewRows: null, _searchViewSeq: 0,   // a ready-made Search view ("prune"), its rows
    groupByFolder: false, searchKind: "files", searchFolders: 0, searchSkipped: [], searchMaxDepth: 20000, groupDepth: 3, groupMinDepth: "", groupMinGb: 0, groupMinFiles: 0, groupLoose: true, groupMode: "deepest", folderVersions: {},
    churnDays: "30", churnDepth: 4, churnView: "folder", churnData: null, churnMeta: "", _churnSeq: 0,
    _churnPhysSeq: 0, _churnPhysCache: {}, _churnPhysTimer: null,   // folder view: on-screen all-versions physical
    indexMode: "daily", indexTime: "23:00", indexInterval: 86400, dashboardMode: "nightly",
    freshness: [], _freshTimer: null,
    searchTotal: 0, searchEmpty: false, searchPage: 1, searchPageSize: "500", searchSort: "stored", searchDir: "desc",
    pageSizes: ["100", "200", "500", "1000", "2000", "5000", "10000", "Infinite"],
    showFilters: false, minGb: "", maxGb: "", dateFrom: "", dateTo: "", minVersions: "", sharedMin: "", recentBackup: "",
    dedupe: false, dedupeBy: "path", headerSearchOnly: false, treeDedupe: false, reclaimMap: {},
    showRestore: false, restore: null, showDiff: false, diffText: "", diffBusy: false, diffRange: "",
    fb: null,   // in-app folder browser (actions.js)
    folderPicker: "auto", mountDir: "", mountHostPath: "",
    // What the machine running KopiaLens can do (/api/app/host); Windows-on-this-PC until it answers.
    host: { os: "windows", docker: false, can_open: true, file_manager: "Explorer", folder_picker: "browser",
            native_picker_possible: true, mount: { ok: true, reason: "", dir: null, host_dir: "" }, data_persistent: true },
    // Settings Account + Encryption sections, their shared dialog, and the unlock box (lock.html).
    acct: null, encStatus: null, acctDialog: null, locked: null, sessionDays: 30,
    // Settings backups, the damaged-file notice, pausing indexing and About (appconfig.js).
    cfg: null, cfgError: false, cfgNotices: [], ixNotices: [], listingNotices: [], ixBackups: null, ixCopyRunning: false, cfgDlg: null, cfgBusy: "", cfgExportAccounts: false, cfgExportKeys: false,
    indexPause: { paused: false }, about: null, controlApi: true, appPort: 7123, controlPort: 7124, savedPorts: {}, trayClick: "window", trayDoubleClick: "window", desktopSignin: false, notifyLevel: "warnings",
    // Onboarding (onboarding.js) and repositories (repos.js): the shared connect form is `cx`.
    ob: null, cx: null, repoMenu: false, repoAdd: false, repoDlg: null, tcDlg: null, repoChecks: {}, kopiaInfo: null, repoKopiaEdit: null,
    policyKeys: ["latest", "hourly", "daily", "weekly", "monthly", "annual"],
    policy: { latest: 100, hourly: 96, daily: 30, weekly: 52, monthly: 36, annual: 5 },
    _simOne: null, _simFreesSeq: 0,   // Simulate one source: the last result, its own-frees fill
    simMode: "all", simMeta: "", simText: "", actionText: "", simEmpty: true, simPolicyByRepo: {}, _simRepo: null, _simRunOnOpen: false,
    mounts: [],
    statsData: null, blobStats: "", blobLoading: false, _statsLoading: false,
    auditRows: [], auditSize: 0, auditHideRoutine: true,
    // Logs tab (): four sections (audit/activity/indexing/app log), all repos in one
    // view with a client-side repo filter; the app log is app-wide by nature.
    logsTab: "audit", logsRepo: "all", jobHistory: [], indexRuns: [], noticeLog: [],
    appLogRecords: [], appLogLoaded: false, appLogFile: "app", appLogLevel: "info",
    appLogDebugAvailable: false, appLogTruncated: false, _logsSeq: 0,
    groupReclaim: {},  // "repo:folder" -> {v: freeable, capped} - keyed by repo so one repo's
                       // numbers can never render in another repo's table
    dashboardMeta: "", busy: "", error: "", repoSwitching: false, _repoSwitchSeq: 0, detail: null, panelStack: [], snapSource: "*ALL*", _poll: null, _searchSeq: 0, _detailSeq: 0,
    _rankedNotice: {},   // "repo|column" -> shown once per session (Search sort falls back to physical)
    // Action results shown in the corner: a click that reports only on the Actions tab looks
    // like it did nothing, so every action raises one of these too.
    toasts: [], _toastSeq: 0,
    // Indexing panel: the live pass, its worker log and the Stop control.
    showIndex: false, indexLogLines: [], indexLogCursor: 0, indexLogNote: "",
    indexLogRepo: "", indexPanelStatus: null,   // the repo the indexing panel follows + its status
    indexLogMode: localStorage.getItem("kl-indexmode") || "clean", _indexLogPoll: null, _indexLogSeq: 0,
    _dashSeq: 0, _snapSeq: 0, _treeSeq: 0, _removePlanSeq: 0, _removeDetailSeq: 0, _rfPlanSeq: 0, _contentsSeq: 0,
    showRemove: false, removePlan: null, _removeBase: null,
    // Job runner (shared for every long kopia action): panel + follow state + header chip
    showJob: false, jobPanel: null, jobLines: [], jobEarly: 0, jobCursor: 0, jobFollow: true,
    jobMode: localStorage.getItem("kl-jobmode") || "clean", jobExpanded: false, runningJobs: [],
    _jobPoll: null, _jobChipPoll: null, _jobOpenSeq: 0, _handledJobs: null, _reindexWatch: {},
    excludedDrives: [], _excludedDrivesStale: false, excludedOk: true, excludedPath: "", excludedFilter: "excluded", excludedMeta: "", _excludedItems: [], _excludedSeq: 0, excludedDetail: null, showExcludedDetail: false,
    excludedScans: [], _excludedSeen: false, _excludedNotes: null, _excludedCountedAt: null, _excludedNotIndexed: false,
    // Free/Pro (licence.js): GET /api/licence; proNotice = the open upgrade notice.
    lic: { state: "none", locked: [], features: {}, pricing_url: "" }, proNotice: null,
    licKey: "", licBusy: false, licError: "", licPatreonWaiting: false, licAdding: false,
    showSettings: false, settingsTab: "general", settingsQuery: "", settingsSearching: false, settingsCounts: {}, backend: "subprocess", repoPinned: false, dashRefresh: 1800,
    excludedTolerance: 0.05, esPath: "",
    // Advanced settings. These existed in settings.json all along and had no UI, so the only way to
    // change them was to edit the file by hand. Defaults here mirror app/settings.py.
    lightInterval: 3600, lightMode: "in_use", _klDefaults: null, dashboardInterval: 14400,
    debugLog: false, experimental: false, expNotes: false,
    duckdbMemory: "", memoryAuto: "",  // memory limit override ("" = automatic) and what automatic resolves to
    duckdbThreads: 0, threadsAuto: 0, threadsWorker: 0, cpuCount: 0,  // thread override (0 = automatic) and the automatic values
    indexWorkerTimeout: 14400, indexFirstTimeout: 86400, indexWaitUnreachable: 1800, indexStallTimeout: 3600, indexKeepVersions: 1, indexBackupDir: "", removePlanCap: 200000,
    indexRetryDelay: 900, indexMaxRetries: 1, reindexAfterDeleteDelay: 300, quitIndexing: "finish",
    folderPrewarmBudget: 900, startupDelay: 300, logCleanupDays: 90, taskHistoryKeep: 90, engineTakeback: "auto", rankCount: 10000,
    showProBadge: true,   // the small Pro mark next to the logo while a licence is active
    logo: "k3g",          // header logo: k3g | k6s | k3a | tile | none (Settings > Appearance)
    trayIcon: "k3g",      // desktop app tray icon (Settings > Desktop app; TRAY_ICONS in settings.js)
    askSnapshotNow: true, // confirm before Snapshot now (Settings > General; the confirm's "Don't ask again")
    snapStarting: {},     // source -> true while its Snapshot now start is in flight
    downloading: "",      // the item a Download is being prepared for
    tasksRefreshing: false,
    autostart: null, autostartBusy: false,   // Start at logon (Settings > Desktop app)
    retentionChips: "kopiaui", retentionChipColor: "#2563eb",   // Snapshots retention chips: kopiaui | uniform | custom
    treeExtra: {}, _treeFilling: false,   // Tree: Physical (all) / Reclaimable / sources worked out for the rows on screen
    fullList: null, _fullListSeq: 0,      // the "Show all" popup (fulllist.js)
    docs: null, docsView: "popup", _docsLoaded: null, _docsCache: null, _docsWindow: false,   // the docs browser (docs.js)
    taskView: "popup",                    // Tasks: details as a full-size popup or a small panel (task_view)
    chipMenu: "",                         // the open header chip list: "jobs" | "pending" | "more"
    tblLayout: null, tableAlign: "auto", timeMenu: false,   // tables.js: "Table layout…", app-wide alignment, the time button's menu
    dashPageSize: "all",                  // Dashboard rows per page: all | 25 | 50 | 100
    design: {},                           // Design widget choices (dev add-on, data-d-* on <html>); see designIs
    repoLabels: "short",  // repo chips and columns: "short" (Repo 1) | "full" (the name from repos.json)
    // Backblaze settings. Defaults mirror app/settings.py; each control saves its own key into
    // the nested "backblaze" dict (KL_FIELDS in settings.js).
    bzDataPath: "", bzReindexMinGap: 21600, bzHistoryDays: 90,
    bzCollapsePrefixes: "", bzEventlogArchive: "", bzStore: "parquet", bzUpdates: "auto",
    raBz: null,   // Backblaze on this computer (GET /api/setup/backblaze), for Settings
    analyzingAll: false, showDelete: false, deletePlan: null, _autoRefresh: null,
    // detail-panel layout ("classic" | "tabbed"), persisted server-side, default Classic.
    panelLayout: "tabbed", hideOverdue: false,
    confirmBox: null,  // the shared confirm dialog; truthy while it is open
    // Policies tab: overview + per-target detail from /api/policies/*; edits stage in
    // polStaged and reach kopia only through Review + save (sync POST, fail-closed backup
    // server-side). polAddPrices / polRowPrices cache "keeping out" pricing per pattern.
    polData: null, polSel: "global", polFilter: "", polStaged: {}, polAddText: "", polAddPrice: null,
    polText: null,   // exclusions as one text box: {target, text} (policies.js polTextOpen)
    polAddPrices: {}, polRowPrices: {}, polPriceError: {}, polStoredTs: {},
    _polAddTimer: null, _polSeq: 0, _polRowPriceSeq: 0, _polAddPriceSeq: 0, _polStoredSeq: 0,
    polDetail: null, polDetailCache: {}, _polDetailSeq: 0,
    polIgnore: null, _polIgnoreSeq: 0,   // ignore files (.kopiaignore) inside the selected policy's folders
    showPolDiff: false, polDiff: null, polDiffBusy: false, polDiffMode: "save",
    polRestoreName: "", polRestoreMode: "merge", polRestoreCmd: null, polRestoreParsed: null,
    showPolBackups: false, polBackups: null, _polBackupSeq: 0,
    // Policy editor: field catalogue, resolved previews per target, layout + leaving behaviour,
    // Preview / leave / new-policy boxes, and the multi-policy draft.
    polMeta: null, polResolved: {}, polResolveError: "", _polResolveSeq: 0, _polResolveTimer: null,
    polLayout: "tabs", polLeave: "ask", polSection: "exclusions", polDiffOnly: null, polFolded: {},
    polLeaveBox: null, polNewBox: null, polNewTargets: [], polPreview: null, _polPreviewSeq: 0,
    polPop: null,   // the policy editor as a popup over another tab: {} while open (policies.js polPopOpen)
    polMulti: { on: false, picked: [], fields: {}, plan: null, ticks: {}, busy: false }, _polMultiSeq: 0, _polMultiTimer: null,
    versionSizes: {}, _versionSeq: 0, _versionTimer: null,
    diskLookup: null, diskLookupMode: "everything", diskScanBudget: 15,
    diskScanThreads: 0, diskScanBackground: false, diskCountDrives: false, diskIgnoreSearch: false, diskFollowLinks: false,
    diskSizesMinutes: 0, diskShowLocal: true, diskPlaces: [], diskPlaceForm: null, _diskComputers: null,
    diskLookupText() {
      const s = this.diskLookup;
      if (!s) return "";
      if (s.provider === "everything") return "In use: Everything.";
      if (s.provider === "scan") {
        return "In use: KopiaLens scanner." + (s.mode === "auto" && s.everything && s.everything.supported && !s.everything.running
          ? (s.everything.installed ? " Everything is installed but not running." : " Everything is not installed.") : "");
      }
      return s.note || "Disk scanning isn't available.";
    },
    // exclusion dialog: priced-before-commit pattern adder over the file/folder panels.
    exDialog: null, showExclude: false, _exOpenSeq: 0, _exTargetsSeq: 0, _exPriceSeq: 0, _exCustomTimer: null, _exPriceTimer: null,
    _exDiffSeq: 0, _exDiffTimer: null, _exLeafTimer: null,
    exVariants: null, _exVarSeq: 0,   // file/folder panel "Stop backing this up" rows
    // Delete panel + the Exclude & delete flow; the rule diff view mode; the delete a held job can still run.
    showDel: false, delPanel: null, _delSeq: 0, _delStatsSeq: 0, showReview: false, flow: null,
    acc: null, accErr: "",   // Settings > Remote access
    showPrune: false, prune: null, _pruneSeq: 0, _prunePriceSeq: 0, _pruneTimer: null, pruneOutliers: null,
    diffMode: localStorage.getItem("kl-diffmode") || "changes", _jobThen: {},
    indexPending: [], indexUpdated: null,
    showMaint: false, maint: null, _maintSeq: 0,
    // Backups run by KopiaLens (engine.js) and the Tasks tab (tasks.js).
    newSnap: null, _newSnapSeq: 0, srcDel: null, verifyDlg: null, cliBox: null,
    wm: null,
    tok: null, tokNew: null, tokBusy: false, tokForm: { name: "", mode: "read_only", days: "90" },
    tokEdit: null, apList: null, apWaiting: 0, apReview: null, apPassword: "", apBusy: false, apHasAccount: false,
    notifyHook: null, notifyForm: null, notifyProfiles: null, notifyRepo: "", _klSettings: null,
    engine: null, engineDlg: null, engineSrc: {}, _engineTimer: null, _engineSrcTimer: null, _alertSeen: -1, tasksRunning: 0,
    taskRows: [], taskFilter: { repo: "all", status: "all", kind: "all", text: "" }, taskHideRoutine: true, taskRoutineSeconds: 1, taskRoutineSecs: 1, taskRoutineHours: 48, taskSel: null, taskLines: [], taskCursor: 0,
    taskLogMode: "own", taskLogSkipped: false,   // a command-line job's own output or kopia's full log
    taskHistoryDays: 0, _tasksSeq: 0, _tasksPoll: null, _taskPoll: null, _tasksSig: "",
    _restoreSeq: 0, _restoreFromSeq: 0, _restoreTimer: null, _restoreTargets: {},

    fmtB(v) { return fmtBytes(v); },
    fmtPhys(v) { return fmtPhys(v); },
    humanTime(s) { return humanTime(s); },
    freshTime(s) { return freshTime(s); },
    fmtDate(s) { return fmtDate(s); },
    fmtDur(s) { return fmtDur(s); },
    // A source's folder; with its owner (user@computer) when another owner backs up the same folder here.
    shortSource(s, owner = true) {
      const raw = String(s || "");
      const p = raw.startsWith("BLINKDISK-DRAFT-") ? sourcePath(s) + " (BlinkDisk draft)" : sourcePath(s);
      if (!owner || !raw.includes("@")) return p;
      const key = sourcePath(s).toLowerCase();
      const shared = (this.sources || []).filter((x) => sourcePath(x).toLowerCase() === key).length > 1;
      return shared ? `${p} (${raw.split(":")[0]})` : p;
    },
    // A file held by nested sources: its first source, "+N" for the rest (full list in filesSourcesTip).
    fileSources(f) { const n = (f && f.sources || []).length; return this.shortSource(f && f.source) + (n > 1 ? ` +${n - 1}` : ""); },
    fileSourcesTip(f) { return ((f && f.sources) || [f && f.source]).map((s) => this.shortSource(s)).join("\n"); },
    midTrunc(s, n) { return midTrunc(s, n); },
    // The source the open tab is showing, for the tab bar ("" when the tab has none).
    navSource() {
      const s = { tree: this.selectedSource, snapshots: this.snapSource, search: this.searchSource }[this.active];
      return s && s !== "*ALL*" && s !== "*REPOS*" ? sourcePath(s) : "";
    },
    sourceRoot() { return this.selectedSource === "*ALL*" ? "" : sourcePath(this.selectedSource); },
    treePrefix: "",

    // ---- Repo labels: ids stay the internal form; chips and columns show a short name unless
    // the "Repo labels" setting asks for the full one. Pickers and tooltips always show the full name.
    isDemo(id) {
      const r = (this.repos || []).find((x) => x.id === (id || this.repo));
      return !!(r && r.demo);
    },
    // A showcase demo copy reads as ordinary repos; its "Demo" badge has its own switch.
    demoBadge(id) {
      const r = (this.repos || []).find((x) => x.id === (id || this.repo));
      return !!(r && r.demo_badge);
    },
    repoName(id) {
      const r = this.repos.find((x) => x.id === id);
      return (r && r.name) || (id === "bz" ? "Backblaze Personal Backup" : "Repo " + id);
    },
    repoShortLabel(id) {
      const r = this.repos.find((x) => x.id === id);
      return (r && (r.short_name || r.shortName)) || (id === "bz" ? "BZ" : "Repo " + id);
    },
    repoLabel(id) { return this.repoLabels === "full" ? this.repoName(id) : this.repoShortLabel(id); },
    // A Design widget choice (dev only); without the widget every choice is its shipped ``fallback``.
    designIs(key, id, fallback) {
      return (this.design[key] || document.documentElement.getAttribute("data-d-" + key) || fallback) === id;
    },

    // Escape closes the topmost panel only. The confirm owns the first check (Cancel on Escape);
    // a panel that holds edits asks before dropping them, and Settings needs no ask (it autosaves).
    async escClose() {
      if (closeTableMenu()) return;   // an open table menu sits above everything
      if (this.confirmBox) { this.confirmResolve(false); return; }
      if (this.apReview) { this.apReview = null; return; }
      if (this.fullList) { this.closeFullList(); return; }
      if (this.docs && this.docs.view !== "window") { this.closeDocs(); return; }
      if (this.proNotice) { this.proNotice = null; return; }
      if (this.fb) { this.fbClose(null); return; }
      if (this.acctDialog) { if (this.acctDialog.mode !== "showkey") this.acctDialog = null; return; }
      if (this.cfgDlg) { if (!this.cfgDlg.busy) this.cfgDlg = null; return; }
      if (this.helpMenu) { this.helpMenu = false; return; }
      if (this.showDiag) { this.showDiag = false; return; }
      if (this.showReport) { this.showReport = false; return; }
      if (this.showFeedback) { this.showFeedback = false; return; }
      if (this.showUpdate) { this.showUpdate = false; return; }
      if (this.locked) return;    // the unlock box has no close: nothing behind it has data
      if (this.engineDlg) { this.engineDlg = null; return; }   // opens over onboarding too
      if (this.ob) return;        // onboarding is the whole page
      if (this.repoMenu) { this.repoMenu = false; return; }
      if (this.chipMenu) { this.chipMenu = ""; return; }
      if (this.timeMenu) { this.timeMenu = false; return; }
      if (this.tabMenu) { this.tabMenu = false; return; }
      if (this.tblLayout) { this.tblLayout = null; return; }
      if (this.repoDlg) { this.repoDlg = null; return; }
      if (this.polPop && !this.cliBox && !this.showPolDiff) {
        if (this.polPreview) { this.polPreview = null; return; }
        if (this.polLeaveBox) { this.polLeaveBox = null; return; }
        if (this.polNewBox) { this.polNewBox = null; return; }
        this.polPopClose(); return;
      }
      if (this.newSnap) { this.closeNewSnap(); return; }
      // Same z-index: the one later in the page is on top (index panel > maintenance > job > review).
      if (this.showIndex) { this.closeIndexPanel(); return; }
      if (this.showMaint) { this.closeMaint(); return; }
      if (this.showJob) { this.closeJob(); return; }
      if (this.showReview) { this.flowCancel(); return; }
      if (this.srcDel) { this.srcDel = null; return; }
      if (this.verifyDlg) { this.verifyDlg = null; return; }
      if (this.cliBox) { this.cliBox = null; return; }
      if (this.repoAdd) { this.closeAddRepo(); return; }
      if (this.showPrune) { this.closePrune(); return; }
      if (this.showDel) { this.closeDelete(); return; }
      if (this.showExclude) { return this.escCloseExclude(); }
      if (this.showDelete) { this.showDelete = false; return; }
      if (this.showRestore) { this.closeRestore(); return; }
      if (this.browse) { this.closeBrowse(); return; }
      if (this.showDiff) { this.showDiff = false; return; }
      if (this.showRemove) { this.closeRemove(); return; }
      if (this.showPolDiff) { this.showPolDiff = false; return; }   // Review restore opens over the backups list
      if (this.showPolBackups) { this.showPolBackups = false; return; }
      if (this.showExcludedDetail) { this.showExcludedDetail = false; return; }
      if (this.bzShowGen) { this.bzShowGen = false; return; }
      if (this.bzDetail) { this.bzDetail = null; return; }
      if (this.showSettings) {
        // Autosave rides @change (fires on blur), so a field still focused would be dropped here.
        if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
        this.showSettings = false;
        return;
      }
      if (this.polLeaveBox) { this.polLeaveBox = null; return; }
      if (this.polNewBox) { this.polNewBox = null; return; }
      if (this.detail) { this.closeDetail(); return; }
      if (this.taskSel && this.active === "tasks") { this.closeTask(); return; }
      const t = this._activeTable();   // nothing open: Escape clears the table's selection
      if (t) { t.deselectRow(); return; }
      // Single-select tables only highlight the clicked row; clear it on the visible ones.
      document.querySelectorAll(".tabulator").forEach((el) => {
        if (el._tabulator && el.offsetParent !== null) el._tabulator.deselectRow();
      });
    },
    // The multi-select table on the open tab, if any.
    _activeTable() {
      const search = this.groupByFolder ? "#groupTable" : "#searchTable";
      const id = { search, tree: "#treeTable", snapshots: "#snapshotsTable" }[this.active];
      const el = id && document.querySelector(id);
      return el && el._tabulator ? el._tabulator : null;
    },
    // Ctrl+A in a table tab selects every row on the page (not inside a text field or a panel).
    _selectAllKey(e) {
      if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== "a") return;
      const tag = (e.target && e.target.tagName) || "";
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (e.target && e.target.isContentEditable)) return;
      if (this.detail || this.showSettings || this.showDel || this.showRemove || this.showExclude || this.confirmBox) return;
      const t = this._activeTable();
      if (!t) return;
      e.preventDefault();
      t.selectRow("active");
    },
    async escCloseExclude() {
      if (this.flow) return this.flowCancel();
      const d = this.exDialog;
      // Only work the USER put in: a typed custom pattern, a shape they picked, ticks they moved.
      // (Smart's own default ticks are not an edit; asking on those would nag on every Escape.)
      const edits = !!(d && (d.custom || d.userShape || d.reach === "manual"));
      if (edits) {
        const ok = await this.klConfirm({
          title: "Close without adding the rule?",
          body: "The exclusion you set up was not added to any backup.",
          confirmLabel: "Close", danger: true,
        });
        if (!ok) return;
      }
      this.exClose();
    },

    async init() {
      // Alpine fires the data object's init() AND x-init="init()" re-calls it. A second run
      // re-picks the already-resolved data-theme and loses "system", and doubles every
      // startup fetch.
      if (this._inited) return;
      this._inited = true;
      this.$watch("actionText", (v) => this.pushNotice(v, "info"));
      this.$watch("active", (_v, old) => viewAbort(old));   // a tab left behind stops its requests
      this.$watch("repo", () => { viewAbort("tree"); viewAbort("search"); });
      window.addEventListener("kl-locked", (e) => this.showLock(e.detail));
      window.addEventListener("kl-setup-code", () => this.obNeedCode());
      window.addEventListener("kl-listing-failed", (e) => {
        const n = e.detail;
        this.listingNotices = [n, ...this.listingNotices.filter((x) => x.repo !== n.repo)];
      });
      window.addEventListener("kl-table-layout", (e) => this.openTableLayout(e.detail.key));
      window.addEventListener("kl-table-rebuild", () => this.rebuildTables());
      window.addEventListener("design-change", (e) => {
        this.design = { ...this.design, [e.detail.key]: e.detail.value };
      });
      this.initHelp();
      this.wmLoad();
      // A fresh install (no repository yet) or a closed setup window shows onboarding, and nothing
      // else loads until it is finished.
      try {
        const obState = await getJson("/api/onboarding/state").finally(() => { this._obChecked = true; });
        if (obState.needed || !obState.setup_unlocked) {
          window.addEventListener("keydown", (e) => { if (e.key === "Escape") this.escClose(); });
          await this.obBegin(obState);
          return;
        }
        this.setupSkipped = !!obState.setup_skipped;
      } catch (e) { /* an older server or a network hiccup: the app loads as usual */ }
      try {   // a note carried across a reload (start over)
        const flash = sessionStorage.getItem("kl-flash");
        if (flash) { sessionStorage.removeItem("kl-flash"); setTimeout(() => this.klNotify(flash), 0); }
      } catch (e) { /* storage blocked: no note */ }
      await this.wrap(async () => {
        TABLE_PREFS = await getJson("/api/settings/tables").catch(() => ({})) || {};
        const data = await getJson("/api/repos");
        this.repos = data.repos;
        // Setup skipped before any repository: an empty app with the way to add one.
        this.noRepos = !data.repos.length;
        const kl = data.settings?.kopialens || {};
        // URL ?repo= pins this tab to a repo (survives refresh, never writes last_repo) -> two
        // browser tabs can hold different repos without the last_repo setting flip-flopping.
        const urlRepo = new URLSearchParams(location.search).get("repo");
        if (urlRepo && data.repos.some((r) => r.id === urlRepo)) { this.repo = urlRepo; this.repoPinned = true; }
        else {
          // A saved repo that no longer exists (or none saved) falls back to the first real repo.
          const saved = data.settings?.last_repo;
          const first = data.repos.find((r) => r.kind !== "backblaze") || data.repos[0];
          this.repo = data.repos.some((r) => r.id === saved) ? saved : (first ? first.id : "");
        }
        this.repoKind = this.noRepos ? "" : this.kindOf(this.repo);
        const urlDocs = new URLSearchParams(location.search).get("docs");   // "Open in a new window" from the docs browser
        if (urlDocs) { this._docsWindow = true; this.openDocs(urlDocs, decodeURIComponent(location.hash.slice(1))); }
        this.loadLicence();
        window.addEventListener("focus", () => this.refreshLicence());
        window.addEventListener("kl-pro-denied", () => this.refreshLicence(true));
        getJson("/api/app/about").then((a) => this.kopiaCompatNote(a.kopia)).catch(() => {});
        getJson("/api/app/host").then((h) => {
          this.host = h;
          if (!h.data_persistent) this.klNotify("The data folder isn't on a Docker volume. The index and settings are lost when the container is recreated.", "warn");
        }).catch(() => {});
        // A locked index shows the unlock box on load, not only once a view happens to read it.
        getJson("/api/encryption/status").then((s) => { if (s.enabled && !s.unlocked) this.showLock({}); }).catch(() => {});
        this.loadCfg();   // a damaged settings file restored at startup shows its notice
        this.loadIxNotices();
        this.backend = data.settings?.listing_backend || "subprocess";
        const _ps = String(kl.page_size || 500);  // snap to a valid <option> so the select can't visually revert to 100
        this.searchPageSize = this.pageSizes.includes(_ps) ? _ps : "500";
        this.snapSourceByRepo = kl.snap_source_by_repo || {};
        this.treeSourceByRepo = kl.tree_source_by_repo || {};
        this.ownerFilterByRepo = kl.owner_filter_by_repo || {};
        this.simPolicyByRepo = kl.sim_policy_by_repo || {};
        this.snapSource = this.snapSourceByRepo[this.repo] || kl.snap_source || "*ALL*";  // default All sources
        this.numberStyle = kl.number_style || "system";
        this.byteUnits = kl.byte_units === "binary" ? "binary" : "decimal";
        this.dateStyle = kl.date_style || "system";
        this.clock = String(kl.clock || "system");
        setFormatStyles(kl);
        this.panelLayout = kl.panel_layout === "classic" ? "classic" : "tabbed";  //
        this.hideOverdue = kl.hide_overdue === true;
        this.docsView = kl.docs_view === "side" ? "side" : "popup";
        this.polLayout = kl.policy_layout === "scroll" ? "scroll" : "tabs";
        this.polFolded = Object.fromEntries((kl.policy_folded || []).map((k) => [k, true]));
        this.polLeave = kl.policy_leave === "keep" ? "keep" : "ask";
        this.repoLabels = kl.repo_labels === "full" ? "full" : "short";
        this.showProBadge = kl.show_pro_badge !== false;
        this._readLook(kl);
        this.searchSort = kl.search_sort || "stored";
        this.searchDir = kl.search_dir || "desc";
        this.dedupe = !!kl.search_dedupe;
        this.dedupeBy = kl.search_dedupe_by === "content" ? "content" : "path";
        this.searchKind = ["files", "folders", "both"].includes(kl.search_kind) ? kl.search_kind : "files";
        this.groupByFolder = this.searchKind === "folders";
        this.headerSearchOnly = kl.header_search_only === true;
        this.recentBackup = kl.recent_backup || "";  // "recently backed up" preset (days; "" = Any)
        this.treeDedupe = !!kl.tree_dedupe;
        this.treeMode = kl.tree_mode || "full";
        this.treeView = kl.tree_view === "tree" ? "tree" : "list";
        this.groupDepth = Number(kl.group_depth || 6);
        this.groupMinDepth = Number(kl.group_min_depth) > 1 ? Number(kl.group_min_depth) : "";
        this.groupLoose = kl.group_loose !== false;
        this.groupMode = ["deepest", "nested", "own"].includes(kl.group_depth_mode) ? kl.group_depth_mode : "deepest";
        this.churnDays = String(kl.churn_days || 30);
        this.churnDepth = Number(kl.churn_depth || 4);
        this.churnView = kl.churn_view || "folder";
        this.indexMode = kl.index_mode || "daily";
        this.indexTime = kl.index_time || "23:00";
        this.indexInterval = Number(kl.index_interval || 86400);
        this.dashboardMode = kl.dashboard_mode || "nightly";
        this.dashRefresh = Number(kl.dashboard_refresh ?? 1800);
        this.excludedTolerance = Number(kl.excluded_tolerance ?? 0.05);
        this.esPath = kl.es_path || "";
        // Also at startup, not only in openSettings: if that re-fetch ever fails, the advanced
        // fields would still hold constructor defaults and a Save would write those over the real
        // values.
        this._readAdvanced(kl);
        this._readBackblaze(data.settings?.backblaze || {});
        this.initTheme(kl);  // sync theme/density state (server stamp already painted)
        if (kl.time_format) { TIME_FMT = kl.time_format; this.timeFmt = kl.time_format; }
        if (this.bzApplySettings) this.bzApplySettings(kl);  // restore persisted BZ prefs (tree drive, depths, toggles)
        this._tourAfterSetup();
        if (this.noRepos) {
          // Nothing to load; adding the first repository reloads into the normal app (closeAddRepo).
        } else if (this.repoKind === "backblaze") {
          this.active = "bz-dashboard";
          await this.bzInit();
        } else {
          if (this.repoKind === "server") this.active = "snapshots";
          await this.refreshBase();
          this.selectedSource = this._rememberedTreeSource();
          if (this.indexStatus.building) this.pollIndex();  // show progress for the auto-triggered (migration) rebuild
          // The open tab, not always the Dashboard: a tab clicked before the sources arrived
          // loaded nothing.
          await this.activate();
        }
      });
      if (!this.noRepos) {
        this.armAutoRefresh();
        this.armFreshness();
        this.armJobChip();
        this.armEngine();
      }
      // One Escape handler for every panel: it closes the TOPMOST one and nothing else, so a
      // confirm never resolves and closes the panel under it in the same keypress.
      window.addEventListener("keydown", (e) => { if (e.key === "Escape") this.escClose(); else this._selectAllKey(e); });
      window.addEventListener("beforeunload", (e) => {
        if (this.polStageCount()) { e.preventDefault(); e.returnValue = ""; }
      });
      this._prevRepo = this.repo;
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
      if (sec > 0) this._autoRefresh = setInterval(() => { if (this.active === "dashboard" && !this.busy && !document.hidden) this.loadDashboard(); }, sec * 1000);
    },

    klSet(patch) {  // persist a kopialens settings patch in the backend (all prefs are permanent)
      getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kopialens: patch }) }).catch(() => {});
    },

    async wrap(fn, label = "") {
      // A later wrap owns the label; this one must not clear it. A number, not an object: Alpine
      // hands back a proxy for an object read off its state, so === would never match.
      const mine = this._busyOwner = (this._busyOwner || 0) + 1;
      this.error = ""; this.busy = label;
      try { return await fn(); } catch (err) { if (!isAbort(err) && !err.notice) this.error = err.message || String(err); } finally { if (this._busyOwner === mine) this.busy = ""; }
    },

    toggleTimeFmt() { this.setTimeFmt({ auto: "relative", relative: "absolute" }[TIME_FMT] || "auto"); },
    copyText(t) { try { navigator.clipboard.writeText(t); } catch (e) {} },
    _downloadText(name, text) {  // trigger a browser download of generated text (e.g. a .bat)
      const blob = new Blob([text], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = name; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },

    // A repo that has never been indexed has no data for the index-backed views; a friendly
    // one-liner beats the raw 500 they would otherwise each raise (observed on a fresh repo).
    _neverIndexed() {   // the banner above the views says so and offers to start
      return !!(this.indexStatus && this.indexStatus.never_indexed);
    },
    notIndexedTitle() { return this.indexStatus && this.indexStatus.building ? "Building the first index…" : "This repository isn't indexed yet"; },
    notIndexedNote() {
      if (this.indexStatus && this.indexStatus.building) return "You can keep using KopiaLens. The pages fill in when it finishes.";
      if (this.indexMode === "off") return "Automatic updates are off. Start indexing to see this repository's data.";
      if (this.indexMode === "interval") return "It runs with the next automatic update, or start it now.";
      return `It runs automatically at ${this.indexTime}, or start it now.`;
    },

    // ---- Freshness strip: how current each repo's data is (one call covers all three) ----
    async loadFreshness() {
      try {
        const f = await getJson("/api/index/freshness");
        this.freshness = f.repos || [];
        this.indexPause = f.pause || { paused: false };
        const seen = this._lastIndexSeen || {};
        if (this.freshness.some((r) => r.repo in seen && seen[r.repo] !== r.last_index)) this.forgetRowValues();
        this._lastIndexSeen = Object.fromEntries(this.freshness.map((r) => [r.repo, r.last_index]));
      } catch (e) { /* non-critical */ }
    },
    // Values worked out per row and remembered (Tree, Search folders, Churn, Excluded, versions)
    // are stale once an index changes; they are worked out again as rows show.
    forgetRowValues() {
      this.treeExtra = {}; this.groupReclaim = {}; this.folderVersions = {};
      this._churnPhysCache = {}; this.versionSizes = {}; this._excludedDrivesStale = true;
    },
    freshAge(row) {
      if (row.via_server) return "browse-only";
      if (row.running) return "indexing now";
      if (!row.last_index) return "never";
      const mins = Math.round((Date.now() / 1000 - row.last_index) / 60);
      if (mins < 60) return `${mins}m ago`;
      const hrs = Math.round(mins / 60);
      return hrs < 36 ? `${hrs}h ago` : `${Math.round(hrs / 24)}d ago`;
    },
    freshStale(row) {  // amber past a day and a half - long enough that a missed run is the reason
      return !row.via_server && !row.running && (!row.last_index || (Date.now() / 1000 - row.last_index) > 36 * 3600);
    },
    // Tooltip per strip row. BZ: verification age ("checked Xm ago") + queued/failed states -
    // the badge itself carries the rebuild age, so the two facts stay separate (2026-09-13).
    freshTitle(row) {
      if (row.via_server) return row.name + " - reached through a kopia server, so it has no index";
      if (row.kind !== "backblaze") return row.name + (row.error ? " - last run failed: " + row.error : "");
      const parts = [];
      if (row.last_check_ts) {
        const mins = Math.max(0, Math.round((Date.now() / 1000 - row.last_check_ts) / 60));
        parts.push("checked for changes " + (mins < 60 ? mins + "m ago" : mins < 1440 ? Math.round(mins / 60) + "h ago" : Math.round(mins / 1440) + "d ago"));
      }
      if (row.pending) parts.push("new data queued");
      if (row.error) parts.push("last run failed: " + row.error);
      return row.name + (parts.length ? " - " + parts.join(" · ") : "");
    },

    async repoChanged() {
      if (this.polStageCount()) {
        const next = this.repo;
        this.repo = this._prevRepo;
        if (!(await this.polConfirmDiscard("Unsaved policy changes belong to this repo."))) return;
        this.repo = next;
      }
      this._prevRepo = this.repo;
      this.selectedSnapshot = ""; this.snapshots = [];
      this.statsData = null; this.blobStats = "";  // stats are per-repo; force reload
      this.polData = null; this.polBackups = null; this.polDiscard();  // policies are per-repo too
      this.polMeta = null; this.polResolved = {}; this.polNewTargets = [];
      this.polMulti = { on: false, picked: [], fields: {}, plan: null, ticks: {}, busy: false };
      this.versionSizes = {};
      this._simOne = null;
      this._stopAnalyzePoll();
      this.pruneOutliers = null;
      this.searchViewRows = null;
      this.repoKind = this.kindOf(this.repo);
      if (this.repoPinned) {  // pinned tab: keep the choice in the URL, don't touch shared last_repo
        const u = new URL(location.href); u.searchParams.set("repo", this.repo); history.replaceState(null, "", u);
      } else {
        getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ last_repo: this.repo }) }).catch(() => {});
      }
      // The old repo's view stays on screen until the new one loads: dim it and say what loads.
      const seq = ++this._repoSwitchSeq;
      this.repoSwitching = true;
      try {
        await this.wrap(async () => {
          if (this.repoKind === "backblaze") {  // BZ repo selected -> its own tabs + loaders
            if (!String(this.active).startsWith("bz-")) this.active = "bz-dashboard";
            return this.bzInit();
          }
          if (String(this.active).startsWith("bz-")) this.active = "dashboard";  // coming back from BZ
          if (this.repoKind === "server" && !this.serverTabs.some((t) => t.id === this.active)) this.active = "snapshots";
          await this.refreshBase(true);
          this.selectedSource = this._rememberedTreeSource();
          this.snapSource = this.snapSourceByRepo[this.repo] || "*ALL*";
          await this.activate();
        }, `Loading ${this.repoName(this.repo)}…`);
      } finally {
        if (seq === this._repoSwitchSeq) this.repoSwitching = false;
      }
    },
    // The Tree's saved source for this repo when it still exists, else the first source.
    _rememberedTreeSource() {
      const saved = this.treeSourceByRepo[this.repo];
      if (saved === "*ALL*" || (saved && this.sources.includes(saved))) return saved;
      return this.sources[0] || "";
    },
    // Re-apply the number/date style and redraw the open tab.
    applyFormats() {
      setFormatStyles({ number_style: this.numberStyle, date_style: this.dateStyle, clock: this.clock, byte_units: this.byteUnits });
      this.activate();
    },

    async refreshBase(force = false) {
      const [status, sources] = await Promise.all([
        getJson(`/api/index/status?repo=${encodeURIComponent(this.repo)}`),
        getJson(`/api/sources?repo=${encodeURIComponent(this.repo)}&force=${force}`),
      ]);
      this.indexStatus = status;
      this.sources = sources.sources || [];
      if (this.selectedSource !== "*ALL*" && !this.sources.includes(this.selectedSource)) this.selectedSource = this.sources[0] || "";
      this.loadClient();
    },

    activate() {
      if (this._excludedSeen && this.active !== "excluded") { this._excludedSeen = false; this.excludedLeft(); }
      if (this.repoKind === "backblaze") return this.bzActivate();
      // Snapshots reads kopia's own list, so it works before the first index too.
      if (["dashboard", "tree", "search", "churn", "stats"].includes(this.active)
          && this._neverIndexed()) return;
      if (this.active === "dashboard") return this.loadDashboard();
      if (this.active === "snapshots") return this.loadSnapshots();
      if (this.active === "tree") return this.openTree();
      if (this.active === "search") return this.loadSearch();
      if (this.active === "churn") return this.loadChurn();
      if (this.active === "simulate") return this.openSimulate();  // a saved result only; Run computes
      if (this.active === "stats") return this.statsData ? null : this.loadStats();
      if (this.active === "logs") return this.loadLogs();
      if (this.active === "excluded") return this.loadExcluded();
      if (this.active === "policies") return this.loadPolicies();
      if (this.active === "tasks") return this.loadTasks();
    },

    goHome() {  // the logo: first tab of whatever kind of repo is open (Backblaze and server repos have no Dashboard)
      this.active = this.currentTabs()[0].id;
      return this.activate();
    },

    headerSearch() {  // top search box (Option B): drives BZ search when a Backblaze repo is active
      if (this.repoKind === "server") return;   // search needs the index
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
        if (this.active === "search") return this.searchView ? this.loadSearchView(true) : this.loadSearch();
        if (this.active === "churn") return this.loadChurn(true);
        if (this.active === "stats") return this.loadStats(true);
        if (this.active === "logs") return this.loadLogs();
        if (this.active === "policies") return this.loadPolicies(true);
        if (this.active === "tasks") return this.loadTasks();
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
      const firstIndex = !!(this.indexStatus && this.indexStatus.never_indexed);
      let loadedEarly = false;
      this._poll = setInterval(async () => {
        try {
          this.indexStatus = await getJson(`/api/index/status?repo=${encodeURIComponent(this.repo)}`);
          // A first index is usable once it is published; the later stages fill in after.
          if (firstIndex && !loadedEarly && this.indexStatus.building && !this.indexStatus.never_indexed) {
            loadedEarly = true;
            this.error = "";
            await this.refreshBase(true);
            this.activate();
          }
          if (!this.indexStatus.building) {
            clearInterval(this._poll); this._poll = null;
            // A repo's FIRST index: the views still show "not indexed yet" - clear it and load the
            // active tab now that data exists. Later runs keep the view as it is.
            if (firstIndex && !this.indexStatus.never_indexed) {
              this.error = "";
              await this.refreshBase(true);
              this.activate();
            }
          }
        } catch (e) { clearInterval(this._poll); this._poll = null; }
      }, 3000);
    },
    // Week options: the view lists what the snapshots taken INSIDE that week recorded as
    // changed - the delta, not a full point-in-time tree. Value = the week's last-day ISO date.
    get treeWeekOptions() {
      const iso = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      const lbl = (d) => d.toLocaleDateString(dateLocale(), { month: "short", day: "numeric" });
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
    // Search's Folders view is its own table; its rows are mapped to the Both view's folder shape.
    _selTableId(scope) { return scope === "tree" ? "#treeTable" : this.groupByFolder ? "#groupTable" : "#searchTable"; },
    selectedRows(scope) {
      const id = this._selTableId(scope);
      const el = document.querySelector(id);
      const rows = (el && el._tabulator) ? el._tabulator.getSelectedData() : [];
      if (id !== "#groupTable") return rows;
      return rows.map((r) => ({ kind: "folder", full_path: r.folder, source: r.source || null, sources: r.sources,
                                size: r.logical, physical: r.physical != null ? r.physical : r.phys, files: r.files }));
    },

    // Tree rows mark folders with type, Search rows (Both view) with kind.
    isFolderRow(r) { return r.type === "folder" || r.kind === "folder"; },

    clearSelection(scope) {
      const el = document.querySelector(this._selTableId(scope));
      if (el && el._tabulator) el._tabulator.deselectRow();
    },

    csv(id, name) { downloadTable(id, name); },
    // A download's name (util.js exportName) with this repository's short name; repo "*" = all repositories.
    fileName(what, about = "", ext = "csv", repo = this.repo) {
      return exportName(what, about, repo === "*" ? "all-repos" : this.repoShortLabel(repo), ext);
    },
    };
    // Duplicate-key guard: two parts declaring the same key would compose silently last-wins.
    const parts = [["core", core], ["toastPart", toastPart], ["tokensPart", tokensPart], ["bzIndexPart", bzIndexPart], ["bzDashboardPart", bzDashboardPart], ["bzSearchPart", bzSearchPart], ["bzTreePart", bzTreePart], ["bzExcludedPart", bzExcludedPart], ["bzExclusionsPart", bzExclusionsPart], ["bzChurnPart", bzChurnPart], ["bzHealthPart", bzHealthPart], ["dashboardPart", dashboardPart], ["snapshotsPart", snapshotsPart], ["browsePart", browsePart], ["treePart", treePart], ["searchPart", searchPart], ["indexPanelPart", indexPanelPart], ["churnPart", churnPart], ["simulatePart", simulatePart], ["statsPart", statsPart], ["logsPart", logsPart], ["excludedPart", excludedPart], ["actionsPart", actionsPart], ["confirmPart", confirmPart], ["runbarPart", runbarPart], ["detailPart", detailPart], ["excludePart", excludePart], ["deletePart", deletePart], ["prunePart", prunePart], ["diffviewPart", diffviewPart], ["jobsPart", jobsPart], ["maintenancePart", maintenancePart], ["settingsPart", settingsPart], ["accountPart", accountPart], ["appConfigPart", appConfigPart], ["reposPart", reposPart], ["onboardingPart", onboardingPart], ["themePart", themePart], ["policiesPart", policiesPart], ["policyEditPart", policyEditPart], ["licencePart", licencePart], ["helpPart", helpPart], ["tasksPart", tasksPart], ["enginePart", enginePart], ["sourceOpsPart", sourceOpsPart], ["notifyPart", notifyPart], ["writeModePart", writeModePart], ["fullListPart", fullListPart], ["docsPart", docsPart], ["tablesPart", tablesPart]];
    const seen = new Map();
    for (const [owner, part] of parts)
      for (const k of Object.keys(part)) {
        if (seen.has(k)) console.error(`kopiaLens: duplicate key "${k}" declared in both ${seen.get(k)} and ${owner}`);
        else seen.set(k, owner);
      }
    return Object.assign(...parts.map(([, part]) => part));   // the same list the guard checked
  });
});
