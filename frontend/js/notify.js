// Settings > Notifications: KopiaLens's own webhook, and kopia's notification profiles (stored in
// each repository, shared with KopiaUI and the command line).
const NOTIFY_FIELDS = {
  email: [["smtpServer", "SMTP server", "smtp.example.com"], ["smtpPort", "SMTP port", "587"], ["smtpUsername", "Username", ""],
    ["smtpPassword", "Password", ""], ["from", "From", "backups@example.com"], ["to", "To", "me@example.com"], ["cc", "Cc", ""]],
  pushover: [["appToken", "App token", ""], ["userKey", "User key", ""]],
  webhook: [["endpoint", "Address", "https://ntfy.sh/my-backups"]],
};
const NOTIFY_SECRET = ["smtpPassword", "appToken", "userKey"];

const notifyPart = {
  notifyInit() {
    const w = (this._klSettings && this._klSettings.notify_webhook) || {};
    this.notifyHook = { url: w.url || "", format: w.format === "json" ? "json" : "text", level: w.level || "warn",
      headers: (w.headers || []).join("\n"), busy: false };
    if (!this.notifyRepo || !this.repos.some((r) => r.id === this.notifyRepo && r.kind === "kopia")) {
      const k = this.repos.find((r) => r.id === this.repo && r.kind === "kopia") || this.repos.find((r) => r.kind === "kopia");
      this.notifyRepo = k ? k.id : "";
    }
    this.notifyLoad();
  },
  notifyRepos() { return (this.repos || []).filter((r) => r.kind === "kopia" && !r.demo); },
  async notifyHookSave() {
    const h = this.notifyHook;
    const val = { url: h.url.trim(), format: h.format, level: h.level, headers: h.headers.split("\n").map((s) => s.trim()).filter(Boolean) };
    try {
      await getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kopialens: { notify_webhook: val } }) });
      if (this._klSettings) this._klSettings.notify_webhook = val;
      this.klNotify("Saved");
    } catch (e) { this.klNotify(e.message, "error"); }
  },
  async notifyHookTest() {
    await this.notifyHookSave();
    this.notifyHook.busy = true;
    try {
      await getJson("/api/notify/webhook/test", { method: "POST" });
      this.klNotify("Test message sent");
    } catch (e) { this.klNotify(e.message, "error"); }
    this.notifyHook.busy = false;
  },

  // ---- kopia's profiles ----
  async notifyLoad() {
    if (!this.notifyRepo) { this.notifyProfiles = null; return; }
    const repo = this.notifyRepo;
    this.notifyProfiles = { loading: true, list: [], error: "" };
    try {
      const d = await getJson(`/api/notify/profiles?repo=${encodeURIComponent(repo)}`);
      if (this.notifyRepo === repo) this.notifyProfiles = { loading: false, list: d.profiles || [], error: "" };
    } catch (e) { if (this.notifyRepo === repo) this.notifyProfiles = { loading: false, list: [], error: e.message }; }
  },
  notifySeverityName(n) { return { "-100": "Everything", "-10": "Successes and up", 0: "Reports and up", 10: "Warnings and errors", 20: "Errors only" }[String(n)] || String(n); },
  notifySummary(p) {
    const c = p.config || {};
    if (p.type === "email") return c.to || "";
    if (p.type === "webhook") return c.endpoint || "";
    if (p.type === "pushover") return "Pushover";
    return "";
  },
  notifyFields(type) { return NOTIFY_FIELDS[type] || []; },
  notifySecret(key) { return NOTIFY_SECRET.includes(key); },
  notifyEdit(p) {
    const sev = { "-100": "verbose", "-10": "success", 0: "report", 10: "warning", 20: "error" };
    this.notifyForm = p
      ? { isNew: false, name: p.name, type: p.type, min_severity: sev[String(p.min_severity)] || "report",
          fields: Object.fromEntries(this.notifyFields(p.type).map(([k]) => [k, NOTIFY_SECRET.includes(k) ? "" : (p.config[k] ?? "")])),
          secretSet: Object.fromEntries(NOTIFY_SECRET.map((k) => [k, !!p.config[k + "Set"]])),
          format: p.config.format || "", method: p.config.method || "POST", headers: (p.config.headers || "").split("\n").filter(Boolean).join("\n"),
          busy: false, error: "" }
      : { isNew: true, name: "", type: "webhook", min_severity: "warning", fields: {}, secretSet: {}, format: "", method: "POST", headers: "", busy: false, error: "" };
  },
  async notifySave(test) {
    const f = this.notifyForm;
    if (!f || f.busy) return;
    f.busy = true; f.error = "";
    const fields = { ...f.fields };
    if (f.format) fields.format = f.format;
    if (f.type === "webhook") fields.method = f.method;
    try {
      await getJson("/api/notify/profile", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: this.notifyRepo, name: f.name, type: f.type, min_severity: f.min_severity, fields,
          headers: f.type === "webhook" ? f.headers.split("\n") : [], test: !!test }) });
      this.notifyForm = null;
      this.klNotify(test ? "Saved, and a test message was sent" : "Profile saved");
      this.notifyLoad();
    } catch (e) { f.error = e.message; }
    if (this.notifyForm) f.busy = false;
  },
  async notifyTest(p) {
    try {
      await getJson("/api/notify/profile/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.notifyRepo, name: p.name }) });
      this.klNotify("Test message sent");
    } catch (e) { this.klNotify(e.message, "error"); }
  },
  async notifyDelete(p) {
    const ok = await this.klConfirm({ title: `Delete the profile ${p.name}?`, facts: [["Repository", this.repoName(this.notifyRepo)], ["Sends to", this.notifySummary(p) || p.type]],
      note: "KopiaUI and the kopia command line use this profile too.", confirmLabel: "Delete profile", danger: true });
    if (!ok) return;
    try {
      await getJson("/api/notify/profile/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.notifyRepo, name: p.name }) });
      this.klNotify("Profile deleted");
      this.notifyLoad();
    } catch (e) { this.klNotify(e.message, "error"); }
  },
};
