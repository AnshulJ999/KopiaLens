// Detail panels + removal: file/folder panel, chunked remove plan.
const detailPart = {

    openRowDetail(d, source = null) {
      if (d.type === "folder") {
        this.detail = { kind: "folder", title: d.path, logical: d.size, physical: d.physical, files: d.files };
      } else {
        this.openFileDetail({ source: source || this.selectedSource, full_path: d.path });
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
      const main = (this.detail.sources || []).find((s) => s.relation === "covers") || (this.detail.sources || [])[0];
      if (main) {
        getJson(`/api/folder/policy?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(main.source)}&prefix=${encodeURIComponent(prefix)}`)
          .then((r) => { if (seq === this._detailSeq && this.detail) this.detail.effective_policy = r.policy; })
          .catch(() => {});
        this._loadPathStatus(seq, main.source, prefix, true);
      }
    },

    _loadPathStatus(seq, source, path, isFolder) {
      const p = new URLSearchParams({ repo: this.repo, source, path, is_folder: isFolder ? "true" : "false" });
      getJson(`/api/policies/path-status?${p.toString()}`)
        .then((r) => { if (seq === this._detailSeq && this.detail) this.detail.pathStatus = r; })
        .catch(() => { if (seq === this._detailSeq && this.detail) this.detail.pathStatus = { error: true }; });
    },

    // ---- Overview "Backup history" rows ----
    _rulesRow() {
      const s = this.detail && this.detail.pathStatus;
      if (!s) return "…";
      if (s.error) return "Couldn't read the policies";
      const from = s.owner === "global" ? "Global" : sourcePath(s.owner || "") || s.owner;
      const list = s.origin === "none" ? "No rules apply"
        : s.origin === "own" ? `Own list of ${s.rule_count} rule${s.rule_count === 1 ? "" : "s"}`
        : `Inherits ${s.rule_count} rule${s.rule_count === 1 ? "" : "s"} from ${from}`;
      if (s.origin === "none") return list;
      return list + (s.excluded ? " · a rule leaves this out" : " · none match");
    },
    fileHistoryRows() {
      const d = this.detail;
      if (!d || d.kind !== "file" || !(d.versions || []).length) return [];
      const vs = d.versions;
      const first = toDate(vs[vs.length - 1].start), last = toDate(vs[0].start);
      const changes = new Set(vs.map((v) => v.content_id)).size;
      const days = Math.max(1, Math.round((last - first) / 86400000));
      const copies = this.copyCount();
      return [
        ["First backed up", fmtDate(vs[vs.length - 1].start)],
        ["Last changed", humanTime(vs[0].start)],
        ["Changes", changes <= 1 ? "Unchanged since the first backup" : `Changed ${changes - 1} time${changes === 2 ? "" : "s"} in ${days} day${days === 1 ? "" : "s"}`],
        ["Copies", copies > 0 ? `${copies} identical cop${copies === 1 ? "y" : "ies"} elsewhere` : "None elsewhere"],
        ["Rules", this._rulesRow()],
      ];
    },
    // Identical copies at OTHER paths. The same path held by nested backups is not a copy.
    copyCount() {
      const d = this.detail;
      if (!d || d.kind !== "file") return 0;
      return new Set((d.others || []).map((o) => o.full_path).filter((p) => p !== d.title)).size;
    },
    folderHistoryRows() {
      const d = this.detail;
      if (!d || d.kind !== "folder") return [];
      const rows = [
        ["First backed up", d.first_seen ? fmtDate(d.first_seen) : "—"],
        ["Last backed up", d.last_seen ? humanTime(d.last_seen) : "—"],
        ["Files", `${Number(d.files || 0).toLocaleString()} · ${Number(d.deleted_files || 0).toLocaleString()} deleted from disk`],
        ["Versions", Number(d.versions || 0).toLocaleString()],
      ];
      const w = d.last_30_days;
      if (w) rows.push(["Last 30 days", `+${fmtBytes(w.bytes)} new data · ${fmtBytes(w.bytes / 30)} a day`]);
      rows.push(["Rules", this._rulesRow()]);
      return rows;
    },

    // Size of each version, oldest to newest (the newest 200).
    versionBars() {
      const vs = ((this.detail && this.detail.versions) || []).slice(0, 200).reverse();
      const max = Math.max(1, ...vs.map((v) => Number(v.size || 0)));
      return vs.map((v, i) => ({ h: Math.max(3, Math.round((Number(v.size || 0) / max) * 100)),
        last: i === vs.length - 1, tip: `${fmtBytes(v.size || 0)} · ${v.time}` }));
    },

    // ---- Folder Contents tab ----
    async folderLoadContents() {
      const d = this.detail;
      if (!d || d.kind !== "folder" || d.contents) return;
      const seq = ++this._contentsSeq;
      const title = d.title;
      d.contentsLoading = true;
      d.contentsSort = d.contentsSort || "folders";
      const p = new URLSearchParams({ repo: this.repo, prefix: title });
      if (d.source) p.set("source", d.source);
      try {
        const r = await getJson(`/api/folder/contents?${p.toString()}`);
        if (seq !== this._contentsSeq || !this.detail || this.detail.title !== title) return;
        this.detail.contents = r.rows || [];
      } catch (e) {
        if (seq === this._contentsSeq && this.detail) this.detail.contentsError = e.message || String(e);
      } finally {
        if (seq === this._contentsSeq && this.detail) this.detail.contentsLoading = false;
      }
    },
    folderContentRows() {
      const d = this.detail;
      const rows = [...((d && d.contents) || [])];
      if (d && d.contentsSort === "largest") rows.sort((a, b) => b.physical - a.physical);
      return rows;
    },
    folderOpenChild(r) {
      const d = this.detail;
      if (!d) return;
      const sep = d.title.includes("\\") || !d.title.includes("/") ? "\\" : "/";
      const path = d.title.replace(/[\\/]+$/, "") + sep + r.child;
      if (r.is_folder) this.openFolderPanel(path, d.source || null);
      else this.openFileDetail({ source: r.source, full_path: path });
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
        purgeDry: this.klDryState(),
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
          return { cmd: Array.isArray(c) ? c.join(" ") : c, time: it.start_time, tag: it.pinned ? "pinned" : ((it.retention && it.retention.length) ? `kept: ${it.retention.join(", ")}` : ""), id: it.id };
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
          reclaim_everywhere: detail.reclaim_everywhere,
          versions: (vres.versions || []).map((x) => ({ time: humanTime(x.start_time), start: x.start_time, size: x.size, snapshot_id: x.snapshot_id, content_id: x.content_id })),
          purge: purgeRows,
          purgeCmds: purgeRows.map((r) => r.cmd).join("\n"),
        });
        this._loadPathStatus(seq, this.detail.source, item.full_path, false);
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
        this.detail.removeFiles = { by: this.detail.rfBy || "object-id", count: r.count, everywhere: r.everywhere, sources: r.sources || [], dry: r.dry_run, commit: r.commit, batches: r.batches };
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
      const ok = await this.klConfirm({
        title: `Remove ${this.detail.title.split(/[\\/]/).pop()} from snapshots?`,
        facts: [
          ["File", this.detail.title],
          ["Match", plan.by === "object-id" ? `Exact content · ${plan.count} version${plan.count === 1 ? "" : "s"}` : "File name · every file with this name"],
          ["Backups", plan.everywhere ? "Every backup in the repo" : plan.sources.map((s) => sourcePath(s)).join(", ")],
        ],
        callout: plan.by === "object-id"
          ? (this.copyCount() ? `${this.copyCount()} identical cop${this.copyCount() === 1 ? "y" : "ies"} at other paths go too.` : "")
          : `Every file named ${this.detail.title.split(/[\\/]/).pop()} in these backups goes.`,
        note: (this.rfDryChecked() ? "" : "No dry run yet. ") + "Snapshots stay. Space frees after the next full maintenance.",
        command: plan.commit, confirmLabel: "Remove from snapshots", danger: true,
      });
      if (!ok) return;
      await this.wrap(async () => {
        const started = await this.startJob("remove_files", { form: "files", source: this.detail.source, full_path: this.detail.title, by: plan.by, everywhere: !!this.detail.rfEverywhere });
        if (started) {
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
      const rows = this.detail.purge || [];
      const warns = rows.filter((r) => r.tag).length;
      const times = rows.map((r) => r.time).filter(Boolean).sort();
      const ok = await this.klConfirm({
        title: `Delete ${ids.length} snapshot${ids.length === 1 ? "" : "s"} holding this file?`,
        facts: [
          ["File", this.detail.title],
          ["Backup", sourcePath(this.detail.source || "")],
          ["Snapshots", `${ids.length}` + (times.length ? ` · ${humanTime(times[0])}` + (times.length > 1 ? ` to ${humanTime(times[times.length - 1])}` : "") : "")],
          ["Kept", `${warns} pinned or kept by retention`],
        ],
        callout: "Every other file in these snapshots is deleted with them.",
        note: (this.purgeDryChecked() ? "" : "No dry run yet. ") + "Space frees after the next full maintenance.",
        command: this.detail.purgeCmds, confirmLabel: `Delete ${ids.length} snapshot${ids.length === 1 ? "" : "s"}`, danger: true,
      });
      if (!ok) return;
      await this._deleteSnapsFromPanel(ids);
    },
    async _deleteSnapsFromPanel(ids) {
      await this.wrap(async () => {
        const started = await this.startJob("snapshot_delete", { snapshot_ids: ids });
        if (started) {
          this.detail = null;
        }
      }, "Deleting snapshots…");
    },

    // ---- Chunked folder/filter delete (remove-files, respects the current view/filter) ----
    openRemoveFromSearch() {  // delete EVERY file matching the current Search filter (not just selected)
      // No filter = "the whole repo": the backend refuses it, so say why instead of opening a
      // panel that can never plan anything.
      const filtered = !!(this.query || this.searchSource || this.searchExt || this.searchExtExclude
        || this.deletedOnly || this.overOneGb || this.minGb !== "" || this.maxGb !== ""
        || this.minVersions !== "" || this.sharedMin !== "" || this.dateFrom || this.dateTo
        || this.recentBackupCutoff());
      if (!filtered) {
        this.klNotify("Type a search or pick a filter first - with no filter this would match the whole repository.", "warn");
        return;
      }
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
        // All-sources mode is a scope marker, not a source: with no source the plan covers every
        // backup holding the folder.
        repo: this.repo, source: this.selectedSource === "*ALL*" ? null : this.selectedSource, prefix: row.path,
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
    // Several folders and files in one plan: parts = [{source|null, prefix}]. filters = Tree filters.
    openRemoveParts(parts, label, filters = {}) {
      this._removeBase = { repo: this.repo, parts, deleted_only: !!filters.deleted_only,
                           over_gb: filters.over_gb || 0, ext: filters.ext || null };
      this._removeLabel = label;
      this.buildRemovePlan();
    },
    buildRemovePlan() {
      // Folder/file-context deletes (a Tree prefix) default to ALL versions: latest-only would
      // silently leave every older version's bytes in old snapshots ("deleted" folder still there).
      const base = this._removeBase || {};
      const allVers = !!(base.prefix || base.parts);
      this.removePlan = { loading: true, label: this._removeLabel, all_versions: allVers, everywhere: false, by: "object-id", detail: null, detailLoading: false, batSaved: null, top_n: 0, dry: this.klDryState(), parts: base.parts || null };
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
    toggleRemoveAllVersions() { if (this.removePlan) { this.removePlan.all_versions = !this.removePlan.all_versions; this.klDryClear(this.removePlan.dry); this._fetchRemovePlan(); } },
    toggleRemoveEverywhere() { if (this.removePlan) { this.removePlan.everywhere = !this.removePlan.everywhere; this.klDryClear(this.removePlan.dry); this._fetchRemovePlan(); } },
    setRemoveBy(by) { if (this.removePlan) { this.removePlan.by = by; this.klDryClear(this.removePlan.dry); this._fetchRemovePlan(); } },
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
      if (!this.removePlan) return;
      this.removePlan.top_n = n || 0;
      this.klDryClear(this.removePlan.dry);
      this._fetchRemovePlan();
    },
    removePlanDry() {  // run the whole matched plan's real kopia dry run
      if (!this.removePlan) return;
      const st = this.removePlan.dry = (this.removePlan.dry || this.klDryState());
      // The ticket is optional for a dry run, but sending it keeps the dry run on the previewed
      // list too (an expired one simply falls back to the filter - nothing mutates).
      return this.klDry("/api/actions/remove-plan-dry-run",
        { ...this._removeBody(), plan_token: this.removePlan.plan_token }, st);
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
      this.klNotify(`Downloaded ${r.filename} (${r.batches} batches). Run it - kopia's output logs next to it.`);
    },
    async removeRun() {
      if (!this.removePlan) return;
      const p = this.removePlan;
      const n = Number(p.file_count || 0);
      const share = p.capped && this.removeCoverage() != null ? ` · ${this.removeCoverage()}% of the match` : "";
      const ok = await this.klConfirm({
        title: `Delete ${n.toLocaleString()} file${n === 1 ? "" : "s"}?`,
        facts: [
          ["What", p.label],
          ["Files", `${n.toLocaleString()} · ${p.all_versions ? "every version" : "latest version only"}${share}`],
          ["Match", p.by === "filename" ? `File name · ${Number(p.name_count || 0).toLocaleString()} names` : "Exact content"],
          ["Backups", p.everywhere ? "Every backup in the repo" : (p.sources_scope || []).map((s) => sourcePath(s)).join(", ")],
          ["Frees", p.detail && p.detail.found !== false ? fmtPhys(p.detail.freeable) + " after maintenance" : fmtPhys(p.physical) + " stored"],
        ],
        callout: p.everywhere ? "Not limited to these backups: removes this content from the whole repo."
          : (p.by === "filename" ? "Every file with these names in these backups goes." : ""),
        note: (this.removePlanDryChecked() ? "" : "No dry run yet. ") + "Snapshots stay. Space frees after the next full maintenance.",
        command: p.commit, confirmLabel: `Delete ${n.toLocaleString()} file${n === 1 ? "" : "s"}`, danger: true,
      });
      if (!ok) return;
      await this.wrap(async () => {
        // the ticket makes the job delete exactly the previewed list; when it has expired
        // the server refuses, so refresh the preview and let the user review the fresh list.
        const started = await this.startJob("remove_files",
          { ...this._removeBody(), form: "plan", plan_token: this.removePlan.plan_token },
          this._removeLabel);
        if (started) {
          this.closeRemove();
        } else if (this.removePlan) {
          this.klDryClear(this.removePlan.dry);   // the old dry run described the old list
          this._fetchRemovePlan();
        }
      }, "Starting the chunked delete…");
    },
    closeRemove() { this.showRemove = false; this.removePlan = null; },
};
