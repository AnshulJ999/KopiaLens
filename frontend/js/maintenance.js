// Maintenance panel: run kopia maintenance as a job (Stats card, and after a delete finishes).
const maintenancePart = {

    async openMaint(repo) {
      const id = repo || this.repo;
      const seq = ++this._maintSeq;
      this.maint = { repo: id, full: true, safety: "full", info: null, loading: true, error: "" };
      this.showMaint = true;
      try {
        const s = await getJson(`/api/stats?repo=${encodeURIComponent(id)}`);
        if (seq !== this._maintSeq || !this.maint) return;
        this.maint.info = (s.parsed && s.parsed.maintenance) || {};
        this.maint.storage = s.parsed || {};
      } catch (e) {
        if (seq === this._maintSeq && this.maint) this.maint.error = e.message || String(e);
      } finally {
        if (seq === this._maintSeq && this.maint) this.maint.loading = false;
      }
    },

    closeMaint() {
      this.showMaint = false;
      this.maint = null;
    },

    maintRepoName() {
      const m = this.maint;
      const r = m && (this.repos || []).find((x) => x.id === m.repo);
      return r ? r.name : (m ? `repo ${m.repo}` : "");
    },

    maintNotOwner() {
      const i = this.maint && this.maint.info;
      return !!(i && i.owner_match === false);
    },

    maintFacts() {
      const m = this.maint, i = m.info || {}, st = m.storage || {};
      const facts = [
        ["Repository", this.maintRepoName()],
        ["Type", m.full ? "Full" : "Quick"],
        ["Safety", m.safety === "none" ? "None" : "Normal"],
        ["Owner", i.owner || "unknown"],
      ];
      if (i.last_run) facts.push(["Last run", this.humanTime(i.last_run)]);
      if (st.capacity) facts.push(["Disk", `${fmtBytes(st.available || 0)} free of ${fmtBytes(st.capacity)}`]);
      return facts;
    },

    async maintStart() {
      const m = this.maint;
      if (!m || m.loading || this.maintNotOwner()) return;
      const none = m.safety === "none";
      const ok = await this.klConfirm({
        title: `Run ${m.full ? "full" : "quick"} maintenance?`,
        facts: this.maintFacts(),
        callout: none ? "Safety none deletes unused data immediately. Run it only when no snapshot is in progress on this repository." : "",
        note: m.full ? "A full run after a large delete can take hours. It keeps running in the background." : "",
        confirmLabel: `Run ${m.full ? "full" : "quick"} maintenance`, danger: none,
      });
      if (!ok || !this.maint) return;
      const jobId = await this.startJob("maintenance", { full: m.full, safety: m.safety },
        `${m.full ? "full" : "quick"} maintenance${none ? ", safety none" : ""}`, m.repo);
      if (jobId) this.closeMaint();
    },
};
