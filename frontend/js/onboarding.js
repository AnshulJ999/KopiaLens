// First-run onboarding: Welcome -> Connect -> Protect -> Index -> Summary
// (partials/onboarding.html). Shown instead of the app while /api/onboarding/state says it is needed.
// Everything it sets is changeable in Settings. Also the in-app tour (Driver.js).
const OB_STEPS = [["welcome", "Welcome"], ["connect", "Connect"], ["protect", "Protect"], ["start", "Index"], ["summary", "Summary"]];

// The Summary step at the end. Off: setup ends on the index step, which starts indexing.
const OB_FINISH_SCREEN = true;

const OB_TIME_CHIPS = [["02:00", "Overnight"], ["08:00", "Morning"], ["20:00", "Evening"]];

const onboardingPart = {
    noRepos: false, setupSkipped: false,

    async obBegin(state) {
      const step = state.rerun ? "connect" : (state.resume_step || (state.kopia_user ? "connect" : "welcome"));
      this.ob = {
        step, state, needsCode: !state.setup_unlocked, code: "", codeError: "", codeBusy: false,
        rerun: !!state.rerun, kopiaUser: state.kopia_user || "yes", returnTo: "",
        kopiaPath: "", kopiaError: "",
        username: "admin", pw: "", confirm: "", level: "index", usePw: true, encPw: "", encConfirm: "",
        remember: !state.docker, hosts: (state.allowed_hosts || []).join(", "), busy: false, error: "",
        recoveryKey: "", keySaved: false, keyNote: "", protected: "", sampleBusy: false, sampleError: "",
        auto: state.index_mode !== "off", time: this._obTime(state), engine: {}, engineAuto: {},
      };
      if (this.ob.needsCode) return;
      await this._obStart();
    },
    // A first setup suggests Overnight; a time already chosen stays.
    _obTime(state) { return state.index_time_set ? (state.index_time || "02:00") : "02:00"; },
    async _obStart() {
      const st = this.ob.state;
      await _setupPost("/api/onboarding/begin").catch(() => {});
      if (st.protection_chosen) this.ob.protected = st.has_account ? "account" : "skipped";
      getJson("/api/app/host").then((h) => { this.host = h; }).catch(() => {});
      await this.cxOpen(st.repos || []);
    },
    // Run setup again starts at Connect; the code switch drops Summary.
    obSteps() {
      const rerun = !!(this.ob && this.ob.rerun);
      return OB_STEPS.filter(([s]) => (s !== "welcome" || !rerun) && (s !== "summary" || OB_FINISH_SCREEN));
    },
    obFinishScreen() { return OB_FINISH_SCREEN; },
    obStepIndex(step) {
      const s = step === "key" ? "protect" : step;
      return this.obSteps().findIndex((x) => x[0] === s);
    },
    obStepDone(step) { return this.obStepIndex(step) < this.obStepIndex(this.ob.step); },
    obGo(step) { if (this.obStepDone(step)) { this.ob.step = step; this.ob.error = ""; } },
    // The step after `from`, or back to Summary after a Change from there.
    obNext(from) {
      const o = this.ob;
      const order = this.obSteps().map((s) => s[0]);
      let next = order[order.indexOf(from === "key" ? "protect" : from) + 1];
      if (o.returnTo && from !== o.returnTo) { next = o.returnTo; o.returnTo = ""; }
      if (next) { o.step = next; o.error = ""; }
    },
    obChange(step) { this.ob.returnTo = "summary"; this.ob.step = step; this.ob.error = ""; },
    obNewcomer() { return !!(this.ob && !this.ob.rerun && this.ob.kopiaUser === "no"); },
    timeChips() { return OB_TIME_CHIPS; },

    // ---- Welcome, skip, close ------------------------------------------------------------------
    async obStartSetup() {
      const o = this.ob;
      o.busy = true; o.error = "";
      try {
        await _setupPost("/api/onboarding/answer", { kopia_user: o.kopiaUser });
        o.state.kopia_user = o.kopiaUser;
        this.obNext("welcome");
      } catch (e) { o.error = e.message || String(e); }
      o.busy = false;
    },
    // Skip setup (Welcome) or Finish setup later (any step): the app opens with a Finish setup banner.
    async obSkip() {
      const o = this.ob;
      if (o.busy) return;
      const welcome = o.step === "welcome";
      await this.loadFreshness();
      const added = (this.cx && this.cx.added) || [];
      const kopia = this.obKopiaAdded().map((a) => a.id);
      const fresh = (this.freshness || []).filter((f) => kopia.includes(f.repo));
      const index = !kopia.length ? "Not built" : (fresh.some((f) => f.running) ? "Building"
        : (fresh.some((f) => f.last_index) ? "Built" : "Not built"));
      const open = o.state.profile === "server" && o.protected !== "account";
      const ha = !!o.state.ingress;
      const ok = await this.klConfirm({
        title: welcome ? "Skip setup?" : "Finish setup later?",
        callout: !open ? "" : (ha ? "The Home Assistant sidebar still asks for Home Assistant's sign-in. If you give KopiaLens its own port, anyone who can reach that port can open it."
          : "Anyone who can reach this address can open KopiaLens and act on your repositories."),
        facts: [["Repositories", added.map((a) => a.name).join(", ") || "None connected"],
                ["Sign-in", o.protected === "account" ? "On" : "Off"], ["Index", index]],
        note: "Finish setup any time from the banner at the top.",
        confirmLabel: welcome ? "Skip setup" : "Finish later", danger: open && !ha,
      });
      if (!ok) return;
      o.busy = true; o.error = "";
      try {
        await this._obSaveHosts();
        await _setupPost("/api/onboarding/skip", { step: welcome ? "" : (o.step === "key" ? "start" : o.step) });
        location.reload();
      } catch (e) { o.error = e.message || String(e); o.busy = false; }
    },
    // "Run setup again": close without finishing; what was already saved stays.
    async obClose() {
      try {
        await _setupPost("/api/onboarding/close");
        location.reload();
      } catch (e) { this.ob.error = e.message || String(e); }
    },
    async obResume() {
      try {
        await _setupPost("/api/onboarding/resume");
        location.reload();
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    async obDismissSetup() {
      try {
        await _setupPost("/api/onboarding/dismiss");
        this.setupSkipped = false;
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },

    // ---- Setup code (server install, setup window closed) -----------------------------------
    obNeedCode() {
      if (this.ob) this.ob.needsCode = true;
      // Before init's own onboarding check, that check shows the code screen; reloading here loops.
      else if (this._obChecked) location.reload();   // the reload lands on the setup-code screen
    },
    async obSubmitCode() {
      const o = this.ob;
      if (!o.code.trim() || o.codeBusy) return;
      o.codeBusy = true; o.codeError = "";
      try {
        await _setupPost("/api/onboarding/setup-code", { code: o.code.trim() });
        o.needsCode = false; o.code = "";
        if (!this.cx) {
          o.state = await getJson("/api/onboarding/state");
          o.hosts = (o.state.allowed_hosts || []).join(", ");
          o.auto = o.state.index_mode !== "off";
          o.time = this._obTime(o.state);
          o.rerun = !!o.state.rerun;
          o.kopiaUser = o.state.kopia_user || "yes";
          o.step = o.rerun ? "connect" : (o.state.resume_step || (o.state.kopia_user ? "connect" : "welcome"));
          await this._obStart();
        }
      } catch (e) { o.codeError = e.message || String(e); }
      o.codeBusy = false;
    },

    // ---- Connect ----------------------------------------------------------------------------
    obKopiaMissing() { return !!(this.ob && this.ob.state.kopia && !this.ob.state.kopia.available); },
    async obKopiaSave() {
      const o = this.ob;
      o.kopiaError = "";
      try {
        o.state.kopia = await _setupPost("/api/setup/kopia", { path: o.kopiaPath.trim() });
      } catch (e) { o.kopiaError = e.message || String(e); }
    },
    obConnectReady() {
      const added = (this.cx && this.cx.added) || [];
      return !!added.length && (!this.obKopiaMissing() || added.every((a) => a.sample || a.backblaze));
    },
    // Repositories in this setup that kopia indexes (not the sample, Backblaze or a server connection).
    obKopiaAdded() { return ((this.cx && this.cx.added) || []).filter((a) => !a.sample && !a.backblaze && !a.server); },
    obBackblazeOnly() {
      const added = (this.cx && this.cx.added) || [];
      return !!added.length && added.every((a) => a.backblaze || a.sample) && added.some((a) => a.backblaze);
    },
    obHasSample() { return !!(this.cx && this.cx.added.some((a) => a.sample)); },
    obSampleOnly() { return !!(this.cx && this.cx.added.length) && this.cx.added.every((a) => a.sample); },
    async obSample() {
      const o = this.ob;
      if (o.sampleBusy || this.obHasSample()) return;
      o.sampleBusy = true; o.sampleError = "";
      try {
        const r = await this.addSampleData();
        this.cx.added.push(this._cxEntry(r));
        this.cxSummary(this.cx.added[this.cx.added.length - 1]);   // the tracked copy
      } catch (e) { o.sampleError = e.message || String(e); }
      o.sampleBusy = false;
    },

    // ---- Protect ----------------------------------------------------------------------------
    obLevelTip(l) {
      return {
        off: "Anyone who can open KopiaLens's data folder can read the file names in the index.",
        index: "The same protection kopia gives your backups: file names in the index need the password.",
        full: "Stricter than kopia: also history, logs and policy backups need the password.",
      }[l] || "";
    },
    obEncryptionText() {
      const o = this.ob;
      if (o.recoveryKey) return this.encLevelLabel(o.level);
      const l = o.state.encryption_level;
      return l && l !== "off" ? this.encLevelLabel(l) : "Off";
    },
    _obProtectCheck() {
      const o = this.ob;
      if (!o.username.trim()) return "Enter a username.";
      if (o.pw.length < 8) return "Use a password of at least 8 characters.";
      if (o.pw !== o.confirm) return "The passwords don't match.";
      if (o.level !== "off" && !o.usePw) {
        if (o.encPw.length < 8) return "Use an encryption password of at least 8 characters.";
        if (o.encPw !== o.encConfirm) return "The encryption passwords don't match.";
      }
      return "";
    },
    async obProtect() {
      const o = this.ob;
      if (o.busy) return;
      o.error = this._obProtectCheck();
      if (o.error) return;
      o.busy = true;
      try {
        if (o.protected !== "account") {   // a retry after a failed encryption step keeps the account
          await _setupPost("/api/account/create", { username: o.username.trim(), password: o.pw });
          o.protected = "account";
        }
        await this._obSaveHosts();
        if (o.level !== "off") {
          const r = await _setupPost("/api/encryption/setup", { password: o.usePw ? o.pw : o.encPw, remember: o.remember, recovery: true, level: o.level });
          try {
            await _setupPost("/api/encryption/level", { level: o.level });
          } catch (e) {
            o.keyNote = `Encryption is set up, but encrypting the data didn't start: ${e.message}. Choose the level again in Settings → Encryption.`;
          }
          o.recoveryKey = r.recovery_key || "";
          o.rememberState = (r.status && r.status.remember_state) || "";
          o.pw = o.confirm = o.encPw = o.encConfirm = "";
          if (o.recoveryKey) o.step = "key"; else this.obNext("protect");
        } else {
          o.pw = o.confirm = "";
          this.obNext("protect");
        }
      } catch (e) { o.error = e.message || String(e); }
      o.busy = false;
    },
    async _obSaveHosts() {
      const o = this.ob;
      if (o.state.profile !== "server") return;
      const hosts = o.hosts.split(/[\s,]+/).map((h) => h.trim()).filter(Boolean);
      await _setupPost("/api/access", { allowed_hosts: hosts });
    },
    async obSkipProtect() {
      const o = this.ob;
      if (o.state.profile === "server") {
        const ha = !!o.state.ingress;
        const ok = await this.klConfirm({
          title: "Continue without a password?",
          body: ha ? "The Home Assistant sidebar still asks for Home Assistant's sign-in. If you give KopiaLens its own port, anyone who can reach that port can open it."
            : "Anyone who can reach this address can open KopiaLens and act on your repositories.",
          note: "You can add a password any time in Settings → Account.",
          confirmLabel: "Continue without a password", danger: !ha,
        });
        if (!ok) return;
      }
      o.busy = true; o.error = "";
      try {
        await _setupPost("/api/account/enabled", { on: false });
        await this._obSaveHosts();
        o.protected = "skipped";
        this.obNext("protect");
      } catch (e) { o.error = e.message || String(e); }
      o.busy = false;
    },

    // ---- Build the index --------------------------------------------------------------------
    // Who runs each repository's backups. A repository created in this setup has nothing else
    // running it, so KopiaLens takes it on; everything else stays as it is unless changed here.
    async obLoadEngine() {
      const o = this.ob;
      await this._cxRefreshRepos();   // names for the Backups dialog
      for (const a of this.obKopiaAdded()) {
        try {
          let d = await getJson(`/api/engine/repo?repo=${encodeURIComponent(a.id)}`);
          const v = d.view;
          if (a.created && !o.engineAuto[a.id]) {   // once: a later "hand back" stays handed back
            o.engineAuto = { ...o.engineAuto, [a.id]: true };
            if (d.status.choice !== "kopialens" && v.engine === "none" && !v.conflict) {
              await _setupPost("/api/engine/choice", { repo: a.id, choice: "kopialens", how: "share" });
              d = await getJson(`/api/engine/repo?repo=${encodeURIComponent(a.id)}`);
            }
          }
          o.engine = { ...o.engine, [a.id]: d };
        } catch (e) { o.engine = { ...o.engine, [a.id]: { error: e.message || String(e) } }; }
      }
    },
    obEngineLine(a) {
      const d = this.ob.engine[a.id];
      if (!d) return "Checking…";
      if (d.error) return "Couldn't check";
      if (d.status.choice === "kopialens") return "KopiaLens";
      const v = d.view;
      if (v.engine === "none") return "Nothing yet";
      return this.engineAppName(v.engine) + (v.engine === "kopiaui" && !v.running.length ? " (when it's open)" : "");
    },
    obSummary() {
      const o = this.ob;
      const names = ((this.cx && this.cx.added) || []).map((a) => a.name + (a.server ? " (browse-only, not indexed)" : "")).join(", ");
      return [
        ["Repositories", names || "None"],
        ["Sign-in", o.protected === "account" ? "On" : (o.protected === "skipped" ? "Off" : "Unchanged")],
        ["Encryption", o.recoveryKey ? `${this.encLevelLabel(o.level)}${this._obRememberText()}` : (o.protected === "account" && o.level === "off" ? "Off" : (o.state.encryption_level && o.state.encryption_level !== "off" ? this.encLevelLabel(o.state.encryption_level) : "Unchanged"))],
      ];
    },
    _obRememberText() {
      const o = this.ob;
      if (!o.remember) return "";
      return { waiting: ", waiting for this computer's keychain", unavailable: ", password needed after a restart (this computer's keychain didn't accept the key)" }[o.rememberState]
        || ", unlocks automatically";
    },
    obEstimate() {
      const list = ((this.cx && this.cx.added) || [])
        .filter((a) => !a.server && !a.sample && a.summary && a.summary.snapshots && a.summary.estimate)
        .map((a) => a.summary.estimate);
      return list.length ? sumEstimates(list) : null;
    },
    obInterval() { return this.ob.state.index_mode === "interval"; },
    obNextRun() {
      const o = this.ob;
      if (!o.auto) return "";
      if (this.obInterval()) return "within the update interval set in Settings";
      const [hh, mm] = String(o.time || "02:00").split(":").map(Number);
      const now = new Date();
      const at = new Date(now); at.setHours(hh || 0, mm || 0, 0, 0);
      return `at ${o.time} ${at > now ? "today" : "tomorrow"}`;
    },
    async obSkipIndex() {
      const o = this.ob;
      const ok = await this.klConfirm({
        title: "Skip indexing for now?",
        body: "Pages stay empty until the index is built.",
        note: o.auto
          ? `Indexing starts ${this.obNextRun()} because automatic updates are on. You can start it any time with Index in the top bar.`
          : "Automatic updates are off, so nothing runs until you start it with Index in the top bar.",
        confirmLabel: "Skip for now",
      });
      if (ok) await this.obFinish(false);
    },
    async obFinish(index) {
      const o = this.ob;
      if (o.busy) return;
      o.busy = true; o.error = "";
      const added = (this.cx && this.cx.added) || [];
      const ids = added.map((a) => a.id);
      const toIndex = added.filter((a) => !a.sample).map((a) => a.id);   // the sample is ready-made
      try {
        const kl = {};
        if (this.obKopiaAdded().length) {   // the kopia schedule is only asked when a kopia repository was added
          kl.index_mode = o.auto ? (this.obInterval() ? "interval" : "daily") : "off";
          if (o.auto && !this.obInterval()) kl.index_time = o.time || "02:00";
        }
        await _setupPost("/api/settings", { kopialens: kl, last_repo: ids[0] || "" });
        await _setupPost("/api/onboarding/finish");
        if (index && toIndex.length) await _setupPost("/api/onboarding/start-indexing", { repos: toIndex });
        this._obAfterSetup(o.rerun ? "" : "tour");
        location.reload();
      } catch (e) {
        o.error = e.message || String(e);
        o.busy = false;
      }
    },

    // ---- Summary ------------------------------------------------------------------------------
    // [label, value, step its Change opens ("" = none: an account set here changes in Settings)].
    obSummaryRows() {
      const o = this.ob;
      const protect = o.protected === "account" ? "settings" : "protect";   // sign-in exists: changed in Settings
      const rows = this.obSummary().map((r) => [r[0], !o.rerun && r[1] === "Unchanged" ? "Off" : r[1], r[0] === "Repositories" ? "connect" : protect]);
      const kopia = this.obKopiaAdded();
      if (kopia.length) {
        rows.push(["Backups", kopia.map((a) => `${a.name}: ${this.obEngineLine(a)}`).join(", "), "start"]);
        rows.push(["Updates the index", !o.auto ? "Off" : (this.obInterval() ? "At the interval set in Settings" : `Every day at ${o.time}`), "start"]);
      }
      return rows;
    },
    // Help pages only: they open over setup, so the final button still finishes it.
    obWhereToStart() {
      return [
        this.obNewcomer() && this.obKopiaAdded().length ? ["guide-start-from-scratch", "Set up your first backup"] : ["dashboard", "See your backups on the Dashboard"],
        ["guide-find-space", "Find what takes the most space"],
        ["getting-started", "Read Getting started"],
      ];
    },
    // What the app does once after setup's reload: start the tour.
    _obAfterSetup(action) {
      try { if (action) sessionStorage.setItem("kl-after-setup", action); } catch (e) { /* storage blocked: the app just opens */ }
    },
    _tourAfterSetup() {
      let a = "";
      try { a = sessionStorage.getItem("kl-after-setup") || ""; sessionStorage.removeItem("kl-after-setup"); } catch (e) { /* storage blocked */ }
      if (a === "tour" && !this.noRepos) setTimeout(() => this.startTour(), 800);
    },

    // ---- The tour (Help > Take the tour) -------------------------------------------------------
    startTour() {
      this.helpMenu = false;
      if (this.noRepos || !(window.driver && window.driver.js)) return;
      if (this.repoKind === "kopia" && this.active !== "dashboard") { this.active = "dashboard"; this.activate(); }
      setTimeout(() => {
        const shown = (sel) => { const el = document.querySelector(sel); return !!(el && el.offsetParent); };
        const steps = [
          { element: "[data-tour=repo]", popover: { title: "Your repositories", description: "Switch between repositories here, or add another one." } },
          { element: "[data-tour=tabs]", popover: { title: "Tabs", description: "Each tab shows the selected repository a different way: snapshots, folders, search, what changed and more." } },
          { element: "[data-tour=search]", popover: { title: "Search", description: "Find any file in every snapshot. Start typing to search." } },
          { popover: { title: "File and folder panels", description: "Double-click a file or folder anywhere to open its panel: its versions, the space it takes and the snapshots that hold it." } },
          { element: "[data-testid=backups-chip]", popover: { title: "Backups", description: "Which app takes your snapshots on schedule. Pause them from here when you need to." } },
          { element: "[data-tour=help]", popover: { title: "Help", description: "Documentation, Report a problem, and this tour again any time." } },
        ].filter((s) => !s.element || shown(s.element));
        window.driver.js.driver({
          steps, showProgress: true, progressText: "{{current}} of {{total}}",
          nextBtnText: "Next", prevBtnText: "Back", doneBtnText: "Done", popoverClass: "kl-tour",
          overlayClickBehavior: () => {},   // only ✕ or Esc close it: a stray click shouldn't lose the tour
        }).drive();
      }, 300);
    },
};
