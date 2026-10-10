// Tree tab: point-in-time / changed / reclaimable / all-snapshots browsing + crumbs + flat view.
const treePart = {

    // All-sources rows are deduped by path and deliberately carry no source. Resolve the sources
    // holding a path at ACTION time (one batched call, never per rendered row); a single holder
    // answers directly, several use the panel's own "Backed up by" list to switch.
    async treeSourceFor(path) {
      if (this.selectedSource !== "*ALL*") return this.selectedSource;
      let r = null;
      try {
        r = await getJson("/api/tree/holders", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, full_paths: [path] }) });
      } catch (e) {
        this.klNotify("Couldn't read which sources hold this path. Pick a source first.", "warn");
        return null;
      }
      const holders = ((r && r.paths && r.paths[path]) || []).map((h) => h.source);
      if (!holders.length) { this.klNotify("No indexed source holds this path.", "warn"); return null; }
      if (holders.length > 1) {
        this.klNotify(`Held by ${holders.length} sources. Showing ${sourcePath(holders[0])}; switch in the panel's "Backed up by" list.`, "info");
      }
      return holders[0];
    },

    treeCrumbs() {
      const root = this.sourceRoot();
      const top = root ? trimSep(root) : "";
      const crumbs = [{ label: top || "root", prefix: root }];
      const chain = [];
      let cur = this.treePrefix || "";
      for (let i = 0; cur && trimSep(cur) !== top && i < 500; i++) {
        const up = parentPath(cur);
        chain.unshift({ label: up === null ? trimSep(cur) : baseName(cur), prefix: cur });
        if (up === null) break;
        cur = up;
      }
      return crumbs.concat(chain);
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
      const haveForSource = this.snapshots.length && this.snapshots.every((s) => s.source === this.selectedSource);
      const ensure = (haveForSource && this.snapshots.find((s) => s.id === this.selectedSnapshot)) ? Promise.resolve() : this._treeSnapshots();
      return Promise.resolve(ensure).then(() => {
        if ((!this.selectedSnapshot || !this.snapshots.find((s) => s.id === this.selectedSnapshot)) && this.snapshots.length) this.selectedSnapshot = this.snapshots[this.snapshots.length - 1].id;
        if (!this.treePrefix || !this.treePrefix.startsWith(this.sourceRoot().replace(/[\\/]+$/, ""))) this.treePrefix = this.sourceRoot();
        return this.loadTree();
      });
    },
    treeSourceChanged() {  // remembered per repo; Snapshots keeps its own choice
      this.treeSourceByRepo = { ...this.treeSourceByRepo, [this.repo]: this.selectedSource };
      this.klSet({ tree_source_by_repo: this.treeSourceByRepo });
      this.snapshots = []; this.selectedSnapshot = ""; this.treePrefix = this.sourceRoot(); this.openTree();
    },
    treeModeChanged() { this.treePrefix = this.sourceRoot(); this.klSet({ tree_mode: this.treeMode }); this.openTree(); },
    treeWeekChanged() { this.treePrefix = this.sourceRoot(); this.loadTree(); },
    navTree(prefix) { this.treePrefix = prefix; this.loadTree(); },
    treeUp() { const c = this.treeCrumbs(); if (c.length > 1) this.navTree(c[c.length - 2].prefix); },

    _treeUrl(prefix, flat) {
      return `/api/tree?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.selectedSource)}`
        + `&snapshot_id=${encodeURIComponent(this.selectedSnapshot)}&prefix=${encodeURIComponent(prefix)}&mode=${this.treeMode}`
        + `&dedupe=${this.treeDedupe ? 1 : 0}`
        + `&deleted_only=${this.treeDeletedOnly ? 1 : 0}&over_gb=${this.treeOverGb ? 1 : 0}`
        + `&flat=${flat ? 1 : 0}`
        + (this.selectedSource === "*ALL*" && this.treeWeek ? `&week=${encodeURIComponent(this.treeWeek)}` : "")
        + (this.treeExt ? `&ext=${encodeURIComponent(this.treeExt)}` : "");
    },
    // One level of rows: share of their folder, and in Tree view a placeholder child under each
    // folder (loaded on expand) plus a "more" row past the per-level cap.
    _treeLevel(children, parent) {
      const total = children.reduce((a, c) => a + (c.size || 0), 0) || 1;
      children.forEach((c) => { c.pct = 100 * (c.size || 0) / total; });
      if (this.treeView !== "tree") return children;
      const CAP = 300;
      const rows = children.length > CAP ? [...children].sort((a, b) => (b.size || 0) - (a.size || 0)).slice(0, CAP) : children;
      rows.forEach((c) => { if (c.type === "folder") c._children = [{ _ph: true, name: "Loading…" }]; });
      if (children.length > CAP) {
        rows.push({ _ph: true, _open: parent, name: `${(children.length - CAP).toLocaleString(NUM_LOCALE)} more. Double-click to open this folder and see everything.` });
      }
      return rows;
    },
    async _treeExpand(row) {
      const d = row.getData();
      const kids = row.getTreeChildren();
      if (d._loaded || !(kids.length === 1 && kids[0].getData()._ph)) return;
      d._loaded = true;
      let children;
      try {
        children = ((await getJson(this._treeUrl(d.path, false))).children) || [];
      } catch (e) {
        d._loaded = false;
        kids[0].update({ name: "Couldn't load this folder. Collapse and expand to retry." });
        return;
      }
      const rows = this._treeLevel(children, d.path);
      // Add before removing the placeholder: a row left with no children is marked collapsed.
      if (!rows.length) rows.push({ _ph: true, name: "Empty" });
      rows.forEach((r) => row.addTreeChild(r));
      kids[0].delete();
      if (!row.isTreeExpanded()) row.treeExpand();
    },
    setTreeView(v) {
      if (this.treeView === v) return;
      this.treeView = v;
      this.klSet({ tree_view: v });
      this.loadTree();
    },

    async loadTree() {
      if (!this.selectedSource) return;
      if (this.selectedSource !== "*ALL*" && this.treeMode !== "all" && !this.selectedSnapshot) return;
      const seq = ++this._treeSeq;
      await this.wrap(async () => {
        const tree = this.treeView === "tree";
        const data = await getJson(this._treeUrl(this.treePrefix, this.treeFlat && !tree));
        if (seq !== this._treeSeq) return;  // a newer navigation superseded this one
        const children = data.children || [];
        if (!children.length && !(this.indexStatus.indexed_snapshots > 0)) this.error = "Snapshot not indexed yet - click Index (top right).";
        const rows = this._treeLevel(children, this.treePrefix);
        // Inside a folder, a first row goes back up one level (the toolbar Up stays too).
        const crumbs = this.treeCrumbs();
        if (crumbs.length > 1) rows.unshift({ _ph: true, _up: true, name: `↑ Up to ${crumbs[crumbs.length - 2].label}` });
        const ph = (fn) => (c) => (c.getData()._ph ? "" : fn(c));
        const ttbl = setTable("#treeTable", rows, [
          { title: "Name", field: "name", widthGrow: 4, minWidth: 260,
            formatter: (c) => { const d = c.getData(); return d._ph ? `<span class="text-muted">${c.getValue()}</span>` : `${d.type === "folder" ? "📁 " : "📄 "}${c.getValue()}`; } },
          { title: term("logical"), headerTooltip: termTip("logical"), field: "size", width: 105, sorter: "number", formatter: ph((c) => fmtBytes(c.getValue())), hozAlign: "right" },
          { title: "Physical", field: "physical", width: 100, sorter: "number", formatter: ph((c) => fmtPhys(c.getValue())), hozAlign: "right" },
          { title: "Share of folder", field: "pct", width: 130, sorter: "number",
            formatter: ph((c) => { const v = Number(c.getValue() || 0); return `<span class="kl-sharebar"><i style="width:${Math.min(100, v).toFixed(1)}%"></i></span>${fmtFix(v, 1)}%`; }) },
          { title: "Files", field: "files", width: 85, hozAlign: "right", formatter: ph((c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE)) },
          { title: "Modified", field: "modified", width: 165, formatter: ph((c) => timeCell(c.getValue())) },
          { title: "Type", field: "type", width: 75, visible: false },
        ], {
          selectableRows: true,
          selectableRowsCheck: (r) => !r.getData()._ph,
          frozenRows: (r) => !!r.getData()._up,
          ...(tree ? { dataTree: true, dataTreeElementColumn: "name", dataTreeStartExpanded: false, dataTreeChildIndent: 14 } : {}),
          initialSort: [{ column: "size", dir: "desc" }],
          onSelect: (rows) => { this.treeSelCount = rows.length; },
          onRowDblClick: async (_e, row) => {
            const d = row.getData();
            if (d._ph) { if (d._open) this.navTree(d._open); return; }   // the Up row goes up on a single click
            if (d.type === "folder") return this.navTree(d.path);
            const s = await this.treeSourceFor(d.path);
            if (s) this.openFileDetail({ source: s, full_path: d.path });
          },
          rowMenu: (_e, row) => (row.getData()._ph ? false : this.treeRowMenu(row.getData())),
        });
        if (tree && ttbl) ttbl.on("dataTreeRowExpanded", (row) => this._treeExpand(row));
        if (ttbl) ttbl.on("rowClick", (_e, row) => { if (row.getData()._up) this.treeUp(); });
        this.treeSelCount = 0;
      }, "Loading tree...");
    },
    // One menu per row type; with several rows selected it acts on all of them.
    treeRowMenu(d) {
      const sel = this.selectedRows("tree");
      const snap = this.treeMode === "full" ? this.selectedSnapshot || null : null;
      const folder = d.type === "folder";
      if (sel.length > 1) {
        const folders = sel.filter((r) => r.type === "folder").length;
        const what = folders === sel.length ? countOf(sel.length, "folder") : folders ? countOf(sel.length, "item") : countOf(sel.length, "file");
        return [
          { label: `Details of ${what}`, action: () => this.openSelection("tree") },
          { separator: true },
          { label: `Restore ${what}…`, action: () => this.selRestore("tree") },
          { label: `Exclude ${what}…`, action: () => this.selExclude("tree") },
          { separator: true },
          { label: `Delete ${what} from backups…` + this.proMenuTag("remove"), action: () => this.selDelete("tree") },
          { label: `Exclude & delete ${what}…`, action: () => this.selExclude("tree", "delete") },
          { separator: true },
          { label: `Copy ${sel.length} paths`, action: () => this.copyText(sel.map((r) => r.path).join("\n")) },
          { label: "Clear selection", action: () => this.clearSelection("tree") },
        ];
      }
      const withSource = async (fn) => { const s = await this.treeSourceFor(d.path); if (s) fn(s); };
      const items = folder
        ? [{ label: "Open", action: () => this.navTree(d.path) },
           { label: "Details", action: () => this.openRowDetail(d) },
           { label: "Show in Search", action: () => this.drillFolder({ folder: d.path }) }]
        : [{ label: "Details", action: () => withSource((s) => this.openFileDetail({ source: s, full_path: d.path })) },
           { label: "Show in Search", action: () => { this.query = d.name; this.searchSource = ""; this.active = "search"; this.loadSearch(); } }];
      items.push(
        { separator: true },
        { label: "Restore…", action: () => withSource((s) => this.openRestore([{ source: s, full_path: d.path, snapshot_id: snap }])) },
        { label: "Download", action: () => withSource((s) => this.downloadItem({ source: s, full_path: d.path, snapshot_id: snap })) },
        { label: this.mountOpenLabel(), action: () => withSource((s) => this.mountOpen(this.selectedSnapshot, s, d.path)) },
        { label: "Exclude…", action: () => this.treeExcludeRow(d, "exclude") },
        { label: "Prune versions…", action: () => (folder ? this.openPrune(d.path, this.selectedSource === "*ALL*" ? null : this.selectedSource)
                                                          : withSource((s) => this.openPrune(d.path, s))) },
        { separator: true },
        // Files go to the Delete panel, folders to the folder plan (which honours the Tree filters).
        { label: (folder ? "Delete folder from backups…" : "Delete from backups…") + this.proMenuTag("remove"),
          action: () => (folder ? this.openRemoveFolder(d) : withSource((s) => this.openDeletePanel([{ source: s, full_path: d.path }]))) },
        { label: "Exclude & delete…" + this.proMenuTag("exclude_delete"), action: () => this.treeExcludeRow(d, "delete") },
        { separator: true },
        { label: "Copy path", action: () => this.copyText(d.path) });
      return items;
    },
    async treeExcludeRow(d, mode) {
      // A folder in All-sources mode belongs to every covering backup source: the dialog resolves
      // them itself, so it gets no source rather than the "*ALL*" scope marker.
      const folder = d.type === "folder";
      const source = folder ? (this.selectedSource === "*ALL*" ? null : this.selectedSource) : await this.treeSourceFor(d.path);
      if (!folder && !source) return;
      this.openExclude({ kind: folder ? "folder" : "file", path: d.path, source }, mode);
    },
    treeFilterChanged() { this.loadTree(); },
    toggleTreeFlat() { if (this.treeView === "tree") return; this.treeFlat = !this.treeFlat; this.loadTree(); },
    jumpToTree(item) {
      this.selectedSource = item.source; this.treeMode = "all";
      const root = trimSep(sourcePath(item.source));
      this.treePrefix = parentPath(item.full_path) || root; this.active = "tree"; this.loadTree();
    },
    toggleTreeDedupe() { this.treeDedupe = !this.treeDedupe; this.klSet({ tree_dedupe: this.treeDedupe }); this.loadTree(); },
};
