// Backblaze view: state, lifecycle + file detail. All keys are bz-prefixed and BZ hits ONLY
// /api/backblaze/* (never the kopia/DuckDB routes). Reuses the shared helpers from util.js
// (fmtBytes, humanTime, timeCell, sourcePath, getJson, setTable) plus the bz-local helpers below.
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
    return isNaN(dt.getTime()) ? s : fmtDate(dt);
  };

const bzIndexPart = {
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
      // Remaining to upload - the "is my backup done" number (2026-09-13). The backed/on-disk
      // counts live in the tooltip (bzStatusTitle); they are identity stats, not actions.
      const r = s.remaining || {};
      if (r.remaining_files == null && r.remaining_bytes == null) return "BZ";
      if (!Number(r.remaining_files) && !Number(r.remaining_bytes)) return "Nothing to upload";
      return `To upload: ${fmtBytes(Number(r.remaining_bytes || 0))} · ${countOf(r.remaining_files, "file")} left`;
    },
    bzStatusTitle() {
      const s = this.bzStatus;
      const parts = [];
      if (s.built) {
        parts.push(`${Number(s.backed_files || 0).toLocaleString(NUM_LOCALE)} backed`);
        parts.push(`${Number(s.total_files || 0).toLocaleString(NUM_LOCALE)} on disk`);
      }
      const ts = Number(s.last_check_ts || 0);
      if (ts) {
        const mins = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
        parts.push("checked " + (mins < 60 ? mins + "m ago" : mins < 1440 ? Math.round(mins / 60) + "h ago" : Math.round(mins / 1440) + "d ago"));
      }
      return parts.join(" · ");
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
              this.bzRefreshResult = "BZ refresh completed " + humanTime(Date.now());
            }
            await this.bzActivate();
          }
        } catch (e) { /* transient fetch error - ignore and let the next 3s tick retry */ }
      }, 3000);
    },

    // ---- paths: the data's own separator ("\\" Windows, "/" Mac) and its volumes ----
    bzSep() { return this.bzStatus.sep || "\\"; },
    bzVol(key) { return (this.bzStatus.drives || []).find((d) => d.drive === key) || null; },
    bzLabel(key) { const v = this.bzVol(key); return v ? v.label : (key ? key + ":" : ""); },
    bzRoot(key) { const v = this.bzVol(key); return v ? v.root : key + ":\\"; },
    bzDriveWidth() { return this.bzSep() === "/" ? 170 : 70; },
    bzCross() { return this.bzStatus.this_computer !== false; },   // another computer's data: no Kopia cross-check
    bzVolOf(path) {   // the volume a path is on (longest matching root)
      const p = String(path || "").toLowerCase(); let best = null, len = -1;
      for (const d of (this.bzStatus.drives || [])) {
        const r = String(d.root || "").toLowerCase(); const bare = r.length > 1 ? r.slice(0, -1) : r;
        if ((p.startsWith(r) || p === bare) && r.length > len) { best = d.drive; len = r.length; }
      }
      if (best === null && this.bzSep() === "\\" && p.length > 1 && p[1] === ":") best = p[0].toUpperCase();
      return best;
    },
    bzJoin(base, name) { const s = this.bzSep(); let b = base || ""; while (b.endsWith(s)) b = b.slice(0, -1); return b + s + name; },
    bzCrumbs(path) {   // [{label, prefix}] from the volume root down
      const s = this.bzSep(); const out = [];
      if (!path) return out;
      const key = this.bzVolOf(path);
      const root = key !== null ? this.bzRoot(key) : path.split(s)[0] + s;
      out.push({ label: key !== null ? this.bzLabel(key) : (path.split(s)[0] || s), prefix: root });
      let acc = root;
      path.slice(Math.min(root.length, path.length)).split(s).filter(Boolean).forEach((seg) => {
        acc = acc + (acc.endsWith(s) ? "" : s) + seg; out.push({ label: seg, prefix: acc });
      });
      return out;
    },
    bzGenFolder(full) {   // Windows folder rules are relative to the drive; a Mac's are full paths
      const s = this.bzSep(); let f = full || "";
      if (s === "\\" && f.length > 2 && f[1] === ":") f = f.slice(2);
      while (f.endsWith(s)) f = f.slice(0, -1);
      this.bzOpenGen("folder", f + s);
    },
    bzGenName(name) {
      if (this.bzSep() === "/") return this.bzOpenGen("editable_contains", "/" + name + "/");
      this.bzOpenGen("folder", "\\" + name + "\\");
    },

    // ---- file detail ----
    async bzOpenDetail(item) {
      this.bzDetail = { ...item, gen: null };
    },
    bzDetailRoot() {
      if (!this.bzDetail) return "";
      const f = this.bzDetail.full_path || "";
      return this.bzSep() === "\\" && f.length > 2 && f[1] === ":" ? f.slice(2) : f;
    },
};
