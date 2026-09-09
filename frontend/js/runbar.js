// Shared run-bar behaviour. Every destructive action owns a dry-state object
// ({running, done, ok, output, truncated, log, elapsed, entries, snapshots, final_line,
//  mode, seq, error}) and passes it to these methods; the markup is the .kl-runbar /
// .kl-term primitives from app.css. One rule the whole bar holds to: "Dry run" only
// where kopia has a real one (remove-files, snapshot delete); everywhere else the button
// says "Preview" and the result says what it is based on.
const runbarPart = {

    // Fresh dry-state. running/done are EXPLICIT false, never undefined: Alpine's boolean
    // :disabled binding treats undefined as truthy and would render the bar's buttons
    // permanently disabled (found live in the browser).
    klDryState() {
      return { mode: "clean", running: false, done: false, ok: false, output: "", error: "" };
    },

    // Run the dry run / preview for a site. url+body come from the caller so the same
    // code serves remove-files, the chunked plan and snapshot delete.
    async klDry(url, body, st) {
      if (!st || st.running) return;
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
    // strands any in-flight response so it cannot land on the new selection.
    klDryClear(st) {
      if (!st) return;
      st.seq = (st.seq || 0) + 1;
      st.running = false; st.done = false; st.output = ""; st.error = ""; st.doneAt = null;
    },
    klDryLines(st) {
      if (!st || !st.output) return [];
      const lines = st.output.split("\n");
      if (st.mode === "raw") return lines;
      const re = /will remove file|would delete|processing snapshot|replaced manifest|fixed .* snapshots|unchanged|error|unable|warn|fail/i;
      return lines.filter((l) => re.test(l));
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
