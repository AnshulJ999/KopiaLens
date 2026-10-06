const fmtBytes = (value) => {
  const n = Number(value || 0);
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} TB`;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)} KB`;
  return `${n} B`;
};
const fmtPhys = (v) => (v === null || v === undefined ? "—" : fmtBytes(v));
// A count with its noun: countOf(1, "file") -> "1 file", countOf(3, "file") -> "3 files".
const countOf = (n, one, many = one + "s") => `${Number(n || 0).toLocaleString()} ${Number(n) === 1 ? one : many}`;

// Accepts an ISO / "YYYY-MM-DD HH:MM:SS" string, a Date, or an epoch number. Epoch SECONDS (the
// backend's time.time()) are told apart from milliseconds by magnitude.
const toDate = (s) => (typeof s === "number" ? new Date(s < 1e12 ? s * 1000 : s) : new Date(s));
// One formatter each: toLocaleString builds a new one per call, which is ~15x slower on long lists.
const _absFmt = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", hour12: true });
const _dateFmt = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" });
const fmtAbs = (s) => {
  if (!s) return "";
  const d = toDate(s);
  return isNaN(d.getTime()) ? String(s).slice(0, 19) : _absFmt.format(d);
};
// Date only ("20 Jul 2024") - for day-granularity values and "first backed up" style labels.
const fmtDate = (s) => {
  if (!s) return "";
  const d = /^\d{4}-\d{2}-\d{2}$/.test(String(s)) ? new Date(`${s}T00:00:00`) : toDate(s);
  return isNaN(d.getTime()) ? String(s).slice(0, 10) : _dateFmt.format(d);
};
// A DURATION in seconds ("0.4 s", "42 s", "6 m 12 s", "1 h 05 m") - never pass one to humanTime.
const fmtDur = (sec) => {
  const n = Number(sec);
  if (!isFinite(n) || n < 0) return "";
  if (n < 10) return `${n.toFixed(1)} s`;
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

const getJson = async (url, options = {}) => {
  const res = await fetch(url, options);
  // Tolerate a non-JSON body (e.g. a plain-text 500 "Internal Server Error") instead of throwing
  // an "Unexpected token" parse error - surface the text as the message.
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
  // A sign-in that expired (or a password changed elsewhere) must land on the login page - the
  // alternative is every poll failing forever in the corner of an open tab.
  if (res.status === 401 && data && data.login) {
    location.href = data.login;
    throw new Error(data.detail || "Not signed in.");
  }
  // An encrypted index that is locked: the app shows one unlock box instead of per-view errors.
  if (res.status === 423 && data && data.locked) window.dispatchEvent(new CustomEvent("kl-locked", { detail: data }));
  // Setup closed mid-way on a server install: onboarding asks for the setup code.
  if (res.status === 403 && data && data.setup_code) window.dispatchEvent(new CustomEvent("kl-setup-code"));
  // A Pro refusal means the screen's licence state is stale.
  if (res.status === 403 && res.headers.get("X-KopiaLens-Pro")) window.dispatchEvent(new CustomEvent("kl-pro-denied"));
  if (!res.ok) throw new Error((data && (data.detail || data.error)) || text || res.statusText);
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
function persistReader(id, type) { return (TABLE_PREFS[id] && TABLE_PREFS[id][type]) || false; }
function persistWriter(id, type, data) {
  TABLE_PREFS[id] = TABLE_PREFS[id] || {};
  TABLE_PREFS[id][type] = data;
  clearTimeout(_persistTimers[id]);
  _persistTimers[id] = setTimeout(() => {
    getJson("/api/settings/table", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, config: TABLE_PREFS[id] }),
    }).catch(() => {});
  }, 600);
}

function colVisibilityMenu(e, column) {
  return column.getTable().getColumns().map((col) => ({
    label: (col.isVisible() ? "☑ " : "☐ ") + (col.getDefinition().title || col.getField()),
    action: (ev) => { ev.stopPropagation(); col.toggle(); },
  }));
}

const setTable = (id, data, columns, opts = {}) => {
  const el = document.querySelector(id);
  if (!el) return null;
  const { onRowDblClick, rowMenu, initialSort, persistSort, ...rest } = opts;
  if (el._tabulator) { try { el._tabulator.destroy(); } catch (e) {} el._tabulator = null; }
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
    persistence: { columns: true, sort: persistSort !== false },
    persistenceID: id.replace("#", ""),
    persistenceReaderFunc: persistReader,
    persistenceWriterFunc: persistWriter,
    initialSort: initialSort || undefined,
    rowContextMenu: rowMenu || undefined,
    ...rest,
  });
  if (onRowDblClick) table.on("rowDblClick", onRowDblClick);
  // A table built while its tab is hidden measures a zero-height container and lays out no
  // rows - it holds the data and shows nothing, with no error. One redraw once building
  // finishes re-measures against the real container.
  table.on("tableBuilt", () => table.redraw(true));
  el._tabulator = table;
  return table;
};

const downloadTable = (id, name) => {
  const el = document.querySelector(id);
  if (el && el._tabulator) el._tabulator.download("csv", `${name}.csv`);
};
