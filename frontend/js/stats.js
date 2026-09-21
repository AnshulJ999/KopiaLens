// Stats tab: parsed kopia cards, health checks, blob stats. The audit trail moved to the
// Logs tab () - Stats stays repository facts + space/dedup + health.
const statsPart = {

    // ---------- Stats ----------
    async loadStats(refresh = false) {
      if (this._statsLoading) return;        // never let stats requests pile up
      this._statsLoading = true;
      try {
        await this.wrap(async () => {
          this.statsData = await getJson(`/api/stats?repo=${encodeURIComponent(this.repo)}`);
        }, "Loading stats...");
      } finally { this._statsLoading = false; }
    },
    async loadBlobStats() {
      if (this.blobLoading) return;
      this.blobLoading = true;
      try {
        await this.wrap(async () => {
          const r = await getJson(`/api/stats/blob?repo=${encodeURIComponent(this.repo)}`);
          this.blobStats = r.blob_stats || "(empty)";
        }, "Running blob stats (slow, one-off)...");
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
