// The selection bar floats over the bottom of the table (true) or sits in the toolbar (false, the
// earlier behaviour, kept for comparison). css/app.css html[data-selbar="float"].
const SELBAR_FLOAT = true;
document.documentElement.dataset.selbar = SELBAR_FLOAT ? "float" : "inline";

// Panel lists longer than this offer "Show as a table" (full-list popup with Download CSV) even when nothing is cut.
const FULL_LIST_MIN = 50;

// ---- Number and date style (Settings > Appearance). undefined = the system's own style. ----
let NUM_LOCALE;
let _dateStyle = "system", _clock = "system", _byteUnits = "decimal";
const _numFmts = {};
// A fixed number of decimals in the chosen number style: fmtFix(1.5, 1) -> "1.5" or "1,5".
const fmtFix = (n, d) => {
  const key = (NUM_LOCALE || "") + "|" + d;
  if (!_numFmts[key]) _numFmts[key] = new Intl.NumberFormat(NUM_LOCALE, { minimumFractionDigits: d, maximumFractionDigits: d });
  return _numFmts[key].format(Number(n) || 0);
};
// Decimal (1 GB = 10^9 bytes, kopia's default) or binary (1 GiB = 2^30 bytes) - Settings > Appearance.
const fmtBytes = (value) => {
  const n = Number(value || 0);
  if (n < 0) return "-" + fmtBytes(-n);
  const [k, u] = _byteUnits === "binary" ? [1024, ["KiB", "MiB", "GiB", "TiB"]] : [1000, ["KB", "MB", "GB", "TB"]];
  if (n >= k ** 4) return `${fmtFix(n / k ** 4, 2)} ${u[3]}`;
  if (n >= k ** 3) return `${fmtFix(n / k ** 3, 2)} ${u[2]}`;
  if (n >= k ** 2) return `${fmtFix(n / k ** 2, 1)} ${u[1]}`;
  if (n >= k) return `${fmtFix(n / k, 1)} ${u[0]}`;
  return `${n} B`;
};
// Text for a table cell built as HTML (Tabulator formatters).
const escHtml = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\x22/g, "&quot;");
// Icons (css/icons.css, built from packaging/icons.json) for HTML built in JS.
const iconHtml = (name, cls = "") => `<i class="ic ic-${name}${cls ? " " + cls : ""}"></i>`;
// File kinds: one icon per kind in every set except Material, which picks by extension, file
// name or folder name from the theme's own map (js/fileicons-material.js).
const _FILE_KINDS = [
  ["file-image", "jpg jpeg png gif webp heic heif avif jxl bmp tif tiff svg ico raw cr2 cr3 nef arw dng psd"],
  ["file-audio", "mp3 flac wav m4a aac ogg opus wma aiff alac mid midi"],
  ["file-video", "mp4 mkv mov avi wmv webm m4v mts m2ts ts flv mpg mpeg 3gp"],
  ["file-zip", "zip 7z rar tar gz tgz bz2 xz zst cab lz4 lzma tbz2 txz"],
  ["file-disk", "iso img vhd vhdx vmdk qcow2 dmg wim esd vdi"],
  ["file-exe", "exe msi dll appimage deb rpm apk msix appx sys pkg"],
  ["file-pdf", "pdf"],
  ["file-doc", "doc docx rtf odt pages"],
  ["file-sheet", "xls xlsx xlsm ods csv tsv numbers"],
  ["file-slides", "ppt pptx odp key"],
  ["file-db", "db sqlite sqlite3 mdb accdb dbf sql"],
  ["file-font", "ttf otf woff woff2 fon"],
  ["file-code", "py js ts jsx tsx json html htm css c cpp h hpp cs go rs java kt sh bat cmd ps1 xml yml yaml toml ini lua rb php"],
  ["file-text", "txt md log nfo"],
].map(([icon, exts]) => [icon, new Set(exts.split(" "))]);
const fileIconName = (name) => {
  const m = /\.([^.\\/]+)$/.exec(String(name || ""));
  const ext = m ? m[1].toLowerCase() : "";
  const hit = ext && _FILE_KINDS.find(([, set]) => set.has(ext));
  return hit ? hit[0] : "file";
};
// The icon in front of a file or folder name: the set from Settings > Appearance (stamped on
// <html> as data-fileicons). The Design menu (dev add-on) can try others, such as Phosphor.
const FILE_ICON_SETS = ["material", "filled", "outline", "fluent", "emoji"];
const fileIconSet = () => {
  const el = document.documentElement;
  const tried = el.getAttribute("data-d-fileicons");
  if (tried && tried !== "setting") return tried;
  const s = el.getAttribute("data-fileicons");
  return FILE_ICON_SETS.includes(s) ? s : "material";
};
// Rows carry the set's own markup, so a set tried from the Design menu redraws the tables.
new MutationObserver(() => document.querySelectorAll(".tabulator").forEach((el) => {
  const t = typeof Tabulator !== "undefined" && Tabulator.findTable(el);
  if (t && t[0]) t[0].redraw(true);
})).observe(document.documentElement, { attributes: true, attributeFilter: ["data-d-fileicons"] });
const _FILE_EMOJI = {
  folder: "\u{1F4C1}", file: "\u{1F4C4}", "file-image": "\u{1F5BC}️", "file-audio": "\u{1F3B5}",
  "file-video": "\u{1F3AC}", "file-zip": "\u{1F4E6}", "file-code": "\u{1F4BB}", "file-text": "\u{1F4DD}",
  "file-pdf": "\u{1F4D5}", "file-doc": "\u{1F4C3}", "file-sheet": "\u{1F4CA}", "file-slides": "\u{1F4FD}️",
  "file-exe": "⚙️", "file-disk": "\u{1F4BF}", "file-db": "\u{1F5C4}️", "file-font": "\u{1F524}",
};
let _matIndex = null;
const _materialIcon = (name, folder) => {
  if (typeof MATERIAL_ICONS === "undefined") return folder ? "folder" : "file";
  if (!_matIndex) {
    const index = (groups) => {
      const out = new Map();
      for (const [icon, keys] of Object.entries(groups)) for (const k of keys.split("|")) out.set(k, icon);
      return out;
    };
    _matIndex = { ext: index(MATERIAL_ICONS.ext), names: index(MATERIAL_ICONS.names), folders: index(MATERIAL_ICONS.folders) };
  }
  const base = baseName(String(name || "")).toLowerCase();
  if (folder) return _matIndex.folders.get(base) || MATERIAL_ICONS.folder;
  const named = _matIndex.names.get(base);
  if (named) return named;
  for (let i = base.indexOf("."); i >= 0; i = base.indexOf(".", i + 1)) {   // longest extension first
    const hit = _matIndex.ext.get(base.slice(i + 1));
    if (hit) return hit;
  }
  return MATERIAL_ICONS.file;
};
const _iconFile = (pack, file) => appUrl(`/static/vendor/icons/${pack}/${file}.svg`);
// set: one of FILE_ICON_SETS or a Design menu set; leave it out for the current one.
const itemIconHtml = (name, folder, set = fileIconSet()) => {
  const kind = folder ? "folder" : fileIconName(name);
  if (set === "material" || set === "fluent") {
    const file = set === "material" ? _materialIcon(name, folder) : kind;
    const pack = set === "material" ? "material" : "fluent-emoji";
    const light = set === "material" && typeof MATERIAL_ICONS !== "undefined" && MATERIAL_ICONS.light[file];
    const vars = `--icd:url('${_iconFile(pack, file)}')` + (light ? `;--icl:url('${_iconFile(pack, light)}')` : "");
    return `<i class="ic kl-name-ic kl-img-ic mr-1" style="${vars}" aria-hidden="true"></i>`;
  }
  if (set === "emoji") return `<span class="kl-name-ic kl-emoji-ic mr-1" aria-hidden="true">${_FILE_EMOJI[kind]}</span>`;
  const solid = set === "filled" || set === "ph-fill" || set === "ph-duotone";
  const tint = folder ? "kl-folder-ic" : solid ? "" : "text-muted";
  const colour = solid && !folder ? ` style="color:var(--ft-${kind.replace("file-", "") || "file"})"` : "";
  const icon = (set === "filled" ? "tf-" : "t-") + kind;   // icons.css swaps the t- icons for Phosphor
  return `<i class="ic ic-${icon} kl-name-ic ${tint} mr-1"${colour} aria-hidden="true"></i>`;
};
// One list for every pause menu (indexing and backups, app and tray). 0 = until resumed.
const PAUSE_CHOICES = [[1, "1 hour"], [4, "4 hours"], [12, "12 hours"], [24, "1 day"], [72, "3 days"], [168, "1 week"], [0, "Until I resume"]];
const fmtPhys = (v) => (v === null || v === undefined ? "—" : fmtBytes(v));
// A count with its noun: countOf(1, "file") -> "1 file", countOf(3, "file") -> "3 files".
const countOf = (n, one, many = one + "s") => `${Number(n || 0).toLocaleString(NUM_LOCALE)} ${Number(n) === 1 ? one : many}`;

