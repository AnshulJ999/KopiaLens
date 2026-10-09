// Policies tab: every kopia policy for the repo in one view. Left rail = Global +
// every source, indented along parent-path chains; right pane = the selected policy with
// Excluded files first. Edits stage locally (`polStaged`) and reach kopia only through
// Review (the diff) + Save (POST /api/policies/save, which takes the fail-closed pre-write
// backup before the write and refuses if the backup fails).
//
// Kopia inheritance facts the backend verified (policies API doc): a source's own ignore list
// REPLACES the global list entirely (never concatenates) - `global_shadowed`; inherited rules
// are removed at the policy that owns them, never on the inheritor. Zero-match priced rules are
// worded neutrally: they may simply not match anything on disk today.
//
// Backend shapes (the /api/policies/*, verified against app/api/policies.py):
//   GET  /api/policies/overview?repo=  -> {stale, global: {rule_count, no_parent_ignore, summarized},
//        sources: [{source, root, defined, has_own_rules, rule_count, effective_origin,
//                   effective_origin_target, effective_rule_count, global_shadowed,
//                   no_parent_ignore, no_parent_dot_files, summarized}]}
//   GET  /api/policies/detail?repo=&target= -> {is_global, defined, effective,
//        rules: {own: [..], inherited: [..], inherited_origin, inherited_active}, summarized, stale}
//   POST /api/policies/price   {repo, target, patterns, limit}
//        -> {results: [{pattern, error, files, bytes, dirs, examples: [{path, size}],
//                       total_examples, capped, negated}]}
//        Capped at 32 patterns per request; a larger ask 400s with a detail pointing at
//        stored-prices (surfaced in the UI, never swallowed).
//   GET  /api/policies/stored-prices?repo=&target=   ( nightly precompute)
//        -> {stored: true, ts, results: <same shape as price>}, or
//           {stored: false, ts: null, results: [], detail} when the nightly has not priced yet.
//   POST /api/policies/diff    {repo, target, changes: {add, remove, delete_policy}}
//        -> {current_rules, resulting_rules, added, removed, impact: {added, removed},
//            will_create_policy, command, backup_dir}
//   POST /api/policies/save    {repo, target, changes, confirmed: true}
//        -> {ok, returncode, output_tail, command, backup, diff}
//   GET  /api/policies/backups?repo= -> {dir, backups: [{name, ts, trigger, size}]}
//   POST /api/policies/backup  {repo} -> {name, verified, size}
//   GET  /api/policies/backup-file?repo=&name= -> {name, content}   // {target: policy} JSON
//   GET  /api/policies/restore-cmd?repo=&name=&mode= -> {command}
//   POST /api/policies/restore {repo, name, mode: "merge"|"mirror", confirmed: true}
//        -> {ok, returncode, output_tail, command, pre_restore_backup}
const policiesPart = {

    // ---------- load ----------
    async loadPolicies(refresh = false) {
      const seq = ++this._polSeq;
      try {
        const data = await this.wrap(async () =>
          getJson(`/api/policies/overview?repo=${encodeURIComponent(this.repo)}`), "Loading policies...");
        if (seq !== this._polSeq || !data) return;
        this.polData = data;
        this.polDetailCache = {};  // detail payloads are invalidated with the overview
        this.polDetail = null;
        this.polResolved = {};
        await this.polLoadMeta();
        const live = new Set([...(data.sources || []).map((s) => s.source), ...(data.others || []).map((o) => o.target)]);
        this.polNewTargets = this.polNewTargets.filter((t) => !live.has(t));
        if (this.polSel !== "global" && !live.has(this.polSel) && !this.polNewTargets.includes(this.polSel)) this.polSel = "global";
        this.loadPolDetail(this.polSel);
        this.polResolve(this.polSel, true);
      } catch (e) { /* wrap() already surfaced the error */ }
    },

    // ---------- per-target detail (right pane) ----------
    async loadPolDetail(target) {
      if (!target) { this.polDetail = null; return; }
      const cached = this.polDetailCache[target];
      if (cached) { this.polDetail = cached; return; }
      const seq = ++this._polDetailSeq;
      this.polDetail = null;
      try {
        const d = await getJson(`/api/policies/detail?repo=${encodeURIComponent(this.repo)}&target=${encodeURIComponent(target)}`);
        if (seq !== this._polDetailSeq) return;
        this.polDetailCache[target] = d;
        this.polDetail = d;
        this.polAutoPrice(target);
      } catch (e) { if (seq === this._polDetailSeq) this.error = e.message || String(e); }
    },

    // ---------- left rail ----------
    // Global first, then sources sorted (UNC last), each indented one step per parent-policy
    // hop along same-host path prefixes. Two sources from different hosts never nest.
    polTargets() {
      if (!this.polData) return [];
      const pols = (this.polData.sources || []).map((s) => ({ ...s, target: s.source }));
      const host = (t) => (t.includes(":") ? t.slice(0, t.indexOf(":")) : "");
      const path = (t) => (t.includes(":") ? t.slice(t.indexOf(":") + 1) : "");
      const norm = (p) => p.replace(/[\\/]+$/, "").toLowerCase();
      const sorted = pols.sort((a, b) => srcSortKey(a.target).localeCompare(srcSortKey(b.target)));
      const depth = {};
      for (const p of sorted) {
        const pp = norm(path(p.target));
        let d = 0;
        for (const q of sorted) {
          if (q.target === p.target || host(q.target) !== host(p.target)) continue;
          const qp = norm(path(q.target));
          if (qp && (pp.startsWith(qp + "\\") || pp.startsWith(qp + "/"))) {
            d = Math.max(d, (depth[q.target] || 0) + 1);
          }
        }
        depth[p.target] = d;
      }
      return sorted.map((p) => ({ ...p, depth: depth[p.target] || 0 }));
    },

    polRail() {
      if (!this.polData) return [];
      const g = this.polData.global;
      const rows = [{ target: "global", isGlobal: true, depth: 0,
                      rule_count: g ? g.rule_count : null,
                      has_own_rules: !!(g && g.rule_count) }];
      for (const t of this.polTargets()) rows.push({ ...t, isGlobal: false });
      const f = this.polFilter.trim().toLowerCase();
      return f ? rows.filter((r) => r.isGlobal || sourcePath(r.target).toLowerCase().includes(f)) : rows;
    },

    polRailLabel(t) {
      if (t.isGlobal) return "Global policy";
      const p = sourcePath(t.target).replace(/[\\/]+$/, "");
      return p || t.target;
    },

    polSelect(target) {
      this.polSel = target;
      this.polAddText = ""; this.polAddPrice = null;
      this.loadPolDetail(target);
      this.polResolve(target, true);
    },

    // The overview row for the selection (header facts live there; rule data in polDetail).
    polSelected() {
      if (!this.polData) return null;
      if (this.polSel === "global") {
        return this.polData.global ? { target: "global", isGlobal: true, ...this.polData.global } : null;
      }
      const row = (this.polData.sources || []).find((s) => s.source === this.polSel);
      if (row) return { ...row, target: row.source, isGlobal: false };
      const other = (this.polData.others || []).find((o) => o.target === this.polSel);
      if (other) return { ...other, source: other.target, defined: true, isGlobal: false };
      return this.polNewTargets.includes(this.polSel)
        ? { target: this.polSel, source: this.polSel, defined: false, isGlobal: false, isNew: true } : null;
    },

    // Ignore files (.kopiaignore and any other names a policy sets) kopia reads inside the folders
    // this policy covers. Read-only; found and read by the nightly, so this never calls kopia.
    // Runs from x-effect: it reloads when the selection changes.
    async polLoadIgnoreFiles() {
      const t = this.polSelected();
      if (!t || t.isNew) return;
      const key = this.repo + "|" + t.target;
      if (this.polIgnore && this.polIgnore.key === key) return;
      const seq = ++this._polIgnoreSeq;
      // Global covers every source, so it lists the whole repo; a host or path target narrows it.
      const target = t.isGlobal ? "" : `&target=${encodeURIComponent(t.target)}`;
      this.polIgnore = { key, loading: true, files: [], stored: true, unavailable: false };
      try {
        const r = await getJson(`/api/policies/ignore-files?repo=${encodeURIComponent(this.repo)}${target}`);
        if (seq !== this._polIgnoreSeq) return;
        this.polIgnore = { key, loading: false, files: r.files || [], stored: r.stored !== false, unavailable: false };
      } catch (e) {
        if (seq !== this._polIgnoreSeq) return;
        this.polIgnore = { key, loading: false, files: [], stored: true, unavailable: true };
      }
    },
    polIgnoreShown() {
      const t = this.polSelected();
      const g = this.polIgnore;
      return !!(t && g && !g.loading && !g.unavailable && g.key === this.repo + "|" + t.target);
    },
    polIgnoreFacts(f) {
      const rules = `${f.rule_count || 0} rule${f.rule_count === 1 ? "" : "s"}`;
      if (f.files == null) return rules;
      return `${rules} · keeps out ${Number(f.files).toLocaleString(NUM_LOCALE)} files · ${this.fmtB(f.bytes || 0)}`;
    },

    // Own rules come from the detail payload (the DEFINED policy); empty until it lands.
    polOwnPatterns() {
      return this.polDetail ? (this.polDetail.rules.own || []) : [];
    },

    // ---------- rules ----------
    // Own rules + staged adds/removes merged into display rows. Pricing comes from the price
    // endpoint (polRowPrices cache per target) - the overview/detail payloads do not price.
    polPriceFor(pat) {
      const m = (this.polRowPrices || {})[this.polSel];
      return m ? m[pat] || null : null;
    },
    polRuleRows() {
      if (!this.polDetail) return [];
      const st = this.polStaged[this.polSel] || { add: [], remove: [] };
      const own = this.polOwnPatterns().map((pat) => {
        const p = this.polPriceFor(pat);
        return {
          pattern: pat,
          files: p ? p.files : null,
          bytes: p ? p.bytes : null,
          dirs: p ? p.dirs : null,
          error: p ? p.error : null,
          state: st.remove.includes(pat) ? "staged-remove" : "own",
        };
      });
      const adds = st.add.map((pat) => {
        const p = (this.polAddPrices || {})[this.polSel + "|" + pat];
        return { pattern: pat, files: p ? p.files : null, bytes: p ? p.bytes : null, dirs: p ? p.dirs : null, error: p ? p.error : null, state: "staged-add" };
      });
      return [...adds, ...own];
    },

    polInheritedRules() {
      return this.polDetail ? (this.polDetail.rules.inherited || []) : [];
    },
    polInheritedOrigin() {
      if (!this.polDetail || !this.polDetail.rules.inherited_active) return "";
      return this.polDetail.rules.inherited_origin || "";
    },

    // Keeping-out totals over the displayed own rules (staged removes excluded). Priced may
    // be below total until the price call lands.
    polKeepout() {
      let files = 0, bytes = 0, priced = 0;
      const rows = this.polRuleRows();
      for (const r of rows) {
        if (r.state === "staged-remove") continue;
        if (r.files != null) { files += r.files; priced++; }
        if (r.bytes != null) bytes += r.bytes;
      }
      return { files, bytes, priced, total: rows.length };
    },

    async polPriceRow() {
      const pats = this.polOwnPatterns();
      if (!pats.length) return;
      const seq = ++this._polRowPriceSeq;
      const results = await this.polPricePatterns(this.polSel, pats, 2);
      if (seq !== this._polRowPriceSeq || !results) return;
      const m = (this.polRowPrices = this.polRowPrices || {})[this.polSel] = (this.polRowPrices[this.polSel] || {});
      for (const p of results) m[p.pattern] = p;
      if (this.polPriceError) delete this.polPriceError[this.polSel];
    },

    // Whole-policy numbers: read the nightly precompute (stored-prices) - instant, any size,
    // Global's 250 rules included. Live pricing stays for the add-box and staged adds only.
    async polPriceStored(target) {
      const seq = ++this._polStoredSeq;
      try {
        const r = await getJson(`/api/policies/stored-prices?repo=${encodeURIComponent(this.repo)}&target=${encodeURIComponent(target)}`);
        if (seq !== this._polStoredSeq) return;
        if (r && r.stored) {
          const m = (this.polRowPrices = this.polRowPrices || {})[target] = (this.polRowPrices[target] || {});
          for (const p of (r.results || [])) m[p.pattern] = p;
          (this.polStoredTs = this.polStoredTs || {})[target] = r.ts || null;
          if (this.polPriceError) delete this.polPriceError[target];
        } else {
          (this.polStoredTs = this.polStoredTs || {})[target] = null;  // nightly has not priced yet
        }
      } catch (e) { /* stored read failing must not break the tab; the live fallback still runs */ }
    },

    // Price the selected policy's own rules when its detail lands: stored numbers first
    // (); a small non-global policy with nothing stored yet falls back to live so the
    // table is not empty on a fresh install. Global never live-prices automatically - one
    // ask fans out to every inheriting source, and the nightly covers it.
    async polAutoPrice(target) {
      target = target || this.polSel;
      await this.polPriceStored(target);
      if (target === "global") return;
      const priced = (this.polRowPrices || {})[target] || {};
      const own = this.polOwnPatterns();
      if (own.length && own.some((pat) => !priced[pat] || priced[pat].files == null)) this.polPriceRow();
    },

    // The one place that talks to the live pricing endpoint. Null on failure - an empty array
    // would leave the table silently unpriced (swallowed errors). The HTTP 400
    // over the 32-pattern cap carries a detail pointing at stored-prices; it lands in
    // polPriceError so the header shows it instead of eating it.
    async polPricePatterns(target, patterns, limit = 10) {
      try {
        const r = await getJson("/api/policies/price", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target, patterns, limit }) });
        if (this.polPriceError) delete this.polPriceError[target];
        return r.results || [];
      } catch (e) {
        if (!this.polPriceError) this.polPriceError = {};
        this.polPriceError[target] = e.message || String(e);
        return null;
      }
    },

    polPriceFailText() {
      return (this.polPriceError || {})[this.polSel] || "";
    },

    // Where the on-screen numbers came from. Stored prices predate any policy save until the
    // next nightly re-prices, so the ts is always shown ( contract).
    polPricingNote() {
      const ts = (this.polStoredTs || {})[this.polSel];
      if (ts) {
        const mins = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
        const age = mins < 60 ? mins + " min" : mins < 1440 ? Math.round(mins / 60) + " h" : Math.round(mins / 1440) + " d";
        return "Priced " + age + " ago by the nightly run - a save re-prices at the next nightly.";
      }
      if (this.polSel === "global") return "Whole-policy pricing runs in the nightly worker; numbers appear after the next index run.";
      return "";
    },

    polNoParentIgnore() {
      if (this.polDetail && this.polDetail.defined) {
        return !!((this.polDetail.defined.files || {}).noParentIgnore);
      }
      const row = this.polSelected();
      return !!(row && row.no_parent_ignore);
    },
    polNoParentDotFiles() {
      const row = this.polSelected();
      return !!(row && row.no_parent_dot_files);
    },
    polGlobalShadowed() {
      const row = this.polSelected();
      return !!(row && !row.isGlobal && row.global_shadowed && this.polData && this.polData.global && this.polData.global.rule_count);
    },

    polTargetNote() {
      if (!this.polDetail) return "";
      if (this.polDetail.is_global) return "Bare patterns at Global match at any depth, in every source.";
      return "Anchored patterns (leading /) are relative to this policy's root: " + sourcePath(this.polSel);
    },

    // Render helpers: x-show does not stop x-text from evaluating, so anything derefing an
    // optional field lives here and returns "" instead of throwing.
    polAlsoLine() {
      const s = this.polDetail && this.polDetail.summarized;
      if (!s) return "";
      const bits = [];
      if ((s.ignore_dot_files || []).length) bits.push("dot-files: " + s.ignore_dot_files.join(", "));
      if (s.ignore_cache_dirs) bits.push("cache dirs");
      if (s.max_file_size) bits.push("files over " + fmtBytes(s.max_file_size));
      return bits.join(" · ");
    },
    polSummaryLine() {
      const s = this.polDetail && this.polDetail.summarized;
      if (!s) return "";
      return (s.schedule || "") + " · keeps " + (s.retention || "");
    },
    polPriceCell(r) {
      if (r.error) return "error: " + r.error;
      if (r.files == null) return "unpriced";
      const dirs = r.dirs ? ` · ${r.dirs} dir${r.dirs === 1 ? "" : "s"}` : "";
      return `${Number(r.files).toLocaleString(NUM_LOCALE)} files · ${fmtBytes(r.bytes || 0)}${dirs}`;
    },
    polAddPreviewText() {
      const p = this.polAddPrice;
      if (!p || p.files == null) return "";
      return "Would exclude " + Number(p.files).toLocaleString(NUM_LOCALE) + " files · " + fmtBytes(p.bytes || 0);
    },
    polAddExamplesText() {
      const p = this.polAddPrice;
      if (!p || !p.examples || !p.examples.length) return "";
      return "  e.g. " + p.examples.slice(0, 2).map((e) => (typeof e === "string" ? e : e.path)).join(", ");
    },
    polImpactText(it) {
      if (!it || it.error) return it && it.error ? "error: " + it.error : "";
      if (it.files == null) return "";
      const dirs = it.dirs ? ` · ${it.dirs} dir${it.dirs === 1 ? "" : "s"}` : "";
      return Number(it.files).toLocaleString(NUM_LOCALE) + " files · " + fmtBytes(it.bytes || 0) + dirs;
    },

    // ---------- staging ----------
    polStagedFor(target) {
      if (!this.polStaged[target]) this.polStaged[target] = { add: [], remove: [], removePolicy: false, fields: {} };
      return this.polStaged[target];
    },
    polStageAdd() {
      if (!this.polDetail) return;
      const pat = (this.polAddText || "").trim();
      if (!pat) return;
      const st = this.polStagedFor(this.polSel);
      // Blocks duplicates AND patterns the policy already defines (even ones staged for
      // removal - staging add+remove of the same pattern is a contradiction).
      if (st.add.includes(pat) || this.polOwnPatterns().includes(pat)) return;
      st.add.push(pat);
      this.polAddText = ""; this.polAddPrice = null;
      this.polPriceStaged(this.polSel, pat);
      this.polResolve();
    },
    polStageRemove(pat) {
      if (!this.polDetail) return;
      // Inherited rules are removed at the policy that owns them (kopia never unsets them on
      // the inheritor) - the UI only stages removals of this policy's own rules.
      if (!this.polOwnPatterns().includes(pat)) return;
      const st = this.polStagedFor(this.polSel);
      st.remove.push(pat);
      this.polResolve();
    },
    polStageRemovePolicy() {
      if (!this.polDetail || this.polDetail.is_global) return;
      this.polStagedFor(this.polSel).removePolicy = true;
    },
    polUndo(target, kind, pat) {
      const st = this.polStaged[target];
      if (!st) return;
      if (kind === "add") st.add = st.add.filter((p) => p !== pat);
      if (kind === "remove") st.remove = st.remove.filter((p) => p !== pat);
      if (kind === "policy") st.removePolicy = false;
      this.polResolve(target);
    },
    polStageCount() {
      let n = 0;
      for (const st of Object.values(this.polStaged)) {
        n += (st.add || []).length + (st.remove || []).length + (st.removePolicy ? 1 : 0) + Object.keys(st.fields || {}).length;
      }
      return n;
    },
    polDiscard() { this.polStaged = {}; },

    polHasStaged(target) {
      const st = this.polStaged[target];
      return !!(st && ((st.add || []).length || (st.remove || []).length || st.removePolicy || Object.keys(st.fields || {}).length));
    },

    // ---------- add-box live pricing ----------
    polAddInput() {
      if (this._polAddTimer) clearTimeout(this._polAddTimer);
      this._polAddTimer = setTimeout(() => this.polPriceAdd(), 400);
    },
    async polPriceAdd() {
      const row = this.polSelected();
      const pat = (this.polAddText || "").trim();
      if (!row || !pat) { this.polAddPrice = null; return; }
      // Global is skipped: one pattern there still fans out to every inheriting source
      // (); the impact surfaces in the review diff instead of a live preview.
      if (row.isGlobal) { this.polAddPrice = null; return; }
      // Seq at call time: a slow answer for an older keystroke must never overwrite the
      // preview of the pattern now in the box (polSelect clears it, this guards races).
      const seq = ++this._polAddPriceSeq;
      const results = await this.polPricePatterns(this.polSel, [pat], 2);
      if (seq !== this._polAddPriceSeq) return;
      this.polAddPrice = results && results[0] ? results[0] : null;
    },
    async polPriceStaged(target, pat) {
      const results = await this.polPricePatterns(target, [pat], 2);
      const p = results && results[0];
      if (p) {
        if (!this.polAddPrices) this.polAddPrices = {};
        this.polAddPrices[target + "|" + pat] = p;
      }
    },

    // ---------- diff + save ----------
    polChanges(only) {
      const out = [];
      for (const [target, st] of Object.entries(this.polStaged)) {
        if (only && !only.includes(target)) continue;
        if (this.polHasStaged(target)) {
          out.push({ target, add: st.add || [], remove: st.remove || [], delete_policy: !!st.removePolicy, fields: st.fields || {} });
        }
      }
      return out;
    },
    polDiffView(g) {
      return { label: g.target === "global" ? "Global" : sourcePath(g.target), before: g.current_rules, after: g.resulting_rules };
    },
    // One diff request per staged target, rendered as one group each.
    async polOpenDiff(only) {
      const changes = this.polChanges(only);
      if (!changes.length) return;
      this.polDiffOnly = only || null;
      this.showPolDiff = true;
      this.polDiffBusy = true;
      this.polDiff = null;
      this.polDiffMode = "save";
      const groups = await Promise.all(changes.map(async (c) => {
        try {
          const r = await getJson("/api/policies/diff", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ repo: this.repo, target: c.target, changes: { add: c.add, remove: c.remove, delete_policy: c.delete_policy, fields: c.fields } }) });
          return { ...r, staged: c };
        } catch (e) {
          return { target: c.target, error: e.message || String(e), staged: c };
        }
      }));
      this.polDiff = { groups };
      this.polDiffBusy = false;
    },
    polDiffErrors() {
      return ((this.polDiff && this.polDiff.groups) || []).flatMap((g) => g.errors || []);
    },
    async polSave() {
      const changes = this.polChanges(this.polDiffOnly);
      if (!changes.length) { this.showPolDiff = false; return; }
      if (this.polDiffErrors().length) return;
      const nRules = changes.reduce((n, c) => n + c.add.length + c.remove.length + (c.delete_policy ? 1 : 0) + Object.keys(c.fields).length, 0);
      const ok = await this.klConfirm({
        title: "Save policy changes?",
        facts: [
          ["Changes", `${nRules} across ${changes.length} polic${changes.length === 1 ? "y" : "ies"}`],
          ["Policies", changes.map((c) => (c.target === "global" ? "Global" : sourcePath(c.target))).join(", ")],
        ],
        note: "A policy backup is taken before each write.",
        command: "", confirmLabel: "Save changes", danger: false,
      });
      if (!ok) return;
      const writes = changes.map((c) => ({ target: c.target, add: c.add, remove: c.remove, delete_policy: c.delete_policy, fields: c.fields }));
      const started = await this.startJob("policy_write", { writes }, `${changes.length} polic${changes.length === 1 ? "y" : "ies"}`);
      if (!started) return;
      this.showPolDiff = false;
      for (const c of changes) delete this.polStaged[c.target];
      this.polStaged = { ...this.polStaged };
      // The stored numbers predate these writes; the reload after the job re-reads them.
      for (const c of changes) {
        if (this.polRowPrices) delete this.polRowPrices[c.target];
        if (this.polStoredTs) delete this.polStoredTs[c.target];
      }
    },

    // ---------- backups ----------
    async polOpenBackups() {
      this.showPolBackups = true;
      const seq = ++this._polBackupSeq;
      try {
        const r = await getJson(`/api/policies/backups?repo=${encodeURIComponent(this.repo)}`);
        if (seq !== this._polBackupSeq) return;
        this.polBackups = r;
      } catch (e) {
        if (seq === this._polBackupSeq) this.polBackups = { backups: [], error: e.message || String(e) };
      }
    },
    async polBackupNow() {
      await this.wrap(async () => {
        const r = await getJson("/api/policies/backup", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo }) });
        this.actionText = `Policy backup written: ${r.name}`;
        this.klNotify(`Policy backup written: ${r.name}`);
        this.polOpenBackups();
      }, "Backing up policies...");
    },

    // Restore review: the backup's own export (a {target: policy} JSON object) summarised
    // client-side, the exact kopia command for the chosen mode, then the confirm dialog.
    async polRestoreReview(name) {
      this.showPolDiff = true;
      this.polDiffBusy = true;
      this.polDiff = null;
      this.polDiffMode = "restore";
      this.polRestoreName = name;
      this.polRestoreMode = "merge";
      this.polRestoreCmd = null;
      this.polRestoreParsed = null;
      try {
        const [file, cmd] = await Promise.all([
          getJson(`/api/policies/backup-file?repo=${encodeURIComponent(this.repo)}&name=${encodeURIComponent(name)}`),
          getJson(`/api/policies/restore-cmd?repo=${encodeURIComponent(this.repo)}&name=${encodeURIComponent(name)}&mode=merge`),
        ]);
        this.polRestoreParsed = this.polParseExport(file && file.content);
        this.polRestoreCmd = cmd;
      } catch (e) {
        this.polRestoreParsed = null;
        this.polRestoreCmd = { error: e.message || String(e) };
      } finally {
        this.polDiffBusy = false;
      }
    },
    polParseExport(content) {
      // kopia policy export = one JSON object {target: policy}. Anything else renders as raw.
      try {
        const data = JSON.parse(content);
        if (data && typeof data === "object" && !Array.isArray(data)) {
          return Object.entries(data).map(([target, pol]) => ({
            target,
            rules: (((pol || {}).files || {}).ignore) || [],
          }));
        }
      } catch (e) { /* fall through */ }
      return null;
    },
    async polRestoreModeChange(mode) {
      this.polRestoreMode = mode;
      const name = this.polRestoreName;
      if (!name) return;
      const cmd = await getJson(`/api/policies/restore-cmd?repo=${encodeURIComponent(this.repo)}&name=${encodeURIComponent(name)}&mode=${encodeURIComponent(mode)}`).catch(() => null);
      this.polRestoreCmd = cmd;
    },
    async polRestoreExecute() {
      const name = this.polRestoreName;
      if (!name) return;
      const mirror = this.polRestoreMode === "mirror";
      const ok = await this.klConfirm({
        title: "Restore this policy backup?",
        facts: [
          ["Backup", name],
          ["Mode", mirror ? "Mirror · policies not in the backup are deleted" : "Merge · policies in the backup replace theirs, others stay"],
          ["Policies", `${(this.polRestoreParsed || []).length || "?"} in the backup`],
        ],
        callout: mirror ? "Every policy not in this backup is deleted." : "",
        note: "Current policies are backed up first.",
        command: this.polRestoreCmd && this.polRestoreCmd.command ? this.polRestoreCmd.command : "",
        confirmLabel: mirror ? "Mirror-restore" : "Restore (merge)", danger: mirror,
      });
      if (!ok) return;
      await this.wrap(async () => {
        const r = await getJson("/api/policies/restore", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, name, mode: this.polRestoreMode, confirmed: true }) });
        if (r.ok) {
          this.actionText = `Policies restored from ${name}.`;
          this.klNotify(`Policies restored from ${name}.`);
        } else {
          this.error = `kopia exited ${r.returncode} - ${(r.output_tail || "").slice(-300)}`;
        }
        this.showPolDiff = false;
        await this.loadPolicies(true);
      }, "Restoring policies...");
    },
};
