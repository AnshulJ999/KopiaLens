// Dashboard tab: per-source coverage table (true physical, freeable, dedup, next snapshots).
const dashboardPart = {

    // ---------- Dashboard ----------
    async loadDashboard(refresh = false) {
      const seq = ++this._dashSeq;
      await this.wrap(async () => {
        const data = await getJson(`/api/dashboard?repo=${encodeURIComponent(this.repo)}&refresh=${refresh}`);
        if (seq !== this._dashSeq) return;  // a newer load superseded this one
        this.dashboardMeta = fmtBytes(data.union_total);
        // Skip re-rendering on the periodic auto-refresh when the data is unchanged (avoids flicker).
        // hash = the backend snaphash: it flips whenever the index moves, so another source's
        // physical/freeable changing is no longer hidden by an unchanged first row.
        // computed_at flips on every cache REBUILD, covering same-hash rebuilds that only moved
        // e.g. next_snapshot times; polls of an untouched cache keep matching and never re-render.
        const sig = `${TIME_FMT}|${NUM_LOCALE}|${_dateStyle}|${_clock}|${this.repo}|${data.hash || ""}|${data.computed_at || ""}|${data.union_total}|${(data.rows || []).length}|${(data.rows && data.rows[0] && data.rows[0].last_time) || ""}`;
        const dEl = document.querySelector("#dashboardTable");
        if (!refresh && dEl && dEl._tabulator && sig === this._dashSig) return;
        this._dashSig = sig;
        setTable("#dashboardTable", data.rows || [], [
          { title: "Source", field: "source", widthGrow: 4, minWidth: 240, sorter: srcSorter, formatter: (c) => sourcePath(c.getValue()) },
          { title: "Snaps", field: "snaps", width: 70, hozAlign: "right" },
          { title: "Files (latest)", field: "files_latest", width: 100, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Paths (all)", field: "files_distinct", width: 95, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Physical", field: "physical", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Accum", field: "accum", width: 75, formatter: (c) => `${fmtFix(c.getValue(), 1)}x`, hozAlign: "right", headerTooltip: "Space all versions take, compared with the latest snapshot alone" },
          { title: "% of repo", field: "repo_pct", width: 82, formatter: (c) => `${fmtFix(c.getValue(), 1)}%`, hozAlign: "right" },
          { title: term("reclaimable"), headerTooltip: termTip("reclaimable"), field: "freeable", width: 110, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Dedup", field: "dedup", width: 72, formatter: (c) => `${fmtFix(c.getValue(), 0)}x`, hozAlign: "right", headerTooltip: "Size of every snapshot added up, compared with the space they take" },
          { title: "Latest size", field: "logical_latest", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Latest physical", field: "physical_latest", width: 110, formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Last snapshot", field: "last_time", width: 165, formatter: (c) => timeCell(c.getValue()) },
          { title: "Next snapshot", field: "next_snapshot", width: 165, formatter: (c) => timeCell(c.getValue()) },
          { title: "Pins", field: "pins", width: 64, hozAlign: "right" },
          { title: "", field: "_actions", download: false, headerSort: false, width: 170, hozAlign: "center",
            formatter: () => "<button class='kl-btn quiet' data-act='snap' style='height:22px'>Snapshot now</button><button class='kl-btn quiet' data-act='policy' style='height:22px'>Policy</button>" },
        ], {
          initialSort: [{ column: "source", dir: "asc" }],
          onRowDblClick: (_e, row) => this.showSourceSnapshots(row.getData().source),
          rowMenu: [
            { label: "Show snapshots", action: (_e, row) => this.showSourceSnapshots(row.getData().source) },
            { label: "Show in Tree", action: (_e, row) => { this.selectedSource = row.getData().source; this.active = "tree"; this.openTree(); } },
            { label: "Simulate this source", action: (_e, row) => { this.selectedSource = row.getData().source; this.simMode = "one"; this.active = "simulate"; this.runSimulate(); } },
            { separator: true },
            { label: "Snapshot now…", action: (_e, row) => this.snapshotNow(row.getData().source) },
            { label: "Open policy", action: (_e, row) => this.openSourcePolicy(row.getData().source) },
            { separator: true },
            { label: "Copy source", action: (_e, row) => this.copyText(row.getData().source) },
          ],
        });
        const dt = document.querySelector("#dashboardTable");
        if (dt && dt._tabulator && !dt._tabulator._klActions) {
          dt._tabulator._klActions = true;
          dt._tabulator.on("cellClick", (e, cell) => {
            const act = e.target && e.target.closest && e.target.closest("[data-act]");
            if (!act) return;
            const src = cell.getRow().getData().source;
            if (act.dataset.act === "snap") this.snapshotNow(src); else this.openSourcePolicy(src);
          });
        }
      }, "Loading dashboard...");
    },

    async snapshotNow(source) {
      const r = await this.klConfirm({
        title: "Take a snapshot now?",
        facts: [["Folder", sourcePath(source)], ["Source", source], ["Repository", this.repoName(this.repo)]],
        note: "Kopia applies this folder's policy: its exclusions, compression and retention.",
        input: { label: "Description (optional)", placeholder: "Before the upgrade" },
        confirmLabel: "Snapshot now", danger: false,
      });
      if (!r) return;
      await this.startJob("snapshot_create", { source, description: r.value || "" }, sourcePath(source));
    },

    showSourceSnapshots(source) { this.snapSource = source; this.active = "snapshots"; this.loadSnapshots(); },

    async openSourcePolicy(source) {
      this.active = "policies";
      await this.loadPolicies();
      this.polTryLeave(source);
    },
};