// Accepts an ISO / "YYYY-MM-DD HH:MM:SS" string, a Date, or an epoch number. Epoch SECONDS (the
// backend's time.time()) are told apart from milliseconds by magnitude.
const toDate = (s) => (typeof s === "number" ? new Date(s < 1e12 ? s * 1000 : s) : new Date(s));
// This computer's own formats (GET /api/app/host `formats`), used by the "system" styles when the
// browser runs on the same computer; otherwise the browser's language decides.
let _os = {};
const _okLocale = (tag) => { try { return tag && Intl.getCanonicalLocales(tag).length ? tag : undefined; } catch (e) { return undefined; } };
const setOsFormats = (f) => { _os = f && f.local ? { ...f, locale: _okLocale(f.locale) } : {}; };
setOsFormats(window.KL_OS_FORMATS);
// The locale dates are written in: day-month and month-day styles pin one; system uses the computer's.
const dateLocale = () => ({ dmy: "en-GB", mdy: "en-US" })[_dateStyle] || (_dateStyle === "system" ? _os.locale : undefined);
const _hour12 = () => (_clock === "12" ? true : _clock === "24" ? false : undefined);
// One formatter each, rebuilt only when the style changes: toLocaleString builds a new one per
// call, which is ~15x slower on long lists.
let _absFmt, _dateFmt, _clockFmt;
const _buildDateFmts = () => {
  _absFmt = new Intl.DateTimeFormat(dateLocale(), { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: _hour12() });
  _dateFmt = new Intl.DateTimeFormat(dateLocale(), { year: "numeric", month: "short", day: "numeric" });
  _clockFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hour12: _hour12() });
};
_buildDateFmts();
const _isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
// Apply the saved styles; callers redraw what is on screen afterwards.
const setFormatStyles = ({ number_style, date_style, clock, byte_units } = {}) => {
  NUM_LOCALE = ["en-US", "de-DE", "fr-FR", "en-IN"].includes(number_style) ? number_style : (_os.number || _os.locale);
  _byteUnits = byte_units === "binary" ? "binary" : "decimal";
  _dateStyle = ["dmy", "mdy", "iso"].includes(date_style) ? date_style : (_os.date || "system");
  _clock = ["12", "24"].includes(String(clock)) ? String(clock) : (_os.clock || "system");
  _buildDateFmts();
};
// What "System" means right now, for the Settings labels.
const sysFormatSamples = () => {
  const loc = _os.locale;
  const num = new Intl.NumberFormat(_os.number || loc).format(1234567.8);
  const day = { year: "numeric", month: "short", day: "numeric" };
  const d = new Date();
  const date = _os.date === "iso" ? _isoDay(d) : d.toLocaleDateString(({ dmy: "en-GB", mdy: "en-US" })[_os.date] || loc, day);
  const h12 = _os.clock ? _os.clock === "12" : new Intl.DateTimeFormat(loc, { hour: "numeric" }).resolvedOptions().hour12;
  return { num, date, clock: h12 ? "12-hour" : "24-hour" };
};
const fmtAbs = (s) => {
  if (!s) return "";
  const d = toDate(s);
  if (isNaN(d.getTime())) return String(s).slice(0, 19);
  return _dateStyle === "iso" ? `${_isoDay(d)} ${_clockFmt.format(d)}` : _absFmt.format(d);
};
// Date only ("20 Jul 2024") - for day-granularity values and "first backed up" style labels.
const fmtDate = (s) => {
  if (!s) return "";
  const d = /^\d{4}-\d{2}-\d{2}$/.test(String(s)) ? new Date(`${s}T00:00:00`) : toDate(s);
  if (isNaN(d.getTime())) return String(s).slice(0, 10);
  return _dateStyle === "iso" ? _isoDay(d) : _dateFmt.format(d);
};
// A DURATION in seconds ("0.4 s", "42 s", "6 m 12 s", "1 h 05 m") - never pass one to humanTime.
const fmtDur = (sec) => {
  const n = Number(sec);
  if (!isFinite(n) || n < 0) return "";
  if (n < 10) return `${fmtFix(n, 1)} s`;
  if (n < 60) return `${Math.round(n)} s`;
  if (n < 3600) return `${Math.floor(n / 60)} m ${String(Math.floor(n % 60)).padStart(2, "0")} s`;
  return `${Math.floor(n / 3600)} h ${String(Math.floor((n % 3600) / 60)).padStart(2, "0")} m`;
};
// Estimated ranges: "under a minute", "about 4–12 min", "about 1–3 h"; "about 2.0 MB",
// "about 340 MB – 1.01 GB".
const fmtDurRange = ([lo, hi]) => {
  if (hi < 60) return "under a minute";
  if (hi < 2 * 3600) return `about ${Math.max(1, Math.round(lo / 60))}–${Math.ceil(hi / 60)} min`;
  return `about ${Math.max(1, Math.round(lo / 3600))}–${Math.ceil(hi / 3600)} h`;
};
const fmtBytesRange = ([lo, hi]) => {
  const a = fmtBytes(lo), b = fmtBytes(hi);
  return a === b ? `about ${a}` : `about ${a} – ${b}`;
};
// Shorten a long path in the middle so both ends stay readable: midTrunc("C:\a\b\c", 9).
const midTrunc = (s, n) => { s = String(s || ""); if (s.length <= n) return s; const k = Math.floor((n - 1) / 2); return s.slice(0, k) + "…" + s.slice(s.length - (n - 1 - k)); };
const capFirst = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
// Sum of several first-index estimates (repositories are indexed one after another).
const sumEstimates = (list) => list.reduce((t, e) => ({
  seconds: [t.seconds[0] + e.seconds[0], t.seconds[1] + e.seconds[1]],
  bytes: [t.bytes[0] + e.bytes[0], t.bytes[1] + e.bytes[1]] }), { seconds: [0, 0], bytes: [0, 0] });
