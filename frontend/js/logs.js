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
      return this.auditRows.filter((r) => this.logsRepo === "all" || (r.repo || "") === this.logsRepo);
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
      const parts = Object.entries(stages).map(([k, v]) => `${k} ${v}s`);
      if ((r.skipped || []).length) parts.push(`skipped: ${r.skipped.join(", ")}`);
      return parts.join(" · ");
    },
    runErrorText(r) {
      if (r.ok) {
        const errs = r.errors || {};
        const failed = r.analysis_failed_sources || [];
        const bits = Object.entries(errs).map(([k, v]) => `${k}: ${v}`);
        if (failed.length) bits.push(`${failed.length} source(s) failed analysis`);
        return bits.join(" · ");
      }
      return r.reason || "failed";
    },

    // ---- App log: the whole app writes here; no repo split exists or is wanted ----
    async loadAppLog() {
      const seq = ++this._logsSeq;
      const r = await getJson(`/api/logs/app?limit=500`).catch(() => ({ lines: [], error: true }));
      if (seq !== this._logsSeq || this.logsTab !== "app") return;
      this.appLogLines = (r.lines || []).join("\n");
      this.appLogTruncated = !!r.truncated;
    },
};
