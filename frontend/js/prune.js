// Prune versions of a file (rows = versions) or snapshots of a folder (rows = snapshots): quick
// picks set the ticks, the server prices the ticked set, the job removes exactly those rows.
const prunePart = {

    async openPrune(fullPath, source = null) {
      if (!fullPath) return;
      const seq = ++this._pruneSeq;
      ++this._prunePriceSeq;
      this.prune = {
        loading: true, error: "", fullPath, source, rows: [], holders: [], tick: {},
        keep: { firstLast: false, newest: false, monthly: false, pinned: true },
        newestN: 3, biggestN: 3, price: null, priceLoading: false, dry: this.klDryState(),
      };
      this.showPrune = true;
      try {
        const r = await getJson("/api/prune/plan", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, full_path: fullPath, source }) });
        if (seq !== this._pruneSeq || !this.prune) return;
        if (!r.found) { this.prune.loading = false; this.prune.error = "No snapshot holds this path any more."; return; }
        Object.assign(this.prune, r, { loading: false });
        if (this.prune.rows.length < 2) this.prune.error = "There is only one " + this.pruneNoun() + ". Nothing to prune.";
      } catch (e) {
        if (seq === this._pruneSeq && this.prune) { this.prune.loading = false; this.prune.error = e.message || String(e); }
      }
    },

    closePrune() {
      ++this._pruneSeq;
      ++this._prunePriceSeq;
      if (this.prune) this.klDryClear(this.prune.dry);
      this.showPrune = false;
      this.prune = null;
    },

    pruneNoun() { return this.prune && this.prune.kind === "folder" ? "snapshot" : "version"; },
    pruneTitle() {
      const p = this.prune;
      if (!p) return "";
      return (p.kind === "folder" ? "Prune snapshots of " : "Prune versions of ") + baseName(p.full_path || p.fullPath);
    },
    pruneSubtitle() {
      const rows = (this.prune && this.prune.rows) || [];
      if (!rows.length) return "";
      return countOf(rows.length, this.pruneNoun()) + ", " + fmtDate(rows[0].first) + " to " + fmtDate(rows[rows.length - 1].last);
    },
    pruneWhen(r) {
      if (r.snapshots.length === 1) return humanTime(r.first);
      return fmtDate(r.first) + " to " + fmtDate(r.last) + " · " + r.snapshots.length;
    },
    pruneRowsNewestFirst() { return [...this.prune.rows].reverse(); },
    pruneBarHeight(r) {
      const max = Math.max(1, ...this.prune.rows.map((x) => x.size || 0));
      return Math.max(4, Math.round(((r.size || 0) / max) * 100));
    },
    pruneTickCount() { return Object.values((this.prune && this.prune.tick) || {}).filter(Boolean).length; },
    pruneElsewhereTicked() { return this.prune.rows.filter((r) => this.prune.tick[r.id] && r.elsewhere).length; },
    pruneIsBiggest(r) {
      if (!r.frees) return false;
      const top = [...this.prune.rows].sort((a, b) => b.frees - a.frees).slice(0, 3);
      return top.some((x) => x.id === r.id);
    },

    // ---- ticks ----
    pruneToggle(id) {
      const p = this.prune;
      p.tick = { ...p.tick, [id]: !p.tick[id] };
      this._pruneChanged();
    },
    pruneClear() {
      const p = this.prune;
      p.tick = {};
      p.keep = { ...p.keep, firstLast: false, newest: false, monthly: false };
      this._pruneChanged();
    },
    pruneKeep(rule) {
      const p = this.prune;
      p.keep = { ...p.keep, [rule]: !p.keep[rule] };
      this.pruneApplyKeep();
    },
    // Keep rules combine: a row stays when any active rule keeps it, every other row is ticked.
    // "Pinned snapshots" only protects; on its own it ticks nothing.
    pruneApplyKeep() {
      const p = this.prune, rows = p.rows, last = rows.length - 1;
      const k = p.keep;
      if (!k.firstLast && !k.newest && !k.monthly) { this._pruneChanged(); return; }
      const keep = new Set();
      if (k.firstLast) { keep.add(0); keep.add(last); }
      if (k.newest) for (let i = Math.max(0, rows.length - Math.max(1, Number(p.newestN) || 1)); i <= last; i++) keep.add(i);
      if (k.monthly) {
        const seen = new Set();
        for (let i = last; i >= 0; i--) {
          const d = toDate(rows[i].last), key = d.getFullYear() + "-" + d.getMonth();
          if (!seen.has(key)) { seen.add(key); keep.add(i); }
        }
      }
      if (k.pinned) rows.forEach((r) => { if (r.pinned) keep.add(r.id); });
      const tick = {};
      rows.forEach((r) => { if (!keep.has(r.id)) tick[r.id] = true; });
      p.tick = tick;
      this._pruneChanged();
    },
    pruneBiggest() {
      const p = this.prune;
      const n = Math.max(1, Number(p.biggestN) || 1);
      const top = [...p.rows].filter((r) => r.frees > 0 && !(p.keep.pinned && r.pinned))
        .sort((a, b) => b.frees - a.frees).slice(0, n);
      const tick = {};
      top.forEach((r) => { tick[r.id] = true; });
      p.tick = tick;
      p.keep = { ...p.keep, firstLast: false, newest: false, monthly: false };
      this._pruneChanged();
    },

    // Re-price the ticked set (debounced); the old dry run no longer describes it.
    _pruneChanged() {
      const p = this.prune;
      if (!p) return;
      this.klDryClear(p.dry);
      clearTimeout(this._pruneTimer);
      const ids = p.rows.filter((r) => p.tick[r.id]).map((r) => r.id);
      if (!ids.length) { ++this._prunePriceSeq; p.price = null; p.priceLoading = false; return; }
      p.priceLoading = true;
      this._pruneTimer = setTimeout(async () => {
        const seq = ++this._prunePriceSeq;
        try {
          const r = await getJson("/api/prune/price", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token: p.token, remove: ids }) });
          if (seq !== this._prunePriceSeq || !this.prune) return;
          this.prune.price = r;
          if (this.prune.priceFailed) { this.prune.error = ""; this.prune.priceFailed = false; }
        } catch (e) {
          // The old price's commands describe the previous ticks: never leave them runnable.
          if (seq === this._prunePriceSeq && this.prune) {
            Object.assign(this.prune, { price: null, priceFailed: true, error: e.message || String(e) });
          }
        } finally {
          if (seq === this._prunePriceSeq && this.prune) this.prune.priceLoading = false;
        }
      }, 250);
    },

    // ---- run ----
    pruneBlockReason() {
      const p = this.prune;
      if (!p || !p.rows.length) return "";
      const n = this.pruneTickCount();
      if (!n) return "Tick the " + this.pruneNoun() + "s to remove";
      if (n >= p.rows.length) return "Keep at least one " + this.pruneNoun() + ", or use Delete…";
      return "";
    },
    pruneCanRun() {
      const p = this.prune;
      return !!(p && p.price && !p.priceLoading && !this.pruneBlockReason() && (p.price.commands || []).length);
    },
    pruneDryChecked() {
      const st = this.prune && this.prune.dry;
      return !!(st && st.done && st.ok && !st.running);
    },
    pruneDry(ev) {
      if (!this.prune || !this.proGate("prune", ev) || !this.pruneCanRun()) return;
      const p = this.prune;
      return this.klDry("/api/prune/dry-run", { repo: this.repo, items: p.price.commands, full_path: p.full_path }, p.dry);
    },
    async pruneExecute(ev) {
      if (!this.proGate("prune", ev) || !this.pruneCanRun()) return;
      const p = this.prune, price = p.price, noun = this.pruneNoun();
      const kept = p.rows.filter((r) => !p.tick[r.id]);
      const keptText = kept.length <= 4 ? kept.map((r) => fmtDate(r.last)).join(", ")
        : kept.slice(0, 3).map((r) => fmtDate(r.last)).join(", ") + " and " + (kept.length - 3) + " more";
      const facts = [
        [p.kind === "folder" ? "Folder" : "File", p.full_path],
        ["Keeps", countOf(kept.length, noun) + ": " + keptText],
        ["Removes", countOf(this.pruneTickCount(), noun) + " from " + countOf(price.snapshots, "snapshot")],
        ["Frees", fmtPhys(price.frees) + ", after full maintenance"],
        [p.holders.length > 1 ? "Sources" : "Source", p.holders.map((s) => sourcePath(s)).join(", ")],
      ];
      const copies = this.pruneElsewhereTicked();
      if (copies) facts.splice(3, 0, ["Also", countOf(copies, "version") + " identical at another path in those snapshots"]);
      const whole = p.rows.filter((r) => p.tick[r.id] && r.whole_snapshot).length;
      if (whole) facts.splice(3, 0, ["Whole snapshots", countOf(whole, "snapshot") + " where the folder is the whole backup"]);
      const ok = await this.klConfirm({
        title: `Remove ${countOf(this.pruneTickCount(), noun)} of ${baseName(p.full_path)}?`,
        facts,
        callout: "This can't be undone. The removed " + noun + "s can't be restored afterwards.",
        note: this.pruneDryChecked() ? "" : "No dry run yet.",
        confirmLabel: "Remove " + countOf(this.pruneTickCount(), noun), danger: true,
      });
      if (!ok || !this.prune) return;
      await this.wrap(async () => {
        const started = await this.startJob("prune_versions", { items: price.commands, full_path: p.full_path },
          `${baseName(p.full_path)} · ${countOf(this.pruneTickCount(), noun)}`);
        if (started) this.closePrune();
      }, "Pruning…");
    },

    // ---- Repository tab: versions worth pruning ----
    async loadPruneOutliers() {
      const repo = this.repo;
      this.pruneOutliers = { loading: true, rows: [], ready: true };
      try {
        const r = await getJson(`/api/prune/outliers?repo=${encodeURIComponent(repo)}&limit=50`);
        if (repo !== this.repo) return;
        this.pruneOutliers = { loading: false, rows: r.rows || [], ready: r.ready !== false };
      } catch (e) {
        if (repo === this.repo) this.pruneOutliers = { loading: false, rows: [], ready: true, error: e.message || String(e) };
      }
    },
};