const fmtRel = (s) => {
  if (!s) return "";
  const d = toDate(s);
  if (isNaN(d.getTime())) return String(s).slice(0, 19);
  let sec = (Date.now() - d.getTime()) / 1000;
  const future = sec < 0; sec = Math.abs(sec);
  for (const [name, span] of [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]]) {
    if (sec >= span) { const n = Math.floor(sec / span); const lbl = `${n} ${name}${n > 1 ? "s" : ""}`; return future ? `in ${lbl}` : `${lbl} ago`; }
  }
  return future ? "in a moment" : "just now";
};
let TIME_FMT = localStorage.getItem("kl-timefmt") || "auto";
// Automatic: how long ago where a time says how fresh something is, a date where it is a record.
const _FRESH_TABLES = new Set(["tabulator-dashboardTable", "tabulator-tasksTable"]);
const _timeMode = (fresh) => (TIME_FMT === "relative" ? "rel" : TIME_FMT === "absolute" ? "abs" : fresh ? "rel" : "abs");
const humanTime = (s) => (_timeMode(false) === "rel" ? fmtRel(s) : fmtAbs(s));
const freshTime = (s) => (_timeMode(true) === "rel" ? fmtRel(s) : fmtAbs(s));
// A time in a table cell; ``cell`` (the Tabulator cell) lets a column keep its own choice (column menu > Times).
const timeCell = (s, cell, note = "") => {   // ``note``: an extra tooltip line before the time
  if (!s) return "—";
  const abs = fmtAbs(s), rel = fmtRel(s);
  const mode = cellTimeMode(cell) || _timeMode(!!(cell && cell.getTable && _FRESH_TABLES.has(tableKey(cell.getTable()))));
  const tip = (t) => escHtml(note ? `${note}\n${t}` : t);
  if (mode === "both") return `<span title="${tip(rel)}">${abs} <span class="text-muted">· ${rel}</span></span>`;
  return mode === "rel" ? `<span title="${tip(abs)}">${rel}</span>` : `<span title="${tip(rel)}">${abs}</span>`;
};

