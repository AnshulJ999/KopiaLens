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
      this.renderTasks();
      if (this.taskSel) {
        const fresh = this.taskRows.find((t) => t.id === this.taskSel.id);
        if (fresh) this.taskSel = { ...this.taskSel, ...fresh };
      }
    } catch (e) { this.error = e.message; }
    this.armTasksPoll();
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
      && (f.status === "all" || (f.status === "failed" ? t.status === "failed" : t.status === "running"))
      && (f.kind === "all" || t.kind === f.kind)
      && (!text || (t.description + " " + (t.error || "")).toLowerCase().includes(text)));
  },
  taskWhat(t) { return t.source ? sourcePath(t.source) : t.description; },
  taskStatusText(t) { return (TASK_STATUS[t.status] || [t.status || ""])[0]; },
  taskStatusClass(t) { return (TASK_STATUS[t.status] || ["", ""])[1]; },
  taskTook(t) {
    if (!t.start) return "";
    return fmtDur((t.end || Date.now() / 1000) - t.start);
  },
  renderTasks() {
    const rows = this.tasksFiltered();
    const el = document.querySelector("#tasksTable");
    const sig = JSON.stringify(rows.map((t) => [t.id, t.status, t.end, t.progress]));
    if (el && el._tabulator && sig === this._tasksSig) return;
    this._tasksSig = sig;
    const sel = this.taskSel && this.taskSel.id;
    const table = setTable("#tasksTable", rows, [
      { title: "Started", field: "start", width: 165, sorter: "number", formatter: (c) => timeCell(c.getValue()) },
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
      { title: "Took", field: "end", width: 95, headerSort: false, formatter: (c) => this.taskTook(c.getRow().getData()) },
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
      const d = await getJson(`/api/tasks/detail?id=${encodeURIComponent(cur.id)}&after=${this.taskCursor}`);
      if (!this.taskSel || this.taskSel.id !== cur.id) return;
      this.taskLines = this.taskLines.concat(d.lines || []).slice(-3000);
      this.taskCursor = d.cursor || this.taskCursor;
      const { lines, cursor, ...rest } = d;
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
  taskCounters(t) {
    const order = { error: 0, warning: 1, notice: 2 };
    return Object.entries(t.counters || {})
      .filter(([, c]) => c && c.value)
      .sort(([a, ca], [b, cb]) => ((order[ca.level] ?? 3) - (order[cb.level] ?? 3)) || a.localeCompare(b))
      .map(([name, c]) => ({ name, level: c.level || "", value: c.units === "bytes" ? fmtBytes(c.value) : Number(c.value).toLocaleString(NUM_LOCALE) }));
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
