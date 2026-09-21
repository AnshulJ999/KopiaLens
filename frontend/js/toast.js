// One notice per action, bottom-right. Many actions report only on the Actions tab, which reads
// as "the click did nothing"; every such action raises a notice too. State lives in app.js.
const toastPart = {

    // kind: "ok" (default) | "warn". Warn stays longer (it usually carries a decision).
    klNotify(msg, kind = "ok") {
      if (!msg) return;
      const id = ++this._toastSeq;
      this.toasts = [...this.toasts, { id, msg: String(msg), kind }].slice(-3);
      setTimeout(() => this.klToastDismiss(id), kind === "ok" ? 5000 : 9000);
    },

    klToastDismiss(id) {
      this.toasts = this.toasts.filter((t) => t.id !== id);
    },
};
