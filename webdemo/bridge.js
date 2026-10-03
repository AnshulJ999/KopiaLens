// KopiaLens web demo: the real backend runs in a worker (worker.js, Pyodide) and answers every
// /api/ call. Loaded in <head>, before the app's own scripts, so their first fetch waits here.
(function () {
  "use strict";
  const cfg = window.KL_WEB_DEMO;
  const realFetch = window.fetch.bind(window);
  const stateKey = "kl-webdemo:" + cfg.version;
  const debug = new URLSearchParams(location.search).has("debug");

  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(stateKey) || "null"); } catch (e) { saved = null; }

  // The server stamps the saved appearance onto <html>; a static page does it here instead.
  try {
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
    const pct = Math.round((100 * got) / total);
    el("kl-web-bar").style.width = pct + "%";
    el("kl-web-bar").parentElement.setAttribute("aria-valuenow", String(pct));
    el("kl-web-size").textContent = `${mb(got)} of about ${mb(total)}`;
    el("kl-web-time").textContent = Math.round((Date.now() - t0) / 1000) + " s";
    for (const s of steps) {
      el("kl-web-step-" + s.id).dataset.state = (doneMb[s.id] || 0) >= s.mb ? "done" : s.active ? "active" : "wait";
    }
  }
  const timer = setInterval(() => { if (!failed) render(); }, 500);
  const step = (id) => steps.find((s) => s.id === id);
  function begin(id) { step(id).active = true; render(); }
  function finish(id) { doneMb[id] = step(id).mb; step(id).active = false; render(); }

  function showError(message) {
    failed = true;
    clearInterval(timer);
    rejectReady(new Error(message));
    const box = el("kl-web-loading");
    if (!box) return;
    box.dataset.state = "error";
    el("kl-web-error-detail").textContent = message;
  }

  // ---- the worker ----
  const worker = new Worker(cfg.base + "worker.js?v=" + encodeURIComponent(cfg.version), { type: "module" });
  const pending = new Map();
  let nextId = 1;

  worker.onmessage = (e) => {
    const m = e.data;
    if (m.type === "reply") {
      const p = pending.get(m.id);
      pending.delete(m.id);
      if (!p) return;
      if (m.state) {
        try { localStorage.setItem(stateKey, JSON.stringify(m.state)); } catch (err) { /* storage off: settings last until reload */ }
      }
      if (m.error) p.reject(new Error(m.error)); else p.resolve(m);
    } else if (m.type === "progress") {
      doneMb[m.id] = m.mb;
    } else if (m.type === "done") {
      finish(m.id);
    } else if (m.type === "begin") {
      begin(m.id);
    } else if (m.type === "ready") {
      finish("start");
      clearInterval(timer);
      const box = el("kl-web-loading");
      if (box) box.remove();
      resolveReady();
    } else if (m.type === "error") {
      console.error(m.message);
      showError(m.message);
    }
  };
  worker.onerror = (e) => { console.error(e); showError(e.message || "The demo's worker stopped."); };

  ["runtime", "libraries", "data"].forEach((id) => { step(id).active = true; });
  // Absolute: inside the worker, relative paths resolve from the worker's own folder.
  worker.postMessage({ type: "init", cfg: { ...cfg, base: new URL(cfg.base, location.href).href }, saved });

  // ---- the API, answered by the worker ----
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
    const body = req.method === "GET" || req.method === "HEAD" ? undefined : new Uint8Array(await req.arrayBuffer());
    const t = performance.now();
    if (debug) console.debug("[kl-web] >", req.method, path);
    const id = nextId++;
    const out = await new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      worker.postMessage({ type: "call", id, method: req.method, path, headers, body });
    });
    if (debug) console.debug("[kl-web] <", out.status, req.method, path, Math.round(performance.now() - t) + " ms");
    const noBody = out.status === 204 || out.status === 304 || req.method === "HEAD";
    return new Response(noBody ? null : out.body, { status: out.status, headers: out.headers });
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
})();
