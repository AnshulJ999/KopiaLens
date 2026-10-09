// Backblaze dashboard: per-drive coverage, stats cards, category breakdown.
const bzDashboardPart = {

    // ---- Dashboard ----
    async bzLoadDashboard() {
      await this.wrap(async () => {
        const [ov, st, ch] = await Promise.all([
          getJson("/api/backblaze/overview"),
          getJson("/api/backblaze/stats"),
          getJson("/api/backblaze/churn?top=40"),
        ]);
        this.bzOverview = ov; this.bzStats = st;
        this._bzChurnAll = ch.rows || [];
        this.bzChurnRows = this._bzChurnAll.filter((r) => r.status !== "gone").slice(0, 10);  // drop deleted folders from the at-a-glance panel
        this.bzChurnTotal = ch.total_recent || 0;
        setTable("#bzDashTable", ov.rows || [], [
          { title: "Drive", field: "drive", width: 70, formatter: (c) => c.getValue() + ":" },
          { title: "Backed files", field: "backed_files", hozAlign: "right", width: 120, sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Backed size", field: "backed_bytes", hozAlign: "right", width: 120, sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Excluded files", field: "excluded_files", hozAlign: "right", width: 120, sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Excluded size", field: "excluded_bytes", hozAlign: "right", width: 120, sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Scanned", field: "total", hozAlign: "right", width: 110, sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "BZ authoritative", field: "auth_files", hozAlign: "right", width: 130, sorter: "number", formatter: (c) => c.getValue() == null ? "—" : Number(c.getValue()).toLocaleString(NUM_LOCALE) },
          { title: "Diff", field: "diff", hozAlign: "right", width: 80, formatter: (c) => { const v = c.getValue(); return v == null ? "—" : (v === 0 ? "<span style='color:#16a34a'>+0</span>" : `<span style='color:#dc2626'>${v > 0 ? "+" : ""}${v}</span>`); } },
        ], {
          initialSort: [{ column: "drive", dir: "asc" }],
          onRowDblClick: (_e, row) => { this.bzTreeDrive = row.getData().drive; this.active = "bz-tree"; this.bzOpenTree(); },
          rowMenu: [
            { label: "Browse drive (Tree)", action: (_e, row) => { this.bzTreeDrive = row.getData().drive; this.active = "bz-tree"; this.bzOpenTree(); } },
            { label: "Search this drive", action: (_e, row) => { this.bzSearchDrive = row.getData().drive; this.active = "bz-search"; this.bzLoadSearch(); } },
          ],
        });
      }, "Loading Backblaze dashboard…");
    },
    bzCategories() {
      const fs = (this.bzStats && this.bzStats.filestats) || {};
      const order = ["everything", "video", "music", "pict", "docs", "spreadsheets", "presentations", "financial", "email", "zips", "browserfavorites", "other"];
      return order.filter((k) => fs[k]).map((k) => ({ name: k, num: fs[k].num, bytes: fs[k].bytes }));
    },
    bzCatSearch(cat) {  // click a category -> Search pre-filtered by its extensions ("other" = exclude all known)
      this.bzSearchExt = ""; this.bzSearchExtExclude = ""; this.bzSearchStatus = "all";
      this.bzSearchDrive = ""; this.query = ""; this.bzGroupByFolder = false;
      if (cat === "other") this.bzSearchExtExclude = Object.values(CAT_EXT).join(",");
      else if (CAT_EXT[cat]) this.bzSearchExt = CAT_EXT[cat];
      this.active = "bz-search"; this.bzLoadSearch();
    },
};
