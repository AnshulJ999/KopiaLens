// Help menu, update notice, usage-stats settings and the diagnostics bundle.
const helpPart = {
    helpMenu: false, help: null, showUpdate: false, usagePreview: null, checkingUpdates: false,
    updateCheck: true, usageStats: true, updateChannel: "stable",
    zoomLevel: window.klZoom ? window.klZoom.level() : null,
    showDiag: false, diag: null, _diagSeq: 0, showReport: false, report: null, showFeedback: false, fbk: null,

    async loadHelp() {
      try { this.help = await getJson("/api/help/info"); } catch (e) { /* the menu still opens; links wait */ }
    },
    initHelp() {
      window.addEventListener("kl-zoom", (e) => { this.zoomLevel = e.detail; });
      // The desktop app reports finished downloads; a browser shows its own download bar.
      window.addEventListener("kl-downloaded", (e) => {
        const d = e.detail || {};
        if (!d.ok) { this.klNotify(`Couldn't save ${d.name || "the download"}`, "warn"); return; }
        this.klNotify(`Saved ${d.name} to ${d.folder}`, "ok",
          { label: "Show in folder", run: () => { location.href = appUrl("/?kl-reveal-download=1"); } });
      });
      setTimeout(() => this.loadHelp(), 5000);   // after the first screen; the check itself runs server-side
    },
    toggleHelpMenu() {
      this.helpMenu = !this.helpMenu;
      if (this.helpMenu) { this.loadHelp(); if (!this.acct) this.loadAccount(); }
    },
    isDesktopWindow() { return !!window.klZoom; },
    zoomStep(dir) { if (window.klZoom) window.klZoom.step(dir); },
    zoomReset() { if (window.klZoom) window.klZoom.reset(); },
    openInBrowser() {
      this.helpMenu = false;
      window.open(location.origin + appUrl("/?open-in-browser=1"), "_blank");
    },
    canOpenInBrowser() { return this.isDesktopWindow() && !(this.help && this.help.access === "window"); },

    // ---- Updates ----
    update() { return (this.help && this.help.update) || {}; },
    async checkUpdatesNow() {
      if (this.checkingUpdates) return;
      this.checkingUpdates = true;
      try {
        const r = await getJson("/api/help/check-updates", { method: "POST" });
        if (this.help) this.help.update = r.update;
        else await this.loadHelp();
        if (!r.ok) this.klNotify("Couldn't check for updates. Try again later.", "warn");
        else if (r.update.available) this.showUpdate = true;
        else this.klNotify(`You have the latest version (${r.update.version}).`);
      } catch (e) {
        this.klNotify("Couldn't check for updates. Try again later.", "warn");
      } finally {
        this.checkingUpdates = false;
      }
    },
    updateDownload() {
      const u = this.update();
      if (u.url) window.open(u.url, "_blank", "noopener");
      this.showUpdate = false;
    },
    // The desktop app installs it (after indexing and jobs finish); this follows its progress.
    async installUpdate() {
      try {
        await getJson("/api/help/install-update", { method: "POST" });
      } catch (e) {
        this.klNotify(e.message || "Couldn't start the update.", "warn");
        return;
      }
      if (this.help) this.help.update = { ...this.update(), install: { state: "checking" } };
      clearInterval(this._updTimer);
      this._updTimer = setInterval(async () => {
        await this.loadHelp();
        const st = (this.update().install || {}).state;
        if (st === "failed" || st === "none") clearInterval(this._updTimer);
      }, 2000);
    },
    updateProgress() {
      const i = this.update().install;
      if (!i) return "";
      return {
        checking: "Preparing…",
        waiting: "Installs when indexing and running jobs finish.",
        downloading: i.pct != null ? `Downloading… ${i.pct}%` : "Downloading…",
        installing: "Installing. KopiaLens restarts when it's done.",
        none: "This update isn't ready to install yet. Try again later, or download it.",
        failed: "Couldn't install the update. Try again, or download it.",
      }[i.state] || "";
    },
    updateInstalling() { return ["checking", "waiting", "downloading", "installing"].includes((this.update().install || {}).state); },
    async loadUsagePreview() {
      try { this.usagePreview = await getJson("/api/help/usage-preview"); } catch (e) { this.usagePreview = { error: true }; }
    },

    // ---- Diagnostics bundle ----
    openDiagnostics(report = false) {
      this.helpMenu = false;
      this.diag = { report, anonymise: true, loading: false, error: "", id: "", files: null, file: "summary.txt", days: 7,
                    sections: { problems: true, app_log: true, index_logs: true, history: true, settings: true, kopia_status: true } };
      this.showDiag = true;
      this.diagPreview();
    },
    _diagQuery() {
      const d = this.diag;
      const sections = Object.keys(d.sections).filter((k) => d.sections[k]).join(",");
      return new URLSearchParams({ sections, anonymise: d.anonymise ? "true" : "false", days: String(d.days || 7) });
    },
    async diagPreview() {
      const seq = ++this._diagSeq;
      this.diag.loading = true;
      this.diag.error = "";
      try {
        const r = await getJson(`/api/diagnostics/preview?${this._diagQuery().toString()}`);
        if (seq !== this._diagSeq || !this.diag) return;
        this.diag.id = r.id;
        this.diag.files = r.files;
        if (!(this.diag.file in r.files)) this.diag.file = "summary.txt";
      } catch (e) {
        if (seq === this._diagSeq && this.diag) this.diag.error = e.message || String(e);
      } finally {
        if (seq === this._diagSeq && this.diag) this.diag.loading = false;
      }
    },
    diagFileNames() { return this.diag && this.diag.files ? Object.keys(this.diag.files) : []; },
    diagDownload() {
      const p = this._diagQuery();
      p.set("id", this.diag.id);
      window.open(appUrl(`/api/diagnostics/download?${p.toString()}`), "_blank");
    },
    // A new issue from the bug report form, its fields filled in (field ids: .github/ISSUE_TEMPLATE/bug_report.yml).
    _issueUrl(fields) {
      const h = this.help || {};
      const base = h.links && h.links.repo ? h.links.repo.replace(/\/$/, "") : "";
      if (!base) return "";
      const p = new URLSearchParams({ template: "bug_report.yml", version: h.version || "", ...fields });
      return `${base}/issues/new?${p.toString()}`;
    },
    diagIssueUrl() {
      const id = (this.diag && this.diag.id) || "";
      return this._issueUrl(id ? { logs: `Diagnostics bundle ${id} (attached).` } : {});
    },
    diagOpenIssue() {
      const url = this.diagIssueUrl();
      if (url) window.open(url, "_blank", "noopener");
    },
    diagEmail() {
      const h = this.help || {};
      const id = (this.diag && this.diag.id) || "";
      const subject = `KopiaLens problem (bundle ${id})`;
      const body = `What happened:\n\nWhat I expected:\n\nKopiaLens ${h.version || ""}, ${h.install_type || ""}. Diagnostics bundle ${id} is attached.`;
      this._openMail(`mailto:${(h.links && h.links.support_email) || ""}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`);
      this.diag.mailed = true;
    },
    // In place, not a new tab: a mail link in a new tab leaves a blank tab behind. The desktop app
    // hands it to the mail app.
    _openMail(url) { location.href = url; },

    // ---- Report a problem ----
    openReport() {
      this.helpMenu = false;
      if (!this.help) this.loadHelp();
      this.report = { what: "", expected: "", attach: true, busy: false, error: "", saved: "", openUrl: "", openLabel: "", mailTo: "" };
      this.showReport = true;
      this._focusIn("[data-testid=report-dialog] textarea");
    },
    // "How it works" in Report a problem: [text, done]; ticks fill in once the file is saved.
    reportSteps() {
      const r = this.report || {};
      const done = !!r.saved;
      return [
        ...(r.attach ? [["KopiaLens saves the diagnostics bundle to Downloads.", done]] : []),
        ["A new GitHub issue opens with your text. It needs a free GitHub account.", done && !r.mailTo],
        ...(r.attach ? [["Drag the bundle into the issue, then submit it.", false]] : []),
      ];
    },
    // The first box of a dialog that just opened; it is drawn after the state change.
    _focusIn(sel) {
      setTimeout(() => { const el = document.querySelector(sel); if (el) el.focus(); }, 50);
    },
    // Saves the diagnostics file (names hidden, every section), then opens the issue or email with
    // the text filled in. A browser that blocks the new window gets a link instead.
    async reportSend(kind) {
      const r = this.report;
      if (!r || r.busy) return;
      if (!r.what.trim()) { r.error = "Describe what happened."; return; }
      r.busy = true; r.error = ""; r.saved = ""; r.openUrl = "";
      try {
        if (!this.help) await this.loadHelp();
        let id = "", file = "";
        if (r.attach) {
          const q = new URLSearchParams({ sections: "problems,app_log,index_logs,history,settings,kopia_status", anonymise: "true", days: "7" });
          const b = await getJson(`/api/diagnostics/preview?${q.toString()}`);
          id = b.id; file = b.file_name;
          q.set("id", id);
          const a = document.createElement("a");
          a.href = appUrl(`/api/diagnostics/download?${q.toString()}`);
          a.download = file;
          a.click();
        }
        const h = this.help || {};
        const what = r.what.trim() + (r.expected.trim() ? `\n\nWhat I expected:\n${r.expected.trim()}` : "");
        let url;
        if (kind === "email") {
          const body = `${what}\n\nKopiaLens ${h.version || ""}, ${h.install_type || ""}.${id ? ` Diagnostics file ${id} is attached.` : ""}`;
          url = `mailto:${(h.links && h.links.support_email) || ""}?subject=${encodeURIComponent("KopiaLens problem")}&body=${encodeURIComponent(body)}`;
        } else {
          url = this._issueUrl({ title: r.what.trim().split("\n")[0].slice(0, 80), what, ...(id ? { logs: `Diagnostics file ${id} (attached).` } : {}) });
        }
        const where = kind === "email" ? "the email" : "the issue";
        r.saved = file ? `Saved ${file} to Downloads. ${kind === "email" ? "Attach it to the email." : "Drag it into the issue."}` : `Opened ${where}.`;
        if (kind === "email") {
          this._openMail(url);
          r.mailTo = (h.links && h.links.support_email) || "";
        } else {
          const w = window.open(url, "_blank");   // "noopener" would always return null
          if (w) w.opener = null;
          else { r.openUrl = url; r.openLabel = "Open the issue"; }
        }
      } catch (e) {
        r.error = e.message || String(e);
      } finally {
        r.busy = false;
      }
    },

    // ---- Give feedback ----
    openFeedback() {
      this.helpMenu = false;
      if (!this.help) this.loadHelp();
      this.fbk = { message: "", rating: null, email: "", quote: true, busy: false, error: "", sent: false };
      this.showFeedback = true;
      this._focusIn("[data-testid=feedback-dialog] textarea");
    },
    async feedbackSend() {
      const f = this.fbk;
      if (!f || f.busy) return;
      if (!f.message.trim()) { f.error = "Write a message first."; return; }
      f.busy = true; f.error = "";
      try {
        await getJson("/api/help/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify({ message: f.message, rating: f.rating, email: f.email, quote: f.quote }) });
        f.sent = true;
      } catch (e) {
        f.error = e.message || String(e);
      } finally {
        f.busy = false;
      }
    },
};
