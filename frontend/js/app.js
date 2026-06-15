const fmtBytes = (value) => {
  const n = Number(value || 0);
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${n} B`;
};

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
let TIME_FMT = localStorage.getItem("kl-timefmt") || "absolute";  // "absolute" (12h AM/PM) | "relative"
const humanTime = (s) => (TIME_FMT === "relative" ? fmtRel(s) : fmtAbs(s));
const timeCell = (s) => {
  if (!s) return "—";
  const abs = fmtAbs(s), rel = fmtRel(s);
  return TIME_FMT === "relative" ? `<span title="${abs}">${rel}</span>` : `<span title="${rel}">${abs}</span>`;
};

// source key = "user@host:path"; path part only (used for display + UNC-aware sort)
const sourcePath = (s) => (s && s.includes(":") ? s.split(":").slice(1).join(":") : (s || ""));
const isUnc = (p) => p.startsWith("\\\\");  // network share -> sorts to the bottom (KopiaUI behaviour)
const srcSortKey = (s) => { const p = sourcePath(s); return (isUnc(p) ? "1" : "0") + "|" + p.toLowerCase(); };
const srcSorter = (a, b) => srcSortKey(a).localeCompare(srcSortKey(b));

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
  const { onRowDblClick, rowMenu, initialSort, ...rest } = opts;
  if (el._tabulator) { try { el._tabulator.destroy(); } catch (e) {} el._tabulator = null; }
  columns.forEach((c) => { c.headerContextMenu = colVisibilityMenu; });
  const table = new Tabulator(id, {
    data, columns,
    layout: "fitColumns",
    height: "100%",
    movableColumns: true,
    selectableRows: 1,
    placeholder: "No rows",
    persistence: { columns: true, sort: true },
    persistenceID: id.replace("#", ""),
    persistenceMode: "local",
    initialSort: initialSort || undefined,
    rowContextMenu: rowMenu || undefined,
    ...rest,
  });
  if (onRowDblClick) table.on("rowDblClick", onRowDblClick);  // double-click to drill (single click selects + allows text select)
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
    timeFmt: localStorage.getItem("kl-timefmt") || "absolute",
    indexStatus: {},
    sources: [], selectedSource: "",
    snapshots: [], selectedSnapshot: "",
    showChurn: false,
    query: "", deletedOnly: false, overOneGb: false, searchSource: "", searchExt: "", omniMeta: "",
    treePrefix: "",
    policyKeys: ["latest", "hourly", "daily", "weekly", "monthly", "annual"],
    policy: { latest: 100, hourly: 96, daily: 30, weekly: 52, monthly: 36, annual: 5 },
    simText: "", actionText: "",
    mountSnapshot: "", mountPoint: "", restoreSource: "", restoreDest: "",
    dashboardMeta: "", busy: "", error: "", detail: null, _poll: null,
    showSettings: false, backend: "subprocess",

    fmtB(v) { return fmtBytes(v); },
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

    async init() {
      this.dark = localStorage.getItem("kl-dark") === "1";
      document.documentElement.classList.toggle("dark", this.dark);
      await this.wrap(async () => {
        const data = await getJson("/api/repos");
        this.repos = data.repos;
        this.repo = data.settings?.last_repo || "1";
        this.backend = data.settings?.listing_backend || "subprocess";
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
    },

    async refreshAll() {
      await this.wrap(async () => {
        await this.refreshBase(true);
        if (this.active === "dashboard") return this.loadDashboard(true);
        if (this.active === "snapshots") return this.loadSnapshots(true);
        if (this.active === "tree") return this.loadTree();
        if (this.active === "search") return this.loadSearch();
      }, "Refreshing...");
    },

    async buildIndex() {  // header button: incremental (fast; only changed sources)
      await this.wrap(async () => {
        await getJson(`/api/index/build?repo=${encodeURIComponent(this.repo)}`, { method: "POST" });
        this.pollIndex();
      }, "Index update started...");
    },
    async rebuildIndex() {  // settings: full rebuild
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
            { label: "Simulate retention", action: (_e, row) => { this.selectedSource = row.getData().source; this.active = "simulate"; } },
            { label: "Copy source", action: (_e, row) => this.copyText(row.getData().source) },
          ],
        });
      }, "Loading dashboard...");
    },

    async loadSnapshots(refresh = false) {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        const data = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`);
        this.snapshots = data.snapshots || [];
        if (!this.snapshots.find((x) => x.id === this.selectedSnapshot)) this.selectedSnapshot = this.snapshots.length ? this.snapshots[this.snapshots.length - 1].id : "";
        const cols = [
          { title: "Start", field: "start_time", width: 185, formatter: (c) => timeCell(c.getValue()) },
          { title: "Logical", field: "logical", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", width: 95, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Dirs", field: "dirs", width: 85, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Retention", field: "retention", widthGrow: 2, minWidth: 150, formatter: (c) => (c.getValue() || []).map((r) => `<span class="pill" style="background:#dbeafe;color:#1e40af">${r}</span>`).join("") },
          { title: "Pins", field: "pins", widthGrow: 1, minWidth: 90, formatter: (c) => (c.getValue() || []).map((p) => `<span class="pill" style="background:#fef3c7;color:#92400e">${p}</span>`).join("") },
          { title: "ID", field: "id", width: 200 },
        ];
        if (this.showChurn) {
          const churn = await getJson(`/api/snapshots/churn?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}&recent=100000${refresh ? "&refresh=true" : ""}`);
          const m = {}; (churn.rows || []).forEach((r) => { m[r.id] = r.incremental_physical; });
          this.snapshots.forEach((s) => { s.incr = m[s.id] || 0; });
          cols.splice(4, 0, { title: "Churn (new)", field: "incr", width: 105, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" });
        }
        setTable("#snapshotsTable", this.snapshots, cols, {
          initialSort: [{ column: "start_time", dir: "desc" }],  // latest first
          onRowDblClick: (_e, row) => { this.selectedSnapshot = row.getData().id; this.mountSnapshot = this.selectedSnapshot; this.active = "tree"; this.treePrefix = this.sourceRoot(); this.loadTree(); },
          rowMenu: [
            { label: "Browse files", action: (_e, row) => { this.selectedSnapshot = row.getData().id; this.active = "tree"; this.treePrefix = this.sourceRoot(); this.loadTree(); } },
            { label: "Mount snapshot (read-only)", action: (_e, row) => this.mountRow(row.getData().id) },
            { label: "Copy snapshot ID", action: (_e, row) => this.copyText(row.getData().id) },
          ],
        });
      }, this.showChurn ? "Computing churn..." : "Loading snapshots...");
    },

    openTree() {
      if (!this.selectedSource) return;
      const ensure = (!this.snapshots.length || !this.snapshots.find((s) => s.id === this.selectedSnapshot)) ? this.loadSnapshots() : Promise.resolve();
      return Promise.resolve(ensure).then(() => {
        if (!this.selectedSnapshot && this.snapshots.length) this.selectedSnapshot = this.snapshots[this.snapshots.length - 1].id;
        if (!this.treePrefix || !this.treePrefix.startsWith(this.sourceRoot().replace(/[\\/]+$/, ""))) this.treePrefix = this.sourceRoot();
        return this.loadTree();
      });
    },

    navTree(prefix) { this.treePrefix = prefix; this.loadTree(); },
    treeUp() { const c = this.treeCrumbs(); if (c.length > 1) this.navTree(c[c.length - 2].prefix); },

    async loadTree() {
      if (!this.selectedSource || !this.selectedSnapshot) return;
      await this.wrap(async () => {
        const url = `/api/tree?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}&snapshot_id=${encodeURIComponent(this.selectedSnapshot)}&prefix=${encodeURIComponent(this.treePrefix)}`;
        const data = await getJson(url);
        const children = data.children || [];
        if (!children.length && !(this.indexStatus.indexed_snapshots > 0)) this.error = "Snapshot not indexed yet - click Index (top right).";
        const total = children.reduce((a, c) => a + (c.size || 0), 0) || 1;
        children.forEach((c) => { c.pct = 100 * (c.size || 0) / total; });
        setTable("#treeTable", children, [
          { title: "Name", field: "name", widthGrow: 4, minWidth: 260, formatter: (c) => `${c.getData().type === "folder" ? "📁 " : "📄 "}${c.getValue()}` },
          { title: "Size", field: "size", width: 110, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "% of folder", field: "pct", width: 95, formatter: (c) => `${Number(c.getValue() || 0).toFixed(1)}%`, hozAlign: "right" },
          { title: "Files", field: "files", width: 90, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Modified", field: "modified", width: 165, formatter: (c) => timeCell(c.getValue()) },
          { title: "Type", field: "type", width: 80 },
        ], {
          initialSort: [{ column: "size", dir: "desc" }],
          onRowDblClick: (_e, row) => { const d = row.getData(); if (d.type === "folder") this.navTree(d.path); else this.openFileDetail({ source: this.selectedSource, full_path: d.path, size: d.size, versions: "?", in_latest: true }); },
          rowMenu: [
            { label: "Open / drill in", action: (_e, row) => { const d = row.getData(); if (d.type === "folder") this.navTree(d.path); } },
            { label: "Version history", action: (_e, row) => this.openFileDetail({ source: this.selectedSource, full_path: row.getData().path, size: row.getData().size, versions: "?", in_latest: true }) },
            { label: "Copy path", action: (_e, row) => this.copyText(row.getData().path) },
            { label: "Mount this snapshot (read-only)", action: () => this.mountRow(this.selectedSnapshot) },
          ],
        });
      }, "Loading tree...");
    },

    async loadSearch() {
      await this.wrap(async () => {
        const over = this.overOneGb ? 1 : 0;
        const params = new URLSearchParams({ repo: this.repo, q: this.query, deleted_only: this.deletedOnly, over_gb: over, limit: 400 });
        if (this.searchSource) params.set("source", this.searchSource);
        if (this.searchExt) params.set("ext", this.searchExt);
        const data = await getJson(`/api/search?${params.toString()}`);
        this.omniMeta = `${(data.results || []).length} results`;
        setTable("#searchTable", data.results || [], [
          { title: "Path", field: "full_path", widthGrow: 5, minWidth: 320 },
          { title: "Size", field: "size", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Ext", field: "ext", width: 70 },
          { title: "Versions", field: "versions", width: 80, hozAlign: "right" },
          { title: "Last seen", field: "last_seen", width: 160, formatter: (c) => timeCell(c.getValue()) },
          { title: "On disk", field: "in_latest", width: 90, formatter: (c) => c.getValue() ? "yes" : "<span style='color:#dc2626'>deleted</span>" },
          { title: "Source", field: "source", widthGrow: 2, minWidth: 180, formatter: (c) => sourcePath(c.getValue()) },
        ], {
          initialSort: [{ column: "size", dir: "desc" }],
          onRowDblClick: (_e, row) => this.openFileDetail(row.getData()),
          rowMenu: [
            { label: "Version history / purge / restore", action: (_e, row) => this.openFileDetail(row.getData()) },
            { label: "Copy full path", action: (_e, row) => this.copyText(row.getData().full_path) },
            { label: "Browse this source", action: (_e, row) => { this.selectedSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); } },
          ],
        });
      }, "Searching...");
    },

    async openFileDetail(item) {
      await this.wrap(async () => {
        const params = `repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`;
        const [versions, purge] = await Promise.all([
          getJson(`/api/search/versions?${params}`),
          getJson(`/api/actions/purge-plan?${params}`),
        ]);
        const v = (versions.versions || []).map((x) =>
          `  ${humanTime(x.start_time)}   ${fmtBytes(x.size)}   snap ${x.snapshot_id}\n      restore: kopia restore ${x.content_id} <destination>`
        ).join("\n");
        const items = purge.items || [];
        const p = (purge.commands || []).map((c, i) => {
          const it = items[i] || {};
          const tag = it.pinned ? "   ⚠ PINNED (kept)" : (it.retention && it.retention.length ? `   ⚠ retained: ${it.retention.join(",")}` : "");
          return "  " + (Array.isArray(c) ? c.join(" ") : c) + tag;
        }).join("\n");
        this.detail = {
          title: item.full_path,
          body: `source / policy: ${item.source}\nsize ${fmtBytes(item.size)} · versions ${item.versions} · on disk: ${item.in_latest ? "yes" : "DELETED"}\n\n`
            + `VERSIONS (newest first):\n${v || "  (none)"}\n\n`
            + `RESTORE: copy a restore line above, set <destination>, run it yourself.\n\n`
            + `PURGE PLAN (deletes whole snapshots - run yourself; ⚠ = retained/pinned, avoid):\n${p || "  (none)"}\n`,
        };
      });
    },

    mountRow(snapId) {
      const mp = window.prompt("Mount snapshot (read-only) to drive/folder:", "Z:");
      if (!mp) return;
      this.wrap(async () => {
        const r = await getJson("/api/actions/mount", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: snapId, mountpoint: mp }) });
        this.actionText = `Mount started (pid ${r.pid}):\n${(r.command || []).join(" ")}`;
        window.alert(`Mounted to ${mp} (pid ${r.pid}).`);
      }, "Starting mount...");
    },

    async runSimulate() {
      if (!this.selectedSource) return;
      await this.wrap(async () => {
        const r = await getJson("/api/simulate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source: this.selectedSource, mode: "all", policy: this.policy }) });
        this.simText = `Source: ${sourcePath(r.source)}\nKeep ${r.keep_snapshots} / Delete ${r.delete_snapshots} of ${r.total_snapshots} snapshots\n\nRECLAIM: ${fmtBytes(r.reclaim)}\nStill pinned by other sources: ${fmtBytes(r.pinned_by_other_sources)}\n\nPolicy: ${JSON.stringify(r.policy)}`;
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

    async saveSettings() {
      await this.wrap(async () => {
        await getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ listing_backend: this.backend }) });
        this.showSettings = false;
        window.alert("Saved. Restart KopiaLens for the listing backend change to take effect.");
      });
    },
  }));
});
