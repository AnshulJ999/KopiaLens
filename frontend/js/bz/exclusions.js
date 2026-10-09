// Backblaze exclusions: rule impact, naming-gap hunter, exclusion generator.
const bzExclusionsPart = {

    // ---- Exclusions + naming-gap + generate ----
    async bzLoadExclusions() {
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/exclusions");
        this.bzExclusions = r;
        setTable("#bzImpactTable", r.impact || [], [
          { title: "Rule", field: "rule", widthGrow: 4, minWidth: 280 },
          { title: "Files", field: "files", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Size", field: "bytes", width: 130, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], { initialSort: [{ column: "bytes", dir: "desc" }] });
      }, "Loading exclusions…");
    },
    async bzNamingGap() {
      if (!this.bzNamingQuery.trim()) { this.bzNamingRows = []; setTable("#bzNamingTable", [], []); return; }
      await this.wrap(async () => {
        const r = await getJson(`/api/backblaze/naming-gap?q=${encodeURIComponent(this.bzNamingQuery)}`);
        this.bzNamingRows = r.rows || [];
        setTable("#bzNamingTable", this.bzNamingRows, [
          { title: "Folder", field: "folder", widthGrow: 5, minWidth: 300 },
          { title: "Status", field: "status", width: 100, formatter: (c) => statusPill(c.getValue()) },
          { title: "Backed #", field: "backed_files", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Excluded #", field: "excluded_files", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Backed size", field: "backed_bytes", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], {
          initialSort: [{ column: "backed_bytes", dir: "desc" }],
          rowMenu: [
            { label: "Generate exclusion (this folder)", action: (_e, row) => { const f = row.getData().folder; const rel = f.length > 2 && f[1] === ":" ? f.slice(2) : f; this.bzOpenGen("folder", rel.replace(/\\+$/, "") + "\\"); } },
            { label: "Show in Search", action: (_e, row) => { this.query = row.getData().folder; this.active = "bz-search"; this.bzLoadSearch(); } },
          ],
        });
      }, "Hunting naming gaps…");
    },
    bzOpenGen(kind, value) { this.bzGen = { kind, value, result: null }; this.bzShowGen = true; this.bzDoGen(); },
    async bzDoGen() {
      if (!this.bzGen) return;
      const r = await getJson("/api/backblaze/exclusions/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: this.bzGen.kind, value: this.bzGen.value }) }).catch((e) => ({ error: e.message }));
      if (this.bzGen) this.bzGen.result = r;
    },
};
