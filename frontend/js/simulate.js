// Simulate tab: retention-policy reclaim simulation (combined union, not the per-source sum).
const simulatePart = {

    // ---------- Simulate (all sources or one) ----------
    async runSimulate(refresh = false) {
      await this.wrap(async () => {
        const body = { repo: this.repo, refresh, policy: this.policy };
        if (this.simMode === "one") body.source = this.selectedSource;
        const r = await getJson("/api/simulate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (this.simMode === "one") {
          this.simMeta = `${sourcePath(r.source)} · keep ${r.keep_snapshots} / delete ${r.delete_snapshots} of ${r.total_snapshots}`;
          setTable("#simulateTable", [r], [
            { title: "Source", field: "source", widthGrow: 4, minWidth: 240, formatter: (c) => sourcePath(c.getValue()) },
            { title: "Total", field: "total_snapshots", width: 80, hozAlign: "right" },
            { title: "Keep", field: "keep_snapshots", width: 80, hozAlign: "right" },
            { title: "Delete", field: "delete_snapshots", width: 80, hozAlign: "right" },
            { title: "Reclaim", field: "reclaim", width: 110, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          ], { initialSort: [{ column: "reclaim", dir: "desc" }] });
        } else {
          this.simMeta = `Combined reclaim ${fmtBytes(r.combined_reclaim)} · per-source sum ${fmtBytes(r.sum_per_source)} · shared between sources ${fmtBytes(r.shared_between_sources)}` + (r.cached ? " · cached" : "");
          setTable("#simulateTable", r.rows || [], [
            { title: "Source", field: "source", widthGrow: 4, minWidth: 240, sorter: srcSorter, formatter: (c) => sourcePath(c.getValue()) },
            { title: "Total", field: "total_snapshots", width: 80, hozAlign: "right" },
            { title: "Keep", field: "keep_snapshots", width: 80, hozAlign: "right" },
            { title: "Delete", field: "delete_snapshots", width: 80, hozAlign: "right" },
            { title: "Reclaim", field: "reclaim", width: 120, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
            { title: "Pins", field: "pins", width: 64, hozAlign: "right" },
          ], {
            initialSort: [{ column: "reclaim", dir: "desc" }],
            onRowDblClick: (_e, row) => { this.selectedSource = row.getData().source; this.snapSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); },
          });
        }
      }, "Simulating retention...");
    },
};
