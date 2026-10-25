// Write modes: what KopiaLens may change in a repository (Settings > Permissions). The server refuses
// what a mode doesn't allow; these helpers grey direct actions. Panels open in every mode, since
// they show what an action would do; only their final button is off, with the reason beside it.
const WM_ORDER = ["read_only", "restores", "non_destructive", "full"];

const writeModePart = {
  async wmLoad() {
    try { this.wm = await getJson("/api/write-mode"); } catch (e) { /* keep the last known state */ }
  },
  wmMode(repo) {
    const w = this.wm, r = repo || this.repo;
    if (!w) return "full";
    return (w.repos[r] && w.repos[r].mode) || w.global || "full";
  },
  wmLabel(mode) { return (this.wm && this.wm.labels[mode]) || mode; },
  // kopia itself holds this repository's connection read-only (Settings > Repositories).
  wmKopiaReadonly(repo) {
    const w = this.wm, r = repo || this.repo;
    return !!(w && w.repos[r] && w.repos[r].kopia_readonly);
  },
  // Why ``need`` is refused on this repository, or "" when it is allowed.
  wmOff(need, repo) {
    if (!need || this.repoKind !== "kopia") return "";
    const mode = this.wmMode(repo);
    if (WM_ORDER.indexOf(mode) >= WM_ORDER.indexOf(need)) return "";
    if (this.wmKopiaReadonly(repo)) return "Connected read-only, so kopia refuses changes.";
    return `${this.wmLabel(mode)} mode. This needs ${this.wmLabel(need)}.`;
  },
  // A menu entry greyed out with the reason when the mode refuses it.
  wmItem(item, need) {
    const off = this.wmOff(need);
    return off ? this.offMenuItem(item.label.replace(/<[^>]*>/g, ""), off) : item;
  },
  // Why ``need`` is refused, with where to change it; "" when it is allowed.
  wmWhy(need, repo) {
    const off = this.wmOff(need, repo);
    if (!off) return "";
    return `${off} Change it in ${this.wmKopiaReadonly(repo) ? "Settings > Repositories" : "Settings > Permissions"}.`;
  },
  // Call at the start of a write action: false (with a notice) when the mode refuses it.
  wmGuard(need, repo) {
    const why = this.wmWhy(need, repo);
    if (!why) return true;
    this.klNotify(why, "warn");
    return false;
  },
  async wmSet(repo, mode) {
    const w = this.wm || {};
    const before = repo ? this.wmMode(repo) : w.global;
    const after = repo ? (mode || w.global) : mode;
    let password = null;
    if (after === "full" && before !== "full" && w.password) {
      const r = await this.klConfirm({ title: "Allow Full Access?", body: "Full Access allows deleting files and snapshots.",
        input: { label: "KopiaLens password", type: "password" }, confirmLabel: "Allow Full Access" });
      if (!r || !r.value) { await this.wmLoad(); return; }
      password = r.value;
    }
    try {
      this.wm = await getJson("/api/write-mode", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: repo || null, mode: mode || null, password }) });
      this.klNotify("Permissions changed");
      this.loadEngine && this.loadEngine();
    } catch (e) {
      this.klNotify(e.message, "warn");
      await this.wmLoad();
    }
  },
};
