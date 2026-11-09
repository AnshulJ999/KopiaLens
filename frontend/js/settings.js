// Settings modal: every control in it saves itself on change (one key per save, never a full
// payload of browser-side defaults - that is what stamped values the user never chose).
const MEMORY_CHOICES = ["512MB", "1GB", "2GB", "3GB", "4GB", "6GB", "8GB"];
const THREAD_CHOICES = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64];
// Tray icon choices (the desktop app draws them from shell/src-tauri/icons/tray/). First = default.
const TRAY_ICONS = [
  ["k3g", "Disc"], ["k6s", "K"], ["k3a", "Ring"], ["tile", "Tile"], ["mono", "K in one colour"], ["q3", "Q with a glint"],
  ["q1", "Lens with a tail"], ["s3", "Ring with a slice out"], ["r4", "Bold lens"], ["slice", "Slice pulled out"], ["q", "Simple Q"],
];

// A duration setting shown as a free number plus a unit; the setting itself stays in seconds.
// Changing the unit converts the shown number, it never changes the stored value.
function klDur(prop, key) {
  return {
    n: 0, unit: "1",
    init() { this._show(true); this.$watch(prop, () => this._show(true)); },
    _show(pickUnit) {
      const v = Number(this[prop]) || 0;
      if (pickUnit) this.unit = v >= 3600 && v % 3600 === 0 ? "3600" : v >= 60 && v % 60 === 0 ? "60" : "1";
      this.n = Math.round((v / Number(this.unit)) * 100) / 100;
    },
    relabel() { this._show(false); },
    commit() {
      this[prop] = Math.round(Number(this.n || 0) * Number(this.unit));
      this.saveKl(key);
    },
  };
}

// The Settings section list: [group, [[section id, title], ...]].
const SETTINGS_NAV = [
  ["App", [["general", "General"], ["appearance", "Appearance"], ["desktop", "Desktop app"], ["notifications", "Notifications"]]],
  ["Data", [["repositories", "Repositories"], ["indexing", "Indexing"], ["diskscan", "Disk scanning"], ["backblaze", "Backblaze"]]],
  ["Access", [["account", "Account"], ["encryption", "Encryption"], ["remote", "Remote access"], ["writemode", "Permissions"], ["tokens", "API tokens"]]],
  ["More", [["licence", "Licence"], ["updates", "Updates"], ["backups", "Config"], ["advanced", "Advanced"], ["experimental", "Experimental"], ["about", "About"]]],
];
// Other words people search for a section by.
const SETTINGS_SYNONYMS = {
  general: "numbers dates time format language", appearance: "theme dark light colour color density font logo layout",
  desktop: "tray startup autostart window", notifications: "webhook ntfy alerts email",
  repositories: "repo connect kopia path", indexing: "index schedule memory threads refresh", diskscan: "disk drives scanner",
  account: "password sign in login logout user", encryption: "password key encrypt unlock", remote: "network lan https certificate address browser",
  writemode: "permissions read only write mode", tokens: "api key token", licence: "license pro key activate patreon",
  updates: "update beta channel version usage statistics", backups: "config backup export import restore settings file",
  advanced: "debug log timeout", about: "version help support",
};

// Built from the page's own markup before Alpine runs, so every setting (even one shown only in some
// cases) is searchable with no upkeep: each label in a section, with the help line under it.
function _settingsIndex() {
  const titles = Object.fromEntries(SETTINGS_NAV.flatMap((g) => g[1]));
  const norm = (t) => (t || "").replace(/\s+/g, " ").trim();
  const out = [];
  for (const sec of document.querySelectorAll('section[x-show^="settingsTab"]')) {
    const tab = (/settingsTab==='(\w+)'/.exec(sec.getAttribute("x-show")) || [])[1];
    if (!titles[tab]) continue;
    out.push({ tab, label: titles[tab], desc: "", section: true,
      hay: (titles[tab] + " " + (SETTINGS_SYNONYMS[tab] || "")).toLowerCase() });
    const seen = new Set([titles[tab].toLowerCase()]);
    const walk = (root) => {
      for (const el of root.querySelectorAll("*")) {
        if (el.tagName === "TEMPLATE") { if (!el.hasAttribute("x-for")) walk(el.content); continue; }
        if (!/^(SPAN|LABEL|DIV|H3|H4|LEGEND)$/.test(el.tagName) || el.closest("button, select, option, a, textarea, [x-text], [x-html]")) continue;
        const label = norm([...el.childNodes].filter((c) => c.nodeType === 3).map((c) => c.textContent).join(" "));
        if (label.length < 3 || label.length > 70 || /[.:]$/.test(label) || !/^[A-Za-z]/.test(label) || seen.has(label.toLowerCase())) continue;
        seen.add(label.toLowerCase());
        const p = _settingsRow(el) && [..._settingsRow(el).children].find((c) => c.tagName === "P");
        const desc = p ? norm(p.textContent) : "";
        out.push({ tab, label, desc, hay: (label + " " + desc).toLowerCase() });
      }
    };
    walk(sec);
  }
  return out;
}
// The block a setting's label belongs to: its row, or the row's wrapper when the help line sits under it.
function _settingsRow(el) {
  let box = el.parentElement;
  if (box && box.classList.contains("flex") && box.parentElement && box.parentElement.tagName !== "SECTION") box = box.parentElement;
  return box;
}
const SETTINGS_INDEX = _settingsIndex();

