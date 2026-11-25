// One notice per action, bottom-right. Many actions report only on the Actions tab, which reads
// as "the click did nothing"; every such action raises a notice too. State lives in app.js.
const toastPart = {

    // kind: "ok" (default) | "warn" | "info". Warn stays longer (it usually carries a decision);
    // info is a neutral note (neither success nor failure) and keeps the warn lifetime.
    // action: optional { label, run } shown as a button on the notice.
    klNotify(msg, kind = "ok", action = null) {
      if (!msg) return;
      this.pushNotice(msg, kind);
      const id = ++this._toastSeq;
      this.toasts = [...this.toasts, { id, msg: String(msg), kind, action }].slice(-3);
      setTimeout(() => this.klToastDismiss(id), kind === "ok" ? 5000 : 9000);
    },

    // Every notice of this session, newest first (Logs > Notices). The same text twice in a row
    // within a few seconds (a toast plus its log line) is kept once.
    pushNotice(msg, kind) {
      if (!msg) return;
      const text = String(msg), now = Date.now(), last = this.noticeLog[0];
      if (last && last.msg === text && now - last.ts < 5000) return;
      this.noticeLog = [{ ts: now, msg: text, kind }, ...this.noticeLog].slice(0, 300);
    },

    klToastDismiss(id) {
      this.toasts = this.toasts.filter((t) => t.id !== id);
    },
};
