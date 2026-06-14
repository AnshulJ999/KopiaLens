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
  if (el._tabulator) {
    el._tabulator.setColumns(columns);
    el._tabulator.setData(data);
    return el._tabulator;
  }
  return new Tabulator(id, {
    data,
    columns,
    layout: "fitDataStretch",
    height: opts.height || "100%",
    movableColumns: true,
    selectableRows: 1,
    placeholder: "No rows",
    ...opts,
  });
};

document.addEventListener("alpine:init", () => {
  Alpine.data("kopiaLens", () => ({
    repos: [],
    repo: "1",
    mode: "all",
    tabs: [
      { id: "dashboard", label: "Dashboard" },
      { id: "snapshots", label: "Snapshots" },
      { id: "tree", label: "Tree" },
      { id: "search", label: "Search" },
      { id: "simulate", label: "Simulate" },
      { id: "actions", label: "Actions" },
    ],
    active: "dashboard",
    indexStatus: {},
    sources: [],
    selectedSource: "",
    snapshots: [],
    selectedSnapshot: "",
    treePrefix: "",
    query: "",
    deletedOnly: false,
    overOneGb: false,
    policyKeys: ["latest", "hourly", "daily", "weekly", "monthly", "annual"],
    policy: { latest: 100, hourly: 96, daily: 30, weekly: 52, monthly: 36, annual: 5 },
    simText: "",
    actionText: "",
    mountSnapshot: "",
    mountPoint: "",
    restoreSource: "",
    restoreDest: "",
    dashboardMeta: "",
    busy: "",
    error: "",

    async init() {
      await this.wrap(async () => {
        const data = await getJson("/api/repos");
        this.repos = data.repos;
        this.repo = data.settings?.last_repo || "1";
        this.mode = data.settings?.laptop_mode || "all";
        await this.refreshBase();
        await this.loadDashboard();
      });
    },

    async wrap(fn, label = "") {
      this.error = "";
      this.busy = label;
      try {
        return await fn();
      } catch (err) {
        this.error = err.message || String(err);
      } finally {
        this.busy = "";
      }
    },

    indexLine() {
      if (!this.indexStatus.snapshots && this.indexStatus.snapshots !== 0) return "Index: unknown";
      return `Index: ${this.indexStatus.indexed_snapshots}/${this.indexStatus.snapshots} snaps, ${fmtBytes(this.indexStatus.summary?.logical_sum || 0)}`;
    },

    async repoChanged() {
      await this.refreshBase();
      await this.activateTab();
    },

    async refreshBase() {
      const [status, sources] = await Promise.all([
        getJson(`/api/index/status?repo=${encodeURIComponent(this.repo)}`),
        getJson(`/api/sources?repo=${encodeURIComponent(this.repo)}`),
      ]);
      this.indexStatus = status;
      this.sources = sources.sources || [];
      this.selectedSource = this.sources.includes(this.selectedSource) ? this.selectedSource : (this.sources[0] || "");
    },

    async activateTab() {
      if (this.active === "dashboard") return this.loadDashboard();
      if (this.active === "snapshots") return this.loadSnapshots();
      if (this.active === "tree") return this.loadSnapshots();
      if (this.active === "search") return this.loadSearch();
    },

    async buildIndex() {
      await this.wrap(async () => {
        const data = await getJson(`/api/index/build?repo=${encodeURIComponent(this.repo)}`, { method: "POST" });
        this.actionText = JSON.stringify(data, null, 2);
        await this.refreshBase();
      }, "Indexing snapshots...");
    },

    async loadDashboard(refresh = false) {
      await this.wrap(async () => {
        const data = await getJson(`/api/dashboard?repo=${encodeURIComponent(this.repo)}&mode=${encodeURIComponent(this.mode)}&refresh=${refresh}`);
        this.dashboardMeta = `true physical ${fmtBytes(data.union_total)} | ${Number(data.elapsed || 0).toFixed(1)}s`;
        setTable("#dashboardTable", data.rows || [], [
          { title: "#", field: "rank", width: 54 },
          { title: "Source", field: "source", width: 420 },
          { title: "Snaps", field: "snaps", hozAlign: "right" },
          { title: "Physical", field: "physical", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Accum", field: "accum", formatter: (c) => `${Number(c.getValue()).toFixed(2)}x`, hozAlign: "right" },
          { title: "%Repo", field: "repo_pct", formatter: (c) => `${Number(c.getValue()).toFixed(1)}%`, hozAlign: "right" },
          { title: "Freeable", field: "freeable", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Dedup", field: "dedup", formatter: (c) => `${Number(c.getValue()).toFixed(0)}x`, hozAlign: "right" },
          { title: "Latest", field: "logical_latest", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Pins", field: "pins", hozAlign: "right" },
        ], {
          rowClick: (_e, row) => {
            this.selectedSource = row.getData().source;
            this.active = "snapshots";
            this.loadSnapshots();
          },
        });
      }, "Loading dashboard...");
    },

    async loadSnapshots() {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        const data = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`);
        this.snapshots = data.snapshots || [];
        this.selectedSnapshot = this.snapshots.length ? this.snapshots[this.snapshots.length - 1].id : "";
        setTable("#snapshotsTable", this.snapshots, [
          { title: "Start", field: "start_time", width: 180 },
          { title: "ID", field: "id", width: 310 },
          { title: "Logical", field: "logical", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", hozAlign: "right" },
          { title: "Dirs", field: "dirs", hozAlign: "right" },
          { title: "Pins", field: "pins", formatter: (c) => c.getValue() ? JSON.stringify(c.getValue()) : "" },
        ], {
          rowClick: (_e, row) => {
            this.selectedSnapshot = row.getData().id;
            this.mountSnapshot = this.selectedSnapshot;
          },
        });
      }, "Loading snapshots...");
    },

    async loadTree() {
      if (!this.selectedSource || !this.selectedSnapshot) return;
      await this.wrap(async () => {
        const url = `/api/tree?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}&snapshot_id=${encodeURIComponent(this.selectedSnapshot)}&prefix=${encodeURIComponent(this.treePrefix)}`;
        const data = await getJson(url);
        setTable("#treeTable", data.children || [], [
          { title: "Name", field: "name", width: 440 },
          { title: "Type", field: "type", width: 100 },
          { title: "Size", field: "size", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", hozAlign: "right" },
          { title: "Path", field: "path", width: 520 },
        ], {
          rowClick: (_e, row) => {
            if (row.getData().type === "folder") {
              this.treePrefix = row.getData().path;
              this.loadTree();
            }
          },
        });
      }, "Loading tree...");
    },

    async loadSearch() {
      await this.wrap(async () => {
        const over = this.overOneGb ? 1 : 0;
        const url = `/api/search?repo=${encodeURIComponent(this.repo)}&q=${encodeURIComponent(this.query)}&deleted_only=${this.deletedOnly}&over_gb=${over}&limit=300`;
        const data = await getJson(url);
        setTable("#searchTable", data.results || [], [
          { title: "Path", field: "full_path", width: 560 },
          { title: "Size", field: "size", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Versions", field: "versions", hozAlign: "right" },
          { title: "Last Seen", field: "last_seen", width: 180 },
          { title: "Latest", field: "in_latest", formatter: "tickCross", width: 80 },
          { title: "Source", field: "source", width: 380 },
        ], {
          rowClick: async (_e, row) => {
            const item = row.getData();
            this.selectedSource = item.source;
            this.restoreSource = item.full_path;
            this.actionText = JSON.stringify(item, null, 2);
          },
        });
      }, "Searching...");
    },

    async runSimulate() {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        const data = await getJson("/api/simulate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, source: this.selectedSource, mode: this.mode, policy: this.policy }),
        });
        this.simText = JSON.stringify(data, null, 2);
      }, "Simulating retention...");
    },

    async mount() {
      await this.wrap(async () => {
        const data = await getJson("/api/actions/mount", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, snapshot_id: this.mountSnapshot, mountpoint: this.mountPoint }),
        });
        this.actionText = JSON.stringify(data, null, 2);
      }, "Starting mount...");
    },

    async restorePreview() {
      await this.wrap(async () => {
        const data = await getJson("/api/actions/restore-preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, source: this.restoreSource, destination: this.restoreDest }),
        });
        this.actionText = JSON.stringify(data, null, 2);
      });
    },
  }));
});
