// Logs tab (): ONE home for everything log-shaped, unified across repos - the audit
// trail (moved out of Stats), Activity (the job runner in past tense, persisted so it survives
// a restart), Indexing (per-run history from the app-side run records - stage timings, errors,
// killed runs included) and the app log. "Audit, Activity - these
// things can go in a new panel called Logs. It doesn't belong in a panel called Stats."
//
// All four sections fetch a tail and filter by repo client-side; the records already carry a
// repo field (the app log is app-wide by nature and shows as such). Every section is
// info-heavy but plain tables - debugging KopiaLens itself, not a Kopia repo.
const logsPart = {

    setLogsTab(t) {
      this.logsTab = t;
      this.loadLogs();
    },

    loadLogs() {
      if (this.logsTab === "audit") return this.loadAudit();
      if (this.logsTab === "activity") return this.loadJobsHistory();
      if (this.logsTab === "indexing") return this.loadIndexRuns();
      if (this.logsTab === "app") return this.loadAppLog();
    },

    // ---- Audit (moved from Stats,) ----
    async loadAudit() {
      const seq = ++this._logsSeq;
      const aud = await getJson(`/api/audit?limit=500`).catch(() => ({ records: [], size: 0 }));
      if (seq !== this._logsSeq || this.logsTab !== "audit") return;
      this.auditRows = aud.records || [];
      this.auditSize = aud.size || 0;
    },
    auditSummary(r) {
      const s = r.summary || {};
      return Object.entries(s).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ");
    },
    logsAudit() {
      // The API returns oldest first (the file's order); the tab shows newest first.
      return this.auditRows.filter((r) => this.logsRepo === "all" || (r.repo || "") === this.logsRepo).reverse();
    },

    // ---- Activity: job history, merged memory + disk by the backend ----
    async loadJobsHistory() {
      const seq = ++this._logsSeq;
      const r = await getJson(`/api/jobs/list`).catch(() => ({ jobs: [] }));
      if (seq !== this._logsSeq || this.logsTab !== "activity") return;
      this.jobHistory = r.jobs || [];
    },
    logsJobs() {
      return this.jobHistory.filter((j) => this.logsRepo === "all" || j.repo === this.logsRepo);
    },
    jobStateClass(j) {
      if (j.state === "running") return "text-warn";
      if (j.state === "failed" || j.errors > 0) return "text-danger";
      return "text-ok";
    },

    // ---- Indexing: per-run history, every repo in one list ----
    async loadIndexRuns() {
      const seq = ++this._logsSeq;
      const r = await getJson(`/api/index/runs?limit=200`).catch(() => ({ runs: [] }));
      if (seq !== this._logsSeq || this.logsTab !== "indexing") return;
      this.indexRuns = r.runs || [];
    },
    logsRuns() {
      return this.indexRuns.filter((r) => this.logsRepo === "all" || r.repo === this.logsRepo);
    },
    runStageText(r) {
      const stages = r.stages || {};
      const parts = Object.entries(stages).map(([k, v]) => `${k} ${fmtDur(v)}`);
      if ((r.skipped || []).length) parts.push(`skipped: ${r.skipped.join(", ")}`);
      return parts.join(" · ");
    },
    runErrorText(r) {
      if (r.ok) {
        const errs = r.errors || {};
        const failed = r.analysis_failed_sources || [];
        const bits = Object.entries(errs).map(([k, v]) => `${k}: ${v}`);
        if (failed.length) bits.push(`${countOf(failed.length, "backup")} failed analysis`);
        return bits.join(" · ");
      }
      return r.reason || "failed";
    },

    // ---- App log: the whole app writes here; no repo split exists or is wanted ----
    // Parsed records, newest first, filtered server-side by level. "debug" reads the separate
    // debug log file (only present while the debug log setting is on).
    async loadAppLog() {
      const seq = ++this._logsSeq;
      const q = `limit=500&file=${encodeURIComponent(this.appLogFile)}&level=${encodeURIComponent(this.appLogLevel)}`;
      const r = await getJson(`/api/logs/app?${q}`).catch(() => ({ records: [] }));
      if (seq !== this._logsSeq || this.logsTab !== "app") return;
      this.appLogRecords = r.records || [];
      this.appLogTruncated = !!r.truncated;
      this.appLogDebugAvailable = !!r.debug_available;
      this.appLogLoaded = true;
    },
    logLevelClass(rec) {
      if (rec.level === "ERROR" || rec.level === "CRITICAL") return "text-danger";
      if (rec.level === "WARNING" || rec.level === "WARN") return "text-warn";
      if (rec.level === "DEBUG") return "text-muted";
      return "text-muted";
    },
    logStamp(ts) {
      if (!ts) return "";
      return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit", hour12: true });
    },
};
