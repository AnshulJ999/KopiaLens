// New snapshot of any folder (with kopia's size estimate), deleting a whole source with its
// policy, and Verify backups. Estimates and verifies run as jobs; the dialog follows the job.
const sourceOpsPart = {
  // ---- New snapshot ----
  openNewSnap(path) {
    const keep = this.newSnap && !path ? this.newSnap.path : "";
    this.newSnap = { path: path || keep, check: null, checking: false, error: "", est: null, description: "", busy: false };
    if (this.newSnap.path) this.newSnapCheck();
  },
  closeNewSnap() { this.newSnapStopEstimate(); this.newSnap = null; },
  newSnapPaths() { return (this.sources || []).map((s) => sourcePath(s)); },
  async newSnapCheck() {
    const g = this.newSnap;
    if (!g) return;
    const seq = ++this._newSnapSeq;
    g.check = null; g.error = ""; g.est = null;
    if (!g.path.trim()) return;
    g.checking = true;
    try {
      const d = await getJson(`/api/newsnap/check?repo=${encodeURIComponent(this.repo)}&path=${encodeURIComponent(g.path)}`);
      if (seq !== this._newSnapSeq || !this.newSnap) return;
      if (d.ok) g.check = d; else g.error = d.error;
    } catch (e) { if (seq === this._newSnapSeq && this.newSnap) g.error = e.message; }
    if (seq === this._newSnapSeq && this.newSnap) g.checking = false;
  },
  newSnapPolicyRows(p) {
    if (!p) return [];
    const every = (n) => [[86400, "day"], [3600, "hour"], [60, "minute"], [1, "second"]].map(([u, w]) => n % u === 0 ? countOf(n / u, w) : "").find(Boolean);
    const when = p.manual ? "Manual only" : [p.interval ? "every " + every(p.interval) : "", ...(p.times || []).map((t) => "at " + t), ...(p.cron || [])].filter(Boolean).join(", ") || "Not scheduled";
    const keep = Object.entries(p.retention || {}).map(([k, v]) => `${v} ${k}`).join(", ") || "Keeps everything";
    return [["Policy", p.own ? "This folder's own policy" : (p.from === "global" ? "Global policy" : "Inherited from " + sourcePath(p.from))],
      ["Schedule", when], ["Keeps", keep], ["Exclusion rules", String(p.rules)]];
  },
  async newSnapEstimate() {
    const g = this.newSnap;
    if (!g || !g.check) return;
    g.est = { job: null, lines: [], cursor: 0, state: "starting", summary: [] };
    try {
      const r = await getJson("/api/jobs/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "snapshot_estimate", repo: this.repo, params: { path: g.check.path }, label: g.check.path }) });
      if (!this.newSnap || this.newSnap.est !== g.est) return;
      g.est.job = r.job_id; g.est.state = "running";
      this._newSnapPoll();
    } catch (e) { g.est.state = "failed"; g.est.lines = [e.message]; }
  },
  async _newSnapPoll() {
    const est = this.newSnap && this.newSnap.est;
    if (!est || !est.job) return;
    try {
      const s = await getJson(`/api/jobs/status?job_id=${encodeURIComponent(est.job)}&after=${est.cursor}`);
      if (!this.newSnap || this.newSnap.est !== est) return;
      est.lines = [...est.lines, ...(s.lines || []).map((l) => l.replace(/^\d\d:\d\d:\d\d /, ""))].slice(-400);
      est.cursor = s.cursor || est.cursor;
      est.summary = est.lines.filter((l) => /^(Snapshot (includes|excludes)|Encountered|Estimated upload time)/.test(l));
      if (s.state === "running") { setTimeout(() => this._newSnapPoll(), 800); return; }
      est.state = s.state === "done" && !s.error ? "done" : (s.state === "stopped" ? "stopped" : "failed");
      if (s.error) est.lines = [...est.lines, s.error];
    } catch (e) { est.state = "failed"; }
  },
  newSnapStopEstimate() {
    const est = this.newSnap && this.newSnap.est;
    if (est && est.job && est.state === "running")
      getJson("/api/jobs/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: est.job }) }).catch(() => {});
  },
  async newSnapBrowse() {
    const g = this.newSnap;
    const path = await this.pickFolder(g && g.path || null);
    if (path && this.newSnap) { this.newSnap.path = path; this.newSnapCheck(); }
  },
  newSnapEditPolicy() {
    const g = this.newSnap;
    if (!g || !g.check) return;
    const target = g.check.source;
    this.newSnapBack = g.path;
    this.newSnap = null;
    this.active = "policies";
    this.loadPolicies().then(() => {
      const known = [...(this.polData.sources || []).map((s) => s.source), ...(this.polData.others || []).map((o) => o.target)];
      if (!known.includes(target) && !this.polNewTargets.includes(target)) this.polNewTargets = [...this.polNewTargets, target];
      this.polTryLeave(target);
    });
  },
  async newSnapStart() {
    const g = this.newSnap;
    if (!g || !g.check || g.busy) return;
    g.busy = true;
    const id = await this.startJob("snapshot_create", { source: g.check.source, description: g.description || "" }, g.check.path);
    g.busy = false;
    if (id) this.newSnap = null;
  },

  // ---- Delete a whole source ----
  async openDeleteSource(source) {
    this.srcDel = { source, info: null, policy: false, error: "", busy: false };
    try {
      const d = await getJson(`/api/newsnap/source?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(source)}`);
      if (this.srcDel && this.srcDel.source === source) this.srcDel.info = d;
    } catch (e) { if (this.srcDel) this.srcDel.error = e.message; }
  },
  srcDelRow() {
    const el = document.querySelector("#dashboardTable");
    const t = el && el._tabulator;
    return (t && this.srcDel && t.getData().find((r) => r.source === this.srcDel.source)) || null;
  },
  srcDelDry() {
    const g = this.srcDel;
    if (!g || !g.info) return;
    this.startJob("source_delete", { source: g.source, delete_policy: g.policy && g.info.has_policy, dry: true }, sourcePath(g.source));
  },
  async srcDelRun() {
    const g = this.srcDel;
    if (!g || !g.info || g.busy) return;
    const row = this.srcDelRow();
    const n = g.info.snapshots;
    const facts = [["Source", g.source], ["Snapshots", String(n)]];
    if (g.info.pinned) facts.push(["Pinned", String(g.info.pinned) + " (deleted too)"]);
    if (row && row.freeable != null) facts.push([term("reclaimable"), fmtBytes(row.freeable)]);
    facts.push(["Policy", g.info.has_policy ? (g.policy ? "Deleted" : "Kept") : "None of its own"]);
    const ok = await this.klConfirm({ title: `Delete every snapshot of ${sourcePath(g.source)}?`, facts,
      callout: "Space is freed after the next full maintenance.", confirmLabel: `Delete ${countOf(n, "snapshot")}`, danger: true });
    if (!ok) return;
    g.busy = true;
    const id = await this.startJob("source_delete", { source: g.source, delete_policy: g.policy && g.info.has_policy }, sourcePath(g.source));
    g.busy = false;
    if (id) this.srcDel = null;
  },

  // ---- Show command: the kopia command behind a screen ----
  async showCli(what, target) {
    try {
      const d = await getJson(`/api/cli/equivalent?repo=${encodeURIComponent(this.repo)}&what=${what}&target=${encodeURIComponent(target || "")}`);
      this.cliBox = { commands: d.commands || [] };
    } catch (e) { this.klNotify(e.message, "error"); }
  },

  // ---- Verify backups ----
  openVerify() {
    this.verifyDlg = { scope: "all", source: "", percent: 0 };
  },
  // Rough size signal for the "this can take a long time" line: the index's file versions.
  verifyBig() {
    const p = (this.statsData && this.statsData.parsed) || {};
    const idx = p.index || {};
    const versions = Number(idx.file_versions || idx.versions || 0);
    return versions > 1_000_000 || (this.verifyDlg && this.verifyDlg.percent > 0 && Number((p.content || {}).total_bytes || 0) > 200e9);
  },
  async verifyStart() {
    const g = this.verifyDlg;
    if (!g) return;
    const sources = g.scope === "one" && g.source ? [g.source] : [];
    const facts = [["Repository", this.repoName(this.repo)], ["Checks", sources.length ? sourcePath(sources[0]) : "Every snapshot"],
      ["Files read back", g.percent ? g.percent + "% of files, downloaded and checked" : "None (folders and file records only)"]];
    const ok = await this.klConfirm({ title: "Verify backups?", facts, callout: this.verifyBig() ? "On a repository this size, this can take hours." : "",
      note: "Reads only. Nothing in the repository changes.", confirmLabel: "Verify", danger: false });
    if (!ok) return;
    const id = await this.startJob("snapshot_verify", { sources, percent: g.percent }, sources.length ? sourcePath(sources[0]) : "all sources");
    if (id) this.verifyDlg = null;
  },
};
