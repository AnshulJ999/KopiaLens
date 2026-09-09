// Snapshots tab: per-snapshot churn/reclaimable, multi-select delete, pin, diff, analysis.
const snapshotsPart = {

    // ---------- Snapshots ----------
    async loadSnapshots(refresh = false) {
      if (!this.snapSource) this.snapSource = "*ALL*";
      if (this.snapSource !== "*ALL*") this.selectedSource = this.snapSource;  // keep tree-drill/mount in sync
      const seq = ++this._snapSeq;
      await this.wrap(async () => {
        const data = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.snapSource)}`);
        if (seq !== this._snapSeq) return;  // a newer load superseded this one
        this.snapshots = data.snapshots || [];
        this.analyzing = !!data.analyzing;
        // the churn/reclaim numbers can belong to the previous index run - say so
        this.snapStaleAnalysis = !!data.analyzed && data.analysis_fresh === false;
        if (!this.snapshots.find((x) => x.id === this.selectedSnapshot)) this.selectedSnapshot = this.snapshots.length ? this.snapshots[this.snapshots.length - 1].id : "";
        const allSrc = this.snapSource === "*ALL*";
        const cols = [
          ...(allSrc ? [{ title: "Source", field: "source", widthGrow: 2, minWidth: 200, formatter: (c) => sourcePath(c.getValue()) }] : []),
          { title: "Start", field: "start_time", width: 185, formatter: (c) => timeCell(c.getValue()) },
          { title: "Logical", field: "logical", width: 100, sorter: "number", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Physical", field: "physical", width: 100, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Accum", field: "accum", width: 72, sorter: "number", formatter: (c) => { const v = c.getValue(); return (v == null) ? "—" : `${Number(v).toFixed(2)}x`; }, hozAlign: "right" },
          { title: "Churn (new)", field: "churn", width: 105, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Reclaimable", field: "reclaimable", width: 110, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", width: 90, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Dirs", field: "dirs", width: 80, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Retention", field: "retention", widthGrow: 2, minWidth: 150, formatter: (c) => (c.getValue() || []).map((r) => `<span class="pill" style="background:#dbeafe;color:#1e40af">${r}</span>`).join("") },
          { title: "Pins", field: "pins", widthGrow: 1, minWidth: 90, formatter: (c) => (c.getValue() || []).map((p) => `<span class="pill" style="background:#fef3c7;color:#92400e">${p}</span>`).join("") },
          { title: "ID", field: "id", width: 200 },
        ];
        const stbl = setTable("#snapshotsTable", this.snapshots, cols, {
          selectableRows: true,  // multi-select for bulk delete / pin / diff
          initialSort: allSrc ? [{ column: "reclaimable", dir: "desc" }] : [{ column: "start_time", dir: "desc" }],
          onRowDblClick: (_e, row) => { const d = row.getData(); this.selectedSource = d.source; this.selectedSnapshot = d.id; this.mountSnapshot = this.selectedSnapshot; this.active = "tree"; this.treeMode = "full"; this.treePrefix = this.sourceRoot(); this.loadTree(); },
          rowMenu: [
            { label: "Browse files", action: (_e, row) => { const d = row.getData(); this.selectedSource = d.source; this.selectedSnapshot = d.id; this.active = "tree"; this.treeMode = "full"; this.treePrefix = this.sourceRoot(); this.loadTree(); } },
            { label: "Restore this snapshot…", action: (_e, row) => this.openRestore(row.getData().root || row.getData().id, "snapshot " + row.getData().id.slice(0, 12)) },
            { label: "Mount snapshot (read-only)", action: (_e, row) => this.mountRow(row.getData().id) },
            { label: "Mount + open in Explorer", action: (_e, row) => this.mountOpen(row.getData().id, this.selectedSource, this.sourceRoot()) },
            { label: "Select all unretained (no retention/pin)", action: () => this.selectUnretained() },
            { label: "Select identical duplicates (keep oldest+newest per content)", action: () => this.selectIdenticalDuplicates() },
            { label: "Delete selected…", action: () => this.planDelete() },
            { label: "Pin selected (protect)", action: () => this.pinSelected(false) },
            { label: "Unpin selected", action: () => this.pinSelected(true) },
            { label: "Diff selected (pick 2)", action: () => this.diffSelected() },
            { label: "Copy snapshot ID", action: (_e, row) => this.copyText(row.getData().id) },
          ],
        });
        if (stbl) stbl.on("rowClick", (e, row) => this._rangeClick(stbl, e, row));  // Shift+Click range select
      }, "Loading snapshots...");
    },

    snapSelected() {
      const el = document.querySelector("#snapshotsTable");
      return (el && el._tabulator) ? el._tabulator.getSelectedData() : [];
    },
    selectUnretained() {  // select snapshots with NO retention reason AND no pin = the safe-to-delete set
      const el = document.querySelector("#snapshotsTable");
      if (!el || !el._tabulator) return;
      let n = 0;
      el._tabulator.getRows().forEach((r) => {
        const d = r.getData();
        const keep = (d.retention && d.retention.length) || (d.pins && d.pins.length);
        if (keep) r.deselect(); else { r.select(); n++; }
      });
      this.actionText = `Selected ${n} unretained snapshot(s) (no retention, no pin). Use 'Delete selected…' to preview/copy the kopia command or delete.`;
    },
    selectIdenticalDuplicates() {  // HYBRID: group the source's snapshots by `root` (byte-identical
      const el = document.querySelector("#snapshotsTable");                       // content = same object),
      if (!el || !el._tabulator) return;                                          // keep OLDEST + NEWEST of
      const tbl = el._tabulator;                                                  // each group, select the
      const groups = {};                                                         // middle. Pinned = never
      for (const s of tbl.getData()) {                                           // selected; retention does
        if (s.root) (groups[s.root] = groups[s.root] || []).push(s);            // NOT block (the whole point
      }                                                                          // is to declutter redundant
      const del = new Set();                                                     // identicals retention keeps).
      for (const root in groups) {
        const g = groups[root];
        if (g.length <= 2) continue;  // oldest+newest already = the whole group; nothing to remove
        g.sort((a, b) => String(a.start_time).localeCompare(String(b.start_time)));
        const keep = new Set([g[0].id, g[g.length - 1].id]);  // oldest + newest endpoints
        for (const s of g) {
          if (keep.has(s.id) || (s.pins && s.pins.length)) continue;  // keep endpoints + pinned
          del.add(s.id);
        }
      }
      let n = 0;
      tbl.getRows().forEach((r) => { if (del.has(r.getData().id)) { r.select(); n++; } else r.deselect(); });
      this.actionText = n
        ? `Selected ${n} identical-duplicate snapshot(s) (kept oldest+newest per content; pinned kept; retained ARE included). Use 'Delete selected…'.`
        : `No identical duplicates to collapse — no content has 3+ snapshots (or 'root' is missing on the rows).`;
    },
    changeSnapSource() {  // persist scope; Tree mirrors it (incl. *ALL*) so the two views stay in parity
      this.klSet({ snap_source: this.snapSource });
      this.selectedSource = this.snapSource;
      this.loadSnapshots();
    },
    planDelete() {  // build a confirm plan from the currently-selected snapshot rows
      const rows = this.snapSelected();
      if (!rows.length) { window.alert("Select one or more snapshots first (click rows / checkboxes)."); return; }
      const reclaimVals = rows.map((r) => r.reclaimable).filter((v) => v !== null && v !== undefined);
      this.deletePlan = {
        ids: rows.map((r) => r.id),
        count: rows.length,
        // Number(): the API can hand these over as strings (a Decimal column serializes quoted),
        // and bare + would concatenate instead of add
        reclaim: reclaimVals.reduce((a, b) => a + Number(b), 0),
        reclaimKnown: reclaimVals.length === rows.length,
        pinned: rows.filter((r) => r.pins && r.pins.length).length,
        retained: rows.filter((r) => r.retention && r.retention.length).length,
        commands: "",
      };
      this.deleteConfirm = "";
      this.deletePlan.preview = [];
      this.deletePlan.combinedReclaim = null;     // accurate union(deleted)-union(kept), filled async
      this.deletePlan.combinedExact = true;
      this.deletePlan.combinedComputing = true;
      this.deletePlan.dry = this.klDryState();
      this.showDelete = true;
      // fetch the exact commands (handles repo2's --config-file) for the copy button
      getJson("/api/actions/snapshot-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_ids: this.deletePlan.ids, confirm: false }) })
        .then((r) => { if (this.deletePlan) this.deletePlan.commands = r.commands || ""; }).catch(() => {});
      // accurate combined reclaim for the WHOLE set (not the per-snapshot sum, which under-counts)
      getJson("/api/snapshots/set-reclaim", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_ids: this.deletePlan.ids }) })
        .then((r) => { if (this.deletePlan) { this.deletePlan.combinedReclaim = r.reclaim; this.deletePlan.combinedExact = r.exact; this.deletePlan.combinedComputing = false; } })
        .catch(() => { if (this.deletePlan) this.deletePlan.combinedComputing = false; });
      // fetch the largest files that would be reclaimed (preview)
      this.loadDeletePreview(false);
    },
    async loadDeletePreview(precise) {
      if (!this.deletePlan) return;
      const ids = this.deletePlan.ids.join(",");
      const r = await getJson(`/api/snapshots/delete-preview?repo=${encodeURIComponent(this.repo)}&ids=${encodeURIComponent(ids)}&precise=${precise ? 1 : 0}&limit=40`).catch(() => null);
      if (r && this.deletePlan) { this.deletePlan.preview = r.files || []; this.deletePlan.precise = precise; }
    },
    deleteDry() {  // run kopia's real snapshot-delete dry run (no --delete) for this selection
      if (!this.deletePlan) return;
      const st = this.deletePlan.dry = (this.deletePlan.dry || this.klDryState());
      return this.klDry("/api/actions/snapshot-delete-dry-run", { repo: this.repo, snapshot_ids: this.deletePlan.ids }, st);
    },
    deleteDryChecked() {
      const st = this.deletePlan && this.deletePlan.dry;
      return !!(st && st.done && st.ok && !st.running);
    },
    async executeDelete() {
      if (!this.deletePlan) return;
      const p = this.deletePlan;
      const warnN = (p.pinned || 0) + (p.retained || 0);
      const ok = await this.klConfirm({
        title: `Delete ${p.count} snapshot(s)?`,
        facts: [
          ["What", "The selected snapshot manifests - those restore points stop existing"],
          ["How many", `${p.count} snapshot(s) · combined reclaim ${fmtPhys(p.combinedReclaim != null ? p.combinedReclaim : p.reclaim)}`],
          ["Where", this.snapSource === "*ALL*" ? "across all sources" : sourcePath(this.snapSource)],
          ["How", `kopia snapshot delete --delete${p.ids.length > 200 ? `, ${Math.ceil(p.ids.length / 200)} call(s)` : ""}`],
        ],
        callout: warnN ? `${p.pinned} pinned and ${p.retained} retained in this selection - deleting removes those restore points (and any pins).` : "",
        note: (this.deleteDryChecked() ? "" : "No dry run has been done for this selection. ")
          + "Space is reclaimed only after maintenance run --full. Files that also exist in other retained snapshots are not affected.",
        command: p.commands, confirmLabel: `Delete ${p.count} snapshot(s)`, danger: true,
      });
      if (!ok) return;
      await this.wrap(async () => {
        const r = await getJson("/api/actions/snapshot-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_ids: p.ids, confirm: true }) });
        this.showDelete = false; this.deletePlan = null; this.deleteConfirm = "";
        this.actionText = `Deleted ${r.deleted} snapshot(s). Run 'kopia maintenance run --full' to reclaim the space.`;
        await this.loadSnapshots(true);
        this.loadDashboard(true);
      }, "Deleting snapshots…");
    },

    async analyzeAll() {  // churn + reclaimable for the whole repo, cached
      await this.wrap(async () => {
        await getJson(`/api/snapshots/analyze-all?repo=${encodeURIComponent(this.repo)}&reclaim=true`, { method: "POST" });
        this.analyzingAll = true;
        const poll = setInterval(async () => {
          const d = await getJson(`/api/snapshots/analyze-status?repo=${encodeURIComponent(this.repo)}`).catch(() => null);
          if (d && !d.analyzing_all) { clearInterval(poll); this.analyzingAll = false; if (this.active === "snapshots") this.loadSnapshots(); }
        }, 5000);
      }, "Analyzing entire repo (churn + reclaimable)…");
    },

    async analyzeSnapshots() {
      if (!this.snapSource) this.snapSource = "*ALL*";
      if (this.snapSource === "*ALL*") return this.analyzeAll();  // all-sources view -> repo-wide
      await this.wrap(async () => {
        await getJson(`/api/snapshots/analyze?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.snapSource)}&reclaim=true`, { method: "POST" });
        this.analyzing = true;
        const poll = setInterval(async () => {
          const d = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.snapSource)}`).catch(() => null);
          if (d && !d.analyzing) { clearInterval(poll); this.loadSnapshots(); }
        }, 4000);
      }, "Analyzing snapshots (churn + reclaimable)...");
    },

    // ---- Snapshots: pin / unpin / diff ----
    async pinSelected(remove) {
      const rows = this.snapSelected();
      if (!rows.length) { window.alert("Select snapshots first."); return; }
      const r = await getJson("/api/snapshots/pin", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_ids: rows.map((x) => x.id), remove }) }).catch((e) => { this.error = e.message; return null; });
      if (!r) return;
      this.copyText(r.command);
      this.actionText = (remove ? "Unpin" : "Pin") + " command copied (run it yourself):\n" + r.command;
    },
    async diffSelected() {
      const rows = this.snapSelected();
      if (rows.length !== 2) { window.alert("Select exactly 2 snapshots to diff."); return; }
      this.showDiff = true; this.diffText = "Running diff…"; this.diffBusy = true;
      try {
        const r = await getJson(`/api/snapshots/diff?repo=${encodeURIComponent(this.repo)}&id1=${encodeURIComponent(rows[0].id)}&id2=${encodeURIComponent(rows[1].id)}&run=true`);
        this.diffText = r.output || r.command || "(no output)";
      } catch (e) { this.diffText = "(diff failed: " + (e.message || e) + ")"; }
      finally { this.diffBusy = false; }
    },
};
