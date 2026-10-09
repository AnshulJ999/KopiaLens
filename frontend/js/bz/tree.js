// Backblaze tree: per-drive folder browsing with backed/excluded states.
const bzTreePart = {

    // ---- Tree ----
    bzOpenTree() {
      if (!this.bzTreeDrive) this.bzTreeDrive = (this.bzStatus.drives && this.bzStatus.drives[0] && this.bzStatus.drives[0].drive) || "C";
      if (this.bzTreeDrive !== "*ALL*" && (!this.bzTreePrefix || this.bzTreePrefix[0].toUpperCase() !== this.bzTreeDrive)) this.bzTreePrefix = this.bzTreeDrive + ":\\";
      return this.bzLoadTree();
    },
    bzTreeChanged() { this.bzTreePrefix = this.bzTreeDrive === "*ALL*" ? "" : this.bzTreeDrive + ":\\"; this.klSet({ bz_tree_drive: this.bzTreeDrive }); this.bzLoadTree(); },
    bzTreeNav(prefix) { this.bzTreePrefix = prefix; this.bzLoadTree(); },
    bzTreeCrumbs() {
      const out = [];
      if (this.bzTreeDrive === "*ALL*") out.push({ label: "All drives", prefix: "" });
      const p = (this.bzTreePrefix || "").replace(/\\+$/, "");
      if (p) {
        let acc = "";
        p.split("\\").forEach((seg, i) => { acc = i === 0 ? seg + "\\" : acc + (acc.endsWith("\\") ? "" : "\\") + seg; out.push({ label: seg || acc, prefix: acc }); });
      }
      return out;
    },
    bzTreeUp() { const c = this.bzTreeCrumbs(); if (c.length > 1) this.bzTreeNav(c[c.length - 2].prefix); },
    async bzLoadTree() {
      const seq = ++this._bzTreeSeq;
      await this.wrap(async () => {
        const atRoot = this.bzTreeDrive === "*ALL*" && !this.bzTreePrefix;
        let kids;
        if (atRoot) {  // All-drives top level = the drive roots (with real per-drive bytes from overview)
          if (!this.bzOverview) this.bzOverview = await getJson("/api/backblaze/overview").catch(() => null);
          const ov = {}; ((this.bzOverview && this.bzOverview.rows) || []).forEach((r) => { ov[r.drive] = r; });
          kids = (this.bzStatus.drives || []).map((d) => {
            const o = ov[d.drive] || {};
            return { name: d.drive + ":", is_dir: true, status: "drive", _root: d.drive + ":\\", excl_rule: "",
                     backed_bytes: o.backed_bytes || 0, excluded_bytes: o.excluded_bytes || 0, total_files: d.total };
          });
        } else {
          const data = await getJson(`/api/backblaze/folder?prefix=${encodeURIComponent(this.bzTreePrefix)}`);
          kids = data.children || [];
        }
        if (seq !== this._bzTreeSeq) return;  // a newer navigation superseded this one
        const tot = kids.reduce((a, c) => a + ((c.backed_bytes || 0) + (c.excluded_bytes || 0)), 0) || 1;
        kids.forEach((c) => { c.total_bytes = (c.backed_bytes || 0) + (c.excluded_bytes || 0); c.pct = 100 * c.total_bytes / tot; });
        setTable("#bzTreeTable", kids, [
          { title: "Name", field: "name", widthGrow: 5, minWidth: 280, formatter: (c) => `${c.getRow().getData().is_dir ? "📁 " : "📄 "}${c.getValue()}` },
          { title: "Status", field: "status", width: 90, formatter: (c) => statusPill(c.getValue()) },
          { title: "Total", field: "total_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "%", field: "pct", width: 68, hozAlign: "right", sorter: "number", formatter: (c) => `${fmtFix(c.getValue() || 0, 1)}%` },
          { title: "Backed", field: "backed_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Excluded", field: "excluded_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
          { title: "Files", field: "total_files", width: 85, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Excluded by", field: "excl_rule", widthGrow: 2, minWidth: 140, formatter: (c) => c.getValue() || "" },
        ], {
          initialSort: [{ column: atRoot ? "name" : "total_bytes", dir: atRoot ? "asc" : "desc" }],
          onRowDblClick: (_e, row) => { const d = row.getData(); if (d._root) return this.bzTreeNav(d._root); if (d.is_dir) this.bzTreeNav(this.bzTreePrefix.replace(/\\+$/, "") + "\\" + d.name); },
          rowMenu: [
            { label: "Open / drill in", action: (_e, row) => { const d = row.getData(); if (d._root) return this.bzTreeNav(d._root); if (d.is_dir) this.bzTreeNav(this.bzTreePrefix.replace(/\\+$/, "") + "\\" + d.name); } },
            { label: "Generate exclusion (this folder)", action: (_e, row) => { const d = row.getData(); const full = d._root || (this.bzTreePrefix.replace(/\\+$/, "") + "\\" + d.name); const rel = full.length > 2 && full[1] === ":" ? full.slice(2) : full; this.bzOpenGen("folder", rel.replace(/\\+$/, "") + "\\"); } },
            { label: "Generate exclusion (any folder with this name)", action: (_e, row) => this.bzOpenGen("folder", "\\" + row.getData().name + "\\") },
            { label: "Find in search", action: (_e, row) => { this.query = row.getData().name; this.active = "bz-search"; this.bzLoadSearch(); } },
            { label: "Copy path", action: (_e, row) => this.copyText(row.getData()._root || (this.bzTreePrefix.replace(/\\+$/, "") + "\\" + row.getData().name)) },
          ],
        });
      }, "Loading folder…");
    },
    bzJumpTree(item) {
      const fp = item.full_path; const drive = fp[0].toUpperCase();
      const parent = fp.includes("\\") ? fp.slice(0, fp.lastIndexOf("\\")) : drive + ":\\";
      this.bzTreeDrive = drive; this.bzTreePrefix = parent; this.active = "bz-tree"; this.bzLoadTree();
    },
};
