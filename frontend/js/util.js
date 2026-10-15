// The selection bar floats over the bottom of the table (true) or sits in the toolbar (false, the
// earlier behaviour, kept for comparison). css/app.css html[data-selbar="float"].
const SELBAR_FLOAT = true;
document.documentElement.dataset.selbar = SELBAR_FLOAT ? "float" : "inline";

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
const _FILE_KINDS = [
  ["file-image", "jpg jpeg png gif webp heic heif bmp tif tiff svg ico raw cr2 cr3 nef arw dng psd"],
  ["file-audio", "mp3 flac wav m4a aac ogg opus wma aiff alac"],
  ["file-video", "mp4 mkv mov avi wmv webm m4v mts m2ts ts flv mpg mpeg"],
  ["file-zip", "zip 7z rar tar gz tgz bz2 xz zst iso cab"],
  ["file-code", "py js ts jsx tsx json html htm css c cpp h hpp cs go rs java kt sh bat cmd ps1 xml yml yaml toml ini sql lua rb php"],
  ["file-text", "txt md log csv pdf doc docx rtf odt xls xlsx ppt pptx"],
].map(([icon, exts]) => [icon, new Set(exts.split(" "))]);
const fileIconName = (name) => {
  const m = /\.([^.\\/]+)$/.exec(String(name || ""));
  const ext = m ? m[1].toLowerCase() : "";
  const hit = ext && _FILE_KINDS.find(([, set]) => set.has(ext));
  return hit ? hit[0] : "file";
};
// The icon in front of a file or folder name in tables: Tabler's set ("t-" icons) or Phosphor's
// (""), kept switchable for comparing. Folders are tinted like the old folder emoji.
const NAME_ICON_PREFIX = "t-";
const itemIconHtml = (name, folder) => (folder
  ? iconHtml(NAME_ICON_PREFIX + "folder", "kl-folder-ic mr-1")
  : iconHtml(NAME_ICON_PREFIX + fileIconName(name), "text-muted mr-1"));
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
let TIME_FMT = localStorage.getItem("kl-timefmt") || "absolute";
const humanTime = (s) => (TIME_FMT === "relative" ? fmtRel(s) : fmtAbs(s));
const timeCell = (s) => {
  if (!s) return "—";
  const abs = fmtAbs(s), rel = fmtRel(s);
  return TIME_FMT === "relative" ? `<span title="${abs}">${rel}</span>` : `<span title="${rel}">${abs}</span>`;
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
  // A Pro refusal means the screen's licence state is stale.
  if (res.status === 403 && res.headers.get("X-KopiaLens-Pro")) window.dispatchEvent(new CustomEvent("kl-pro-denied"));
  if (!res.ok) {
    const err = new Error((data && (data.detail || data.error)) || text || res.statusText);
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
};

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
  clearTimeout(_persistTimers[id]);
  _persistTimers[id] = setTimeout(() => {
    getJson("/api/settings/table", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, config: TABLE_PREFS[id] }),
    }).catch(() => {});
    if (_prefsChannel) _prefsChannel.postMessage({ id, config: TABLE_PREFS[id] });
  }, 600);
}
function applyTablePrefs(id, config) {
  if (!config || JSON.stringify(TABLE_PREFS[id] || null) === JSON.stringify(config)) return;
  clearTimeout(_persistTimers[id]);   // a pending save of this tab's older layout must not win
  TABLE_PREFS[id] = config;
  const t = _tablesByPid[id];
  if (!t || !t.element || !document.contains(t.element)) return;
  _applyingPrefs = true;
  try {
    if (config.columns) t.setColumnLayout(config.columns);
    if (config.sort) t.setSort(config.sort);
  } catch (e) { /* the table rebuilds from TABLE_PREFS next time */ } finally { _applyingPrefs = false; }
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
  columns.forEach((c) => {
    c.headerContextMenu = colVisibilityMenu;
    if (c.headerTooltip === undefined) c.headerTooltip = true;   // full title on hover when a header is clipped
    if (!c.sorter && data && data.length && typeof data[0][c.field] === "number") c.sorter = "number";
  });
  const table = new Tabulator(id, {
    data, columns,
    layout: "fitColumns",
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
  _tablesByPid["tabulator-" + (rest.persistenceID || id.replace("#", ""))] = table;
  return table;
};

// Close an open table menu. Tabulator's own Escape check compares e.key with 27, so it never fires;
// a mousedown on the page is the event its menus close on.
const closeTableMenu = () => {
  if (!document.querySelector(".tabulator-popup-container")) return false;
  document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  return true;
};

const downloadTable = (id, name) => {
  const el = document.querySelector(id);
  if (el && el._tabulator) el._tabulator.download("csv", `${name}.csv`);
};