// Changed settings (x-kl-default on a label): [{key, label}] per Settings section, read from the page.
// A key may be "a,b" for a row that holds two settings.
function _klTabKeys() {
  const out = {};
  for (const sec of document.querySelectorAll('section[x-show^="settingsTab"]')) {
    const tab = (/settingsTab==='(\w+)'/.exec(sec.getAttribute("x-show")) || [])[1];
    if (!tab) continue;
    const walk = (root) => {
      for (const el of root.querySelectorAll("[x-kl-default], template")) {
        if (el.tagName === "TEMPLATE") { walk(el.content); continue; }
        const label = [...el.childNodes].filter((c) => c.nodeType === 3).map((c) => c.textContent).join(" ").replace(/\s+/g, " ").trim();
        (out[tab] = out[tab] || []).push({ key: el.getAttribute("x-kl-default").trim(), label });
      }
    };
    walk(sec);
  }
  return out;
}
const KL_TAB_KEYS = _klTabKeys();

// Settings search, in place: matching rows show with their own controls. These sections are small
// apps rather than lists of settings, so a match shows a line that opens them instead.
const KL_APP_TABS = new Set(["notifications", "repositories", "account", "encryption", "remote", "writemode", "tokens", "licence", "backups", "about"]);
// Every settings row (x-kl-row in the markup), read before Alpine runs: {id, tab, hay, keys}. Each
// section also gets its search heading, its place in section-list order, and a search-aware x-show.
function _klSearchSetup() {
  const titles = Object.fromEntries(SETTINGS_NAV.flatMap((g) => g[1]));
  const order = SETTINGS_NAV.flatMap((g) => g[1].map((s) => s[0]));
  const text = (el) => [...el.childNodes].map((c) => c.nodeType === 3 ? c.textContent
    : c.nodeType === 1 && !/^(SELECT|OPTION|BUTTON|TEXTAREA|INPUT|TEMPLATE|SCRIPT)$/.test(c.tagName) ? text(c) : "").join(" ");
  const rows = [];
  for (const sec of document.querySelectorAll('section[x-show^="settingsTab"]')) {
    const tab = (/settingsTab==='(\w+)'/.exec(sec.getAttribute("x-show")) || [])[1];
    if (!titles[tab]) continue;
    const app = KL_APP_TABS.has(tab);
    sec.dataset.klTab = tab;
    if (app) sec.dataset.klApp = "";
    sec.style.order = order.indexOf(tab);
    sec.setAttribute("x-show", `settingsSearching ? klSearchShows('${tab}') : (${sec.getAttribute("x-show")})`);
    const head = document.createElement("div");
    head.className = "kl-search-head";
    head.setAttribute("x-show", "settingsSearching");
    head.innerHTML = `<span class="font-medium shrink-0">${titles[tab]}</span>`
      + (app ? `<span class="min-w-0 truncate text-muted" x-text="klSearchLabels('${tab}')"></span>` : "")
      + `<button type="button" class="kl-btn quiet ml-auto shrink-0" @click="settingsGo('${tab}')">Open section ›</button>`;
    sec.prepend(head);
    const walk = (root) => {
      for (const el of root.querySelectorAll("[x-kl-row], template")) {
        if (el.tagName === "TEMPLATE") { if (!el.hasAttribute("x-for")) walk(el.content); continue; }
        el.dataset.klRow = rows.length;
        const keys = [...el.querySelectorAll("[x-kl-default]")].map((k) => k.getAttribute("x-kl-default").trim()).join(",");
        rows.push({ id: rows.length, tab, hay: text(el).replace(/\s+/g, " ").trim().toLowerCase(), keys });
      }
    };
    walk(sec);
  }
  return rows;
}
const KL_ROWS = _klSearchSetup();
let _klHits = null;   // the last search's matches (every row asks, so it runs once per query)
// Reset one at a time only: a port change needs a restart, and update checks and usage stats are
// the user's privacy choice.
const KL_RESET_SKIP = new Set(["port", "control_port", "update_check", "usage_stats"]);
// Settings saved by their own setter rather than saveKl: [state field, setter].
const KL_LOOK = {
  theme: ["theme", (vm, v) => vm.setTheme(v)],
  density: ["density", (vm, v) => vm.setDensity(v)],
  font: ["font", (vm, v) => vm.setFont(v)],
  file_icons: ["fileIcons", (vm, v) => vm.setFileIcons(v)],
  sort_arrows: ["sortArrows", (vm, v) => vm.setSortArrows(v)],
  table_align: ["tableAlign", (vm, v) => vm.setTableAlign(v)],
  panel_layout: ["panelLayout", (vm, v) => { vm.panelLayout = v; vm.klSet({ panel_layout: v }); }],
  docs_view: ["docsView", (vm, v) => { vm.docsView = v; vm.klSet({ docs_view: v }); }],
  policy_layout: ["polLayout", (vm, v) => { vm.polLayout = v; vm.klSet({ policy_layout: v }); }],
  policy_leave: ["polLeave", (vm, v) => { vm.polLeave = v; vm.klSet({ policy_leave: v }); }],
};
const KL_DURATIONS = new Set(["disk_scan_budget", "index_worker_timeout", "index_first_timeout", "index_wait_unreachable",
  "index_stall_timeout", "reindex_after_delete_delay", "index_retry_delay", "folder_prewarm_budget", "startup_delay"]);
// How a default reads where the control's own options can't say it (keyed by the value as text).
const KL_DEFAULT_TEXT = {
  header_search_only: { false: "Header and Search tab", true: "Header only" },
  show_pro_badge: { true: "Shown", false: "Hidden" },
  index_backup_dir: { "": "Off" },
  es_path: { "": "Found automatically" },
  bzdata_path: { "": "Found automatically" },
  mount_dir: { "": "In KopiaLens's data folder" },
  mount_host_path: { "": "Not set" },
  collapse_prefixes: { "": "No paths" },
  eventlog_archive_path: { "": "Off" },
};
function _klSecs(s) {
  if (s % 86400 === 0) return s / 86400 + (s === 86400 ? " day" : " days");
  if (s % 3600 === 0) return s / 3600 + (s === 3600 ? " hour" : " hours");
  if (s % 60 === 0) return s / 60 + (s === 60 ? " minute" : " minutes");
  return s + " seconds";
}

