// Simulate tab: retention-policy reclaim simulation (combined union, not the per-source sum).
// Simulate and Snapshots share one source (snapSource); the Tree keeps its own.
const simulatePart = {
    openSimulateFromSnapshots() {
      this.active = "simulate";
      this.activate();
    },
    // "Simulate this source" from the Dashboard or Churn: that source becomes Snapshots' too.
    simulateSource(source) {
      this.snapSource = source;
      this.snapSourceByRepo = { ...this.snapSourceByRepo, [this.repo]: source };
      this.klSet({ snap_source_by_repo: this.snapSourceByRepo });
      this.simEmpty = true; this.simMeta = "";
      this._simRunOnOpen = true;
      this.active = "simulate";
      this.activate();
    },
    simSourceChanged() {
      this.snapSourceByRepo = { ...this.snapSourceByRepo, [this.repo]: this.snapSource };
      this.klSet({ snap_source_by_repo: this.snapSourceByRepo });
      this.simEmpty = true; this.simMeta = "";
      setTable("#simulateTable", [], []);
      this.openSimulate();
    },


    // Opening the tab: the policy last tried on this repo (else its Global retention), and the
    // saved result for it when one exists. Nothing heavy runs until Run.
    async openSimulate() {
      const repo = this.repo;
      this.simMode = this.snapSource && this.snapSource !== "*ALL*" ? "one" : "all";
      if (this._simRepo !== repo) {
        this._simRepo = repo;
        const saved = this.simPolicyByRepo[repo];
        if (saved) this.policy = { ...this.policy, ...saved };
        else {
          const d = await getJson(`/api/policies/detail?repo=${encodeURIComponent(repo)}&target=global`).catch(() => null);
          if (repo !== this.repo) return;
          const ret = (d && d.defined && d.defined.retention) || {};
          const kopiaDefault = { latest: 10, hourly: 48, daily: 7, weekly: 4, monthly: 24, annual: 3 };
          const field = { latest: "keepLatest", hourly: "keepHourly", daily: "keepDaily", weekly: "keepWeekly", monthly: "keepMonthly", annual: "keepAnnual" };
          this.policy = Object.fromEntries(this.policyKeys.map((k) => [k, ret[field[k]] != null ? Number(ret[field[k]]) : kopiaDefault[k]]));
        }
        this.simEmpty = true; this.simMeta = "";
      }
      if (this._simRunOnOpen) { this._simRunOnOpen = false; return this.runSimulate(); }
      if (this.simMode === "all" && this.simEmpty) this.runSimulate(false, true);
    },
    simPolicyLabel(k) { return { latest: "Latest", hourly: "Hourly", daily: "Daily", weekly: "Weekly", monthly: "Monthly", annual: "Annual" }[k] || k; },

    // ---------- Simulate (all sources or one) ----------
    async runSimulate(refresh = false, cachedOnly = false) {
      if (!cachedOnly) {   // the policy tried last is remembered per repo
        this.simPolicyByRepo = { ...this.simPolicyByRepo, [this.repo]: { ...this.policy } };
        this.klSet({ sim_policy_by_repo: this.simPolicyByRepo });
      }
      await this.wrap(async () => {
        const body = { repo: this.repo, refresh, policy: this.policy, cached_only: cachedOnly };
        if (this.simMode === "one") body.source = this.snapSource;
        const r = await getJson("/api/simulate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (r && r.empty) return;   // no saved result for this policy yet
        this.simEmpty = false;
        if (this.simMode === "one") {
          this.simMeta = `${sourcePath(r.source)} · keeps ${r.keep_snapshots} of ${countOf(r.total_snapshots, "snapshot")}, deletes ${r.delete_snapshots}`;
          setTable("#simulateTable", [r], [
            { title: "Source", field: "source", widthGrow: 4, minWidth: 240, formatter: (c) => sourcePath(c.getValue()) },
            { title: "Total", field: "total_snapshots", width: 80, hozAlign: "right" },
            { title: "Keep", field: "keep_snapshots", width: 80, hozAlign: "right" },
            { title: "Delete", field: "delete_snapshots", width: 80, hozAlign: "right" },
            { title: term("reclaimable"), headerTooltip: termTip("reclaimable"), field: "reclaim", width: 120, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          ], { initialSort: [{ column: "reclaim", dir: "desc" }] });
        } else {
          this.simMeta = `Frees ${fmtBytes(r.combined_reclaim)} in total · the sources' own figures add up to ${fmtBytes(r.sum_per_source)}; ${fmtBytes(r.shared_between_sources)} is shared between sources`
            + (r.cached ? ` · saved result${r.computed_at ? " from " + humanTime(r.computed_at) : ""}` : "");
          setTable("#simulateTable", r.rows || [], [
            { title: "Source", field: "source", widthGrow: 4, minWidth: 240, sorter: srcSorter, formatter: (c) => sourcePath(c.getValue()) },
            { title: "Total", field: "total_snapshots", width: 80, hozAlign: "right" },
            { title: "Keep", field: "keep_snapshots", width: 80, hozAlign: "right" },
            { title: "Delete", field: "delete_snapshots", width: 80, hozAlign: "right" },
            { title: term("reclaimable"), headerTooltip: "What this source alone gives back. Data shared with other sources is only in the total.", field: "reclaim", width: 120, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
            { title: "Pins", field: "pins", width: 64, hozAlign: "right" },
          ], {
            initialSort: [{ column: "reclaim", dir: "desc" }],
            onRowDblClick: (_e, row) => this.showSourceSnapshots(row.getData().source),
          });
        }
      }, cachedOnly ? "" : "Simulating retention…");
    },
};
