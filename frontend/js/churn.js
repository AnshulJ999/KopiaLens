// Churn tab: new-data rollups by source / folder / file type from one cached computation.
const churnPart = {

    // ---- Churn: what added NEW backed-up data in a window (by source / folder / file type) ----
    // One backend computation feeds all three views, so the numbers can never disagree.
    async loadChurn(refresh = false) {
      const seq = ++this._churnSeq;
      await this.wrap(async () => {
        const p = new URLSearchParams({ repo: this.repo, days: this.churnDays, depth: this.churnDepth, limit: "400" });
        if (refresh) p.set("refresh", "1");
        const d = await getJson(`/api/churn?${p.toString()}`);
        if (seq !== this._churnSeq) return;   // a faster later request already won
        this.churnData = d;
        this.churnMeta = `${fmtPhys(d.total_bytes)} attributed · ${fmtPhys(d.total_bytes_deduped)} after dedup · `
          + `${Number(d.total_files || 0).toLocaleString()} files · ${Number(d.total_versions || 0).toLocaleString()} versions · `
          + (d.cached ? `cached${d.computed_at ? ` ${fmtRel(d.computed_at * 1000)}` : ""}` : `computed in ${Number(d.elapsed || 0).toFixed(1)}s`);
        this.renderChurn();
      }, "Computing churn…");
    },
    changeChurn() { this.klSet({ churn_days: this.churnDays, churn_depth: this.churnDepth, churn_view: this.churnView }); this.loadChurn(); },
    setChurnView(v) { this.churnView = v; this.klSet({ churn_view: v }); this.renderChurn(); },

    renderChurn() {
      const d = this.churnData;
      if (!d) return;
      const days = Math.max(1, Number(this.churnDays) || 30);
      const enrich = (r) => ({ ...r, per_day: (r.bytes || 0) / days, rewrite: r.files ? (r.versions || 0) / r.files : 0 });
      const bytesCol = { title: "New data", field: "bytes", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => fmtPhys(c.getValue()) };
      const rateCol = { title: "Per day", field: "per_day", width: 105, hozAlign: "right", sorter: "number",
        headerTooltip: "New data divided by the window length, so windows of different sizes compare fairly.",
        formatter: (c) => fmtPhys(c.getValue()) + "/d" };
      const verCol = { title: "Versions", field: "versions", width: 95, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(), headerTooltip: "How many file-versions were written. A big number against few files means the same files are being rewritten." };
      const rewriteCol = { title: "Rewrite", field: "rewrite", width: 90, hozAlign: "right", sorter: "number",
        headerTooltip: "Versions divided by files - how many times the average file here was rewritten.",
        formatter: (c) => { const v = Number(c.getValue() || 0);
          return v >= 5 ? `<span style="color:var(--warn)" title="Heavily rewritten">${v.toFixed(1)}×</span>` : `${v.toFixed(1)}×`; } };
      const fileCol = { title: "Files", field: "files", width: 90, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() };
      const size = (c) => { const v = c.getValue(); return v == null ? "" : fmtPhys(v); };
      const logicalCol = { title: "New (logical)", field: "logical", width: 115, hozAlign: "right", sorter: "number", formatter: size,
        headerTooltip: "The same new versions at their plain file size, before dedup and compression." };
      const nowCol = { title: "Size now", field: "size_now", width: 100, hozAlign: "right", sorter: "number", formatter: size,
        headerTooltip: "Plain size of everything here in the latest backup." };
      const physLatestCol = { title: "Physical (latest)", field: "physical_latest", width: 125, hozAlign: "right", sorter: "number", formatter: size,
        headerTooltip: "Stored size of the latest versions." };
      // Rows without a number yet (folder view fills as you scroll) always sort to the bottom.
      const missingLast = (a, b, _ar, _br, _col, dir) => {
        const ma = typeof a !== "number", mb = typeof b !== "number";
        if (ma || mb) return ma === mb ? 0 : (ma ? 1 : -1) * (dir === "desc" ? -1 : 1);
        return a - b;
      };
      const physAllCol = (onScreen) => ({ title: "Physical (all versions)", field: "physical_all", width: 150, hozAlign: "right",
        sorter: missingLast, headerTooltip: onScreen
          ? "Stored size of every version kept. Worked out for the rows on screen as you scroll; sorting uses the rows worked out so far."
          : "Stored size of every version kept.",
        formatter: (c) => { const v = c.getValue();
          if (v === false) return `<span title="Could not work this out">–</span>`;
          return v == null ? (onScreen ? "…" : "") : fmtPhys(v); } });
      const sizeCols = [logicalCol, nowCol, physLatestCol];
      let rows = [], cols = [], menu = [];
      if (this.churnView === "source") {
        rows = (d.by_source || []).map(enrich);
        cols = [
          { title: "Source", field: "source", widthGrow: 3, minWidth: 220, formatter: (c) => {
              const r = c.getRow().getData();
              const warn = r.nested_in ? ` <span title="Also covered by ${sourcePath(r.nested_in)}, so this data is snapshotted twice and counted in both rows" style="color:var(--warn)">⚠ inside ${sourcePath(r.nested_in)}</span>` : "";
              return sourcePath(c.getValue()) + warn;
            } },
          bytesCol, rateCol, verCol, rewriteCol, fileCol, ...sizeCols, physAllCol(false),
          { title: "Mostly from", field: "top_folder", widthGrow: 2, minWidth: 180, headerSort: false,
            formatter: (c) => { const r = c.getRow().getData(); return r.top_folder ? `${r.top_folder} <span style="opacity:.6">(${fmtPhys(r.top_folder_bytes)})</span>` : ""; } },
        ];
        menu = [
          { label: "Folder details for the top folder", action: (_e, row) => { const r = row.getData(); if (r.top_folder) this.openFolderPanel(r.top_folder); } },
          { label: "Browse snapshots", action: (_e, row) => { this.selectedSource = row.getData().source; this.snapSource = row.getData().source; this.active = "snapshots"; this.loadSnapshots(); } },
          { label: "Browse files (tree)", action: (_e, row) => { this.selectedSource = row.getData().source; this.active = "tree"; this.openTree(); } },
          { label: "Search this source", action: (_e, row) => { this.active = "search"; this.searchSource = row.getData().source; this.query = ""; this.loadSearch(); } },
          { label: "Simulate this source", action: (_e, row) => { this.selectedSource = row.getData().source; this.simMode = "one"; this.active = "simulate"; this.runSimulate(); } },
          { label: "Copy source", action: (_e, row) => this.copyText(row.getData().source) },
        ];
      } else if (this.churnView === "folder") {
        rows = (d.by_folder || []).map(enrich);
        cols = [
          { title: "Folder", field: "folder", widthGrow: 4, minWidth: 280 },
          bytesCol, rateCol, verCol, rewriteCol, fileCol, ...sizeCols, physAllCol(true),
          { title: "Sources", field: "source_count", width: 90, hozAlign: "right", sorter: "number",
            headerTooltip: "How many backup sources contributed churn in this folder. Several usually means sub-folders with their own policies, not duplication - open the folder to see which actually cover it.",
            formatter: (c) => { const r = c.getRow().getData(); const n = r.source_count || 0;
              return n > 1 ? `<span title="${(r.sources || []).map(sourcePath).join(", ")}" >${n}</span>` : String(n); } },
        ];
        menu = [
          { label: "Folder details", action: (_e, row) => this.openFolderPanel(row.getData().folder) },
          { label: "Find in search", action: (_e, row) => { this.query = row.getData().folder; this.searchSource = ""; this.active = "search"; this.loadSearch(); } },
          { label: "Exclude & delete…", action: (_e, row) => this.openExclude({ kind: "folder", path: row.getData().folder }, "delete") },
          { label: "Copy path", action: (_e, row) => this.copyText(row.getData().folder) },
          { label: "Delete folder (chunked)…", action: (_e, row) => this.openRemoveFolderPath(row.getData().folder) },
        ];
      } else {
        rows = (d.by_ext || []).map((r) => enrich({ ...r, ext: r.ext || "(no extension)" }));
        cols = [{ title: "File type", field: "ext", widthGrow: 2, minWidth: 160 }, bytesCol, rateCol, verCol, rewriteCol, fileCol, ...sizeCols];
        menu = [
          { label: "Search this extension", action: (_e, row) => { const r = row.getData();
              if (r.ext && r.ext !== "(no extension)") { this.active = "search"; this.searchExt = r.ext; this.loadSearch(); } } },
        ];
      }
      const table = setTable("#churnTable", rows, cols, {
        persistenceID: "churnTable_" + this.churnView,   // each view has its own columns and layout
        rowMenu: menu.length ? menu : undefined,
        onRowDblClick: (_e, row) => {
          const r = row.getData();
          if (this.churnView === "folder") this.openFolderPanel(r.folder);
          else if (this.churnView === "source") { this.active = "search"; this.searchSource = r.source; this.query = ""; this.loadSearch(); }
          else if (r.ext && r.ext !== "(no extension)") { this.active = "search"; this.searchExt = r.ext; this.loadSearch(); }
        },
      });
      if (table && this.churnView === "folder") this._churnFillPhysical(table);
      else ++this._churnPhysSeq;
    },

    // Folder view: all-versions physical for the rows on screen, one folder at a time. The server
    // caches each folder (the same cache as the folder panel), so a revisit is instant.
    _churnFillPhysical(table) {
      const seq = ++this._churnPhysSeq;
      let busy = false;
      const next = async () => {
        if (busy || seq !== this._churnPhysSeq || this.active !== "churn") return;
        const row = table.getRows("visible").find((r) => r.getData().physical_all == null);
        if (!row) return;
        busy = true;
        const folder = row.getData().folder;
        const key = this.repo + "|" + folder;
        let v = this._churnPhysCache[key];
        if (v === undefined) {
          try {
            const d = await getJson("/api/folder/detail", { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ repo: this.repo, prefix: folder }) });
            v = d && d.physical != null ? Number(d.physical) : false;
          } catch (e) { v = false; }
          if (v !== false) this._churnPhysCache[key] = v;
        }
        busy = false;
        if (seq !== this._churnPhysSeq) return;
        row.update({ physical_all: v });
        next();
      };
      table.on("tableBuilt", () => setTimeout(next, 50));
      setTimeout(next, 400);   // in case the build finished before the listener attached
      const soon = () => { clearTimeout(this._churnPhysTimer); this._churnPhysTimer = setTimeout(next, 250); };
      table.on("scrollVertical", soon);
      table.on("renderComplete", soon);   // the first build can measure one visible row
    },
};