// x-kl-default="key" on a setting's label: when the value differs from its default, a reset button
// shown on hover or keyboard focus (always on touch screens) and the default in the label's tooltip.
document.addEventListener("alpine:init", () => {
  Alpine.directive("kl-default", (el, { expression }, { effect }) => {
    const keys = expression.trim();
    const label = [...el.childNodes].filter((c) => c.nodeType === 3).map((c) => c.textContent).join(" ").replace(/\s+/g, " ").trim();
    el.classList.add("kl-setting");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "kl-reset-one";
    btn.title = "Reset to default";
    btn.setAttribute("aria-label", `Reset ${label} to default`);
    btn.innerHTML = '<i class="ic ic-restore"></i>';
    btn.hidden = true;
    btn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); Alpine.$data(el).klResetOne(keys); });
    el.append(btn);
    effect(() => {
      const vm = Alpine.$data(el);
      const changed = vm.klChanged(keys);
      btn.hidden = !changed;
      if (changed) el.title = "Changed. Default: " + vm.klDefaultText(keys);
      else el.removeAttribute("title");
    });
  });
  // x-kl-row: hidden while a search is typed that it doesn't match.
  Alpine.directive("kl-row", (el, _, { effect }) => {
    const id = Number(el.dataset.klRow);
    effect(() => {
      const hits = Alpine.$data(el).settingsHits();
      el.classList.toggle("kl-miss", !!hits && !hits.rows.has(id));
    });
  });
});

