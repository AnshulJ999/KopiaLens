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
  // Backblaze defines categories internally (no on-disk map), so these are sensible hardcoded sets.
  const CAT_EXT = {
    video: "mp4,mkv,mov,avi,wmv,flv,m4v,mpg,mpeg,webm,3gp,m2ts,mts,vob,ogv,divx,rm,rmvb,asf,ts",
    music: "mp3,flac,wav,aac,m4a,ogg,wma,aiff,aif,alac,ape,opus,mid,midi,amr",
    pict: "jpg,jpeg,png,gif,bmp,tiff,tif,heic,heif,webp,raw,cr2,cr3,nef,arw,dng,orf,rw2,psd,svg,ico",
    docs: "pdf,doc,docx,txt,rtf,odt,pages,md,tex,wpd,epub,mobi,azw3",
    spreadsheets: "xls,xlsx,csv,ods,numbers,tsv",
    presentations: "ppt,pptx,odp,key",
    zips: "zip,rar,7z,gz,tar,bz2,xz,tgz,iso,cab,arj,z",
    email: "eml,msg,pst,ost,mbox",
  };
  const fmtBzDate = (s) => {  // 'YYYYMMDD' (or longer) -> 'Apr 20, 2026'
    s = String(s || "");
    const m = s.match(/^(\d{4})(\d{2})(\d{2})/);
    if (!m) return s;
    const dt = new Date(`${m[1]}-${m[2]}-${m[3]}T00:00:00`);
    return isNaN(dt.getTime()) ? s : dt.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };

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
    bzChurnRows: [], bzChurnTotal: 0, bzChurnActiveOnly: true, _bzChurnAll: [],
    bzChurnDepth: 2, bzChurnCollapse: true, bzChurnDrive: "", bzImportMsg: "",
    bzSearchDrive: "", bzSearchExt: "", bzSearchExtExclude: "", bzSearchStatus: "all",
    bzSearchSort: "size", bzSearchDir: "desc", bzSearchTotal: 0, bzSearchPage: 1, bzSearchPageSize: "500",
    bzGroupByFolder: false, bzGroupDepth: 2, bzSearchMeta: "",
    bzTreeDrive: "", bzTreePrefix: "",
    bzExclPath: "", bzExclFilter: "all", bzExclMeta: "", _bzExclItems: [], _bzExclSeq: 0,
    _bzSearchSeq: 0, _bzTreeSeq: 0,
    bzExclusions: null, bzNamingQuery: "", bzNamingRows: [],
    bzGen: null, bzShowGen: false, bzRefreshResult: "",
    bzQueue: null, bzQueueDepth: 1, bzQueueCollapse: true, bzSessions: null, bzHealth: null,
    bzDetail: null, bzDrives: [], _bzPoll: null,

    // ---- lifecycle ----
    bzApplySettings(kl) {  // restore persisted BZ prefs (called from app.js init with the kopialens settings)
      if (kl.bz_tree_drive) this.bzTreeDrive = kl.bz_tree_drive;
      if (kl.bz_queue_depth) this.bzQueueDepth = Number(kl.bz_queue_depth);
      if (kl.bz_churn_depth) this.bzChurnDepth = Number(kl.bz_churn_depth);
      if (kl.bz_churn_collapse !== undefined) this.bzChurnCollapse = !!kl.bz_churn_collapse;
      if (kl.bz_queue_collapse !== undefined) this.bzQueueCollapse = !!kl.bz_queue_collapse;
    },
    async bzInit() {
      await this.wrap(async () => {
        await this.bzRefreshStatus();
        if (this.bzStatus.building) this.bzPollRefresh();  // a bg-cycle refresh was already in flight
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
      // Non-blocking: the refresh runs in a subprocess worker (~4 min), so this only starts it and
      // returns immediately - bzPollRefresh() watches /status and reloads the active tab when done.
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/refresh", { method: "POST" });
        await this.bzRefreshStatus();
        if (r && r.started !== false) this.bzPollRefresh();
      }, "Starting Backblaze refresh…");
    },
    bzPollRefresh() {
      if (this._bzPoll) clearInterval(this._bzPoll);
      this._bzPoll = setInterval(async () => {
        try {
          await this.bzRefreshStatus();
          if (!this.bzStatus.building) {
            clearInterval(this._bzPoll); this._bzPoll = null;
            // Own status slot, NOT this.error: wrap() clears this.error on entry, so a failure
            // message set here was wiped by the very next tab activation. Stale data still beats
            // no data, but the user must see that it happened.
            if (this.bzStatus.last_error) {
              this.bzRefreshResult = "Last BZ refresh FAILED: " + this.bzStatus.last_error;
            } else {
              this.bzRefreshResult = "BZ refresh completed " + new Date().toLocaleTimeString();
            }
            await this.bzActivate();
          }
        } catch (e) { /* transient fetch error - ignore and let the next 3s tick retry */ }
      }, 3000);
    },

    // ---- Dashboard ----
    async bzLoadDashboard() {
      await this.wrap(async () => {
        const [ov, st, ch] = await Promise.all([
          getJson("/api/backblaze/overview"),
          getJson("/api/backblaze/stats"),
          getJson("/api/backblaze/churn?top=40"),
        ]);
        this.bzOverview = ov; this.bzStats = st;
        this._bzChurnAll = ch.rows || [];
        this.bzChurnRows = this._bzChurnAll.filter((r) => r.status !== "gone").slice(0, 10);  // drop deleted folders from the at-a-glance panel
        this.bzChurnTotal = ch.total_recent || 0;
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
    bzCatSearch(cat) {  // click a category -> Search pre-filtered by its extensions ("other" = exclude all known)
      this.bzSearchExt = ""; this.bzSearchExtExclude = ""; this.bzSearchStatus = "all";
      this.bzSearchDrive = ""; this.query = ""; this.bzGroupByFolder = false;
      if (cat === "other") this.bzSearchExtExclude = Object.values(CAT_EXT).join(",");
      else if (CAT_EXT[cat]) this.bzSearchExt = CAT_EXT[cat];
      this.active = "bz-search"; this.bzLoadSearch();
    },

    // ---- Search (FTS5) ----
    bzSearchUrl(extra = {}) {
      const p = new URLSearchParams({ q: this.query || "", sort: this.bzSearchSort, direction: this.bzSearchDir, status: this.bzSearchStatus });
      if (this.bzSearchDrive) p.set("drive", this.bzSearchDrive);
      if (this.bzSearchExt) p.set("ext", this.bzSearchExt);
      if (this.bzSearchExtExclude) p.set("ext_exclude", this.bzSearchExtExclude);
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
      const seq = ++this._bzSearchSeq;
      await this.wrap(async () => {
        const size = this.bzSearchPageSize === "Infinite" ? 100000 : (parseInt(this.bzSearchPageSize, 10) || 500);
        const offset = this.bzSearchPageSize === "Infinite" ? 0 : (this.bzSearchPage - 1) * size;
        const p = this.bzSearchUrl({ limit: size, offset });
        const data = await getJson(`/api/backblaze/search?${p.toString()}`);
        if (seq !== this._bzSearchSeq) return;  // a newer search superseded this one
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
      const seq = ++this._bzSearchSeq;  // shared with bzLoadSearch: the two views replace each other
      await this.wrap(async () => {
        const p = this.bzSearchUrl({ depth: this.bzGroupDepth, status: this.bzSearchStatus, top: 500 });
        const data = await getJson(`/api/backblaze/group-by-folder?${p.toString()}`);
        if (seq !== this._bzSearchSeq) return;  // a newer search/group superseded this one
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
      if (this.bzTreeDrive !== "*ALL*" && (!this.bzTreePrefix || this.bzTreePrefix[0].toUpperCase() !== this.bzTreeDrive)) this.bzTreePrefix = this.bzTreeDrive + ":\\";
      return this.bzLoadTree();
    },
    bzTreeChanged() { this.bzTreePrefix = this.bzTreeDrive === "*ALL*" ? "" : this.bzTreeDrive + ":\\"; this.klSet({ bz_tree_drive: this.bzTreeDrive }); this.bzLoadTree(); },
    bzTreeNav(prefix) { this.bzTreePrefix = prefix; this.bzLoadTree(); },
    bzTreeCrumbs() {
      const out = [];
      if (this.bzTreeDrive === "*ALL*") out.push({ label: "All drives", prefix: "" });
      const p = (this.bzTreePrefix || "").replace(/\\+$/, "");
      if (p) {
        let acc = "";
        p.split("\\").forEach((seg, i) => { acc = i === 0 ? seg + "\\" : acc + (acc.endsWith("\\") ? "" : "\\") + seg; out.push({ label: seg || acc, prefix: acc }); });
      }
      return out;
    },
    bzTreeUp() { const c = this.bzTreeCrumbs(); if (c.length > 1) this.bzTreeNav(c[c.length - 2].prefix); },
    async bzLoadTree() {
      const seq = ++this._bzTreeSeq;
      await this.wrap(async () => {
        const atRoot = this.bzTreeDrive === "*ALL*" && !this.bzTreePrefix;
        let kids;
        if (atRoot) {  // All-drives top level = the drive roots (with real per-drive bytes from overview)
          if (!this.bzOverview) this.bzOverview = await getJson("/api/backblaze/overview").catch(() => null);
          const ov = {}; ((this.bzOverview && this.bzOverview.rows) || []).forEach((r) => { ov[r.drive] = r; });
          kids = (this.bzStatus.drives || []).map((d) => {
            const o = ov[d.drive] || {};
            return { name: d.drive + ":", is_dir: true, status: "drive", _root: d.drive + ":\\", excl_rule: "",
                     backed_bytes: o.backed_bytes || 0, excluded_bytes: o.excluded_bytes || 0, total_files: d.total };
          });
        } else {
          const data = await getJson(`/api/backblaze/folder?prefix=${encodeURIComponent(this.bzTreePrefix)}`);
          kids = data.children || [];
        }
        if (seq !== this._bzTreeSeq) return;  // a newer navigation superseded this one
        const tot = kids.reduce((a, c) => a + ((c.backed_bytes || 0) + (c.excluded_bytes || 0)), 0) || 1;
        kids.forEach((c) => { c.total_bytes = (c.backed_bytes || 0) + (c.excluded_bytes || 0); c.pct = 100 * c.total_bytes / tot; });
        setTable("#bzTreeTable", kids, [
          { title: "Name", field: "name", widthGrow: 5, minWidth: 280, formatter: (c) => `${c.getRow().getData().is_dir ? "📁 " : "📄 "}${c.getValue()}` },
          { title: "Status", field: "status", width: 90, formatter: (c) => statusPill(c.getValue()) },
          { title: "Total", field: "total_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "%", field: "pct", width: 68, hozAlign: "right", sorter: "number", formatter: (c) => `${Number(c.getValue() || 0).toFixed(1)}%` },
          { title: "Backed", field: "backed_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Excluded", field: "excluded_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Files", field: "total_files", width: 85, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Excluded by", field: "excl_rule", widthGrow: 2, minWidth: 140, formatter: (c) => c.getValue() || "" },
        ], {
          initialSort: [{ column: atRoot ? "name" : "total_bytes", dir: atRoot ? "asc" : "desc" }],
          onRowDblClick: (_e, row) => { const d = row.getData(); if (d._root) return this.bzTreeNav(d._root); if (d.is_dir) this.bzTreeNav(this.bzTreePrefix.replace(/\\+$/, "") + "\\" + d.name); },
          rowMenu: [
            { label: "Open / drill in", action: (_e, row) => { const d = row.getData(); if (d._root) return this.bzTreeNav(d._root); if (d.is_dir) this.bzTreeNav(this.bzTreePrefix.replace(/\\+$/, "") + "\\" + d.name); } },
            { label: "Generate exclusion (this folder)", action: (_e, row) => this.bzOpenGen("folder", "\\" + row.getData().name + "\\") },
            { label: "Find in search", action: (_e, row) => { this.query = row.getData().name; this.active = "bz-search"; this.bzLoadSearch(); } },
            { label: "Copy path", action: (_e, row) => this.copyText(row.getData()._root || (this.bzTreePrefix.replace(/\\+$/, "") + "\\" + row.getData().name)) },
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
        // Render BZ-native instantly; the slower Kopia cross-coverage fills in lazily (5B).
        // kopia_backed_bytes=null renders as "…" until the second request patches it.
        this._bzExclItems = (r.items || []).map((it) => ({ ...it, kopia_backed_bytes: null, kopia_backed_files: 0, kopia_repos: [] }));
        this._bzDrawExcl(false);
        getJson(`/api/backblaze/excluded/kopia-coverage?prefix=${encodeURIComponent(path)}`).then((kr) => {
          if (seq !== this._bzExclSeq) return;  // a newer navigation superseded this fetch
          const cov = kr.coverage || {};
          this._bzExclItems.forEach((it) => {
            const c = cov[it.name.toLowerCase()];
            if (c) { it.kopia_backed_bytes = c.bytes; it.kopia_backed_files = c.files; it.kopia_repos = c.repos; }
            else { it.kopia_backed_bytes = 0; }
          });
          this._bzDrawExcl(false);
        }).catch(() => {});
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
        { title: "Also in Kopia", field: "kopia_backed_bytes", width: 130, hozAlign: "right", sorter: "number", formatter: (c) => { const d = c.getRow().getData(); const v = c.getValue(); if (v === null || v === undefined) return "<span style='color:#cbd5e1'>…</span>"; return d.kopia_backed_files ? `${reposBadges(d.kopia_repos)} ${bzFmt(v)}` : "<span style='color:#94a3b8'>—</span>"; } },
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
        const r = await getJson(`/api/backblaze/churn?top=200&depth=${this.bzChurnDepth}&collapse=${this.bzChurnCollapse ? 1 : 0}`);
        this.bzChurnTotal = r.total_recent || 0;
        this._bzChurnAll = r.rows || [];
        this._bzDrawChurn();
      }, "Loading churn…");
    },
    bzChurnChanged() { this.klSet({ bz_churn_depth: this.bzChurnDepth, bz_churn_collapse: this.bzChurnCollapse }); this.bzLoadChurn(); },
    bzToggleChurnActive() { this.bzChurnActiveOnly = !this.bzChurnActiveOnly; this._bzDrawChurn(); },
    _bzDrawChurn() {
      const all = this._bzChurnAll || [];
      let rows = this.bzChurnActiveOnly ? all.filter((r) => r.status === "active") : all;
      if (this.bzChurnDrive) rows = rows.filter((r) => r.drive === this.bzChurnDrive);
      const cs = (s) => { const m = { active: ["#dcfce7", "#166534"], excluded: ["#fef9c3", "#854d0e"], gone: ["#e2e8f0", "#475569"] }[s] || ["#e2e8f0", "#475569"]; return `<span class="pill" style="background:${m[0]};color:${m[1]}">${s}</span>`; };
      setTable("#bzChurnTable", rows, [
        { title: "Folder (recently added)", field: "folder", widthGrow: 5, minWidth: 270 },
        { title: "Status", field: "status", width: 88, formatter: (c) => cs(c.getValue()) },
        { title: "New IDs", field: "count", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
        { title: "% recent", field: "pct", width: 85, hozAlign: "right", sorter: "number", formatter: (c) => `${Number(c.getValue() || 0).toFixed(1)}%` },
        { title: "Backed now", field: "backed_now", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
        { title: "Size", field: "bytes_now", width: 92, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        { title: "Δ vs first", field: "delta", width: 92, hozAlign: "right", sorter: "number", formatter: (c) => { const v = c.getValue(); if (v === null || v === undefined) return "<span style='color:#cbd5e1'>—</span>"; const col = v > 0 ? "#dc2626" : (v < 0 ? "#16a34a" : "#64748b"); return `<span style="color:${col}">${v > 0 ? "+" : ""}${Number(v).toLocaleString()}</span>`; } },
        { title: "Drive", field: "drive", width: 58, formatter: (c) => c.getValue() ? c.getValue() + ":" : "" },
      ], {
        initialSort: [{ column: "count", dir: "desc" }],
        rowMenu: [
          { label: "Generate exclusion for this folder", action: (_e, row) => { const f = row.getData().folder; const rel = f.length > 2 && f[1] === ":" ? f.slice(2) : f; this.bzOpenGen("folder", rel.replace(/\\+$/, "") + "\\"); } },
          { label: "Find in search", action: (_e, row) => { this.query = row.getData().folder; this.active = "bz-search"; this.bzLoadSearch(); } },
        ],
      });
    },
    bzChurnAlerts() {
      // active churners (still backed) that aren't an intentional Kopia repo = "something new started churning".
      return (this._bzChurnAll || []).filter((r) => r.status === "active" && r.pct >= 3 && !/kopia/i.test(r.folder)).slice(0, 4);
    },

    // ---- Queue / Sessions / Health ----
    bzQueueChanged() { this.klSet({ bz_queue_depth: this.bzQueueDepth, bz_queue_collapse: this.bzQueueCollapse }); this.bzLoadQueue(); },
    async bzLoadQueue() {
      await this.wrap(async () => {
        const r = await getJson(`/api/backblaze/queue?depth=${this.bzQueueDepth || 1}&collapse=${this.bzQueueCollapse ? 1 : 0}`);
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
        const r = await getJson("/api/backblaze/sessions?limit=500");
        this.bzSessions = r;
        // explicit heights so the stacked tables don't collapse/overlap inside the scrolling section
        setTable("#bzSessionsTable", r.sessions || [], [
          { title: "Time", field: "ts", width: 190, formatter: (c) => timeCell(c.getValue()) },
          { title: "Event", field: "kind", width: 130, formatter: (c) => c.getValue() === "session_end" ? "<span style='color:#16a34a'>backup ended</span>" : "backup started" },
          { title: "Files", field: "files", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() ? Number(c.getValue()).toLocaleString() : "" },
          { title: "Uploaded", field: "bytes", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() ? bzFmt(c.getValue()) : "" },
        ], { initialSort: [{ column: "ts", dir: "desc" }], height: "15rem" });
        setTable("#bzBackupDaysTable", r.backup_days || [], [
          { title: "Day", field: "day", width: 150 },
          { title: "Files backed up", field: "files", width: 150, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Uploaded", field: "bytes", width: 140, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], { initialSort: [{ column: "day", dir: "desc" }], height: "17rem" });
        setTable("#bzUploadsTable", r.uploads || [], [
          { title: "Date", field: "date", width: 170, formatter: (c) => fmtBzDate(c.getValue()) },
          { title: "Uploaded OK", field: "success", width: 130, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Failed", field: "fail", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => { const v = Number(c.getValue() || 0); return v ? `<span style='color:#dc2626'>${v.toLocaleString()}</span>` : "0"; } },
        ], { initialSort: [{ column: "date", dir: "desc" }], height: "17rem" });
      }, "Loading sessions…");
    },
    async bzImportArchive() {
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/import-archive", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        this.bzImportMsg = r.ok ? `Imported ${r.imported} new event(s) · ${r.total} total archived.` : `Import failed: ${r.reason || "unknown"}`;
        await this.bzLoadSessions();
      }, "Importing archived history…");
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
