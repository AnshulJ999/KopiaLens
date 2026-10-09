// Search tab: server-side filtered search, dedupe, reclaim, by-folder bloat finder.
const searchPart = {

    // ---------- Search (server-side count + sort + pagination) ----------
    searchParams() {
      const p = new URLSearchParams({ repo: this.repo, q: this.query, deleted_only: this.deletedOnly, over_gb: this.overOneGb ? 1 : 0, sort: this.searchSort, dir: this.searchDir });
      if (this.searchSource && !this.searchAllRepos()) p.set("source", this.searchSource);
      if (this.searchExt) p.set("ext", this.searchExt);
      if (this.searchExtExclude) p.set("ext_exclude", this.searchExtExclude);
      if (this.minGb !== "" && this.minGb != null) p.set("min_gb", this.minGb);
      if (this.maxGb !== "" && this.maxGb != null) p.set("max_gb", this.maxGb);
      if (this.dateFrom) p.set("date_from", this.dateFrom);
      if (this.dateTo) p.set("date_to", this.dateTo);
      if (this.minVersions !== "" && this.minVersions != null) p.set("min_versions", this.minVersions);
      if (this.sharedMin !== "" && this.sharedMin != null) p.set("shared_min", this.sharedMin);
      const ff = this.recentBackupCutoff();
      if (ff) p.set("first_from", ff);
      if (this.dedupe) { p.set("dedupe", "1"); p.set("dedupe_by", this.dedupeBy); }
      return p;
    },
    // Filters behind "More filters" that are on, as chips: [{key, label, value}].
    searchChips() {
      const out = [];
      const range = (a, b, unit = "") => (a && b ? `${a}–${b}${unit}` : a ? `≥ ${a}${unit}` : `≤ ${b}${unit}`);
      if (this.searchExt) out.push({ key: "ext", label: "ext", value: this.searchExt });
      if (this.searchExtExclude) out.push({ key: "hide", label: "hide", value: this.searchExtExclude });
      if (this.recentBackup) out.push({ key: "first", label: "first backed up", value: { 1: "24 hours", 7: "7 days", 30: "30 days" }[this.recentBackup] || this.recentBackup + " days" });
      if (this.minGb || this.maxGb) out.push({ key: "size", label: "size", value: range(this.minGb, this.maxGb, " GB") });
      if (this.dateFrom || this.dateTo) out.push({ key: "seen", label: "last seen", value: range(this.dateFrom && fmtDate(this.dateFrom), this.dateTo && fmtDate(this.dateTo)) });
      if (this.minVersions) out.push({ key: "versions", label: "versions", value: "≥ " + this.minVersions });
      if (this.sharedMin) out.push({ key: "shared", label: "same content", value: "≥ " + this.sharedMin + " paths" });
      return out;
    },
    clearChip(key) {
      if (key === "ext") this.searchExt = "";
      if (key === "hide") this.searchExtExclude = "";
      if (key === "first") { this.recentBackup = ""; this.klSet({ recent_backup: "" }); }
      if (key === "size") { this.minGb = ""; this.maxGb = ""; }
      if (key === "seen") { this.dateFrom = ""; this.dateTo = ""; }
      if (key === "versions") this.minVersions = "";
      if (key === "shared") this.sharedMin = "";
      this.loadSearch();
    },
    recentBackupCutoff() {  // dropdown days -> YYYY-MM-DD cutoff for first_seen ("recently backed up")
      const days = parseInt(this.recentBackup, 10);
      if (!days) return "";
      return new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
    },
    changeRecentBackup() { this.searchPage = 1; this.klSet({ recent_backup: this.recentBackup }); this.loadSearch(); },
    searchPages() { return Math.max(1, Math.ceil(this.searchTotal / (parseInt(this.searchPageSize, 10) || 500))); },
    setSearchPage(n) { this.searchPage = Math.min(Math.max(1, n), this.searchPages()); this.loadSearch(true); },
    setSearchSort(field, dir) { this.searchSort = field; this.searchDir = dir; this.searchPage = 1; this.klSet({ search_sort: field, search_dir: dir }); this.loadSearch(); },
    changePageSize() {
      this.klSet({ page_size: this.searchPageSize });  // persist the raw value, including "Infinite"
      this.searchPage = 1; this.loadSearch();
    },
    searchFiltersOn() {
      return !!(this.minGb || this.maxGb || this.dateFrom || this.dateTo || this.minVersions || this.sharedMin
        || this.searchExt || this.searchExtExclude || this.searchSource || this.deletedOnly || this.overOneGb
        || this.recentBackup);
    },
    searchEmptyText() {
      if (this.query) return `No results for “${this.query}”.`;
      return this.searchFiltersOn() ? "No results for these filters." : "No results.";
    },
    clearFilters() {
      this.minGb = ""; this.maxGb = ""; this.dateFrom = ""; this.dateTo = ""; this.minVersions = ""; this.sharedMin = "";
      this.searchExt = ""; this.searchExtExclude = ""; this.searchSource = ""; this.deletedOnly = false; this.overOneGb = false;
      this.recentBackup = ""; this.klSet({ recent_backup: "" });
      this.loadSearch();
    },

    // "All repositories" is a choice in the source picker; it searches files only.
    searchAllRepos() { return this.searchSource === "*REPOS*"; },
    searchSourceChanged() {
      if (this.searchAllRepos() && this.searchKind !== "files") { this.searchKind = "files"; this.groupByFolder = false; }
      this.loadSearch();
    },
    setSearchKind(k) {
      if (this.searchAllRepos() && k !== "files") return;
      this.searchKind = k; this.groupByFolder = k === "folders"; this.searchPage = 1;
      this.klSet({ search_kind: k });
      this.loadSearch();
    },

    // ---- Bloat finder: group backed-up paths by folder, ranked by physical (own #groupTable) ----
    changeGroupDepth() { this.klSet({ group_depth: Number(this.groupDepth) || 3 }); this.loadSearch(); },
    drillFolder(row) { this.searchKind = "files"; this.groupByFolder = false; this.klSet({ search_kind: "files" }); this.query = row.folder; this.searchPage = 1; this.loadSearch(); },
    // A row from another repository opens there: switch repo, then its panel.
    async openInRepo(d) {
      this.searchSource = "";
      this.repo = d.repo;
      await this.repoChanged();
      this.active = "search";
      this.openFileDetail(d);
    },
    selectedGroupFolder() {  // the By-folder table is a SEPARATE Tabulator instance from #searchTable
      const el = document.querySelector("#groupTable");
      const rows = (el && el._tabulator) ? el._tabulator.getSelectedData() : [];
      return rows.length ? rows[0] : null;
    },
    async loadGroupByFolder() {
      await this.wrap(async () => {
        const p = new URLSearchParams({ repo: this.repo, depth: this.groupDepth, q: this.query || "", sort: "physical", direction: "desc", limit: 500 });
        if (this.searchSource) p.set("source", this.searchSource);
        if (this.searchExt) p.set("ext", this.searchExt);
        if (this.searchExtExclude) p.set("ext_exclude", this.searchExtExclude);
        if (this.deletedOnly) p.set("deleted_only", "1");
        if (this.groupMinGb) p.set("min_gb", this.groupMinGb);
        if (this.groupMinFiles) p.set("min_files", this.groupMinFiles);
        const data = await getJson(`/api/search/group-by-folder?${p.toString()}`);
        const rows = data.rows || [];
        this.omniMeta = `${countOf(rows.length, "folder")} · depth ${data.depth} · ranked by physical`;
        const cols = [
          { title: "Folder", field: "folder", widthGrow: 5, minWidth: 300 },
          { title: "Physical", field: "phys", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => fmtPhys(c.getValue()) },
          { title: "Logical", field: "logical", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => fmtBytes(c.getValue()) },
          // Excl column hidden here too - same reason as the Search table ().
          { title: "Files", field: "files", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Deleted #", field: "deleted_files", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Deleted %", field: "deleted_pct", width: 90, headerSort: false, hozAlign: "right",
            accessorDownload: (_v, d) => (d.files ? Math.round((d.deleted_files / d.files) * 100) : ""), formatter: (c) => { const d = c.getRow().getData(); return d.files ? Math.round((d.deleted_files / d.files) * 100) + "%" : ""; } },
          { title: "Avg size", field: "avg_size", width: 95, headerSort: false, hozAlign: "right",
            accessorDownload: (_v, d) => (d.files ? Math.round(d.phys / d.files) : ""), formatter: (c) => { const d = c.getRow().getData(); return d.files ? fmtBytes(Math.round(d.phys / d.files)) : ""; } },
          // per-folder freeable, computed on demand (right-click) - a chunk walk per folder.
          // "≥" marks a partial result: the backend caps the walk at the first 5,000 files.
          { title: term("reclaimable"), headerTooltip: termTip("reclaimable") + " Right-click a folder to work it out.", field: "reclaim_val", headerSort: false, width: 115, hozAlign: "right",
            accessorDownload: (_v, d) => { const v = this.groupReclaim[this.repo + ":" + d.folder]; return v && v.v != null ? v.v : ""; },
            formatter: (c) => {
              const v = this.groupReclaim[this.repo + ":" + c.getRow().getData().folder];
              if (v === "pending") return "<span style='color:var(--faint)'>…</span>";
              if (v === "error") return "<span style='color:var(--danger)' title='Could not work this out'>error</span>";
              if (v == null) return "<span style='color:var(--faint)' title='Right-click: Work out reclaimable space'>—</span>";
              const tip = v.capped ? ' title="At least this much: only the biggest 5,000 files were checked"' : "";
              return `<span style="color:var(--ok)"${tip}>${v.capped ? "&ge; " : ""}${fmtPhys(v.v)}</span>`;
            } },
          { title: "Last seen", field: "last_seen", width: 155, formatter: (c) => timeCell(c.getValue()) },
        ];
        setTable("#groupTable", rows, cols, {
          onRowDblClick: (_e, row) => this.openFolderPanel(row.getData().folder),
          rowMenu: [
            { label: "Details", action: (_e, row) => this.openFolderPanel(row.getData().folder) },
            { label: "Show its files", action: (_e, row) => this.drillFolder(row.getData()) },
            { label: "Work out reclaimable space", action: (_e, row) => this.computeGroupReclaim(row) },
            { separator: true },
            { label: "Delete folder from backups…", action: (_e, row) => this.openRemoveFolderPath(row.getData().folder) },
            { separator: true },
            { label: "Copy path", action: (_e, row) => this.copyText(row.getData().folder) },
          ],
        });
      }, "Grouping folders…");
    },
    // freeable for one folder row of the By-folder view - the same on-demand walk the
    // remove-detail button uses, scoped to the folder prefix. Heavy, so it only runs when asked.
    // Keyed by repo: switching repos must never show the previous repo's number.
    async computeGroupReclaim(row) {
      const d = row.getData();
      const key = this.repo + ":" + d.folder;
      const cur = this.groupReclaim[key];
      if (cur === "pending") return;  // block repeats only while in flight; a deliberate
                                      // second pick recomputes - that pick IS the manual refresh
      this.groupReclaim[key] = "pending";
      row.reformat();
      try {
        const r = await getJson("/api/actions/remove-detail", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, prefix: d.folder, all_versions: true }) });
        this.groupReclaim[key] = (r.found === false)
          ? { v: 0, capped: false }
          : { v: r.freeable ?? 0, capped: !!r.capped };
      } catch (e) {
        this.groupReclaim[key] = "error";
      }
      row.reformat();
    },
    async loadSearch(keepPage = false) {
      if (this.groupByFolder) return this.loadGroupByFolder();
      const seq = ++this._searchSeq;
      if (!keepPage) this.searchPage = 1;
      await this.wrap(async () => {
        const infinite = this.searchPageSize === "Infinite";
        const size = infinite ? 100000 : (parseInt(this.searchPageSize, 10) || 500);
        const offset = infinite ? 0 : (this.searchPage - 1) * size;
        const allRepos = this.searchAllRepos();
        const both = !allRepos && this.searchKind === "both";
        const p = this.searchParams(); p.set("limit", allRepos && infinite ? 20000 : size); p.set("offset", offset);
        if (allRepos) p.delete("repo");
        if (both) {
          p.set("depth", this.groupDepth);
          if (this.groupMinGb) p.set("folder_min_gb", this.groupMinGb);
          if (this.groupMinFiles) p.set("folder_min_files", this.groupMinFiles);
        }
        const url = allRepos ? "/api/search/all-repos" : both ? "/api/search/mixed" : "/api/search";
        const data = await getJson(`${url}?${p.toString()}`);
        if (seq !== this._searchSeq) return;
        this.searchTotal = data.total || 0;
        this.searchFolders = both ? (data.folders || 0) : 0;
        this.searchSkipped = allRepos ? (data.skipped || []) : [];
        this.searchEmpty = this.searchTotal === 0 && !this.searchFolders;
        const fileRows = (data.results || []).filter((r) => r.kind !== "folder").length;
        const shownFrom = this.searchTotal ? offset + 1 : 0;
        const shownTo = Math.min(offset + fileRows, this.searchTotal);
        const head = both ? `${countOf(this.searchTotal, "file")} and ${countOf(this.searchFolders, "folder")}`
          : allRepos ? `${countOf(this.searchTotal, "result")} in ${countOf((data.searched || []).length, "repository", "repositories")}`
          : countOf(this.searchTotal, "result");
        const skipped = this.searchSkipped.length ? ` · not searched: ${this.searchSkipped.map((s) => this.repoLabel(s.repo)).join(", ")}` : "";
        this.omniMeta = this.searchEmpty ? ""
          : head + (infinite || this.searchTotal <= size ? "" : ` · showing ${shownFrom.toLocaleString(NUM_LOCALE)}–${shownTo.toLocaleString(NUM_LOCALE)}`) + skipped;
        // Headers do NOT sort locally (headerSort:false) - a click re-queries the server so the
        // sort is always over the ENTIRE result set, never just the visible page. Active column
        // shows a ▼/▲ arrow.
        const self = this;
        const arrow = (f) => (this.searchSort === f ? (this.searchDir === "desc" ? "  ▼" : "  ▲") : "");
        const H = (title, field, extra, key = field) => Object.assign({
          title: title + arrow(key), field, headerSort: false,
          headerClick: () => self.setSearchSort(key, (self.searchSort === key && self.searchDir === "desc") ? "asc" : "desc"),
        }, extra || {});
        const ranked = data.ranked || {};
        if (["stored", "frees"].includes(this.searchSort) && ranked.kind && !ranked.available) {
          const noticeKey = this.repo + "|" + this.searchSort;
          if (!this._rankedNotice[noticeKey]) {   // once per repo and column per session
            this._rankedNotice[noticeKey] = 1;
            this.klNotify(this.indexStatus && this.indexStatus.building
              ? "That list is still being built. Sorted by Physical (latest) until it's ready."
              : "That list is built by the nightly index. Sorted by Physical (latest) until then.", "warn");
          }
        }
        // Folder rows (Both view) carry only sizes, file count and last seen.
        const nf = (fn) => (c) => (c.getRow().getData().kind === "folder" ? "" : fn(c));
        const cols = [
          { title: "Repo", field: "repo", headerSort: false, width: 90, visible: allRepos, formatter: (c) => this.repoLabel(c.getValue()) },
          H("Path", "full_path", { widthGrow: 5, minWidth: 320, formatter: (c) => {
            const d = c.getRow().getData();
            return d.kind === "folder" ? `📁 ${c.getValue()} <span style="color:var(--muted)">· ${countOf(d.files || 0, "file")}</span>` : c.getValue();
          } }),
          H("Size", "size", { width: 100, formatter: (c) => fmtBytes(c.getValue()), hozAlign: "right" }),
          H("Physical (latest)", "physical", { width: 120, formatter: (c) => fmtPhys(c.getValue()), hozAlign: "right" }),
          // Excl column intentionally hidden: paths.exclusive is unfilled (the fill was removed -
          // wrong semantics for shared indirect content + write amplification) and its per-object
          // semantics overstated exclusivity. Returns with the proper implementation ().
          { title: "Src", field: "sources", headerSort: false, width: 62, hozAlign: "right",
            headerTooltip: "How many sources hold this content. Copies inside one source count once.",
            formatter: nf((c) => { const v = Number(c.getValue() || 1); return v > 1 ? `<span style="color:var(--warn)" title="Held by ${v} sources">${v}</span>` : "1"; }) },
          H("Stored (all versions)", "stored_all", { width: 140, hozAlign: "right",
            headerTooltip: "Space every version of this file takes. Data shared between its own versions counts once. Sorting uses the nightly top list; files outside it come after.",
            formatter: nf((c) => self.versionCell(c.getRow().getData(), "stored")) }, "stored"),
          H(term("reclaimableAll"), "frees_all", { width: 190, hozAlign: "right",
            headerTooltip: termTip("reclaimableAll") + " Sorting uses the nightly top list; files outside it come after.",
            formatter: nf((c) => self.versionCell(c.getRow().getData(), "frees")) }, "frees"),
          H("Ext", "ext", { width: 70, formatter: nf((c) => c.getValue() || "") }),
          H("Versions", "versions", { width: 80, hozAlign: "right", formatter: nf((c) => c.getValue() ?? "") }),
          { title: "Copies", field: "copies", headerSort: false, width: 75, hozAlign: "right",
            headerTooltip: "How many paths hold this exact content. More filters: same content in at least N paths.",
            formatter: nf((c) => { const v = Number(c.getValue() || 1); return v > 1 ? `<span style="color:var(--warn)" title="This exact content is at ${v} paths">${v}</span>` : "1"; }) },
          H("Last seen", "last_seen", { width: 160, formatter: (c) => timeCell(c.getValue()) }),
          H("First seen", "first_seen", { width: 160, formatter: nf((c) => timeCell(c.getValue())) }),
          H("On disk", "in_latest", { width: 90, formatter: nf((c) => c.getValue() ? "yes" : "<span style='color:var(--danger)'>deleted</span>") }),
          H("Source", "source", { widthGrow: 2, minWidth: 180, formatter: nf((c) => sourcePath(c.getValue())) }),
        ];
        // Reuse the existing Tabulator (replaceData + header-title update) rather than
        // destroy+recreate, so re-querying on sort/filter no longer flickers the whole table.
        const sEl = document.querySelector("#searchTable");
        if (sEl && sEl._tabulator) {
          const t = sEl._tabulator;
          // pass the full def (keeps headerClick + formatter) so only the rows + sort arrow change
          cols.forEach((c) => { try { t.updateColumnDefinition(c.field, c); } catch (e) {} });
          try { if (allRepos) t.showColumn("repo"); else t.hideColumn("repo"); } catch (e) {}
          t.replaceData(data.results || []).then(() => { this.searchSelCount = t.getSelectedData().length; this.fillVersionSizes(); });
        } else {
          const sStbl = setTable("#searchTable", data.results || [], cols, {
            persistSort: false,
            placeholder: false,   // the tab shows its own no-results line above the table
            selectableRows: true,
            // Folder rows and rows of other repositories have their own menu, not the selection actions.
            selectableRowsCheck: (row) => row.getData().kind !== "folder" && !this.searchAllRepos(),
            onSelect: (rows) => { this.searchSelCount = rows.length; },
            onRowDblClick: (_e, row) => {
              const d = row.getData();
              if (d.kind === "folder") return this.openFolderPanel(d.full_path);
              if (d.repo && d.repo !== this.repo) return this.openInRepo(d);
              this.openFileDetail(d);
            },
            rowMenu: (_e, row) => this.searchRowMenu(row.getData()),
          });
          if (sStbl) {
            sStbl.on("scrollVertical", () => this.fillVersionSizes());
            sStbl.on("tableBuilt", () => this.fillVersionSizes());
          }
          this.fillVersionSizes();
        }
      }, "Searching...");
    },

    // Row menu: one file, or every selected file. Whole-result actions live in the toolbar's ⋯ menu.
    searchRowMenu(d) {
      if (d.kind === "folder") {
        return [
          { label: "Details", action: () => this.openFolderPanel(d.full_path) },
          { label: "Show its files", action: () => this.drillFolder({ folder: d.full_path }) },
          { separator: true },
          { label: "Delete folder from backups…", action: () => this.openRemoveFolderPath(d.full_path) },
          { separator: true },
          { label: "Copy path", action: () => this.copyText(d.full_path) },
        ];
      }
      if (d.repo && d.repo !== this.repo) {
        return [
          { label: `Open in ${this.repoLabel(d.repo)}`, action: () => this.openInRepo(d) },
          { separator: true },
          { label: "Copy path", action: () => this.copyText(d.full_path) },
        ];
      }
      const sel = this.selectedRows("search");
      if (sel.length > 1) {
        const what = countOf(sel.length, "file");
        return [
          { label: `Details of ${what}`, action: () => this.openSelection("search") },
          { separator: true },
          { label: `Restore ${what}…`, action: () => this.selRestore("search") },
          { label: `Exclude ${what}…`, action: () => this.selExclude("search") },
          { separator: true },
          { label: `Delete ${what} from backups…`, action: () => this.selDelete("search") },
          { label: `Exclude & delete ${what}…`, action: () => this.selExclude("search", "delete") },
          { separator: true },
          { label: `Copy ${sel.length} paths`, action: () => this.copyText(sel.map((r) => r.full_path).join("\n")) },
          { label: "Clear selection", action: () => this.clearSelection("search") },
        ];
      }
      const item = { source: d.source, full_path: d.full_path };
      return [
        { label: "Details", action: () => this.openFileDetail(d) },
        { label: "Show in Tree", action: () => this.jumpToTree(d) },
        { label: "Show source in Snapshots", action: () => { this.snapSource = d.source; this.active = "snapshots"; this.loadSnapshots(); } },
        { separator: true },
        { label: "Restore…", action: () => this.openRestore([item]) },
        { label: "Download", action: () => this.downloadItem(item) },
        { label: this.mountOpenLabel() + " (latest)", action: () => this.mountFileOpen(d) },
        { label: "Exclude…", action: () => this.openExclude({ kind: "file", path: d.full_path, source: d.source }, "exclude") },
        { label: "Prune versions…", action: () => this.openPrune(d.full_path, d.source) },
        { separator: true },
        { label: "Delete from backups…", action: () => this.openDeletePanel([item]) },
        { label: "Exclude & delete…", action: () => this.openExclude({ kind: "file", path: d.full_path, source: d.source }, "delete") },
        { separator: true },
        { label: "Copy path", action: () => this.copyText(d.full_path) },
        { label: "Copy source", action: () => this.copyText(d.source) },
      ];
    },

    exportSearch() {
      if (this.groupByFolder) return downloadTable("#groupTable", "kopialens_folders_repo" + this.repo);
      if (this.searchAllRepos()) return downloadTable("#searchTable", "kopialens_search_all_repos");
      if (this.searchKind === "both") return downloadTable("#searchTable", "kopialens_files_folders_repo" + this.repo);
      const p = this.searchParams();
      window.open(`/api/search/export?${p.toString()}`, "_blank");
    },

    // ---- Dedupe (Search) ----
    toggleDedupe() { this.dedupe = !this.dedupe; this.searchPage = 1; this.klSet({ search_dedupe: this.dedupe }); this.loadSearch(); },

    // ---- All-versions sizes for the rows on screen ----
    _versionKey(d) { return (d.repo || this.repo) + "|" + d.source + "|" + d.full_path; },
    versionCell(d, which) {
      const exact = d[which + "_all_versions"];   // present when sorted by this column
      if (exact != null) return fmtBytes(exact);
      const v = this.versionSizes[this._versionKey(d)];
      if (v === undefined) return "<span style='color:var(--faint)'>—</span>";
      if (v === "pending") return "<span style='color:var(--faint)'>…</span>";
      if (v[which] == null) return "<span style='color:var(--muted)' title='Filled in by the next index run'>not yet</span>";
      return fmtBytes(v[which]);
    },
    fillVersionSizes() {
      if (this._versionTimer) clearTimeout(this._versionTimer);
      this._versionTimer = setTimeout(() => this._fillVersionSizes(), 250);
    },
    async _fillVersionSizes() {
      const el = document.querySelector("#searchTable");
      const t = el && el._tabulator;
      if (!t || this.active !== "search" || this.groupByFolder) return;
      const repo = this.repo;
      const need = t.getRows("visible").map((r) => r.getData())
        .filter((d) => d.full_path && d.kind !== "folder" && !(this._versionKey(d) in this.versionSizes)).slice(0, 60);
      if (!need.length) return;
      for (const d of need) this.versionSizes[this._versionKey(d)] = "pending";
      const redraw = () => t.getRows("visible").forEach((r) => r.reformat());
      redraw();
      // Rows of several repositories (All repositories) are asked for per repository.
      const byRepo = {};
      for (const d of need) (byRepo[d.repo || repo] = byRepo[d.repo || repo] || []).push(d);
      try {
        await Promise.all(Object.entries(byRepo).map(async ([rid, list]) => {
          const r = await getJson("/api/search/versions", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ repo: rid, rows: list.map((d) => ({ source: d.source, full_path: d.full_path, content_id: d.content_id || null })) }) });
          for (const x of r.rows || []) this.versionSizes[rid + "|" + x.source + "|" + x.full_path] = { stored: x.stored, frees: x.frees };
        }));
      } catch (e) {
        for (const d of need) if (this.versionSizes[this._versionKey(d)] === "pending") delete this.versionSizes[this._versionKey(d)];
        this.error = e.message || String(e);
        return;
      }
      if (repo !== this.repo) return;
      redraw();
      this.fillVersionSizes();
    },
};
