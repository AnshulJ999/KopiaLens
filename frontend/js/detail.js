// Detail panels + removal: file/folder panel, chunked remove plan.

// The extra Backup history rows (compression, on disk, Backblaze). Off hides them; their place in
// the panel is still being decided.
const PANEL_EXTRA_FACTS = true;

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
    async openFolderPanel(prefix, source = null, keepStack = false) {
      if (!prefix) return;
      if (!keepStack) this.panelStack = [];
      const seq = ++this._detailSeq;
      this.detail = { kind: "folder", title: prefix, source, loading: true, heavyLoading: true, sources: [], children: [], panelTab: "overview" };
      try {
        const p = new URLSearchParams({ repo: this.repo, prefix });
        if (source) p.set("source", source);
        const sum = await getJson(`/api/folder/summary?${p.toString()}`);
        if (seq !== this._detailSeq) return;
        Object.assign(this.detail, sum, { loading: false, notFound: !sum.files && !sum.source_count });
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
            physical_dedup: r.physical, physical_newest: r.physical_newest, freeable: r.freeable, shared: r.shared,
            objects: r.objects, all_versions: r.all_versions, heavyLoading: false,
            reclaim_stale: !!r.stale, reclaim_at: r.computed_at,
          });
        })
        .catch(() => { if (seq === this._detailSeq && this.detail) this.detail.heavyLoading = false; });
      // Folder versions walk every snapshot, so they fill in after the panel opens.
      this.detail.folderVersions = null;
      getJson(`/api/folder/versions?${new URLSearchParams({ repo: this.repo, prefix, ...(source ? { source } : {}) })}`)
        .then((r) => { if (seq === this._detailSeq && this.detail) this.detail.folderVersions = r; })
        .catch(() => { if (seq === this._detailSeq && this.detail) this.detail.folderVersions = "error"; });
      // Effective policy for this exact folder (kopia resolves inheritance), via its main source.
      const main = (this.detail.sources || []).find((s) => s.relation === "covers") || (this.detail.sources || [])[0];
      if (main) {
        getJson(`/api/folder/policy?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(main.source)}&prefix=${encodeURIComponent(prefix)}`)
          .then((r) => { if (seq === this._detailSeq && this.detail) this.detail.effective_policy = r.policy; })
          .catch(() => {});
        this._loadPathStatus(seq, main.source, prefix, true);
      }
    },

    // Fills in after the panel opens: a second pass over the same pieces, so it never delays the cards.
    _loadBreakdown(seq, base) {
      this.detail.breakdown = "pending";
      getJson(`/api/search/breakdown?${base}`)
        .then((r) => { if (seq === this._detailSeq && this.detail) this.detail.breakdown = r; })
        .catch(() => { if (seq === this._detailSeq && this.detail) this.detail.breakdown = "error"; });
    },
    breakdownSegments() {
      const b = this.detail && this.detail.breakdown;
      if (!b || !b.found || !b.all_versions) return [];
      const pct = (v) => Math.max(0, (v / b.all_versions) * 100);
      return [
        { cls: "ok", pct: pct(b.frees), title: term("reclaimable") + " " + fmtPhys(b.frees) },
        { cls: "warn", pct: pct(b.shared.bytes), title: "Shared with other files " + fmtPhys(b.shared.bytes) },
        { cls: "info", pct: pct(b.copies.bytes), title: "Identical copies " + fmtPhys(b.copies.bytes) },
        { cls: "rest", pct: pct(b.unmatched), title: "Not indexed yet " + fmtPhys(b.unmatched) },
      ].filter((s) => s.pct > 0);
    },
    breakdownLabel() {
      return this.breakdownSegments().map((s) => s.title).join(", ");
    },
    chunkTip(ck) {
      if (ck.exclusive) return "Only this file uses this chunk";
      const b = this.detail && this.detail.breakdown;
      const p = b && b.found && b.pieces ? b.pieces[ck.cid] : null;
      if (!p) return "Also used by other data";
      const names = p.files.map((f) => { const s = pathSep(f); return trimSep(f).split(s).slice(-2).join(s); });
      const lines = names.length === 1 ? ["Also used by " + names[0]]
        : names.length ? [`Also used by ${countOf(names.length, "file")}: ` + names.slice(0, 3).join(", ") + (names.length > 3 ? ", …" : "")]
        : [];
      if (p.copy) lines.push("Also in an identical copy at another path");
      return lines.join("\n");
    },
    breakdownTogether() {
      const t = this.detail && this.detail.breakdown && this.detail.breakdown.together;
      if (!t) return "";
      return "Deleting it with " + (t.files === 1 ? "that file" : `those ${t.files} files`) + " frees";
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
      if (!s.excluded) return list + " · none match";
      const w = s.why;
      if (!w) return list + " · a rule leaves this out";
      const where = w.kind === "ignore_file" ? `${w.name || "an ignore file"} in ${w.anchor}`
        : w.kind === "root" ? (s.owner === "global" ? "the Global policy" : `the policy of ${sourcePath(w.owner || "") || w.owner}`)
        : `the policy of ${sourcePath(w.owner || "") || w.anchor}`;
      return `Left out by ${w.rule} (${where})` + (w.via ? `, which leaves out the folder ${w.via.split("/").pop()}` : "")
        + ". Not backed up any more; older snapshots still hold it.";
    },
    // Compressed + deduplicated size against the real size, newest version.
    _compressionRow() {
      const d = this.detail;
      if (!d || !d.logical || d.physical == null) return "—";
      const pct = Math.round((d.physical / d.logical) * 100);
      return `${fmtPhys(d.physical)} stored for ${fmtBytes(d.logical)}: ${pct}% of its size (compression and deduplication)`;
    },
    _diskRow() {
      const k = this.detail && this.detail.disk;
      if (!k) return "…";
      if (k.error || k.exists === null) return "Couldn't check";
      if (!k.exists) return "Deleted from disk";
      const v = (this.detail.versions || [])[0];
      if (!v) return "On disk";
      const same = Number(k.size) === Number(v.size)
        && (!v.mtime || Math.abs(toDate(k.mtime) - toDate(v.mtime)) < 2000);
      return same ? "On disk, unchanged since the last snapshot" : `On disk, changed since the last snapshot (now ${fmtBytes(k.size)}, modified ${humanTime(k.mtime)})`;
    },
    _bzRow() {
      const b = this.detail && this.detail.bz;
      if (!b) return "…";
      if (b.error) return "Couldn't check";
      if (!b.found) return "Not in Backblaze";
      return b.excluded ? `Left out of Backblaze${b.rule ? " by " + b.rule : ""}` : "Backed up by Backblaze";
    },
    hasBackblaze() { return (this.repos || []).some((r) => r.kind === "backblaze"); },
    // On disk and Backblaze fill in after the panel opens; each is a separate small lookup.
    _loadExtraFacts(seq, source, path) {
      if (!PANEL_EXTRA_FACTS) return;
      const set = (k, v) => { if (seq === this._detailSeq && this.detail) this.detail[k] = v; };
      getJson(`/api/path/disk?${new URLSearchParams({ source, path })}`)
        .then((r) => set("disk", r)).catch(() => set("disk", { error: true }));
      if (this.hasBackblaze()) {
        getJson(`/api/backblaze/path-status?${new URLSearchParams({ path })}`)
          .then((r) => set("bz", r)).catch(() => set("bz", { error: true }));
      }
    },
    // "All 53 versions take 12.58 GB. Removing every one of them frees 8.76 GB."
    fileVersionsLine() {
      const d = this.detail;
      const n = ((d && d.versions) || []).length;
      if (!n) return "";
      return n === 1 ? `Its one version takes ${fmtPhys(d.physical_all)}. Removing it frees ${fmtPhys(d.freeable)}.`
        : `All ${Number(n).toLocaleString(NUM_LOCALE)} versions take ${fmtPhys(d.physical_all)}. Removing every one of them frees ${fmtPhys(d.freeable)}.`;
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
        ["Versions", Number(vs.length).toLocaleString(NUM_LOCALE)],
        ["Copies", copies > 0 ? `${copies.toLocaleString(NUM_LOCALE)} identical cop${copies === 1 ? "y" : "ies"} elsewhere` : "None elsewhere"],
        ["Rules", this._rulesRow()],
        ...(PANEL_EXTRA_FACTS ? [
          ["Compression", this._compressionRow()],
          ...(d.disk && d.disk.checked === false ? [] : [["On disk", this._diskRow()]]),
          ...(this.hasBackblaze() ? [["Backblaze", this._bzRow()]] : []),
        ] : []),
      ];
    },
    // Every identical copy in the full-list popup (the panel lists the first 500).
    detailShowCopies() {
      const d = this.detail;
      if (!d || d.kind !== "file") return;
      const q = `repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(d.source)}&full_path=${encodeURIComponent(d.title)}&others_cap=1000000`;
      this.openFullList({
        title: "Identical copies", name: "identical-copies",
        columns: [["source", "Source"], ["full_path", "Path"]],
        load: async () => {
          const r = await getJson(`/api/search/detail?${q}`);
          return (r.other_paths || []).map((o) => ({ source: this.shortSource(o.source), full_path: o.full_path }));
        },
      });
    },
    // Identical copies at OTHER paths. The same path held by nested backups is not a copy.
    copyCount() {
      const d = this.detail;
      if (!d || d.kind !== "file") return 0;
      if (d.instances != null) return Math.max(0, d.instances - 1);   // the server's count; the list is capped
      return new Set((d.others || []).map((o) => o.full_path).filter((p) => p !== d.title)).size;
    },
    // "48 across 270 snapshots": the folder's distinct states, each one a change inside it.
    _folderVersionsText() {
      const v = this.detail && this.detail.folderVersions;
      if (v === "error") return "Couldn't work this out";
      if (!v) return "…";
      return `${Number(v.folder_versions).toLocaleString(NUM_LOCALE)} across ${countOf(v.snapshots, "snapshot")}`;
    },
    folderHistoryRows() {
      const d = this.detail;
      if (!d || d.kind !== "folder") return [];
      const rows = [
        ["First backed up", d.first_seen ? fmtDate(d.first_seen) : "—"],
        ["Last backed up", d.last_seen ? humanTime(d.last_seen) : "—"],
        ["Files", `${Number(d.files || 0).toLocaleString(NUM_LOCALE)} · ${Number(d.deleted_files || 0).toLocaleString(NUM_LOCALE)} deleted from disk`],
        ["Folder versions", this._folderVersionsText()],
        ["File versions", Number(d.versions || 0).toLocaleString(NUM_LOCALE)],
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
    // A row inside the folder panel. "Biggest inside" rows carry no source: a file then takes the
    // panel's source, or the first source that backs it up.
    async folderOpenChild(r) {
      const d = this.detail;
      if (!d) return;
      const path = joinPath(d.title, r.child);
      if (r.is_folder) { this.panelPush(); return this.openFolderPanel(path, d.source || null, true); }
      let source = r.source || d.source;
      if (!source) {
        const held = await this._holdersOf([path]);
        source = held && (held[path] || [])[0];
        if (!source) { if (held) this.klNotify("No indexed source holds this file.", "warn"); return; }
      }
      this.panelPush();
      this.openFileDetail({ source, full_path: path }, true);
    },

    // A file type in the folder panel: Search, filtered to this folder and type.
    folderTypeInSearch(ext) {
      const d = this.detail;
      if (!d) return;
      this.detail = null;
      this.searchKind = "files"; this.groupByFolder = false;
      if (this.searchAllRepos()) this.searchSource = "";
      this.query = d.title; this.searchExt = ext; this.searchExtExclude = "";
      this.active = "search";
      this.loadSearch();
    },

    // ---- Panel trail: a panel opened from inside a panel keeps the way back ----
    panelPush() {
      if (!this.detail) return;
      const sc = document.querySelector("[data-panel-scroll]");
      this.panelStack.push({ detail: this.detail, scroll: sc ? sc.scrollTop : 0 });
    },
    // Back to stack entry i (default: the previous panel), with its tab and scroll as they were.
    panelBack(i = this.panelStack.length - 1) {
      if (i < 0 || i >= this.panelStack.length) return;
      const entry = this.panelStack[i];
      this.panelStack = this.panelStack.slice(0, i);
      ++this._detailSeq;   // the child's loads must not land on the restored panel
      const d = entry.detail;
      if (d.kind !== "selection" && (d.loading || d.heavyLoading)) {
        // Its own loads were dropped when the child opened: load it again.
        return d.kind === "folder" ? this.openFolderPanel(d.title, d.source || null, true)
          : this.openFileDetail({ source: d.source, full_path: d.title }, true);
      }
      if (d.kind === "selection") d.statsLoading = false;
      if (d.contentsLoading) { d.contentsLoading = false; d.contents = null; }
      this.detail = d;
      if (d.kind === "folder" && d.panelTab === "contents") this.folderLoadContents();
      this.$nextTick(() => { const sc = document.querySelector("[data-panel-scroll]"); if (sc) sc.scrollTop = entry.scroll; });
    },
    panelCrumbs() {
      return this.panelStack.map((e, i) => ({ i, label: e.detail.kind === "selection" ? e.detail.title : (baseName(e.detail.title) || e.detail.title) }));
    },
    async panelOpenItem(it, push = true) {
      if (push) this.panelPush();
      if (it.kind === "folder") return this.openFolderPanel(it.path, it.source || null, push);
      let src = it.source;
      if (!src) {
        const held = await this._holdersOf([it.path]);
        src = held && (held[it.path] || [])[0];
        if (!src) {
          if (push) this.panelStack.pop();
          if (held) this.klNotify("No indexed source holds this file.", "warn");
          return;
        }
      }
      return this.openFileDetail({ source: src, full_path: it.path }, push);
    },

    // ---- Several selected items: one panel for files, folders or a mix ----
    async openSelection(scope) {
      const rows = this.selectedRows(scope);
      if (!rows.length) { this.klNotify("Select one or more rows first.", "warn"); return; }
      if (rows.length === 1 && scope === "search" && rows[0].full_path && !this.isFolderRow(rows[0])) return this.openFileDetail(rows[0]);
      const items = this._selItems(scope);
      if (items.length === 1) return this.panelOpenItem(items[0], false);
      const seq = ++this._detailSeq;
      this.panelStack = [];
      const byPath = new Map(rows.map((r) => [r.full_path || r.path, r]));
      const list = items.map((it) => {
        const r = byPath.get(it.path) || {};
        const phys = r.physical != null ? r.physical : r.phys;
        return { ...it, size: Number(r.size || r.logical || 0), physical: phys == null ? null : Number(phys) };
      });
      const files = list.filter((it) => it.kind === "file");
      this.detail = {
        kind: "selection", title: countOf(list.length, "item"), scope, items: list, loading: false,
        files: files.length, folders: list.length - files.length,
        sources: new Set(list.map((it) => it.source).filter(Boolean)).size,
        size: list.reduce((a, it) => a + it.size, 0),
        physical: list.every((it) => it.physical != null) ? list.reduce((a, it) => a + it.physical, 0) : null,
        freeable: null, exact: false, statsLoading: false,
      };
      if (files.length !== list.length) return;   // folders: the delete plan works out what they free
      const need = files.filter((f) => !f.source).map((f) => f.path);
      const held = need.length ? (await this._holdersOf(need)) || {} : {};
      const pairs = files.map((f) => ({ source: f.source || (held[f.path] || [])[0], full_path: f.path })).filter((f) => f.source);
      if (seq !== this._detailSeq || !this.detail || this.detail.kind !== "selection" || !pairs.length) return;
      this.detail.statsLoading = true;
      const r = await getJson("/api/actions/batch-detail", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: this.repo, files: pairs, all_versions: true }) }).catch(() => null);
      if (seq !== this._detailSeq || !this.detail || this.detail.kind !== "selection") return;
      this.detail.statsLoading = false;
      if (r && r.found) Object.assign(this.detail, { size: r.logical, physical: r.physical, freeable: r.freeable, exact: true });
    },
    selectionLine() {
      const d = this.detail;
      if (!d || d.kind !== "selection") return "";
      return [d.files && countOf(d.files, "file"), d.folders && countOf(d.folders, "folder")].filter(Boolean).join(", ")
        + (d.sources ? " · " + countOf(d.sources, "source") : "");
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
    async openFileDetail(item, keepStack = false) {
      if (!keepStack) this.panelStack = [];
      const seq = ++this._detailSeq;
      // Render the panel immediately from the row we already have; the heavy fields (refcount,
      // dedup chunks, purge plan) fill in when they arrive. A second click bumps _detailSeq so a
      // stale in-flight response is dropped (no race).
      this.detail = {
        kind: "file", loading: true, title: item.full_path, source: item.source,
        on_disk: item.in_latest, logical: item.size, physical: item.physical,
        freeable: null, shared: null, chunks: [], others: [], versions: [], purge: [],
        breakdown: null,
        panelTab: "overview",
        // the Actions tab's presets for the Delete panel
        rfBy: "object-id", rfEverywhere: false,
      };
      const base = `repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`;
      try {
        const [detail, vres, purge] = await Promise.all([
          getJson(`/api/search/detail?${base}`),
          getJson(`/api/search/versions?${base}`),
          getJson(`/api/actions/purge-plan?${base}`),
        ]);
        if (seq !== this._detailSeq) return;  // a newer file was opened meanwhile - drop this result
        // A Free install gets the snapshots without ids or commands (the purge plan is Pro).
        const cmds = purge.commands || [];
        const purgeRows = (purge.items || []).map((it, i) => {
          const c = cmds[i] || "";
          return { cmd: Array.isArray(c) ? c.join(" ") : c, time: it.start_time, tag: it.pinned ? "pinned" : ((it.retention && it.retention.length) ? `kept: ${it.retention.join(", ")}` : ""), id: it.id };
        });
        Object.assign(this.detail, {   // merge (not replace) so presets set meanwhile survive
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
          versions: (vres.versions || []).map((x) => ({ time: humanTime(x.start_time), start: x.start_time, size: x.size, mtime: x.mtime, snapshot_id: x.snapshot_id, content_id: x.content_id })),
          purge: purgeRows,
          notFound: !(vres.versions || []).length && !detail.content_id,
        });
        this._loadPathStatus(seq, this.detail.source, item.full_path, false);
        this._loadExtraFacts(seq, this.detail.source, item.full_path);
        this._loadBreakdown(seq, base);
      } catch (err) {
        if (seq === this._detailSeq) this.error = err.message || String(err);
      }
    },

    // ---- Actions tab: each delete section opens the Delete panel, preset to its mode ----
    rfSetBy(by) { if (this.detail) this.detail.rfBy = by; },
    rfToggleEverywhere() { if (this.detail) this.detail.rfEverywhere = !this.detail.rfEverywhere; },
    fileOpenDelete(method) {
      const d = this.detail;
      if (!d) return;
      this.openDeletePanel([{ source: d.source, full_path: d.title }],
        method === "snapshots" ? { method } : { method, by: d.rfBy || "object-id", every: !!d.rfEverywhere });
    },

    // ---- Chunked folder/filter delete (remove-files, respects the current view/filter) ----
    openRemoveFromSearch() {  // delete EVERY file matching the current Search filter (not just selected)
      if (this.searchDeleteTooBig()) {
        this.klNotify(`Narrow the search to ${SEARCH_DELETE_MAX.toLocaleString(NUM_LOCALE)} files or fewer to delete them all.`, "warn");
        return;
      }
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
      if (!this.wmGuard("full")) return;
      this._removeBase = {
        // All-sources mode is a scope marker, not a source: with no source the plan covers every
        // backup holding the folder.
        repo: this.repo, source: this.selectedSource === "*ALL*" ? null : this.selectedSource, prefix: row.path,
        deleted_only: this.treeDeletedOnly, over_gb: this.treeOverGb ? 1 : 0, ext: this.treeExt || null,
      };
      const filtered = this.treeDeletedOnly || this.treeOverGb || this.treeExt;
      this._removeLabel = (row.type === "folder" ? "folder " : "") + (row.path || row.name) + (filtered ? " (filtered)" : "");
      this.buildRemovePlan();
    },
    openRemoveFolderPath(prefix, source = null) {
      if (!this.wmGuard("full")) return;
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
    removeTitle() {
      const b = this._removeBase || {}, p = this.removePlan;
      if (p && p.flow) return "Exclude and delete";
      if (b.parts) return `Delete ${this._removeLabel} from backups`;
      const name = baseName(b.prefix || "") || b.prefix;
      if (b.prefix) return (this._removeLabel || "").startsWith("folder ") ? `Delete folder “${name}” from backups` : `Delete “${name}” from backups`;
      return "Delete everything matching the search";
    },
    removePathLine() {
      const b = this._removeBase || {};
      const filtered = b.deleted_only || b.over_gb || b.ext ? " · Tree filters applied" : "";
      if (b.prefix) return b.prefix + (b.source ? ` · in ${sourcePath(b.source)}` : "") + filtered;
      if (b.parts) return b.parts.slice(0, 3).map((x) => x.prefix).join("  ·  ") + (b.parts.length > 3 ? ` and ${b.parts.length - 3} more` : "") + filtered;
      return this._removeLabel || "";
    },
    removeConfirmLabel() {
      const p = this.removePlan;
      if (!p) return "Delete";
      const files = countOf(this.removeFileTotal(), "file");
      return p.everywhere ? `Remove ${files} from every source` : `Remove ${files} from ${countOf((p.sources_scope || []).length, "source")}`;
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
      this.removePlan.shown = 100;
      this.removePlan.loading = false;
      // Auto-compute freeable/shared (like the file panel) unless the set is huge (avoid a long walk).
      if (r && (r.file_count || 0) <= 200000) this.removeComputeReclaim();
    },
    // Every same-named file / identical copy outside the plan, in the full-list popup.
    removeShowExtra(kind) {
      if (!this.removePlan) return;
      const body = JSON.stringify({ ...this._removeBody(), extra_cap: 1000000 });
      const byName = kind === "filename";
      this.openFullList({
        title: byName ? "Other same-named files" : "Identical copies", name: byName ? "same-named-files" : "identical-copies",
        columns: [["full_path", "Path"], ["sources_text", "Sources"], ["on_disk", "On disk"], ["size", "Size", "bytes"]],
        load: async () => {
          const r = await getJson("/api/actions/remove-plan", { method: "POST", headers: { "Content-Type": "application/json" }, body });
          return ((byName ? r.filename_extra : r.content_extra) || []).map((f) => ({ full_path: f.full_path,
            sources_text: this.fileSources(f), on_disk: f.in_latest === false ? "deleted" : "yes", size: f.size }));
        },
      });
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
    removePlanDry(ev) {  // run the whole matched plan's real kopia dry run
      if (!this.removePlan || !this.proGate("remove", ev)) return;
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
    // Files outside the plan that go too: same-named files (file name) or identical copies at other
    // paths (exact content). Every count and size the panel and confirm show includes them.
    removeExtra(kind) {
      const p = this.removePlan;
      if (!p) return 0;
      if (p.by !== "filename" && kind === "physical") return 0;   // identical copies store the same pieces
      return Number(p[(p.by === "filename" ? "filename" : "content") + "_extra_" + kind] || 0);
    },
    // Reclaimable for a match method, shown in the switch so both can be compared.
    removeByFig(by) {
      const p = this.removePlan, d = p && p.detail;
      if (!d || d.found === false) return "";
      const v = by === p.by ? d.freeable : d.alt_freeable;
      return v == null ? "" : fmtPhys(v);
    },
    removeAllVersions() {  // kopia's --filename rewrites every snapshot, so a name delete is always every version
      const p = this.removePlan;
      return !!(p && (p.all_versions || p.by === "filename"));
    },
    removeFileTotal() {
      return this.removePlan ? Number(this.removePlan.file_count || 0) + this.removeExtra("count") : 0;
    },
    removePhysTotal() {
      return this.removePlan ? Number(this.removePlan.physical || 0) + this.removeExtra("physical") : 0;
    },
    removeFreesText() {
      const d = this.removePlan && this.removePlan.detail;
      if (d && d.found !== false) return (d.capped ? "at least " : "") + fmtPhys(d.freeable) + " after maintenance";
      return fmtPhys(this.removePhysTotal()) + " stored";
    },
    removeFilesSplit() {  // "9 here + 8 with the same name elsewhere", or "" when nothing rides along
      const extra = this.removeExtra("count");
      if (!extra) return "";
      const what = this.removePlan.by === "filename" ? "with the same name" : (extra === 1 ? "identical copy" : "identical copies");
      return `${Number(this.removePlan.file_count || 0).toLocaleString(NUM_LOCALE)} here + ${extra.toLocaleString(NUM_LOCALE)} ${what} elsewhere`;
    },
    removeCoverage() {  // "these 100 files are 70% of the folder" - the number that makes top-N a decision
      const p = this.removePlan;
      if (!p || !p.total_physical) return null;
      return Math.round((p.physical / p.total_physical) * 100);
    },
    copyRemoveCommand(ev) { if (this.proGate("remove", ev)) this.copyText(this.removePlan.commit); },
    // "What gets removed": the biggest files, or the plan grouped by folder or by file type.
    removeListCount() {
      const p = this.removePlan;
      if (!p) return "";
      const b = p.breakdown || {};
      const total = Number(p.file_count || 0);   // the list holds the plan's own files; copies have their own section
      if (p.listView === "folders") return (b.folder_count || 0) > (b.folders || []).length ? `biggest ${(b.folders || []).length} of ${Number(b.folder_count).toLocaleString(NUM_LOCALE)} folders` : countOf((b.folders || []).length, "folder");
      if (p.listView === "types") return countOf((b.types || []).length, "file type");
      const shown = Math.min(p.shown || 100, (p.preview || []).length);
      return shown < total ? `${shown.toLocaleString(NUM_LOCALE)} of ${countOf(total, "file")}, biggest first` : `${countOf(total, "file")}, biggest first`;
    },
    removeFolderLabel(f) {
      return f.direct ? `Files directly in ${baseName(f.folder) || f.folder}` : f.folder;
    },
    async removeDownloadList(ev) {
      if (!this.proGate("remove", ev)) return;
      const r = await getJson("/api/actions/remove-list", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this._removeBody()) }).catch((e) => { this.error = e.message; return null; });
      if (!r) return;
      this._downloadText(r.filename, r.content);
      this.klNotify(`Downloaded ${r.filename} (${countOf(r.rows, "file")}).`);
    },
    async removeSaveBat(ev) {
      if (!this.proGate("remove", ev)) return;
      const r = await getJson("/api/actions/remove-bat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(this._removeBody()) }).catch((e) => { this.error = e.message; return null; });
      if (!r || !this.removePlan) return;
      this._downloadText(r.filename, r.content);
      this.removePlan.batSaved = { filename: r.filename, batches: r.batches };
      this.actionText = `Downloaded ${r.filename} (${r.batches} batches). Run it; kopia output logs next to it.`;
      this.klNotify(`Downloaded ${r.filename} (${r.batches} batches). Run it - kopia's output logs next to it.`);
    },
    async removeRun(ev) {
      if (!this.removePlan || !this.proGate("remove", ev)) return;
      const p = this.removePlan;
      const files = countOf(this.removeFileTotal(), "file");
      const share = p.capped && this.removeCoverage() != null ? ` · ${this.removeCoverage()}% of the match` : "";
      const split = this.removeFilesSplit();
      const ok = await this.klConfirm({
        title: `Delete ${files}?`,
        facts: [
          ["What", p.label],
          ["Files", `${split || files} · ${this.removeAllVersions() ? "every version" : "latest version only"}${share}`],
          ["Match", p.by === "filename" ? `File name · ${countOf(p.name_count, "name")}` : "Exact content"],
          ["Sources", p.everywhere ? "Every source in the repository" : (p.sources_scope || []).map((s) => sourcePath(s)).join(", ")],
          [term("reclaimable"), this.removeFreesText()],
          ...(p.by === "filename" && p.detail && p.detail.kept_count
            ? [["Stays stored", `${fmtPhys(p.detail.kept)} · kept by ${countOf(p.detail.kept_count, "identical file")} with other names`]] : []),
        ],
        callout: p.everywhere ? "Not limited to these sources: removes this content from the whole repository."
          : (p.by === "filename" ? "Every file with these names in these sources goes."
            : (this.removeExtra("count") ? "Identical copies at other paths in these sources go too." : "")),
        note: (this.removePlanDryChecked() ? "" : "No dry run yet. ") + "Snapshots stay. Space comes back after the next full maintenance.",
        command: p.commit, confirmLabel: this.removeConfirmLabel(), danger: true,
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
