// Dashboard tab: per-source coverage table (true physical, freeable, dedup, next snapshots).
const dashboardPart = {

    // ---------- Dashboard ----------
    async loadDashboard(refresh = false) {
      const seq = ++this._dashSeq;
      await this.wrap(async () => {
        const data = await getJson(`/api/dashboard?repo=${encodeURIComponent(this.repo)}&refresh=${refresh}`);
        if (seq !== this._dashSeq) return;  // a newer load superseded this one
        this.dashboardMeta = fmtBytes(data.union_total);
        // Stale numbers come with a recompute in the background: look again shortly.
        if (this._dashStaleTimer) clearTimeout(this._dashStaleTimer);
        if ((data.stale || data.stale_build_in_progress) && this.active === "dashboard") {
          this._dashStaleTries = (this._dashStaleTries || 0) + 1;
          if (this._dashStaleTries <= 10) this._dashStaleTimer = setTimeout(() => this.loadDashboard(), 2000);
        } else this._dashStaleTries = 0;
        // Skip re-rendering on the periodic auto-refresh when the data is unchanged (avoids flicker).
        // hash = the backend snaphash: it flips whenever the index moves, so another source's
        // physical/freeable changing is no longer hidden by an unchanged first row.
        // computed_at flips on every cache REBUILD, covering same-hash rebuilds that only moved
        // e.g. next_snapshot times; polls of an untouched cache keep matching and never re-render.
        const rows = (data.rows || []).filter((r) => this.ownerMatch(r.source)).map((r) => ({ ...r, owner: this.ownerOf(r.source) }));
        const sig = `${this.dashPageSize}|${TIME_FMT}|${NUM_LOCALE}|${_dateStyle}|${_clock}|${this.repo}|${this.ownerFilter()}|${this.myOwner()}|${data.hash || ""}|${data.computed_at || ""}|${data.union_total}|${(data.rows || []).length}|${(data.rows && data.rows[0] && data.rows[0].last_time) || ""}`;
        const dEl = document.querySelector("#dashboardTable");
        if (!refresh && dEl && dEl._tabulator && sig === this._dashSig) return;
        this._dashSig = sig;
        setTable("#dashboardTable", rows, [
          { title: "Source", field: "source", widthGrow: 4, minWidth: 240, sorter: srcSorter, formatter: (c) => this.shortSource(c.getValue()), headerTooltip: "The folder kopia backs up" },
          { title: "Owner", field: "owner", width: 150, headerTooltip: "The user and computer that back it up" },
          { title: "Snaps", field: "snaps", width: 70, hozAlign: "right", headerTooltip: "Snapshots of this folder" },
          { title: "Files", field: "files_latest", width: 100, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE), headerTooltip: "Files in the latest snapshot" },
          { title: "Files (all)", field: "files_distinct", width: 95, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE), headerTooltip: "Every file in any snapshot of this folder" },
          { title: term("physicalAll"), field: "physical", width: 105, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right", headerTooltip: "Space every snapshot of this folder takes in the repository" },
          { title: "Accum", field: "accum", width: 75, formatter: (c) => `${fmtFix(c.getValue(), 1)}x`, hozAlign: "right", headerTooltip: "Space all versions take, compared with the latest snapshot alone" },
          { title: "% of repo", field: "repo_pct", width: 82, formatter: (c) => `${fmtFix(c.getValue(), 1)}%`, hozAlign: "right", headerTooltip: "This folder's share of the space the repository takes" },
          { title: term("reclaimable"), headerTooltip: termTip("reclaimable"), field: "freeable", width: 110, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Dedup", field: "dedup", width: 72, formatter: (c) => `${fmtFix(c.getValue(), 0)}x`, hozAlign: "right", headerTooltip: "Size of every snapshot added up, compared with the space they take" },
          { title: "Size", field: "logical_latest", width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right", headerTooltip: "Size of the latest snapshot, as on disk" },
          { title: term("physical"), field: "physical_latest", width: 100, formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right", headerTooltip: "Space the latest snapshot takes in the repository" },
          { title: "Last snapshot", field: "last_time", width: 165, formatter: (c) => timeCell(c.getValue(), c), headerTooltip: "When the newest snapshot was taken" },
          { title: "Next snapshot", field: "next_snapshot", width: 190, formatter: (c) => this.nextCellHtml(c.getRow().getData(), c), headerTooltip: "When the schedule takes the next one" },
          { title: "Pins", field: "pins", width: 64, hozAlign: "right", headerTooltip: "Pinned snapshots, which retention never removes" },
          { title: "Actions", field: "_actions", download: false, headerSort: false, width: 200, hozAlign: "center",
            formatter: (c) => {
              const src = c.getRow().getData().source;
              const live = this.sourceStatusHtml(src);
              if (live && this.engineSrc[src] && this.engineSrc[src].status === "UPLOADING")
                return live + " <button class='kl-btn quiet' data-act='cancel' style='height:22px'>Cancel</button>";
              if (live) return live;
              return "<button class='kl-btn quiet' data-act='snap' style='height:22px'>Snapshot now</button><button class='kl-btn quiet' data-act='policy' style='height:22px'>Policy</button>";
            } },
        ], {
          initialSort: [{ column: "source", dir: "asc" }],
          ...(this.dashPageSize !== "all" ? { pagination: true, paginationSize: Number(this.dashPageSize), paginationCounter: "rows" } : {}),
          onRowDblClick: (_e, row) => this.showSourceSnapshots(row.getData().source),
          rowMenu: (_e, row) => {
            const src = row.getData().source;
            return [
              { label: "Show snapshots", action: () => this.showSourceSnapshots(src) },
              { label: "Show in Tree", action: () => { this.selectedSource = src; this.active = "tree"; this.openTree(); } },
              { label: "Simulate this source", action: () => this.simulateSource(src) },
              { separator: true },
              this.wmItem({ label: "Snapshot now…", action: () => this.snapshotNowAny(src) }, "non_destructive"),
              ...this.engineSourceMenu(src),
              { label: "Open policy", action: () => this.openSourcePolicy(src) },
              { separator: true },
              { label: "Copy source", action: () => this.copyText(src) },
              { separator: true },
              this.wmItem({ label: "Delete this source…", action: () => this.openDeleteSource(src) }, "full"),
            ];
          },
        });
        this.loadEngineSources();
        const dt = document.querySelector("#dashboardTable");
        if (dt && dt._tabulator && !dt._tabulator._klActions) {
          dt._tabulator._klActions = true;
          dt._tabulator.on("cellClick", (e, cell) => {
            const act = e.target && e.target.closest && e.target.closest("[data-act]");
            if (!act) return;
            const src = cell.getRow().getData().source;
            if (act.dataset.act === "snap") this.snapshotNowAny(src);
            else if (act.dataset.act === "cancel") this.engineCancel(src);
            else if (act.dataset.act === "sresume") this.engineSourceResume(src);
            else this.openSourcePolicy(src);
          });
        }
      }, "Loading dashboard...");
    },

    // Rows per page ("…" menu): all, or pages of 25 / 50 / 100 for repositories with many folders.
    setDashPageSize(v) {
      this.dashPageSize = v;
      this.saveKl("dashboard_page_size");
      this._dashSig = "";
      this.loadDashboard();
    },

    // Through KopiaLens's kopia server when it runs this repository's backups (live progress in
    // the table, a task in Tasks), otherwise as a job on the command line.
    // A paused folder is still backed up once on request: kopia's server skips paused folders, so
    // that one run goes through the command line and the schedule stays paused.
    snapshotNowAny(source) { return this.engineRuns() && !this.engineSrcPaused(source) ? this.engineSnapshotNow(source) : this.snapshotNow(source); },

    async snapshotNow(source) {
      let description = "";
      if (this.askSnapshotNow) {
        const r = await this.klConfirm({
          title: "Take a snapshot now?",
          facts: [["Folder", sourcePath(source)], ["Source", source], ["Repository", this.repoName(this.repo)],
            ...(this.engineSrcPaused(source) ? [["Schedule", "Stays paused"]] : [])],
          note: "Kopia applies this folder's policy: its exclusions, compression and retention.",
          input: { label: "Description (optional)", placeholder: "e.g. before the upgrade" },
          confirmLabel: "Snapshot now", danger: false, dontAsk: true,
        });
        if (!r) return;
        this.snapshotNowDontAsk(r);
        description = r.value || "";
      }
      await this.startJob("snapshot_create", { source, description }, sourcePath(source));
    },

    // "Don't ask again" in the Snapshot now confirm turns the setting off (Settings > General).
    snapshotNowDontAsk(r) {
      if (!r || !r.dontAsk) return;
      this.askSnapshotNow = false;
      this.klSet({ ask_snapshot_now: false });
      this.klNotify("Snapshot now won't ask again. Change it in Settings > General.");
    },

    // Dashboard Refresh: read kopia's snapshot list again (new snapshots, no re-index), then recompute.
    async refreshDashboard() {
      await this.refreshBase(true);
      await this.loadDashboard(true);
    },

    showSourceSnapshots(source) { this.snapSource = source; this.active = "snapshots"; this.loadSnapshots(); },

    async openSourcePolicy(source) {
      this.active = "policies";
      await this.loadPolicies();
      this.polTryLeave(source);
    },
};
