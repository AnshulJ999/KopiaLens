// Tasks tab: every task from KopiaLens's jobs and from its kopia servers (scheduled backups,
// maintenance), across repositories, with a task panel holding the details and the log.
const TASK_STATUS = {
  running: ["Running", "text-warn"], success: ["Succeeded", "text-ok"], warning: ["Finished with errors", "text-warn"],
  failed: ["Failed", "text-danger"], canceled: ["Canceled", "text-muted"], canceling: ["Canceling", "text-warn"],
};

const tasksPart = {
  async loadTasks() {
    const seq = ++this._tasksSeq;
    try {
      const d = await getJson("/api/tasks");
      if (seq !== this._tasksSeq) return;
      this.taskRows = d.tasks || [];
      this.taskHistoryDays = d.history_days;
      this.taskRoutineSeconds = d.routine_seconds || 1;
      this.renderTasks();
      if (this.taskSel) {
        const fresh = this.taskRows.find((t) => t.id === this.taskSel.id);
        if (fresh) this.taskSel = { ...this.taskSel, ...fresh };
        // The log refresh stops when the tab is left; pick it up again.
        if (this.taskSel.status === "running" && !this._taskPoll) this.pollTask();
      }
    } catch (e) { this.error = e.message; }
    this.armTasksPoll();
  },
  // The Refresh button: a spinning icon while the list reloads, shown long enough to be seen.
  async refreshTasks() {
    if (this.tasksRefreshing) return;
    this.tasksRefreshing = true;
    try {
      await Promise.all([this.loadTasks(), new Promise((r) => setTimeout(r, 400))]);
    } finally {
      this.tasksRefreshing = false;
    }
  },
  armTasksPoll() {
    if (this._tasksPoll) clearInterval(this._tasksPoll);
    this._tasksPoll = setInterval(() => {
      if (this.active !== "tasks") { clearInterval(this._tasksPoll); this._tasksPoll = null; return; }
      if (!document.hidden) this.loadTasks();
    }, 5000);
  },
  openTasksFor(repo) {
    this.engineDlg = null;
    this.taskFilter.repo = repo || "all";
    this.active = "tasks";
    this.activate();
  },
  taskKinds() { return [...new Set(this.taskRows.map((t) => t.kind).filter(Boolean))].sort(); },
  tasksFiltered() {
    const f = this.taskFilter;
    const text = (f.text || "").toLowerCase();
    return this.taskRows.filter((t) =>
      (f.repo === "all" || t.repo === f.repo)
      && (f.status === "all" || (f.status === "failed" ? t.status === "failed" || t.status === "warning" : t.status === "running"))
      && (f.kind === "all" || t.kind === f.kind)
      && (!this.taskHideRoutine || !t.routine || (this.taskSel && this.taskSel.id === t.id))
      && (!text || (t.description + " " + (t.error || "")).toLowerCase().includes(text)));
  },
  taskWhat(t) { return t.source ? this.shortSource(t.source) : t.description; },
  taskStatusText(t) { return (TASK_STATUS[t.status] || [t.status || ""])[0]; },
  taskStatusClass(t) { return (TASK_STATUS[t.status] || ["", ""])[1]; },
  taskTook(t) {
    if (!t || !t.start) return "";
    return fmtDur((t.end || Date.now() / 1000) - t.start);
  },
  // A counter's number from kopia's table (0 when it isn't there).
  taskCount(t, name) { return Number((((t && t.counters) || {})[name] || {}).value || 0); },
  renderTasks() {
    const rows = this.tasksFiltered().map((t) => ({ ...t,
      _took: t.start ? (t.end || Date.now() / 1000) - t.start : null,
      _files: this.taskCount(t, "Hashed Files") + this.taskCount(t, "Cached Files"),
      _hashed: this.taskCount(t, "Hashed Bytes"), _cached: this.taskCount(t, "Cached Bytes"),
      _uploaded: this.taskCount(t, "Uploaded Bytes"), _errors: this.taskCount(t, "Errors") }));
    const num = (v) => (v ? Number(v).toLocaleString(NUM_LOCALE) : "");
    const bytes = (v) => (v ? fmtBytes(v) : "");
    const el = document.querySelector("#tasksTable");
    const sig = JSON.stringify(rows.map((t) => t.id));
    if (el && el._tabulator && sig === this._tasksSig) {
      el._tabulator.updateData(rows).catch(() => {});   // same tasks: refresh their cells, keep scroll and selection
      return;
    }
    this._tasksSig = sig;
    const sel = this.taskSel && this.taskSel.id;
    const table = setTable("#tasksTable", rows, [
      { title: "Started", field: "start", width: 165, sorter: "number", formatter: (c) => timeCell(c.getValue(), c) },
      { title: "Status", field: "status", width: 150, formatter: (c) => {
          const t = c.getRow().getData();
          const pct = t.status === "running" && t.total ? ` ${Math.round((t.done / t.total) * 100)}%` : "";
          return `<span class="font-medium ${this.taskStatusClass(t)}">${this.taskStatusText(t)}${pct}</span>`;
        } },
      { title: "Kind", field: "kind", width: 130 },
      { title: "What", field: "description", widthGrow: 4, minWidth: 220, formatter: (c) => {
          const t = c.getRow().getData();
          const span = document.createElement("span");
          span.textContent = this.taskWhat(t);
          span.title = t.error || t.description;
          return span;
        } },
      { title: "Repository", field: "repo", width: 120, formatter: (c) => this.repoLabel(c.getValue()) },
      { title: "Took", field: "_took", width: 95, sorter: "number", hozAlign: "right", formatter: (c) => this.taskTook(c.getRow().getData()) },
      { title: "Files", field: "_files", width: 85, sorter: "number", hozAlign: "right", headerTooltip: "Files read (hashed) plus files kopia knew were unchanged (cached)", formatter: (c) => num(c.getValue()) },
      { title: "Hashed", field: "_hashed", width: 95, sorter: "number", hozAlign: "right", headerTooltip: "Data read and checked because it was new or changed", formatter: (c) => bytes(c.getValue()) },
      { title: "Cached", field: "_cached", width: 95, sorter: "number", hozAlign: "right", headerTooltip: "Data skipped because it hadn't changed", formatter: (c) => bytes(c.getValue()) },
      { title: "Uploaded", field: "_uploaded", width: 95, sorter: "number", hozAlign: "right", headerTooltip: "New data written to the repository", formatter: (c) => bytes(c.getValue()) },
      { title: "Errors", field: "_errors", width: 70, sorter: "number", hozAlign: "right", formatter: (c) => (c.getValue() ? `<span class="text-danger">${num(c.getValue())}</span>` : "") },
    ], {
      initialSort: [{ column: "start", dir: "desc" }],
      placeholder: this.taskRows.length ? "No tasks match these filters" : "Backups, maintenance and actions show here when they run",
      onSelect: (data) => { if (data && data[0]) this.openTask(data[0]); },
    });
    if (table && sel) table.on("tableBuilt", () => { const r = table.getRow(sel); if (r) r.select(); });
  },
  async openTask(t) {
    if (this.taskSel && this.taskSel.id === t.id) return;
    this.taskSel = { ...t };
    this.taskLines = [];
    this.taskCursor = 0;
    this.taskLogMode = "own";
    this.taskLogSkipped = false;
    await this.pollTask();
  },
  closeTask() {
    this.taskSel = null;
    if (this._taskPoll) { clearTimeout(this._taskPoll); this._taskPoll = null; }
    const el = document.querySelector("#tasksTable");
    if (el && el._tabulator) el._tabulator.deselectRow();
  },
  async pollTask() {
    if (this._taskPoll) { clearTimeout(this._taskPoll); this._taskPoll = null; }
    const cur = this.taskSel;
    if (!cur) return;
    try {
      const mode = this.taskLogMode;
      const d = await getJson(`/api/tasks/detail?id=${encodeURIComponent(cur.id)}&after=${this.taskCursor}${mode === "kopia" ? "&log=kopia" : ""}`);
      if (!this.taskSel || this.taskSel.id !== cur.id || mode !== this.taskLogMode) return;
      this.taskLines = this.taskLines.concat(d.lines || []).slice(-5000);
      this.taskCursor = d.cursor || this.taskCursor;
      if (d.skipped_earlier) this.taskLogSkipped = true;
      const { lines, cursor, skipped_earlier, ...rest } = d;
      this.taskSel = { ...this.taskSel, ...rest };
      this.$nextTick(() => { const term = this.$refs.taskTerm; if (term) term.scrollTop = term.scrollHeight; });
    } catch (e) { /* the task may have rolled out of kopia's memory; keep what we have */ }
    if (this.taskSel && this.taskSel.status === "running" && this.active === "tasks") {
      this._taskPoll = setTimeout(() => this.pollTask(), 1500);
    }
  },
  taskSummary(t) {
    const took = this.taskTook(t);
    if (t.status === "running") return `Running for ${took}` + (t.progress ? `: ${t.progress}` : "");
    if (t.status === "success") return `Succeeded after ${took}`;
    if (t.status === "warning") return `Finished after ${took} with errors`;
    if (t.status === "canceled") return "Canceled";
    if (t.status === "failed") return t.error || "Failed";
    return this.taskStatusText(t);
  },
  // The summary above the log (the line above it has the result and times): where, what, and
  // kopia's key counts (all of them are in the Counter table).
  taskFacts(t) {
    const facts = [["Repository", this.repoName(t.repo)], ["What", this.taskWhat(t)]];
    for (const c of this.taskCounters(t).filter((x) => !x.zero && /file|byte|size|error/i.test(x.name)).slice(0, 3)) {
      facts.push([c.name, c.value, c.level === "error" ? "text-danger" : c.level === "warning" ? "text-warn" : ""]);
    }
    return facts;
  },
  // Task details as a full-size popup or a small panel under the list (remembered).
  setTaskView(v) {
    this.taskView = v;
    this.klSet({ task_view: v });
  },
  // Every counter kopia reports, like KopiaUI's table: errors and warnings first (coloured when
  // not zero), zeros greyed.
  taskCounters(t) {
    const order = { error: 0, warning: 1, notice: 2 };
    return Object.entries((t && t.counters) || {})
      .filter(([, c]) => c)
      .map(([name, c]) => ({ name, level: c.value ? (c.level || "") : "", zero: !c.value,
        value: c.units === "bytes" ? fmtBytes(c.value || 0) : Number(c.value || 0).toLocaleString(NUM_LOCALE) }))
      .sort((a, b) => ((order[a.level] ?? 3) - (order[b.level] ?? 3)) || (a.zero - b.zero) || a.name.localeCompare(b.name));
  },
  // A log line "time level message<TAB>details": the details show muted, like KopiaUI.
  logMsg(ln) { const i = String(ln).indexOf("\t"); return i < 0 ? ln : ln.slice(0, i); },
  logDetail(ln) { const i = String(ln).indexOf("\t"); return i < 0 ? "" : " " + ln.slice(i + 1); },
  // KopiaLens's own command-line jobs: their output, or kopia's full log for the command.
  setTaskLog(mode) {
    if (!this.taskSel || this.taskLogMode === mode) return;
    this.taskLogMode = mode;
    this.taskLines = [];
    this.taskCursor = 0;
    this.taskLogSkipped = false;
    this.pollTask();
  },
  async openTaskById(id) {
    this.active = "tasks";
    await this.loadTasks();
    const t = this.taskRows.find((x) => x.id === id);
    if (t) await this.openTask(t);
  },
  async stopTask() {
    const t = this.taskSel;
    if (!t) return;
    const ok = await this.klConfirm({
      title: `Stop this ${t.kind ? t.kind.toLowerCase() : "task"}?`,
      facts: [["What", this.taskWhat(t)], ["Repository", this.repoName(t.repo)]],
      confirmLabel: "Stop", danger: true,
    });
    if (!ok) return;
    try {
      await getJson("/api/tasks/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: t.id }) });
      this.klNotify("Stopping…");
      this.pollTask();
    } catch (e) { this.klNotify(e.message, "error"); }
  },
};
