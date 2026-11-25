// Show/hide button on every password box, including boxes added later. Self-contained: the
// sign-in page inlines this file as is.
(function () {
  const EYE = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/></svg>';
  const EYE_OFF = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 2l12 12M6.6 6.6a2 2 0 0 0 2.8 2.8M4.3 4.4C2.6 5.6 1.5 8 1.5 8S4 12.5 8 12.5c1.3 0 2.5-.5 3.5-1.1M7 3.6c.3 0 .7-.1 1-.1 4 0 6.5 4.5 6.5 4.5s-.5 1-1.5 2.1"/></svg>';
  const CSS = ".kl-pw{position:relative;display:flex}.kl-pw>input{flex:1;min-width:0;padding-right:30px}"
    + ".kl-pw-eye{position:absolute;right:4px;top:50%;transform:translateY(-50%);width:24px;height:24px;display:flex;align-items:center;justify-content:center;"
    + "border:0;border-radius:4px;background:transparent;color:var(--muted,#78716c);cursor:pointer;padding:0}"
    + ".kl-pw-eye:hover{color:var(--fg,#1c1917)}";

  function add(inp) {
    if (inp.dataset.pwEye || !inp.parentNode) return;
    inp.dataset.pwEye = "1";
    const wrap = document.createElement("span");
    wrap.className = "kl-pw";
    inp.parentNode.insertBefore(wrap, inp);
    wrap.appendChild(inp);
    const b = document.createElement("button");
    b.type = "button";
    b.className = "kl-pw-eye";
    // Inside a <label> the button's name would join the box's own name, so there it is mouse-only.
    const inLabel = !!inp.closest("label");
    if (inLabel) { b.tabIndex = -1; b.setAttribute("aria-hidden", "true"); }
    const sync = () => {
      const shown = inp.type === "text";
      b.innerHTML = shown ? EYE_OFF : EYE;
      b.title = shown ? "Hide password" : "Show password";
      if (!inLabel) b.setAttribute("aria-label", b.title);
      b.setAttribute("aria-pressed", shown ? "true" : "false");
    };
    b.addEventListener("click", () => { inp.type = inp.type === "password" ? "text" : "password"; sync(); inp.focus(); });
    sync();
    wrap.appendChild(b);
  }

  function scan() { document.querySelectorAll("input[type=password]:not([data-pw-eye])").forEach(add); }

  function start() {
    const st = document.createElement("style");
    st.textContent = CSS;
    document.head.appendChild(st);
    scan();
    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; scan(); });
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