const settingsPart = {

    settingsNav() {
      return SETTINGS_NAV.map(([g, items]) => [g, items.filter(([id]) => id !== "experimental" || this.experimental)]);
    },
    // The search's matches: {rows: Set of row ids, tabs: {tab: [matched labels]}} for sections shown
    // as a heading only (the app-like ones, or a section named by its other words). Null with no search.
    // A section's own name shows all its rows; "changed" shows every setting that differs from its default.
    settingsHits() {
      const q = this.settingsQuery.trim().toLowerCase();
      if (!q) return null;
      const changed = q === "changed";
      const exp = !!this.experimental;
      if (!changed && _klHits && _klHits.q === q && _klHits.exp === exp) return _klHits.out;
      const titles = Object.fromEntries(this.settingsNav().flatMap((g) => g[1]));
      const words = q.split(/\s+/);
      const out = { rows: new Set(), tabs: {} };
      for (const tab of Object.keys(titles)) {
        const sec = SETTINGS_INDEX.find((r) => r.tab === tab && r.section);
        const named = !changed && sec && docsMatch(sec.hay, q, words).tier > 0;
        if (KL_APP_TABS.has(tab)) {
          if (changed) continue;
          const labels = SETTINGS_INDEX.filter((r) => r.tab === tab && !r.section && docsMatch(r.hay, q, words).tier).map((r) => r.label);
          if (named || labels.length) out.tabs[tab] = labels.slice(0, 3);
          continue;
        }
        const all = !changed && docsMatch(titles[tab].toLowerCase(), q, words).tier >= 2;
        let n = 0;
        for (const r of KL_ROWS) {
          if (r.tab !== tab) continue;
          if (changed ? r.keys && this.klChanged(r.keys) : all || docsMatch(r.hay, q, words).tier) { out.rows.add(r.id); n++; }
        }
        if (!n && named) out.tabs[tab] = [];
      }
      if (!changed) _klHits = { q, exp, out };
      return out;
    },
    // Matches per section once the rows have updated; a row this install doesn't show (another
    // platform, a feature that's off) doesn't count. Re-run when Desktop app's data arrives.
    klMeasure() {
      this.$nextTick(() => {
        const hits = this.settingsHits();
        const counts = {};
        if (hits) {
          for (const sec of document.querySelectorAll('[data-testid="settings-modal"] section[data-kl-tab]')) {
            const tab = sec.dataset.klTab;
            let n = 0;
            for (const el of sec.querySelectorAll("[data-kl-row]")) {
              if (!hits.rows.has(Number(el.dataset.klRow))) continue;
              let shown = true;
              for (let e = el; e && e !== sec; e = e.parentElement) if (e.style.display === "none") { shown = false; break; }
              if (shown) n++;
            }
            if (n || hits.tabs[tab]) counts[tab] = n;
          }
        }
        this.settingsCounts = counts;
      });
    },
    klSearchShows(tab) { return tab in this.settingsCounts; },
    klSearchLabels(tab) {
      const hits = this.settingsHits();
      return (hits && hits.tabs[tab] || []).join(" · ");
    },
    klSearchTotal() { return Object.values(this.settingsCounts).reduce((n, c) => n + (c || 1), 0); },
    settingsQueryChanged(q) {
      const on = !!q.trim();
      if (on !== this.settingsSearching) {
        this.settingsSearching = on;
        if (this.$refs.settingsBody) this.$refs.settingsBody.scrollTop = 0;
      }
      if (!on) this.settingsCounts = {};
    },
    settingsGo(tab) {
      this.settingsQuery = "";
      this.settingsTab = tab;
    },
    // Ctrl+F (Cmd+F) or "/" while Settings is open; Escape in the box clears it first.
    settingsSearchKey(e) {
      if (!this.showSettings) return;
      const act = document.activeElement;
      const typing = act && (/^(INPUT|TEXTAREA|SELECT)$/.test(act.tagName) || act.isContentEditable);
      if (((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") || (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey)) {
        e.preventDefault();
        const box = document.querySelector('[data-testid="settings-search"]');
        if (box) { box.focus(); box.select(); }
      }
    },

    // ---- Changed settings: the dot, and reset (one setting, a page, all) ----
    async _klLoadDefaults() {
      if (this._klDefaults) return;
      try { this._klDefaults = await getJson("/api/settings/defaults"); } catch (e) { /* no markers until it loads */ }
    },
    _klDef(key) {
      const d = this._klDefaults;
      if (!d) return undefined;
      // Without Everything on this computer, its choice reads as Off (_refreshDiskLookup).
      if (key === "disk_lookup" && this.diskLookup && this.diskLookup.everything && !this.diskLookup.everything.supported) return "off";
      const f = this.KL_FIELDS[key];
      if (f && f.top) return d[key];
      if (f && f.block === "backblaze") return (d.backblaze || {})[key];
      return (d.kopialens || {})[key];
    },
    _klNow(key) {
      if (KL_LOOK[key]) return this[KL_LOOK[key][0]];
      const f = this.KL_FIELDS[key];
      if (!f) return undefined;
      const v = this[f.s];
      if (f.t === "int" || f.t === "num") return Number(v);
      if (f.t === "bool") return !!v;
      if (f.t === "list") return String(v || "").split(",").map((p) => p.trim()).filter(Boolean);
      return v == null ? "" : String(v);
    },
    klChanged(keys) {
      return keys.split(",").some((key) => {
        const d = this._klDef(key);
        if (d === undefined) return false;
        const v = this._klNow(key);
        if (d !== null && typeof d === "object") return JSON.stringify(v) !== JSON.stringify(d);
        return typeof d === "number" ? Number(v) !== d : v !== d;
      });
    },
    // The default as the control shows it: the matching option's text where there is one.
    klDefaultText(keys) {
      const el = document.querySelector(`[data-testid="settings-modal"] [x-kl-default="${keys}"]`);
      const row = el && el.parentElement;
      const sels = row ? [...row.querySelectorAll("select")] : [];
      return keys.split(",").map((key, i) => {
        const d = this._klDef(key);
        const named = (KL_DEFAULT_TEXT[key] || {})[Array.isArray(d) ? d.join(", ") : String(d)];
        if (named) return named;
        if (key === "logo" || key === "tray_icon") return (TRAY_ICONS.find((t) => t[0] === d) || [d, d])[1];
        if (KL_DURATIONS.has(key)) return d ? _klSecs(Number(d)) : "Off";
        const opt = sels[i] && [...sels[i].options].find((o) => o.value === String(d));
        if (opt) return opt.textContent.trim();
        if (typeof d === "boolean") return d ? "On" : "Off";
        if (d === "" || (Array.isArray(d) && !d.length)) return "None";
        if (typeof d === "string") return d.charAt(0).toUpperCase() + d.slice(1);
        return String(d);
      }).join(", ");
    },

    klPageChanged() {
      const tab = this.settingsTab === "refresh" ? "indexing" : this.settingsTab;
      return (KL_TAB_KEYS[tab] || []).filter((k) => !k.key.split(",").some((x) => KL_RESET_SKIP.has(x)) && this.klChanged(k.key));
    },
    klAllChanged() {
      return Object.values(KL_TAB_KEYS).flat().filter((k) => !k.key.split(",").some((x) => KL_RESET_SKIP.has(x)) && this.klChanged(k.key));
    },
    // Puts each setting back through its control's own save; the backend then drops the saved value.
    async klResetKeys(keys) {
      await this._klLoadDefaults();
      let done = 0, restart = false;
      for (const key of keys) {
        const d = this._klDef(key);
        if (d === undefined) continue;
        if (KL_LOOK[key]) { KL_LOOK[key][1](this, d); done++; continue; }
        const f = this.KL_FIELDS[key];
        if (!f) continue;
        this[f.s] = f.t === "list" ? (d || []).join(", ") : d;
        if (await this.saveKl(key, true)) { done++; restart = restart || !!f.restart; }
      }
      return { done, restart };
    },
    async klResetOne(keys) {
      const r = await this.klResetKeys(keys.split(","));
      if (r.done) this.klNotify(r.restart ? "Reset to default - this setting needs a restart" : "Reset to default");
    },
    _klResetDone(r) {
      if (!r.done) return;
      const what = r.done === 1 ? "Reset 1 setting" : `Reset ${r.done} settings`;
      this.klNotify(r.restart ? `${what}. Some need a restart.` : what);
    },
    _klResetFacts(items) {
      const facts = items.slice(0, 12).map((k) => [k.label, this.klDefaultText(k.key)]);
      if (items.length > 12) facts.push(["", `and ${items.length - 12} more`]);
      return facts;
    },
    async klResetPage() {
      const items = this.klPageChanged();
      if (!items.length) return;
      const n = items.length;
      const ok = await this.klConfirm({
        title: n === 1 ? "Reset 1 setting on this page?" : `Reset ${n} settings on this page?`,
        body: "These go back to their defaults:", facts: this._klResetFacts(items),
        confirmLabel: n === 1 ? "Reset 1 setting" : `Reset ${n} settings`,
      });
      if (!ok) return;
      this._klResetDone(await this.klResetKeys(items.flatMap((k) => k.key.split(","))));
    },
    async klResetAll() {
      const items = this.klAllChanged();
      if (!items.length) return;
      const n = items.length;
      const ok = await this.klConfirm({
        title: n === 1 ? "Reset 1 changed setting?" : `Reset all ${n} changed settings?`,
        body: "These go back to their defaults. A backup of your settings is made first, so you can undo this from the list above.",
        facts: this._klResetFacts(items), confirmLabel: n === 1 ? "Reset 1 setting" : `Reset ${n} settings`,
      });
      if (!ok) return;
      try { await _cfgPost("/api/config-backups"); }
      catch (e) { this.klNotify(`Couldn't back up your settings, so nothing was reset. ${e.message || ""}`.trim(), "warn"); return; }
      this._klResetDone(await this.klResetKeys(items.flatMap((k) => k.key.split(","))));
      this.loadCfg();
    },

    async openSettings() {  // re-read the live settings file each open so the modal can't show stale state
      this.acc = null; this.accErr = "";
      this.settingsQuery = "";
      this._klLoadDefaults();
      try {
        const data = await getJson("/api/repos");
        const kl = data.settings?.kopialens || {};
        this._klSettings = kl;
        this.notifyHook = null; this.notifyForm = null;
        this.backend = data.settings?.listing_backend || "subprocess";
        this.groupDepth = Number(kl.group_depth || 6);
        this.churnDays = String(kl.churn_days || 30);
        this.churnDepth = Number(kl.churn_depth || 4);
        this.churnView = kl.churn_view || "folder";
        this.indexMode = kl.index_mode || "daily";
        this.indexTime = kl.index_time || "23:00";
        this.indexInterval = Number(kl.index_interval || 86400);
        this.dashboardMode = kl.dashboard_mode || "nightly";
        this.dashRefresh = Number(kl.dashboard_refresh ?? 1800);
        this.excludedTolerance = Number(kl.excluded_tolerance ?? 0.05);
        this.esPath = kl.es_path || "";
        this.repoLabels = kl.repo_labels === "full" ? "full" : "short";
        this.showProBadge = kl.show_pro_badge !== false;
        this._readLook(kl);
        this.updateCheck = kl.update_check !== false;
        this.usageStats = kl.usage_stats !== false;
        this.updateChannel = kl.update_channel === "beta" ? "beta" : "stable";
        this.diskLookupMode = ["everything", "auto", "scan", "off"].includes(kl.disk_lookup) ? kl.disk_lookup : "everything";
        this.diskScanBudget = Number(kl.disk_scan_budget || 15);
        this.diskScanThreads = Number(kl.disk_scan_threads || 0);
        this.diskScanBackground = kl.disk_scan_background === true;
        this.diskCountDrives = kl.disk_count_drives === true;
        this.diskIgnoreSearch = kl.disk_ignore_search === true;
        this.diskFollowLinks = kl.disk_follow_links === true;
        this.diskSizesMinutes = Number(kl.disk_sizes_minutes || 0);
        this.diskShowLocal = kl.disk_show_local !== false;
        this.diskPlaces = Array.isArray(kl.disk_paths) ? kl.disk_paths.filter((p) => p && p.path) : [];
        this.diskPlaceForm = null;
        this.folderPicker = ["auto", "native", "browser"].includes(kl.folder_picker) ? kl.folder_picker : "auto";
        this.mountDir = kl.mount_dir || "";
        this.mountHostPath = kl.mount_host_path || "";
        this._refreshDiskLookup();
        this.sessionDays = Number(kl.session_days || 30);
        this.loadAccount();
        this.raLoad();
        this.panelLayout = kl.panel_layout === "classic" ? "classic" : "tabbed";  //
        this.docsView = kl.docs_view === "side" ? "side" : "popup";
        this._readAdvanced(kl);
        this._readBackblaze(data.settings?.backblaze || {});
        this.loadCfg();
        this.loadIxBackups();
        this.loadAbout();
      } catch (e) { /* keep last-known state on fetch failure */ }
      this.showSettings = true;
    },
    _readAdvanced(kl) {
      this.lightInterval = Number(kl.light_interval || 3600);
      this.lightMode = ["always", "off"].includes(kl.light_mode) ? kl.light_mode : "in_use";
      this.dashboardInterval = Number(kl.dashboard_interval || 14400);
      this.indexWorkerTimeout = Number(kl.index_worker_timeout || 14400);
      this.indexFirstTimeout = Number(kl.index_first_timeout || 86400);
      this.indexWaitUnreachable = Number(kl.index_wait_unreachable || 1800);
      this.indexStallTimeout = Number(kl.index_stall_timeout ?? 3600);
      this.indexKeepVersions = Number(kl.index_keep_versions ?? 1);
      this.indexBackupDir = kl.index_backup_dir || "";
      this.removePlanCap = Number(kl.remove_plan_cap || 200000);
      this.indexRetryDelay = Number(kl.index_retry_delay || 900);
      this.reindexAfterDeleteDelay = Number(kl.reindex_after_delete_delay ?? 300);
      this.indexMaxRetries = Number(kl.index_max_retries ?? 1);
      this.quitIndexing = kl.quit_indexing === "stop" ? "stop" : "finish";
      // ?? not ||: 0 is a meaningful value here (disables the stage) and || would silently
      // rewrite it to the default on every read and every save.
      this.folderPrewarmBudget = Number(kl.folder_prewarm_budget ?? 900);
      this.startupDelay = Number(kl.startup_delay ?? 300);
      this.debugLog = kl.debug_log === true;
      this.experimental = kl.experimental === true;
      this.expNotes = kl.exp_notes === true;
      this.controlApi = kl.control_api === true;
      this.appPort = Number(kl.port || 7123);
      this.controlPort = Number(kl.control_port || 7124);
      this.savedPorts = { port: this.appPort, control_port: this.controlPort };
      this.trayClick = kl.tray_click === "menu" ? "menu" : "window";
      this.trayDoubleClick = kl.tray_double_click === "none" ? "none" : "window";
      this.desktopSignin = kl.desktop_signin === true;
      this.notifyLevel = ["all", "off"].includes(kl.notifications) ? kl.notifications : "warnings";
      this.logCleanupDays = Number(kl.log_cleanup_days ?? 90);
      this.taskHistoryKeep = Number(kl.task_history_days ?? 90);
      this.engineTakeback = kl.engine_takeback === "wait" ? "wait" : "auto";
      this.rankCount = Number(kl.rank_count ?? 10000);
      this.duckdbMemory = String(kl.duckdb_memory || "").toUpperCase();
      this.memoryAuto = kl.memory_auto || "";
      this.duckdbThreads = Number(kl.duckdb_threads || 0);
      this.threadsAuto = Number(kl.threads_auto || 0);
      this.threadsWorker = Number(kl.threads_worker || 0);
      this.cpuCount = Number(kl.cpu_count || 0);
    },
    // Logo, retention chips and the Snapshot now confirm: read at startup and on each Settings open.
    _readLook(kl) {
      this.logo = ["k3g", "k6s", "k3a", "tile", "none"].includes(kl.logo) ? kl.logo : "k3g";
      this.trayIcon = TRAY_ICONS.some((t) => t[0] === kl.tray_icon) ? kl.tray_icon : "k3g";
      this.askSnapshotNow = kl.ask_snapshot_now !== false;
      this.taskView = kl.task_view === "panel" ? "panel" : "popup";
      this.taskHideRoutine = kl.task_hide_routine !== false;
      this.auditHideRoutine = kl.audit_hide_routine !== false;
      this.auditGroups = Array.isArray(kl.audit_groups)
        ? kl.audit_groups.filter((g) => ["changes", "snapshots", "previews", "failed"].includes(g)) : ["changes"];
      this.taskRoutineSecs = Number(kl.task_routine_seconds ?? 1);
      this.taskRoutineHours = Number(kl.task_routine_hours ?? 48);
      this.retentionChips = ["uniform", "custom"].includes(kl.retention_chips) ? kl.retention_chips : "kopiaui";
      this.retentionChipColor = /^#[0-9a-f]{6}$/i.test(kl.retention_chip_color || "") ? kl.retention_chip_color : "#2563eb";
      this.tableAlign = ALIGNS.some((a) => a[0] === kl.table_align) ? kl.table_align : "auto";
      TABLE_ALIGN = this.tableAlign;
      this.tabOrder = Array.isArray(kl.tab_order) ? kl.tab_order.filter((x) => typeof x === "string") : [];
      this.dashPageSize = ["25", "50", "100"].includes(String(kl.dashboard_page_size)) ? String(kl.dashboard_page_size) : "all";
      this.applyFavicon();
    },
    // The browser tab icon follows the header logo (the Disc when the logo is hidden).
    applyFavicon() {
      const el = document.querySelector('link[rel="icon"]');
      if (el) el.href = appUrl(`/static/logos/${this.logo === "none" ? "k3g" : this.logo}.svg`);
    },
    TRAY_ICONS,
    MEMORY_CHOICES,
    fmtLimit(v) { return String(v || "").replace(/(\d)\s*([KMGT]B)$/i, "$1 $2"); },
    threadChoices() { return THREAD_CHOICES.filter((n) => n <= (this.cpuCount || 64)); },
    _readBackblaze(bz) {
      this.bzDataPath = bz.bzdata_path || "";
      this.bzComputer = ["this", "other"].includes(bz.computer) ? bz.computer : "auto";
      this.bzUpdates = bz.updates === "off" ? "off" : "auto";
      this.bzReindexMinGap = Number(bz.bz_reindex_min_gap || 21600);
      this.bzHistoryDays = Number(bz.history_days || 90);
      this.bzCollapsePrefixes = (bz.collapse_prefixes || []).join(", ");
      this.bzEventlogArchive = bz.eventlog_archive_path || "";
      this.bzStore = bz.store === "sqlite" ? "sqlite" : "parquet";
    },
    // One row per control in the modal: which state field it mirrors, how the value is written,
    // and any bound. `restart` = the app reads it once at startup. A control missing from here
    // saves nothing - that is the whole "silently dropped edit" class gone.
    KL_FIELDS: {
      listing_backend: { s: "backend", top: true, t: "choice", of: ["subprocess", "server"], restart: true },
      disk_lookup: { s: "diskLookupMode", t: "choice", of: ["everything", "auto", "scan", "off"] },
      disk_scan_budget: { s: "diskScanBudget", t: "int", min: 1 },
      disk_scan_threads: { s: "diskScanThreads", t: "int", min: 0, max: 64 },
      disk_scan_background: { s: "diskScanBackground", t: "bool" },
      disk_count_drives: { s: "diskCountDrives", t: "bool" },
      disk_ignore_search: { s: "diskIgnoreSearch", t: "bool" },
      disk_follow_links: { s: "diskFollowLinks", t: "bool" },
      disk_sizes_minutes: { s: "diskSizesMinutes", t: "int", min: 0 },
      disk_show_local: { s: "diskShowLocal", t: "bool" },
      folder_picker: { s: "folderPicker", t: "choice", of: ["auto", "native", "browser"] },
      mount_dir: { s: "mountDir", t: "str" },
      mount_host_path: { s: "mountHostPath", t: "str" },
      es_path: { s: "esPath", t: "str" },
      excluded_tolerance: { s: "excludedTolerance", t: "num" },
      index_mode: { s: "indexMode", t: "choice", of: ["daily", "interval", "off"] },
      index_time: { s: "indexTime", t: "str" },
      index_interval: { s: "indexInterval", t: "int", min: 3600 },
      dashboard_mode: { s: "dashboardMode", t: "choice", of: ["nightly", "interval"] },
      light_interval: { s: "lightInterval", t: "int", min: 60 },
      light_mode: { s: "lightMode", t: "choice", of: ["in_use", "always", "off"] },
      dashboard_refresh: { s: "dashRefresh", t: "int", min: 0 },
      dashboard_interval: { s: "dashboardInterval", t: "int", min: 900 },
      index_worker_timeout: { s: "indexWorkerTimeout", t: "int", min: 600 },
      index_first_timeout: { s: "indexFirstTimeout", t: "int", min: 600 },
      index_wait_unreachable: { s: "indexWaitUnreachable", t: "int", min: 60 },
      index_stall_timeout: { s: "indexStallTimeout", t: "int", min: 0 },
      index_keep_versions: { s: "indexKeepVersions", t: "int", min: 0, max: 3 },
      index_backup_dir: { s: "indexBackupDir", t: "str" },
      remove_plan_cap: { s: "removePlanCap", t: "int", min: 1000 },
      reindex_after_delete_delay: { s: "reindexAfterDeleteDelay", t: "int", min: 0 },
      index_retry_delay: { s: "indexRetryDelay", t: "int", min: 60 },
      index_max_retries: { s: "indexMaxRetries", t: "int", min: 0, max: 5 },
      quit_indexing: { s: "quitIndexing", t: "choice", of: ["finish", "stop"] },
      folder_prewarm_budget: { s: "folderPrewarmBudget", t: "int", min: 0 },

      startup_delay: { s: "startupDelay", t: "int", min: 0, restart: true },
      debug_log: { s: "debugLog", t: "bool" },
      experimental: { s: "experimental", t: "bool", reload: true },
      exp_notes: { s: "expNotes", t: "bool", reload: true },
      control_api: { s: "controlApi", t: "bool", restart: true },
      port: { s: "appPort", t: "int", min: 1024, max: 65535, restart: true },
      control_port: { s: "controlPort", t: "int", min: 1024, max: 65535, restart: true },
      tray_click: { s: "trayClick", t: "choice", of: ["window", "menu"] },
      tray_double_click: { s: "trayDoubleClick", t: "choice", of: ["window", "none"] },
      desktop_signin: { s: "desktopSignin", t: "bool" },
      notifications: { s: "notifyLevel", t: "choice", of: ["all", "warnings", "off"] },
      duckdb_memory: { s: "duckdbMemory", t: "choice", of: ["", ...MEMORY_CHOICES], restart: true },
      duckdb_threads: { s: "duckdbThreads", t: "int", min: 0, max: 256, restart: true },
      log_cleanup_days: { s: "logCleanupDays", t: "int", min: 0 },
      task_history_days: { s: "taskHistoryKeep", t: "int", min: 0 },
      task_routine_seconds: { s: "taskRoutineSecs", t: "num" },
      task_routine_hours: { s: "taskRoutineHours", t: "int", min: 1 },
      engine_takeback: { s: "engineTakeback", t: "choice", of: ["auto", "wait"] },
      rank_count: { s: "rankCount", t: "int", min: 0 },
      repo_labels: { s: "repoLabels", t: "choice", of: ["short", "full"] },
      show_pro_badge: { s: "showProBadge", t: "bool" },
      logo: { s: "logo", t: "choice", of: ["k3g", "k6s", "k3a", "tile", "none"] },
      tray_icon: { s: "trayIcon", t: "choice", of: TRAY_ICONS.map((t) => t[0]) },
      ask_snapshot_now: { s: "askSnapshotNow", t: "bool" },
      retention_chips: { s: "retentionChips", t: "choice", of: ["uniform", "kopiaui", "custom"] },
      retention_chip_color: { s: "retentionChipColor", t: "str" },
      table_align: { s: "tableAlign", t: "choice", of: ["auto", "left", "center", "right"] },
      dashboard_page_size: { s: "dashPageSize", t: "choice", of: ["all", "25", "50", "100"] },
      update_check: { s: "updateCheck", t: "bool" },
      usage_stats: { s: "usageStats", t: "bool" },
      update_channel: { s: "updateChannel", t: "choice", of: ["stable", "beta"] },
      bzdata_path: { s: "bzDataPath", t: "str", block: "backblaze" },
      computer: { s: "bzComputer", t: "choice", of: ["auto", "this", "other"], block: "backblaze" },
      store: { s: "bzStore", t: "choice", of: ["parquet", "sqlite"], block: "backblaze" },
      updates: { s: "bzUpdates", t: "choice", of: ["auto", "off"], block: "backblaze" },
      bz_reindex_min_gap: { s: "bzReindexMinGap", t: "int", min: 300, block: "backblaze" },
      history_days: { s: "bzHistoryDays", t: "int", min: 7, block: "backblaze" },
      collapse_prefixes: { s: "bzCollapsePrefixes", t: "list", block: "backblaze" },
      eventlog_archive_path: { s: "bzEventlogArchive", t: "str", block: "backblaze" },
      session_days: { s: "sessionDays", t: "int", min: 1, max: 365 },
      number_style: { s: "numberStyle", t: "choice", of: ["system", "en-US", "de-DE", "fr-FR", "en-IN"] },
      byte_units: { s: "byteUnits", t: "choice", of: ["decimal", "binary"] },
      date_style: { s: "dateStyle", t: "choice", of: ["system", "dmy", "mdy", "iso"] },
      clock: { s: "clock", t: "choice", of: ["system", "12", "24"] },
      search_dedupe_by: { s: "dedupeBy", t: "choice", of: ["path", "content"] },
      header_search_only: { s: "headerSearchOnly", t: "bool" },
    },

    setTimeFmt(v) {
      if (v === TIME_FMT) return;
      TIME_FMT = v;
      this.timeFmt = v;
      localStorage.setItem("kl-timefmt", v);
      this.klSet({ time_format: v });  // permanent in backend too
      this.activate();
    },

    // ---- Remote access (read at start: a change needs a restart) ----
    async loadAccess() {
      this.accErr = "";
      try { this.acc = await getJson("/api/access"); } catch (e) { this.accErr = e.message || String(e); }
    },
    async saveAccess(change) {
      this.accErr = "";
      try {
        this.acc = await getJson("/api/access", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(change) });
        this.klNotify("Saved - restart KopiaLens to apply");
      } catch (e) {
        this.accErr = e.message || String(e);
        this.loadAccess();   // put the form back to what is stored
      }
    },
    accessLabel(m) {
      return { window: "This window only", computer: "This computer", addresses: "These addresses",
               network: "Anyone on the network" }[m] || m;
    },
    accessHint(m) {
      return { window: "Only the KopiaLens app window. Browsers, even on this computer, are turned away.",
               computer: "Any browser on this computer.",
               addresses: "This computer, plus the addresses and networks you list.",
               network: "Anyone who can reach this computer on the network. Turn on sign-in." }[m] || "";
    },

    // Save ONE key on change. The payload never carries a value the user did not touch, so a
    // save cannot stamp a browser-side default over a setting that was resolved elsewhere.
    // Settings > Disk scanning: other places to check (a share, a mounted folder), optionally
    // backed up under another path on another computer. The list saves as one value.
    diskPlaceAdd() {
      this.diskPlaceForm = { index: -1, path: "", backup_path: "", computer: "", error: "" };
      this._loadDiskComputers();
    },
    diskPlaceEdit(i) {
      const pl = this.diskPlaces[i];
      this.diskPlaceForm = { index: i, path: pl.path, backup_path: pl.backup_path || "", computer: pl.computer || "", error: "" };
      this._loadDiskComputers();
    },
    _loadDiskComputers() {
      if (this._diskComputers) return;
      getJson("/api/disk-lookup/computers").then((r) => { this._diskComputers = r.computers || []; }).catch(() => {});
    },
    diskPlaceComputers() { return this._diskComputers || []; },
    async _saveDiskPlaces(list) {
      await getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kopialens: { disk_paths: list } }) });
      this.diskPlaces = list;
      this._excludedDrivesStale = true;   // the Excluded tab re-reads its locations
      this._refreshDiskLookup();
    },
    // Which reader answers here. On a computer without Everything, the Everything choice reads as Off.
    _refreshDiskLookup() {
      getJson("/api/disk-lookup?refresh=true").then((s) => {
        this.diskLookup = s;
        if (s.everything && !s.everything.supported && this.diskLookupMode === "everything") this.diskLookupMode = "off";
      }).catch(() => {});
    },
    async diskPlaceSave() {
      const f = this.diskPlaceForm;
      const entry = { path: f.path.trim(), backup_path: f.backup_path.trim(), computer: f.computer.trim() };
      const clash = this.diskPlaces.findIndex((p, i) => i !== f.index && p.path.toLowerCase() === entry.path.toLowerCase());
      if (clash >= 0) { f.error = "That place is already in the list."; return; }
      const list = this.diskPlaces.slice();
      if (f.index >= 0) list[f.index] = entry; else list.push(entry);
      try {
        await this._saveDiskPlaces(list);
        this.diskPlaceForm = null;
        this.klNotify(f.index >= 0 ? "Place saved" : "Place added");
      } catch (e) { f.error = e.message || String(e); }
    },
    async diskPlaceRemove(i) {
      const list = this.diskPlaces.filter((_p, j) => j !== i);
      try { await this._saveDiskPlaces(list); this.klNotify("Place removed"); }
      catch (e) { this.klNotify(e.message || String(e), "warn"); }
    },
    saveKl(key, quiet = false) {   // quiet: no "Saved" notice (a reset says what it did itself)
      const f = this.KL_FIELDS[key];
      if (!f) return Promise.resolve(false);
      let v = this[f.s];
      if (f.t === "int") {
        v = Math.round(Number(v));
        if (!isFinite(v)) v = f.min != null ? f.min : 0;
        if (f.min != null) v = Math.max(f.min, v);
        if (f.max != null) v = Math.min(f.max, v);
        this[f.s] = v;  // show the clamped value the backend will actually store
      } else if (f.t === "num") {
        v = Number(v) || 0;
      } else if (f.t === "bool") {
        v = !!v;
      } else if (f.t === "list") {
        v = String(v || "").split(",").map((p) => p.trim()).filter(Boolean);
      } else if (f.t === "choice") {
        v = f.of.includes(v) ? v : f.of[0];
        this[f.s] = v;
      } else {
        v = v == null ? "" : String(v);
      }
      if (key === "number_style" || key === "date_style" || key === "clock" || key === "byte_units") this.applyFormats();
      if (key === "logo") this.applyFavicon();
      if ((key === "retention_chips" || key === "retention_chip_color") && this.active === "snapshots") this.loadSnapshots(true);
      const body = f.top ? { [key]: v }
        : f.block === "backblaze" ? { backblaze: { [key]: v } }
        : { kopialens: { [key]: v } };
      return getJson("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
        .then(() => {
          if (!quiet) this.klNotify(f.restart ? "Saved - this setting needs a restart"
            : f.reload ? "Saved - reload the page to see the change" : "Saved");
          if (key === "experimental" && !v && this.settingsTab === "experimental") this.settingsTab = "advanced";
          if (key === "dashboard_refresh") this.armAutoRefresh();
          if (key.startsWith("disk_") || key === "es_path") { this._excludedDrivesStale = true; this._refreshDiskLookup(); }
          if (key in this.savedPorts) this.savedPorts[key] = v;
          if (key === "update_channel") { this.loadUsagePreview(); if (this.updateCheck) this.checkUpdatesNow(); }
          if (f.block === "backblaze" && (key === "bzdata_path" || key === "computer")) {   // applies now, no restart
            this._excludedDrivesStale = true; this.raLoad(); this.bzRefreshStatus();
          }
          return true;
        })
        .catch((e) => {
          this.klNotify(e.message || String(e), "warn");   // Settings is open over the header
          if (key in this.savedPorts) this[f.s] = this.savedPorts[key];  // refused: show what is saved
          return false;
        });
    },
};
