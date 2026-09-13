// Detail panels + removal: file/folder panel, batch union, chunked remove plan.
const detailPart = {

    openRowDetail(d) {
      if (d.type === "folder") {
        this.detail = { kind: "folder", title: d.path, logical: d.size, physical: d.physical, files: d.files };
      } else {
        this.openFileDetail({ source: this.selectedSource, full_path: d.path });
      }
    },

    // ---- Folder detail panel (reuses the file detail modal shell; folder-specific fields) ----
    // Renders instantly from indexed lookups, then patches in the expensive freeable/shared numbers
    // and the effective kopia policy - the same lazy pattern the file panel uses.
    async openFolderPanel(prefix, source = null) {
      if (!prefix) return;
      const seq = ++this._detailSeq;
      this.detail = { kind: "folder", title: prefix, source, loading: true, heavyLoading: true, sources: [], children: [], panelTab: "overview" };
      try {
        const p = new URLSearchParams({ repo: this.repo, prefix });
        if (source) p.set("source", source);
        const sum = await getJson(`/api/folder/summary?${p.toString()}`);
        if (seq !== this._detailSeq) return;
        Object.assign(this.detail, sum, { loading: false });
      } catch (e) {
        if (seq === this._detailSeq) { this.detail.loading = false; this.detail.error = e.message; }
        return;
      }
      // Heavy pass: the chunk walk behind freeable / shared.
      // Do NOT blind-assign over the summary: `physical` means two different things. The summary's
      // is the SUM of each path's latest version (shared storage counted once per file); the
      // detail's is the DEDUPED chunk union across all versions. Overwriting made the headline
      // number silently drop - by 57% on a duplicate-heavy folder - with no explanation.
      getJson("/api/folder/detail", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: this.repo, prefix, source }) })
        .then((r) => {
          if (seq !== this._detailSeq || !this.detail) return;
          this.detail.physical_sum = this.detail.physical;   // keep the fast, additive figure
          Object.assign(this.detail, {
            physical_dedup: r.physical, freeable: r.freeable, shared: r.shared,
            objects: r.objects, all_versions: r.all_versions, heavyLoading: false,
            reclaim_stale: !!r.stale, reclaim_at: r.computed_at,
          });
        })
        .catch(() => { if (seq === this._detailSeq && this.detail) this.detail.heavyLoading = false; });
      // Effective policy for this exact folder (kopia resolves inheritance), via its main source.
      const main = (this.detail.sources || [])[0];
      if (main) {
        getJson(`/api/folder/policy?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(main.source)}&prefix=${encodeURIComponent(prefix)}`)
          .then((r) => { if (seq === this._detailSeq && this.detail) this.detail.effective_policy = r.policy; })
          .catch(() => {});
      }
    },
    folderDeleteFromPanel() {
      if (!this.detail || this.detail.kind !== "folder") return;
      const prefix = this.detail.title;
      const only = this.detail.source || null;   // panel opened scoped to one source keeps that scope
      this.detail = null;
      this.openRemoveFolderPath(prefix, only);
    },
    folderShowFiles() {
      if (!this.detail || this.detail.kind !== "folder") return;
      const prefix = this.detail.title;
      this.detail = null;
      this.groupByFolder = false; this.active = "search"; this.query = prefix; this.searchPage = 1;
      this.loadSearch();
    },

    // ---------- File / folder detail ----------
    async openFileDetail(item) {
      const seq = ++this._detailSeq;
      // Render the panel immediately from the row we already have; the heavy fields (refcount,
      // dedup chunks, purge plan) fill in when they arrive. A second click bumps _detailSeq so a
      // stale in-flight response is dropped (no race).
      this.detail = {
        kind: "file", loading: true, title: item.full_path, source: item.source,
        on_disk: item.in_latest, logical: item.size, physical: item.physical,
        freeable: null, shared: null, chunks: [], others: [], versions: [], purge: [],
        panelTab: "overview",
        // run-bar states, initialised so the markup can deref them freely
        rfBy: "object-id", rfEverywhere: false, rfDry: this.klDryState(),
        purgeDry: this.klDryState(), dupDry: this.klDryState(),
      };
      const base = `repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`;
      try {
        const [detail, vres, purge] = await Promise.all([
          getJson(`/api/search/detail?${base}`),
          getJson(`/api/search/versions?${base}`),
          getJson(`/api/actions/purge-plan?${base}`),
        ]);
        if (seq !== this._detailSeq) return;  // a newer file was opened meanwhile - drop this result
        const items = purge.items || [];
        const purgeRows = (purge.commands || []).map((c, i) => {
          const it = items[i] || {};
          return { cmd: Array.isArray(c) ? c.join(" ") : c, tag: it.pinned ? "PINNED" : ((it.retention && it.retention.length) ? `retained: ${it.retention.join(",")}` : ""), id: it.id };
        });
        Object.assign(this.detail, {   // merge (not replace) so a command built meanwhile (removeFiles) survives
          kind: "file", loading: false,
          title: item.full_path,
          source: detail.source || item.source,
          on_disk: item.in_latest,
          content_id: detail.content_id,
          logical: detail.logical, physical: detail.physical, freeable: detail.freeable,
          shared: detail.shared, physical_all: detail.physical_all_versions, chunk_count: detail.chunk_count,
          chunks: detail.chunks || [],
          others: detail.other_paths || [],
          locations: detail.locations, instances: detail.instances, holders: detail.holders || [],
          reclaim_everywhere: detail.reclaim_everywhere, purge_everywhere_count: detail.purge_everywhere_count,
          purgeEverywhereCmds: detail.purge_everywhere_cmds || "",
          snapsEverywhere: detail.snapshots_everywhere || [],   // the content's full delete-set (ids)
          versions: (vres.versions || []).map((x) => ({ time: humanTime(x.start_time), size: x.size, snapshot_id: x.snapshot_id, content_id: x.content_id })),
          purge: purgeRows,
          purgeCmds: purgeRows.map((r) => r.cmd).join("\n"),
        });
      } catch (err) {
        if (seq === this._detailSeq) this.error = err.message || String(err);
      }
    },

    // ---- Remove file from ALL snapshots (kopia snapshot fix remove-files) ----
    // The run bar drives this section: params (mechanism + scope) rebuild the plan, Dry run
    // runs kopia's real dry run, Execute confirms in-app then commits. Any param change
    // clears the dry-run result - it no longer describes the selection.
    rfSetBy(by) {
      if (!this.detail) return;
      this.detail.rfBy = by;
      this.detail.removeFiles = null;  // the old plan describes the OLD mode - execute must never reuse it
      this.klDryClear(this.detail.rfDry);
      this.rfEnsurePlan();
    },
    rfToggleEverywhere() {
      if (!this.detail) return;
      this.detail.rfEverywhere = !this.detail.rfEverywhere;
      this.detail.removeFiles = null;  // same staleness: scope changed, plan is void
      this.klDryClear(this.detail.rfDry);
      this.rfEnsurePlan();
    },
    async rfEnsurePlan() {  // counts + chunked command text for the current params (index-only, fast)
      if (!this.detail) return null;
      const seq = ++this._rfPlanSeq;  // two rapid switches: the slower stale response must not win
      try {
        const r = await getJson("/api/actions/remove-files", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source: this.detail.source, full_path: this.detail.title, by: this.detail.rfBy || "object-id", everywhere: !!this.detail.rfEverywhere, confirm: false }) });
        if (seq !== this._rfPlanSeq || !this.detail) return null;
        this.detail.removeFiles = { by: this.detail.rfBy || "object-id", count: r.count, everywhere: r.everywhere, dry: r.dry_run, commit: r.commit, batches: r.batches };
        return this.detail.removeFiles;
      } catch (err) {
        if (seq === this._rfPlanSeq) this.error = err.message || String(err);
        return null;
      }
    },
    rfDry() {  // RUN remove-files without --commit; kopia reports what it would rewrite
      if (!this.detail) return;
      const st = this.detail.rfDry = (this.detail.rfDry || this.klDryState());
      return this.klDry("/api/actions/remove-files-dry-run",
        { repo: this.repo, source: this.detail.source, full_path: this.detail.title, by: this.detail.rfBy || "object-id", everywhere: !!this.detail.rfEverywhere }, st);
    },
    rfDryChecked() {  // the "dry run checked" pill state
      const st = this.detail && this.detail.rfDry;
      return !!(st && st.done && st.ok && !st.running);
    },
    async executeRemoveFiles() {
      const plan = (this.detail && this.detail.removeFiles) || await this.rfEnsurePlan();
      if (!plan || !this.detail) return;
      const unit = plan.by === "object-id" ? "content object(s)" : "name(s)";
      const ok = await this.klConfirm({
        title: "Remove this file from all snapshots?",
        facts: [
          ["What", plan.by === "object-id" ? "Every version of this file, matched by exact content" : "Every version of this file, matched by filename"],
          ["How many", `${plan.count} ${unit}`],
          ["Where", plan.everywhere ? "Every source in the repo" : "The sources holding this content"],
          ["How", "kopia snapshot fix remove-files" + (plan.everywhere ? " (no source scope)" : " --source ...")],
        ],
        callout: "Snapshots keep every other file. Space frees on the next maintenance run, not now.",
        note: this.rfDryChecked() ? "" : "No dry run has been done for this selection.",
        command: plan.commit, confirmLabel: "Remove from snapshots", danger: true,
      });
      if (!ok) return;
      await this.wrap(async () => {
        const started = await this.startJob("remove_files", { form: "files", source: this.detail.source, full_path: this.detail.title, by: plan.by, everywhere: !!this.detail.rfEverywhere });
        if (started) {
          this.actionText = "Remove running - progress and live output in the job panel. Space frees on the next maintenance run.";
          this.detail = null;
        }
      }, "Removing file from all snapshots…");
    },

    // ---- Purge plan (path-scoped): delete the whole snapshots carrying this path ----
    purgeIds() { return ((this.detail && this.detail.purge) || []).map((r) => r.id).filter(Boolean); },
    purgeDry() {
      if (!this.detail) return;
      const ids = this.purgeIds();
      if (!ids.length) { this.error = "No snapshots to purge."; return; }
      const st = this.detail.purgeDry = (this.detail.purgeDry || this.klDryState());
      return this.klDry("/api/actions/snapshot-delete-dry-run", { repo: this.repo, snapshot_ids: ids }, st);
    },
    purgeDryChecked() {
      const st = this.detail && this.detail.purgeDry;
      return !!(st && st.done && st.ok && !st.running);
    },
    async executePurge() {
      const ids = this.purgeIds();
      if (!ids.length || !this.detail) return;
      const warns = (this.detail.purge || []).filter((r) => r.tag).length;
      const ok = await this.klConfirm({
        title: `Delete ${ids.length} snapshot(s) to purge this file?`,
        facts: [
          ["What", "Whole snapshots that carry this path - the snapshot itself is deleted, not just the file reference"],
          ["How many", `${ids.length} snapshot(s)`],
          ["Where", sourcePath(this.detail.source || "") || "across sources"],
          ["How", "kopia snapshot delete --delete"],
        ],
        callout: warns ? `${warns} of them are pinned or kept by a retention rule - deleting removes those restore points.` : "",
        note: (this.purgeDryChecked() ? "" : "No dry run has been done for this selection. ")
          + "Space is reclaimed only after maintenance run --full.",
        command: this.detail.purgeCmds, confirmLabel: `Delete ${ids.length} snapshot(s)`, danger: true,
      });
      if (!ok) return;
      await this._deleteSnapsFromPanel(ids);
    },
    async _deleteSnapsFromPanel(ids) {
      await this.wrap(async () => {
        const started = await this.startJob("snapshot_delete", { snapshot_ids: ids });
        if (started) {
          this.actionText = "Delete running - progress and live output in the job panel. Space is reclaimed on maintenance run --full.";
          this.detail = null;
        }
      }, "Deleting snapshots…");
    },

    // ---- Exact duplicate: delete the snapshots carrying this content EVERYWHERE ----
    dupDry() {
      if (!this.detail) return;
      const ids = this.detail.snapsEverywhere || [];
      if (!ids.length) { this.error = "No snapshots to delete."; return; }
      const st = this.detail.dupDry = (this.detail.dupDry || this.klDryState());
      return this.klDry("/api/actions/snapshot-delete-dry-run", { repo: this.repo, snapshot_ids: ids }, st);
    },
    dupDryChecked() {
      const st = this.detail && this.detail.dupDry;
      return !!(st && st.done && st.ok && !st.running);
    },
    async executeDupEverywhere() {
      const ids = (this.detail && this.detail.snapsEverywhere) || [];
      if (!ids.length) return;
      const ok = await this.klConfirm({
        title: `Delete ${ids.length} snapshot(s) to purge this content everywhere?`,
        facts: [
          ["What", "Whole snapshots that carry this exact content, in every location"],
          ["How many", `${ids.length} snapshot(s) · frees ${fmtPhys(this.detail.reclaim_everywhere)}`],
          ["Where", `${this.detail.locations || "?"} source(s) hold this content`],
          ["How", "kopia snapshot delete --delete"],
        ],
        note: (this.dupDryChecked() ? "" : "No dry run has been done for this selection. ")
          + "Space is reclaimed only after maintenance run --full.",
        command: this.detail.purgeEverywhereCmds, confirmLabel: `Delete ${ids.length} snapshot(s)`, danger: true,
      });
      if (!ok) return;
      await this._deleteSnapsFromPanel(ids);
    },

    // ---- Exclusion-pattern suggestions (paste into the KopiaUI policy) ----
    excludePatterns() {
      if (!this.detail || this.detail.kind !== "file") return [];
      const full = this.detail.title || "";
      const root = sourcePath(this.detail.source || "").replace(/[\\/]+$/, "");
      const rel = full.startsWith(root) ? full.slice(root.length).replace(/^[\\/]+/, "") : full;
      const relFwd = rel.replace(/\\/g, "/");
      const name = full.split("\\").pop().split("/").pop();
      const parentRel = relFwd.includes("/") ? relFwd.slice(0, relFwd.lastIndexOf("/")) : "";
      const parentName = parentRel ? parentRel.split("/").pop() : "";
      const dot = name.lastIndexOf(".");
      const out = [
        { label: "This file (any depth)", pat: name },
        { label: "This file (anchored)", pat: "/" + relFwd },
      ];
      if (parentName) {
        out.push({ label: "Containing folder (any depth)", pat: parentName });
        out.push({ label: "Containing folder (anchored)", pat: "/" + parentRel + "/**" });
      }
      if (dot > 0) out.push({ label: "By extension", pat: "*" + name.slice(dot) });
      return out;
    },
    batchExclude(scope) {
      const names = [...new Set(this._selFiles(scope).map((f) => f.full_path.split("\\").pop().split("/").pop()))].filter(Boolean);
      if (!names.length) { window.alert("Select one or more files first."); return; }
      this.copyText(names.join("\n"));
      this.actionText = "Exclude patterns (bare names — paste into KopiaUI policy):\n" + names.join("\n");
    },

    // ---- Batch detail panel (multi-file union: physical/freeable + multi-choice removal) ----
    async batchDetail(scope) {
      const files = this._selFiles(scope);
      if (!files.length) { window.alert("Select one or more files first (Shift+Click for a range)."); return; }
      const seq = ++this._batchSeq;
      this.batch = { loading: true, files, count: files.length, by: "object-id", everywhere: false, stats: null, cmd: null, cmdLoading: false, dry: this.klDryState() };
      this.showBatch = true;
      const r = await getJson("/api/actions/batch-detail", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, files }) }).catch((e) => { this.error = e.message; return null; });
      if (seq !== this._batchSeq) return;  // a newer batch was opened meanwhile - drop this result
      if (this.batch) { this.batch.stats = r; this.batch.loading = false; }
      this.buildBatchCmd();
    },
    async buildBatchCmd() {
      if (!this.batch) return;
      const seq = ++this._batchCmdSeq;
      this.batch.cmd = null; this.batch.cmdLoading = true;
      const body = { repo: this.repo, by: this.batch.by, everywhere: this.batch.everywhere, files: this.batch.files, confirm: false };
      const r = await getJson("/api/actions/remove-files", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).catch((e) => { this.error = e.message; return null; });
      if (seq !== this._batchCmdSeq) return;  // a newer by/everywhere toggle superseded this build
      if (this.batch) { if (r) this.batch.cmd = { dry: r.dry_run, commit: r.commit, count: r.count, everywhere: r.everywhere, batches: r.batches, bat: r.bat, bat_name: r.bat_name }; this.batch.cmdLoading = false; }
    },
    batchSaveBat() {
      const c = this.batch && this.batch.cmd;
      if (!c || !c.bat) return;
      this._downloadText(c.bat_name || "kopialens_remove.bat", c.bat);
      this.actionText = `Downloaded ${c.bat_name} (${c.batches} batch(es)).`;
    },
    setBatchBy(by) { if (this.batch) { this.batch.by = by; this.klDryClear(this.batch.dry); this.buildBatchCmd(); } },
    toggleBatchEverywhere() { if (this.batch) { this.batch.everywhere = !this.batch.everywhere; this.klDryClear(this.batch.dry); this.buildBatchCmd(); } },
    batchDry() {  // run the batch selection's real kopia dry run
      if (!this.batch) return;
      const st = this.batch.dry = (this.batch.dry || this.klDryState());
      return this.klDry("/api/actions/remove-files-dry-run",
        { repo: this.repo, by: this.batch.by, everywhere: this.batch.everywhere, files: this.batch.files }, st);
    },
    batchDryChecked() {
      const st = this.batch && this.batch.dry;
      return !!(st && st.done && st.ok && !st.running);
    },
    async executeBatchRemove() {
      if (!this.batch) return;
      const ok = await this.klConfirm({
        title: `Remove ${this.batch.count} file(s) from all snapshots?`,
        facts: [
          ["What", this.batch.by === "object-id" ? "Every version of each selected file, matched by exact content" : "Every version of each selected file, matched by filename"],
          ["How many", this.batch.cmd ? `${this.batch.cmd.count} ${this.batch.by === "object-id" ? "content object(s)" : "name(s)"} · ${this.batch.cmd.batches} batch(es)` : `${this.batch.count} file(s)`],
          ["Where", this.batch.everywhere ? "Every source in the repo" : "The sources holding this content"],
          ["How", "Chunked kopia snapshot fix remove-files --commit, run in the job panel"],
        ],
        callout: "Snapshots keep every other file. Space frees on the next maintenance run, not now.",
        note: this.batchDryChecked() ? "" : "No dry run has been done for this selection.",
        command: this.batch.cmd ? this.batch.cmd.commit : "", confirmLabel: `Remove ${this.batch.count} file(s)`, danger: true,
      });
      if (!ok) return;
      const b = this.batch;
      await this.wrap(async () => {
        const started = await this.startJob("remove_files", { form: "files", by: b.by, everywhere: b.everywhere, files: b.files });
        if (started) {
          this.actionText = "Remove running - progress and live output in the job panel. Space frees on the next maintenance run.";
          this.showBatch = false; this.batch = null;
        }
      }, "Removing files…");
    },

    // ---- Chunked folder/filter delete (remove-files, respects the current view/filter) ----
    openRemoveFromSearch() {  // delete EVERY file matching the current Search filter (not just selected)
      this._removeBase = {
        repo: this.repo, query: this.query, source: this.searchSource || null,
        ext: this.searchExt || null, ext_exclude: this.searchExtExclude || null,
        deleted_only: this.deletedOnly, over_gb: this.overOneGb ? 1 : 0,
        min_gb: this.minGb ? Number(this.minGb) : 0, max_gb: this.maxGb ? Number(this.maxGb) : 0,
        min_versions: this.minVersions ? Number(this.minVersions) : 0,
        shared_min: this.sharedMin ? Number(this.sharedMin) : 0,
        date_from: this.dateFrom || null, date_to: this.dateTo || null, prefix: null,
        // the same cutoff Search is filtered by - without it, "delete all matching" would sweep
        // files the user narrowed OUT of the view
        first_from: this.recentBackupCutoff() || null,
      };
      this._removeLabel = "current Search filter" + (this.query ? ` · “${this.query}”` : "");
      this.buildRemovePlan();
    },
    openRemoveFolder(row) {  // delete a Tree folder/file, honoring the active Tree filters
      this._removeBase = {
        repo: this.repo, source: this.selectedSource, prefix: row.path,
        deleted_only: this.treeDeletedOnly, over_gb: this.treeOverGb ? 1 : 0, ext: this.treeExt || null,
      };
      this._removeLabel = (row.type === "folder" ? "folder " : "") + (row.path || row.name);
      this.buildRemovePlan();
    },
    openRemoveFolderPath(prefix, source = null) {
      // Cross-source folder delete (By-folder view, Churn, the folder panel). No tree filters, and
      // no source scope unless one is given, so every source holding the folder is covered.
      this._removeBase = { repo: this.repo, source, prefix, deleted_only: false, over_gb: 0, ext: null };
      this._removeLabel = "folder " + prefix + (source ? " (in " + sourcePath(source) + ")" : " (all sources)");
      this.buildRemovePlan();
    },
    buildRemovePlan() {
      // Folder/file-context deletes (a Tree prefix) default to ALL versions: latest-only would
      // silently leave every older version's bytes in old snapshots ("deleted" folder still there).
      const allVers = !!(this._removeBase && this._removeBase.prefix);
      this.removePlan = { loading: true, label: this._removeLabel, all_versions: allVers, everywhere: false, by: "object-id", detail: null, detailLoading: false, job: null, batSaved: null, top_n: 0, dry: this.klDryState() };
      this.showRemove = true;
      this._fetchRemovePlan();
    },
    async _fetchRemovePlan() {
      if (!this.removePlan) return;
      const seq = ++this._removePlanSeq;
      this.removePlan.loading = true;
      this.removePlan.detail = null;  // freeable/shared depends on the matched set
      ++this._removeDetailSeq;
      this.removePlan.detailLoading = false;
      const r = await getJson("/api/actions/remove-plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this._removeBody()) }).catch((e) => { this.error = e.message; return null; });
      if (seq !== this._removePlanSeq || !this.removePlan) return;  // a newer toggle superseded this fetch
      if (r) Object.assign(this.removePlan, r);
      this.removePlan.loading = false;
      // Auto-compute freeable/shared (like the file panel) unless the set is huge (avoid a long walk).
      if (r && (r.file_count || 0) <= 200000) this.removeComputeReclaim();
    },
    toggleRemoveAllVersions() { if (this.removePlan && !this.removePlan.job) { this.removePlan.all_versions = !this.removePlan.all_versions; this.klDryClear(this.removePlan.dry); this._fetchRemovePlan(); } },
    toggleRemoveEverywhere() { if (this.removePlan && !this.removePlan.job) { this.removePlan.everywhere = !this.removePlan.everywhere; this.klDryClear(this.removePlan.dry); this._fetchRemovePlan(); } },
    setRemoveBy(by) { if (this.removePlan && !this.removePlan.job) { this.removePlan.by = by; this.klDryClear(this.removePlan.dry); this._fetchRemovePlan(); } },
    async removeComputeReclaim() {
      if (!this.removePlan) return;
      const seq = ++this._removeDetailSeq;
      this.removePlan.detailLoading = true;
      const r = await getJson("/api/actions/remove-detail", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this._removeBody()) }).catch((e) => { this.error = e.message; return null; });
      if (seq !== this._removeDetailSeq || !this.removePlan) return;  // a newer plan fetch superseded this
      this.removePlan.detail = r; this.removePlan.detailLoading = false;
    },
    _removeBody() {
      const body = { ...this._removeBase, all_versions: this.removePlan.all_versions,
                     everywhere: this.removePlan.everywhere, by: this.removePlan.by };
      if (this.removePlan.top_n) body.cap = this.removePlan.top_n;  // biggest-physical-first is already the order
      return body;
    },
    setRemoveTopN(n) {
      if (!this.removePlan || this.removePlan.job) return;
      this.removePlan.top_n = n || 0;
      this.klDryClear(this.removePlan.dry);
      this._fetchRemovePlan();
    },
    removePlanDry() {  // run the whole matched plan's real kopia dry run
      if (!this.removePlan || this.removePlan.job) return;
      const st = this.removePlan.dry = (this.removePlan.dry || this.klDryState());
      return this.klDry("/api/actions/remove-plan-dry-run", this._removeBody(), st);
    },
    removePlanDryChecked() {
      const st = this.removePlan && this.removePlan.dry;
      return !!(st && st.done && st.ok && !st.running);
    },
    removeCoverage() {  // "these 100 files are 70% of the folder" - the number that makes top-N a decision
      const p = this.removePlan;
      if (!p || !p.total_physical) return null;
      return Math.round((p.physical / p.total_physical) * 100);
    },
    async removeSaveBat() {
      const r = await getJson("/api/actions/remove-bat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this._removeBody()) }).catch((e) => { this.error = e.message; return null; });
      if (!r || !this.removePlan) return;
      this._downloadText(r.filename, r.content);
      this.removePlan.batSaved = { filename: r.filename, batches: r.batches };
      this.actionText = `Downloaded ${r.filename} (${r.batches} batches). Run it; kopia output logs next to it.`;
    },
    async removeRun() {
      if (!this.removePlan) return;
      const p = this.removePlan;
      const scopeRows = p.everywhere
        ? "Every source in the repo (whole-repo sweep)"
        : `Scoped to ${((p.sources_scope || []).length)} source(s): ${(p.sources_scope || []).map((s) => sourcePath(s)).join(", ")}`;
      const ok = await this.klConfirm({
        title: "Run the chunked delete now?",
        facts: [
          ["What", (p.all_versions ? "Every version" : "The latest version") + " of the matched files, biggest first"],
          ["How many", `${Number(p.object_count || 0).toLocaleString()} content objects · ${p.batches} batch(es)`],
          ["Where", scopeRows],
          ["How", "Chunked kopia snapshot fix remove-files --commit, run in the background with progress below"],
        ],
        callout: p.everywhere ? "NOT source-scoped: this sweeps the whole repo." : "",
        note: (this.removePlanDryChecked() ? "" : "No dry run has been done for this selection. ")
          + "Space frees on the next maintenance run.",
        command: p.commit, confirmLabel: "Delete now", danger: true,
      });
      if (!ok) return;
      await this.wrap(async () => {
        const started = await this.startJob("remove_files", { ...this._removeBody(), form: "plan" }, this._removeLabel);
        if (started) {
          this.actionText = "Delete running - progress and live output in the job panel. Space frees on the next maintenance run.";
          this.closeRemove();
        }
      }, "Starting the chunked delete…");
    },
    closeRemove() { if (this.removeJobPoll) { clearInterval(this.removeJobPoll); this.removeJobPoll = null; } this.showRemove = false; this.removePlan = null; },
};
