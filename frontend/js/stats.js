// Stats tab: parsed kopia cards, health checks, audit trail, blob stats.
const statsPart = {

    // ---------- Stats ----------
    async loadStats(refresh = false) {
      if (this._statsLoading) return;        // never let stats requests pile up
      this._statsLoading = true;
      try {
        await this.wrap(async () => {
          const [stats, aud] = await Promise.all([
            getJson(`/api/stats?repo=${encodeURIComponent(this.repo)}`),
            getJson(`/api/audit?limit=200`).catch(() => ({ records: [], size: 0 })),
          ]);
          this.statsData = stats;
          this.auditRows = aud.records || [];
          this.auditSize = aud.size || 0;
        }, "Loading stats...");
      } finally { this._statsLoading = false; }
    },
    async loadAudit() {
      const aud = await getJson(`/api/audit?limit=200`).catch(() => ({ records: [], size: 0 }));
      this.auditRows = aud.records || [];
      this.auditSize = aud.size || 0;
    },
    auditSummary(r) {
      const s = r.summary || {};
      return Object.entries(s).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ");
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
