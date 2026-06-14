const fmtBytes = (value) => {
  const n = Number(value || 0);
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${n} B`;
};

const humanTime = (s) => {
  if (!s) return "";
  const d = new Date(s);
  return isNaN(d.getTime()) ? String(s).slice(0, 19)
    : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

const getJson = async (url, options = {}) => {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || data.error || res.statusText);
  return data;
};

function colVisibilityMenu(e, column) {
  return column.getTable().getColumns().map((col) => ({
    label: (col.isVisible() ? "☑ " : "☐ ") + (col.getDefinition().title || col.getField()),
    action: (ev) => { ev.stopPropagation(); col.toggle(); },
  }));
}

const setTable = (id, data, columns, opts = {}) => {
  const el = document.querySelector(id);
  if (!el) return null;
  const { onRowClick, ...rest } = opts;
  if (el._tabulator) { try { el._tabulator.destroy(); } catch (e) {} el._tabulator = null; }
  columns.forEach((c) => { c.headerContextMenu = colVisibilityMenu; });
  const table = new Tabulator(id, {
    data, columns,
    layout: "fitColumns",
    height: "100%",
    movableColumns: true,
    selectableRows: 1,
    placeholder: "No rows",
    persistence: { columns: true },
    persistenceID: id.replace("#", ""),
    persistenceMode: "local",
    ...rest,
  });
  if (onRowClick) table.on("rowClick", onRowClick);
  el._tabulator = table;
  return table;
};

document.addEventListener("alpine:init", () => {
  Alpine.data("kopiaLens", () => ({
    repos: [], repo: "1",
    tabs: [
      { id: "dashboard", label: "Dashboard" }, { id: "snapshots", label: "Snapshots" },
      { id: "tree", label: "Tree" }, { id: "search", label: "Search" },
      { id: "simulate", label: "Simulate" }, { id: "actions", label: "Actions" },
    ],
    active: "dashboard",
    dark: false,
    indexStatus: {},
    sources: [], selectedSource: "",
    snapshots: [], selectedSnapshot: "",
    showChurn: false,
    query: "", deletedOnly: false, overOneGb: false, omniMeta: "",
    treePrefix: "",
    policyKeys: ["latest", "hourly", "daily", "weekly", "monthly", "annual"],
    policy: { latest: 100, hourly: 96, daily: 30, weekly: 52, monthly: 36, annual: 5 },
    simText: "", actionText: "",
    mountSnapshot: "", mountPoint: "", restoreSource: "", restoreDest: "",
    dashboardMeta: "", busy: "", error: "", detail: null, _poll: null,

    fmtB(v) { return fmtBytes(v); },
    humanTime(s) { return humanTime(s); },
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
      try { return await fn(); } catch (err) { this.error = err.message || String(err); } finally { this.busy = ""; }
    },

    toggleDark() {
      this.dark = !this.dark;
      document.documentElement.classList.toggle("dark", this.dark);
      localStorage.setItem("kl-dark", this.dark ? "1" : "0");
    },

    indexLine() {
      const s = this.indexStatus;
      if (s.building) return `Indexing ${s.indexed_snapshots || 0}/${s.snapshots || "?"}...`;
      if (s.snapshots === undefined) return "Index: unknown";
      return `Index ${s.indexed_snapshots}/${s.snapshots}`;
    },

    async repoChanged() { await this.refreshBase(); this.activate(); },

    async refreshBase() {
      const [status, sources] = await Promise.all([
        getJson(`/api/index/status?repo=${encodeURIComponent(this.repo)}`),
        getJson(`/api/sources?repo=${encodeURIComponent(this.repo)}`),
      ]);
      this.indexStatus = status;
      this.sources = sources.sources || [];
      if (!this.sources.includes(this.selectedSource)) this.selectedSource = this.sources[0] || "";
    },

    activate() {
      if (this.active === "dashboard") return this.loadDashboard();
      if (this.active === "snapshots") return this.loadSnapshots();
      if (this.active === "tree") return this.loadSnapshots().then(() => this.loadTree());
      if (this.active === "search") return this.loadSearch();
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
        const data = await getJson(`/api/dashboard?repo=${encodeURIComponent(this.repo)}&refresh=${refresh}`);
        this.dashboardMeta = `true physical ${fmtBytes(data.union_total)}` + (data.cached ? " (cached - Recompute to refresh)" : ` · computed ${Number(data.elapsed || 0).toFixed(1)}s`);
        setTable("#dashboardTable", data.rows || [], [
          { title: "Source", field: "source", widthGrow: 4, minWidth: 240, formatter: (c) => this.shortSource(c.getValue()) },
          { title: "Snaps", field: "snaps", width: 70, hozAlign: "right" },
          { title: "Physical", field: "physical", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Accum", field: "accum", width: 75, formatter: (c) => `${Number(c.getValue()).toFixed(1)}x`, hozAlign: "right" },
          { title: "%Repo", field: "repo_pct", width: 72, formatter: (c) => `${Number(c.getValue()).toFixed(1)}%`, hozAlign: "right" },
          { title: "Freeable", field: "freeable", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Dedup", field: "dedup", width: 72, formatter: (c) => `${Number(c.getValue()).toFixed(0)}x`, hozAlign: "right" },
          { title: "Latest size", field: "logical_latest", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Last snapshot", field: "last", width: 150, formatter: (c) => humanTime(c.getValue()) },
          { title: "Next snapshot", field: "next_snapshot", width: 150, formatter: (c) => c.getValue() ? humanTime(c.getValue()) : "—" },
          { title: "Pins", field: "pins", width: 64, hozAlign: "right" },
        ], { onRowClick: (_e, row) => { this.selectedSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); } });
      }, "Loading dashboard...");
    },

    async loadSnapshots() {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        const data = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`);
        this.snapshots = data.snapshots || [];
        if (!this.snapshots.find((x) => x.id === this.selectedSnapshot)) this.selectedSnapshot = this.snapshots.length ? this.snapshots[this.snapshots.length - 1].id : "";
        const cols = [
          { title: "Start", field: "start_time", width: 185, formatter: (c) => humanTime(c.getValue()) },
          { title: "Logical", field: "logical", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", width: 95, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Dirs", field: "dirs", width: 85, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Retention", field: "retention", widthGrow: 2, minWidth: 150, formatter: (c) => (c.getValue() || []).map((r) => `<span class="pill" style="background:#dbeafe;color:#1e40af">${r}</span>`).join("") },
          { title: "Pins", field: "pins", widthGrow: 1, minWidth: 90, formatter: (c) => (c.getValue() || []).map((p) => `<span class="pill" style="background:#fef3c7;color:#92400e">${p}</span>`).join("") },
          { title: "ID", field: "id", width: 200 },
        ];
        if (this.showChurn) {
          const churn = await getJson(`/api/snapshots/churn?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}&recent=100000`);
          const m = {}; (churn.rows || []).forEach((r) => { m[r.id] = r.incremental_physical; });
          this.snapshots.forEach((s) => { s.incr = m[s.id] || 0; });
          cols.splice(4, 0, { title: "Churn (new)", field: "incr", width: 105, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" });
        }
        setTable("#snapshotsTable", this.snapshots, cols, { onRowClick: (_e, row) => { this.selectedSnapshot = row.getData().id; this.mountSnapshot = this.selectedSnapshot; this.active = "tree"; this.treePrefix = ""; this.loadTree(); } });
      }, this.showChurn ? "Computing churn (slower)..." : "Loading snapshots...");
    },

    async loadTree() {
      if (!this.selectedSource || !this.selectedSnapshot) return;
      await this.wrap(async () => {
        const url = `/api/tree?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}&snapshot_id=${encodeURIComponent(this.selectedSnapshot)}&prefix=${encodeURIComponent(this.treePrefix)}`;
        const data = await getJson(url);
        if (!(data.children || []).length && !(this.indexStatus.indexed_snapshots > 0)) this.error = "Snapshot not indexed yet - click Index (top right).";
        setTable("#treeTable", data.children || [], [
          { title: "Name", field: "name", widthGrow: 4, minWidth: 260, formatter: (c) => `${c.getData().type === "folder" ? "📁 " : ""}${c.getValue()}` },
          { title: "Size", field: "size", width: 110, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", width: 95, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Type", field: "type", width: 90 },
        ], { onRowClick: (_e, row) => { const d = row.getData(); if (d.type === "folder") { this.treePrefix = d.path; this.loadTree(); } } });
      }, "Loading tree...");
    },

    async loadSearch() {
      await this.wrap(async () => {
        const over = this.overOneGb ? 1 : 0;
        const url = `/api/search?repo=${encodeURIComponent(this.repo)}&q=${encodeURIComponent(this.query)}&deleted_only=${this.deletedOnly}&over_gb=${over}&limit=400`;
        const data = await getJson(url);
        this.omniMeta = `${(data.results || []).length} results`;
        setTable("#searchTable", data.results || [], [
          { title: "Path", field: "full_path", widthGrow: 4, minWidth: 320, formatter: (c) => this.shortSource(c.getValue()) },
          { title: "Size", field: "size", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Versions", field: "versions", width: 80, hozAlign: "right" },
          { title: "Last seen", field: "last_seen", width: 160, formatter: (c) => humanTime(c.getValue()) },
          { title: "On disk", field: "in_latest", width: 90, formatter: (c) => c.getValue() ? "yes" : "<span style='color:#dc2626'>deleted</span>" },
        ], { onRowClick: (_e, row) => this.openFileDetail(row.getData()) });
      }, "Searching...");
    },

    async openFileDetail(item) {
      await this.wrap(async () => {
        const params = `repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`;
        const [versions, purge] = await Promise.all([
          getJson(`/api/search/versions?${params}`),
          getJson(`/api/actions/purge-plan?${params}`),
        ]);
        const v = (versions.versions || []).map((x) => `  ${humanTime(x.start_time)}   ${fmtBytes(x.size)}   ${x.snapshot_id}`).join("\n");
        const p = (purge.commands || []).map((c) => "  " + (Array.isArray(c) ? c.join(" ") : c)).join("\n");
        this.detail = { title: item.full_path, body: `size ${fmtBytes(item.size)} · versions ${item.versions} · on disk: ${item.in_latest ? "yes" : "DELETED"}\n\nVERSIONS:\n${v || "  (none)"}\n\nPURGE PLAN (run yourself - app never executes):\n${p || "  (none)"}\n` };
      });
    },

    async runSimulate() {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        const r = await getJson("/api/simulate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source: this.selectedSource, mode: "all", policy: this.policy }) });
        this.simText = `Source: ${this.shortSource(r.source)}\nKeep ${r.keep_snapshots} / Delete ${r.delete_snapshots} of ${r.total_snapshots} snapshots\n\nRECLAIM: ${fmtBytes(r.reclaim)}\nStill pinned by other sources: ${fmtBytes(r.pinned_by_other_sources)}\n\nPolicy: ${JSON.stringify(r.policy)}`;
      }, "Simulating retention...");
    },

    async mount() {
      await this.wrap(async () => {
        const r = await getJson("/api/actions/mount", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: this.mountSnapshot, mountpoint: this.mountPoint }) });
        this.actionText = `Mount started (pid ${r.pid}):\n${(r.command || []).join(" ")}`;
      }, "Starting mount...");
    },

    async restorePreview() {
      await this.wrap(async () => {
        const r = await getJson("/api/actions/restore-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source: this.restoreSource, destination: this.restoreDest }) });
        this.actionText = `Restore command (preview - run it yourself):\n${(r.command || []).join(" ")}`;
      });
    },
  }));
});