const sourcePath = (s) => (s && s.includes(":") ? s.split(":").slice(1).join(":") : (s || ""));
const isUnc = (p) => p.startsWith("\\\\");
// A full path's own separator: POSIX paths start with "/", everything else is a Windows path.
// A POSIX name may hold a backslash, so paths are never split on both.
const pathSep = (p) => ((p || "").startsWith("/") ? "/" : "\\");
const trimSep = (p) => { const s = pathSep(p); let t = p || ""; while (t.length > 1 && t.endsWith(s)) t = t.slice(0, -1); return t; };
const joinPath = (base, name) => { const s = pathSep(base); const b = trimSep(base); return (b === s ? "" : b) + s + name; };
const baseName = (p) => { const s = pathSep(p); const t = trimSep(p); return t.slice(t.lastIndexOf(s) + 1); };
// The folder above a path, or null at a top-level node ("C:", "\\\\host\\share", "/").
const parentPath = (p) => {
  const t = trimSep(p);
  const s = pathSep(t);
  if (s === "/") return t === "/" ? null : (t.lastIndexOf("/") <= 0 ? "/" : t.slice(0, t.lastIndexOf("/")));
  if (isUnc(t) ? t.slice(2).split("\\").length <= 2 : !t.includes("\\")) return null;
  return t.slice(0, t.lastIndexOf("\\"));
};
const srcSortKey = (s) => { const p = sourcePath(s); return (isUnc(p) ? "1" : "0") + "|" + p.toLowerCase(); };
const srcSorter = (a, b) => srcSortKey(a).localeCompare(srcSortKey(b));

// An app path as the browser must request it: window.KL_BASE is the prefix KopiaLens is served under
// (Home Assistant ingress, a proxy sub-path), stamped by the server, "" otherwise.
const appUrl = (path) => (typeof path === "string" && path.startsWith("/") && !path.startsWith("//")
  ? (window.KL_BASE || "") + path : path);

const getJson = async (url, options = {}) => {
  const res = await fetch(appUrl(url), options);
  // Tolerate a non-JSON body (e.g. a plain-text 500 "Internal Server Error") instead of throwing
  // an "Unexpected token" parse error - surface the text as the message.
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
  // A sign-in that expired (or a password changed elsewhere) must land on the login page - the
  // alternative is every poll failing forever in the corner of an open tab.
  if (res.status === 401 && data && data.login) {
    location.href = appUrl(data.login);
    throw new Error(data.detail || "Not signed in.");
  }
  // An encrypted index that is locked: the app shows one unlock box instead of per-view errors.
  if (res.status === 423 && data && data.locked) window.dispatchEvent(new CustomEvent("kl-locked", { detail: data }));
  // Setup closed mid-way on a server install: onboarding asks for the setup code.
  if (res.status === 403 && data && data.setup_code) window.dispatchEvent(new CustomEvent("kl-setup-code"));
  // kopia couldn't list a repository: one notice at the top of the page, not an error per view.
  if (res.status === 503 && data && data.listing_failed) window.dispatchEvent(new CustomEvent("kl-listing-failed", { detail: data }));
  // A Pro refusal means the screen's licence state is stale.
  if (res.status === 403 && res.headers.get("X-KopiaLens-Pro")) window.dispatchEvent(new CustomEvent("kl-pro-denied"));
  if (!res.ok) {
    const err = new Error((data && (data.detail || data.error)) || text || res.statusText);
    err.status = res.status; err.data = data; err.notice = !!(data && data.listing_failed);
    throw err;
  }
  return data;
};

