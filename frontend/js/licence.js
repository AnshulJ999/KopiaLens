// Free/Pro in the UI: which Pro features are locked (GET /api/licence), the "PRO" tag and the
// notice a locked button opens, and Settings -> Licence. The server decides; this only marks
// buttons. Demo repos unlock everything (their actions are simulated).
//
//   GET    /api/licence                 -> {state, plan, name, email, expires, renewable, stored, origin,
//                                           patreon?, locked: [feature], features: {feature: line}, pricing_url}
//   POST   /api/licence {text}          paste or file content
//   POST   /api/licence/activate {key}  store key -> licence (online, once)
//   POST   /api/licence/renew           renew what is due now (renew_due); renew_error says why one didn't
//   POST   /api/licence/patreon/start   -> {url, session};  POST /api/licence/patreon/claim {session}
//   DELETE /api/licence[?id=]             the licence in use, or another stored one (lic.others)
const LIC_PATREON_POLL_MS = 2500;
const LIC_PATREON_TIMEOUT_MS = 10 * 60 * 1000;
const LIC_REFRESH_MS = 60 * 1000;
// PRO tags on demo repos: the feature works there (simulated), the tag says it is paid. false hides them.
const PRO_TAGS_ON_DEMO = true;

const licencePart = {

    async loadLicence() {
      this._licAt = Date.now();
      try { this.lic = await getJson("/api/licence"); } catch (e) { /* keep last-known state */ }
    },
    // A window stays open for days: re-read on focus (at most once a minute) and after a Pro refusal.
    refreshLicence(force) {
      if (force || Date.now() - (this._licAt || 0) > LIC_REFRESH_MS) this.loadLicence();
    },

    proOk(feature) {
      return !((this.lic && this.lic.locked) || []).includes(feature) || this.isDemo();
    },

    // The PRO mark: shown for a feature this licence doesn't include, demo repos too (see PRO_TAGS_ON_DEMO).
    proTag(feature) {
      const locked = ((this.lic && this.lic.locked) || []).includes(feature);
      return locked && (PRO_TAGS_ON_DEMO || !this.isDemo());
    },
    proTagTitle() { return this.isDemo() ? "Pro feature. Free to try in the demo." : null; },

    // Call first in every Pro action: true = go ahead; false = the notice opened by the button.
    proGate(feature, ev) {
      if (this.proOk(feature)) return true;
      this.openProNotice(feature, ev && (ev.currentTarget || ev.target));
      return false;
    },

    openProNotice(feature, el) {
      const width = 300, gap = 8;
      let top = 120, left = Math.max(16, (window.innerWidth - width) / 2), below = true;   // no button: centred
      if (el && el.getBoundingClientRect) {
        const r = el.getBoundingClientRect();
        left = Math.max(16, Math.min(r.right - width, window.innerWidth - width - 16));
        below = r.top < 170;
        top = below ? r.bottom + gap : r.top - gap;
      }
      this.proNotice = { feature, line: (this.lic.features || {})[feature] || "This needs Pro.", top, left, below };
      this.$nextTick(() => { const b = document.getElementById("kl-pro-plans"); if (b) b.focus(); });
    },
    closeProNotice() { this.proNotice = null; },
    proNoticeTitle() {
      if (this.lic.state === "expired" && this.lic.expires) return `Your Pro licence ended on ${fmtDate(this.lic.expires)}`;
      return "Part of KopiaLens Pro";
    },
    proSeePlans() {
      this.proNotice = null;
      window.open(this.lic.pricing_url, "_blank", "noopener");
    },
    // An ended subscription or Patreon licence renews from Settings; buying again is for the rest.
    proRenewHere() { return this.lic.state === "expired" && this.lic.renewable; },
    proPrimary() {
      if (this.proRenewHere()) this.proEnterLicence(); else this.proSeePlans();
    },
    proEnterLicence() {
      this.proNotice = null;
      this.settingsTab = "licence";
      this.openSettings();
    },

    // ---- Settings -> Licence ----
    licExpiresText() {
      const l = this.lic;
      if (!l || !l.expires) return "Never";
      return fmtDate(l.expires);
    },
    licPillText() {
      return { none: "Free", active: "Active", expired: "Ended", invalid: "Not valid", banned: "Withdrawn",
               not_covered: "Older version" }[this.lic.state] || "Free";
    },
    licPillClass() {
      if (this.lic.state === "active") return "ok";
      return this.lic.state === "none" ? "" : "warn";
    },
    licProblem() {
      const l = this.lic;
      if (l.state === "expired") return `Pro ended on ${fmtDate(l.expires)}. Everything free still works.`;
      if (l.state === "invalid") return "This licence couldn't be verified. KopiaLens runs as Free.";
      if (l.state === "banned") return "This licence was withdrawn. KopiaLens runs as Free.";
      if (l.state === "not_covered") return "This licence covers an older version of KopiaLens. It runs as Free here.";
      return "";
    },
    licOriginText() {
      const o = this.lic.origin || "";
      const renew = this.lic.renewable ? " Renewed copies are kept in KopiaLens's data folder." : "";
      if (o.startsWith("env ")) return `Set by ${o.slice(4)}. Change it there.` + renew;
      if (o === "shared folder") return "From the shared licence folder (.licensekit in your home folder)." + renew;
      return "";
    },
    licProgress() {
      const p = this.lic && this.lic.patreon;
      if (!p || !p.threshold_cents) return null;
      const pct = Math.min(100, Math.round((p.support_cents / p.threshold_cents) * 100));
      return { pct, text: `$${Math.floor(p.support_cents / 100)} of $${Math.floor(p.threshold_cents / 100)}` };
    },

    async _licCall(url, opts, done) {
      this.licBusy = true; this.licError = "";
      try {
        this.lic = await getJson(url, opts);
        this.licAdding = false;
        if (done) this.klNotify(done);
        return true;
      } catch (e) {
        this.licError = e.message || String(e);
        return false;
      } finally {
        this.licBusy = false;
      }
    },
    async licActivate() {
      if (!this.licKey.trim()) { this.licError = "Enter your licence key."; return; }
      if (await this._licCall("/api/licence/activate", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: this.licKey }) }, "Pro is active.")) this.licKey = "";
    },
    async licRenew() {
      await this._licCall("/api/licence/renew", { method: "POST" }, "Licence renewed.");
    },
    licPickFile() { const el = document.getElementById("kl-lic-file"); if (el) { el.value = ""; el.click(); } },
    async licImportFile(ev) {
      const file = ev.target.files && ev.target.files[0];
      if (!file) return;
      const text = await file.text().catch(() => "");
      await this._licCall("/api/licence", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }) }, "Pro is active.");
    },
    async licPatreon() {
      this.licBusy = true; this.licError = "";
      let start;
      try {
        start = await getJson("/api/licence/patreon/start", { method: "POST" });
      } catch (e) {
        this.licError = e.message || String(e); this.licBusy = false; return;
      }
      window.open(start.url, "_blank", "noopener");
      this.licPatreonWaiting = true;
      const until = Date.now() + LIC_PATREON_TIMEOUT_MS;
      while (this.licPatreonWaiting && Date.now() < until) {
        await new Promise((r) => setTimeout(r, LIC_PATREON_POLL_MS));
        if (!this.licPatreonWaiting) break;
        try {
          const r = await getJson("/api/licence/patreon/claim", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session: start.session }) });
          if (!r.pending) { this.lic = r; this.licAdding = false; this.klNotify("Pro is active."); break; }
        } catch (e) {
          this.licError = e.message || String(e); break;
        }
      }
      if (this.licPatreonWaiting && Date.now() >= until) this.licError = "The Patreon sign-in timed out. Try again.";
      this.licPatreonWaiting = false; this.licBusy = false;
    },
    licPatreonCancel() { this.licPatreonWaiting = false; },
    async licRemove() {
      const next = (this.lic.others || []).find((o) => o.state === "active");
      const ok = await this.klConfirm({
        title: "Remove the licence from this computer?",
        facts: [["Plan", this.lic.plan || ""], ["Licensed to", this.lic.name || ""]],
        note: next ? `Your ${next.plan} licence takes over.` : "KopiaLens runs as Free until you add it again.",
        confirmLabel: "Remove licence", danger: true,
      });
      if (ok) await this._licCall("/api/licence", { method: "DELETE" }, "Licence removed.");
    },
    licOtherWhen(o) {
      return { active: o.expires ? `Ends ${fmtDate(o.expires)}` : "Never ends",
               expired: `Ended ${fmtDate(o.expires)}`, banned: "Withdrawn", invalid: "Not valid",
               not_covered: "Older version" }[o.state] || o.state;
    },
    licOtherText(o) { const w = this.licOtherWhen(o); return `${o.plan || "Licence"} · ${w[0].toLowerCase()}${w.slice(1)}`; },
    async licRemoveOther(o) {
      const ok = await this.klConfirm({
        title: "Remove this licence from this computer?",
        facts: [["Plan", o.plan || ""], ["Status", this.licOtherWhen(o)]],
        confirmLabel: "Remove licence", danger: true,
      });
      if (ok) await this._licCall(`/api/licence?id=${encodeURIComponent(o.id)}`, { method: "DELETE" }, "Licence removed.");
    },
};
