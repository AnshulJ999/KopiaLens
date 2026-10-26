// Snapshots tab: per-snapshot churn/reclaimable, multi-select delete, pin, diff, analysis.
const snapshotsPart = {

    // ---------- Snapshots ----------
    async loadSnapshots(refresh = false) {
      if (!this.snapSource) this.snapSource = "*ALL*";
      const seq = ++this._snapSeq;
      await this.wrap(async () => {
        const data = await getJson(`/api/snapshots?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(this.snapSource)}`);
        if (seq !== this._snapSeq) return;  // a newer load superseded this one
        this.snapshots = this.snapSource === "*ALL*" ? (data.snapshots || []).filter((x) => this.ownerMatch(x.source)) : (data.snapshots || []);
        this.analyzing = !!data.analyzing;
        // the churn/reclaim numbers can belong to the previous index run - say so
        this.snapStaleAnalysis = !!data.analyzed && data.analysis_fresh === false;
        if (!this.snapshots.find((x) => x.id === this.selectedSnapshot)) this.selectedSnapshot = this.snapshots.length ? this.snapshots[this.snapshots.length - 1].id : "";
        const allSrc = this.snapSource === "*ALL*";
        const server = this.repoKind === "server";   // no index: the space columns would stay empty
        const spaceCols = ["physical", "accum", "churn", "reclaimable"];
        const cols = [
          ...(allSrc ? [{ title: "Source", field: "source", widthGrow: 2, minWidth: 200, formatter: (c) => this.shortSource(c.getValue()) }] : []),
          { title: "Snap time", field: "start_time", width: 185, formatter: (c) => timeCell(c.getValue(), c) },
          { title: "Age", field: "age", width: 120, visible: false,
            sorter: (a, b, ra, rb) => String(rb.getData().start_time || "").localeCompare(String(ra.getData().start_time || "")),
            formatter: (c) => { const s = c.getRow().getData().start_time; return s ? `<span title="${escHtml(fmtAbs(s))}">${fmtRel(s)}</span>` : "—"; } },
          { title: "Description", field: "description", widthGrow: 1, minWidth: 120, formatter: (c) => escHtml(c.getValue() || "") },
          { title: "Logical", field: "logical", width: 100, sorter: "number", formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" },
          { title: "Physical", field: "physical", width: 100, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Accum", field: "accum", width: 72, sorter: "number", formatter: (c) => { const v = c.getValue(); return (v == null) ? "—" : `${fmtFix(v, 2)}x`; }, hozAlign: "right" },
          { title: "Churn (new)", field: "churn", width: 105, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: term("reclaimable"), headerTooltip: termTip("reclaimable"), field: "reclaimable", width: 110, sorter: "number", formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" },
          { title: "Files", field: "files", width: 90, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Dirs", field: "dirs", width: 80, hozAlign: "right", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Retention", field: "retention", sorter: listSorter, headerSortStartingDir: "desc", accessorDownload: (v) => (v || []).join(", "), widthGrow: 2, minWidth: 150, formatter: (c) => (c.getValue() || []).map((r) => this.retentionPill(r)).join("") },
          { title: "Pins", field: "pins", sorter: listSorter, headerSortStartingDir: "desc", accessorDownload: (v) => (v || []).join(", "), widthGrow: 1, minWidth: 90, formatter: (c) => (c.getValue() || []).map((p) => `<span class="pill" style="background:#fef3c7;color:#92400e">${escHtml(p)}</span>`).join("") },
          { title: "ID", field: "id", width: 200 },
        ].filter((c) => !(server && spaceCols.includes(c.field)));
        // The menu acts on the selection (a right-click outside it selects that row first).
        const rowMenu = (_e, row) => {
          const n = this.snapSelected().length;
          const many = (verb) => (n > 1 ? `${verb} ${countOf(n, "snapshot")}` : verb);
          const d = row.getData();
          const items = [];
          if (n === 1) items.push(
            { label: "Show in Tree", action: () => this.browseSnapshot(d) },
            { label: "Restore this snapshot…", action: () => this.openRestore([{ source: d.source, full_path: sourcePath(d.source), snapshot_id: d.id }], { label: "snapshot " + this.humanTime(d.start_time) }) },
            { label: this.mountOpenLabel(), action: () => this.mountOpen(d.id, d.source, sourcePath(d.source)) },
            ...(server || this.isDemo() ? [] : [this.wmItem({ label: "Edit description…", action: () => this.editDescription(d) }, "non_destructive")]),
            { separator: true });
          if (n === 2) items.push({ label: "Compare 2 snapshots", action: () => this.diffSelected() });
          const pinNames = [...new Set(this.snapSelected().flatMap((x) => x.pins || []))].sort();
          items.push(this.wmItem({ label: many("Pin") + "…", action: () => this.pinSelected(false) }, "non_destructive"));
          if (pinNames.length === 1) items.push(this.wmItem({ label: many("Unpin"), action: () => this.pinSelected(true, pinNames[0]) }, "non_destructive"));
          else if (pinNames.length > 1) items.push({ label: many("Unpin"), menu: pinNames.map((p) => ({ label: p, action: () => this.pinSelected(true, p) })) });
          if (pinNames.length === 1) items.push(this.wmItem({ label: "Rename pin…", action: () => this.renamePin(pinNames[0]) }, "non_destructive"));
          else if (pinNames.length > 1) items.push({ label: "Rename pin", menu: pinNames.map((p) => ({ label: p + "…", action: () => this.renamePin(p) })) });
          if (!server) items.push(
            { separator: true },
            { label: "Select unretained snapshots", action: () => this.selectUnretained() },
            { label: "Select identical duplicates", action: () => this.selectIdenticalDuplicates() },
            { separator: true },
            { label: many("Delete") + "…", action: () => this.planDelete() });
          items.push({ separator: true },
            { label: n === 1 ? "Copy snapshot ID" : "Copy snapshot IDs", action: () => this.copyText(this.snapSelected().map((x) => x.id).join("\n")) });
          return items;
        };
        setTable("#snapshotsTable", this.snapshots, cols, {
          selectableRows: true,  // multi-select for bulk delete / pin / compare
          initialSort: allSrc && !server ? [{ column: "reclaimable", dir: "desc" }] : [{ column: "start_time", dir: "desc" }],
          onRowDblClick: (_e, row) => this.browseSnapshot(row.getData()),
          rowMenu,
          onSelect: (rows) => { this.snapSelCount = rows.length; },
        });
        this.snapSelCount = 0;
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
      this.klNotify(n ? `${countOf(n, "snapshot")} selected that no retention rule or pin keeps.` : "Every snapshot is pinned or kept by retention.", n ? "ok" : "warn");
    },
    selectIdenticalDuplicates() {  // HYBRID: group the source's snapshots by `same` (byte-identical
      const el = document.querySelector("#snapshotsTable");                       // content = same object),
      if (!el || !el._tabulator) return;                                          // keep OLDEST + NEWEST of
      const tbl = el._tabulator;                                                  // each group, select the
      const groups = {};                                                         // middle. Pinned = never
      for (const s of tbl.getData()) {                                           // selected; retention does
        if (s.same) (groups[s.same] = groups[s.same] || []).push(s);            // NOT block (the whole point
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
      this.klNotify(n ? `${countOf(n, "duplicate snapshot")} selected. The oldest and newest of each identical set are kept.` : "No identical duplicates to collapse.", n ? "ok" : "warn");
    },
    changeSnapSource() {  // remembered per repo; the Tree keeps its own choice
      this.snapSourceByRepo = { ...this.snapSourceByRepo, [this.repo]: this.snapSource };
      this.klSet({ snap_source_by_repo: this.snapSourceByRepo });
      this.loadSnapshots();
    },

    // ---- Owner filter (Dashboard + Snapshots): all computers, this computer, or one user@host ----
    async loadClient() {
      const repo = this.repo;
      if (this.repoKind !== "kopia" || this.clientByRepo[repo]) return;
      try {
        const c = await getJson(`/api/client?repo=${encodeURIComponent(repo)}`);
        this.clientByRepo = { ...this.clientByRepo, [repo]: c.user && c.host ? `${c.user}@${c.host}` : "" };
      } catch (e) { /* no filter for "this computer" then */ }
    },
    // A retention reason chip (Settings > Appearance > Retention chips). "By type" uses KopiaUI's
    // colours: latest, daily, weekly, monthly, annual, and one colour for the rest.
    retentionPill(r) {
      const t = escHtml(r);
      if (this.retentionChips === "kopiaui") {
        const kind = ["latest", "daily", "weekly", "monthly", "annual"].find((k) => String(r).startsWith(k + "-")) || "other";
        return `<span class="pill rt rt-${kind}">${t}</span>`;
      }
      if (this.retentionChips === "custom") {
        const c = this.retentionChipColor;
        const [R, G, B] = [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
        const fg = 0.299 * R + 0.587 * G + 0.114 * B > 150 ? "#111" : "#fff";
        return `<span class="pill" style="background:${c};color:${fg}">${t}</span>`;
      }
      return `<span class="pill" style="background:#dbeafe;color:#1e40af">${t}</span>`;
    },

    myOwner() { return this.clientByRepo[this.repo] || ""; },
    ownerOf(source) { return String(source || "").split(":")[0]; },
    owners() { return [...new Set(this.sources.map((s) => this.ownerOf(s)))].sort(); },
    otherOwners() { return this.owners().filter((o) => o !== this.myOwner()); },
    showOwnerFilter() { const o = this.owners(); return o.length > 1 || (o.length === 1 && !!this.myOwner() && o[0] !== this.myOwner()); },
    ownerFilter() { return this.ownerFilterByRepo[this.repo] || "all"; },
    ownerMatch(source) {
      const f = this.ownerFilter();
      if (f === "all") return true;
      return this.ownerOf(source) === (f === "me" ? this.myOwner() : f);
    },
    setOwnerFilter(value) {
      this.ownerFilterByRepo = { ...this.ownerFilterByRepo, [this.repo]: value };
      this.klSet({ owner_filter_by_repo: this.ownerFilterByRepo });
      if (this.snapSource !== "*ALL*" && !this.ownerMatch(this.snapSource)) { this.snapSource = "*ALL*"; this.changeSnapSource(); return; }
      this.activate();
    },

    async editDescription(d) {
      if (!this.wmGuard("non_destructive")) return;
      const r = await this.klConfirm({
        title: "Edit snapshot description",
        facts: [["Folder", this.shortSource(d.source)], ["Snapshot", humanTime(d.start_time)]],
        input: { label: "Description", placeholder: "e.g. before the upgrade", value: d.description || "" },
        confirmLabel: "Save description", danger: false,
      });
      if (!r) return;
      const send = (password) => getJson("/api/engine/snapshot-description", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: this.repo, snapshot_id: d.id, description: r.value || "", password }) });
      try {
        let res = await send(null);
        if (res.need_password) {
          // kopia's saved password for this repository wasn't found; it's used for this edit only.
          const p = await this.klConfirm({ title: "Repository password", body: "Needed to save the description. It isn't stored.",
            input: { label: "Repository password", type: "password" }, confirmLabel: "Save description" });
          if (!p || !p.value) return;
          res = await send(p.value);
        }
        this.klNotify("Description saved");
        this.loadSnapshots();
      } catch (e) { this.klNotify(e.message, "error"); }
    },
    planDelete() {  // build a confirm plan from the currently-selected snapshot rows
      const rows = this.snapSelected();
      if (!rows.length) { this.klNotify("Select one or more snapshots first.", "warn"); return; }
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
        sources: [...new Set(rows.map((r) => r.source || this.snapSource))].filter((s) => s && s !== "*ALL*"),
        times: rows.map((r) => r.start_time).filter(Boolean).sort(),
        // One row per selected snapshot for the modal's list (newest first).
        rows: rows.map((r) => ({ id: r.id, source: r.source || this.snapSource, start_time: r.start_time,
                                 pinned: !!(r.pins && r.pins.length), retention: r.retention || [] }))
                  .sort((a, b) => String(b.start_time).localeCompare(String(a.start_time))),
        commands: "",
      };
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
      this.deletePlan.previewLoading = true;
      this.deletePlan.previewError = false;
      const r = await getJson(`/api/snapshots/delete-preview?repo=${encodeURIComponent(this.repo)}&ids=${encodeURIComponent(ids)}&precise=${precise ? 1 : 0}&limit=40`)
        .catch((e) => { if (this.deletePlan) { this.deletePlan.previewError = true; this.klNotify(e.message || String(e), "warn"); } return null; });
      if (!this.deletePlan) return;
      this.deletePlan.previewLoading = false;
      if (r) { this.deletePlan.preview = r.files || []; this.deletePlan.precise = precise; this.deletePlan.previewExact = !!r.exact; }
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
      if (!this.deletePlan || !this.wmGuard("full")) return;
      const p = this.deletePlan;
      const warnN = (p.pinned || 0) + (p.retained || 0);
      const ok = await this.klConfirm({
        title: `Delete ${p.count} snapshot${p.count === 1 ? "" : "s"}?`,
        facts: [
          ["Snapshots", `${p.count}` + (p.times.length ? ` · ${humanTime(p.times[0])}` + (p.times.length > 1 ? ` to ${humanTime(p.times[p.times.length - 1])}` : "") : "")],
          [p.sources.length > 1 ? "Sources" : "Source", p.sources.length ? p.sources.map((s) => this.shortSource(s)).join(", ") : this.shortSource(this.snapSource)],
          [term("reclaimable"), fmtPhys(p.combinedReclaim != null ? p.combinedReclaim : p.reclaim) + " after maintenance"],
          ["Kept", `${p.pinned} pinned · ${p.retained} kept by retention`],
        ],
        callout: warnN ? "Pinned and retained snapshots are restore points you chose to keep." : "",
        note: (this.deleteDryChecked() ? "" : "No dry run yet. ") + "Files also in other snapshots stay.",
        command: p.commands, confirmLabel: `Delete ${p.count} snapshot${p.count === 1 ? "" : "s"}`, danger: true,
      });
      if (!ok) return;
      await this.wrap(async () => {
        const started = await this.startJob("snapshot_delete", { snapshot_ids: p.ids });
        if (started) {
          this.showDelete = false; this.deletePlan = null;
        }
      }, "Deleting snapshots…");
    },

    // One analyze followed at a time, for the repo and source it was started on; a repo switch stops it.
    _stopAnalyzePoll() {
      if (this._analyzePoll) { clearInterval(this._analyzePoll); this._analyzePoll = null; }
      this.analyzingAll = false;
    },
    async analyzeAll() {  // churn + reclaimable for the whole repo, cached
      if (this._analyzePoll) return;
      const repo = this.repo;
      await this.wrap(async () => {
        await getJson(`/api/snapshots/analyze-all?repo=${encodeURIComponent(repo)}&reclaim=true`, { method: "POST" });
        this.analyzingAll = true;
        this._analyzePoll = setInterval(async () => {
          if (this.repo !== repo) return this._stopAnalyzePoll();
          const d = await getJson(`/api/snapshots/analyze-status?repo=${encodeURIComponent(repo)}`).catch(() => null);
          if (d && !d.analyzing_all && this.repo === repo) { this._stopAnalyzePoll(); if (this.active === "snapshots") this.loadSnapshots(); }
        }, 5000);
      }, "Analyzing every snapshot…");
    },

    async analyzeSnapshots() {
      if (!this.snapSource) this.snapSource = "*ALL*";
      if (this.snapSource === "*ALL*") return this.analyzeAll();  // all-sources view -> repo-wide
      if (this._analyzePoll) return;
      const repo = this.repo, source = this.snapSource;
      await this.wrap(async () => {
        await getJson(`/api/snapshots/analyze?repo=${encodeURIComponent(repo)}&source=${encodeURIComponent(source)}&reclaim=true`, { method: "POST" });
        this.analyzing = true;
        this._analyzePoll = setInterval(async () => {
          if (this.repo !== repo) return this._stopAnalyzePoll();
          const d = await getJson(`/api/snapshots?repo=${encodeURIComponent(repo)}&source=${encodeURIComponent(source)}`).catch(() => null);
          if (d && !d.analyzing && this.repo === repo) { this._stopAnalyzePoll(); this.loadSnapshots(); }
        }, 4000);
      }, "Analyzing snapshots…");
    },

    // ---- Snapshots: pin / unpin / diff ----
    // A pin is a name kopia keeps on the snapshot; retention never removes a pinned snapshot.
    async pinSelected(remove, name = "do-not-delete") {
      if (!this.wmGuard("non_destructive")) return;
      const rows = this.snapSelected();
      if (!rows.length) { this.klNotify("Select one or more snapshots first.", "warn"); return; }
      const n = rows.length;
      const facts = [
        ["Snapshots", rows.slice(0, 4).map((x) => `${this.shortSource(x.source || this.snapSource)} · ${humanTime(x.start_time || x.startTime)}`).join("; ") + (n > 4 ? `; and ${n - 4} more` : "")],
      ];
      if (remove) facts.push(["Pin", name], ["Pinned now", String(rows.filter((x) => (x.pins || []).includes(name)).length)]);
      const r = await this.klConfirm({
        title: `${remove ? "Unpin" : "Pin"} ${countOf(n, "snapshot")}?`, facts,
        input: remove ? null : { label: "Pin name", value: name },
        note: remove ? "Unpinned snapshots follow retention again and can expire at the next cleanup." : "Pinned snapshots are never removed by retention.",
        confirmLabel: `${remove ? "Unpin" : "Pin"} ${countOf(n, "snapshot")}`, danger: remove,
      });
      if (!r) return;
      const pin = remove ? name : ((r.value || "").trim() || "do-not-delete");
      await this.startJob("snapshot_pin", { snapshot_ids: rows.map((x) => x.id), remove, pin_name: pin }, `${remove ? "unpin" : "pin"} ${countOf(n, "snapshot")}`);
    },
    async renamePin(oldName) {
      if (!this.wmGuard("non_destructive")) return;
      const rows = this.snapSelected().filter((x) => (x.pins || []).includes(oldName));
      if (!rows.length) return;
      const r = await this.klConfirm({
        title: `Rename the pin ${oldName}?`, facts: [["Snapshots with it", String(rows.length)]],
        input: { label: "New name", value: oldName }, confirmLabel: "Rename pin", danger: false,
      });
      const pin = r && (r.value || "").trim();
      if (!pin || pin === oldName) return;
      await this.startJob("snapshot_pin", { snapshot_ids: rows.map((x) => x.id), pin_name: pin, rename_from: oldName }, `rename pin on ${countOf(rows.length, "snapshot")}`);
    },
    async diffSelected() {
      const rows = this.snapSelected();
      if (rows.length !== 2) { this.klNotify("Select 2 snapshots to compare.", "warn"); return; }
      this.showDiff = true; this.diffText = "Running diff…"; this.diffBusy = true;
      try {
        const r = await getJson(`/api/snapshots/diff?repo=${encodeURIComponent(this.repo)}&id1=${encodeURIComponent(rows[0].id)}&id2=${encodeURIComponent(rows[1].id)}&run=true`);
        this.diffText = r.output || r.command || "(no output)";
      } catch (e) { this.diffText = "(diff failed: " + (e.message || e) + ")"; }
      finally { this.diffBusy = false; }
    },
};
