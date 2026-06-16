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
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
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
    repos: [], repo: "1",
    tabs: [
      { id: "dashboard", label: "Dashboard" }, { id: "snapshots", label: "Snapshots" },
      { id: "tree", label: "Tree" }, { id: "search", label: "Search" },
      { id: "simulate", label: "Simulate" }, { id: "stats", label: "Stats" },
      { id: "actions", label: "Actions" },
    ],
    active: "dashboard",
    dark: false,
    timeFmt: localStorage.getItem("kl-timefmt") || "absolute",
    indexStatus: {},
    sources: [], selectedSource: "",
    snapshots: [], selectedSnapshot: "",
    showChurn: localStorage.getItem("kl-churn") === "1",
    analyzing: false,
    treeMode: "full",
    query: "", deletedOnly: false, overOneGb: false, searchSource: "", searchExt: "", omniMeta: "",
    searchTotal: 0, searchPage: 1, searchPageSize: "500", searchSort: "size", searchDir: "desc",
    pageSizes: ["100", "200", "500", "1000", "2000", "5000", "10000", "Infinite"],
    policyKeys: ["latest", "hourly", "daily", "weekly", "monthly", "annual"],
    policy: { latest: 100, hourly: 96, daily: 30, weekly: 52, monthly: 36, annual: 5 },
    simMode: "all", simMeta: "", simText: "", actionText: "",
    mounts: [],
    statsData: null,
    mountSnapshot: "", mountPoint: "", restoreSource: "", restoreDest: "",
    dashboardMeta: "", busy: "", error: "", detail: null, _poll: null, _searchSeq: 0,
    showSettings: false, backend: "subprocess",

    fmtB(v) { return fmtBytes(v); },
    fmtPhys(v) { return fmtPhys(v); },
    humanTime(s) { return humanTime(s); },
    shortSource(s) { return sourcePath(s); },
    sourceRoot() { return sourcePath(this.selectedSource); },

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
        this.repo = data.settings?.last_repo || "1";
        this.backend = data.settings?.listing_backend || "subprocess";
        this.searchPageSize = String(data.settings?.kopialens?.page_size || 500);
        await this.refreshBase();
        await this.loadDashboard();
      });
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
      this.activate();
    },
    copyText(t) { try { navigator.clipboard.writeText(t); } catch (e) {} },

    indexLine() {
      const s = this.indexStatus;
      if (s.building) return `Indexing ${s.indexed_snapshots || 0}/${s.snapshots || "?"}...`;
      if (s.snapshots === undefined) return "Index: unknown";
      return `Index ${s.indexed_snapshots}/${s.snapshots}`;
    },

    async repoChanged() {
      this.selectedSnapshot = ""; this.snapshots = [];
      getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ last_repo: this.repo }) }).catch(() => {});
      await this.wrap(async () => {
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
      if (!this.sources.includes(this.selectedSource)) this.selectedSource = this.sources[0] || "";
    },

    activate() {
      if (this.active === "dashboard") return this.loadDashboard();
      if (this.active === "snapshots") return this.loadSnapshots();
      if (this.active === "tree") return this.openTree();
      if (this.active === "search") return this.loadSearch();
      if (this.active === "simulate") return;  // heavy - user clicks Run
      if (this.active === "stats") return this.statsData ? null : this.loadStats();
      if (this.active === "actions") return this.loadMounts();
    },

    async refreshAll() {
      await this.wrap(async () => {
        await this.refreshBase(true);
        if (this.active === "dashboard") return this.loadDashboard(true);
        if (this.active === "snapshots") return this.loadSnapshots(true);
        if (this.active === "tree") return this.loadTree();
        if (this.active === "search") return this.loadSearch();
        if (this.active === "stats") return this.loadStats(true);
        if (this.active === "actions") return this.loadMounts();
      }, "Refreshing...");
    },

    async buildIndex() {
      await this.wrap(async () => {
        await getJson(`/api/index/build?repo=${encodeURIComponent(this.repo)}`, { method: "POST" });
        this.pollIndex();
      }, "Index update started...");
    },
    async rebuildIndex() {
      await this.wrap(async () => {
        await getJson(`/api/index/build?repo=${encodeURIComponent(this.repo)}&full=true`, { method: "POST" });
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
      await this.wrap(async () => {
        const data = await getJson(`/api/dashboard?repo=${encodeURIComponent(this.repo)}&refresh=${refresh}`);
        this.dashboardMeta = `true physical ${fmtBytes(data.union_total)}` + (data.cached ? "" : ` · computed in ${Number(data.elapsed || 0).toFixed(1)}s`);
        setTable("#dashboardTable", data.rows || [], [
          { title: "Source", field: "source", widthGrow: 4, minWidth: 240, sorter: srcSorter, formatter: (c) => sourcePath(c.getValue()) },
          { title: "Snaps", field: "snaps", width: 70, hozAlign: "right" },
          { title: "Physical", field: "physical", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Accum", field: "accum", width: 75, formatter: (c) => `${Number(c.getValue()).toFixed(1)}x`, hozAlign: "right" },
          { title: "%Repo", field: "repo_pct", width: 72, formatter: (c) => `${Number(c.getValue()).toFixed(1)}%`, hozAlign: "right" },
          { title: "Freeable", field: "freeable", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Dedup", field: "dedup", width: 72, formatter: (c) => `${Number(c.getValue()).toFixed(0)}x`, hozAlign: "right" },
          { title: "Latest size", field: "logical_latest", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Last snapshot", field: "last_time", width: 165, formatter: (c) => timeCell(c.getValue()) },
          { title: "Next snapshot", field: "next_snapshot", width: 165, formatter: (c) => timeCell(c.getValue()) },
          { title: "Pins", field: "pins", width: 64, hozAlign: "right" },
        ], {
          initialSort: [{ column: "source", dir: "asc" }],
          onRowDblClick: (_e, row) => { this.selectedSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); },
          rowMenu: [
            { label: "Browse snapshots", action: (_e, row) => { this.selectedSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); } },
            { label: "Browse files (tree)", action: (_e, row) => { this.selectedSource = row.getData().source; this.active = "tree"; this.openTree(); } },
            { label: "Simulate this source", action: (_e, row) => { this.selectedSource = row.getData().source; this.simMode = "one"; this.active = "simulate"; this.runSimulate(); } },
            { label: "Copy source", action: (_e, row) => this.copyText(row.getData().source) },
          ],
        });
      }, "Loading dashboard...");
    },

    // ---------- Snapshots ----------
    async loadSnapshots(refresh = false) {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        const data = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`);
        this.snapshots = data.snapshots || [];
        this.analyzing = !!data.analyzing;
        if (!this.snapshots.find((x) => x.id === this.selectedSnapshot)) this.selectedSnapshot = this.snapshots.length ? this.snapshots[this.snapshots.length - 1].id : "";
        const cols = [
          { title: "Start", field: "start_time", width: 185, formatter: (c) => timeCell(c.getValue()) },
          { title: "Logical", field: "logical", width: 100, sorter: "number", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Churn (new)", field: "churn", width: 105, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Reclaimable", field: "reclaimable", width: 110, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", width: 90, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Dirs", field: "dirs", width: 80, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Retention", field: "retention", widthGrow: 2, minWidth: 150, formatter: (c) => (c.getValue() || []).map((r) => `<span class="pill" style="background:#dbeafe;color:#1e40af">${r}</span>`).join("") },
          { title: "Pins", field: "pins", widthGrow: 1, minWidth: 90, formatter: (c) => (c.getValue() || []).map((p) => `<span class="pill" style="background:#fef3c7;color:#92400e">${p}</span>`).join("") },
          { title: "ID", field: "id", width: 200 },
        ];
        setTable("#snapshotsTable", this.snapshots, cols, {
          initialSort: [{ column: "start_time", dir: "desc" }],
          onRowDblClick: (_e, row) => { this.selectedSnapshot = row.getData().id; this.mountSnapshot = this.selectedSnapshot; this.active = "tree"; this.treeMode = "full"; this.treePrefix = this.sourceRoot(); this.loadTree(); },
          rowMenu: [
            { label: "Browse files", action: (_e, row) => { this.selectedSnapshot = row.getData().id; this.active = "tree"; this.treeMode = "full"; this.treePrefix = this.sourceRoot(); this.loadTree(); } },
            { label: "Mount snapshot (read-only)", action: (_e, row) => this.mountRow(row.getData().id) },
            { label: "Copy snapshot ID", action: (_e, row) => this.copyText(row.getData().id) },
          ],
        });
      }, "Loading snapshots...");
    },

    async analyzeSnapshots() {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        await getJson(`/api/snapshots/analyze?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}&reclaim=true`, { method: "POST" });
        this.analyzing = true;
        const poll = setInterval(async () => {
          const d = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`).catch(() => null);
          if (d && !d.analyzing) { clearInterval(poll); this.loadSnapshots(); }
        }, 4000);
      }, "Analyzing snapshots (churn + reclaimable)...");
    },

    // ---------- Tree ----------
    openTree() {
      if (!this.selectedSource) return;
      if (this.treeMode === "all") { this.treePrefix = this.sourceRoot(); return this.loadTree(); }
      const ensure = (!this.snapshots.length || !this.snapshots.find((s) => s.id === this.selectedSnapshot)) ? this.loadSnapshots() : Promise.resolve();
      return Promise.resolve(ensure).then(() => {
        if (!this.selectedSnapshot && this.snapshots.length) this.selectedSnapshot = this.snapshots[this.snapshots.length - 1].id;
        if (!this.treePrefix || !this.treePrefix.startsWith(this.sourceRoot().replace(/[\\/]+$/, ""))) this.treePrefix = this.sourceRoot();
        return this.loadTree();
      });
    },
    treeSourceChanged() { this.snapshots = []; this.selectedSnapshot = ""; this.treePrefix = this.sourceRoot(); this.openTree(); },
    treeModeChanged() { this.treePrefix = this.sourceRoot(); this.openTree(); },
    navTree(prefix) { this.treePrefix = prefix; this.loadTree(); },
    treeUp() { const c = this.treeCrumbs(); if (c.length > 1) this.navTree(c[c.length - 2].prefix); },

    async loadTree() {
      if (!this.selectedSource) return;
      if (this.treeMode !== "all" && !this.selectedSnapshot) return;
      await this.wrap(async () => {
        const url = `/api/tree?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`
          + `&snapshot_id=${encodeURIComponent(this.selectedSnapshot)}&prefix=${encodeURIComponent(this.treePrefix)}&mode=${this.treeMode}`;
        const data = await getJson(url);
        const children = data.children || [];
        if (!children.length && !(this.indexStatus.indexed_snapshots > 0)) this.error = "Snapshot not indexed yet - click Index (top right).";
        const total = children.reduce((a, c) => a + (c.size || 0), 0) || 1;
        children.forEach((c) => { c.pct = 100 * (c.size || 0) / total; });
        setTable("#treeTable", children, [
          { title: "Name", field: "name", widthGrow: 4, minWidth: 260, formatter: (c) => `${c.getData().type === "folder" ? "📁 " : "📄 "}${c.getValue()}` },
          { title: "Size", field: "size", width: 105, sorter: "number", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Physical", field: "physical", width: 100, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "% of folder", field: "pct", width: 90, formatter: (c) => `${Number(c.getValue() || 0).toFixed(1)}%`, hozAlign: "right" },
          { title: "Files", field: "files", width: 85, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Modified", field: "modified", width: 165, formatter: (c) => timeCell(c.getValue()) },
          { title: "Type", field: "type", width: 75 },
        ], {
          initialSort: [{ column: "size", dir: "desc" }],
          onRowDblClick: (_e, row) => { const d = row.getData(); if (d.type === "folder") this.navTree(d.path); else this.openFileDetail({ source: this.selectedSource, full_path: d.path }); },
          rowMenu: [
            { label: "Open / drill in", action: (_e, row) => { const d = row.getData(); if (d.type === "folder") this.navTree(d.path); } },
            { label: "Details (physical / dedup)", action: (_e, row) => this.openRowDetail(row.getData()) },
            { label: "Version history", action: (_e, row) => this.openFileDetail({ source: this.selectedSource, full_path: row.getData().path }) },
            { label: "Find in search", action: (_e, row) => { this.query = row.getData().name; this.searchSource = ""; this.active = "search"; this.loadSearch(); } },
            { label: "Copy path", action: (_e, row) => this.copyText(row.getData().path) },
            { label: "Mount this snapshot (read-only)", action: () => this.mountRow(this.selectedSnapshot) },
          ],
        });
      }, "Loading tree...");
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
      return p;
    },
    searchPages() { return Math.max(1, Math.ceil(this.searchTotal / (parseInt(this.searchPageSize, 10) || 500))); },
    setSearchPage(n) { this.searchPage = Math.min(Math.max(1, n), this.searchPages()); this.loadSearch(true); },
    setSearchSort(field, dir) { this.searchSort = field; this.searchDir = dir; this.searchPage = 1; this.loadSearch(); },
    changePageSize() {
      getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kopialens: { page_size: this.searchPageSize === "Infinite" ? 500 : this.searchPageSize } }) }).catch(() => {});
      this.searchPage = 1; this.loadSearch();
    },

    async loadSearch(keepPage = false) {
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
        const cols = [
          { title: "Path", field: "full_path", widthGrow: 5, minWidth: 320, headerSortStartingDir: "asc" },
          { title: "Size", field: "size", width: 100, sorter: "number", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Physical", field: "physical", width: 100, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Ext", field: "ext", width: 70 },
          { title: "Versions", field: "versions", width: 80, hozAlign: "right" },
          { title: "Last seen", field: "last_seen", width: 160, formatter: (c) => timeCell(c.getValue()) },
          { title: "On disk", field: "in_latest", width: 90, formatter: (c) => c.getValue() ? "yes" : "<span style='color:#dc2626'>deleted</span>" },
          { title: "Source", field: "source", widthGrow: 2, minWidth: 180, formatter: (c) => sourcePath(c.getValue()) },
        ];
        const sortMap = { full_path: "full_path", size: "size", physical: "physical", ext: "ext", versions: "versions", last_seen: "last_seen", in_latest: "in_latest", source: "source" };
        const table = setTable("#searchTable", data.results || [], cols, {
          persistSort: false,  // server controls sort; client-persisted sort would fight it
          initialSort: [{ column: this.searchSort, dir: this.searchDir }],
          onRowDblClick: (_e, row) => this.openFileDetail(row.getData()),
          rowMenu: [
            { label: "File detail (physical / reclaim / dedup)", action: (_e, row) => this.openFileDetail(row.getData()) },
            { label: "Version history", action: (_e, row) => this.openFileDetail(row.getData()) },
            { label: "Mount the snapshot it lives in", action: (_e, row) => this.mountLatestFor(row.getData()) },
            { label: "Jump to it in tree", action: (_e, row) => this.jumpToTree(row.getData()) },
            { label: "Browse this source", action: (_e, row) => { this.selectedSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); } },
            { label: "Copy full path", action: (_e, row) => this.copyText(row.getData().full_path) },
          ],
        });
        // server-side sort: re-query when the user clicks a header (don't sort the page locally)
        if (table) table.on("dataSorting", (sorters) => {
          if (!sorters.length) return;
          const f = sortMap[sorters[0].field]; if (!f) return;
          if (f === this.searchSort && sorters[0].dir === this.searchDir) return;
          this.setSearchSort(f, sorters[0].dir);
        });
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
      await this.wrap(async () => {
        const base = `repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`;
        const [detail, vres, purge] = await Promise.all([
          getJson(`/api/search/detail?${base}`),
          getJson(`/api/search/versions?${base}`),
          getJson(`/api/actions/purge-plan?${base}`),
        ]);
        const items = purge.items || [];
        const purgeRows = (purge.commands || []).map((c, i) => {
          const it = items[i] || {};
          return { cmd: Array.isArray(c) ? c.join(" ") : c, tag: it.pinned ? "PINNED" : ((it.retention && it.retention.length) ? `retained: ${it.retention.join(",")}` : "") };
        });
        this.detail = {
          kind: "file",
          title: item.full_path,
          source: detail.source || item.source,
          on_disk: item.in_latest,
          logical: detail.logical, physical: detail.physical, freeable: detail.freeable,
          shared: detail.shared, physical_all: detail.physical_all_versions, chunk_count: detail.chunk_count,
          chunks: detail.chunks || [],
          others: detail.other_paths || [],
          versions: (vres.versions || []).map((x) => ({ time: humanTime(x.start_time), size: x.size, snapshot_id: x.snapshot_id, content_id: x.content_id })),
          purge: purgeRows,
          purgeCmds: purgeRows.map((r) => r.cmd).join("\n"),
        };
      }, "Loading file detail...");
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
            onRowDblClick: (_e, row) => { this.selectedSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); },
          });
        }
      }, "Simulating retention...");
    },

    // ---------- Stats ----------
    async loadStats(refresh = false) {
      await this.wrap(async () => {
        this.statsData = await getJson(`/api/stats?repo=${encodeURIComponent(this.repo)}`);
      }, "Loading stats...");
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

    async saveSettings() {
      await this.wrap(async () => {
        await getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ listing_backend: this.backend }) });
        this.showSettings = false;
        window.alert("Saved. Restart KopiaLens for the listing backend change to take effect.");
      });
    },
  }));
});
