// Job runner: one shared panel for every long kopia action (remove/delete/restore/maintenance
// + dry runs). Backend: app/core/jobs.py + /api/jobs/*. Run-bar wiring (merge step): call
// startJob(kind, params, label) to launch + open a job, openJobPanel(jobId) to open one.
const jobsPart = {

    async startJob(kind, params, label, repo) {
      const r = await getJson("/api/jobs/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, repo: repo || this.repo, params, label }) }).catch((e) => { this.error = e.message; return null; });
      if (!r) return null;
      this.openJobPanel(r.job_id);
      return r.job_id;
    },

    async openJobPanel(jobId) {
      const seq = ++this._jobOpenSeq;
      const s = await getJson(`/api/jobs/status?job_id=${encodeURIComponent(jobId)}&after=0`).catch((e) => { this.error = e.message; return null; });
      if (!s) {  // gone from memory and history: never leave a chip that cannot open
        this._markJobSeen(jobId);
        this.runningJobs = (this.runningJobs || []).filter((j) => j.job_id !== jobId);
        return;
      }
      if (seq !== this._jobOpenSeq) return;
      if (s.state !== "running") this._markJobSeen(jobId);  // opening the panel acknowledges a finished job
      this.jobEarly = 0;
      this.jobLines = [];
      this.jobCursor = 0;
      this.jobFollow = true;
      this.jobExpanded = false;
      this.jobPanel = { job_id: s.job_id, kind: s.kind, kind_label: s.kind_label, repo: s.repo,
        label: s.label, dry: s.dry, mutating: s.mutating, log_path: s.log_path, commands_text: s.commands_text, target: s.target || "" };
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
      const wasRunning = this.jobPanel.state === "running";
      Object.assign(this.jobPanel, { state: s.state, rc: s.rc, error: s.error, total: s.total,
        done: s.done, errors: s.errors, line_count: s.line_count, elapsed: s.elapsed, skipped: s.skipped || 0,
        results: s.results || [], held: !!s.held, freed: s.freed || null });
      if (s.lines && s.lines.length) {
        this.jobLines.push(...s.lines);
        if (this.jobLines.length > 5000) {  // render buffer cap; the log file always has everything
          this.jobEarly += this.jobLines.length - 5000;
          this.jobLines = this.jobLines.slice(-5000);
        }
        this.jobCursor = s.cursor;
        if (this.jobFollow) this.$nextTick(() => this._jobScrollBottom());
      }
      if (wasRunning && s.state !== "running") this._markJobSeen(s.job_id);  // the user watched it finish
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

    // A one-step job has nothing to count: the bar pulses instead of sitting empty.
    jobIndeterminate() {
      const p = this.jobPanel;
      return !!(p && p.state === "running" && p.total <= 1);
    },

    jobProgressLabel() {
      const p = this.jobPanel;
      if (!p) return "";
      const unit = p.kind === "policy_write" ? "step" : "batch";
      if (p.total > 1) return `${p.done} of ${p.total} ${unit}s · ${this.jobProgress()}%` + (p.errors ? ` · ${p.errors} failed` : "");
      if (p.state === "running") return "Working…";
      return p.errors ? "Failed" : "Done";
    },

    jobResultDiff(r) {
      return { label: r.target === "global" ? "Global" : sourcePath(r.target), before: r.before, after: r.stored || r.after };
    },

    async jobRunHeldDelete() {
      const p = this.jobPanel;
      const then = p && this._jobThen[p.job_id];
      if (!then) return;
      const ok = await this.klConfirm({
        title: "Run the delete without the rule?",
        facts: [["Rules", `${(p.results || []).filter((r) => r.ok).length} of ${(p.results || []).length} added`]],
        callout: "Backups without the rule back this up again on their next snapshot.",
        command: "", confirmLabel: "Run the delete", danger: true,
      });
      if (!ok) return;
      delete this._jobThen[p.job_id];
      await this.startJob(then.kind, then.params, p.label);
    },

    jobStateLine() {
      const p = this.jobPanel;
      if (!p) return "";
      if (p.state === "running") return this.jobPanel.dry ? "Dry run · nothing changes" : "";
      if (p.state === "stopped") return "Stopped" + (p.errors ? ` · ${p.errors} failed` : "");
      if (p.state === "failed") return "Failed";
      if (p.errors) return `Finished · ${p.errors} failed`;
      return "Finished";
    },

    // A finished real delete: offer the maintenance run that frees its space.
    jobFreesLater() {
      const p = this.jobPanel;
      if (!p || p.dry || p.state !== "done" || p.held) return false;
      return p.kind === "remove_files" || p.kind === "snapshot_delete"
        || (p.kind === "policy_write" && p.kind_label === "Excluding and deleting");
    },

    jobFooterNote() {
      const p = this.jobPanel;
      if (!p) return "";
      if (p.kind === "maintenance") return "Kopia reclaims space during this run.";
      if (p.kind === "restore") return p.target ? `Writes to ${p.target}` : "";
      if (p.kind === "policy_write" && p.kind_label !== "Excluding and deleting") return "Takes effect from each backup's next snapshot.";
      return "Space frees after the next full maintenance.";
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
      if (this.jobPanel.kind === "maintenance") {
        const ok = await this.klConfirm({
          title: "Stop maintenance?",
          facts: [["Repo", this.jobPanel.repo], ["Running", this.jobElapsed()]],
          callout: "Kopia stops at once. Work done so far is kept; the rest runs next time.",
          confirmLabel: "Stop maintenance", danger: true,
        });
        if (!ok || !this.jobPanel || this.jobPanel.state !== "running") return;
      }
      await getJson("/api/jobs/stop", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: this.jobPanel.job_id }) }).catch((e) => { this.error = e.message; });
    },

    // ---- Header chip (the way back into a backgrounded job) ----
    // The chip carries jobs worth coming back to: running ones, plus UNSEEN bad endings -
    // a failed delete that vanishes the moment it fails is worse than no chip. Opening the
    // panel acknowledges a finished job; the seen list survives reloads (last 100 kept).
    _seenJobs() {
      try { return JSON.parse(localStorage.getItem("kl-jobseen") || "[]"); } catch (e) { return []; }
    },

    _markJobSeen(jobId) {
      try {
        const seen = this._seenJobs().filter((id) => id !== jobId);
        seen.push(jobId);
        localStorage.setItem("kl-jobseen", JSON.stringify(seen.slice(-100)));
      } catch (e) { /* storage unavailable - the chip just re-shows after a reload */ }
    },

    // Self-scheduling poll: 5 s while something is running or queued, 20 s when the app is idle
    // (the idle case was 24 requests a minute for nothing). A job started elsewhere then shows up
    // within one idle interval.
    armJobChip() {
      if (this._jobChipPoll) { clearTimeout(this._jobChipPoll); clearInterval(this._jobChipPoll); this._jobChipPoll = null; }
      const tick = async () => {
        if (!document.hidden) {
          try { await this._jobChipTick(); } catch (e) { /* the next tick retries */ }
        }
        const busy = (this.runningJobs || []).length > 0 || (this.indexPending || []).length > 0
          || !!this.indexStatus.building;
        this._jobChipPoll = setTimeout(tick, busy ? JOB_CHIP_POLL_ACTIVE : JOB_CHIP_POLL_IDLE);
      };
      tick();
    },

    async _jobChipTick() {
      const r = await getJson("/api/jobs/list").catch(() => null);
      if (!r) return;
      // Terminal-state transitions drive the post-action view refreshes: jobs already
      // terminal at page load are primed as handled so history cannot fire them.
      if (!this._handledJobs) {
        this._handledJobs = new Set((r.jobs || []).filter((j) => j.state !== "running").map((j) => j.job_id));
      }
      for (const j of (r.jobs || [])) {
        if (j.state !== "running" && !this._handledJobs.has(j.job_id)) {
          this._handledJobs.add(j.job_id);
          this._jobCompleted(j);
        }
      }
      const seen = this._seenJobs();
      const now = Date.now() / 1000;
      this.runningJobs = (r.jobs || []).filter((j) => j.state === "running"
        || ((j.state === "failed" || (j.state === "done" && j.errors > 0)) && !seen.includes(j.job_id)
            && now - (j.finished || j.created || 0) < JOB_CHIP_FAILURE_WINDOW));
      const q = await getJson("/api/index/pending").catch(() => null);
      if (q) {
        this.indexPending = (q.pending || []).map((p) => ({ ...p, offset: Date.now() / 1000 - q.now }));
        for (const p of this.indexPending) this._armReindexReload(p.repo, p.requested);  // survives a page reload
      }
      this._reindexWatchTick();
    },

    // ---- Index update queued after deletes ----
    pendingFor(repo) {
      return (this.indexPending || []).find((p) => p.repo === repo) || null;
    },
    pendingText(p) {
      const left = Math.max(0, Math.round(p.due + p.offset - Date.now() / 1000));
      if (!left) return "Index update starting…";
      return `Index update in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    },
    async pendingRunNow(repo) {
      const r = await getJson(`/api/index/pending/run?repo=${encodeURIComponent(repo)}`, { method: "POST" }).catch((e) => { this.error = e.message; return null; });
      if (r && r.started === false && r.reason && r.reason !== "already up to date") {
        this.actionText = `Index update queued: ${r.reason}.`;
        this.klNotify(`Index update queued: ${r.reason}.`, "warn");
      }
      this._jobChipTick();
      if (r && r.started && repo === this.repo) this.pollIndex();
    },
    reloadAfterIndex() {
      this.indexUpdated = null;
      this.activate();
    },

    _jobCompleted(j) {
      // A finished mutating job means this repo's data is changing. The snapshots list reads
      // kopia's own listing - invalidated by the job's on_done - so it can reload right away.
      // Everything index-backed (Search, dashboard, tree) cannot: those rows change only when
      // the reindex publishes a new generation, so they wait for that moment instead.
      if (j.dry || j.repo !== this.repo) return;
      if (j.kind === "policy_write" && this.polData) this.loadPolicies(true);
      // A pin rewrites the snapshot record under a new id; a new snapshot adds a row.
      if ((j.kind === "snapshot_pin" || j.kind === "snapshot_create") && this.active === "snapshots") this.loadSnapshots(true);
      const deleted = j.kind === "remove_files" || j.kind === "snapshot_delete"
        || (j.kind === "policy_write" && j.kind_label === "Excluding and deleting" && !j.held);
      if (deleted && this.active === "snapshots") this.loadSnapshots(true);
      if (deleted) this._armReindexReload(j.repo, j.finished || j.created || Date.now() / 1000);
    },

    // Offer a reload once the index update a delete queued has published, instead of rebuilding
    // tables under the user. `since` is server time; the watch is driven by the header chip's
    // poll (paused in a hidden tab) and reads freshness, which makes no kopia calls.
    _armReindexReload(repo, since) {
      const w = this._reindexWatch[repo];
      if (w) { w.since = Math.min(w.since, since); return; }
      this._reindexWatch[repo] = { since, active: Date.now() / 1000 };
    },

    async _reindexWatchTick() {
      const repos = Object.keys(this._reindexWatch);
      if (!repos.length) return;
      const now = Date.now() / 1000;
      const waiting = [];
      for (const r of repos) {
        if (this.pendingFor(r)) this._reindexWatch[r].active = now; else waiting.push(r);
      }
      if (!waiting.length) return;
      const f = await getJson("/api/index/freshness").catch(() => null);
      if (!f) return;
      this.freshness = f.repos || [];
      for (const r of waiting) {
        const w = this._reindexWatch[r];
        if (!w) continue;
        const row = this.freshness.find((x) => x.repo === r);
        if (row && row.running) { w.active = now; continue; }
        if (row && row.last_index && row.last_index >= w.since) {
          delete this._reindexWatch[r];
          if (this.repo === r && ["dashboard", "search", "tree", "churn"].includes(this.active)) this.indexUpdated = { repo: r };
        } else if (now - w.active > REINDEX_WATCH_IDLE) {
          delete this._reindexWatch[r];
        }
      }
    },
};

// Clean-mode noise: the per-file action lines a big remove emits once per file; the plan
// already knows the file list, so the pane shows what is being processed instead.
// Unseen failures stay in the header chip for a day; older ones live in Logs > Activity.
const JOB_CHIP_FAILURE_WINDOW = 24 * 3600;
// A reload watch with no queued or running update gives up after this long.
const REINDEX_WATCH_IDLE = 600;
// Header poll cadence: fast only while something is actually happening.
const JOB_CHIP_POLL_ACTIVE = 5000;
const JOB_CHIP_POLL_IDLE = 20000;
const JOB_LINE_NOISE = /^will remove file /i;
