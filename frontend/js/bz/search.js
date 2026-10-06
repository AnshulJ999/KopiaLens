// Backblaze search (FTS5): filters, pagination, by-folder group view, export.
const bzSearchPart = {

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
    bzExportSearch() {
      if (this.bzGroupByFolder) return downloadTable("#bzGroupTable", "backblaze_folders");
      window.open(`/api/backblaze/export?${this.bzSearchUrl().toString()}`, "_blank");
    },
};
