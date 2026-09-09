// Shared run-bar behaviour. Every destructive action owns a dry-state object
// ({running, done, ok, lines, cursor, truncated, log, elapsed, entries, snapshots,
//  final_line, mode, seq, error}) and passes it to these methods; the markup is the
// .kl-runbar / .kl-term primitives from app.css. One rule the whole bar holds to: "Dry
// run" only where kopia has a real one (remove-files, snapshot delete); everywhere else
// the button says "Preview" and the result says what it is based on.
//
// Dry runs are JOBS: a big plan's dry run walks every snapshot for minutes, so it runs
// through the shared job runner and streams here live. The full text lands in the job's
// log file (the inline term shows a window), the parsed summary comes back on the job,
// and the header chip carries the run if you navigate away.
const runbarPart = {

    // Fresh dry-state. running/done are EXPLICIT false, never undefined: Alpine's boolean
    // :disabled binding treats undefined as truthy and would render the bar's buttons
    // permanently disabled (found live in the browser).
    klDryState() {
      return { mode: "clean", running: false, done: false, ok: false, output: "", error: "",
               lines: [], cursor: 0, truncated: false };
    },

    // The dry-run routes the run bar uses, mapped to runner jobs. The legacy endpoints
    // stay for any caller not yet moved; an unmapped url falls back to its old POST.
    _dryJobSpec(url, body) {
      if (url === "/api/actions/remove-files-dry-run")
        return { kind: "remove_files", params: { ...body, dry: true, form: "files" } };
      if (url === "/api/actions/remove-plan-dry-run")
        return { kind: "remove_files", params: { ...body, dry: true, form: "plan" } };
      if (url === "/api/actions/snapshot-delete-dry-run")
        return { kind: "snapshot_delete", params: { ...body, dry: true } };
      return null;
    },

    // Run the dry run / preview for a site. url+body come from the caller so the same
    // code serves remove-files, the chunked plan and snapshot delete.
    async klDry(url, body, st) {
      if (!st || st.running) return;
      const spec = this._dryJobSpec(url, body);
      if (!spec) return this._klDrySync(url, body, st);
      const seq = (st.seq = (st.seq || 0) + 1);
      st.running = true; st.error = ""; st.done = false; st.output = "";
      st.lines = []; st.cursor = 0; st.truncated = false;
      let jobId;
      try {
        const r = await getJson("/api/jobs/start", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind: spec.kind, repo: body.repo, params: spec.params }) });
        if (seq !== st.seq) return;  // the selection changed while this was in flight
        jobId = r.job_id;
      } catch (e) {
        if (seq !== st.seq) return;
        st.running = false; st.error = e.message || String(e);
        return;
      }
      this._klDryPoll(jobId, st, seq);
    },

    _klDryPoll(jobId, st, seq) {
      if (st._timer) clearInterval(st._timer);
      st._timer = setInterval(async () => {
        if (seq !== st.seq) { clearInterval(st._timer); st._timer = null; return; }
        const s = await getJson(`/api/jobs/status?job_id=${encodeURIComponent(jobId)}&after=${st.cursor}`).catch(() => null);
        if (!s || seq !== st.seq) return;
        if (s.lines && s.lines.length) {
          st.lines.push(...s.lines);
          if (st.lines.length > 5000) st.lines = st.lines.slice(-5000);
          st.cursor = s.cursor;
        }
        if (s.state === "running") return;
        clearInterval(st._timer); st._timer = null;
        Object.assign(st, {
          running: false, done: true, ok: s.state === "done" && s.rc === 0,
          truncated: (s.skipped || 0) > 0, log: s.log_path || "", elapsed: s.elapsed,
          entries: s.summary ? s.summary.entries : undefined,
          snapshots: s.summary ? s.summary.snapshots : undefined,
          final_line: s.summary ? (s.summary.final_line || "") : "",
          doneAt: Date.now(), mode: st.mode || "clean",
        });
        if (!st.ok) st.error = s.error || "kopia exited non-zero - see the output below";
      }, 1000);
    },

    // The pre-runner path, kept for any dry-run url not mapped above.
    async _klDrySync(url, body, st) {
      const seq = (st.seq = (st.seq || 0) + 1);
      st.running = true; st.error = ""; st.done = false; st.output = "";
      try {
        const r = await getJson(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (seq !== st.seq) return;  // the selection changed while this was in flight
        Object.assign(st, {
          running: false, done: true, ok: !!r.ok, output: r.output || "",
          truncated: !!r.truncated, log: r.log || "", elapsed: r.elapsed,
          entries: r.entries, snapshots: r.snapshots, final_line: r.final_line || "",
          doneAt: Date.now(), mode: st.mode || "clean",
        });
        if (!r.ok) st.error = r.error || "kopia exited non-zero - see the output below";
      } catch (e) {
        if (seq !== st.seq) return;
        st.running = false; st.error = e.message || String(e);
      }
    },

    // The selection changed: the old result no longer describes it. Bumping seq also
    // strands any in-flight response / stops the job poller.
    klDryClear(st) {
      if (!st) return;
      st.seq = (st.seq || 0) + 1;
      if (st._timer) { clearInterval(st._timer); st._timer = null; }
      st.running = false; st.done = false; st.output = ""; st.error = ""; st.doneAt = null;
    },

    klDryLines(st) {
      if (!st || !st.lines || !st.lines.length) return [];
      const re = /will remove file|would delete|processing snapshot|replaced manifest|fixed .* snapshots|unchanged|error|unable|warn|fail/i;
      const lines = st.mode === "raw" ? st.lines : st.lines.filter((l) => re.test(l));
      // The render window keeps a 100k-line plan from building 100k DOM nodes; the log
      // file always has everything.
      return lines.length > 300 ? lines.slice(lines.length - 300) : lines;
    },

    klDryCut(st) {
      if (!st || !st.lines || !st.lines.length) return 0;
      const re = /will remove file|would delete|processing snapshot|replaced manifest|fixed .* snapshots|unchanged|error|unable|warn|fail/i;
      const n = st.mode === "raw" ? st.lines.length : st.lines.filter((l) => re.test(l)).length;
      return Math.max(0, n - 300);
    },

    // The plain line under the result: what kopia says it would do, nothing written.
    klDrySummary(st) {
      if (!st || !st.done) return "";
      if (st.final_line) return st.final_line + " - nothing written.";
      if (st.mode === "clean" && st.snapshots != null) {
        return `Would touch ${st.snapshots} snapshot(s) - nothing written.`;
      }
      return "";
    },
};
