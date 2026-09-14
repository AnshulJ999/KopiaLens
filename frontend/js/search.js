// Search tab: server-side filtered search, dedupe, reclaim, by-folder bloat finder.
const searchPart = {

    // ---------- Search (server-side count + sort + pagination) ----------
    searchParams() {
      const p = new URLSearchParams({ repo: this.repo, q: this.query, deleted_only: this.deletedOnly, over_gb: this.overOneGb ? 1 : 0, sort: this.searchSort, dir: this.searchDir });
      if (this.searchSource) p.set("source", this.searchSource);
      if (this.searchExt) p.set("ext", this.searchExt);
      if (this.searchExtExclude) p.set("ext_exclude", this.searchExtExclude);
      if (this.minGb !== "" && this.minGb != null) p.set("min_gb", this.minGb);
      if (this.maxGb !== "" && this.maxGb != null) p.set("max_gb", this.maxGb);
      if (this.dateFrom) p.set("date_from", this.dateFrom);
      if (this.dateTo) p.set("date_to", this.dateTo);
      if (this.minVersions !== "" && this.minVersions != null) p.set("min_versions", this.minVersions);
      if (this.sharedMin !== "" && this.sharedMin != null) p.set("shared_min", this.sharedMin);
      const ff = this.recentBackupCutoff();
      if (ff) p.set("first_from", ff);
      if (this.dedupe) p.set("dedupe", "1");
      return p;
    },
    recentBackupCutoff() {  // dropdown days -> YYYY-MM-DD cutoff for first_seen ("recently backed up")
      const days = parseInt(this.recentBackup, 10);
      if (!days) return "";
      return new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
    },
    changeRecentBackup() { this.searchPage = 1; this.klSet({ recent_backup: this.recentBackup }); this.loadSearch(); },
    searchPages() { return Math.max(1, Math.ceil(this.searchTotal / (parseInt(this.searchPageSize, 10) || 500))); },
    setSearchPage(n) { this.searchPage = Math.min(Math.max(1, n), this.searchPages()); this.loadSearch(true); },
    setSearchSort(field, dir) { this.searchSort = field; this.searchDir = dir; this.searchPage = 1; this.klSet({ search_sort: field, search_dir: dir }); this.loadSearch(); },
    changePageSize() {
      this.klSet({ page_size: this.searchPageSize });  // persist the raw value, including "Infinite"
      this.searchPage = 1; this.loadSearch();
    },
    clearFilters() {
      this.minGb = ""; this.maxGb = ""; this.dateFrom = ""; this.dateTo = ""; this.minVersions = ""; this.sharedMin = "";
      this.searchExt = ""; this.searchExtExclude = ""; this.searchSource = ""; this.deletedOnly = false; this.overOneGb = false;
      this.recentBackup = ""; this.klSet({ recent_backup: "" });
      this.loadSearch();
    },

    // ---- Bloat finder: group backed-up paths by folder, ranked by physical (own #groupTable) ----
    toggleGroupByFolder() { this.groupByFolder = !this.groupByFolder; this.searchPage = 1; this.loadSearch(); },
    changeGroupDepth() { this.klSet({ group_depth: Number(this.groupDepth) || 3 }); this.loadSearch(); },
    drillFolder(row) { this.groupByFolder = false; this.query = row.folder; this.searchPage = 1; this.loadSearch(); },
    selectedGroupFolder() {  // the By-folder table is a SEPARATE Tabulator instance from #searchTable
      const el = document.querySelector("#groupTable");
      const rows = (el && el._tabulator) ? el._tabulator.getSelectedData() : [];
      return rows.length ? rows[0] : null;
    },
    async loadGroupByFolder() {
      await this.wrap(async () => {
        const p = new URLSearchParams({ repo: this.repo, depth: this.groupDepth, q: this.query || "", sort: "physical", direction: "desc", limit: 500 });
        if (this.searchSource) p.set("source", this.searchSource);
        if (this.searchExt) p.set("ext", this.searchExt);
        if (this.searchExtExclude) p.set("ext_exclude", this.searchExtExclude);
        if (this.deletedOnly) p.set("deleted_only", "1");
        if (this.groupMinGb) p.set("min_gb", this.groupMinGb);
        if (this.groupMinFiles) p.set("min_files", this.groupMinFiles);
        const data = await getJson(`/api/search/group-by-folder?${p.toString()}`);
        const rows = data.rows || [];
        this.omniMeta = `${rows.length} folders · depth ${data.depth} · ranked by physical`;
        const cols = [
          { title: "Folder", field: "folder", widthGrow: 5, minWidth: 300 },
          { title: "Physical", field: "phys", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => fmtPhys(c.getValue()) },
          { title: "Logical", field: "logical", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => fmtBytes(c.getValue()) },
          // Excl column hidden here too - same reason as the Search table ().
          { title: "Files", field: "files", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Deleted #", field: "deleted_files", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Deleted %", field: "deleted_pct", width: 90, headerSort: false, hozAlign: "right", formatter: (c) => { const d = c.getRow().getData(); return d.files ? Math.round((d.deleted_files / d.files) * 100) + "%" : ""; } },
          { title: "Avg size", field: "avg_size", width: 95, headerSort: false, hozAlign: "right", formatter: (c) => { const d = c.getRow().getData(); return d.files ? fmtBytes(Math.round(d.phys / d.files)) : ""; } },
          // per-folder freeable, computed on demand (right-click) - a chunk walk per folder.
          // "≥" marks a partial result: the backend caps the walk at the first 5,000 files.
          { title: "∑ Reclaim", field: "reclaim_val", headerSort: false, width: 105, hozAlign: "right",
            formatter: (c) => {
              const v = this.groupReclaim[this.repo + ":" + c.getRow().getData().folder];
              if (v === "pending") return "<span style='color:var(--faint)'>…</span>";
              if (v === "error") return "<span style='color:var(--danger)'>err</span>";
              if (v == null) return "<span style='color:var(--faint)' title='Right-click > Compute reclaim'>—</span>";
              const tip = v.capped ? ' title="Partial - first 5,000 files only"' : "";
              return `<span style="color:var(--ok)"${tip}>${v.capped ? "&ge; " : ""}${fmtPhys(v.v)}</span>`;
            } },
          { title: "Last seen", field: "last_seen", width: 155, formatter: (c) => timeCell(c.getValue()) },
        ];
        setTable("#groupTable", rows, cols, {
          onRowDblClick: (_e, row) => this.openFolderPanel(row.getData().folder),
          rowMenu: [
            { label: "Folder details", action: (_e, row) => this.openFolderPanel(row.getData().folder) },
            { label: "Compute reclaim for this folder", action: (_e, row) => this.computeGroupReclaim(row) },
            { label: "Drill into files (then batch-remove)", action: (_e, row) => this.drillFolder(row.getData()) },
            { label: "Delete folder (chunked)…", action: (_e, row) => this.openRemoveFolderPath(row.getData().folder) },
          ],
        });
      }, "Grouping folders…");
    },
    // freeable for one folder row of the By-folder view - the same on-demand walk the
    // remove-detail button uses, scoped to the folder prefix. Heavy, so it only runs when asked.
    // Keyed by repo: switching repos must never show the previous repo's number.
    async computeGroupReclaim(row) {
      const d = row.getData();
      const key = this.repo + ":" + d.folder;
      const cur = this.groupReclaim[key];
      if (cur === "pending") return;  // block repeats only while in flight; a deliberate
                                      // second pick recomputes - that pick IS the manual refresh
      this.groupReclaim[key] = "pending";
      row.reformat();
      try {
        const r = await getJson("/api/actions/remove-detail", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, prefix: d.folder, all_versions: true }) });
        this.groupReclaim[key] = (r.found === false)
          ? { v: 0, capped: false }
          : { v: r.freeable ?? 0, capped: !!r.capped };
      } catch (e) {
        this.groupReclaim[key] = "error";
      }
      row.reformat();
    },
    async loadSearch(keepPage = false) {
      if (this.groupByFolder) return this.loadGroupByFolder();
      const seq = ++this._searchSeq;
      if (!keepPage) this.searchPage = 1;
      await this.wrap(async () => {
        const infinite = this.searchPageSize === "Infinite";
        const size = infinite ? 100000 : (parseInt(this.searchPageSize, 10) || 500);
        const offset = infinite ? 0 : (this.searchPage - 1) * size;
        const p = this.searchParams(); p.set("limit", size); p.set("offset", offset);
        const data = await getJson(`/api/search?${p.toString()}`);
        if (seq !== this._searchSeq) return;
        this.searchTotal = data.total || 0;
        const shownFrom = this.searchTotal ? offset + 1 : 0;
        const shownTo = Math.min(offset + (data.results || []).length, this.searchTotal);
        this.omniMeta = `${this.searchTotal.toLocaleString()} results` + (infinite ? "" : ` · showing ${shownFrom.toLocaleString()}-${shownTo.toLocaleString()}`);
        // Headers do NOT sort locally (headerSort:false) - a click re-queries the server so the
        // sort is always over the ENTIRE result set, never just the visible page. Active column
        // shows a ▼/▲ arrow.
        const self = this;
        const arrow = (f) => (this.searchSort === f ? (this.searchDir === "desc" ? "  ▼" : "  ▲") : "");
        const H = (title, field, extra) => Object.assign({
          title: title + arrow(field), field, headerSort: false,
          headerClick: () => self.setSearchSort(field, (self.searchSort === field && self.searchDir === "desc") ? "asc" : "desc"),
        }, extra || {});
        const cols = [
          H("Path", "full_path", { widthGrow: 5, minWidth: 320 }),
          H("Size", "size", { width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" }),
          H("Physical (latest)", "physical", { width: 120, formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" }),
          // Excl column intentionally hidden: paths.exclusive is unfilled (the fill was removed -
          // wrong semantics for shared indirect content + write amplification) and its per-object
          // semantics overstated exclusivity. Returns with the proper implementation ().
          { title: "Src", field: "sources", headerSort: false, width: 62, hozAlign: "right",
            formatter: (c) => { const v = Number(c.getValue() || 1); return v > 1 ? `<span style="color:var(--warn)" title="content is held by ${v} sources (within-source copies excluded)">${v}</span>` : "1"; } },
          { title: "Reclaim", field: "reclaim", headerSort: false, width: 110, hozAlign: "right",
            formatter: (c) => { const v = c.getValue(); return (v == null) ? "<span style='color:var(--faint)'>—</span>" : fmtBytes(v); } },
          H("Ext", "ext", { width: 70 }),
          H("Versions", "versions", { width: 80, hozAlign: "right" }),
          { title: "Copies", field: "copies", headerSort: false, width: 75, hozAlign: "right",
            formatter: (c) => { const v = Number(c.getValue() || 1); return v > 1 ? `<span style="color:var(--warn)" title="this exact content is at ${v} paths (filter: Shared ≥ N)">${v}</span>` : "1"; } },
          H("Last seen", "last_seen", { width: 160, formatter: (c) => timeCell(c.getValue()) }),
          H("First seen", "first_seen", { width: 160, formatter: (c) => timeCell(c.getValue()) }),
          H("On disk", "in_latest", { width: 90, formatter: (c) => c.getValue() ? "yes" : "<span style='color:var(--danger)'>deleted</span>" }),
          H("Source", "source", { widthGrow: 2, minWidth: 180, formatter: (c) => sourcePath(c.getValue()) }),
        ];
        // Reuse the existing Tabulator (replaceData + header-title update) rather than
        // destroy+recreate, so re-querying on sort/filter no longer flickers the whole table.
        const sEl = document.querySelector("#searchTable");
        if (sEl && sEl._tabulator) {
          const t = sEl._tabulator;
          // pass the full def (keeps headerClick + formatter) so only the rows + sort arrow change
          cols.forEach((c) => { try { t.updateColumnDefinition(c.field, c); } catch (e) {} });
          t.replaceData(data.results || []);
        } else {
          const sStbl = setTable("#searchTable", data.results || [], cols, {
            persistSort: false,
            selectableRows: true,
            onRowDblClick: (_e, row) => this.openFileDetail(row.getData()),
            rowMenu: [
              { label: "File detail (physical / reclaim / dedup)", action: (_e, row) => this.openFileDetail(row.getData()) },
              { label: "Version history", action: (_e, row) => this.openFileDetail(row.getData()) },
              { label: "Mount + open in Explorer (latest)", action: (_e, row) => this.mountFileOpen(row.getData()) },
              { label: "Restore…", action: (_e, row) => { const d = row.getData(); this.openRestore([{ source: d.source, full_path: d.full_path }]); } },
              { label: "Download", action: (_e, row) => { const d = row.getData(); this.downloadItem({ source: d.source, full_path: d.full_path }); } },
              { label: "Jump to it in tree", action: (_e, row) => this.jumpToTree(row.getData()) },
              { label: "Browse this source", action: (_e, row) => { this.selectedSource = row.getData().source; this.snapSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); } },
              { label: "Delete ALL matching this filter (chunked)…", action: () => this.openRemoveFromSearch() },
              { label: "Copy full path", action: (_e, row) => this.copyText(row.getData().full_path) },
            ],
          });
          if (sStbl) sStbl.on("rowClick", (e, row) => this._rangeClick(sStbl, e, row));  // Shift+Click range select
        }
      }, "Searching...");
    },

    exportSearch() {
      const p = this.searchParams();
      window.open(`/api/search/export?${p.toString()}`, "_blank");
    },

    // ---- Dedupe + compute-reclaim (Search) ----
    toggleDedupe() { this.dedupe = !this.dedupe; this.searchPage = 1; this.klSet({ search_dedupe: this.dedupe }); this.loadSearch(); },
    async computeReclaim() {
      const el = document.querySelector("#searchTable");
      if (!el || !el._tabulator) return;
      const rows = el._tabulator.getData();
      const top = [...rows].sort((a, b) => (b.physical || 0) - (a.physical || 0)).slice(0, 500);
      const cids = [...new Set(top.map((r) => r.content_id).filter(Boolean))];
      if (!cids.length) return;
      this.reclaimComputing = true;
      try {
        const r = await getJson("/api/search/reclaim", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, content_ids: cids, cap: 500 }) });
        const map = r.reclaim || {};
        rows.forEach((row) => { row.reclaim = map[row.content_id]; });
        rows.sort((a, b) => (b.reclaim || 0) - (a.reclaim || 0));  // most freeable first
        el._tabulator.replaceData(rows);
      } catch (err) { this.error = err.message || String(err); }
      finally { this.reclaimComputing = false; }
    },
};
