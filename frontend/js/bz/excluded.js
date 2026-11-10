// Backblaze excluded browser: BZ-native coverage + Kopia cross-column.
const bzExcludedPart = {

    // ---- Excluded (BZ-native coverage + Kopia cross-column) ----
    async bzLoadExcluded() {
      if (this.bzExclPath) return this.bzExclList(this.bzExclPath);
      return this.bzExclDrives();
    },
    bzExclNav(path) { if (!path) return this.bzExclDrives(); this.bzExclList(path); },
    async bzExclDrives() {
      this.bzExclPath = "";
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/excluded/drives");
        this.bzExclMeta = `${countOf(r.drives.length, "drive")} · ${bzFmt(r.grand_bytes)} backed by Backblaze`;
        this._bzExclItems = (r.drives || []).map((d) => ({
          name: d.label || d.drive, path: d.path, is_dir: true, status: "drive",
          backed_bytes: d.backed_bytes, backed_files: d.backed_files,
          excluded_bytes: d.excluded_bytes, excluded_files: d.excluded_files,
          total_bytes: (d.backed_bytes || 0) + (d.excluded_bytes || 0),
          kopia_backed_bytes: d.kopia_backed_bytes, kopia_backed_files: d.kopia_backed_files, kopia_repos: [],
        }));
        this._bzDrawExcl(true);
      }, "Loading Backblaze drives…");
    },
    async bzExclList(path) {
      const seq = ++this._bzExclSeq;
      this.bzExclPath = path;
      await this.wrap(async () => {
        const r = await getJson(`/api/backblaze/excluded/list?prefix=${encodeURIComponent(path)}`);
        if (seq !== this._bzExclSeq) return;
        this.bzExclMeta = countOf(r.count, "item");
        // Render BZ-native instantly; the slower Kopia cross-coverage fills in lazily (5B).
        // kopia_backed_bytes=null renders as "…" until the second request patches it.
        this._bzExclItems = (r.items || []).map((it) => ({ ...it, kopia_backed_bytes: null, kopia_backed_files: 0, kopia_repos: [] }));
        this._bzDrawExcl(false);
        if (!this.bzCross()) return;
        getJson(`/api/backblaze/excluded/kopia-coverage?prefix=${encodeURIComponent(path)}`).then((kr) => {
          if (seq !== this._bzExclSeq) return;  // a newer navigation superseded this fetch
          const cov = kr.coverage || {};
          const fold = this.bzSep() === "\\";   // Windows names compare case-folded, a Mac's exactly
          this._bzExclItems.forEach((it) => {
            const c = cov[fold ? it.name.toLowerCase() : it.name];
            if (c) { it.kopia_backed_bytes = c.bytes; it.kopia_backed_files = c.files; it.kopia_repos = c.repos; }
            else { it.kopia_backed_bytes = 0; }
          });
          this._bzDrawExcl(false);
        }).catch(() => {});
      }, "Listing folder…");
    },
    bzExclRowOpen(row) {
      if (row.status === "drive") return this.bzExclList(row.path);
      if (!row.is_dir) return;
      this.bzExclList(this.bzJoin(this.bzExclPath, row.name));
    },
    bzExclUp() {
      const c = this.bzCrumbs(this.bzExclPath || "");
      if (c.length <= 1) return this.bzExclDrives();
      this.bzExclList(c[c.length - 2].prefix);
    },
    bzExclCrumbs() {
      return [{ label: "All drives", path: "" }].concat(this.bzCrumbs(this.bzExclPath || "").map((c) => ({ label: c.label, path: c.prefix })));
    },
    bzSetExclFilter(f) { this.bzExclFilter = f; this._bzDrawExcl(this.bzExclPath === ""); },
    _bzDrawExcl(isDrives) {
      const f = this.bzExclFilter;
      const items = (this._bzExclItems || []).filter((it) => {
        if (f === "all" || it.status === "drive") return true;
        if (f === "backed") return it.status === "backed" || it.status === "mixed";
        return it.status !== "backed";
      });
      setTable("#bzExcludedTable", items, [
        { title: "Name", field: "name", widthGrow: 4, minWidth: 240, formatter: (c) => itemIconHtml(c.getValue(), c.getRow().getData().is_dir) + escHtml(c.getValue()) },
        { title: "Status", field: "status", width: 105, formatter: (c) => statusPill(c.getValue()) },
        { title: "Backed Up", field: "backed_bytes", width: 105, hozAlign: "right", sorter: "number", headerTooltip: "Size Backblaze backs up here", formatter: (c) => bzFmt(c.getValue()) },
        { title: "Excluded", field: "excluded_bytes", width: 105, hozAlign: "right", sorter: "number", headerTooltip: "Size Backblaze leaves out here", formatter: (c) => bzFmt(c.getValue()) },
        { title: "Backed Files", field: "backed_files", width: 110, hozAlign: "right", sorter: "number", headerTooltip: "Files Backblaze backs up here", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
        { title: "Excl Files", field: "excluded_files", width: 95, hozAlign: "right", sorter: "number", headerTooltip: "Files Backblaze leaves out here", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
        { title: "Excluded by", field: "excl_rule", widthGrow: 2, minWidth: 150, headerTooltip: "The rule that leaves these files out", formatter: (c) => escHtml(c.getValue() || "") },
        ...(this.bzCross() ? [{ title: "Also in Kopia", field: "kopia_backed_bytes", width: 130, hozAlign: "right", sorter: "number", headerTooltip: "This folder is also in a Kopia backup", formatter: (c) => { const d = c.getRow().getData(); const v = c.getValue(); if (v === null || v === undefined) return "<span class='text-faint'>…</span>"; return d.kopia_backed_files ? `${reposBadges(d.kopia_repos)} ${bzFmt(v)}` : "<span class='text-muted'>—</span>"; } }] : []),
      ], {
        persistSort: !isDrives,
        initialSort: isDrives ? [{ column: "name", dir: "asc" }] : [{ column: "excluded_bytes", dir: "desc" }],
        onRowDblClick: (_e, row) => this.bzExclRowOpen(row.getData()),
        rowMenu: [
          { label: "Open", action: (_e, row) => this.bzExclRowOpen(row.getData()) },
          { label: "Generate exclusion (this folder)", action: (_e, row) => { const d = row.getData(); this.bzGenFolder(d.status === "drive" ? d.path : this.bzJoin(this.bzExclPath, d.name)); } },
          { label: "Generate exclusion (any folder with this name)", action: (_e, row) => this.bzGenName(row.getData().name) },
          { label: "Copy path", action: (_e, row) => this.copyText(row.getData().status === "drive" ? row.getData().path : this.bzJoin(this.bzExclPath, row.getData().name)) },
        ],
      });
    },
};
