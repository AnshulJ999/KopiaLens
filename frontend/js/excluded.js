// Excluded browser: disk coverage gaps vs both repos (Everything es.exe disk side).
const excludedPart = {

    // ---- Excluded browser (local-disk coverage gaps vs both repos; es.exe disk side) ----
    async loadExcluded() {
      if (!this.excludedDrives.length) {
        const d = await getJson("/api/excluded/drives").catch(() => null);
        if (d) { this.excludedDrives = d.drives || []; this.excludedOk = !!d.everything_ok; this.diskLookup = d.disk_lookup || null; }
      }
      if (this.excludedPath) return this.listExcluded(this.excludedPath);
      this.renderExcludedDrives();
    },
    renderExcludedDrives() {
      this.excludedPath = "";
      this.excludedMeta = this.excludedOk ? `${this.excludedDrives.length} drive(s)` : "Disk lookup is not available";
      this.renderExcluded(this.excludedDrives.map((dr) => ({
        name: dr.path.replace(/\\$/, ""), path: dr.path, is_dir: true, status: "drive",
        total_size: dr.total_size || 0, disk_bytes: dr.total_size || 0,
        backed_bytes: dr.backed_bytes || 0, excluded_bytes: dr.excluded_bytes || 0,
        pct_excluded: dr.total_size ? Math.round((dr.excluded_bytes / dr.total_size) * 1000) / 10 : null,
        total_files: dr.total_files || 0, backed_files: dr.backed_files || 0, excluded_files: dr.excluded_files || 0,
        bz_backed_bytes: dr.bz_backed_bytes || 0, bz_backed_files: dr.bz_backed_files || 0,
        last_backed: "", repo: "", mtime: "",
      })));
    },
    excludedNav(path) { if (!path) return this.renderExcludedDrives(); this.listExcluded(path); },
    async listExcluded(path) {
      const seq = ++this._excludedSeq;
      this.excludedPath = path;
      await this.wrap(async () => {
        const r = await getJson(`/api/excluded/list?path=${encodeURIComponent(path)}`);
        if (seq !== this._excludedSeq) return;  // a newer navigation superseded this one
        this.excludedMeta = `${r.count} items · ${fmtBytes(r.total_excluded)} not backed up here`;
        this.renderExcluded(r.items || []);
      }, "Listing (Everything)…");
    },
    excludedRowOpen(row) {
      if (row.status === "drive") return this.listExcluded(row.path);
      if (!row.is_dir) return;
      const base = this.excludedPath.replace(/[\\/]+$/, "");
      this.listExcluded(base + "\\" + row.name);
    },
    excludedUp() {
      const p = (this.excludedPath || "").replace(/[\\/]+$/, "");
      if (!p) return;
      if (/^[A-Za-z]:$/.test(p)) return this.renderExcludedDrives();
      const idx = p.lastIndexOf("\\");
      this.listExcluded(idx <= 2 ? p.slice(0, 2) + "\\" : p.slice(0, idx));
    },
    excludedCrumbs() {
      const out = [{ label: "This PC", path: "" }];
      if (!this.excludedPath) return out;
      const p = this.excludedPath.replace(/[\\/]+$/, "");
      let acc = "";
      p.split("\\").forEach((seg, i) => {
        acc = i === 0 ? seg + "\\" : acc + (acc.endsWith("\\") ? "" : "\\") + seg;
        out.push({ label: seg, path: acc });
      });
      return out;
    },
    setExcludedFilter(f) { this.excludedFilter = f; this._drawExcluded(); },
    renderExcluded(items) { this._excludedItems = items; this._drawExcluded(); },
    _drawExcluded() {
      const f = this.excludedFilter;
      const items = (this._excludedItems || []).filter((it) => {
        if (f === "all" || it.status === "drive") return true;
        if (f === "backed") return it.status === "backed" || it.status === "partial";
        return it.status !== "backed";  // "Not backed up" view = excluded / partial / new
      });
      const pill = (s) => {
        const m = { backed: ["var(--ok-bg)", "var(--ok)", "backed up"], excluded: ["var(--danger-bg)", "var(--danger)", "NOT backed up"], partial: ["var(--warn-bg)", "var(--warn)", "partial"], new: ["var(--accent-bg)", "var(--accent)", "new (not yet)"], drive: ["var(--surface2)", "var(--muted)", "drive"] };
        const [bg, fg, label] = m[s] || ["var(--surface2)", "var(--muted)", s];
        return `<span class="pill" style="background:${bg};color:${fg}">${label}</span>`;
      };
      const bytes = (c) => c.getValue() ? fmtBytes(c.getValue()) : "";
      const num = (c) => c.getValue() ? Number(c.getValue()).toLocaleString() : "";
      const cols = [
        { title: "Name", field: "name", widthGrow: 4, minWidth: 220, formatter: (c) => (c.getRow().getData().is_dir ? "📁 " : "📄 ") + c.getValue() },
        { title: "Status", field: "status", width: 115, formatter: (c) => pill(c.getValue()) },
        { title: "Total", field: "total_size", width: 90, hozAlign: "right", sorter: "number", formatter: bytes },
        { title: "Backed", field: "backed_bytes", width: 90, hozAlign: "right", sorter: "number", formatter: bytes },
        { title: "Excluded", field: "excluded_bytes", width: 95, hozAlign: "right", sorter: "number", formatter: bytes },
        { title: "% Excl", field: "pct_excluded", width: 70, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() != null ? c.getValue() + "%" : "" },
        { title: "Files", field: "total_files", width: 80, hozAlign: "right", sorter: "number", formatter: num },
        { title: "Bkd #", field: "backed_files", width: 75, hozAlign: "right", sorter: "number", formatter: num },
        { title: "Excl #", field: "excluded_files", width: 75, hozAlign: "right", sorter: "number", formatter: num },
        { title: "Last backed", field: "last_backed", width: 145, formatter: (c) => (c.getValue() ? timeCell(c.getValue()) : "") },
        { title: "Repo", field: "repo", width: 80 },
        { title: "Also in BZ", field: "bz_backed_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() ? fmtBytes(c.getValue()) : "<span style='color:var(--faint)'>—</span>" },
        { title: "Modified", field: "mtime", width: 145, formatter: (c) => (c.getValue() ? timeCell(c.getValue()) : "") },
      ];
      const isDrives = this.excludedPath === "";
      setTable("#excludedTable", items, cols, {
        persistSort: !isDrives,  // drives view = always alphabetical; folders keep your persisted sort
        initialSort: isDrives ? [{ column: "name", dir: "asc" }] : undefined,
        onRowDblClick: (_e, row) => this.excludedRowOpen(row.getData()),
        rowMenu: [
          { label: "Open", action: (_e, row) => this.excludedRowOpen(row.getData()) },
          { label: "Details", action: (_e, row) => this.openExcludedDetail(row.getData()) },
          { label: "Include in Kopia… (copy commands)", action: (_e, row) => this.excludedInclude(row.getData()) },
          { label: "Exclude…", action: (_e, row) => this.excludedExclude(row.getData()) },
          { label: "Copy path", action: (_e, row) => this.copyText(this._excludedPath(row.getData())) },
        ],
      });
    },
    _excludedPath(row) {
      if (row.status === "drive") return row.path;
      return this.excludedPath.replace(/[\\/]+$/, "") + "\\" + row.name;
    },
    async excludedInclude(row) {
      const path = this._excludedPath(row);
      const r = await getJson(`/api/excluded/include-cmds?path=${encodeURIComponent(path)}`).catch((e) => { this.error = e.message; return null; });
      if (!r) return;
      const txt = (r.commands || []).map((c) => `# ${c.name}\n${c.command}`).join("\n\n");
      this.copyText(txt);
      this.actionText = "Include commands (copied — run the one for the repo you want):\n" + txt;
      this.klNotify("Include commands copied - run the one for the backup you want.");
    },
    excludedExclude(row) {
      // The real Exclude dialog, not clipboard text: it resolves the backups holding the path and
      // writes the rule through the same review as everywhere else.
      if (row.status === "drive") { this.klNotify("Open a folder or file first - a whole drive is not a rule target.", "warn"); return; }
      const path = this._excludedPath(row);
      this.showExcludedDetail = false;
      this.openExclude({ kind: row.is_dir ? "folder" : "file", path });
    },
    openExcludedDetail(row) {
      this.excludedDetail = { ...row, full_path: this._excludedPath(row) };
      this.showExcludedDetail = true;
    },
};
