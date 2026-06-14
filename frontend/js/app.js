const fmtBytes = (value) => {
  const n = Number(value || 0);
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${n} B`;
};

const getJson = async (url, options = {}) => {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
  return data;
};

const setTable = (id, data, columns, opts = {}) => {
  const el = document.querySelector(id);
  if (!el) return null;
  const { onRowClick, ...rest } = opts;
  if (el._tabulator) {
    el._tabulator.setColumns(columns);
    el._tabulator.replaceData(data);
    return el._tabulator;
  }
  const table = new Tabulator(id, {
    data, columns,
    layout: "fitDataStretch",
    height: "100%",
    movableColumns: true,
    selectableRows: 1,
    placeholder: "No rows",
    ...rest,
  });
  if (onRowClick) table.on("rowClick", onRowClick);   // Tabulator 6.x: events via .on(), not constructor option
  el._tabulator = table;
  return table;
};

document.addEventListener("alpine:init", () => {
  Alpine.data("kopiaLens", () => ({
    repos: [], repo: "1",
    level: "sources",
    dark: false,
    indexStatus: {},
    sources: [],
    selectedSource: "",
    snapshots: [], selectedSnapshot: "",
    showChurn: false,
    // search / omnibox
    query: "", omniOpen: false, deletedOnly: false, overOneGb: false, omniMeta: "",
    // tree
    treeStack: [], treePrefix: "",
    restoreOpen: false, restoreSource: "", restoreDest: "",
    // simulate
    simOpen: false,
    policyKeys: ["latest", "hourly", "daily", "weekly", "monthly", "annual"],
    policy: { latest: 100, hourly: 96, daily: 30, weekly: 52, monthly: 36, annual: 5 },
    simResult: null,
    detail: null,
    dashboardMeta: "", busy: "", error: "",
    _poll: null,

    fmtB(v) { return fmtBytes(v); },

    get indexReady() { return (this.indexStatus.indexed_snapshots || 0) > 0; },
    get treeSegs() { return this.treeStack.map((s) => s.name); },
    get crumbs() {
      const c = [{ label: "Sources", go: () => this.goRoot() }];
      if (this.level === "source" || this.level === "snapshot") {
        c.push({ label: this.shortSource(this.selectedSource), go: () => { this.level = "source"; } });
      }
      if (this.level === "snapshot") {
        const s = this.snapshots.find((x) => x.id === this.selectedSnapshot);
        c.push({ label: s ? s.start_time.slice(0, 19) : this.selectedSnapshot.slice(0, 12), go: () => {} });
      }
      return c;
    },
    shortSource(s) { return s && s.includes(":") ? s.split(":").slice(1).join(":") : (s || ""); },

    async init() {
      this.dark = localStorage.getItem("kl-dark") === "1";
      document.documentElement.classList.toggle("dark", this.dark);
      await this.wrap(async () => {
        const data = await getJson("/api/repos");
        this.repos = data.repos;
        this.repo = data.settings?.last_repo || "1";
        await this.refreshBase();
        await this.loadDashboard();
      });
    },

    async wrap(fn, label = "") {
      this.error = ""; this.busy = label;
      try { return await fn(); }
      catch (err) { this.error = err.message || String(err); }
      finally { this.busy = ""; }
    },

    toggleDark() {
      this.dark = !this.dark;
      document.documentElement.classList.toggle("dark", this.dark);
      localStorage.setItem("kl-dark", this.dark ? "1" : "0");
    },

    goRoot() { this.level = "sources"; this.loadDashboard(); },

    indexLine() {
      const s = this.indexStatus;
      if (s.building) return `Indexing ${s.indexed_snapshots || 0}/${s.snapshots || "?"}...`;
      if (s.snapshots === undefined) return "Index: unknown";
      return `Index ${s.indexed_snapshots}/${s.snapshots}`;
    },

    async repoChanged() {
      await this.refreshBase();
      this.goRoot();
    },

    async refreshBase() {
      const [status, sources] = await Promise.all([
        getJson(`/api/index/status?repo=${encodeURIComponent(this.repo)}`),
        getJson(`/api/sources?repo=${encodeURIComponent(this.repo)}`),
      ]);
      this.indexStatus = status;
      this.sources = sources.sources || [];
    },

    async buildIndex() {
      await this.wrap(async () => {
        await getJson(`/api/index/build?repo=${encodeURIComponent(this.repo)}`, { method: "POST" });
        this.pollIndex();
      }, "Index build started...");
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

    async loadDashboard(refresh = false) {
      await this.wrap(async () => {
        const data = await getJson(`/api/dashboard?repo=${encodeURIComponent(this.repo)}&mode=all&refresh=${refresh}`);
        this.dashboardMeta = `true physical ${fmtBytes(data.union_total)} · ${Number(data.elapsed || 0).toFixed(1)}s`;
        setTable("#dashboardTable", data.rows || [], [
          { title: "#", field: "rank", width: 48 },
          { title: "Source", field: "source", widthGrow: 4, minWidth: 280, formatter: (c) => this.shortSource(c.getValue()) },
          { title: "Snaps", field: "snaps", width: 70, hozAlign: "right" },
          { title: "Physical", field: "physical", width: 95, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Accum", field: "accum", width: 75, formatter: (c) => `${Number(c.getValue()).toFixed(1)}x`, hozAlign: "right" },
          { title: "%Repo", field: "repo_pct", width: 70, formatter: (c) => `${Number(c.getValue()).toFixed(1)}%`, hozAlign: "right" },
          { title: "Freeable", field: "freeable", width: 95, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Dedup", field: "dedup", width: 70, formatter: (c) => `${Number(c.getValue()).toFixed(0)}x`, hozAlign: "right" },
          { title: "Latest", field: "logical_latest", width: 90, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Pins", field: "pins", width: 56, hozAlign: "right" },
        ], { onRowClick: (_e, row) => this.openSource(row.getData().source) });
      }, "Loading dashboard...");
    },

    openSource(source) {
      this.selectedSource = source;
      this.level = "source";
      this.simResult = null; this.simOpen = false;
      this.loadSourceDetail();
    },

    async loadSourceDetail() {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        const data = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`);
        this.snapshots = data.snapshots || [];
        let churnMap = {};
        const cols = [
          { title: "Start", field: "start_time", width: 175, formatter: (c) => (c.getValue() || "").slice(0, 19) },
          { title: "Logical", field: "logical", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", width: 90, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Dirs", field: "dirs", width: 80, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Retention", field: "retention", widthGrow: 1, minWidth: 150, formatter: (c) => (c.getValue() || []).map((r) => `<span class="pill" style="background:#dbeafe;color:#1e40af">${r}</span>`).join("") },
          { title: "Pins", field: "pins", formatter: (c) => (c.getValue() || []).map((p) => `<span class="pill" style="background:#fef3c7;color:#92400e">${p}</span>`).join("") },
          { title: "ID", field: "id", widthGrow: 2, minWidth: 200 },
        ];
        if (this.showChurn) {
          const churn = await getJson(`/api/snapshots/churn?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}&recent=100000`);
          (churn.rows || []).forEach((r) => { churnMap[r.id] = r.incremental_physical; });
          this.snapshots.forEach((s) => { s.incr = churnMap[s.id] || 0; });
          cols.splice(4, 0, { title: "Churn (new)", field: "incr", width: 105, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" });
        }
        setTable("#snapshotsTable", this.snapshots, cols, { onRowClick: (_e, row) => this.openSnapshot(row.getData()) });
      }, this.showChurn ? "Computing churn (can take a while)..." : "Loading snapshots...");
    },

    openSnapshot(snap) {
      this.selectedSnapshot = snap.id;
      this.level = "snapshot";
      this.treeStack = []; this.treePrefix = "";
      this.restoreOpen = false; this.restoreSource = ""; this.restoreDest = "";
      this.loadTree();
    },

    async loadTree() {
      await this.wrap(async () => {
        const url = `/api/tree?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}&snapshot_id=${encodeURIComponent(this.selectedSnapshot)}&prefix=${encodeURIComponent(this.treePrefix)}`;
        const data = await getJson(url);
        if (!(data.children || []).length && !this.indexReady) {
          this.error = "This snapshot is not indexed yet. Click Index (top right) to enable the tree/search.";
        }
        setTable("#treeTable", data.children || [], [
          { title: "Name", field: "name", widthGrow: 4, minWidth: 280, formatter: (c) => `${c.getData().type === "folder" ? "📁 " : ""}${c.getValue()}` },
          { title: "Size", field: "size", width: 110, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", width: 90, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Type", field: "type", width: 90 },
        ], {
          onRowClick: (_e, row) => {
            const d = row.getData();
            if (d.type === "folder") { this.treeStack.push({ name: d.name, path: d.path }); this.treePrefix = d.path; this.loadTree(); }
          },
        });
      }, "Loading tree...");
    },

    treeGo(i) {
      if (i < 0) { this.treeStack = []; this.treePrefix = ""; }
      else { this.treeStack = this.treeStack.slice(0, i + 1); this.treePrefix = this.treeStack[i].path; }
      this.loadTree();
    },

    async runOmnibox() {
      if (!this.query) { this.omniOpen = false; return; }
      this.omniOpen = true;
      await this.wrap(async () => {
        const over = this.overOneGb ? 1 : 0;
        const url = `/api/search?repo=${encodeURIComponent(this.repo)}&q=${encodeURIComponent(this.query)}&deleted_only=${this.deletedOnly}&over_gb=${over}&limit=300`;
        const data = await getJson(url);
        this.omniMeta = `${(data.results || []).length} results`;
        setTable("#omniTable", data.results || [], [
          { title: "Path", field: "full_path", widthGrow: 4, minWidth: 320, formatter: (c) => this.shortSource(c.getValue()) },
          { title: "Size", field: "size", width: 95, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Vers", field: "versions", width: 60, hozAlign: "right" },
          { title: "On disk", field: "in_latest", width: 80, formatter: (c) => c.getValue() ? "yes" : "<span style='color:#dc2626'>deleted</span>" },
        ], { height: "55vh", onRowClick: (_e, row) => this.openFileDetail(row.getData()) });
      }, "Searching...");
    },

    async openFileDetail(item) {
      await this.wrap(async () => {
        const params = `repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`;
        const [versions, purge] = await Promise.all([
          getJson(`/api/search/versions?${params}`),
          getJson(`/api/actions/purge-plan?${params}`),
        ]);
        const vlines = (versions.versions || []).map((v) => `  ${(v.start_time || "").toString().slice(0, 19)}   ${fmtBytes(v.size)}   ${v.snapshot_id}`).join("\n");
        const plines = (purge.commands || []).map((c) => "  " + (Array.isArray(c) ? c.join(" ") : c)).join("\n");
        this.detail = {
          title: item.full_path,
          body: `size ${fmtBytes(item.size)} · versions ${item.versions} · on disk: ${item.in_latest ? "yes" : "DELETED"}\n\n`
            + `VERSIONS (time-travel):\n${vlines || "  (none)"}\n\n`
            + `PURGE PLAN (commands you run yourself - app never executes):\n${plines || "  (none)"}\n`,
        };
      });
    },

    async runSimulate() {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        const r = await getJson("/api/simulate", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, source: this.selectedSource, mode: "all", policy: this.policy }),
        });
        this.simResult = { reclaim: r.reclaim, keep: r.keep_snapshots, deleted: r.delete_snapshots, pinned: r.pinned_by_other_sources };
      }, "Simulating retention...");
    },

    async mountCurrent() {
      await this.wrap(async () => {
        const mp = prompt("Mount this snapshot at (empty folder / drive letter):", "");
        if (!mp) return;
        const r = await getJson("/api/actions/mount", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, snapshot_id: this.selectedSnapshot, mountpoint: mp }),
        });
        this.detail = { title: "Mount started", body: `pid ${r.pid}\n${(r.command || []).join(" ")}` };
      }, "Starting mount...");
    },

    async restorePreview() {
      await this.wrap(async () => {
        const r = await getJson("/api/actions/restore-preview", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, source: this.restoreSource, destination: this.restoreDest }),
        });
        this.detail = { title: "Restore command (preview - run it yourself)", body: (r.command || []).join(" ") };
      });
    },
  }));
});