// Requests that belong to one view (a tab's list and its column fills, an open panel). A new load
// of the view, or leaving it, aborts the old ones, and the server stops working on them.
const _views = {};
const viewSignal = (view) => (_views[view] || (_views[view] = new AbortController())).signal;
const viewAbort = (view) => { if (_views[view]) { _views[view].abort(); delete _views[view]; } };
const viewRestart = (view) => { viewAbort(view); return viewSignal(view); };
const isAbort = (e) => !!e && e.name === "AbortError";

// Column fills (values worked out in the background) keep at most two requests open, so a click
// always finds a free connection: browsers allow six per site, and the server runs one fill at a
// time anyway. A fill still waiting when its view is left is dropped, never sent.
const FILL_LIMIT = 2;
let _fillsOpen = 0;
const _fillWaiting = [];
const _abortError = () => new DOMException("The view was left", "AbortError");
const _fillNext = () => { while (_fillsOpen < FILL_LIMIT && _fillWaiting.length) _fillWaiting.shift()(); };
const fillJson = (url, options = {}) => new Promise((resolve, reject) => {
  const signal = options.signal;
  const start = () => {
    if (signal && signal.aborted) { reject(_abortError()); return; }
    _fillsOpen++;
    const headers = { ...(options.headers || {}), "X-KopiaLens-Fill": "1" };
    getJson(url, { ...options, headers, priority: "low" }).then(resolve, reject)
      .finally(() => { _fillsOpen--; _fillNext(); });
  };
  if (signal && signal.aborted) { reject(_abortError()); return; }
  if (_fillsOpen < FILL_LIMIT) { start(); return; }
  _fillWaiting.push(start);
  if (signal) signal.addEventListener("abort", () => {
    const i = _fillWaiting.indexOf(start);
    if (i >= 0) { _fillWaiting.splice(i, 1); reject(_abortError()); }
  }, { once: true });
});

// A log line reads as a warning when a failure word appears WITHOUT a zero count in front of it:
// the index worker's stage summaries legitimately print "0 failed"/"0 errors" on a clean pass, and
// colouring those amber made every normal nightly look alarming. Shared by the job and index panes.
const klLogLineWarn = (ln) => {
  // Drop the zero counts first, then test what is left: "1 warning, 0 errors" stays amber.
  const s = String(ln || "").replace(/\b(0|no)\s+(failed|failures|errors?|warnings?)\b/gi, "");
  return /\b(error|warn|unable|failed|cannot|abort|timed out|traceback)/i.test(s) ? "w" : "";
};

// ---- Backend-persisted table preferences (columns + sort), survives browser-data clears ----
let TABLE_PREFS = {};
const _persistTimers = {};
// Several browser tabs stay in step: a layout saved in one tab is applied in the others at once
// (same browser), and a tab coming back into view re-reads the saved layouts (another browser).
// Without this, a tab kept its startup copy and wrote it back over the newer layout.
const _tablesByPid = {};
let _applyingPrefs = false;
const _prefsChannel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel("kl-table-prefs") : null;
function persistReader(id, type) { return (TABLE_PREFS[id] && TABLE_PREFS[id][type]) || false; }
function persistWriter(id, type, data) {
  TABLE_PREFS[id] = TABLE_PREFS[id] || {};
  TABLE_PREFS[id][type] = data;
  if (_applyingPrefs) return;
  _queueTableSave(id);
}
function _queueTableSave(id) {
  clearTimeout(_persistTimers[id]);
  _persistTimers[id] = setTimeout(() => {
    getJson("/api/settings/table", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, config: TABLE_PREFS[id] || null }),
    }).catch(() => {});
    if (_prefsChannel) _prefsChannel.postMessage({ id, config: TABLE_PREFS[id] || null });
  }, 600);
}
function applyTablePrefs(id, config) {
  if (JSON.stringify(TABLE_PREFS[id] || null) === JSON.stringify(config || null)) return;
  clearTimeout(_persistTimers[id]);   // a pending save of this tab's older layout must not win
  const klBefore = JSON.stringify((TABLE_PREFS[id] || {}).kl || {});
  if (!config) { delete TABLE_PREFS[id]; window.dispatchEvent(new CustomEvent("kl-table-rebuild")); return; }
  TABLE_PREFS[id] = config;
  const t = _tablesByPid[id];
  if (!t || !t.element || !document.contains(t.element)) return;
  _applyingPrefs = true;
  try {
    if (config.columns) t.setColumnLayout(config.columns);
    if (config.sort) t.setSort(config.sort);
  } catch (e) { /* the table rebuilds from TABLE_PREFS next time */ } finally { _applyingPrefs = false; }
  if (JSON.stringify(config.kl || {}) !== klBefore) applyTableKl(t);
}
if (_prefsChannel) _prefsChannel.onmessage = (e) => { if (e.data && e.data.id) applyTablePrefs(e.data.id, e.data.config); };
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  getJson("/api/settings/tables").then((all) => {
    for (const [id, cfg] of Object.entries(all || {})) applyTablePrefs(id, cfg);
  }).catch(() => {});
});

