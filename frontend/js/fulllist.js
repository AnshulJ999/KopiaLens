// Full list popup: wherever a panel lists only the first N of a long list, "Show all" opens every
// row here and "Download CSV" saves it. Callers pass a loader that returns all rows.
const fullListPart = {

    // opts: {title, columns: [[field, title, kind]], load: async () => rows, name}. kind "bytes" formats sizes.
    async openFullList(opts) {
      const seq = ++this._fullListSeq;
      this.fullList = { title: opts.title, name: opts.name || "list", loading: true, error: "", count: 0 };
      try {
        const rows = await opts.load();
        if (seq !== this._fullListSeq || !this.fullList) return;
        this.fullList.loading = false;
        this.fullList.count = rows.length;
        await this.$nextTick();
        setTable("#fullListTable", rows, opts.columns.map(([field, title, kind]) => ({
          title, field, headerTooltip: false,
          ...(kind === "bytes" ? { width: 110, hozAlign: "right", sorter: "number", formatter: (c) => fmtBytes(c.getValue()) }
            : { widthGrow: field === "full_path" ? 4 : 1, minWidth: 100 }),
        })), { persistence: false, placeholder: "Nothing to show" });
      } catch (e) {
        if (seq !== this._fullListSeq || !this.fullList) return;
        this.fullList.loading = false;
        this.fullList.error = e.message || String(e);
      }
    },
    closeFullList() { ++this._fullListSeq; this.fullList = null; },
    fullListCsv() { downloadTable("#fullListTable", this.fullList ? this.fullList.name : "list"); },
};
