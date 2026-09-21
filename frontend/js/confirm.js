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
          danger: !!opts.danger, input: opts.input || null, value: "", _resolve: resolve,
          _openedAt: Date.now(),
        };
      });
    },
    confirmResolve(v) {
      const cb = this.confirmBox;
      this.confirmBox = null;
      if (cb && cb._resolve) cb._resolve(v && cb.input ? { value: cb.value } : v);
    },
    // Enter may confirm a reversible action, but only as a deliberate press: never a held key's
    // repeats and never the same keypress that opened the dialog (a double-tap in a filter box
    // used to write a kopia policy). Danger dialogs stay click-only.
    confirmEnter(e) {
      const cb = this.confirmBox;
      if (!cb || cb.danger || e.repeat) return;
      if (Date.now() - (cb._openedAt || 0) < 500) return;
      this.confirmResolve(true);
    },
};
