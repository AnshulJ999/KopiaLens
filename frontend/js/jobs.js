// Job runner: one shared panel for every long kopia action (remove/delete/restore/maintenance
// + dry runs). Backend: app/core/jobs.py + /api/jobs/*. Run-bar wiring (merge step): call
// startJob(kind, params, label) to launch + open a job, openJobPanel(jobId) to open one.
const jobsPart = {

    async startJob(kind, params, label) {
      const r = await getJson("/api/jobs/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, repo: this.repo, params, label }) }).catch((e) => { this.error = e.message; return null; });
      if (!r) return null;
      this.openJobPanel(r.job_id);
      return r.job_id;
    },

    async openJobPanel(jobId) {
      const seq = ++this._jobOpenSeq;
      const s = await getJson(`/api/jobs/status?job_id=${encodeURIComponent(jobId)}&after=0`).catch((e) => { this.error = e.message; return null; });
      if (!s || seq !== this._jobOpenSeq) return;
      this.jobEarly = 0;
      this.jobLines = [];
      this.jobCursor = 0;
      this.jobFollow = true;
      this.jobExpanded = false;
      this.jobPanel = { job_id: s.job_id, kind: s.kind, kind_label: s.kind_label, repo: s.repo,
        label: s.label, dry: s.dry, mutating: s.mutating, log_path: s.log_path, commands_text: s.commands_text };
      this.showJob = true;
      this._jobApply(s);
      this._armJobPoll();
    },

    closeJob() {  // also "Run in background" - the job keeps running; the header chip finds it
      if (this._jobPoll) { clearInterval(this._jobPoll); this._jobPoll = null; }
      this.showJob = false;
      this.jobPanel = null;
      this.jobLines = [];
      this.jobEarly = 0;
      this.jobCursor = 0;
    },

    _armJobPoll() {
      if (this._jobPoll) clearInterval(this._jobPoll);
      this._jobPoll = setInterval(() => { if (this.showJob && this.jobPanel) this._jobTick(); }, 1000);
    },

    async _jobTick() {
      if (!this.jobPanel) return;
      const s = await getJson(`/api/jobs/status?job_id=${encodeURIComponent(this.jobPanel.job_id)}&after=${this.jobCursor}`).catch(() => null);
      if (!s || !this.jobPanel || s.job_id !== this.jobPanel.job_id) return;
      this._jobApply(s);
    },

    _jobApply(s) {
      if (!this.jobPanel) return;
      Object.assign(this.jobPanel, { state: s.state, rc: s.rc, error: s.error, total: s.total,
        done: s.done, errors: s.errors, line_count: s.line_count, elapsed: s.elapsed, skipped: s.skipped || 0 });
      if (s.lines && s.lines.length) {
        this.jobLines.push(...s.lines);
        if (this.jobLines.length > 5000) {  // render buffer cap; the log file always has everything
          this.jobEarly += this.jobLines.length - 5000;
          this.jobLines = this.jobLines.slice(-5000);
        }
        this.jobCursor = s.cursor;
        if (this.jobFollow) this.$nextTick(() => this._jobScrollBottom());
      }
    },

    jobView() {  // Clean collapses per-file spam; the render window is the tail so 100k-line runs stay cheap
      const clean = this.jobMode === "clean";
      let hidden = 0;
      const kept = [];
      for (const ln of this.jobLines) {
        if (clean && JOB_LINE_NOISE.test(ln)) { hidden++; continue; }
        kept.push(ln);
      }
      const cut = Math.max(0, kept.length - 400);
      return { lines: cut ? kept.slice(cut) : kept, cut: cut + this.jobEarly, hidden };
    },

    jobLineClass(ln) {
      return /error|warn|unable|failed|cannot|abort/i.test(ln) ? "w" : "";
    },

    jobLineLabel() {
      if (!this.jobPanel) return "";
      const v = this.jobView();
      let t = `${this.jobPanel.line_count || 0} line${(this.jobPanel.line_count || 0) === 1 ? "" : "s"}`;
      if (v.hidden) t += ` · ${v.hidden} filtered`;
      if (v.cut) t += ` · ${v.cut} earlier in log`;
      return t;
    },

    jobProgress() {
      const p = this.jobPanel;
      return p && p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : null;
    },

    jobProgressLabel() {
      const p = this.jobPanel;
      if (!p) return "";
      if (p.total) return `${p.done} of ${p.total} batch(es) · ${this.jobProgress()}%` + (p.errors ? ` · ${p.errors} errored` : "");
      if (p.state === "running") return "running";
      return p.total ? `${p.done} of ${p.total}` : "done";
    },

    jobStateLine() {
      const p = this.jobPanel;
      if (!p) return "";
      if (p.state === "running") return this.jobPanel.dry ? "Dry run - nothing is being changed" : "";
      if (p.state === "stopped") return "Stopped" + (p.errors ? ` · ${p.errors} batch(es) had errored` : "");
      if (p.state === "failed") return "Failed" + (p.error ? `: ${p.error}` : "");
      if (p.errors) return `Finished · ${p.errors} batch(es) errored`;
      return "Finished";
    },

    jobFooterNote() {
      const p = this.jobPanel;
      if (!p) return "";
      if (p.kind === "maintenance") return "Kopia reclaims space during this run.";
      if (p.kind === "restore") return `Writes to ${p.label.replace(/^to /, "")}.`;
      return "Space frees on the next maintenance run.";
    },

    jobElapsed() {
      const p = this.jobPanel;
      if (!p || !p.elapsed) return "0s";
      const s = Math.round(p.elapsed);
      if (s < 60) return `${s}s`;
      const m = Math.floor(s / 60);
      if (m < 60) return `${m}m ${s % 60}s`;
      return `${Math.floor(m / 60)}h ${m % 60}m`;
    },

    _jobScrollBottom() {
      const el = this.$refs.jobTerm;
      if (el) el.scrollTop = el.scrollHeight;
    },

    jobOnScroll() {  // auto-follow releases on scroll-up, re-arms at the bottom
      const el = this.$refs.jobTerm;
      if (!el) return;
      this.jobFollow = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    },

    jobSetMode(m) {
      this.jobMode = m;
      localStorage.setItem("kl-jobmode", m);
    },

    jobCopyCommand() {
      if (this.jobPanel && this.jobPanel.commands_text) this.copyText(this.jobPanel.commands_text);
    },

    async jobStop() {
      if (!this.jobPanel || this.jobPanel.state !== "running") return;
      await getJson("/api/jobs/stop", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: this.jobPanel.job_id }) }).catch((e) => { this.error = e.message; });
    },

    // ---- Header chip (the way back into a backgrounded job) ----
    armJobChip() {
      if (this._jobChipPoll) clearInterval(this._jobChipPoll);
      this._jobChipPoll = setInterval(() => { if (!document.hidden) this._jobChipTick(); }, 5000);
      this._jobChipTick();
    },

    async _jobChipTick() {
      const r = await getJson("/api/jobs/list").catch(() => null);
      if (r) this.runningJobs = (r.jobs || []).filter((j) => j.state === "running");
    },
};

// Clean-mode noise: the per-file action lines a big remove emits once per file; the plan
// already knows the file list, so the pane shows what is being processed instead.
const JOB_LINE_NOISE = /^will remove file /i;
