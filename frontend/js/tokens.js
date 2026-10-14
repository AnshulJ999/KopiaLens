// API tokens (Settings > API tokens): create, show once, list, edit and revoke; approve or deny what
// a token asks to do. The server decides what a token may do; this screen only manages them.
const TOKEN_EXPIRY = { 30: "30 days", 90: "90 days", 180: "6 months", 365: "1 year", 0: "Never" };
const APPROVAL_STATUS = { waiting: "Waiting", approved: "Approved, not run yet", used: "Approved and run",
  denied: "Denied", expired: "Expired" };

const tokensPart = {
  async tokLoad() {
    try { this.tok = await getJson("/api/tokens"); } catch (e) { this.klNotify(e.message, "warn"); }
    this.apLoad();
  },
  tokExpiry(t) {
    if (!t.expires) return "Never";
    return (t.expired ? "Expired " : "") + this.fmtDate(t.expires);
  },
  async tokPassword(title, body, confirmLabel) {
    const r = await this.klConfirm({ title, body, input: { label: "KopiaLens password", type: "password" }, confirmLabel });
    return r && r.value ? r.value : null;
  },
  async tokCreate() {
    const f = this.tokForm, name = (f.name || "").trim();
    if (!name) { this.klNotify("Give the token a name", "warn"); return; }
    let password = null;
    if (f.mode !== "read_only" && this.tok && this.tok.sign_in) {
      password = await this.tokPassword(`Create a ${this.tok.labels[f.mode]} token?`,
        "This token can change things in KopiaLens.", "Create token");
      if (!password) return;
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
  tokStartEdit(t) {
    this.tokEdit = { id: t.id, name: t.name, mode: t.mode, days: "", was: t };
  },
  async tokSaveEdit() {
    const e = this.tokEdit, t = e.was, name = (e.name || "").trim();
    if (!name) { this.klNotify("Give the token a name", "warn"); return; }
    const body = {};
    if (name !== t.name) body.name = name;
    if (e.mode !== t.mode) body.mode = e.mode;
    if (e.days !== "") body.days = Number(e.days);
    if (!Object.keys(body).length) { this.tokEdit = null; return; }
    const modes = this.tok.modes;
    const raises = body.mode && modes.indexOf(body.mode) > modes.indexOf(t.mode);
    const extends_ = body.days !== undefined && t.expires
      && (body.days === 0 || Date.now() / 1000 + body.days * 86400 > t.expires + 3600);
    if ((raises || extends_) && this.tok.sign_in) {
      body.password = await this.tokPassword(`Save changes to "${t.name}"?`,
        raises ? `The token becomes ${this.tok.labels[body.mode]}.` : "The token stays valid for longer.", "Save changes");
      if (!body.password) return;
    }
    this.tokBusy = true;
    try {
      this.tok = await getJson(`/api/tokens/${encodeURIComponent(t.id)}`, { method: "PATCH",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      this.tokEdit = null;
      this.klNotify("Token saved");
    } catch (err) {
      this.klNotify(err.message, "warn");
    } finally {
      this.tokBusy = false;
    }
  },
  async tokRevoke(t) {
    const ok = await this.klConfirm({ title: `Revoke "${t.name}"?`, body: "Anything using this token stops working straight away.",
      confirmLabel: "Revoke token", danger: true });
    if (!ok) return;
    try {
      this.tok = await getJson(`/api/tokens/${encodeURIComponent(t.id)}`, { method: "DELETE" });
      if (this.tokNew && this.tokNew.name === t.name) this.tokNew = null;
      if (this.tokEdit && this.tokEdit.id === t.id) this.tokEdit = null;
      this.klNotify("Token revoked");
    } catch (e) {
      this.klNotify(e.message, "warn");
    }
  },
  // ---- approvals
  async apLoad() {
    try {
      const d = await getJson("/api/approvals");
      this.apList = d.approvals || [];
      this.apWaiting = d.waiting || 0;
    } catch (e) { /* the list reloads with the next status poll */ }
  },
  apOpen() {
    this.openSettings();
    this.settingsTab = "tokens";
  },
  apWhen(a) {
    if (a.status !== "waiting") return a.decided_by ? `${APPROVAL_STATUS[a.status] || a.status} by ${a.decided_by}` : (APPROVAL_STATUS[a.status] || a.status);
    const min = Math.max(0, Math.round((a.expires * 1000 - Date.now()) / 60000));
    return `Asked ${fmtAbs(a.created)} · expires in ${min} min`;
  },
  async apDecide(a, approve) {
    let password = null;
    if (approve) {
      const opts = { title: `Approve: ${a.label}?`, body: `Asked by the API token "${a.token_name}". ${a.detail || ""}`.trim(),
        confirmLabel: "Approve", danger: a.label.startsWith("Remove") || a.label.startsWith("Delete") };
      if (a.needs_password && this.tok && this.tok.sign_in) opts.input = { label: "KopiaLens password", type: "password" };
      const r = await this.klConfirm(opts);
      if (!r || (opts.input && !r.value)) return;
      password = opts.input ? r.value : null;
    }
    try {
      const d = await getJson(`/api/approvals/${encodeURIComponent(a.id)}/${approve ? "approve" : "deny"}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) });
      this.apList = d.approvals || [];
      this.apWaiting = d.waiting || 0;
      this.klNotify(approve ? "Approved. The token can run it now." : "Denied");
    } catch (e) {
      this.klNotify(e.message, "warn");
      this.apLoad();
    }
  },
};
