// KopiaLens web demo worker: the backend (Pyodide) runs here, off the page's main thread, so the
// page stays responsive while a request is worked on. Talks to bridge.js by messages.
// Runs as a module worker (pyodide.mjs).
let py = null;
let web = null;
const post = (msg) => self.postMessage(msg);

async function fetchBytes(url, id, mb) {
  const res = await fetch(url);
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
    if (id && len) post({ type: "progress", id, mb: (mb * got) / len });
  }
  const out = new Uint8Array(got);
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}

async function init(cfg, saved) {
  const mbOf = (id) => cfg.steps.find((s) => s.id === id).mb;
  const { loadPyodide } = await import(cfg.pyodide + "pyodide.mjs");
  py = await loadPyodide({ indexURL: cfg.pyodide });
  post({ type: "done", id: "runtime" });
  // Each library file counts as it arrives (cached files report 0 bytes and count at the end).
  let libBytes = 0;
  let libSeen = null;
  try {
    libSeen = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.name.startsWith(cfg.pyodide) && /\.(whl|zip)$/.test(e.name)) libBytes += e.encodedBodySize || e.transferSize || 0;
      }
      post({ type: "progress", id: "libraries", mb: Math.min(libBytes / 1e6, mbOf("libraries") * 0.98) });
    });
    libSeen.observe({ type: "resource", buffered: true });
  } catch (e) { libSeen = null; }
  const libs = py.loadPackage(cfg.packages, { messageCallback: () => {} })
    .then(() => { if (libSeen) libSeen.disconnect(); post({ type: "done", id: "libraries" }); });
  const code = fetchBytes(cfg.base + "app.zip");
  const data = fetchBytes(cfg.base + "data.zip", "data", mbOf("data"))
    .then((b) => { post({ type: "done", id: "data" }); return b; });
  const [, codeZip, dataZip] = await Promise.all([libs, code, data]);
  post({ type: "begin", id: "start" });
  py.unpackArchive(codeZip, "zip", { extractDir: "/kl" });
  py.unpackArchive(dataZip, "zip", { extractDir: "/data" });
  py.runPython("import sys; sys.path.insert(0, '/kl')");
  web = py.pyimport("kl_web");
  if (saved) web.restore_state(py.toPy(saved));
  web.boot();
  post({ type: "ready" });
}

async function call(m) {
  try {
    // undefined, not null: Pyodide turns null into JsNull rather than None.
    const out = await web.handle(m.method, m.path, py.toPy(m.headers), m.body || undefined);
    const [status, headers, body, changed] = out;
    const reply = { type: "reply", id: m.id, status, headers, body };
    if (changed) reply.state = web.state().toJs({ dict_converter: Object.fromEntries });
    post(reply);
  } catch (err) {
    post({ type: "reply", id: m.id, error: String((err && err.message) || err) });
  }
}

self.onmessage = (e) => {
  const m = e.data;
  if (m.type === "init") {
    init(m.cfg, m.saved).catch((err) => post({ type: "error", message: String((err && err.message) || err) }));
  } else if (m.type === "call") {
    call(m);
  }
};
