// Backblaze queue, sessions, import and health check.
const bzHealthPart = {

    // ---- Queue / Sessions / Health ----
    bzQueueChanged() { this.klSet({ bz_queue_depth: this.bzQueueDepth, bz_queue_collapse: this.bzQueueCollapse }); this.bzLoadQueue(); },
    async bzLoadQueue() {
      await this.wrap(async () => {
        const r = await getJson(`/api/backblaze/queue?depth=${this.bzQueueDepth || 1}&collapse=${this.bzQueueCollapse ? 1 : 0}`);
        this.bzQueue = r;
        setTable("#bzQueueTable", r.by_folder || [], [
          { title: "Folder", field: "folder", widthGrow: 5, minWidth: 320 },
          { title: "Files", field: "files", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Size", field: "bytes", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], { initialSort: [{ column: "bytes", dir: "desc" }] });
      }, "Loading upload queue…");
    },
    async bzLoadSessions() {
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/sessions?limit=500");
        this.bzSessions = r;
        // explicit heights so the stacked tables don't collapse/overlap inside the scrolling section
        setTable("#bzSessionsTable", r.sessions || [], [
          { title: "Time", field: "ts", width: 190, formatter: (c) => timeCell(c.getValue()) },
          { title: "Event", field: "kind", width: 130, formatter: (c) => c.getValue() === "session_end" ? "<span style='color:#16a34a'>backup ended</span>" : "backup started" },
          { title: "Files", field: "files", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() ? Number(c.getValue()).toLocaleString() : "" },
          { title: "Uploaded", field: "bytes", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() ? bzFmt(c.getValue()) : "" },
        ], { initialSort: [{ column: "ts", dir: "desc" }], height: "15rem" });
        setTable("#bzBackupDaysTable", r.backup_days || [], [
          { title: "Day", field: "day", width: 150 },
          { title: "Files backed up", field: "files", width: 150, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Uploaded", field: "bytes", width: 140, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], { initialSort: [{ column: "day", dir: "desc" }], height: "17rem" });
        setTable("#bzUploadsTable", r.uploads || [], [
          { title: "Date", field: "date", width: 170, formatter: (c) => fmtBzDate(c.getValue()) },
          { title: "Uploaded OK", field: "success", width: 130, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "Failed", field: "fail", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => { const v = Number(c.getValue() || 0); return v ? `<span style='color:#dc2626'>${v.toLocaleString()}</span>` : "0"; } },
        ], { initialSort: [{ column: "date", dir: "desc" }], height: "17rem" });
      }, "Loading sessions…");
    },
    async bzImportArchive() {
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/import-archive", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        this.bzImportMsg = r.ok ? `Imported ${r.imported} new event(s) · ${r.total} total archived.` : `Import failed: ${r.reason || "unknown"}`;
        await this.bzLoadSessions();
      }, "Importing archived history…");
    },
    async bzLoadHealth() {
      await this.wrap(async () => {
        this.bzHealth = await getJson("/api/backblaze/health");
        setTable("#bzHealthTable", this.bzHealth.drives || [], [
          { title: "Drive", field: "drive", width: 70, formatter: (c) => c.getValue() + ":" },
          { title: "Backed files", field: "backed_files", hozAlign: "right", width: 130, sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString() },
          { title: "BZ authoritative", field: "auth_files", hozAlign: "right", width: 140, sorter: "number", formatter: (c) => c.getValue() == null ? "—" : Number(c.getValue()).toLocaleString() },
          { title: "Diff", field: "diff", hozAlign: "right", width: 90, formatter: (c) => { const v = c.getValue(); return v == null ? "—" : (v === 0 ? "<span style='color:#16a34a'>+0</span>" : `<span style='color:#dc2626'>${v}</span>`); } },
        ], { initialSort: [{ column: "drive", dir: "asc" }] });
      }, "Running health check…");
    },
};
