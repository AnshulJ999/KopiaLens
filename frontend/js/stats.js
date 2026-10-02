// Stats tab: parsed kopia cards, health checks, blob stats. The audit trail moved to the
// Logs tab () - Stats stays repository facts + space/dedup + health.
const statsPart = {

    // ---------- Stats ----------
    async loadStats(refresh = false) {
      if (this._statsLoading) return;        // never let stats requests pile up
      this._statsLoading = true;
      try {
        await this.wrap(async () => {
          this.statsData = await getJson(`/api/stats?repo=${encodeURIComponent(this.repo)}${refresh ? "&refresh=true" : ""}`);
        }, refresh ? "Reading from kopia..." : "Loading stats...");
      } finally { this._statsLoading = false; }
    },
    async loadBlobStats() {
      if (this.blobLoading) return;
      const ok = await this.klConfirm({
        title: "Run blob stats?",
        note: "Lists every file in the repository's storage. On a large repository this takes a long time and keeps the disk busy. You can stop it from the progress panel.",
        confirmLabel: "Run blob stats",
      });
      if (!ok) return;
      const repo = this.repo;
      const jobId = await this.startJob("blob_stats", {}, "blob stats", repo);
      if (!jobId) return;
      this.blobLoading = true;
      try {
        // The job panel shows progress and Stop; the card fills in when the job ends.
        for (;;) {
          await new Promise((r) => setTimeout(r, 3000));
          const s = await getJson(`/api/jobs/status?job_id=${encodeURIComponent(jobId)}&after=1000000`).catch(() => null);
          if (!s || s.state !== "running") break;
        }
        if (this.repo !== repo) return;
        const d = await getJson(`/api/stats?repo=${encodeURIComponent(repo)}`).catch(() => null);
        if (d && d.blob_stats) this.blobStats = d.blob_stats;
      } finally { this.blobLoading = false; }
    },
    statsBlock(v) { return typeof v === "string" ? v : JSON.stringify(v, null, 2); },

    // Nightly retention self-check (validates our retention view against kopia's own reasons).
    // Silent while it agrees - it only speaks up when something disagrees or the check fails.
    retentionCheck() {
      return (this.statsData && this.statsData.parsed && this.statsData.parsed.retention_check) || null;
    },
    retentionIssueText() {
      const c = this.retentionCheck();
      if (!c) return "";
      const at = c.at ? " · " + c.at : "";
      if (c.status === "failed") {
        return `Retention check did not run: ${c.failed || 0} of ${c.checked || 0} sources failed${at}.`;
      }
      if (c.status === "mismatch") {
        const detail = (c.detail || []).slice(0, 3).join("; ");
        return `Retention check: ${c.mismatches} of ${c.checked} sources disagree with kopia${at}${detail ? " · " + detail : ""}.`;
      }
      return "";
    },
};