function colVisibilityMenu(e, column) {
  return column.getTable().getColumns().map((col) => ({
    label: iconHtml(col.isVisible() ? "checkbox" : "checkbox-empty", "mr-1") + escHtml(col.getDefinition().title || col.getField()),
    action: (ev) => { ev.stopPropagation(); col.toggle(); },
  }));
}

// ---- Table layout choices, saved with the table's layout under `kl` (ui_state.json): ----
// {align: {field: a}, tableAlign, layout: "fill"|"fit", frozen: [field], time: {field: {mode, title}}}.
// Alignment: column > table > app default (Settings > Appearance) > the column's own (Auto).
let TABLE_ALIGN = "auto";
const ALIGNS = [["auto", "Auto"], ["left", "Left"], ["center", "Centre"], ["right", "Right"]];
const _origAlign = {};       // "key|field" -> the column's own hozAlign, for Auto
const _timeCols = new Set(); // "key|field" of columns drawn with timeCell
const tableKey = (table) => "tabulator-" + table.options.persistenceID;
const tableKl = (key) => (TABLE_PREFS[key] && TABLE_PREFS[key].kl) || {};
function saveTableKl(table, patch) {   // ``table``: a Tabulator, or its prefs key
  const key = typeof table === "string" ? table : tableKey(table);
  const kl = { ...tableKl(key), ...patch };
  for (const k of Object.keys(kl)) if (kl[k] == null || (typeof kl[k] === "object" && !Object.keys(kl[k]).length)) delete kl[k];
  TABLE_PREFS[key] = { ...(TABLE_PREFS[key] || {}), kl };
  _queueTableSave(key);
}
const _alignOf = (key, field) => {
  const kl = tableKl(key);
  return (kl.align && kl.align[field]) || kl.tableAlign || TABLE_ALIGN || "auto";
};
const _hoz = (key, field) => { const a = _alignOf(key, field); return a === "auto" ? (_origAlign[key + "|" + field] || undefined) : a; };
// Bring a built table in line with its saved choices: alignment, pinned columns, width mode, times.
function applyTableKl(table) {
  const key = tableKey(table);
  const kl = tableKl(key);
  for (const col of table.getColumns()) {
    const f = col.getField();
    if (!f) continue;
    const want = _hoz(key, f) || "left";
    const def = col.getDefinition();
    const pinned = (kl.frozen || []).includes(f);
    if ((def.hozAlign || "left") !== want || !!def.frozen !== pinned) {
      col.updateDefinition({ hozAlign: want, headerHozAlign: want, frozen: pinned, width: col.getWidth() }).catch(() => {});
    }
  }
  _setTableLayout(table, kl.layout === "fit" ? "fitData" : (table._klLayout || "fitColumns"));
  table.getRows().forEach((r) => r.reformat());
}
function _setTableLayout(table, mode) {
  if (table.options.layout === mode) return;
  table.options.layout = mode;
  table.modules.layout.mode = mode;
  table.element.setAttribute("tabulator-layout", mode);
  if (mode === "fitData") table.getColumns().forEach((c) => c.isVisible() && c.setWidth(true));
  table.redraw(true);
}
// A time column's own choice: "rel" (how long ago), "abs" (date) or "" (the header button's).
function cellTimeMode(cell) {
  if (!cell || !cell.getColumn) return "";
  const table = cell.getTable(), f = cell.getColumn().getField(), key = tableKey(table);
  _timeCols.add(key + "|" + f);
  const t = tableKl(key).time;
  return (t && t[f] && t[f].mode) || "";
}
// Every per-column time choice, for the header button's menu: [{key, field, title, mode}].
function timeOverrides() {
  const out = [];
  for (const [key, cfg] of Object.entries(TABLE_PREFS)) {
    const t = cfg && cfg.kl && cfg.kl.time;
    const live = _tablesByPid[key];
    for (const [field, v] of Object.entries(t || {})) {
      const col = live && live.getColumn && live.getColumn(field);   // the column's title today, if it was renamed
      out.push({ key, field, title: (col && col.getDefinition().title) || v.title || field, mode: v.mode });
    }
  }
  return out;
}
function clearTimeOverride(key, field) {
  const cfg = TABLE_PREFS[key];
  if (!(cfg && cfg.kl && cfg.kl.time && cfg.kl.time[field])) return;
  const time = { ...cfg.kl.time };
  delete time[field];
  const kl = { ...cfg.kl, time };
  if (!Object.keys(time).length) delete kl.time;
  TABLE_PREFS[key] = { ...cfg, kl };
  _queueTableSave(key);
  const t = _tablesByPid[key];
  if (t && t.element && document.contains(t.element)) t.getRows().forEach((r) => r.reformat());
}
function clearTimeOverrides() {
  for (const [key, cfg] of Object.entries(TABLE_PREFS)) {
    if (!(cfg && cfg.kl && cfg.kl.time)) continue;
    const kl = { ...cfg.kl };
    delete kl.time;
    TABLE_PREFS[key] = { ...cfg, kl };
    _queueTableSave(key);
    const t = _tablesByPid[key];
    if (t && t.element && document.contains(t.element)) t.getRows().forEach((r) => r.reformat());
  }
}
const _menuMark = (on) => iconHtml(on ? "check" : "dot", on ? "mr-1" : "mr-1 opacity-0");
// Right-click on a column header.
function colHeaderMenu(e, column) {
  const table = column.getTable(), key = tableKey(table), f = column.getField();
  const kl = tableKl(key);
  const own = (kl.align && kl.align[f]) || "";
  const setAlign = (a) => { saveTableKl(table, { align: { ...(kl.align || {}), [f]: a === "inherit" ? undefined : a } }); applyTableKl(table); };
  const menu = [
    { label: "Align", menu: [
      { label: _menuMark(!own) + "Table default", action: () => setAlign("inherit") },
      ...ALIGNS.map(([a, l]) => ({ label: _menuMark(own === a) + l, action: () => setAlign(a) })),
    ] },
  ];
  if (_timeCols.has(key + "|" + f)) {
    const cur = ((kl.time || {})[f] || {}).mode || "";
    const title = column.getDefinition().title || f;
    const setTime = (m) => { const t = { ...(kl.time || {}) }; if (m) t[f] = { mode: m, title }; else delete t[f]; saveTableKl(table, { time: t }); table.getRows().forEach((r) => r.reformat()); };
    menu.push({ label: "Times", menu: [
      { label: _menuMark(!cur) + "Default (header button)", action: () => setTime("") },
      { label: _menuMark(cur === "rel") + "How long ago", action: () => setTime("rel") },
      { label: _menuMark(cur === "abs") + "Date", action: () => setTime("abs") },
      { label: _menuMark(cur === "both") + "Date and how long ago", action: () => setTime("both") },
    ] });
  }
  const pinned = (kl.frozen || []).includes(f);
  menu.push(
    { label: "Size to fit", action: () => column.setWidth(true) },
    { label: "Size all to fit", action: () => table.getColumns().forEach((c) => c.isVisible() && c.setWidth(true)) },
    { label: pinned ? "Unpin" : "Pin to left", action: () => {
      const frozen = (kl.frozen || []).filter((x) => x !== f);
      if (!pinned) {
        frozen.push(f);
        const first = table.getColumns().find((c) => !(kl.frozen || []).includes(c.getField()));
        if (first && first !== column) table.moveColumn(column, first, false);
      }
      saveTableKl(table, { frozen });
      applyTableKl(table);
    } },
    { separator: true },
    { label: "Columns", menu: colVisibilityMenu(e, column) },
    { label: "Table layout…", action: () => window.dispatchEvent(new CustomEvent("kl-table-layout", { detail: { key } })) },
    { label: "Reset layout", action: () => resetTableLayout(key) },
  );
  return menu;
}
// Widths, order, hidden columns, sort and the choices above go back to the defaults.
function resetTableLayout(key) {
  delete TABLE_PREFS[key];
  clearTimeout(_persistTimers[key]);
  getJson("/api/settings/table", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: key, config: null }) }).catch(() => {});
  if (_prefsChannel) _prefsChannel.postMessage({ id: key, config: null });
  window.dispatchEvent(new CustomEvent("kl-table-rebuild"));
}
// Tabulator opens a submenu only on click. Hovering one opens it after a short pause; it then stays
// open like a clicked one, until another submenu opens, a click lands outside or Escape.
let _submenuTimer = null, _submenuOpen = null;
document.addEventListener("mouseover", (e) => {
  const item = e.target.closest && e.target.closest(".tabulator-menu .tabulator-menu-item");
  if (!item) return;
  clearTimeout(_submenuTimer);
  if (!item.classList.contains("tabulator-menu-item-submenu") || item === _submenuOpen) return;
  _submenuTimer = setTimeout(() => {
    if (item.isConnected && item.matches(":hover")) { _submenuOpen = item; item.click(); }
  }, 150);
});
document.addEventListener("click", (e) => {
  const item = e.isTrusted && e.target.closest && e.target.closest(".tabulator-menu-item-submenu");
  if (item) { clearTimeout(_submenuTimer); _submenuOpen = item; }
}, true);

