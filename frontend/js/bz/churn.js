// Backblaze churn: recently-added folders by drive and depth.
const bzChurnPart = {

    // ---- Churn ----
    async bzLoadChurn() {
      await this.wrap(async () => {
        const r = await getJson(`/api/backblaze/churn?top=200&depth=${this.bzChurnDepth}&collapse=${this.bzChurnCollapse ? 1 : 0}`);
        this.bzChurnTotal = r.total_recent || 0;
        this._bzChurnAll = r.rows || [];
        this._bzDrawChurn();
      }, "Loading churn…");
    },
    bzChurnChanged() { this.klSet({ bz_churn_depth: this.bzChurnDepth, bz_churn_collapse: this.bzChurnCollapse }); this.bzLoadChurn(); },
    bzToggleChurnActive() { this.bzChurnActiveOnly = !this.bzChurnActiveOnly; this._bzDrawChurn(); },
    _bzDrawChurn() {
      const all = this._bzChurnAll || [];
      let rows = this.bzChurnActiveOnly ? all.filter((r) => r.status === "active") : all;
      if (this.bzChurnDrive) rows = rows.filter((r) => r.drive === this.bzChurnDrive);
      const cs = (s) => { const m = { active: ["#dcfce7", "#166534"], excluded: ["#fef9c3", "#854d0e"], gone: ["#e2e8f0", "#475569"] }[s] || ["#e2e8f0", "#475569"]; return `<span class="pill" style="background:${m[0]};color:${m[1]}">${s}</span>`; };
      setTable("#bzChurnTable", rows, [
        { title: "Folder (recently added)", field: "folder", widthGrow: 5, minWidth: 270 },
        { title: "Status", field: "status", width: 88, formatter: (c) => cs(c.getValue()) },
        { title: "New IDs", field: "count", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
        { title: "% recent", field: "pct", width: 85, hozAlign: "right", sorter: "number", formatter: (c) => `${fmtFix(c.getValue() || 0, 1)}%` },
        { title: "Backed now", field: "backed_now", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
        { title: "Size", field: "bytes_now", width: 92, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        { title: "Δ vs first", field: "delta", width: 92, hozAlign: "right", sorter: "number", formatter: (c) => { const v = c.getValue(); if (v === null || v === undefined) return "<span style='color:#cbd5e1'>—</span>"; const col = v > 0 ? "#dc2626" : (v < 0 ? "#16a34a" : "#64748b"); return `<span style="color:${col}">${v > 0 ? "+" : ""}${Number(v).toLocaleString(NUM_LOCALE)}</span>`; } },
        { title: "Drive", field: "drive", width: 58, formatter: (c) => c.getValue() ? c.getValue() + ":" : "" },
      ], {
        initialSort: [{ column: "count", dir: "desc" }],
        rowMenu: [
          { label: "Generate exclusion for this folder", action: (_e, row) => { const f = row.getData().folder; const rel = f.length > 2 && f[1] === ":" ? f.slice(2) : f; this.bzOpenGen("folder", rel.replace(/\\+$/, "") + "\\"); } },
          { label: "Find in search", action: (_e, row) => { this.query = row.getData().folder; this.active = "bz-search"; this.bzLoadSearch(); } },
        ],
      });
    },
    bzChurnAlerts() {
      // active churners (still backed) that aren't an intentional Kopia repo = "something new started churning".
      return (this._bzChurnAll || []).filter((r) => r.status === "active" && r.pct >= 3 && !/kopia/i.test(r.folder)).slice(0, 4);
    },
};
