// API tokens (Settings > API tokens): create, show once, list and revoke. The server decides what a
// token may do; this screen only manages them.
const TOKEN_EXPIRY = { 30: "30 days", 90: "90 days", 180: "6 months", 365: "1 year", 0: "Never" };

const tokensPart = {
  async tokLoad() {
    try { this.tok = await getJson("/api/tokens"); } catch (e) { this.klNotify(e.message, "warn"); }
  },
  tokExpiry(t) {
    if (!t.expires) return "Never";
    return (t.expired ? "Expired " : "") + this.fmtDate(t.expires);
  },
  async tokCreate() {
    const f = this.tokForm, name = (f.name || "").trim();
    if (!name) { this.klNotify("Give the token a name", "warn"); return; }
    let password = null;
    if (f.mode !== "read_only" && this.tok && this.tok.sign_in) {
      const r = await this.klConfirm({ title: `Create a ${this.tok.labels[f.mode]} token?`,
        body: "This token can change things in KopiaLens.",
        input: { label: "KopiaLens password", type: "password" }, confirmLabel: "Create token" });
      if (!r || !r.value) return;
      password = r.value;
    }
    this.tokBusy = true;
    try {
      const out = await getJson("/api/tokens", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, mode: f.mode, days: Number(f.days), password }) });
      this.tok = out;
      this.tokNew = { name: out.created.name, secret: out.secret };
      this.tokForm = { ...this.tokForm, name: "" };
    } catch (e) {
      this.klNotify(e.message, "warn");
    } finally {
      this.tokBusy = false;
    }
  },
  tokCopy() {
    const s = this.tokNew && this.tokNew.secret;
    if (s && navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(s).then(() => this.klNotify("Token copied"), () => this.klNotify("Couldn't copy. Select the token and copy it.", "warn"));
    } else {
      this.klNotify("This browser blocks copying here. Select the token and copy it.", "warn");
    }
  },
  async tokRevoke(t) {
    const ok = await this.klConfirm({ title: `Revoke "${t.name}"?`, body: "Anything using this token stops working straight away.",
      confirmLabel: "Revoke token", danger: true });
    if (!ok) return;
    try {
      this.tok = await getJson(`/api/tokens/${encodeURIComponent(t.id)}`, { method: "DELETE" });
      if (this.tokNew && this.tokNew.name === t.name) this.tokNew = null;
      this.klNotify("Token revoked");
    } catch (e) {
      this.klNotify(e.message, "warn");
    }
  },
};