// Tabulator sorter for list cells (pins, retention reasons): by how many entries, then by their text.
const listSorter = (a, b) => {
  const la = (a || []).length, lb = (b || []).length;
  return la !== lb ? la - lb : (a || []).join(",").localeCompare((b || []).join(","));
};

const setTable = (id, data, columns, opts = {}) => {
  const el = document.querySelector(id);
  if (!el) return null;
  const { onRowDblClick, rowMenu, initialSort, persistSort, onSelect, ...rest } = opts;
  if (el._tabulator) { try { el._tabulator.destroy(); } catch (e) {} el._tabulator = null; }
  // Multi-select tables select the standard way: click = that row only, Ctrl/Cmd+click adds or
  // removes one, Shift+click selects the range from the last clicked row.
  const multi = rest.selectableRows === true;
  if (multi && !rest.selectableRowsRangeMode) rest.selectableRowsRangeMode = "click";
  // Right-click on a row outside the selection makes it the selection first, so the menu always
  // acts on what is highlighted. A menu may be a list or a function of (event, row).
  const menu = rowMenu && multi
    ? (e, row) => {
        if (!row.isSelected()) { row.getTable().deselectRow(); row.select(); }
        return typeof rowMenu === "function" ? rowMenu(e, row) : rowMenu;
      }
    : rowMenu;
  const key = "tabulator-" + (rest.persistenceID || id.replace("#", ""));
  const kl = tableKl(key);
  columns.forEach((c) => {
    c.headerContextMenu = colHeaderMenu;
    if (c.headerTooltip === undefined) c.headerTooltip = true;   // full title on hover when a header is clipped
    if (!c.sorter && data && data.length && typeof data[0][c.field] === "number") c.sorter = "number";
    if (!c.field) return;
    if (!(key + "|" + c.field in _origAlign)) _origAlign[key + "|" + c.field] = c.hozAlign || "";
    const a = _hoz(key, c.field);
    if (a) { c.hozAlign = a; c.headerHozAlign = a; }
    if ((kl.frozen || []).includes(c.field)) c.frozen = true;
  });
  const baseLayout = rest.layout || "fitColumns";
  delete rest.layout;
  const table = new Tabulator(id, {
    data, columns,
    layout: kl.layout === "fit" ? "fitData" : baseLayout,
    height: "100%",
    movableColumns: true,
    selectableRows: 1,
    placeholder: "Nothing to show",
    // Titles are not saved: a renamed column would keep its old title for anyone with a saved layout.
    persistence: { columns: ["width", "visible"], sort: persistSort !== false },
    persistenceID: id.replace("#", ""),
    persistenceReaderFunc: persistReader,
    persistenceWriterFunc: persistWriter,
    initialSort: initialSort || undefined,
    rowContextMenu: menu || undefined,
    ...rest,
  });
  // Shift+click selects a range of rows; stop the browser also selecting the text in between.
  if (multi && !el._shiftGuard) {
    el._shiftGuard = true;
    el.addEventListener("mousedown", (e) => { if (e.shiftKey && e.target.closest(".tabulator-row")) e.preventDefault(); });
  }
  if (onRowDblClick) table.on("rowDblClick", onRowDblClick);
  if (onSelect) table.on("rowSelectionChanged", (data) => onSelect(data));
  // A table built while its tab is hidden measures a zero-height container and lays out no
  // rows - it holds the data and shows nothing, with no error. One redraw once building
  // finishes re-measures against the real container.
  table.on("tableBuilt", () => table.redraw(true));
  el._tabulator = table;
  table._klLayout = baseLayout;
  _tablesByPid[key] = table;
  return table;
};

