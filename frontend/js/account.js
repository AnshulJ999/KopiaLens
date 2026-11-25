// Settings Account + Encryption sections and the unlock box. One dialog
// (acctDialog, partials/lock.html) serves every account and encryption form; each route answers
// with the current state, which replaces acct / encStatus.
const _acctPost = (url, body) => getJson(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

const ACCT_DIALOGS = {
  create: ["Create an account", "Create account"],
  username: ["Change username", "Change username"],
  password: ["Change your password?", "Change password"],
  split: ["Use a separate encryption password?", "Use separate passwords"],
  join: ["Use one password for both?", "Use one password"],
  encpw: ["Change the encryption password", "Change password"],
  "enc-setup": ["Turn on encryption", "Turn on encryption"],
  level: ["Change the encryption level", "Change level"],
  "recovery-new": ["Create a new recovery key?", "Create recovery key"],
  showkey: ["Save your recovery key", "Done"],
  startover: ["Start over without the password?", "Start over"],
};
// Modes that set a new password: it needs the minimum length and a matching confirmation.
const ACCT_NEW_PASSWORD = ["create", "password", "split", "encpw"];

const accountPart = {

    async loadAccount() {
      try {
        const [a, e] = await Promise.all([getJson("/api/account"), getJson("/api/encryption/status")]);
        this.acct = a;
        this.encStatus = e;
      } catch (err) { this.error = err.message || String(err); }
    },
    _acctApply(r) {
      if (r && r.state) this.acct = r.state;
      if (r && r.status) this.encStatus = r.status;
    },
    daysLabel(d) {
      return { 1: "1 day", 7: "1 week", 14: "2 weeks", 30: "30 days", 90: "3 months", 180: "6 months", 365: "1 year" }[d] || `${d} days`;
    },
    acctPasswordNote() {
      const a = this.acct;
      if (!a) return "";
      if (a.unlocks_encryption) return "Also unlocks encryption. One password for both.";
      return a.encryption && a.encryption.configured ? "Separate from the encryption password." : "";
    },

    // ---- Encryption state --------------------------------------------------------------------
    encOn() { const s = this.encStatus; return !!(s && s.configured && s.level !== "off"); },
    encPill() {
      const s = this.encStatus;
      if (!this.encOn()) return ["Off", ""];
      return s.unlocked ? ["On · unlocked", "ok"] : ["On · locked", "warn"];
    },
    encLevelLabel(l) { return { off: "Off", index: "Index", full: "Full" }[l] || l; },
    // Lowering the level asks for the encryption password (the server checks it too).
    encLowering(l) {
      const s = this.encStatus, order = ["off", "index", "full"];
      return !!(s && s.configured) && order.indexOf(l) < order.indexOf(s.level);
    },
    encLevelNote(l) {
      return {
        off: "The index is stored as plain files.",
        index: "File and folder names in the index are encrypted.",
        full: "The index, history, logs, policy backups and Backblaze data are encrypted.",
      }[l] || "";
    },
    encPendingText() {
      const s = this.encStatus;
      if (!s || !this.encOn()) return "";
      const repos = (s.pending || []).map((r) => this.repoLabel(r));
      if (s.files_pending) repos.push("settings and history files");
      return repos.length ? `Not yet rewritten at this level: ${repos.join(", ")}.` : "";
    },
    encPasswordNote() {
      const a = this.acct;
      if (a && a.unlocks_encryption) return "Same as your sign-in password.";
      return a && a.has_account ? "Separate from your sign-in password." : "";
    },
    encTurnOn() {
      if (this.encStatus && this.encStatus.configured) this.openAcct("level", { level: "index" });
      else this.openAcct("enc-setup", { usePw: !!(this.acct && this.acct.has_account) });
    },

    // ---- The shared dialog -------------------------------------------------------------------
    openAcct(mode, extra) {
      const level = this.encStatus && this.encStatus.level !== "off" ? this.encStatus.level : "index";
      this.acctDialog = {
        mode, busy: false, error: "", username: "", current: "", pw: "", confirm: "", code: "",
        level, usePw: true, remember: true, key: "", note: "", ...(extra || {}),
      };
    },
    acctTitle() { return (ACCT_DIALOGS[this.acctDialog?.mode] || ["", ""])[0]; },
    acctButton() {
      const d = this.acctDialog;
      if (!d) return "";
      if (d.busy) return { "enc-setup": "Turning on…", startover: "Starting over…" }[d.mode] || "Saving…";
      return (ACCT_DIALOGS[d.mode] || ["", "Save"])[1];
    },
    acctSharedPw() { return !!(this.acct && this.acct.unlocks_encryption); },
    _acctCheck(d) {
      if ((d.mode === "create" || d.mode === "username") && !d.username.trim()) return "Enter a username.";
      if (["password", "split", "join", "encpw", "recovery-new"].includes(d.mode) && !d.current) return "Enter your current password.";
      if (d.mode === "level" && this.encLowering(d.level) && !d.current) return "Enter the encryption password to lower encryption.";
      const setsNew = ACCT_NEW_PASSWORD.includes(d.mode) || (d.mode === "enc-setup" && !d.usePw);
      if (setsNew || d.mode === "join" || d.mode === "enc-setup") {
        if (!d.pw) return "Enter a password.";
      }
      if (setsNew) {
        if (d.pw.length < 8) return "Use a password of at least 8 characters.";
        if (d.pw !== d.confirm) return "The passwords don't match.";
      }
      if (d.mode === "create" && this.acct?.first_run?.needs_code && !d.code.trim()) return "Enter the setup code.";
      if (d.mode === "startover" && !d.choice) return "Choose what happens to the old files.";
      return "";
    },
    async acctSubmit() {
      const d = this.acctDialog;
      if (!d || d.busy) return;
      if (d.mode === "showkey") { this.acctDialog = null; return; }
      d.error = this._acctCheck(d);
      if (d.error) return;
      d.busy = true;
      try {
        await this._acctRun(d);
      } catch (e) {
        d.error = e.message || String(e);
      } finally {
        d.busy = false;
      }
    },
    async _acctRun(d) {
      let r;
      switch (d.mode) {
        case "create":
          r = await _acctPost("/api/account/create", { username: d.username.trim(), password: d.pw, code: d.code.trim() });
          this._acctApply(r); this.acctDialog = null;
          this.klNotify("Account created. Sign-in is on.");
          return;
        case "username":
          r = await _acctPost("/api/account/username", { username: d.username.trim() });
          this._acctApply(r); this.acctDialog = null;
          this.klNotify("Username changed");
          return;
        case "password":
          r = await _acctPost("/api/account/password", { current: d.current, new: d.pw });
          this._acctApply(r); this.acctDialog = null;
          this.klNotify(r.both ? "Password changed for sign-in and encryption" : "Password changed");
          return;
        case "split":
          r = await _acctPost("/api/account/encryption/split", { current: d.current, encryption_password: d.pw });
          this._acctApply(r); this.acctDialog = null;
          this.klNotify("Encryption now has its own password");
          return;
        case "join":
          r = await _acctPost("/api/account/encryption/join", { password: d.current, encryption_password: d.pw });
          this._acctApply(r); this.acctDialog = null;
          this.klNotify("One password now does both");
          return;
        case "encpw":
          r = await _acctPost("/api/encryption/change-password", { old: d.current, new: d.pw });
          this._acctApply(r); await this.loadAccount(); this.acctDialog = null;
          this.klNotify("Encryption password changed");
          return;
        case "level":
          r = await _acctPost("/api/encryption/level", { level: d.level, password: this.encLowering(d.level) ? d.current : "" });
          this._acctApply(r); this.acctDialog = null;
          this.klNotify(`Changing encryption to ${this.encLevelLabel(d.level)}. Progress is in the jobs panel.`);
          return;
        case "recovery-new":
          r = await _acctPost("/api/encryption/recovery-key", { password: d.current });
          this._acctApply(r);
          this.openAcct("showkey", { key: r.recovery_key });
          return;
        case "enc-setup":
          return this._encSetup(d);
        case "startover":
          r = await _acctPost("/api/encryption/reset", { confirm: true, keep_copy: d.choice === "aside" });
          try {
            const left = r.failed ? ` ${countOf(r.failed, "file")} couldn't be ${r.kept_copy ? "moved" : "deleted"}; see the log.` : "";
            sessionStorage.setItem("kl-flash", (r.kept_copy ? `Started over. The old files are in ${r.kept_copy}.` : "Started over. The old files were deleted.") + left);
          } catch (e) { /* the page still reloads without the note */ }
          location.reload();
          return;
      }
    },
    // Setup creates the key and returns the recovery key once; the level call then starts the
    // rewrite job. The key is shown even if starting the job fails - it can never be shown again.
    async _encSetup(d) {
      const r = await _acctPost("/api/encryption/setup", { password: d.pw, remember: d.remember, recovery: true, level: d.level });
      let note = "";
      try {
        await _acctPost("/api/encryption/level", { level: d.level });
      } catch (e) {
        note = `Encryption is set up, but rewriting the data didn't start: ${e.message}. Choose the level again in Settings to retry.`;
      }
      await this.loadAccount();
      if (d.usePw && this.acct && this.acct.has_account && !this.acct.unlocks_encryption)
        note = (note ? note + " " : "") + "That wasn't your sign-in password, so encryption uses it as a separate password.";
      this.openAcct("showkey", { key: r.recovery_key || "", note });
    },
    copyRecoveryKey(key) {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(key).then(() => this.klNotify("Recovery key copied"), () => this.klNotify("Couldn't copy. Select the key and copy it.", "warn"));
      } else {
        this.klNotify("This browser blocks copying here. Select the key and copy it.", "warn");
      }
    },
    downloadRecoveryKey(key) {
      const text = `KopiaLens recovery key\n\n${key}\n\nIt unlocks the KopiaLens index if the password is lost. Keep it somewhere safe, away from this computer.\n`;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
      a.download = "kopialens-recovery-key.txt";
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    },

    // ---- Direct actions ----------------------------------------------------------------------
    async toggleSignIn(on) {
      let password = "";
      if (!on) {
        const ok = await this.klConfirm({
          title: "Turn off sign-in?",
          callout: "Anyone who can reach this address will be able to browse and act on the repositories.",
          note: "Your account is kept. Turning sign-in back on needs no new password.",
          input: this.acct && this.acct.protected ? { label: "Your password", type: "password" } : null,
          confirmLabel: "Turn off sign-in", danger: true,
        });
        if (!ok) return;
        password = (ok && ok.value) || "";
      }
      try {
        this._acctApply(await _acctPost("/api/account/enabled", { on, password }));
        this.klNotify(on ? "Sign-in is on" : "Sign-in is off");
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    // Inside Home Assistant, HA has signed its user in: signing out of KopiaLens does nothing there.
    viaHa() { return !!window.KL_HA; },
    async signOutHere() {
      await _acctPost("/api/auth/logout").catch(() => {});
      location.href = appUrl("/login");
    },
    async signOutAll() {
      const ok = await this.klConfirm({
        title: "Sign out every browser?",
        body: "Every browser signed in to KopiaLens is signed out, including this one.",
        confirmLabel: "Sign out all devices",
      });
      if (!ok) return;
      try {
        await _acctPost("/api/account/sign-out-all");
        location.href = appUrl("/login");
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    async toggleRemember(on) {
      try {
        const r = await _acctPost("/api/encryption/remember", { on });
        this._acctApply(r);
        const waiting = r && r.status && r.status.remember_state === "waiting";
        this.klNotify(waiting ? "Waiting for this computer's keychain" : (on ? "Unlocks automatically on this computer" : "Asks for the password after every restart"));
        if (waiting) this._watchKeychain();
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    // The keychain answers in the background (a password window may be waiting); follow it until it does.
    _watchKeychain() {
      clearTimeout(this._keychainTimer);
      this._keychainTimer = setTimeout(async () => {
        const s = await getJson("/api/encryption/status").catch(() => null);
        if (s) this.encStatus = s;
        if (!s || s.remember_state === "waiting") return this._watchKeychain();
        if (s.remember_state === "unavailable") this.klNotify("This computer's keychain didn't accept the key", "warn");
        else this.klNotify(s.remembered ? "Unlocks automatically on this computer" : "Asks for the password after every restart");
      }, 2000);
    },
    async lockNow() {
      const ok = await this.klConfirm({
        title: "Lock the index?",
        body: "KopiaLens shows no data until someone enters the password.",
        confirmLabel: "Lock",
      });
      if (!ok) return;
      try {
        await _acctPost("/api/encryption/lock");
        location.reload();
      } catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    // Lost the password AND the recovery key. The user must choose what happens to the
    // unreadable files; nothing is pre-selected.
    async startOver() {
      let p;
      try { p = await getJson("/api/encryption/reset-preview"); } catch (e) { this.klNotify(e.message || String(e), "warn"); return; }
      this.openAcct("startover", { preview: p, choice: "" });
    },
    startOverFacts() {
      const p = this.acctDialog && this.acctDialog.preview;
      if (!p) return [];
      return [
        ["Encrypted files", fmtBytes(p.bytes || 0)],
        ["Re-indexed", (p.reindex || []).map((r) => this.repoLabel(r)).join(", ") || "None"],
        ["History lost", p.history_lost ? countOf(p.history_lost, "encrypted history file") : "None"],
        ["Kopia repositories", "Not touched"],
      ];
    },
    setAsideText() {
      const list = (this.encStatus && this.encStatus.set_aside) || [];
      if (!list.length) return "";
      const bytes = list.reduce((n, x) => n + (x.bytes || 0), 0);
      return `${fmtBytes(bytes)} of old encrypted files in ${list.map((x) => x.path).join(", ")}. Delete ${list.length > 1 ? "these folders" : "the folder"} when you no longer need ${list.length > 1 ? "them" : "it"}.`;
    },

    // ---- The unlock box ----------------------------------------------------------------------
    async showLock(detail) {
      if (this.locked) return;
      this.locked = { repos: (detail && detail.repos) || [], mode: "password", pw: "", key: "", remember: false,
                      error: "", busy: false, shared: false, signedIn: "", remembered: true };
      try {
        const [a, e] = await Promise.all([getJson("/api/account"), getJson("/api/encryption/status")]);
        Object.assign(this.locked, {
          shared: !!a.unlocks_encryption, signedIn: a.sign_in_on && a.username ? a.username : "",
          remembered: !!e.remembered,
        });
      } catch (err) { /* the box still works without the wording details */ }
    },
    lockIntro() {
      const l = this.locked;
      if (!l) return "";
      if (l.mode === "recovery") return "Enter the recovery key you saved when encryption was set up.";
      const who = l.signedIn ? `Signed in as ${l.signedIn}. ` : "";
      return who + (l.shared ? "The index is locked. Enter your KopiaLens password." : "The index is locked. Enter the encryption password.");
    },
    lockToggleMode() {
      this.locked.mode = this.locked.mode === "password" ? "recovery" : "password";
      this.locked.error = "";
    },
    async unlockSubmit() {
      const l = this.locked;
      if (!l || l.busy) return;
      const recovery = l.mode === "recovery";
      if (!(recovery ? l.key.trim() : l.pw)) { l.error = recovery ? "Enter the recovery key." : "Enter the password."; return; }
      l.busy = true; l.error = "";
      try {
        if (recovery) await _acctPost("/api/encryption/unlock-recovery", { recovery_key: l.key.trim() });
        else await _acctPost("/api/encryption/unlock", { password: l.pw });
        if (l.remember) await _acctPost("/api/encryption/remember", { on: true }).catch(() => {});
        location.reload();
      } catch (e) {
        const msg = e.message || String(e);
        l.error = msg === "Wrong password." && recovery ? "That recovery key didn't work." : msg;
        l.busy = false;
      }
    },
    async lockSignOut() {
      await _acctPost("/api/auth/logout").catch(() => {});
      location.href = appUrl("/login");
    },
};
