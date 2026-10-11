// Shared confirm dialog: one component, two shapes picked by what the caller passes.
// A sentence gets the short form; a set of facts gets the detailed one. The confirm button
// states the action with its count, never "OK", and nothing is typed to confirm.
const confirmPart = {

    // opts: {title, body, facts: [[label, value], ...], callout, note, command,
    //        confirmLabel, danger, input: {label, placeholder}}. Resolves true (confirmed) /
    //        false (cancelled); with `input`, a confirm resolves {value}.
    klConfirm(opts) {
      return new Promise((resolve) => {
        this.confirmBox = {
          title: opts.title || "Are you sure?", body: opts.body || "",
          facts: opts.facts || null, callout: opts.callout || "", note: opts.note || "",
          command: opts.command || "", confirmLabel: opts.confirmLabel || "Confirm",
          danger: !!opts.danger, input: opts.input || null, value: (opts.input && opts.input.value) || "", _resolve: resolve,
          _openedAt: Date.now(), _returnTo: document.activeElement,
        };
      });
    },
    confirmResolve(v) {
      const cb = this.confirmBox;
      this.confirmBox = null;
      if (cb && cb._resolve) cb._resolve(v && cb.input ? { value: cb.value } : v);
      const back = cb && cb._returnTo;
      if (back && back.focus) setTimeout(() => { if (document.contains(back)) back.focus(); }, 0);
    },
    // First focus: the input if there is one, else Cancel on a danger dialog, else the confirm button.
    confirmFocus(root) {
      const cb = this.confirmBox;
      const el = root.querySelector("input") || root.querySelector(cb && cb.danger ? "[data-cf=cancel]" : "[data-cf=ok]");
      if (el) el.focus();
    },
    // Keep Tab inside an open dialog.
    trapTab(e, root) {
      const f = [...root.querySelectorAll("button, input, select, textarea, a[href]")].filter((el) => !el.disabled && el.offsetParent !== null);
      if (!f.length) return;
      const first = f[0], last = f[f.length - 1], at = document.activeElement;
      if (!root.contains(at)) { e.preventDefault(); first.focus(); }
      else if (e.shiftKey && at === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && at === last) { e.preventDefault(); first.focus(); }
    },
    // Enter may confirm a reversible action, but only as a deliberate press: never a held key's
    // repeats and never the same keypress that opened the dialog (a double-tap in a filter box
    // used to write a kopia policy). Danger dialogs stay click-only. Enter on Cancel or another
    // dialog button presses that button.
    confirmEnter(e) {
      const cb = this.confirmBox;
      if (!cb) return;
      const role = e.target && e.target.dataset ? e.target.dataset.cf : "";
      if (e.target && e.target.tagName === "BUTTON" && role !== "ok") return;
      if (role === "ok") e.preventDefault();
      if (cb.danger || e.repeat) return;
      if (Date.now() - (cb._openedAt || 0) < 500) return;
      this.confirmResolve(true);
    },
};