// Close an open table menu. Tabulator's own Escape check compares e.key with 27, so it never fires;
// a mousedown on the page is the event its menus close on.
const closeTableMenu = () => {
  if (!document.querySelector(".tabulator-popup-container")) return false;
  document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  return true;
};

// Download names: kopialens-<what>-<about>-<repo>-<date-time>.<ext>, lowercase a-z 0-9 . _ - only.
// <about> (the file, folder or search words) is cut at a word near 24 characters. Twin of app/core/exportname.py.
const _slug = (text, limit = 0) => {
  let s = String(text || "").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/[-._]*-[-._]*/g, "-").replace(/^[-._]+|[-._]+$/g, "");
  if (limit && s.length > limit) {
    const cut = s.lastIndexOf("-", limit);
    s = (cut >= limit / 2 ? s.slice(0, cut) : s.slice(0, limit)).replace(/[-._]+$/, "");
  }
  return s;
};
const aboutOf = (names) => {
  const list = (names || []).filter(Boolean);
  if (!list.length) return "";
  const first = String(list[0]).replace(/[\\/]+$/, "").split(/[\\/]/).pop() || list[0];
  return first + (list.length > 1 ? `-and-${list.length - 1}-more` : "");
};
const exportName = (what, about = "", repo = "", ext = "csv") => {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
  const tail = String(about || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop();   // never a whole path
  const more = /-and-\d+-more$/.exec(tail);
  const head = _slug(more ? tail.slice(0, more.index) : tail, 24);
  const stem = ["kopialens", _slug(what), head + (more && head ? more[0] : ""), _slug(repo, 16)].filter(Boolean).join("-")
    .slice(0, 100 - stamp.length - ext.length - 2).replace(/[-._]+$/, "");
  return `${stem}-${stamp}.${ext}`;
};
// ``name``: a full file name from exportName (or a bare stem, given .csv).
const downloadTable = (id, name) => {
  const el = document.querySelector(id);
  if (el && el._tabulator) el._tabulator.download("csv", /\.csv$/.test(name) ? name : `${name}.csv`);
};
