// Tree tab: point-in-time / changed / reclaimable / all-snapshots browsing + crumbs + flat view.
const treePart = {

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
    jumpToTree(item) {
      this.selectedSource = item.source; this.treeMode = "all";
      const root = sourcePath(item.source).replace(/[\\/]+$/, "");
      const parent = item.full_path.includes("\\") ? item.full_path.slice(0, item.full_path.lastIndexOf("\\")) : root;
      this.treePrefix = parent || root; this.active = "tree"; this.loadTree();
    },
    toggleTreeDedupe() { this.treeDedupe = !this.treeDedupe; this.klSet({ tree_dedupe: this.treeDedupe }); this.loadTree(); },
};
