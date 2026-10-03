// KopiaLens web demo: runs the real backend in the page (Pyodide) and answers every /api/
// call from it. Loaded in <head>, before the app's own scripts, so their first fetch waits here.
(function () {
  "use strict";
  const cfg = window.KL_WEB_DEMO;
  const realFetch = window.fetch.bind(window);
  const stateKey = "kl-webdemo:" + cfg.version;
  const debug = new URLSearchParams(location.search).has("debug");

  // The server stamps the saved appearance onto <html>; a static page does it here instead.
  try {
    const saved = JSON.parse(localStorage.getItem(stateKey) || "null");
    const kl = (saved && saved["settings.json"] && JSON.parse(saved["settings.json"]).kopialens) || {};
    const root = document.documentElement;
    if (kl.theme) root.dataset.theme = kl.theme;
    if (kl.density) root.dataset.density = kl.density;
    if (kl.sort_arrows === "all") root.dataset.sortarrows = "all";
    const t = root.dataset.theme;
    const dark = t && t !== "system" ? ["ember", "midnight", "pine", "obsidian"].includes(t)
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.classList.toggle("dark", dark);
  } catch (e) { /* no saved appearance: the page default stands */ }
  let web = null;
  let resolveReady, rejectReady;
  const ready = new Promise((res, rej) => { resolveReady = res; rejectReady = rej; });
  ready.catch(() => {});

  // ---- loading screen ----
  const t0 = Date.now();
  const steps = cfg.steps;                    // [{id, label, mb}]
  const doneMb = {};
  let failed = false;
  const el = (id) => document.getElementById(id);
  const mb = (n) => n.toFixed(n < 10 ? 1 : 0) + " MB";

  function render() {
    const box = el("kl-web-loading");
    if (!box) return;
    const total = steps.reduce((n, s) => n + s.mb, 0);
    const got = steps.reduce((n, s) => n + Math.min(s.mb, doneMb[s.id] || 0), 0);
    el("kl-web-bar").style.width = Math.round((100 * got) / total) + "%";
    el("kl-web-bar").parentElement.setAttribute("aria-valuenow", String(Math.round((100 * got) / total)));
    el("kl-web-size").textContent = `${mb(got)} of about ${mb(total)}`;
    el("kl-web-time").textContent = Math.round((Date.now() - t0) / 1000) + " s";
    for (const s of steps) {
      const row = el("kl-web-step-" + s.id);
      const state = (doneMb[s.id] || 0) >= s.mb ? "done" : s.active ? "active" : "wait";
      row.dataset.state = state;
    }
  }
  const timer = setInterval(() => { if (!failed) render(); }, 500);
  function begin(id) { steps.find((s) => s.id === id).active = true; render(); }
  function finish(id) { const s = steps.find((x) => x.id === id); doneMb[id] = s.mb; s.active = false; render(); }

  function showError(err) {
    failed = true;
    clearInterval(timer);
    const box = el("kl-web-loading");
    if (!box) return;
    box.dataset.state = "error";
    el("kl-web-error-detail").textContent = String((err && err.message) || err);
  }

  async function fetchBytes(url, id) {
    const res = await realFetch(url);
    if (!res.ok) throw new Error(`${url}: ${res.status} ${res.statusText}`);
    const len = Number(res.headers.get("content-length")) || 0;
    const reader = res.body.getReader();
    const parts = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value);
      got += value.length;
      if (id && len) doneMb[id] = (steps.find((s) => s.id === id).mb * got) / len;
    }
    const out = new Uint8Array(got);
    let at = 0;
    for (const p of parts) { out.set(p, at); at += p.length; }
    return out;
  }

  function loadScript(src) {
    return new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = res;
      s.onerror = () => rej(new Error("Couldn't load " + src));
      document.head.appendChild(s);
    });
  }

  async function boot() {
    begin("runtime");
    await loadScript(cfg.pyodide + "pyodide.js");
    const py = await window.loadPyodide({ indexURL: cfg.pyodide });
    finish("runtime");
    begin("libraries");
    begin("data");
    // Each library file counts as it arrives (cached files report 0 bytes and count at the end).
    let libBytes = 0;
    const libSeen = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.name.startsWith(cfg.pyodide) && /\.(whl|zip)$/.test(e.name)) libBytes += e.encodedBodySize || e.transferSize || 0;
      }
      doneMb.libraries = Math.min(libBytes / 1e6, steps.find((s) => s.id === "libraries").mb * 0.98);
    });
    try { libSeen.observe({ type: "resource", buffered: true }); } catch (e) { /* older browser: step counts when done */ }
    const libs = py.loadPackage(cfg.packages, { messageCallback: () => {} })
      .then(() => { libSeen.disconnect(); finish("libraries"); });
    const code = fetchBytes(cfg.base + "app.zip");
    const data = fetchBytes(cfg.base + "data.zip", "data").then((b) => { finish("data"); return b; });
    const [, codeZip, dataZip] = await Promise.all([libs, code, data]);
    begin("start");
    py.unpackArchive(codeZip, "zip", { extractDir: "/kl" });
    py.unpackArchive(dataZip, "zip", { extractDir: "/data" });
    py.runPython("import sys; sys.path.insert(0, '/kl')");
    web = py.pyimport("kl_web");
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(stateKey) || "null"); } catch (e) { saved = null; }
    if (saved) web.restore_state(py.toPy(saved));
    web.boot();
    window.klWebPy = py;
    finish("start");
    clearInterval(timer);
    const box = el("kl-web-loading");
    if (box) box.remove();
    resolveReady();
  }

  function saveState() {
    try { localStorage.setItem(stateKey, JSON.stringify(web.state().toJs({ dict_converter: Object.fromEntries }))); } catch (e) { /* storage off: settings last until reload */ }
  }

  // ---- the API, answered in the page ----
  const apiPath = (input) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const u = new URL(raw, location.href);
    return u.origin === location.origin && u.pathname.startsWith("/api/") ? u.pathname + u.search : null;
  };

  async function apiFetch(path, input, init) {
    await ready;
    const req = new Request(new URL(path, location.origin), typeof input === "string" || input instanceof URL ? init : new Request(input, init));
    const headers = {};
    req.headers.forEach((v, k) => { headers[k] = v; });
    // undefined, not null: Pyodide turns null into JsNull rather than None.
    const body = req.method === "GET" || req.method === "HEAD" ? undefined : new Uint8Array(await req.arrayBuffer());
    const t = performance.now();
    if (debug) console.debug("[kl-web] >", req.method, path);
    const out = await web.handle(req.method, path, window.klWebPy.toPy(headers), body);
    const [status, hdrs, content, changed] = out;
    if (debug) console.debug("[kl-web] <", status, req.method, path, Math.round(performance.now() - t) + " ms");
    if (changed) saveState();
    const noBody = status === 204 || status === 304 || req.method === "HEAD";
    return new Response(noBody ? null : content, { status, headers: hdrs });
  }

  window.fetch = function (input, init) {
    const path = apiPath(input);
    return path ? apiFetch(path, input, init) : realFetch(input, init);
  };

  // CSV exports open a server URL in a new tab; here they become a download.
  const realOpen = window.open.bind(window);
  window.open = function (url, ...rest) {
    const path = typeof url === "string" ? apiPath(url) : null;
    if (!path) return realOpen(url, ...rest);
    apiFetch(path, url).then(async (res) => {
      const name = /filename="?([^";]+)"?/.exec(res.headers.get("content-disposition") || "");
      const a = document.createElement("a");
      a.href = URL.createObjectURL(await res.blob());
      a.download = name ? name[1] : "export.csv";
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    });
    return null;
  };

  document.addEventListener("DOMContentLoaded", () => {
    render();
    const retry = el("kl-web-retry");
    if (retry) retry.addEventListener("click", () => location.reload());
  });

  boot().catch((err) => { console.error(err); showError(err); rejectReady(err); });
})();
