// Rule-list diff viewer: one component wherever a policy's ignore list changes.
// "changes" lists added/removed rules (position ignored); "full" shows the list as kopia
// stores it. With ! rules present, order changes meaning, so moved rules are flagged.
const diffviewPart = {

    diffSetMode(m) {
      this.diffMode = m;
      try { localStorage.setItem("kl-diffmode", m); } catch (e) { /* storage unavailable */ }
    },

    diffRows(before, after, mode) {
      const b = before || [], a = after || [];
      const bs = new Set(b), as = new Set(a);
      const removed = b.filter((r) => !as.has(r));
      const added = a.filter((r) => !bs.has(r));
      const ordered = [...b, ...a].some((r) => r.startsWith("!"));
      const moved = new Set();
      if (ordered) {
        const cb = b.filter((r) => as.has(r)), ca = a.filter((r) => bs.has(r));
        ca.forEach((r, i) => { if (cb[i] !== r) moved.add(r); });
      }
      if (mode === "full") {
        return [
          ...a.map((r, i) => ({ rule: r, kind: bs.has(r) ? (moved.has(r) ? "moved" : "same") : "add", n: i + 1 })),
          ...removed.map((r) => ({ rule: r, kind: "remove", n: null })),
        ];
      }
      return [
        ...added.map((r) => ({ rule: r, kind: "add" })),
        ...removed.map((r) => ({ rule: r, kind: "remove" })),
        ...[...moved].map((r) => ({ rule: r, kind: "moved" })),
      ];
    },

    diffCounts(before, after) {
      const bs = new Set(before || []), as = new Set(after || []);
      const add = (after || []).filter((r) => !bs.has(r)).length;
      const rem = (before || []).filter((r) => !as.has(r)).length;
      const n = (after || []).length;
      return `+${add} −${rem} · ${n} rule${n === 1 ? "" : "s"}`;
    },

    diffMark(kind) {
      return { add: "+", remove: "−", moved: "↕", same: "" }[kind] || "";
    },
};
