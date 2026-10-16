// In-app docs browser: the docs/ pages (the same files as the repo and the website), shown as a
// popup or a side panel (Appearance > Docs), or full page in its own window (/?docs=<page>#<section>).
// "?" buttons call openDocs(page, section).

// Heading ids the way GitHub makes them, so links written for GitHub work here too.
const docSlug = (text) => text.trim().toLowerCase().replace(/<[^>]+>/g, "").replace(/[^\p{L}\p{N}\s-]/gu, "").replace(/\s/g, "-");

// Settings sections whose topic already has its own page; the rest are sections of settings.md.
const SETTINGS_DOCS = {
  notifications: ["notifications"], repositories: ["repositories"], account: ["sign-in"],
  encryption: ["encryption"], remote: ["remote-access"], licence: ["licensing"], updates: ["usage-stats"],
  general: ["settings", "general"], appearance: ["settings", "appearance"], desktop: ["settings", "desktop-app"],
  indexing: ["settings", "indexing"], refresh: ["settings", "indexing"], backblaze: ["settings", "backblaze"],
  writemode: ["settings", "permissions"], tokens: ["api-tokens"], backups: ["settings", "config"], advanced: ["settings", "advanced"],
  about: ["settings", "about"],
};

let _docsRes = null;   // the last search's results (docsResults runs several times per render)

