const fmtBytes = (value) => {
  const n = Number(value || 0);
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${n} B`;
};
const fmtPhys = (v) => (v === null || v === undefined ? "—" : fmtBytes(v));

const fmtAbs = (s) => {
  if (!s) return "";
  const d = new Date(s);
  return isNaN(d.getTime()) ? String(s).slice(0, 19)
    : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true });
};
const fmtRel = (s) => {
  if (!s) return "";
  const d = new Date(s);
  if (isNaN(d.getTime())) return String(s).slice(0, 19);
  let sec = (Date.now() - d.getTime()) / 1000;
  const future = sec < 0; sec = Math.abs(sec);
  for (const [name, span] of [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]]) {
    if (sec >= span) { const n = Math.floor(sec / span); const lbl = `${n} ${name}${n > 1 ? "s" : ""}`; return future ? `in ${lbl}` : `${lbl} ago`; }
  }
  return future ? "in a moment" : "just now";
};
let TIME_FMT = localStorage.getItem("kl-timefmt") || "absolute";
const humanTime = (s) => (TIME_FMT === "relative" ? fmtRel(s) : fmtAbs(s));
const timeCell = (s) => {
  if (!s) return "—";
  const abs = fmtAbs(s), rel = fmtRel(s);
  return TIME_FMT === "relative" ? `<span title="${abs}">${rel}</span>` : `<span title="${rel}">${abs}</span>`;
};

const sourcePath = (s) => (s && s.includes(":") ? s.split(":").slice(1).join(":") : (s || ""));
const isUnc = (p) => p.startsWith("\\\\");
const srcSortKey = (s) => { const p = sourcePath(s); return (isUnc(p) ? "1" : "0") + "|" + p.toLowerCase(); };
const srcSorter = (a, b) => srcSortKey(a).localeCompare(srcSortKey(b));

const getJson = async (url, options = {}) => {
  const res = await fetch(url, options);
  // Tolerate a non-JSON body (e.g. a plain-text 500 "Internal Server Error") instead of throwing
  // an "Unexpected token" parse error - surface the text as the message.
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
  if (!res.ok) throw new Error((data && (data.detail || data.error)) || text || res.statusText);
  return data;
};

// ---- Backend-persisted table preferences (columns + sort), survives browser-data clears ----
let TABLE_PREFS = {};
const _persistTimers = {};
function persistReader(id, type) { return (TABLE_PREFS[id] && TABLE_PREFS[id][type]) || false; }
function persistWriter(id, type, data) {
  TABLE_PREFS[id] = TABLE_PREFS[id] || {};
  TABLE_PREFS[id][type] = data;
  clearTimeout(_persistTimers[id]);
  _persistTimers[id] = setTimeout(() => {
    getJson("/api/settings/table", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, config: TABLE_PREFS[id] }),
    }).catch(() => {});
  }, 600);
}

function colVisibilityMenu(e, column) {
  return column.getTable().getColumns().map((col) => ({
    label: (col.isVisible() ? "☑ " : "☐ ") + (col.getDefinition().title || col.getField()),
    action: (ev) => { ev.stopPropagation(); col.toggle(); },
  }));
}

const setTable = (id, data, columns, opts = {}) => {
  const el = document.querySelector(id);
  if (!el) return null;
  const { onRowDblClick, rowMenu, initialSort, persistSort, ...rest } = opts;
  if (el._tabulator) { try { el._tabulator.destroy(); } catch (e) {} el._tabulator = null; }
  columns.forEach((c) => {
    c.headerContextMenu = colVisibilityMenu;
    if (!c.sorter && data && data.length && typeof data[0][c.field] === "number") c.sorter = "number";
  });
  const table = new Tabulator(id, {
    data, columns,
    layout: "fitColumns",
    height: "100%",
    movableColumns: true,
    selectableRows: 1,
    placeholder: "No rows",
    persistence: { columns: true, sort: persistSort !== false },
    persistenceID: id.replace("#", ""),
    persistenceReaderFunc: persistReader,
    persistenceWriterFunc: persistWriter,
    initialSort: initialSort || undefined,
    rowContextMenu: rowMenu || undefined,
    ...rest,
  });
  if (onRowDblClick) table.on("rowDblClick", onRowDblClick);
  el._tabulator = table;
  return table;
};

const downloadTable = (id, name) => {
  const el = document.querySelector(id);
  if (el && el._tabulator) el._tabulator.download("csv", `${name}.csv`);
};

document.addEventListener("alpine:init", () => {
  Alpine.data("kopiaLens", () => ({
    ...(window.bzMixin ? window.bzMixin() : {}),  // Backblaze view (bz* state + methods, from backblaze.js)
    repos: [], repo: "1", repoKind: "kopia",
    tabs: [
      { id: "dashboard", label: "Dashboard" }, { id: "snapshots", label: "Snapshots" },
      { id: "tree", label: "Tree" }, { id: "search", label: "Search" },
      { id: "churn", label: "Churn" }, { id: "simulate", label: "Simulate" },
      { id: "stats", label: "Stats" }, { id: "excluded", label: "Excluded" },
      { id: "actions", label: "Actions" },
    ],
    active: "dashboard",
    currentTabs() { return this.repoKind === "backblaze" ? this.bzTabs : this.tabs; },
    dark: false,
    timeFmt: localStorage.getItem("kl-timefmt") || "absolute",
    indexStatus: {},
    sources: [], selectedSource: "",
    snapshots: [], selectedSnapshot: "",
    showChurn: localStorage.getItem("kl-churn") === "1",
    analyzing: false,
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
    groupReclaim: {},  // folder -> freeable bytes, computed on demand per group row
    mountSnapshot: "", mountPoint: "", restoreSource: "", restoreDest: "",
    dashboardMeta: "", busy: "", error: "", detail: null, batch: null, showBatch: false, snapSource: "*ALL*", _poll: null, _searchSeq: 0, _detailSeq: 0,
    _dashSeq: 0, _snapSeq: 0, _treeSeq: 0, _batchSeq: 0, _batchCmdSeq: 0, _removePlanSeq: 0, _removeDetailSeq: 0,
    showRemove: false, removePlan: null, removeJobPoll: null, _removeBase: null,
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

    fmtB(v) { return fmtBytes(v); },
    fmtPhys(v) { return fmtPhys(v); },
    humanTime(s) { return humanTime(s); },
    shortSource(s) { return sourcePath(s); },
    sourceRoot() { return this.selectedSource === "*ALL*" ? "" : sourcePath(this.selectedSource); },

    treeCrumbs() {
      const root = this.sourceRoot();
      const rootTrim = root.replace(/[\\/]+$/, "");
      const crumbs = [{ label: rootTrim || "root", prefix: root }];
      const cur = this.treePrefix || "";
      if (cur.length > root.length) {
        const tail = cur.slice(root.length).replace(/^[\\/]+/, "");
        let acc = rootTrim;
        for (const seg of tail.split("\\").filter(Boolean)) {
          acc = acc + "\\" + seg;
          crumbs.push({ label: seg, prefix: acc });
        }
      }
      return crumbs;
    },
    treePrefix: "",

    async init() {
      this.dark = localStorage.getItem("kl-dark") === "1";
      document.documentElement.classList.toggle("dark", this.dark);
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

    toggleDark() {
      this.dark = !this.dark;
      document.documentElement.classList.toggle("dark", this.dark);
      localStorage.setItem("kl-dark", this.dark ? "1" : "0");
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

    // ---------- Dashboard ----------
    async loadDashboard(refresh = false) {
      const seq = ++this._dashSeq;
      await this.wrap(async () => {
        const data = await getJson(`/api/dashboard?repo=${encodeURIComponent(this.repo)}&refresh=${refresh}`);
        if (seq !== this._dashSeq) return;  // a newer load superseded this one
        this.dashboardMeta = `true physical ${fmtBytes(data.union_total)}` + (data.cached ? "" : ` · computed in ${Number(data.elapsed || 0).toFixed(1)}s`);
        // Skip re-rendering on the periodic auto-refresh when the data is unchanged (avoids flicker).
        // hash = the backend snaphash: it flips whenever the index moves, so another source's
        // physical/freeable changing is no longer hidden by an unchanged first row.
        // computed_at flips on every cache REBUILD, covering same-hash rebuilds that only moved
        // e.g. next_snapshot times; polls of an untouched cache keep matching and never re-render.
        const sig = `${this.repo}|${data.hash || ""}|${data.computed_at || ""}|${data.union_total}|${(data.rows || []).length}|${(data.rows && data.rows[0] && data.rows[0].last_time) || ""}`;
        const dEl = document.querySelector("#dashboardTable");
        if (!refresh && dEl && dEl._tabulator && sig === this._dashSig) return;
        this._dashSig = sig;
        setTable("#dashboardTable", data.rows || [], [
          { title: "Source", field: "source", widthGrow: 4, minWidth: 240, sorter: srcSorter, formatter: (c) => sourcePath(c.getValue()) },
          { title: "Snaps", field: "snaps", width: 70, hozAlign: "right" },
          { title: "Files (latest)", field: "files_latest", width: 100, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Paths (all)", field: "files_distinct", width: 95, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Physical", field: "physical", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Accum", field: "accum", width: 75, formatter: (c) => `${Number(c.getValue()).toFixed(1)}x`, hozAlign: "right" },
          { title: "%Repo", field: "repo_pct", width: 72, formatter: (c) => `${Number(c.getValue()).toFixed(1)}%`, hozAlign: "right" },
          { title: "Freeable", field: "freeable", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Dedup", field: "dedup", width: 72, formatter: (c) => `${Number(c.getValue()).toFixed(0)}x`, hozAlign: "right" },
          { title: "Latest size", field: "logical_latest", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Latest physical", field: "physical_latest", width: 110, formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Last snapshot", field: "last_time", width: 165, formatter: (c) => timeCell(c.getValue()) },
          { title: "Next snapshot", field: "next_snapshot", width: 165, formatter: (c) => timeCell(c.getValue()) },
          { title: "Pins", field: "pins", width: 64, hozAlign: "right" },
        ], {
          initialSort: [{ column: "source", dir: "asc" }],
          onRowDblClick: (_e, row) => { this.selectedSource = row.getData().source; this.snapSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); },
          rowMenu: [
            { label: "Browse snapshots", action: (_e, row) => { this.selectedSource = row.getData().source; this.snapSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); } },
            { label: "Browse files (tree)", action: (_e, row) => { this.selectedSource = row.getData().source; this.active = "tree"; this.openTree(); } },
            { label: "Simulate this source", action: (_e, row) => { this.selectedSource = row.getData().source; this.simMode = "one"; this.active = "simulate"; this.runSimulate(); } },
            { label: "Copy source", action: (_e, row) => this.copyText(row.getData().source) },
          ],
        });
      }, "Loading dashboard...");
    },

    // ---------- Snapshots ----------
    async loadSnapshots(refresh = false) {
      if (!this.snapSource) this.snapSource = "*ALL*";
      if (this.snapSource !== "*ALL*") this.selectedSource = this.snapSource;  // keep tree-drill/mount in sync
      const seq = ++this._snapSeq;
      await this.wrap(async () => {
        const data = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.snapSource)}`);
        if (seq !== this._snapSeq) return;  // a newer load superseded this one
        this.snapshots = data.snapshots || [];
        this.analyzing = !!data.analyzing;
        if (!this.snapshots.find((x) => x.id === this.selectedSnapshot)) this.selectedSnapshot = this.snapshots.length ? this.snapshots[this.snapshots.length - 1].id : "";
        const allSrc = this.snapSource === "*ALL*";
        const cols = [
          ...(allSrc ? [{ title: "Source", field: "source", widthGrow: 2, minWidth: 200, formatter: (c) => sourcePath(c.getValue()) }] : []),
          { title: "Start", field: "start_time", width: 185, formatter: (c) => timeCell(c.getValue()) },
          { title: "Logical", field: "logical", width: 100, sorter: "number", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Physical", field: "physical", width: 100, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Accum", field: "accum", width: 72, sorter: "number", formatter: (c) => { const v = c.getValue(); return (v == null) ? "—" : `${Number(v).toFixed(2)}x`; }, hozAlign: "right" },
          { title: "Churn (new)", field: "churn", width: 105, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Reclaimable", field: "reclaimable", width: 110, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", width: 90, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Dirs", field: "dirs", width: 80, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Retention", field: "retention", widthGrow: 2, minWidth: 150, formatter: (c) => (c.getValue() || []).map((r) => `<span class="pill" style="background:#dbeafe;color:#1e40af">${r}</span>`).join("") },
          { title: "Pins", field: "pins", widthGrow: 1, minWidth: 90, formatter: (c) => (c.getValue() || []).map((p) => `<span class="pill" style="background:#fef3c7;color:#92400e">${p}</span>`).join("") },
          { title: "ID", field: "id", width: 200 },
        ];
        const stbl = setTable("#snapshotsTable", this.snapshots, cols, {
          selectableRows: true,  // multi-select for bulk delete / pin / diff
          initialSort: allSrc ? [{ column: "reclaimable", dir: "desc" }] : [{ column: "start_time", dir: "desc" }],
          onRowDblClick: (_e, row) => { const d = row.getData(); this.selectedSource = d.source; this.selectedSnapshot = d.id; this.mountSnapshot = this.selectedSnapshot; this.active = "tree"; this.treeMode = "full"; this.treePrefix = this.sourceRoot(); this.loadTree(); },
          rowMenu: [
            { label: "Browse files", action: (_e, row) => { const d = row.getData(); this.selectedSource = d.source; this.selectedSnapshot = d.id; this.active = "tree"; this.treeMode = "full"; this.treePrefix = this.sourceRoot(); this.loadTree(); } },
            { label: "Restore this snapshot…", action: (_e, row) => this.openRestore(row.getData().root || row.getData().id, "snapshot " + row.getData().id.slice(0, 12)) },
            { label: "Mount snapshot (read-only)", action: (_e, row) => this.mountRow(row.getData().id) },
            { label: "Mount + open in Explorer", action: (_e, row) => this.mountOpen(row.getData().id, this.selectedSource, this.sourceRoot()) },
            { label: "Select all unretained (no retention/pin)", action: () => this.selectUnretained() },
            { label: "Select identical duplicates (keep oldest+newest per content)", action: () => this.selectIdenticalDuplicates() },
            { label: "Delete selected…", action: () => this.planDelete() },
            { label: "Pin selected (protect)", action: () => this.pinSelected(false) },
            { label: "Unpin selected", action: () => this.pinSelected(true) },
            { label: "Diff selected (pick 2)", action: () => this.diffSelected() },
            { label: "Copy snapshot ID", action: (_e, row) => this.copyText(row.getData().id) },
          ],
        });
        if (stbl) stbl.on("rowClick", (e, row) => this._rangeClick(stbl, e, row));  // Shift+Click range select
      }, "Loading snapshots...");
    },

    snapSelected() {
      const el = document.querySelector("#snapshotsTable");
      return (el && el._tabulator) ? el._tabulator.getSelectedData() : [];
    },
    selectUnretained() {  // select snapshots with NO retention reason AND no pin = the safe-to-delete set
      const el = document.querySelector("#snapshotsTable");
      if (!el || !el._tabulator) return;
      let n = 0;
      el._tabulator.getRows().forEach((r) => {
        const d = r.getData();
        const keep = (d.retention && d.retention.length) || (d.pins && d.pins.length);
        if (keep) r.deselect(); else { r.select(); n++; }
      });
      this.actionText = `Selected ${n} unretained snapshot(s) (no retention, no pin). Use 'Delete selected…' to preview/copy the kopia command or delete.`;
    },
    selectIdenticalDuplicates() {  // HYBRID: group the source's snapshots by `root` (byte-identical
      const el = document.querySelector("#snapshotsTable");                       // content = same object),
      if (!el || !el._tabulator) return;                                          // keep OLDEST + NEWEST of
      const tbl = el._tabulator;                                                  // each group, select the
      const groups = {};                                                         // middle. Pinned = never
      for (const s of tbl.getData()) {                                           // selected; retention does
        if (s.root) (groups[s.root] = groups[s.root] || []).push(s);            // NOT block (the whole point
      }                                                                          // is to declutter redundant
      const del = new Set();                                                     // identicals retention keeps).
      for (const root in groups) {
        const g = groups[root];
        if (g.length <= 2) continue;  // oldest+newest already = the whole group; nothing to remove
        g.sort((a, b) => String(a.start_time).localeCompare(String(b.start_time)));
        const keep = new Set([g[0].id, g[g.length - 1].id]);  // oldest + newest endpoints
        for (const s of g) {
          if (keep.has(s.id) || (s.pins && s.pins.length)) continue;  // keep endpoints + pinned
          del.add(s.id);
        }
      }
      let n = 0;
      tbl.getRows().forEach((r) => { if (del.has(r.getData().id)) { r.select(); n++; } else r.deselect(); });
      this.actionText = n
        ? `Selected ${n} identical-duplicate snapshot(s) (kept oldest+newest per content; pinned kept; retained ARE included). Use 'Delete selected…'.`
        : `No identical duplicates to collapse — no content has 3+ snapshots (or 'root' is missing on the rows).`;
    },
    changeSnapSource() {  // persist scope; Tree mirrors it (incl. *ALL*) so the two views stay in parity
      this.klSet({ snap_source: this.snapSource });
      this.selectedSource = this.snapSource;
      this.loadSnapshots();
    },
    planDelete() {  // build a confirm plan from the currently-selected snapshot rows
      const rows = this.snapSelected();
      if (!rows.length) { window.alert("Select one or more snapshots first (click rows / checkboxes)."); return; }
      const reclaimVals = rows.map((r) => r.reclaimable).filter((v) => v !== null && v !== undefined);
      this.deletePlan = {
        ids: rows.map((r) => r.id),
        count: rows.length,
        reclaim: reclaimVals.reduce((a, b) => a + b, 0),
        reclaimKnown: reclaimVals.length === rows.length,
        pinned: rows.filter((r) => r.pins && r.pins.length).length,
        retained: rows.filter((r) => r.retention && r.retention.length).length,
        commands: "",
      };
      this.deleteConfirm = "";
      this.deletePlan.preview = [];
      this.deletePlan.combinedReclaim = null;     // accurate union(deleted)-union(kept), filled async
      this.deletePlan.combinedExact = true;
      this.deletePlan.combinedComputing = true;
      this.showDelete = true;
      // fetch the exact commands (handles repo2's --config-file) for the copy button
      getJson("/api/actions/snapshot-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_ids: this.deletePlan.ids, confirm: false }) })
        .then((r) => { if (this.deletePlan) this.deletePlan.commands = r.commands || ""; }).catch(() => {});
      // accurate combined reclaim for the WHOLE set (not the per-snapshot sum, which under-counts)
      getJson("/api/snapshots/set-reclaim", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_ids: this.deletePlan.ids }) })
        .then((r) => { if (this.deletePlan) { this.deletePlan.combinedReclaim = r.reclaim; this.deletePlan.combinedExact = r.exact; this.deletePlan.combinedComputing = false; } })
        .catch(() => { if (this.deletePlan) this.deletePlan.combinedComputing = false; });
      // fetch the largest files that would be reclaimed (preview)
      this.loadDeletePreview(false);
    },
    async loadDeletePreview(precise) {
      if (!this.deletePlan) return;
      const ids = this.deletePlan.ids.join(",");
      const r = await getJson(`/api/snapshots/delete-preview?repo=${encodeURIComponent(this.repo)}&ids=${encodeURIComponent(ids)}&precise=${precise ? 1 : 0}&limit=40`).catch(() => null);
      if (r && this.deletePlan) { this.deletePlan.preview = r.files || []; this.deletePlan.precise = precise; }
    },
    async executeDelete() {
      if (!this.deletePlan) return;
      if (this.deleteConfirm.trim().toUpperCase() !== "DELETE") { window.alert('Type DELETE to confirm.'); return; }
      await this.wrap(async () => {
        const r = await getJson("/api/actions/snapshot-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_ids: this.deletePlan.ids, confirm: true }) });
        this.showDelete = false; this.deletePlan = null; this.deleteConfirm = "";
        this.actionText = `Deleted ${r.deleted} snapshot(s). Run 'kopia maintenance run --full' to reclaim the space.`;
        await this.loadSnapshots(true);
        this.loadDashboard(true);
      }, "Deleting snapshots…");
    },

    async analyzeAll() {  // churn + reclaimable for the whole repo, cached
      await this.wrap(async () => {
        await getJson(`/api/snapshots/analyze-all?repo=${encodeURIComponent(this.repo)}&reclaim=true`, { method: "POST" });
        this.analyzingAll = true;
        const poll = setInterval(async () => {
          const d = await getJson(`/api/snapshots/analyze-status?repo=${encodeURIComponent(this.repo)}`).catch(() => null);
          if (d && !d.analyzing_all) { clearInterval(poll); this.analyzingAll = false; if (this.active === "snapshots") this.loadSnapshots(); }
        }, 5000);
      }, "Analyzing entire repo (churn + reclaimable)…");
    },

    async analyzeSnapshots() {
      if (!this.snapSource) this.snapSource = "*ALL*";
      if (this.snapSource === "*ALL*") return this.analyzeAll();  // all-sources view -> repo-wide
      await this.wrap(async () => {
        await getJson(`/api/snapshots/analyze?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.snapSource)}&reclaim=true`, { method: "POST" });
        this.analyzing = true;
        const poll = setInterval(async () => {
          const d = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.snapSource)}`).catch(() => null);
          if (d && !d.analyzing) { clearInterval(poll); this.loadSnapshots(); }
        }, 4000);
      }, "Analyzing snapshots (churn + reclaimable)...");
    },

    // ---------- Tree ----------
    async _treeSnapshots() {  // snapshots for the TREE's own source (selectedSource), NOT the snapshots-view snapSource scope
      const data = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`).catch(() => null);
      this.snapshots = (data && data.snapshots) || [];
    },
    openTree() {
      if (!this.selectedSource) return;
      // *ALL* (repo-wide) and 'all' mode don't need a single snapshot - go straight to loadTree.
      if (this.selectedSource === "*ALL*" || this.treeMode === "all") { this.treePrefix = this.sourceRoot(); return this.loadTree(); }
      // Ensure this.snapshots belong to THIS source - the snapshots view loads snapSource (e.g. *ALL*),
      // which would leave a selectedSnapshot that isn't in this source -> "snapshot root not found".
      const haveForSource = this.snapshots.length && this.snapshots[0].source === this.selectedSource;
      const ensure = (haveForSource && this.snapshots.find((s) => s.id === this.selectedSnapshot)) ? Promise.resolve() : this._treeSnapshots();
      return Promise.resolve(ensure).then(() => {
        if ((!this.selectedSnapshot || !this.snapshots.find((s) => s.id === this.selectedSnapshot)) && this.snapshots.length) this.selectedSnapshot = this.snapshots[this.snapshots.length - 1].id;
        if (!this.treePrefix || !this.treePrefix.startsWith(this.sourceRoot().replace(/[\\/]+$/, ""))) this.treePrefix = this.sourceRoot();
        return this.loadTree();
      });
    },
    treeSourceChanged() { this.snapshots = []; this.selectedSnapshot = ""; if (this.selectedSource === "*ALL*") this.snapSource = "*ALL*"; this.treePrefix = this.sourceRoot(); this.openTree(); },
    treeModeChanged() { this.treePrefix = this.sourceRoot(); this.klSet({ tree_mode: this.treeMode }); this.openTree(); },
    treeWeekChanged() { this.treePrefix = this.sourceRoot(); this.loadTree(); },
    // week picker: the last 12 calendar weeks (Sunday-ended), newest first. Value = the
    // week's last day, which is what /api/tree?week= expects.
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
    navTree(prefix) { this.treePrefix = prefix; this.loadTree(); },
    treeUp() { const c = this.treeCrumbs(); if (c.length > 1) this.navTree(c[c.length - 2].prefix); },

    async loadTree() {
      if (!this.selectedSource) return;
      if (this.selectedSource !== "*ALL*" && this.treeMode !== "all" && !this.selectedSnapshot) return;
      const seq = ++this._treeSeq;
      await this.wrap(async () => {
        const url = `/api/tree?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`
          + `&snapshot_id=${encodeURIComponent(this.selectedSnapshot)}&prefix=${encodeURIComponent(this.treePrefix)}&mode=${this.treeMode}`
          + `&dedupe=${this.treeDedupe ? 1 : 0}`
          + `&deleted_only=${this.treeDeletedOnly ? 1 : 0}&over_gb=${this.treeOverGb ? 1 : 0}`
          + `&flat=${this.treeFlat ? 1 : 0}`
          + (this.selectedSource === "*ALL*" && this.treeWeek ? `&week=${encodeURIComponent(this.treeWeek)}` : "")
          + (this.treeExt ? `&ext=${encodeURIComponent(this.treeExt)}` : "");
        const data = await getJson(url);
        if (seq !== this._treeSeq) return;  // a newer navigation superseded this one
        const children = data.children || [];
        if (!children.length && !(this.indexStatus.indexed_snapshots > 0)) this.error = "Snapshot not indexed yet - click Index (top right).";
        const total = children.reduce((a, c) => a + (c.size || 0), 0) || 1;
        children.forEach((c) => { c.pct = 100 * (c.size || 0) / total; });
        const ttbl = setTable("#treeTable", children, [
          { title: "Name", field: "name", widthGrow: 4, minWidth: 260, formatter: (c) => `${c.getData().type === "folder" ? "📁 " : "📄 "}${c.getValue()}` },
          { title: "Size", field: "size", width: 105, sorter: "number", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Physical", field: "physical", width: 100, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "% of folder", field: "pct", width: 90, formatter: (c) => `${Number(c.getValue() || 0).toFixed(1)}%`, hozAlign: "right" },
          { title: "Files", field: "files", width: 85, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Modified", field: "modified", width: 165, formatter: (c) => timeCell(c.getValue()) },
          { title: "Type", field: "type", width: 75 },
        ], {
          selectableRows: true,
          initialSort: [{ column: "size", dir: "desc" }],
          onRowDblClick: (_e, row) => { const d = row.getData(); if (d.type === "folder") this.navTree(d.path); else this.openFileDetail({ source: this.selectedSource, full_path: d.path }); },
          rowMenu: [
            { label: "Open / drill in", action: (_e, row) => { const d = row.getData(); if (d.type === "folder") this.navTree(d.path); } },
            { label: "Details (physical / dedup)", action: (_e, row) => this.openRowDetail(row.getData()) },
            { label: "Version history", action: (_e, row) => this.openFileDetail({ source: this.selectedSource, full_path: row.getData().path }) },
            { label: "Restore…", action: (_e, row) => { const d = row.getData(); this.openRestore(d.content_id || d.path, d.name); } },
            { label: "Mount + open in Explorer", action: (_e, row) => this.mountOpen(this.selectedSnapshot, this.selectedSource, row.getData().path) },
            { label: "Find in search", action: (_e, row) => { this.query = row.getData().name; this.searchSource = ""; this.active = "search"; this.loadSearch(); } },
            { label: "Delete (chunked, respects filters)…", action: (_e, row) => this.openRemoveFolder(row.getData()) },
            { label: "Copy path", action: (_e, row) => this.copyText(row.getData().path) },
          ],
        });
        if (ttbl) ttbl.on("rowClick", (e, row) => this._rangeClick(ttbl, e, row));  // Shift+Click range select
      }, "Loading tree...");
    },
    treeFilterChanged() { this.loadTree(); },
    toggleTreeFlat() { this.treeFlat = !this.treeFlat; this.loadTree(); },
    removeSelectedTree() {  // Remove button: route a selected folder to the chunked plan; files -> batch panel
      const rows = this.selectedRows("tree");
      const folders = rows.filter((r) => r.type === "folder");
      if (folders.length === 1 && rows.length === 1) return this.openRemoveFolder(folders[0]);
      if (folders.length) { window.alert("Select a single folder (or right-click it → Delete), or select files. Tip: use ☰ Flat to list files and multi-select."); return; }
      return this.batchDetail("tree");
    },

    openRowDetail(d) {
      if (d.type === "folder") {
        this.detail = { kind: "folder", title: d.path, logical: d.size, physical: d.physical, files: d.files };
      } else {
        this.openFileDetail({ source: this.selectedSource, full_path: d.path });
      }
    },

    // ---------- Search (server-side count + sort + pagination) ----------
    searchParams() {
      const p = new URLSearchParams({ repo: this.repo, q: this.query, deleted_only: this.deletedOnly, over_gb: this.overOneGb ? 1 : 0, sort: this.searchSort, dir: this.searchDir });
      if (this.searchSource) p.set("source", this.searchSource);
      if (this.searchExt) p.set("ext", this.searchExt);
      if (this.searchExtExclude) p.set("ext_exclude", this.searchExtExclude);
      if (this.minGb !== "" && this.minGb != null) p.set("min_gb", this.minGb);
      if (this.maxGb !== "" && this.maxGb != null) p.set("max_gb", this.maxGb);
      if (this.dateFrom) p.set("date_from", this.dateFrom);
      if (this.dateTo) p.set("date_to", this.dateTo);
      if (this.minVersions !== "" && this.minVersions != null) p.set("min_versions", this.minVersions);
      if (this.sharedMin !== "" && this.sharedMin != null) p.set("shared_min", this.sharedMin);
      const ff = this.recentBackupCutoff();
      if (ff) p.set("first_from", ff);
      if (this.dedupe) p.set("dedupe", "1");
      return p;
    },
    recentBackupCutoff() {  // dropdown days -> YYYY-MM-DD cutoff for first_seen ("recently backed up")
      const days = parseInt(this.recentBackup, 10);
      if (!days) return "";
      return new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
    },
    changeRecentBackup() { this.searchPage = 1; this.klSet({ recent_backup: this.recentBackup }); this.loadSearch(); },
    searchPages() { return Math.max(1, Math.ceil(this.searchTotal / (parseInt(this.searchPageSize, 10) || 500))); },
    setSearchPage(n) { this.searchPage = Math.min(Math.max(1, n), this.searchPages()); this.loadSearch(true); },
    setSearchSort(field, dir) { this.searchSort = field; this.searchDir = dir; this.searchPage = 1; this.klSet({ search_sort: field, search_dir: dir }); this.loadSearch(); },
    changePageSize() {
      this.klSet({ page_size: this.searchPageSize });  // persist the raw value, including "Infinite"
      this.searchPage = 1; this.loadSearch();
    },
    clearFilters() {
      this.minGb = ""; this.maxGb = ""; this.dateFrom = ""; this.dateTo = ""; this.minVersions = ""; this.sharedMin = "";
      this.searchExt = ""; this.searchExtExclude = ""; this.searchSource = ""; this.deletedOnly = false; this.overOneGb = false;
      this.recentBackup = ""; this.klSet({ recent_backup: "" });
      this.loadSearch();
    },

    // ---- Churn: what added NEW backed-up data in a window (by source / folder / file type) ----
    // One backend computation feeds all three views, so the numbers can never disagree.
    async loadChurn(refresh = false) {
      const seq = ++this._churnSeq;
      await this.wrap(async () => {
        const p = new URLSearchParams({ repo: this.repo, days: this.churnDays, depth: this.churnDepth, limit: "400" });
        if (refresh) p.set("refresh", "1");
        const d = await getJson(`/api/churn?${p.toString()}`);
        if (seq !== this._churnSeq) return;   // a faster later request already won
        this.churnData = d;
        this.churnMeta = `${fmtPhys(d.total_bytes)} attributed · ${fmtPhys(d.total_bytes_deduped)} after dedup · `
          + `${Number(d.total_files || 0).toLocaleString()} files · ${Number(d.total_versions || 0).toLocaleString()} versions · `
          + (d.cached ? "cached" : `computed in ${Number(d.elapsed || 0).toFixed(1)}s`);
        this.renderChurn();
      }, "Computing churn…");
    },
    changeChurn() { this.klSet({ churn_days: this.churnDays, churn_depth: this.churnDepth, churn_view: this.churnView }); this.loadChurn(); },
    setChurnView(v) { this.churnView = v; this.klSet({ churn_view: v }); this.renderChurn(); },

    renderChurn() {
      const d = this.churnData;
      if (!d) return;
      const bytesCol = { title: "New data", field: "bytes", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => fmtPhys(c.getValue()) };
      const verCol = { title: "Versions", field: "versions", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(), headerTooltip: "How many file-versions were written. A big number against few files means the same files are being rewritten." };
      const fileCol = { title: "Files", field: "files", width: 90, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() };
      let rows = [], cols = [], menu = [];
      if (this.churnView === "source") {
        rows = d.by_source || [];
        cols = [
          { title: "Source", field: "source", widthGrow: 3, minWidth: 220, formatter: (c) => {
              const r = c.getRow().getData();
              const warn = r.nested_in ? ` <span title="Also covered by ${sourcePath(r.nested_in)}, so this data is snapshotted twice and counted in both rows" style="color:#d97706">⚠ inside ${sourcePath(r.nested_in)}</span>` : "";
              return sourcePath(c.getValue()) + warn;
            } },
          bytesCol, verCol, fileCol,
          { title: "Mostly from", field: "top_folder", widthGrow: 2, minWidth: 180, headerSort: false,
            formatter: (c) => { const r = c.getRow().getData(); return r.top_folder ? `${r.top_folder} <span style="opacity:.6">(${fmtPhys(r.top_folder_bytes)})</span>` : ""; } },
        ];
        menu = [{ label: "Folder details for the top folder", action: (_e, row) => { const r = row.getData(); if (r.top_folder) this.openFolderPanel(r.top_folder); } }];
      } else if (this.churnView === "folder") {
        rows = d.by_folder || [];
        cols = [
          { title: "Folder", field: "folder", widthGrow: 4, minWidth: 280 },
          bytesCol, verCol, fileCol,
          { title: "Sources", field: "source_count", width: 90, hozAlign: "right", sorter: "number",
            headerTooltip: "How many backup sources contributed churn in this folder. Several usually means sub-folders with their own policies, not duplication - open the folder to see which actually cover it.",
            formatter: (c) => { const r = c.getRow().getData(); const n = r.source_count || 0;
              return n > 1 ? `<span title="${(r.sources || []).map(sourcePath).join(", ")}" >${n}</span>` : String(n); } },
        ];
        menu = [
          { label: "Folder details", action: (_e, row) => this.openFolderPanel(row.getData().folder) },
          { label: "Delete folder (chunked)…", action: (_e, row) => this.openRemoveFolderPath(row.getData().folder) },
        ];
      } else {
        rows = (d.by_ext || []).map((r) => ({ ...r, ext: r.ext || "(no extension)" }));
        cols = [{ title: "File type", field: "ext", widthGrow: 2, minWidth: 160 }, bytesCol, verCol, fileCol];
      }
      setTable("#churnTable", rows, cols, {
        rowMenu: menu.length ? menu : undefined,
        onRowDblClick: (_e, row) => {
          const r = row.getData();
          if (this.churnView === "folder") this.openFolderPanel(r.folder);
          else if (this.churnView === "source") { this.active = "search"; this.searchSource = r.source; this.query = ""; this.loadSearch(); }
          else if (r.ext && r.ext !== "(no extension)") { this.active = "search"; this.searchExt = r.ext; this.loadSearch(); }
        },
      });
    },

    // ---- Folder detail panel (reuses the file detail modal shell; folder-specific fields) ----
    // Renders instantly from indexed lookups, then patches in the expensive freeable/shared numbers
    // and the effective kopia policy - the same lazy pattern the file panel uses.
    async openFolderPanel(prefix, source = null) {
      if (!prefix) return;
      const seq = ++this._detailSeq;
      this.detail = { kind: "folder", title: prefix, source, loading: true, heavyLoading: true, sources: [], children: [] };
      try {
        const p = new URLSearchParams({ repo: this.repo, prefix });
        if (source) p.set("source", source);
        const sum = await getJson(`/api/folder/summary?${p.toString()}`);
        if (seq !== this._detailSeq) return;
        Object.assign(this.detail, sum, { loading: false });
      } catch (e) {
        if (seq === this._detailSeq) { this.detail.loading = false; this.detail.error = e.message; }
        return;
      }
      // Heavy pass: the chunk walk behind freeable / shared.
      // Do NOT blind-assign over the summary: `physical` means two different things. The summary's
      // is the SUM of each path's latest version (shared storage counted once per file); the
      // detail's is the DEDUPED chunk union across all versions. Overwriting made the headline
      // number silently drop - by 57% on a duplicate-heavy folder - with no explanation.
      getJson("/api/folder/detail", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: this.repo, prefix, source }) })
        .then((r) => {
          if (seq !== this._detailSeq || !this.detail) return;
          this.detail.physical_sum = this.detail.physical;   // keep the fast, additive figure
          Object.assign(this.detail, {
            physical_dedup: r.physical, freeable: r.freeable, shared: r.shared,
            objects: r.objects, all_versions: r.all_versions, heavyLoading: false,
            reclaim_stale: !!r.stale, reclaim_at: r.computed_at,
          });
        })
        .catch(() => { if (seq === this._detailSeq && this.detail) this.detail.heavyLoading = false; });
      // Effective policy for this exact folder (kopia resolves inheritance), via its main source.
      const main = (this.detail.sources || [])[0];
      if (main) {
        getJson(`/api/folder/policy?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(main.source)}&prefix=${encodeURIComponent(prefix)}`)
          .then((r) => { if (seq === this._detailSeq && this.detail) this.detail.effective_policy = r.policy; })
          .catch(() => {});
      }
    },
    folderDeleteFromPanel() {
      if (!this.detail || this.detail.kind !== "folder") return;
      const prefix = this.detail.title;
      const only = this.detail.source || null;   // panel opened scoped to one source keeps that scope
      this.detail = null;
      this.openRemoveFolderPath(prefix, only);
    },
    folderShowFiles() {
      if (!this.detail || this.detail.kind !== "folder") return;
      const prefix = this.detail.title;
      this.detail = null;
      this.groupByFolder = false; this.active = "search"; this.query = prefix; this.searchPage = 1;
      this.loadSearch();
    },

    // ---- Bloat finder: group backed-up paths by folder, ranked by physical (own #groupTable) ----
    toggleGroupByFolder() { this.groupByFolder = !this.groupByFolder; this.searchPage = 1; this.loadSearch(); },
    changeGroupDepth() { this.klSet({ group_depth: Number(this.groupDepth) || 3 }); this.loadSearch(); },
    drillFolder(row) { this.groupByFolder = false; this.query = row.folder; this.searchPage = 1; this.loadSearch(); },
    selectedGroupFolder() {  // the By-folder table is a SEPARATE Tabulator instance from #searchTable
      const el = document.querySelector("#groupTable");
      const rows = (el && el._tabulator) ? el._tabulator.getSelectedData() : [];
      return rows.length ? rows[0] : null;
    },
    async loadGroupByFolder() {
      await this.wrap(async () => {
        const p = new URLSearchParams({ repo: this.repo, depth: this.groupDepth, q: this.query || "", sort: "physical", direction: "desc", limit: 500 });
        if (this.searchSource) p.set("source", this.searchSource);
        if (this.searchExt) p.set("ext", this.searchExt);
        if (this.searchExtExclude) p.set("ext_exclude", this.searchExtExclude);
        if (this.deletedOnly) p.set("deleted_only", "1");
        if (this.groupMinGb) p.set("min_gb", this.groupMinGb);
        if (this.groupMinFiles) p.set("min_files", this.groupMinFiles);
        const data = await getJson(`/api/search/group-by-folder?${p.toString()}`);
        const rows = data.rows || [];
        this.omniMeta = `${rows.length} folders · depth ${data.depth} · ranked by physical`;
        const cols = [
          { title: "Folder", field: "folder", widthGrow: 5, minWidth: 300 },
          { title: "Physical", field: "phys", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => fmtPhys(c.getValue()) },
          { title: "Logical", field: "logical", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => fmtBytes(c.getValue()) },
          // folder exclusive share = SUM(exclusive)/SUM(physical); sorts by the exclusive BYTES.
          { title: "Excl", field: "excl", width: 90, hozAlign: "right", sorter: "number",
            formatter: (c) => { const d = c.getRow().getData(); const e = Number(d.excl || 0), p = Number(d.phys || 0);
              return p ? `<span title="${fmtPhys(e)} of ${fmtPhys(p)} held only by this folder's sources">${Math.round((e / p) * 100)}%</span>` : ""; } },
          { title: "Files", field: "files", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Deleted #", field: "deleted_files", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Deleted %", field: "deleted_pct", width: 90, headerSort: false, hozAlign: "right", formatter: (c) => { const d = c.getRow().getData(); return d.files ? Math.round((d.deleted_files / d.files) * 100) + "%" : ""; } },
          { title: "Avg size", field: "avg_size", width: 95, headerSort: false, hozAlign: "right", formatter: (c) => { const d = c.getRow().getData(); return d.files ? fmtBytes(Math.round(d.phys / d.files)) : ""; } },
          // per-folder freeable, computed on demand (right-click) - a chunk walk per folder.
          { title: "∑ Reclaim", field: "reclaim_val", headerSort: false, width: 105, hozAlign: "right",
            formatter: (c) => {
              const v = this.groupReclaim[c.getRow().getData().folder];
              if (v === "pending") return "<span style='color:#94a3b8'>…</span>";
              if (v === "error") return "<span style='color:#dc2626'>err</span>";
              if (v == null) return "<span style='color:#94a3b8' title='Right-click > Compute reclaim'>—</span>";
              return `<span style="color:#059669" title="freeable if this folder's matched content were removed (all versions, all sources)">${fmtPhys(v)}</span>`;
            } },
          { title: "Last seen", field: "last_seen", width: 155, formatter: (c) => String(c.getValue() || "").replace("T", " ").slice(0, 19) },
        ];
        setTable("#groupTable", rows, cols, {
          onRowDblClick: (_e, row) => this.openFolderPanel(row.getData().folder),
          rowMenu: [
            { label: "Folder details", action: (_e, row) => this.openFolderPanel(row.getData().folder) },
            { label: "Compute reclaim for this folder", action: (_e, row) => this.computeGroupReclaim(row) },
            { label: "Drill into files (then batch-remove)", action: (_e, row) => this.drillFolder(row.getData()) },
            { label: "Delete folder (chunked)…", action: (_e, row) => this.openRemoveFolderPath(row.getData().folder) },
          ],
        });
      }, "Grouping folders…");
    },
    // freeable for one folder row of the By-folder view - the same on-demand walk the
    // remove-detail button uses, scoped to the folder prefix. Heavy, so it only runs when asked.
    async computeGroupReclaim(row) {
      const d = row.getData();
      const cur = this.groupReclaim[d.folder];
      if (cur && cur !== "error") return;
      this.groupReclaim[d.folder] = "pending";
      row.reformat();
      try {
        const r = await getJson("/api/actions/remove-detail", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, prefix: d.folder, all_versions: true }) });
        this.groupReclaim[d.folder] = (r.found === false) ? "none" : (r.freeable ?? 0);
      } catch (e) {
        this.groupReclaim[d.folder] = "error";
      }
      row.reformat();
    },
    async loadSearch(keepPage = false) {
      if (this.groupByFolder) return this.loadGroupByFolder();
      const seq = ++this._searchSeq;
      if (!keepPage) this.searchPage = 1;
      await this.wrap(async () => {
        const infinite = this.searchPageSize === "Infinite";
        const size = infinite ? 100000 : (parseInt(this.searchPageSize, 10) || 500);
        const offset = infinite ? 0 : (this.searchPage - 1) * size;
        const p = this.searchParams(); p.set("limit", size); p.set("offset", offset);
        const data = await getJson(`/api/search?${p.toString()}`);
        if (seq !== this._searchSeq) return;
        this.searchTotal = data.total || 0;
        const shownFrom = this.searchTotal ? offset + 1 : 0;
        const shownTo = Math.min(offset + (data.results || []).length, this.searchTotal);
        this.omniMeta = `${this.searchTotal.toLocaleString()} results` + (infinite ? "" : ` · showing ${shownFrom.toLocaleString()}-${shownTo.toLocaleString()}`);
        // Headers do NOT sort locally (headerSort:false) - a click re-queries the server so the
        // sort is always over the ENTIRE result set, never just the visible page. Active column
        // shows a ▼/▲ arrow.
        const self = this;
        const arrow = (f) => (this.searchSort === f ? (this.searchDir === "desc" ? "  ▼" : "  ▲") : "");
        const H = (title, field, extra) => Object.assign({
          title: title + arrow(field), field, headerSort: false,
          headerClick: () => self.setSearchSort(field, (self.searchSort === field && self.searchDir === "desc") ? "asc" : "desc"),
        }, extra || {});
        const cols = [
          H("Path", "full_path", { widthGrow: 5, minWidth: 320 }),
          H("Size", "size", { width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" }),
          H("Physical (latest)", "physical", { width: 120, formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" }),
          // "Excl" = share of the packed bytes held ONLY by this source (exclusive/physical);
          // amber when mostly shared - deleting such a copy frees (almost) nothing. "—" until the
          // next dashboard pass fills paths.exclusive after an upgrade.
          { title: "Excl", field: "exclusive", headerSort: false, width: 85, hozAlign: "right",
            formatter: (c) => {
              const d = c.getRow().getData();
              if (d.exclusive == null || !Number(d.physical)) return "<span style='color:#94a3b8'>—</span>";
              const pct = Math.round((Number(d.exclusive) / Number(d.physical)) * 100);
              return `<span ${pct < 50 ? 'style="color:#d97706"' : ""} title="${fmtPhys(d.exclusive)} held only by this source">${pct}%</span>`;
            } },
          { title: "Src", field: "sources", headerSort: false, width: 62, hozAlign: "right",
            formatter: (c) => { const v = Number(c.getValue() || 1); return v > 1 ? `<span style="color:#d97706" title="content is held by ${v} sources (within-source copies excluded)">${v}</span>` : "1"; } },
          { title: "Reclaim", field: "reclaim", headerSort: false, width: 110, hozAlign: "right",
            formatter: (c) => { const v = c.getValue(); return (v == null) ? "<span style='color:#94a3b8'>—</span>" : fmtBytes(v); } },
          H("Ext", "ext", { width: 70 }),
          H("Versions", "versions", { width: 80, hozAlign: "right" }),
          { title: "Copies", field: "copies", headerSort: false, width: 75, hozAlign: "right",
            formatter: (c) => { const v = Number(c.getValue() || 1); return v > 1 ? `<span style="color:#d97706" title="this exact content is at ${v} paths (filter: Shared ≥ N)">${v}</span>` : "1"; } },
          H("Last seen", "last_seen", { width: 160, formatter: (c) => timeCell(c.getValue()) }),
          H("First seen", "first_seen", { width: 160, formatter: (c) => timeCell(c.getValue()) }),
          H("On disk", "in_latest", { width: 90, formatter: (c) => c.getValue() ? "yes" : "<span style='color:#dc2626'>deleted</span>" }),
          H("Source", "source", { widthGrow: 2, minWidth: 180, formatter: (c) => sourcePath(c.getValue()) }),
        ];
        // Reuse the existing Tabulator (replaceData + header-title update) rather than
        // destroy+recreate, so re-querying on sort/filter no longer flickers the whole table.
        const sEl = document.querySelector("#searchTable");
        if (sEl && sEl._tabulator) {
          const t = sEl._tabulator;
          // pass the full def (keeps headerClick + formatter) so only the rows + sort arrow change
          cols.forEach((c) => { try { t.updateColumnDefinition(c.field, c); } catch (e) {} });
          t.replaceData(data.results || []);
        } else {
          const sStbl = setTable("#searchTable", data.results || [], cols, {
            persistSort: false,
            selectableRows: true,
            onRowDblClick: (_e, row) => this.openFileDetail(row.getData()),
            rowMenu: [
              { label: "File detail (physical / reclaim / dedup)", action: (_e, row) => this.openFileDetail(row.getData()) },
              { label: "Version history", action: (_e, row) => this.openFileDetail(row.getData()) },
              { label: "Mount + open in Explorer (latest)", action: (_e, row) => this.mountFileOpen(row.getData()) },
              { label: "Restore…", action: (_e, row) => { const d = row.getData(); this.openRestore(d.content_id || d.full_path, d.name || d.full_path); } },
              { label: "Jump to it in tree", action: (_e, row) => this.jumpToTree(row.getData()) },
              { label: "Browse this source", action: (_e, row) => { this.selectedSource = row.getData().source; this.snapSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); } },
              { label: "Delete ALL matching this filter (chunked)…", action: () => this.openRemoveFromSearch() },
              { label: "Copy full path", action: (_e, row) => this.copyText(row.getData().full_path) },
            ],
          });
          if (sStbl) sStbl.on("rowClick", (e, row) => this._rangeClick(sStbl, e, row));  // Shift+Click range select
        }
      }, "Searching...");
    },

    exportSearch() {
      const p = this.searchParams();
      window.open(`/api/search/export?${p.toString()}`, "_blank");
    },

    async mountLatestFor(item) {
      const v = await getJson(`/api/search/versions?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`).catch(() => null);
      const snap = v && v.versions && v.versions[0] && v.versions[0].snapshot_id;
      if (snap) this.mountRow(snap); else window.alert("No snapshot found for this file.");
    },
    jumpToTree(item) {
      this.selectedSource = item.source; this.treeMode = "all";
      const root = sourcePath(item.source).replace(/[\\/]+$/, "");
      const parent = item.full_path.includes("\\") ? item.full_path.slice(0, item.full_path.lastIndexOf("\\")) : root;
      this.treePrefix = parent || root; this.active = "tree"; this.loadTree();
    },

    // ---------- File / folder detail ----------
    async openFileDetail(item) {
      const seq = ++this._detailSeq;
      // Render the panel immediately from the row we already have; the heavy fields (refcount,
      // dedup chunks, purge plan) fill in when they arrive. A second click bumps _detailSeq so a
      // stale in-flight response is dropped (no race).
      this.detail = {
        kind: "file", loading: true, title: item.full_path, source: item.source,
        on_disk: item.in_latest, logical: item.size, physical: item.physical,
        freeable: null, shared: null, chunks: [], others: [], versions: [], purge: [],
      };
      const base = `repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`;
      try {
        const [detail, vres, purge] = await Promise.all([
          getJson(`/api/search/detail?${base}`),
          getJson(`/api/search/versions?${base}`),
          getJson(`/api/actions/purge-plan?${base}`),
        ]);
        if (seq !== this._detailSeq) return;  // a newer file was opened meanwhile - drop this result
        const items = purge.items || [];
        const purgeRows = (purge.commands || []).map((c, i) => {
          const it = items[i] || {};
          return { cmd: Array.isArray(c) ? c.join(" ") : c, tag: it.pinned ? "PINNED" : ((it.retention && it.retention.length) ? `retained: ${it.retention.join(",")}` : "") };
        });
        Object.assign(this.detail, {   // merge (not replace) so a command built meanwhile (removeFiles) survives
          kind: "file", loading: false,
          title: item.full_path,
          source: detail.source || item.source,
          on_disk: item.in_latest,
          content_id: detail.content_id,
          logical: detail.logical, physical: detail.physical, freeable: detail.freeable,
          shared: detail.shared, physical_all: detail.physical_all_versions, chunk_count: detail.chunk_count,
          chunks: detail.chunks || [],
          others: detail.other_paths || [],
          locations: detail.locations, instances: detail.instances,
          reclaim_everywhere: detail.reclaim_everywhere, purge_everywhere_count: detail.purge_everywhere_count,
          purgeEverywhereCmds: detail.purge_everywhere_cmds || "",
          versions: (vres.versions || []).map((x) => ({ time: humanTime(x.start_time), size: x.size, snapshot_id: x.snapshot_id, content_id: x.content_id })),
          purge: purgeRows,
          purgeCmds: purgeRows.map((r) => r.cmd).join("\n"),
        });
      } catch (err) {
        if (seq === this._detailSeq) this.error = err.message || String(err);
      }
    },

    // ---- Mount + open in Explorer (auto drive letter, registered in the mount manager) ----
    async mountFileOpen(item) {  // mount the latest snapshot carrying a file and open its folder
      const v = await getJson(`/api/search/versions?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`).catch(() => null);
      const snap = v && v.versions && v.versions[0] && v.versions[0].snapshot_id;
      if (!snap) { window.alert("No snapshot found for this file."); return; }
      this.mountOpen(snap, item.source, item.full_path);
    },
    mountOpen(snapshotId, source, fullPath) {
      this.wrap(async () => {
        const r = await getJson("/api/actions/mount-open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: snapshotId, source, full_path: fullPath || null }) });
        this.actionText = `Mounted at ${r.mountpoint} (pid ${r.pid}); opening Explorer…\n${(r.command || []).join(" ")}`;
        this.loadMounts();
      }, "Mounting + opening…");
    },
    mountVersionOpen(v) { if (this.detail) this.mountOpen(v.snapshot_id, this.detail.source, this.detail.title); },
    mountTreeSnapshot() {  // tree control-bar: mount the selected snapshot + open the current folder
      if (!this.selectedSnapshot) { window.alert("Pick a snapshot first."); return; }
      this.mountOpen(this.selectedSnapshot, this.selectedSource, this.treePrefix || this.sourceRoot());
    },

    // ---- Remove file from ALL snapshots (kopia snapshot fix remove-files) ----
    async loadRemoveFiles(by) {  // by = 'object-id' (precise) | 'filename' (basename + source)
      if (!this.detail) return;
      try {
        const r = await getJson("/api/actions/remove-files", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source: this.detail.source, full_path: this.detail.title, by, everywhere: !!this.detail.rfEverywhere, confirm: false }) });
        this.detail.removeFiles = { by, dry: r.dry_run, commit: r.commit, count: r.count, everywhere: r.everywhere };
      } catch (err) { this.error = err.message || String(err); }
    },
    async executeRemoveFiles() {
      const rf = this.detail && this.detail.removeFiles;
      if (!rf) return;
      if ((window.prompt("Remove this file from ALL snapshots (rewrites manifests, keeps the snapshots)?\nType REMOVE to confirm:") || "").trim().toUpperCase() !== "REMOVE") return;
      await this.wrap(async () => {
        await getJson("/api/actions/remove-files", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source: this.detail.source, full_path: this.detail.title, by: rf.by, everywhere: !!this.detail.rfEverywhere, confirm: true }) });
        this.actionText = "Removed from snapshots. Run 'kopia maintenance run --full' to reclaim the space.";
        this.detail = null;
      }, "Removing file from all snapshots…");
    },

    // ---- Exclusion-pattern suggestions (paste into the KopiaUI policy) ----
    excludePatterns() {
      if (!this.detail || this.detail.kind !== "file") return [];
      const full = this.detail.title || "";
      const root = sourcePath(this.detail.source || "").replace(/[\\/]+$/, "");
      const rel = full.startsWith(root) ? full.slice(root.length).replace(/^[\\/]+/, "") : full;
      const relFwd = rel.replace(/\\/g, "/");
      const name = full.split("\\").pop().split("/").pop();
      const parentRel = relFwd.includes("/") ? relFwd.slice(0, relFwd.lastIndexOf("/")) : "";
      const parentName = parentRel ? parentRel.split("/").pop() : "";
      const dot = name.lastIndexOf(".");
      const out = [
        { label: "This file (any depth)", pat: name },
        { label: "This file (anchored)", pat: "/" + relFwd },
      ];
      if (parentName) {
        out.push({ label: "Containing folder (any depth)", pat: parentName });
        out.push({ label: "Containing folder (anchored)", pat: "/" + parentRel + "/**" });
      }
      if (dot > 0) out.push({ label: "By extension", pat: "*" + name.slice(dot) });
      return out;
    },

    // ---- Dedupe + compute-reclaim (Search) ----
    toggleDedupe() { this.dedupe = !this.dedupe; this.searchPage = 1; this.klSet({ search_dedupe: this.dedupe }); this.loadSearch(); },
    toggleTreeDedupe() { this.treeDedupe = !this.treeDedupe; this.klSet({ tree_dedupe: this.treeDedupe }); this.loadTree(); },
    async computeReclaim() {
      const el = document.querySelector("#searchTable");
      if (!el || !el._tabulator) return;
      const rows = el._tabulator.getData();
      const top = [...rows].sort((a, b) => (b.physical || 0) - (a.physical || 0)).slice(0, 500);
      const cids = [...new Set(top.map((r) => r.content_id).filter(Boolean))];
      if (!cids.length) return;
      this.reclaimComputing = true;
      try {
        const r = await getJson("/api/search/reclaim", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, content_ids: cids, cap: 500 }) });
        const map = r.reclaim || {};
        rows.forEach((row) => { row.reclaim = map[row.content_id]; });
        rows.sort((a, b) => (b.reclaim || 0) - (a.reclaim || 0));  // most freeable first
        el._tabulator.replaceData(rows);
      } catch (err) { this.error = err.message || String(err); }
      finally { this.reclaimComputing = false; }
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
    async batchRemove(scope) {
      // In By-folder view the visible table is #groupTable, which _selFiles never looked at - so a
      // selected folder always reported "select one or more files first". Route it to the chunked
      // folder plan instead, which is what deleting a folder actually needs.
      if (scope === "search" && this.groupByFolder) {
        const row = this.selectedGroupFolder();
        if (!row) { window.alert("Select a folder first."); return; }
        return this.openRemoveFolderPath(row.folder);
      }
      const files = this._selFiles(scope);
      if (!files.length) { window.alert("Select one or more files first."); return; }
      const r = await getJson("/api/actions/remove-files", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, by: "filename", files, confirm: false }) }).catch((e) => { this.error = e.message; return null; });
      if (!r) return;
      const ans = window.prompt(`Remove ${files.length} file(s) from ALL snapshots (keeps the snapshots).\n\nDRY-RUN:\n${r.dry_run}\n\nType REMOVE to execute now, or Cancel to just copy the commit commands:`);
      if (ans === null) { this.copyText(r.commit); this.actionText = "Remove commands copied:\n" + r.commit; return; }
      if (ans.trim().toUpperCase() !== "REMOVE") return;
      await this.wrap(async () => {
        await getJson("/api/actions/remove-files", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, by: "filename", files, confirm: true }) });
        this.actionText = "Removed from snapshots. Run 'kopia maintenance run --full' to reclaim.";
        this.loadSearch();
      }, "Removing files…");
    },
    batchExclude(scope) {
      const names = [...new Set(this._selFiles(scope).map((f) => f.full_path.split("\\").pop().split("/").pop()))].filter(Boolean);
      if (!names.length) { window.alert("Select one or more files first."); return; }
      this.copyText(names.join("\n"));
      this.actionText = "Exclude patterns (bare names — paste into KopiaUI policy):\n" + names.join("\n");
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

    // ---- Batch detail panel (multi-file union: physical/freeable + multi-choice removal) ----
    async batchDetail(scope) {
      const files = this._selFiles(scope);
      if (!files.length) { window.alert("Select one or more files first (Shift+Click for a range)."); return; }
      const seq = ++this._batchSeq;
      this.batch = { loading: true, files, count: files.length, by: "object-id", everywhere: false, stats: null, cmd: null, cmdLoading: false };
      this.showBatch = true;
      const r = await getJson("/api/actions/batch-detail", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, files }) }).catch((e) => { this.error = e.message; return null; });
      if (seq !== this._batchSeq) return;  // a newer batch was opened meanwhile - drop this result
      if (this.batch) { this.batch.stats = r; this.batch.loading = false; }
      this.buildBatchCmd();
    },
    async buildBatchCmd() {
      if (!this.batch) return;
      const seq = ++this._batchCmdSeq;
      this.batch.cmd = null; this.batch.cmdLoading = true;
      const body = { repo: this.repo, by: this.batch.by, everywhere: this.batch.everywhere, files: this.batch.files, confirm: false };
      const r = await getJson("/api/actions/remove-files", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch((e) => { this.error = e.message; return null; });
      if (seq !== this._batchCmdSeq) return;  // a newer by/everywhere toggle superseded this build
      if (this.batch) { if (r) this.batch.cmd = { dry: r.dry_run, commit: r.commit, count: r.count, everywhere: r.everywhere, batches: r.batches, bat: r.bat, bat_name: r.bat_name }; this.batch.cmdLoading = false; }
    },
    batchSaveBat() {
      const c = this.batch && this.batch.cmd;
      if (!c || !c.bat) return;
      this._downloadText(c.bat_name || "kopialens_remove.bat", c.bat);
      this.actionText = `Downloaded ${c.bat_name} (${c.batches} batch(es)).`;
    },
    setBatchBy(by) { if (this.batch) { this.batch.by = by; this.buildBatchCmd(); } },
    toggleBatchEverywhere() { if (this.batch) { this.batch.everywhere = !this.batch.everywhere; this.buildBatchCmd(); } },
    async executeBatchRemove() {
      if (!this.batch) return;
      if (this.batch.cmd && this.batch.cmd.batches > 1) { window.alert(`Too many for one command (${this.batch.cmd.batches} batches). Use Copy or Save .bat to run it in chunks.`); return; }
      if ((window.prompt(`Remove ${this.batch.count} file(s) from snapshots (keeps the snapshots).\nType REMOVE to confirm:`) || "").trim().toUpperCase() !== "REMOVE") return;
      const b = this.batch;
      await this.wrap(async () => {
        await getJson("/api/actions/remove-files", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, by: b.by, everywhere: b.everywhere, files: b.files, confirm: true }) });
        this.actionText = "Removed from snapshots. Run 'kopia maintenance run --full' to reclaim.";
        this.showBatch = false; this.batch = null;
        this.loadSearch(true);
      }, "Removing files…");
    },

    // ---- Chunked folder/filter delete (remove-files, respects the current view/filter) ----
    openRemoveFromSearch() {  // delete EVERY file matching the current Search filter (not just selected)
      this._removeBase = {
        repo: this.repo, query: this.query, source: this.searchSource || null,
        ext: this.searchExt || null, ext_exclude: this.searchExtExclude || null,
        deleted_only: this.deletedOnly, over_gb: this.overOneGb ? 1 : 0,
        min_gb: this.minGb ? Number(this.minGb) : 0, max_gb: this.maxGb ? Number(this.maxGb) : 0,
        min_versions: this.minVersions ? Number(this.minVersions) : 0,
        shared_min: this.sharedMin ? Number(this.sharedMin) : 0,
        date_from: this.dateFrom || null, date_to: this.dateTo || null, prefix: null,
      };
      this._removeLabel = "current Search filter" + (this.query ? ` · “${this.query}”` : "");
      this.buildRemovePlan();
    },
    openRemoveFolder(row) {  // delete a Tree folder/file, honoring the active Tree filters
      this._removeBase = {
        repo: this.repo, source: this.selectedSource, prefix: row.path,
        deleted_only: this.treeDeletedOnly, over_gb: this.treeOverGb ? 1 : 0, ext: this.treeExt || null,
      };
      this._removeLabel = (row.type === "folder" ? "folder " : "") + (row.path || row.name);
      this.buildRemovePlan();
    },
    openRemoveFolderPath(prefix, source = null) {
      // Cross-source folder delete (By-folder view, Churn, the folder panel). No tree filters, and
      // no source scope unless one is given, so every source holding the folder is covered.
      this._removeBase = { repo: this.repo, source, prefix, deleted_only: false, over_gb: 0, ext: null };
      this._removeLabel = "folder " + prefix + (source ? " (in " + sourcePath(source) + ")" : " (all sources)");
      this.buildRemovePlan();
    },
    buildRemovePlan() {
      // Folder/file-context deletes (a Tree prefix) default to ALL versions: latest-only would
      // silently leave every older version's bytes in old snapshots ("deleted" folder still there).
      const allVers = !!(this._removeBase && this._removeBase.prefix);
      this.removePlan = { loading: true, label: this._removeLabel, all_versions: allVers, everywhere: false, by: "object-id", detail: null, detailLoading: false, job: null, batSaved: null, top_n: 0 };
      this.showRemove = true;
      this._fetchRemovePlan();
    },
    async _fetchRemovePlan() {
      if (!this.removePlan) return;
      const seq = ++this._removePlanSeq;
      this.removePlan.loading = true;
      this.removePlan.detail = null;  // freeable/shared depends on the matched set
      ++this._removeDetailSeq;
      this.removePlan.detailLoading = false;
      const r = await getJson("/api/actions/remove-plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this._removeBody()) }).catch((e) => { this.error = e.message; return null; });
      if (seq !== this._removePlanSeq || !this.removePlan) return;  // a newer toggle superseded this fetch
      if (r) Object.assign(this.removePlan, r);
      this.removePlan.loading = false;
      // Auto-compute freeable/shared (like the file panel) unless the set is huge (avoid a long walk).
      if (r && (r.file_count || 0) <= 200000) this.removeComputeReclaim();
    },
    toggleRemoveAllVersions() { if (this.removePlan && !this.removePlan.job) { this.removePlan.all_versions = !this.removePlan.all_versions; this._fetchRemovePlan(); } },
    toggleRemoveEverywhere() { if (this.removePlan && !this.removePlan.job) { this.removePlan.everywhere = !this.removePlan.everywhere; this._fetchRemovePlan(); } },
    setRemoveBy(by) { if (this.removePlan && !this.removePlan.job) { this.removePlan.by = by; this._fetchRemovePlan(); } },
    async removeComputeReclaim() {
      if (!this.removePlan) return;
      const seq = ++this._removeDetailSeq;
      this.removePlan.detailLoading = true;
      const r = await getJson("/api/actions/remove-detail", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this._removeBody()) }).catch((e) => { this.error = e.message; return null; });
      if (seq !== this._removeDetailSeq || !this.removePlan) return;  // a newer plan fetch superseded this
      this.removePlan.detail = r; this.removePlan.detailLoading = false;
    },
    _removeBody() {
      const body = { ...this._removeBase, all_versions: this.removePlan.all_versions,
                     everywhere: this.removePlan.everywhere, by: this.removePlan.by };
      if (this.removePlan.top_n) body.cap = this.removePlan.top_n;  // biggest-physical-first is already the order
      return body;
    },
    setRemoveTopN(n) {
      if (!this.removePlan || this.removePlan.job) return;
      this.removePlan.top_n = n || 0;
      this._fetchRemovePlan();
    },
    removeCoverage() {  // "these 100 files are 70% of the folder" - the number that makes top-N a decision
      const p = this.removePlan;
      if (!p || !p.total_physical) return null;
      return Math.round((p.physical / p.total_physical) * 100);
    },
    async removeSaveBat() {
      const r = await getJson("/api/actions/remove-bat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this._removeBody()) }).catch((e) => { this.error = e.message; return null; });
      if (!r || !this.removePlan) return;
      this._downloadText(r.filename, r.content);
      this.removePlan.batSaved = { filename: r.filename, batches: r.batches };
      this.actionText = `Downloaded ${r.filename} (${r.batches} batches). Run it; kopia output logs next to it.`;
    },
    async removeRun() {
      if (!this.removePlan) return;
      const msg = `Run the chunked delete NOW?\n${this.removePlan.object_count} content objects · ${this.removePlan.batches} batch(es)` +
        (this.removePlan.everywhere ? "\n\n⚠ EVERYWHERE: not source-scoped (whole-repo sweep)." : "") +
        "\n\nType DELETE to confirm:";
      if ((window.prompt(msg) || "").trim().toUpperCase() !== "DELETE") return;
      const r = await getJson("/api/actions/remove-execute", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this._removeBody()) }).catch((e) => { this.error = e.message; return null; });
      if (!r || !this.removePlan) return;
      this.removePlan.job = { job_id: r.job_id, total: this.removePlan.batches, done: 0, running: true, errors: 0, log: r.log };
      this._pollRemove(r.job_id);
    },
    _pollRemove(jobId) {
      if (this.removeJobPoll) clearInterval(this.removeJobPoll);
      this.removeJobPoll = setInterval(async () => {
        const s = await getJson(`/api/actions/remove-status?job_id=${encodeURIComponent(jobId)}`).catch(() => null);
        if (s && this.removePlan) this.removePlan.job = { ...this.removePlan.job, ...s };
        if (s && !s.running) {
          clearInterval(this.removeJobPoll); this.removeJobPoll = null;
          this.actionText = `Remove finished: ${s.done}/${s.total} batches, ${s.errors || 0} error(s).\nRun 'kopia maintenance run --full' to reclaim the space.\nLog: ${s.log}`;
        }
      }, 1500);
    },
    closeRemove() { if (this.removeJobPoll) { clearInterval(this.removeJobPoll); this.removeJobPoll = null; } this.showRemove = false; this.removePlan = null; },

    // ---- Excluded browser (local-disk coverage gaps vs both repos; es.exe disk side) ----
    async loadExcluded() {
      if (!this.excludedDrives.length) {
        const d = await getJson("/api/excluded/drives").catch(() => null);
        if (d) { this.excludedDrives = d.drives || []; this.excludedOk = !!d.everything_ok; }
      }
      if (this.excludedPath) return this.listExcluded(this.excludedPath);
      this.renderExcludedDrives();
    },
    renderExcludedDrives() {
      this.excludedPath = "";
      this.excludedMeta = this.excludedOk ? `${this.excludedDrives.length} drive(s)` : "Everything (es.exe) not reachable";
      this.renderExcluded(this.excludedDrives.map((dr) => ({
        name: dr.path.replace(/\\$/, ""), path: dr.path, is_dir: true, status: "drive",
        total_size: dr.total_size || 0, disk_bytes: dr.total_size || 0,
        backed_bytes: dr.backed_bytes || 0, excluded_bytes: dr.excluded_bytes || 0,
        pct_excluded: dr.total_size ? Math.round((dr.excluded_bytes / dr.total_size) * 1000) / 10 : null,
        total_files: dr.total_files || 0, backed_files: dr.backed_files || 0, excluded_files: dr.excluded_files || 0,
        bz_backed_bytes: dr.bz_backed_bytes || 0, bz_backed_files: dr.bz_backed_files || 0,
        last_backed: "", repo: "", mtime: "",
      })));
    },
    excludedNav(path) { if (!path) return this.renderExcludedDrives(); this.listExcluded(path); },
    async listExcluded(path) {
      const seq = ++this._excludedSeq;
      this.excludedPath = path;
      await this.wrap(async () => {
        const r = await getJson(`/api/excluded/list?path=${encodeURIComponent(path)}`);
        if (seq !== this._excludedSeq) return;  // a newer navigation superseded this one
        this.excludedMeta = `${r.count} items · ${fmtBytes(r.total_excluded)} not backed up here`;
        this.renderExcluded(r.items || []);
      }, "Listing (Everything)…");
    },
    excludedRowOpen(row) {
      if (row.status === "drive") return this.listExcluded(row.path);
      if (!row.is_dir) return;
      const base = this.excludedPath.replace(/[\\/]+$/, "");
      this.listExcluded(base + "\\" + row.name);
    },
    excludedUp() {
      const p = (this.excludedPath || "").replace(/[\\/]+$/, "");
      if (!p) return;
      if (/^[A-Za-z]:$/.test(p)) return this.renderExcludedDrives();
      const idx = p.lastIndexOf("\\");
      this.listExcluded(idx <= 2 ? p.slice(0, 2) + "\\" : p.slice(0, idx));
    },
    excludedCrumbs() {
      const out = [{ label: "This PC", path: "" }];
      if (!this.excludedPath) return out;
      const p = this.excludedPath.replace(/[\\/]+$/, "");
      let acc = "";
      p.split("\\").forEach((seg, i) => {
        acc = i === 0 ? seg + "\\" : acc + (acc.endsWith("\\") ? "" : "\\") + seg;
        out.push({ label: seg, path: acc });
      });
      return out;
    },
    setExcludedFilter(f) { this.excludedFilter = f; this._drawExcluded(); },
    renderExcluded(items) { this._excludedItems = items; this._drawExcluded(); },
    _drawExcluded() {
      const f = this.excludedFilter;
      const items = (this._excludedItems || []).filter((it) => {
        if (f === "all" || it.status === "drive") return true;
        if (f === "backed") return it.status === "backed" || it.status === "partial";
        return it.status !== "backed";  // "Not backed up" view = excluded / partial / new
      });
      const pill = (s) => {
        const m = { backed: ["#dcfce7", "#166534", "backed up"], excluded: ["#fee2e2", "#991b1b", "NOT backed up"], partial: ["#fef9c3", "#854d0e", "partial"], new: ["#dbeafe", "#1e40af", "new (not yet)"], drive: ["#e2e8f0", "#334155", "drive"] };
        const [bg, fg, label] = m[s] || ["#e2e8f0", "#334155", s];
        return `<span class="pill" style="background:${bg};color:${fg}">${label}</span>`;
      };
      const bytes = (c) => c.getValue() ? fmtBytes(c.getValue()) : "";
      const num = (c) => c.getValue() ? Number(c.getValue()).toLocaleString() : "";
      const cols = [
        { title: "Name", field: "name", widthGrow: 4, minWidth: 220, formatter: (c) => (c.getRow().getData().is_dir ? "📁 " : "📄 ") + c.getValue() },
        { title: "Status", field: "status", width: 115, formatter: (c) => pill(c.getValue()) },
        { title: "Total", field: "total_size", width: 90, hozAlign: "right", sorter: "number", formatter: bytes },
        { title: "Backed", field: "backed_bytes", width: 90, hozAlign: "right", sorter: "number", formatter: bytes },
        { title: "Excluded", field: "excluded_bytes", width: 95, hozAlign: "right", sorter: "number", formatter: bytes },
        { title: "% Excl", field: "pct_excluded", width: 70, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() != null ? c.getValue() + "%" : "" },
        { title: "Files", field: "total_files", width: 80, hozAlign: "right", sorter: "number", formatter: num },
        { title: "Bkd #", field: "backed_files", width: 75, hozAlign: "right", sorter: "number", formatter: num },
        { title: "Excl #", field: "excluded_files", width: 75, hozAlign: "right", sorter: "number", formatter: num },
        { title: "Last backed", field: "last_backed", width: 145, formatter: (c) => c.getValue() || "" },
        { title: "Repo", field: "repo", width: 80 },
        { title: "Also in BZ", field: "bz_backed_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() ? fmtBytes(c.getValue()) : "<span style='color:#94a3b8'>—</span>" },
        { title: "Modified", field: "mtime", width: 145, formatter: (c) => (c.getValue() || "").replace("T", " ") },
      ];
      const isDrives = this.excludedPath === "";
      setTable("#excludedTable", items, cols, {
        persistSort: !isDrives,  // drives view = always alphabetical; folders keep your persisted sort
        initialSort: isDrives ? [{ column: "name", dir: "asc" }] : undefined,
        onRowDblClick: (_e, row) => this.excludedRowOpen(row.getData()),
        rowMenu: [
          { label: "Open", action: (_e, row) => this.excludedRowOpen(row.getData()) },
          { label: "Details", action: (_e, row) => this.openExcludedDetail(row.getData()) },
          { label: "Include in Kopia… (copy commands)", action: (_e, row) => this.excludedInclude(row.getData()) },
          { label: "Exclude from Kopia (copy patterns)", action: (_e, row) => this.excludedExclude(row.getData()) },
          { label: "Copy path", action: (_e, row) => this.copyText(this._excludedPath(row.getData())) },
        ],
      });
    },
    _excludedPath(row) {
      if (row.status === "drive") return row.path;
      return this.excludedPath.replace(/[\\/]+$/, "") + "\\" + row.name;
    },
    async excludedInclude(row) {
      const path = this._excludedPath(row);
      const r = await getJson(`/api/excluded/include-cmds?path=${encodeURIComponent(path)}`).catch((e) => { this.error = e.message; return null; });
      if (!r) return;
      const txt = (r.commands || []).map((c) => `# ${c.name}\n${c.command}`).join("\n\n");
      this.copyText(txt);
      this.actionText = "Include commands (copied — run the one for the repo you want):\n" + txt;
    },
    excludedExclude(row) {
      const name = row.name;
      const pats = [name];
      if (!row.is_dir) { const dot = name.lastIndexOf("."); if (dot > 0) pats.push("*" + name.slice(dot)); }
      this.copyText(pats.join("\n"));
      this.actionText = "Exclude patterns (copied — paste into the KopiaUI policy ignore rules):\n" + pats.join("\n");
    },
    openExcludedDetail(row) {
      this.excludedDetail = { ...row, full_path: this._excludedPath(row) };
      this.showExcludedDetail = true;
    },

    // ---- Restore (accessible everywhere) ----
    openRestore(sourceId, name) {
      this.restore = {
        source_id: sourceId, name: name || sourceId, target: "", command: "",
        options: {
          restore_owners: true, restore_permissions: true, restore_times: true, skip_existing: true,
          overwrite_files: false, overwrite_directories: false, overwrite_symlinks: false,
          ignore_errors: false, write_files_atomically: false, write_sparse_files: false,
          ignore_permission_errors: false, delete_extra: false, parallel: 8, shallow: "",
        },
      };
      this.showRestore = true;
    },
    async restorePreview() {
      if (!this.restore || !this.restore.target) { window.alert("Enter a destination (folder, or a .zip/.tar path)."); return; }
      const r = await getJson("/api/actions/restore", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source_id: this.restore.source_id, target: this.restore.target, options: this.restore.options, confirm: false }) }).catch((e) => { this.error = e.message; return null; });
      if (r) this.restore.command = r.command;
    },
    async restoreExecute() {
      if (!this.restore || !this.restore.target) { window.alert("Enter a destination."); return; }
      const o = this.restore.options;
      if (o.overwrite_files || o.overwrite_directories || o.delete_extra) {
        if ((window.prompt("This restore can OVERWRITE or DELETE files at the destination. Type RESTORE to confirm:") || "").trim().toUpperCase() !== "RESTORE") return;
      }
      await this.wrap(async () => {
        const r = await getJson("/api/actions/restore", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source_id: this.restore.source_id, target: this.restore.target, options: o, confirm: true }) });
        this.actionText = "Restore complete:\n" + (r.command || []).join(" ");
        this.showRestore = false;
      }, "Restoring (large restores can take a while)…");
    },
    async browseFolder() {
      const r = await getJson("/api/actions/browse-folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ initial: this.restore && this.restore.target || null }) }).catch(() => null);
      if (r && r.path) this.restore.target = r.path;
    },
    restoreFromDetail() { if (this.detail) this.openRestore(this.detail.content_id || this.detail.title, this.detail.title); },

    // ---- Snapshots: pin / unpin / diff ----
    async pinSelected(remove) {
      const rows = this.snapSelected();
      if (!rows.length) { window.alert("Select snapshots first."); return; }
      const r = await getJson("/api/snapshots/pin", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_ids: rows.map((x) => x.id), remove }) }).catch((e) => { this.error = e.message; return null; });
      if (!r) return;
      this.copyText(r.command);
      this.actionText = (remove ? "Unpin" : "Pin") + " command copied (run it yourself):\n" + r.command;
    },
    async diffSelected() {
      const rows = this.snapSelected();
      if (rows.length !== 2) { window.alert("Select exactly 2 snapshots to diff."); return; }
      this.showDiff = true; this.diffText = "Running diff…"; this.diffBusy = true;
      try {
        const r = await getJson(`/api/snapshots/diff?repo=${encodeURIComponent(this.repo)}&id1=${encodeURIComponent(rows[0].id)}&id2=${encodeURIComponent(rows[1].id)}&run=true`);
        this.diffText = r.output || r.command || "(no output)";
      } catch (e) { this.diffText = "(diff failed: " + (e.message || e) + ")"; }
      finally { this.diffBusy = false; }
    },

    // ---------- Simulate (all sources or one) ----------
    async runSimulate(refresh = false) {
      await this.wrap(async () => {
        const body = { repo: this.repo, mode: "all", refresh, policy: this.policy };
        if (this.simMode === "one") body.source = this.selectedSource;
        const r = await getJson("/api/simulate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (this.simMode === "one") {
          this.simMeta = `${sourcePath(r.source)} · keep ${r.keep_snapshots} / delete ${r.delete_snapshots} of ${r.total_snapshots}`;
          setTable("#simulateTable", [r], [
            { title: "Source", field: "source", widthGrow: 4, minWidth: 240, formatter: (c) => sourcePath(c.getValue()) },
            { title: "Total", field: "total_snapshots", width: 80, hozAlign: "right" },
            { title: "Keep", field: "keep_snapshots", width: 80, hozAlign: "right" },
            { title: "Delete", field: "delete_snapshots", width: 80, hozAlign: "right" },
            { title: "Reclaim", field: "reclaim", width: 110, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          ], { initialSort: [{ column: "reclaim", dir: "desc" }] });
        } else {
          this.simMeta = `Combined reclaim ${fmtBytes(r.combined_reclaim)} · per-source sum ${fmtBytes(r.sum_per_source)} · shared between sources ${fmtBytes(r.shared_between_sources)}` + (r.cached ? " · cached" : "");
          setTable("#simulateTable", r.rows || [], [
            { title: "Source", field: "source", widthGrow: 4, minWidth: 240, sorter: srcSorter, formatter: (c) => sourcePath(c.getValue()) },
            { title: "Total", field: "total_snapshots", width: 80, hozAlign: "right" },
            { title: "Keep", field: "keep_snapshots", width: 80, hozAlign: "right" },
            { title: "Delete", field: "delete_snapshots", width: 80, hozAlign: "right" },
            { title: "Reclaim", field: "reclaim", width: 120, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
            { title: "Pins", field: "pins", width: 64, hozAlign: "right" },
          ], {
            initialSort: [{ column: "reclaim", dir: "desc" }],
            onRowDblClick: (_e, row) => { this.selectedSource = row.getData().source; this.snapSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); },
          });
        }
      }, "Simulating retention...");
    },

    // ---------- Stats ----------
    async loadStats(refresh = false) {
      if (this._statsLoading) return;        // never let stats requests pile up
      this._statsLoading = true;
      try {
        await this.wrap(async () => {
          const [stats, aud] = await Promise.all([
            getJson(`/api/stats?repo=${encodeURIComponent(this.repo)}`),
            getJson(`/api/audit?limit=200`).catch(() => ({ records: [], size: 0 })),
          ]);
          this.statsData = stats;
          this.auditRows = aud.records || [];
          this.auditSize = aud.size || 0;
        }, "Loading stats...");
      } finally { this._statsLoading = false; }
    },
    async loadAudit() {
      const aud = await getJson(`/api/audit?limit=200`).catch(() => ({ records: [], size: 0 }));
      this.auditRows = aud.records || [];
      this.auditSize = aud.size || 0;
    },
    auditSummary(r) {
      const s = r.summary || {};
      return Object.entries(s).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ");
    },
    async loadBlobStats() {
      if (this.blobLoading) return;
      this.blobLoading = true;
      try {
        await this.wrap(async () => {
          const r = await getJson(`/api/stats/blob?repo=${encodeURIComponent(this.repo)}`);
          this.blobStats = r.blob_stats || "(empty)";
        }, "Running blob stats (slow, one-off)...");
      } finally { this.blobLoading = false; }
    },
    statsBlock(v) { return typeof v === "string" ? v : JSON.stringify(v, null, 2); },

    // ---------- Mounts / actions ----------
    async loadMounts() {
      const r = await getJson(`/api/actions/mounts`).catch(() => ({ mounts: [] }));
      this.mounts = r.mounts || [];
    },
    mountRow(snapId) {
      const mp = window.prompt("Mount snapshot (read-only) to drive/folder:", "Z:");
      if (!mp) return;
      this.wrap(async () => {
        const r = await getJson("/api/actions/mount", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: snapId, mountpoint: mp }) });
        this.actionText = `Mount started (pid ${r.pid}):\n${(r.command || []).join(" ")}`;
        this.loadMounts();
      }, "Starting mount...");
    },
    async mount() {
      await this.wrap(async () => {
        const r = await getJson("/api/actions/mount", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: this.mountSnapshot, mountpoint: this.mountPoint }) });
        this.actionText = `Mount started (pid ${r.pid}):\n${(r.command || []).join(" ")}`;
        this.loadMounts();
      }, "Starting mount...");
    },
    async unmount(pid) {
      await this.wrap(async () => {
        await getJson("/api/actions/unmount", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pid }) });
        this.loadMounts();
      }, "Unmounting...");
    },
    async restorePreview() {
      await this.wrap(async () => {
        const r = await getJson("/api/actions/restore-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source: this.restoreSource, destination: this.restoreDest }) });
        this.actionText = `Restore command (preview - run it yourself):\n${(r.command || []).join(" ")}`;
      });
    },

    csv(id, name) { downloadTable(id, name); },

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
        this.authProtected = !!kl.auth_password_hash;
        this.newPassword = "";
        this._readAdvanced(kl);
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
      this.indexMaxRetries = Number(kl.index_max_retries ?? 1);
      // ?? not ||: 0 is a meaningful value here (disables the stage) and || would silently
      // rewrite it to the default on every read and every save.
      this.folderPrewarmBudget = Number(kl.folder_prewarm_budget ?? 900);
      this.startupDelay = Number(kl.startup_delay ?? 300);
      this.dirMemoMaxMb = Number(kl.dir_memo_max_mb || 1500);
      this.refcountWriteInterval = Number(kl.refcount_write_interval || 21600);
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
          index_max_retries: Math.max(0, Number(this.indexMaxRetries) || 0),
          folder_prewarm_budget: Number.isFinite(Number(this.folderPrewarmBudget))
            ? Math.max(0, Number(this.folderPrewarmBudget)) : 900,   // 0 = disable the stage
          startup_delay: Number(this.startupDelay) || 0,
          dir_memo_max_mb: Number(this.dirMemoMaxMb) || 1500,
          refcount_write_interval: Number(this.refcountWriteInterval) || 21600,
        };
        await getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ listing_backend: this.backend, kopialens }) });
        this.armAutoRefresh();  // apply the new dashboard auto-refresh interval immediately
        this.showSettings = false;
        window.alert("Saved. Scheduling and interval changes are picked up on the next tick (within a minute). The listing backend, directory-cache budget and startup delay need a restart.");
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
  }));
});
