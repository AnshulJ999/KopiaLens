// Help menu, update notice, usage-stats settings and the diagnostics bundle.
const helpPart = {
    helpMenu: false, help: null, showUpdate: false, usagePreview: null, checkingUpdates: false,
    updateCheck: true, usageStats: true, updateChannel: "stable",
    zoomLevel: window.klZoom ? window.klZoom.level() : null,
    showDiag: false, diag: null, _diagSeq: 0,

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
    helpLink(which) {
      this.helpMenu = false;
      const url = this.help && this.help.links && this.help.links[which];
      if (url) window.open(url, "_blank", "noopener");
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
    diagIssueUrl() {
      const h = this.help || {};
      const base = h.links && h.links.repo ? h.links.repo.replace(/\/$/, "") : "";
      if (!base) return "";
      const p = new URLSearchParams({ template: "bug_report.yml", version: h.version || "",
                                      install: h.install_type || "", bundle: (this.diag && this.diag.id) || "" });
      return `${base}/issues/new?${p.toString()}`;
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
      window.open(`mailto:${(h.links && h.links.support_email) || ""}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`, "_blank");
    },
};
