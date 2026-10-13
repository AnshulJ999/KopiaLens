// Indexing panel: the running pass in one place - whole-run progress, the stage list, the
// current source, the live worker log and Stop. Reads whatever /api/index/status carries: the flat
// counters exist today; pct / eta_seconds / phase / sync / seed / queued_repo / log ride the
// progress pass-through and are simply omitted from the UI when absent (never faked).
const INDEX_STAGES = [
  ["index", "Index snapshots"],
  ["dashboard", "Dashboard numbers"],
  ["analysis", "Snapshot analysis"],
  ["churn", "Churn"],
  ["folders", "Folder reclaimable space"],
  ["policies", "Policies"],
  ["policy_prices", "Policy sizes"],
  ["ignore_files", "Ignore files"],
  ["rank_paths", "Biggest files"],
  ["next_snapshots", "Next snapshot times"],
];
// The worker's phase names, in plain words.
const INDEX_PHASES = {
  "tree sync": "Reading folders from kopia", "seeding the tree": "Reading the old cache",
  "rebuilding sources": "Rebuilding sources", maps: "Working out stored sizes",
  sources: "Building file history", writing: "Saving the index",
};
const fmtNum = (n) => Number(n || 0).toLocaleString(NUM_LOCALE);
// Worker lines the Clean view hides: timings, per-target pricing and data dumps.
const INDEX_LINE_NOISE = /^\s*(perf:|policy prices .* (target=|global rollup priced)|.*\{'[a-z_]+': )/;
const INDEX_STAGE_LINE = /^\s*stage: ([a-z_]+)\s*$/;

const indexPanelPart = {

    // The repo the panel follows: the one indexing now (a nightly moves through every repo), else
    // the selected one. The header indicator keeps showing the selected repo's own status.
    indexPanelRepo() {
      const run = (this.freshness || []).find((f) => f.running && f.kind !== "backblaze");
      return run ? String(run.repo) : this.repo;
    },
    ixs() { return (this.showIndex && this.indexPanelStatus) || this.indexStatus || {}; },

    indexProg(s) {
      const st = s || this.ixs();
      return (st && (st.progress || st)) || {};
    },

    indexPct(s) {
      const v = this.indexProg(s).pct;
      return (v === null || v === undefined) ? null : Math.max(0, Math.min(100, Math.round(Number(v))));
    },

    indexEtaText() {
      const p = this.indexProg();
      const secs = p.eta_seconds != null ? p.eta_seconds : p.eta;
      if (secs === null || secs === undefined || !isFinite(Number(secs)) || Number(secs) <= 0) return "";
      return "about " + fmtDur(Number(secs)) + " left";
    },

    indexQueuedText() {
      const q = this.ixs().queued_repo;
      return q ? this.repoLabel(q) : "";
    },

    // The phase inside a stage (the tree sync discovers its folders level by level, so it reports
    // counters and no percentage), plus the seed's folder count when that is what is running.
    indexDetailText() {
      const p = this.indexProg();
      const bits = [];
      if (p.phase) bits.push(INDEX_PHASES[p.phase] || p.phase);
      const sync = p.sync || null;
      if (sync && sync.waiting_since) {
        bits.push(`Waiting for the repository since ${humanTime(sync.waiting_since)}`);
      } else if (sync && sync.pieces) {
        bits.push(`large files: ${fmtNum(sync.pieces_read)} of ${fmtNum(sync.pieces)}`);
      } else if (sync && sync.read != null) {
        const est = p.estimate && !p.estimate.exact && p.estimate.folders;
        bits.push(countOf(sync.read, "folder") + " read" + (est ? ` of about ${fmtNum(est[0])}–${fmtNum(est[1])}` : ""));
        const pace = this.indexPace(sync, est);
        if (pace) bits.push(pace);
      }
      const seed = p.seed || null;
      if (seed && seed.total) bits.push(`seed ${Number(seed.done || 0).toLocaleString(NUM_LOCALE)}/${Number(seed.total).toLocaleString(NUM_LOCALE)}`);
      if (p.current_source) bits.push(sourcePath(p.current_source));
      return bits.join(" · ");
    },

    // Folders per minute over this run, and a time-left range only when there is an estimate
    // the read has not passed yet.
    indexPace(sync, est) {
      const secs = Number(sync.at) - Number(sync.since);
      const done = Number(sync.read) - Number(sync.read0 || 0);
      if (!sync.since || !(secs >= 60) || !(done > 0)) return "";
      const perMin = done / (secs / 60);
      let text = `${fmtNum(Math.round(perMin))} folders a minute`;
      if (est && Number(sync.read) < est[1]) {
        const lo = Math.max(0, est[0] - sync.read) / perMin * 60;
        const hi = (est[1] - sync.read) / perMin * 60;
        text += lo < 60 ? `, under ${fmtDur(Math.max(hi, 60))} left` : `, about ${fmtDur(lo)}–${fmtDur(hi)} left`;
      }
      return text;
    },

    // When kopia last answered during the folder read; a long silence is shown as one.
    indexActivity() {
      const sync = this.indexProg().sync;
      if (!sync || !sync.at) return null;
      const secs = Math.max(0, Date.now() / 1000 - Number(sync.at));
      return { text: secs < 10 ? "Last read just now" : `Last read ${fmtDur(secs)} ago`, slow: secs > 300 };
    },

    indexPanelCounts(st) {
      const s = st || this.ixs();
      const pct = this.indexPct(s);
      const counts = s.offline ? `${s.indexed_snapshots || 0} of ${countOf(s.snapshots, "snapshot")}`
        : s.building ? `${Number(s.indexed_snapshots || 0).toLocaleString(NUM_LOCALE)} of ${Number(s.snapshots || 0).toLocaleString(NUM_LOCALE)} snapshots`
        : (s.never_indexed ? "Not indexed yet" : `${s.indexed_snapshots || 0} of ${countOf(s.snapshots, "snapshot")} indexed`);
      return counts + (pct == null ? "" : ` · ${pct}%`);
    },

    indexStageRows() {
      const cur = this.indexProg().stage || "";
      const idx = INDEX_STAGES.findIndex(([k]) => k === cur);
      return INDEX_STAGES.map(([key, label], i) => ({
        key, label,
        state: idx < 0 ? "" : (i < idx ? "done" : (i === idx ? "now" : "")),
      }));
    },

    // Same shapes the job panel uses: the flat stage list, then the panel.
    indexLine() {
      const s = this.indexStatus;
      if (s.via_server) return "Browse-only";
      if (s.offline) return `Indexing (${s.stage || "working"}) ${s.indexed_snapshots || 0}/${s.snapshots || "?"}`;
      if (s.building) {
        const pct = this.indexPct(s);
        return `Indexing ${s.indexed_snapshots || 0}/${s.snapshots || "?"}` + (pct == null ? "" : ` · ${pct}%`);
      }
      if (s.never_indexed) return "Not indexed yet";
      if (s.snapshots === undefined) return "Index: unknown";
      return `Index ${s.indexed_snapshots}/${s.snapshots}`;
    },

    indexTooltip() {
      const s = this.indexStatus || {};
      if (s.via_server) return "Reached through a kopia server: snapshots, folders, restore and policies work; the index can't be built.";
      if (!s.building) return s.never_indexed ? "Build the first index for this repo" : "Indexed. Click for details.";
      const bits = [this.indexPanelCounts(s)];
      const detail = this.indexDetailText();
      if (detail) bits.push(detail);
      if (s.stage) bits.push(s.stage);
      const eta = this.indexEtaText();
      if (eta) bits.push(eta);
      return bits.join(" · ");
    },

    // ---- the panel ----
    openIndexPanel() {
      this.showIndex = true;
      this.indexLogLines = [];
      this.indexLogCursor = 0;   // 0 = from the current run's first line (server side)
      this.indexLogNote = "";
      this.indexLogRepo = "";
      this.indexPanelStatus = null;
      if (this._indexLogPoll) clearInterval(this._indexLogPoll);
      this._indexLogPoll = setInterval(() => { if (!document.hidden && this.showIndex) this._indexLogTick(); }, 2000);
      this._indexLogTick();
    },

    closeIndexPanel() {
      if (this._indexLogPoll) { clearInterval(this._indexLogPoll); this._indexLogPoll = null; }
      this.showIndex = false;
    },

    indexLogSetMode(m) {
      this.indexLogMode = m;
      localStorage.setItem("kl-indexmode", m);
    },

    async _indexLogTick() {
      const repo = this.indexPanelRepo();
      if (repo !== this.indexLogRepo) {   // a nightly moved on to the next repo: start its log fresh
        this.indexLogRepo = repo;
        this.indexLogLines = [];
        this.indexLogCursor = 0;
        this.indexPanelStatus = null;
      }
      const seq = ++this._indexLogSeq;
      try {
        const r = await getJson(`/api/index/log?repo=${encodeURIComponent(repo)}&after=${this.indexLogCursor}`);
        if (seq !== this._indexLogSeq || !this.showIndex) return;
        this.indexLogNote = (r && r.note) || "";
        if (r && r.lines && r.lines.length) {
          this.indexLogLines.push(...r.lines);
          if (this.indexLogLines.length > 2000) this.indexLogLines = this.indexLogLines.slice(-2000);
          this.indexLogCursor = r.cursor || this.indexLogCursor;
        }
      } catch (e) {
        if (seq === this._indexLogSeq && this.showIndex) {
          this.indexLogNote = "The live worker log is not available from this build.";
        }
      }
      const s = await getJson(`/api/index/status?repo=${encodeURIComponent(repo)}`).catch(() => null);
      if (seq !== this._indexLogSeq || !this.showIndex) return;
      if (s) {
        this.indexPanelStatus = s;
        if (repo === this.repo) this.indexStatus = s;
      }
    },

    indexLogView() {
      const clean = this.indexLogMode === "clean";
      const kept = [];
      for (const ln of (this.indexLogLines || [])) {
        if (!clean) { kept.push(ln); continue; }
        const body = ln.slice(9);
        if (JOB_LINE_NOISE.test(body) || INDEX_LINE_NOISE.test(body)) continue;
        const st = body.match(INDEX_STAGE_LINE);
        const lbl = st && (INDEX_STAGES.find(([k]) => k === st[1]) || [])[1];
        kept.push(lbl ? ln.slice(0, 9) + " " + lbl : ln);
      }
      return kept.slice(-400);
    },
    indexLogClass(ln) { return klLogLineWarn(ln); },

    async indexStop() {
      const repo = this.indexLogRepo || this.repo;
      if (!this.ixs().building) return;
      const stage = this.ixs().stage;
      const reading = !stage || stage === "index";
      const ok = await this.klConfirm({
        title: `Stop indexing ${this.repoLabel(repo)}?`,
        callout: reading ? "Progress is saved. The next run continues from here." : "The index is saved. The next run repeats the remaining steps.",
        confirmLabel: "Stop indexing", danger: true,
      });
      if (!ok) return;
      try {
        const r = await getJson(`/api/index/stop?repo=${encodeURIComponent(repo)}`, { method: "POST" });
        if (r && r.stopped === false && r.reason) this.klNotify(`Could not stop: ${r.reason}`, "warn");
        else this.klNotify(reading ? "Stopping. Saving what's read so far…" : "Indexing stopped.");
      } catch (e) {
        this.klNotify("Stopping an index run is not available from this build.", "warn");
      }
      const s = await getJson(`/api/index/status?repo=${encodeURIComponent(repo)}`).catch(() => null);
      if (s) {
        this.indexPanelStatus = s;
        if (repo === this.repo) this.indexStatus = s;
      }
    },
};
