// Page zoom inside the desktop app (a browser has its own). Ctrl +/-/0 and Ctrl+wheel, remembered
// per window. Not an Alpine part: it runs on its own and only where the Tauri bridge exists.
(() => {
  const webview = window.__TAURI__ && window.__TAURI__.webview;
  if (!webview || !webview.getCurrentWebview) return;
  const view = webview.getCurrentWebview();
  const STEPS = [0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];
  const key = "kl-zoom-" + view.label;
  const read = () => { try { return Number(localStorage.getItem(key)) || 1; } catch (e) { return 1; } };
  let level = read();

  const apply = (next, announce) => {
    level = next;
    view.setZoom(level).catch(() => {});
    try { localStorage.setItem(key, String(level)); } catch (e) { /* zoom still applies this session */ }
    window.dispatchEvent(new CustomEvent("kl-zoom", { detail: level }));
    if (!announce) return;
    const el = document.getElementById("app");
    const app = el && window.Alpine && Alpine.$data(el);
    if (app && app.klNotify) app.klNotify("Zoom " + Math.round(level * 100) + "%");
  };
  const step = (dir) => {
    const i = STEPS.findIndex((s) => s >= level - 1e-6);
    const at = i < 0 ? STEPS.length - 1 : i;
    const exact = Math.abs(STEPS[at] - level) < 1e-6;
    const next = dir > 0 ? STEPS[Math.min(STEPS.length - 1, exact ? at + 1 : at)] : STEPS[Math.max(0, at - 1)];
    if (next !== level) apply(next, true);
  };

  window.klZoom = { level: () => level, step, reset: () => { if (level !== 1) apply(1, true); } };   // the Help menu's zoom row
  if (level !== 1) apply(level, false);
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
    if (e.key === "=" || e.key === "+") step(1);
    else if (e.key === "-" || e.key === "_") step(-1);
    else if (e.key === "0") { if (level !== 1) apply(1, true); }
    else return;
    e.preventDefault();
  });
  let lastWheel = 0;   // a touchpad pinch fires dozens of wheel events; one step per burst slice
  document.addEventListener("wheel", (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    if (e.timeStamp - lastWheel < 150) return;
    lastWheel = e.timeStamp;
    step(e.deltaY < 0 ? 1 : -1);
  }, { passive: false });
})();
