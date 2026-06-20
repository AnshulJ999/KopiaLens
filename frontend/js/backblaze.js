// Backblaze view logic for KopiaLens. Loaded BEFORE app.js; exposes window.bzMixin(), which app.js
// spreads into the single Alpine `kopiaLens` component. All state + methods are bz-prefixed and BZ
// hits ONLY /api/backblaze/* (never the kopia/DuckDB routes). Reuses app.js's module-level helpers
// (fmtBytes, fmtPhys, humanTime, timeCell, sourcePath, getJson, setTable) at call time.
window.bzMixin = function () {
  const bzFmt = (v) => fmtBytes(v);
  const statusPill = (s) => {
    const m = {
      backed: ["#dcfce7", "#166534", "backed up"], excluded: ["#fee2e2", "#991b1b", "excluded"],
      mixed: ["#fef9c3", "#854d0e", "mixed"], drive: ["#e2e8f0", "#334155", "drive"],
    };
    const [bg, fg, label] = m[s] || ["#e2e8f0", "#334155", s];
    return `<span class="pill" style="background:${bg};color:${fg}">${label}</span>`;
  };
  const reposBadges = (arr) => (arr || []).map((r) => {
    const c = { "1": ["#dbeafe", "#1e40af"], "2": ["#ede9fe", "#5b21b6"], "bz": ["#fee2e2", "#991b1b"] }[r] || ["#e2e8f0", "#334155"];
    return `<span class="pill" style="background:${c[0]};color:${c[1]}">${r}</span>`;
  }).join("");

  return {
    // ---- BZ state ----
    bzTabs: [
      { id: "bz-dashboard", label: "Dashboard" }, { id: "bz-search", label: "Search" },
      { id: "bz-tree", label: "Tree" }, { id: "bz-excluded", label: "Excluded" },
      { id: "bz-exclusions", label: "Exclusions" }, { id: "bz-churn", label: "Churn" },
      { id: "bz-queue", label: "Queue" }, { id: "bz-sessions", label: "Sessions" },
      { id: "bz-health", label: "Health" },
    ],
    bzStatus: { built: false, building: false, total_files: 0, backed_files: 0 },
    bzStats: null, bzOverview: null,
    bzChurnRows: [], bzChurnTotal: 0,
    bzSearchDrive: "", bzSearchExt: "", bzSearchStatus: "all",
    bzSearchSort: "size", bzSearchDir: "desc", bzSearchTotal: 0, bzSearchPage: 1, bzSearchPageSize: "500",
    bzGroupByFolder: false, bzGroupDepth: 2, bzSearchMeta: "",
    bzTreeDrive: "", bzTreePrefix: "",
    bzExclPath: "", bzExclFilter: "all", bzExclMeta: "", _bzExclItems: [], _bzExclSeq: 0,
    bzExclusions: null, bzNamingQuery: "", bzNamingRows: [],
    bzGen: null, bzShowGen: false,
    bzQueue: null, bzQueueDepth: 1, bzSessions: null, bzHealth: null,
    bzDetail: null, bzDrives: [],

    // ---- lifecycle ----
    async bzInit() {
      await this.wrap(async () => {
        await this.bzRefreshStatus();
        await this.bzLoadDashboard();
      }, "Loading Backblaze…");
    },
    bzActivate() {
      switch (this.active) {
        case "bz-dashboard": return this.bzLoadDashboard();
        case "bz-search": return this.bzLoadSearch();
        case "bz-tree": return this.bzOpenTree();
        case "bz-excluded": return this.bzLoadExcluded();
        case "bz-exclusions": return this.bzLoadExclusions();
        case "bz-churn": return this.bzLoadChurn();
        case "bz-queue": return this.bzLoadQueue();
        case "bz-sessions": return this.bzLoadSessions();
        case "bz-health": return this.bzLoadHealth();
      }
    },
    async bzRefreshStatus() {
      const s = await getJson("/api/backblaze/status").catch(() => null);
      if (s) this.bzStatus = s;
    },
    bzStatusLine() {
      const s = this.bzStatus;
      if (s.building) return "Indexing Backblaze…";
      if (!s.built) return "BZ: not indexed";
      return `BZ ${Number(s.backed_files || 0).toLocaleString()} backed / ${Number(s.total_files || 0).toLocaleString()} scanned`;
    },
    async bzRefresh() {
      await this.wrap(async () => {
        await getJson("/api/backblaze/refresh", { method: "POST" });
        await this.bzRefreshStatus();
        await this.bzActivate();
      }, "Refreshing Backblaze index…");
    },

    // ---- Dashboard ----
    async bzLoadDashboard() {
      await this.wrap(async () => {
        const [ov, st, ch] = await Promise.all([
          getJson("/api/backblaze/overview"),
          getJson("/api/backblaze/stats"),
          getJson("/api/backblaze/churn?top=10"),
        ]);
        this.bzOverview = ov; this.bzStats = st;
        this.bzChurnRows = (ch.rows || []).slice(0, 10); this.bzChurnTotal = ch.total_recent || 0;
        setTable("#bzDashTable", ov.rows || [], [
          { title: "Drive", field: "drive", width: 70, formatter: (c) => c.getValue() + ":" },
          { title: "Backed files", field: "backed_files", hozAlign: "right", width: 120, sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Backed size", field: "backed_bytes", hozAlign: "right", width: 120, sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Excluded files", field: "excluded_files", hozAlign: "right", width: 120, sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Excluded size", field: "excluded_bytes", hozAlign: "right", width: 120, sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Scanned", field: "total", hozAlign: "right", width: 110, sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "BZ authoritative", field: "auth_files", hozAlign: "right", width: 130, sorter: "number", formatter: (c) => c.getValue() == null ? "—" : Number(c.getValue()).toLocaleString() },
          { title: "Diff", field: "diff", hozAlign: "right", width: 80, formatter: (c) => { const v = c.getValue(); return v == null ? "—" : (v === 0 ? "<span style='color:#16a34a'>+0</span>" : `<span style='color:#dc2626'>${v > 0 ? "+" : ""}${v}</span>`); } },
        ], {
          initialSort: [{ column: "drive", dir: "asc" }],
          onRowDblClick: (_e, row) => { this.bzTreeDrive = row.getData().drive; this.active = "bz-tree"; this.bzOpenTree(); },
          rowMenu: [
            { label: "Browse drive (Tree)", action: (_e, row) => { this.bzTreeDrive = row.getData().drive; this.active = "bz-tree"; this.bzOpenTree(); } },
            { label: "Search this drive", action: (_e, row) => { this.bzSearchDrive = row.getData().drive; this.active = "bz-search"; this.bzLoadSearch(); } },
          ],
        });
      }, "Loading Backblaze dashboard…");
    },
    bzCategories() {
      const fs = (this.bzStats && this.bzStats.filestats) || {};
      const order = ["everything", "video", "music", "pict", "docs", "spreadsheets", "presentations", "financial", "email", "zips", "browserfavorites", "other"];
      return order.filter((k) => fs[k]).map((k) => ({ name: k, num: fs[k].num, bytes: fs[k].bytes }));
    },

    // ---- Search (FTS5) ----
    bzSearchUrl(extra = {}) {
      const p = new URLSearchParams({ q: this.query || "", sort: this.bzSearchSort, direction: this.bzSearchDir, status: this.bzSearchStatus });
      if (this.bzSearchDrive) p.set("drive", this.bzSearchDrive);
      if (this.bzSearchExt) p.set("ext", this.bzSearchExt);
      for (const [k, v] of Object.entries(extra)) p.set(k, v);
      return p;
    },
    bzSearchPages() { return Math.max(1, Math.ceil(this.bzSearchTotal / (parseInt(this.bzSearchPageSize, 10) || 500))); },
    bzSetSearchPage(n) { this.bzSearchPage = Math.min(Math.max(1, n), this.bzSearchPages()); this.bzLoadSearch(true); },
    bzSetSearchSort(field) {
      this.bzSearchDir = (this.bzSearchSort === field && this.bzSearchDir === "desc") ? "asc" : "desc";
      this.bzSearchSort = field; this.bzSearchPage = 1; this.bzLoadSearch();
    },
    bzToggleGroup() { this.bzGroupByFolder = !this.bzGroupByFolder; this.bzLoadSearch(); },
    async bzLoadSearch(keepPage = false) {
      if (this.bzGroupByFolder) return this.bzLoadGroup();
      if (!keepPage) this.bzSearchPage = 1;
      await this.wrap(async () => {
        const size = this.bzSearchPageSize === "Infinite" ? 100000 : (parseInt(this.bzSearchPageSize, 10) || 500);
        const offset = this.bzSearchPageSize === "Infinite" ? 0 : (this.bzSearchPage - 1) * size;
        const p = this.bzSearchUrl({ limit: size, offset });
        const data = await getJson(`/api/backblaze/search?${p.toString()}`);
        this.bzSearchTotal = data.total || 0;
        this.bzSearchMeta = `${this.bzSearchTotal.toLocaleString()} results`;
        const arrow = (f) => (this.bzSearchSort === f ? (this.bzSearchDir === "desc" ? " ▼" : " ▲") : "");
        const self = this;
        const H = (title, field, extra) => Object.assign({ title: title + arrow(field), field, headerSort: false, headerClick: () => self.bzSetSearchSort(field) }, extra || {});
        setTable("#bzSearchTable", data.rows || [], [
          H("Path", "full_path", { widthGrow: 5, minWidth: 320, formatter: (c) => `${c.getRow().getData().excluded ? "🚫 " : "✓ "}${c.getValue()}` }),
          H("Size", "size", { width: 100, hozAlign: "right", formatter: (c) => bzFmt(c.getValue()) }),
          H("Ext", "ext", { width: 70 }),
          { title: "Status", field: "excluded", headerSort: false, width: 95, formatter: (c) => statusPill(c.getValue() ? "excluded" : "backed") },
          { title: "Excluded by", field: "excl_rule", headerSort: false, widthGrow: 2, minWidth: 160, formatter: (c) => c.getValue() || "" },
          H("Drive", "drive", { width: 70, formatter: (c) => c.getValue() + ":" }),
        ], {
          persistSort: false,
          onRowDblClick: (_e, row) => this.bzOpenDetail(row.getData()),
          rowMenu: [
            { label: "Details", action: (_e, row) => this.bzOpenDetail(row.getData()) },
            { label: "Browse containing folder (Tree)", action: (_e, row) => this.bzJumpTree(row.getData()) },
            { label: "Generate exclusion (this file)", action: (_e, row) => this.bzOpenGen("editable_endswith", row.getData().name) },
            { label: "Copy path", action: (_e, row) => this.copyText(row.getData().full_path) },
          ],
        });
      }, "Searching Backblaze…");
    },
    async bzLoadGroup() {
      await this.wrap(async () => {
        const p = this.bzSearchUrl({ depth: this.bzGroupDepth, status: this.bzSearchStatus, top: 500 });
        const data = await getJson(`/api/backblaze/group-by-folder?${p.toString()}`);
        const rows = data.rows || [];
        this.bzSearchMeta = `${rows.length} folders · depth ${data.depth}`;
        setTable("#bzGroupTable", rows, [
          { title: "Folder", field: "folder", widthGrow: 5, minWidth: 320 },
          { title: "Files", field: "files", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Size", field: "bytes", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], {
          initialSort: [{ column: "bytes", dir: "desc" }],
          onRowDblClick: (_e, row) => { this.query = row.getData().folder; this.bzGroupByFolder = false; this.bzLoadSearch(); },
        });
      }, "Grouping folders…");
    },
    bzExportSearch() { window.open(`/api/backblaze/export?${this.bzSearchUrl().toString()}`, "_blank"); },

    // ---- Tree ----
    bzOpenTree() {
      if (!this.bzTreeDrive) this.bzTreeDrive = (this.bzStatus.drives && this.bzStatus.drives[0] && this.bzStatus.drives[0].drive) || "C";
      if (!this.bzTreePrefix || this.bzTreePrefix[0].toUpperCase() !== this.bzTreeDrive) this.bzTreePrefix = this.bzTreeDrive + ":\\";
      return this.bzLoadTree();
    },
    bzTreeChanged() { this.bzTreePrefix = this.bzTreeDrive + ":\\"; this.bzLoadTree(); },
    bzTreeNav(prefix) { this.bzTreePrefix = prefix; this.bzLoadTree(); },
    bzTreeCrumbs() {
      const out = []; const p = (this.bzTreePrefix || "").replace(/\\+$/, "");
      let acc = "";
      p.split("\\").forEach((seg, i) => { acc = i === 0 ? seg + "\\" : acc + (acc.endsWith("\\") ? "" : "\\") + seg; out.push({ label: seg || acc, prefix: acc }); });
      return out;
    },
    bzTreeUp() { const c = this.bzTreeCrumbs(); if (c.length > 1) this.bzTreeNav(c[c.length - 2].prefix); },
    async bzLoadTree() {
      await this.wrap(async () => {
        const data = await getJson(`/api/backblaze/folder?prefix=${encodeURIComponent(this.bzTreePrefix)}`);
        const kids = data.children || [];
        setTable("#bzTreeTable", kids, [
          { title: "Name", field: "name", widthGrow: 5, minWidth: 300, formatter: (c) => `${c.getRow().getData().is_dir ? "📁 " : "📄 "}${c.getValue()}` },
          { title: "Status", field: "status", width: 95, formatter: (c) => statusPill(c.getValue()) },
          { title: "Backed", field: "backed_bytes", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Excluded", field: "excluded_bytes", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Files", field: "total_files", width: 90, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Excluded by", field: "excl_rule", widthGrow: 2, minWidth: 150, formatter: (c) => c.getValue() || "" },
        ], {
          initialSort: [{ column: "total_files", dir: "desc" }],
          onRowDblClick: (_e, row) => { const d = row.getData(); if (d.is_dir) this.bzTreeNav(this.bzTreePrefix.replace(/\\+$/, "") + "\\" + d.name); },
          rowMenu: [
            { label: "Open / drill in", action: (_e, row) => { const d = row.getData(); if (d.is_dir) this.bzTreeNav(this.bzTreePrefix.replace(/\\+$/, "") + "\\" + d.name); } },
            { label: "Generate exclusion (this folder)", action: (_e, row) => this.bzOpenGen("folder", "\\" + row.getData().name + "\\") },
            { label: "Find in search", action: (_e, row) => { this.query = row.getData().name; this.active = "bz-search"; this.bzLoadSearch(); } },
            { label: "Copy path", action: (_e, row) => this.copyText(this.bzTreePrefix.replace(/\\+$/, "") + "\\" + row.getData().name) },
          ],
        });
      }, "Loading folder…");
    },
    bzJumpTree(item) {
      const fp = item.full_path; const drive = fp[0].toUpperCase();
      const parent = fp.includes("\\") ? fp.slice(0, fp.lastIndexOf("\\")) : drive + ":\\";
      this.bzTreeDrive = drive; this.bzTreePrefix = parent; this.active = "bz-tree"; this.bzLoadTree();
    },

    // ---- Excluded (BZ-native coverage + Kopia cross-column) ----
    async bzLoadExcluded() {
      if (this.bzExclPath) return this.bzExclList(this.bzExclPath);
      return this.bzExclDrives();
    },
    bzExclNav(path) { if (!path) return this.bzExclDrives(); this.bzExclList(path); },
    async bzExclDrives() {
      this.bzExclPath = "";
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/excluded/drives");
        this.bzExclMeta = `${r.drives.length} drive(s) · ${bzFmt(r.grand_bytes)} backed by Backblaze`;
        this._bzExclItems = (r.drives || []).map((d) => ({
          name: d.drive, path: d.path, is_dir: true, status: "drive",
          backed_bytes: d.backed_bytes, backed_files: d.backed_files,
          excluded_bytes: d.excluded_bytes, excluded_files: d.excluded_files,
          total_bytes: (d.backed_bytes || 0) + (d.excluded_bytes || 0),
          kopia_backed_bytes: d.kopia_backed_bytes, kopia_backed_files: d.kopia_backed_files, kopia_repos: [],
        }));
        this._bzDrawExcl(true);
      }, "Loading Backblaze drives…");
    },
    async bzExclList(path) {
      const seq = ++this._bzExclSeq;
      this.bzExclPath = path;
      await this.wrap(async () => {
        const r = await getJson(`/api/backblaze/excluded/list?prefix=${encodeURIComponent(path)}`);
        if (seq !== this._bzExclSeq) return;
        this.bzExclMeta = `${r.count} items`;
        this._bzExclItems = r.items || [];
        this._bzDrawExcl(false);
      }, "Listing folder…");
    },
    bzExclRowOpen(row) {
      if (row.status === "drive") return this.bzExclList(row.path);
      if (!row.is_dir) return;
      this.bzExclList(this.bzExclPath.replace(/\\+$/, "") + "\\" + row.name);
    },
    bzExclUp() {
      const p = (this.bzExclPath || "").replace(/\\+$/, "");
      if (!p || /^[A-Za-z]:$/.test(p)) return this.bzExclDrives();
      const idx = p.lastIndexOf("\\");
      this.bzExclList(idx <= 2 ? p.slice(0, 2) + "\\" : p.slice(0, idx));
    },
    bzExclCrumbs() {
      const out = [{ label: "All drives", path: "" }];
      if (!this.bzExclPath) return out;
      const p = this.bzExclPath.replace(/\\+$/, ""); let acc = "";
      p.split("\\").forEach((seg, i) => { acc = i === 0 ? seg + "\\" : acc + (acc.endsWith("\\") ? "" : "\\") + seg; out.push({ label: seg, path: acc }); });
      return out;
    },
    bzSetExclFilter(f) { this.bzExclFilter = f; this._bzDrawExcl(this.bzExclPath === ""); },
    _bzDrawExcl(isDrives) {
      const f = this.bzExclFilter;
      const items = (this._bzExclItems || []).filter((it) => {
        if (f === "all" || it.status === "drive") return true;
        if (f === "backed") return it.status === "backed" || it.status === "mixed";
        return it.status !== "backed";
      });
      setTable("#bzExcludedTable", items, [
        { title: "Name", field: "name", widthGrow: 4, minWidth: 240, formatter: (c) => (c.getRow().getData().is_dir ? "📁 " : "📄 ") + c.getValue() },
        { title: "Status", field: "status", width: 105, formatter: (c) => statusPill(c.getValue()) },
        { title: "Backed", field: "backed_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        { title: "Excluded", field: "excluded_bytes", width: 105, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        { title: "Bkd #", field: "backed_files", width: 85, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
        { title: "Excl #", field: "excluded_files", width: 85, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
        { title: "Excluded by", field: "excl_rule", widthGrow: 2, minWidth: 150, formatter: (c) => c.getValue() || "" },
        { title: "Also in Kopia", field: "kopia_backed_bytes", width: 130, hozAlign: "right", sorter: "number", formatter: (c) => { const d = c.getRow().getData(); return (d.kopia_backed_files ? `${reposBadges(d.kopia_repos)} ${bzFmt(c.getValue())}` : "<span style='color:#94a3b8'>—</span>"); } },
      ], {
        persistSort: !isDrives,
        initialSort: isDrives ? [{ column: "name", dir: "asc" }] : [{ column: "excluded_bytes", dir: "desc" }],
        onRowDblClick: (_e, row) => this.bzExclRowOpen(row.getData()),
        rowMenu: [
          { label: "Open", action: (_e, row) => this.bzExclRowOpen(row.getData()) },
          { label: "Generate exclusion (this folder)", action: (_e, row) => this.bzOpenGen("folder", "\\" + row.getData().name + "\\") },
          { label: "Copy path", action: (_e, row) => this.copyText(row.getData().status === "drive" ? row.getData().path : this.bzExclPath.replace(/\\+$/, "") + "\\" + row.getData().name) },
        ],
      });
    },

    // ---- Exclusions + naming-gap + generate ----
    async bzLoadExclusions() {
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/exclusions");
        this.bzExclusions = r;
        setTable("#bzImpactTable", r.impact || [], [
          { title: "Rule", field: "rule", widthGrow: 4, minWidth: 280 },
          { title: "Files", field: "files", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Size", field: "bytes", width: 130, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], { initialSort: [{ column: "bytes", dir: "desc" }] });
      }, "Loading exclusions…");
    },
    async bzNamingGap() {
      if (!this.bzNamingQuery.trim()) { this.bzNamingRows = []; setTable("#bzNamingTable", [], []); return; }
      await this.wrap(async () => {
        const r = await getJson(`/api/backblaze/naming-gap?q=${encodeURIComponent(this.bzNamingQuery)}`);
        this.bzNamingRows = r.rows || [];
        setTable("#bzNamingTable", this.bzNamingRows, [
          { title: "Folder", field: "folder", widthGrow: 5, minWidth: 300 },
          { title: "Status", field: "status", width: 100, formatter: (c) => statusPill(c.getValue()) },
          { title: "Backed #", field: "backed_files", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Excluded #", field: "excluded_files", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Backed size", field: "backed_bytes", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], {
          initialSort: [{ column: "backed_bytes", dir: "desc" }],
          rowMenu: [
            { label: "Generate exclusion for this folder", action: (_e, row) => { const f = row.getData().folder; const rel = f.length > 2 && f[1] === ":" ? f.slice(2) : f; this.bzOpenGen("folder", rel.replace(/\\+$/, "") + "\\"); } },
            { label: "Find in search", action: (_e, row) => { this.query = row.getData().folder; this.active = "bz-search"; this.bzLoadSearch(); } },
          ],
        });
      }, "Hunting naming gaps…");
    },
    bzOpenGen(kind, value) { this.bzGen = { kind, value, result: null }; this.bzShowGen = true; this.bzDoGen(); },
    async bzDoGen() {
      if (!this.bzGen) return;
      const r = await getJson("/api/backblaze/exclusions/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: this.bzGen.kind, value: this.bzGen.value }) }).catch((e) => ({ error: e.message }));
      if (this.bzGen) this.bzGen.result = r;
    },

    // ---- Churn ----
    async bzLoadChurn() {
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/churn?top=200");
        this.bzChurnTotal = r.total_recent || 0;
        const rows = r.rows || [];
        setTable("#bzChurnTable", rows, [
          { title: "Folder (recently added)", field: "folder", widthGrow: 5, minWidth: 320 },
          { title: "New IDs", field: "count", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "% of recent", field: "pct", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => `${Number(c.getValue() || 0).toFixed(1)}%` },
          { title: "Drive", field: "drive", width: 70, formatter: (c) => c.getValue() ? c.getValue() + ":" : "" },
        ], {
          initialSort: [{ column: "count", dir: "desc" }],
          rowMenu: [
            { label: "Generate exclusion for this folder", action: (_e, row) => { const f = row.getData().folder; const rel = f.length > 2 && f[1] === ":" ? f.slice(2) : f; this.bzOpenGen("folder", rel.replace(/\\+$/, "") + "\\"); } },
            { label: "Find in search", action: (_e, row) => { this.query = row.getData().folder; this.active = "bz-search"; this.bzLoadSearch(); } },
          ],
        });
      }, "Loading churn…");
    },
    bzChurnAlerts() {
      // flag the top churners that are NOT one of the intentional Kopia repos (the "something new
      // started churning" signal). Heuristic only.
      return (this.bzChurnRows || []).filter((r) => r.pct >= 3 && !/kopia/i.test(r.folder)).slice(0, 4);
    },

    // ---- Queue / Sessions / Health ----
    async bzLoadQueue() {
      await this.wrap(async () => {
        const r = await getJson(`/api/backblaze/queue?depth=${this.bzQueueDepth || 1}`);
        this.bzQueue = r;
        setTable("#bzQueueTable", r.by_folder || [], [
          { title: "Folder", field: "folder", widthGrow: 5, minWidth: 320 },
          { title: "Files", field: "files", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Size", field: "bytes", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], { initialSort: [{ column: "bytes", dir: "desc" }] });
      }, "Loading upload queue…");
    },
    async bzLoadSessions() {
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/sessions?limit=300");
        this.bzSessions = r;
        setTable("#bzSessionsTable", r.sessions || [], [
          { title: "Time", field: "ts", width: 190, formatter: (c) => timeCell(c.getValue()) },
          { title: "Event", field: "kind", width: 140, formatter: (c) => c.getValue() === "session_end" ? "<span style='color:#16a34a'>backup ended</span>" : "backup started" },
          { title: "Files", field: "files", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() ? Number(c.getValue()).toLocaleString() : "" },
          { title: "Uploaded", field: "bytes", width: 130, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() ? bzFmt(c.getValue()) : "" },
        ], { initialSort: [{ column: "ts", dir: "desc" }] });
      }, "Loading sessions…");
    },
    async bzLoadHealth() {
      await this.wrap(async () => {
        this.bzHealth = await getJson("/api/backblaze/health");
        setTable("#bzHealthTable", this.bzHealth.drives || [], [
          { title: "Drive", field: "drive", width: 70, formatter: (c) => c.getValue() + ":" },
          { title: "Backed files", field: "backed_files", hozAlign: "right", width: 130, sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "BZ authoritative", field: "auth_files", hozAlign: "right", width: 140, sorter: "number", formatter: (c) => c.getValue() == null ? "—" : Number(c.getValue()).toLocaleString() },
          { title: "Diff", field: "diff", hozAlign: "right", width: 90, formatter: (c) => { const v = c.getValue(); return v == null ? "—" : (v === 0 ? "<span style='color:#16a34a'>+0</span>" : `<span style='color:#dc2626'>${v}</span>`); } },
        ], { initialSort: [{ column: "drive", dir: "asc" }] });
      }, "Running health check…");
    },

    // ---- file detail ----
    async bzOpenDetail(item) {
      this.bzDetail = { ...item, gen: null };
    },
    bzDetailRoot() {
      if (!this.bzDetail) return "";
      const f = this.bzDetail.full_path || "";
      return f.length > 2 && f[1] === ":" ? f.slice(2) : f;
    },
  };
};