const docsPart = {

    async _docsData() {
      if (!this._docsCache) this._docsCache = getJson("/api/help/docs").catch((e) => { this._docsCache = null; throw e; });
      return this._docsCache;
    },
    // page: a docs file name without .md ("README" = the contents page); section: a heading id.
    async openDocs(page, section) {
      this.helpMenu = false;
      const view = this.docs ? this.docs.view : (this._docsWindow ? "window" : this.docsView);
      this.docs = { view, page: page || "README", section: section || "", query: "", loading: true, error: "" };
      try {
        this._docsLoaded = await this._docsData();
        if (!this.docs) return;
        if (!this._docsLoaded.pages[this.docs.page]) this.docs.page = "README";
        this.docs.loading = false;
        this._docsShow();
      } catch (e) {
        if (this.docs) { this.docs.loading = false; this.docs.error = e.message || String(e); }
      }
    },
    openSettingsDocs() {
      const [page, section] = SETTINGS_DOCS[this.settingsTab] || ["settings"];
      this.openDocs(page, section);
    },
    closeDocs() {
      this.docs = null;
      if (this._docsWindow) { this._docsWindow = false; history.replaceState(null, "", location.pathname); }
    },
    docsSetView(view) {
      if (!this.docs) return;
      this.docs.view = view;
      if (view !== "window") { this.docsView = view; this.klSet({ docs_view: view }); }
    },
    docsNewWindow() {
      if (!this.docs) return;
      const d = this.docs;
      window.open(`${location.origin}${appUrl("/")}?docs=${encodeURIComponent(d.page)}${d.section ? "#" + encodeURIComponent(d.section) : ""}`, "_blank", "noopener");
    },
    docsPages(group) { return (group.pages || []).filter((p) => this._docsLoaded && this._docsLoaded.pages[p]); },
    docsTitle(page) { const p = this._docsLoaded && this._docsLoaded.pages[page]; return p ? p.title : page; },
    // The current page's second-level headings, for the contents list.
    docsSections() {
      const p = this.docs && this._docsLoaded && this._docsLoaded.pages[this.docs.page];
      if (!p) return [];
      return p.md.split("\n").filter((l) => /^## /.test(l)).map((l) => ({ id: docSlug(l.slice(3)), title: l.slice(3).trim() }));
    },
    docsHtml() {
      const p = this.docs && this._docsLoaded && this._docsLoaded.pages[this.docs.page];
      if (!p) return "";
      const files = new Set(this._docsLoaded.files || []);
      const renderer = {
        heading({ tokens, depth, text }) {
          return `<h${depth} id="doc-${docSlug(text)}">${this.parser.parseInline(tokens)}</h${depth}>`;
        },
        link({ href, title, tokens }) {
          const inner = this.parser.parseInline(tokens);
          const tip = title ? ` title="${escHtml(title)}"` : "";
          const m = /^([\w.-]+)\.md(?:#(.*))?$/.exec(href || "") || /^#(.*)$/.exec(href || "");
          if (m && m.length === 3) return `<a href="#" data-doc="${escHtml(m[1])}" data-sec="${escHtml(m[2] || "")}"${tip}>${inner}</a>`;
          if (m) return `<a href="#" data-sec="${escHtml(m[1])}"${tip}>${inner}</a>`;
          return `<a href="${escHtml(href)}" target="_blank" rel="noopener"${tip}>${inner}</a>`;
        },
        // A screenshot not taken yet shows as a labelled box until its file exists in docs/.
        image({ href, text }) {
          if (/^https?:/.test(href || "")) return `<img src="${escHtml(href)}" alt="${escHtml(text)}">`;
          if (!files.has(href)) return `<span class="kl-doc-ph">${escHtml(text || "Screenshot")}</span>`;
          return `<img src="${appUrl("/api/help/docs/file")}?path=${encodeURIComponent(href)}" alt="${escHtml(text)}">`;
        },
      };
      return window.marked.marked.parse(p.md, { gfm: true, renderer: Object.assign(new window.marked.Renderer(), renderer) });
    },
    // After a page renders, scroll to the section.
    async _docsShow() {
      await this.$nextTick();
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));   // x-show reveals on a frame
      const body = document.getElementById("docsBody");
      if (!body || !this.docs) return;
      const target = this.docs.section && document.getElementById("doc-" + this.docs.section);
      if (target) target.scrollIntoView({ block: "start" }); else body.scrollTop = 0;
    },
    docsGo(page, section) {
      if (!this.docs) return;
      this.docs.query = "";
      this.docs.page = page || this.docs.page;
      this.docs.section = section || "";
      this._docsShow();
    },
    docsClick(e) {
      const a = e.target.closest("a[data-sec], a[data-doc]");
      if (!a) return;
      e.preventDefault();
      this.docsGo(a.dataset.doc || this.docs.page, a.dataset.sec || "");
    },
    // Search across every page; each heading's section is one result. Best first: the exact phrase,
    // then every word in any order, then close matches (a word's start, or one or two letters off).
    docsResults() {
      const q = this.docs && this.docs.query.trim().toLowerCase();
      if (!q || !this._docsLoaded) return [];
      const src = Alpine.raw(this._docsLoaded);
      if (_docsRes && _docsRes.q === q && _docsRes.src === src) return _docsRes.out;
      const words = q.split(/\s+/);
      const hits = [];
      for (const [page, p] of Object.entries(this._docsLoaded.pages)) {
        let sec = { id: "", title: p.title, text: [] };
        const flush = () => {
          const text = sec.text.join(" ").replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[#*`>|_]+|-{3,}/g, " ").replace(/\s+/g, " ").trim();
          const hay = (sec.title + " " + text).toLowerCase();
          const m = docsMatch(hay, q, words);
          if (m.tier) {
            const lower = text.toLowerCase();
            const at = Math.max(0, lower.indexOf(m.at) - 60);
            hits.push({ page, section: sec.id, title: p.title + (sec.id ? " › " + sec.title : ""), close: m.tier === 1,
              snippet: (at ? "…" : "") + text.slice(at, at + 160), rank: m.tier * 2 + (docsMatch(sec.title.toLowerCase(), q, words).tier >= m.tier ? 1 : 0), n: hits.length });
          }
        };
        for (const line of p.md.split("\n")) {
          const h = /^#{2,3} (.*)/.exec(line);
          if (h) { flush(); sec = { id: docSlug(h[1]), title: h[1].trim(), text: [] }; } else if (!/^# /.test(line)) sec.text.push(line);
        }
        flush();
      }
      const out = hits.sort((x, y) => y.rank - x.rank || x.n - y.n).slice(0, 50);
      _docsRes = { q, src, out };
      return out;
    },
};

// How a section matches a docs search: tier 3 = the exact phrase, 2 = every word, 1 = every word
// closely (a word starting with it, or one letter off; two for words of 7+ letters), 0 = no match.
// `at` is the text to centre the snippet on.
function docsMatch(hay, q, words) {
  if (hay.includes(q)) return { tier: 3, at: q };
  if (words.every((w) => hay.includes(w))) return { tier: 2, at: words[0] };
  const tokens = hay.match(/[a-z0-9]+/g) || [];
  let at = "";
  for (const w of words) {
    if (hay.includes(w)) { at = at || w; continue; }
    const t = tokens.find((x) => (w.length >= 3 && x.startsWith(w)) || _within(x, w, w.length >= 7 ? 2 : 1));
    if (!t || w.length < 3) return { tier: 0, at: "" };
    at = at || t;
  }
  return { tier: 1, at };
}
// Edit distance between a and b is at most k (insert, delete, change one letter).
function _within(a, b, k) {
  if (Math.abs(a.length - b.length) > k) return false;
  let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      best = Math.min(best, cur[j]);
    }
    if (best > k) return false;
    prev = cur;
  }
  return prev[b.length] <= k;
}
