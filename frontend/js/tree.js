// Tree tab: point-in-time / changed / reclaimable / all-snapshots browsing + crumbs + flat view.
const treePart = {

    // All-sources rows are deduped by path and deliberately carry no source. Resolve the backups
    // holding a path at ACTION time (one batched call, never per rendered row); a single holder
    // answers directly, several use the panel's own "Backed up by" list to switch.
    async treeSourceFor(path) {
      if (this.selectedSource !== "*ALL*") return this.selectedSource;
      let r = null;
      try {
        r = await getJson("/api/tree/holders", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, full_paths: [path] }) });
      } catch (e) {
        this.klNotify("Could not read which backups hold this path - pick a source first.", "warn");
        return null;
      }
      const holders = ((r && r.paths && r.paths[path]) || []).map((h) => h.source);
      if (!holders.length) { this.klNotify("No indexed backup holds this path.", "warn"); return null; }
      if (holders.length > 1) {
        this.klNotify(`Held by ${holders.length} backups - showing ${sourcePath(holders[0])}. Switch in the panel's "Backed up by" list.`, "info");
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
          onRowDblClick: async (_e, row) => {
            const d = row.getData();
            if (d.type === "folder") return this.navTree(d.path);
            const s = await this.treeSourceFor(d.path);
            if (s) this.openFileDetail({ source: s, full_path: d.path });
          },
          rowMenu: [
            { label: "Open / drill in", action: (_e, row) => { const d = row.getData(); if (d.type === "folder") this.navTree(d.path); } },
            { label: "Details (physical / dedup)", action: async (_e, row) => {
              const d = row.getData();
              if (d.type === "folder") return this.openRowDetail(d);
              const s = await this.treeSourceFor(d.path);
              if (s) this.openRowDetail(d, s);
            } },
            { label: "Version history", action: async (_e, row) => {
              const s = await this.treeSourceFor(row.getData().path);
              if (s) this.openFileDetail({ source: s, full_path: row.getData().path });
            } },
            { label: "Restore…", action: async (_e, row) => {
              const d = row.getData();
              const s = await this.treeSourceFor(d.path);
              if (s) this.openRestore([{ source: s, full_path: d.path, snapshot_id: this.treeMode === "full" ? this.selectedSnapshot || null : null }]);
            } },
            { label: "Download", action: async (_e, row) => {
              const d = row.getData();
              const s = await this.treeSourceFor(d.path);
              if (s) this.downloadItem({ source: s, full_path: d.path, snapshot_id: this.treeMode === "full" ? this.selectedSnapshot || null : null });
            } },
            { label: () => this.mountOpenLabel(), action: async (_e, row) => {
              const d = row.getData();
              const s = await this.treeSourceFor(d.path);
              if (s) this.mountOpen(this.selectedSnapshot, s, d.path);
            } },
            { label: "Find in search", action: (_e, row) => { this.query = row.getData().name; this.searchSource = ""; this.active = "search"; this.loadSearch(); } },
            { label: "Exclude & delete…", action: async (_e, row) => {
              const d = row.getData();
              const kind = d.type === "folder" ? "folder" : "file";
              // A folder in All-sources mode belongs to every covering backup: the dialog resolves
              // them itself, so hand it no source rather than the "*ALL*" scope marker.
              const source = d.type === "folder" ? (this.selectedSource === "*ALL*" ? null : this.selectedSource)
                                                 : await this.treeSourceFor(d.path);
              if (d.type !== "folder" && !source) return;
              this.openExclude({ kind, path: d.path, source }, "delete");
            } },
            { label: "Delete…", action: (_e, row) => this.openRemoveFolder(row.getData()) },
            { label: "Copy path", action: (_e, row) => this.copyText(row.getData().path) },
          ],
        });
        if (ttbl) ttbl.on("rowClick", (e, row) => this._rangeClick(ttbl, e, row));  // Shift+Click range select
      }, "Loading tree...");
    },
    treeFilterChanged() { this.loadTree(); },
    toggleTreeFlat() { this.treeFlat = !this.treeFlat; this.loadTree(); },
    jumpToTree(item) {
      this.selectedSource = item.source; this.treeMode = "all";
      const root = trimSep(sourcePath(item.source));
      this.treePrefix = parentPath(item.full_path) || root; this.active = "tree"; this.loadTree();
    },
    toggleTreeDedupe() { this.treeDedupe = !this.treeDedupe; this.klSet({ tree_dedupe: this.treeDedupe }); this.loadTree(); },
};
