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
          { title: "Files", field: "files", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Size", field: "bytes", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], { initialSort: [{ column: "bytes", dir: "desc" }] });
      }, "Loading upload queue…");
    },
    async bzLoadSessions() {
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/sessions?limit=500");
        this.bzSessions = r;
        // explicit heights so the stacked tables don't collapse/overlap inside the scrolling section
        const took = (a, b) => {
          const m = Math.round((Date.parse(String(b).replace(" ", "T")) - Date.parse(String(a).replace(" ", "T"))) / 60000);
          return !isFinite(m) || m <= 0 ? "" : m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
        };
        setTable("#bzSessionsTable", r.runs || [], [
          { title: "Started", field: "started", width: 190, formatter: (c) => timeCell(c.getValue(), c) },
          { title: "Took", field: "ended", width: 110, headerSort: false, formatter: (c) => took(c.getRow().getData().started, c.getValue()) },
          { title: "Files", field: "files", width: 110, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Uploaded", field: "bytes", width: 120, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], { initialSort: [{ column: "started", dir: "desc" }], height: "15rem" });
        setTable("#bzBackupDaysTable", r.backup_days || [], [
          { title: "Day", field: "day", width: 150, formatter: (c) => fmtDate(c.getValue()) },
          { title: "Files backed up", field: "files", width: 150, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Uploaded", field: "bytes", width: 140, hozAlign: "right", sorter: "number", formatter: (c) => bzFmt(c.getValue()) },
        ], { initialSort: [{ column: "day", dir: "desc" }], height: "17rem" });
        setTable("#bzUploadsTable", r.uploads || [], [
          { title: "Date", field: "date", width: 170, formatter: (c) => fmtBzDate(c.getValue()) },
          { title: "Uploaded OK", field: "success", width: 130, hozAlign: "right", sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "Failed", field: "fail", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => { const v = Number(c.getValue() || 0); return v ? `<span class="text-danger">${v.toLocaleString(NUM_LOCALE)}</span>` : "0"; } },
        ], { initialSort: [{ column: "date", dir: "desc" }], height: "17rem" });
      }, "Loading sessions…");
    },
    async bzImportArchive() {
      await this.wrap(async () => {
        const r = await getJson("/api/backblaze/import-archive", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        this.bzImportMsg = r.ok ? `Imported ${countOf(r.imported, "new event")} · ${r.total} total archived.` : `Import failed: ${r.reason || "unknown"}`;
        await this.bzLoadSessions();
      }, "Importing archived history…");
    },
    async bzLoadHealth() {
      await this.wrap(async () => {
        this.bzHealth = await getJson("/api/backblaze/health");
        // Drive icon: a Design-menu choice under review; the app ships without one.
        const icon = ["a", "b", "d"].find((k) => this.designIs("bzdriveicon", k, "none"));
        const ic = icon ? `<i class="ic ${{ a: "ic-mount", b: "ic-drive-b", d: "ic-drive-d" }[icon]}"></i> ` : "";
        setTable("#bzHealthTable", this.bzHealth.drives || [], [
          { title: "Drive", field: "drive", width: this.bzDriveWidth(), sorter: this.bzDriveSorter(), formatter: (c) => ic + escHtml(this.bzLabel(c.getValue())) },
          { title: "Backed files", field: "backed_files", hozAlign: "right", width: 130, sorter: "number", formatter: (c) => Number(c.getValue() || 0).toLocaleString(NUM_LOCALE) },
          { title: "BZ authoritative", field: "auth_files", hozAlign: "right", width: 140, sorter: "number", headerTooltip: "The number Backblaze itself reports for this drive", formatter: (c) => c.getValue() == null ? "—" : Number(c.getValue()).toLocaleString(NUM_LOCALE) },
          { title: "Diff", field: "diff", hozAlign: "right", width: 90, headerTooltip: "KopiaLens's backed-up count minus Backblaze's own count", formatter: (c) => bzDiffHtml(c.getValue(), c.getRow().getData()) },
        ], { initialSort: [{ column: "drive", dir: "asc" }], persistenceID: this.bzTableId("bzHealthTable") });
      }, "Running health check…");
    },
    bzReleaseNotesUrl() {
      const mac = this.bzHealth && this.bzHealth.version && this.bzHealth.version.platform === "mac";
      return `https://www.backblaze.com/computer-backup/docs/backup-client-release-notes-${mac ? "mac" : "windows"}`;
    },
};
