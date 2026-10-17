// Backups run by KopiaLens (kopia server per repository): status, "Who runs backups", pause,
// Snapshot now / Cancel through the engine, and alerts in the page when there is no tray.
const ENGINE_APPS = { kopiaui: "KopiaUI", blinkdisk: "BlinkDisk", kopia: "A kopia server", kopialens: "KopiaLens", none: "Nothing" };

const enginePart = {
  armEngine() {
    if (this._engineTimer) clearInterval(this._engineTimer);
    this.loadEngine();
    this._engineTimer = setInterval(() => { if (!document.hidden) this.loadEngine(); }, 10000);
  },
  async loadEngine() {
    try {
      const d = await getJson("/api/engine/status");
      this.engine = d;
      this.tasksRunning = (d.running_tasks || 0) + (this.runningJobs || []).filter((j) => j.state === "running").length;
      if ((d.approvals_waiting || 0) !== this.apWaiting) this.apLoad();   // the notice shows the newest
      this.apWaiting = d.approvals_waiting || 0;
      if (d.alert_seq > this._alertSeen) {
        if (this._alertSeen >= 0 && !this.isDesktopWindow()) {
          const a = await getJson(`/api/engine/alerts?after=${this._alertSeen}`);
          for (const al of a.alerts || []) {
            if (al.level !== "info") this.klNotify(al.title + (al.body ? ". " + al.body : ""), al.level === "error" ? "error" : "warn");
          }
        }
        this._alertSeen = d.alert_seq;
      } else if (this._alertSeen < 0) this._alertSeen = d.alert_seq || 0;
      if (this.active === "dashboard") this.loadEngineSources();
      // A backup or maintenance run finished: kopia's snapshot list changed under these screens.
      const e = this.engineFor();
      const seq = e ? `${e.repo}:${e.done_seq || 0}` : "";
      if (this._doneSeq && seq && seq !== this._doneSeq && this._doneSeq.split(":")[0] === e.repo) {
        if (this.active === "dashboard") this.loadDashboard();
        else if (this.active === "snapshots") this.loadSnapshots(true);
      }
      this._doneSeq = seq;
    } catch (e) { /* the status line is optional */ }
    if (!this.isDemo()) this.loadMounts();
  },
  engineFor(repo) { return ((this.engine && this.engine.repos) || []).find((r) => r.repo === (repo || this.repo)) || null; },
  engineRuns(repo) { const e = this.engineFor(repo); return !!(e && e.state === "running"); },
  engineAppName(id) { return ENGINE_APPS[id] || id; },
  // The Dashboard strip's one line about this repository's backups.
  backupsLine(repo) {
    const e = this.engineFor(repo);
    if (!e) return "";
    if (e.choice === "kopialens") {
      if (e.state === "running") return "Backups: KopiaLens" + (e.next ? " · next " + humanTime(e.next) : "");
      if (e.state === "starting") return "Backups: KopiaLens · starting…";
      if (e.state === "paused") return "Backups: paused";
      if (e.state === "blocked" && e.until_restart) return "Backups: KopiaLens takes over when KopiaUI restarts";
      if (e.state === "blocked") return `Backups: ${this.engineAppName(e.engine)} is running them`;
      if (e.state === "failed") return "Backups: stopped by an error";
      if (e.state === "write_mode") return `Backups: off in ${this.wmLabel(this.wmMode(repo))} mode`;
      return "Backups: KopiaLens";
    }
    if (e.engine === "none") return "Backups: nothing runs them";
    return "Backups: " + this.engineAppName(e.engine);
  },
  backupsClass(repo) {
    const e = this.engineFor(repo);
    if (!e) return "";
    if (e.choice === "kopialens" && ["blocked", "failed"].includes(e.state) && !e.until_restart) return "border-danger text-danger";
    if (e.until_restart || e.state === "paused" || e.state === "write_mode" || (e.choice !== "kopialens" && e.engine === "none")) return "border-warn-bd text-warn";
    return "";
  },
  // Policy editor note: who makes the schedule actually run.
  schedulesNote() {
    const e = this.engineFor();
    if (!e) return "Schedules run while KopiaUI or a kopia server is running.";
    if (e.choice === "kopialens") return e.state === "paused" ? "KopiaLens runs these schedules. Backups are paused." : "KopiaLens runs these schedules while it's open.";
    if (e.engine === "none") return "Nothing runs these schedules for this repository right now.";
    return this.engineAppName(e.engine) + " runs these schedules for this repository.";
  },
  enginePauseText(p) {
    if (!p || !p.on) return "";
    return p.until ? "until " + humanTime(p.until) : "until you resume";
  },
  pauseChoices() { return PAUSE_CHOICES; },
  // "Custom…" in a pause menu: pick an end time; resolves hours, or null when cancelled.
  async pauseCustomHours(title) {
    const pad = (n) => String(n).padStart(2, "0");
    const d = new Date(Date.now() + 2 * 86400000);
    const local = (x) => `${x.getFullYear()}-${pad(x.getMonth() + 1)}-${pad(x.getDate())}T${pad(x.getHours())}:${pad(x.getMinutes())}`;
    const r = await this.klConfirm({ title, input: { label: "Pause until", type: "datetime-local", value: local(d), min: local(new Date()) },
      confirmLabel: "Pause", danger: false });
    if (!r || !r.value) return null;
    const hours = (new Date(r.value).getTime() - Date.now()) / 3600000;
    if (!(hours > 0)) { this.klNotify("Pick a time in the future", "warn"); return null; }
    return Math.min(hours, 24 * 366);
  },

  // ---- Who runs backups ----
  async openEngineDlg(repo) {
    repo = repo || this.repo;
    this.engineDlg = { repo, data: null, choice: "", how: "share", password: "", busy: false, error: "", loading: true };
    try {
      const d = await getJson(`/api/engine/repo?repo=${encodeURIComponent(repo)}`);
      if (!this.engineDlg || this.engineDlg.repo !== repo) return;
      const v = d.view;
      this.engineDlg.data = d;
      this.engineDlg.choice = d.status.choice;
      this.engineDlg.how = v.kopiaui_shares_file && !this.engineDefaultConfig(v) ? "move" : "share";
    } catch (e) { if (this.engineDlg) this.engineDlg.error = e.message; }
    if (this.engineDlg) this.engineDlg.loading = false;
  },
  engineDefaultConfig(v) { return /[\\/]repository\.config$/.test((v && v.config) || ""); },
  engineOtherApp(d) {
    const v = d && d.view;
    if (!v) return "another app";
    if (v.running.some((r) => r.app !== "kopialens")) return v.running.find((r) => r.app !== "kopialens").label;
    if (v.kopiaui_config) return "KopiaUI";
    if (v.blinkdisk) return "BlinkDisk";
    return "another app";
  },
  engineFacts(d) {
    const v = d.view, s = d.status;
    const rows = [["Runs backups now", this.engineAppName(v.engine) + (v.running.length ? "" : (v.engine === "kopiaui" ? " (when KopiaUI is open)" : ""))]];
    if (v.kopiaui_config) {
      rows.push(["KopiaUI", (v.kopiaui_running ? "Open" : "Not open") + (v.kopiaui_autostart ? ", starts at logon" : "")
        + (v.kopiaui_shares_file ? ", same connection file" : ", own connection file")]);
    }
    if (v.blinkdisk) rows.push(["BlinkDisk", "This is a BlinkDisk vault"]);
    if (v.identity) rows.push(["Connected as", v.identity]);
    if (v.owner) rows.push(["Maintenance owner", v.owner + (v.owner_is_us ? " (this connection)" : "")]);
    if (s.choice === "kopialens" && s.next) rows.push(["Next backup", humanTime(s.next)]);
    if (s.pause && s.pause.on) rows.push(["Paused", this.enginePauseText(s.pause)]);
    return rows;
  },
  engineCanSave() {
    const g = this.engineDlg;
    if (!g || !g.data || g.busy) return false;
    if (g.choice === g.data.status.choice) return false;
    if (g.choice === "kopialens" && g.how === "move") return !!g.password;
    if (g.choice === "kopialens" && g.how === "share") return !g.data.view.conflict;
    return true;
  },
  async engineSave() {
    if (!this.wmGuard("non_destructive", this.engineDlg && this.engineDlg.repo)) return;
    const g = this.engineDlg;
    if (!this.engineCanSave()) return;
    const v = g.data.view;
    const other = this.engineOtherApp(g.data);
    const facts = [["Repository", this.repoName(g.repo)]];
    let title, label, note = "";
    if (g.choice === "kopialens") {
      title = "Run this repository's backups with KopiaLens?";
      label = "Run backups with KopiaLens";
      facts.push(["Schedules", "Each folder's policy decides when"], ["Maintenance", v.owner_is_us === false ? "Stays with " + v.owner : "Runs from KopiaLens"]);
      if (g.how === "move") facts.push(["KopiaUI", "Stops loading this repository after it restarts"]);
      note = "Backups run while KopiaLens is open.";
    } else if (other === "another app") {   // no other app is known to run them
      title = "Stop running this repository's backups?";
      label = "Stop backups";
      facts.push(["KopiaLens", "Stops its backups and maintenance here"], ["Then", "Nothing runs them until you choose again"]);
    } else {
      title = `Hand this repository's backups back to ${other}?`;
      label = "Hand back";
      facts.push(["KopiaLens", "Stops its backups and maintenance here"], ["Then", `${other} runs them when it's open`]);
    }
    if (!(await this.klConfirm({ title, facts, note, confirmLabel: label, danger: false }))) return;
    g.busy = true; g.error = "";
    try {
      const st = await getJson("/api/engine/choice", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: g.repo, choice: g.choice, how: g.how, password: g.password }) });
      this.engineDlg = null;
      this.klNotify(g.choice !== "kopialens" ? (other === "another app" ? "Backups stopped" : "Backups handed back")
        : st && st.until_restart ? "Saved. KopiaLens takes over when KopiaUI restarts."
        : "KopiaLens runs backups for this repository now");
      if (this.ob) this.obLoadEngine();   // setup's Backups rows
      setTimeout(() => this.loadEngine(), 1500);
    } catch (e) { g.error = e.message; }
    g.busy = false;
  },
  async engineFixIdentity() {
    if (!this.wmGuard("non_destructive")) return;
    const g = this.engineDlg;
    const idc = g && g.data && g.data.identity;
    if (!idc) return;
    const facts = [["Now", idc.current], ["After", idc.suggested]];
    const ok = await this.klConfirm({
      title: `Connect as ${idc.suggested}?`, facts,
      callout: idc.kopiaui_shares_file ? "KopiaUI uses this connection file too, so it would back up these folders as well." : "",
      note: "Only this connection's name changes. The repository and its snapshots stay as they are.",
      confirmLabel: "Change the name", danger: false,
    });
    if (!ok) return;
    try {
      await getJson("/api/engine/identity", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: g.repo, identity: idc.suggested }) });
      this.klNotify("Connection name changed to " + idc.suggested);
      this.openEngineDlg(g.repo);
    } catch (e) { g.error = e.message; }
  },

  // One history again: move the snapshots under the other spelling into the kept one (kopia's
  // move-history). Policies under the old name stay where they are.
  async engineMergeHistory(dry) {
    if (!dry && !this.wmGuard("non_destructive", this.engineDlg && this.engineDlg.repo)) return;
    const g = this.engineDlg;
    const idc = g && g.data && g.data.identity;
    if (!idc || !idc.split) return;
    const params = { src: idc.merge_from, dst: idc.suggested, dry };
    if (!dry) {
      const ok = await this.klConfirm({
        title: `Merge ${idc.merge_from}'s snapshots into ${idc.suggested}?`,
        facts: [["From", idc.merge_from], ["Into", idc.suggested], ["Afterwards", "One history per folder; moved snapshots get new IDs"]],
        callout: "Policies set under " + idc.current + " stay under that name.",
        note: "Run the dry run first to see every snapshot that moves.",
        confirmLabel: "Merge histories", danger: true,
      });
      if (!ok) return;
    }
    await this.startJob("move_history", params, `${idc.merge_from} -> ${idc.suggested}`, g.repo);
  },

  // ---- Pause ----
  async enginePause(repo, hours) {
    if (hours === "custom") { hours = await this.pauseCustomHours(repo ? "Pause this repository's backups" : "Pause all backups"); if (hours == null) return; }
    try {
      await getJson("/api/engine/pause", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: repo || null, hours }) });
      this.klNotify(repo ? "Backups paused for this repository" : "Backups paused");
      this.loadEngine();
    } catch (e) { this.klNotify(e.message, "error"); }
  },
  async engineDlgPause(ev) {
    const h = ev.target.value;
    ev.target.value = "";
    if (h === "" || !this.engineDlg) return;
    const repo = this.engineDlg.repo;
    await this.enginePause(repo, h === "custom" ? "custom" : Number(h));
    this.openEngineDlg(repo);
  },
  async engineDlgResume() {
    if (!this.engineDlg) return;
    const repo = this.engineDlg.repo;
    await this.engineResume(repo);
    this.openEngineDlg(repo);
  },
  async engineResume(repo) {
    try {
      await getJson("/api/engine/resume", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: repo || null }) });
      this.klNotify("Backups resumed");
      this.loadEngine();
    } catch (e) { this.klNotify(e.message, "error"); }
  },

  // ---- Live source status on the Dashboard ----
  async loadEngineSources() {
    if (!this.engineRuns()) { if (Object.keys(this.engineSrc).length) { this.engineSrc = {}; this._refreshDashStatus(); } return; }
    try {
      const d = await getJson(`/api/engine/sources?repo=${encodeURIComponent(this.repo)}`);
      const map = {};
      for (const s of d.sources || []) map[s.source] = s;
      this.engineSrc = map;
      this._refreshDashStatus();
      const busy = Object.values(map).some((s) => s.status === "UPLOADING" || s.status === "PENDING");
      if (this._engineSrcTimer) clearTimeout(this._engineSrcTimer);
      if (busy && this.active === "dashboard") this._engineSrcTimer = setTimeout(() => this.loadEngineSources(), 3000);
    } catch (e) { /* the column just stays as it was */ }
  },
  _refreshDashStatus() {
    const el = document.querySelector("#dashboardTable");
    if (!el || !el._tabulator) return;
    for (const row of el._tabulator.getRows()) row.reformat();
  },
  sourceStatusHtml(source) {
    const s = this.engineSrc[source];
    if (!s) return "";
    if (s.status === "UPLOADING") {
      const p = s.progress || {};
      const done = (p.hashedBytes || 0) + (p.cachedBytes || 0);
      const pct = p.estimatedBytes ? Math.min(100, Math.round((done * 100) / p.estimatedBytes)) : null;
      return `<span class="text-warn font-medium" title="${(p.directory || "").replace(/"/g, "&quot;")}">Backing up${pct != null ? " " + pct + "%" : ""} · ${fmtBytes(done)}</span>`;
    }
    if (s.status === "PENDING") return `<span class="text-warn" title="Starts when the backup before it finishes">Waiting</span>`;
    if (s.paused) {
      const until = s.paused.until ? "Until " + humanTime(s.paused.until) : "Until you resume";
      return `<span class="text-muted" title="${escHtml(until)}">Paused</span> <button class='kl-btn quiet' data-act='sresume' style='height:22px'>Resume</button>`;
    }
    return "";
  },
  engineSrcPaused(source) { const s = this.engineSrc[source]; return !!(s && s.paused); },
  // Why a KopiaLens-only action is off for this repository (null when it's available).
  engineOffReason() {
    if (this.engineRuns()) return null;
    const e = this.engineFor();
    if (e && e.choice === "kopialens") return e.state === "paused" ? "Backups are paused for this repository" : "KopiaLens's backups aren't running right now";
    return "Only when KopiaLens runs this repository's backups";
  },
  // A menu entry that stays visible but greyed out, with the reason on hover.
  offMenuItem(label, reason) {
    return { label: `<span title="${escHtml(reason)}">${escHtml(label)}</span>`, disabled: true };
  },
  // Row menu entries for one folder's scheduled backups (only where KopiaLens runs them).
  engineSourceMenu(source) {
    if (!this.engineFor() || this.isDemo()) return [];
    const off = this.engineOffReason();
    if (off) return [this.offMenuItem("Pause backups", off)];
    if (this.engineSrcPaused(source)) return [{ label: "Resume backups", action: () => this.engineSourceResume(source) }];
    return [{ label: "Pause backups", menu: [...PAUSE_CHOICES.map(([h, l]) => ({ label: l, action: () => this.engineSourcePause(source, h) })),
      { label: "Custom…", action: () => this.engineSourcePause(source, "custom") }] }];
  },
  async engineSourcePause(source, hours) {
    if (hours === "custom") { hours = await this.pauseCustomHours("Pause backups of " + this.shortSource(source)); if (hours == null) return; }
    const s = this.engineSrc[source];
    if (s && s.status === "UPLOADING") {
      const ok = await this.klConfirm({ title: "Pause this folder's backups?",
        facts: [["Folder", this.shortSource(source)], ["Paused", hours ? "until " + humanTime(Date.now() / 1000 + hours * 3600) : "until you resume"]],
        callout: "The backup running now stops.", confirmLabel: "Pause backups", danger: false });
      if (!ok) return;
    }
    try {
      await getJson("/api/engine/source-pause", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source, hours }) });
      this.klNotify("Backups paused for " + this.shortSource(source));
      setTimeout(() => this.loadEngineSources(), 800);
    } catch (e) { this.klNotify(e.message, "error"); }
  },
  async engineSourceResume(source) {
    try {
      await getJson("/api/engine/source-resume", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source }) });
      this.klNotify("Backups resumed for " + this.shortSource(source));
      setTimeout(() => this.loadEngineSources(), 800);
    } catch (e) { this.klNotify(e.message, "error"); }
  },
  nextCellHtml(row, cell) {
    const s = this.engineSrc[row.source];
    const next = (s && s.next) || row.next_snapshot;
    if (!next) return "—";
    const overdue = toDate(next).getTime() < Date.now() - 15 * 60 * 1000 && !(s && (s.status === "UPLOADING" || s.status === "PENDING"));
    return timeCell(next, cell) + (overdue && (this.engineRuns() || row.next_snapshot) ? ' <span class="pill" style="background:var(--warn-bg);color:var(--warn)" title="This backup didn\'t start at its scheduled time">overdue</span>' : "");
  },
  async engineSnapshotNow(source) {
    if (!this.wmGuard("non_destructive")) return;
    if (this.askSnapshotNow) {
      const ok = await this.klConfirm({
        title: "Back up this folder now?",
        facts: [["Folder", this.shortSource(source)], ["Repository", this.repoName(this.repo)]],
        note: "Kopia applies this folder's policy: its exclusions, compression and retention.",
        confirmLabel: "Back up now", danger: false, dontAsk: true,
      });
      if (!ok) return;
      this.snapshotNowDontAsk(ok);
    }
    try {
      await getJson("/api/engine/snapshot", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source }) });
      this.klNotify("Backup started", "ok", { label: "Show in Tasks", run: () => { this.active = "tasks"; this.activate(); } });
      setTimeout(() => this.loadEngineSources(), 1000);
    } catch (e) { this.klNotify(e.message, "error"); }
  },
  async engineCancel(source) {
    const ok = await this.klConfirm({
      title: "Stop this backup?", facts: [["Folder", this.shortSource(source)]],
      confirmLabel: "Stop backup", danger: true,
    });
    if (!ok) return;
    try {
      await getJson("/api/engine/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source }) });
      this.klNotify("Stopping the backup…");
      setTimeout(() => this.loadEngineSources(), 1000);
    } catch (e) { this.klNotify(e.message, "error"); }
  },
};
