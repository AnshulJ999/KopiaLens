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
};
