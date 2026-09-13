// exclusion dialog: the adder that lives in the file/folder panels (and the
// Churn/Tree right-clicks). Picks a pattern shape, prices it against the index BEFORE anything
// is written, previews the matched set biggest-first, then confirms. The exclude half writes
// policy rules synchronously (/api/policies/save, fail-closed backup server-side); the delete
// half routes to the standard delete flow afterwards, which keeps its own dry run + confirm
// (nothing is ever removed straight from this dialog).
//
// Targets come from POST /api/policies/exclusion-targets (Option A): only
// policies that OWN an ignore rule list are offered - a source that merely inherits is never a
// write target, because kopia replaces (never stacks) the inherited list. The response carries
// each target's pattern ALREADY RECOMPUTED for that target (the anchor trap) and, per cover,
// whether that cover is ALREADY excluded by the owner's existing rules.
//
// multi-select: the rule must reach everywhere the delete reaches, but
// the tick set is DERIVED from the ownership resolution, not a blind tick-all - an owner whose
// pattern would not newly protect anything stays unticked, a rule-less source shows "creates
// its first rule list", and inheritors are only reachable through the collapsed Advanced
// escape hatch (warned, with the copy-rules-down offer). The delete half stays WIDE: the
// delete reclaims space now; the RULE is what stops re-ingestion.
//
// Other endpoints (verified against app/api/policies.py):
//   POST /api/policies/price {repo, target, patterns, limit}
//     -> {"results": [{"pattern", "error", "files", "bytes", ...}]}   (per-target reach incl. inheritors)
//   POST /api/policies/save {repo, target, changes: {add, remove, delete_policy}, confirmed: true}
//     -> {ok, returncode, output_tail, command, backup}   // synchronous, fail-closed backup inside
const excludePart = {

    // ctx: {kind: "file"|"folder", path, source?, preset?}; mode: "exclude"|"delete".
    async openExclude(ctx, mode = "exclude") {
      this.showExclude = true;
      const seq = ++this._exOpenSeq;
      const d = this.exDialog = {
        kind: ctx.kind, path: ctx.path, mode,
        src: ctx.source || "",              // the panel's source: disambiguates user@host + routes the delete half
        preset: ctx.preset || "",
        shape: "exact",                     // exact | name | extension | parent | custom
        custom: "",
        reach: "everywhere",                // "everywhere" | "max" (top-ranked target only)
        targetData: null,                   // /api/policies/exclusion-targets response
        target: "",                         // the ranked-first target (kept for custom-text anchoring hints)
        ticked: {},                         // target -> true
        hatched: {},                        // escape-hatch source -> true
        copyDown: {},                       // escape-hatch source -> copy inherited rules down (default true)
        prices: {},                         // target -> {pattern, pending, result}
        price: null, priceFor: "", priceTarget: "",   // the ranked-first target's price (preview block)
        pricing: false, loading: false,
      };
      // A preset (the panel's suggestion rows) maps onto a backend shape so the endpoint
      // recomputes the pattern properly. Anything globby or otherwise not exactly one of the
      // four shapes stays custom text - mapping it onto a shape would let the endpoint
      // silently REWRITE the intended pattern (e.g. "/a/*/[ab].txt" -> the literal path).
      if (d.preset) {
        const p = d.preset;
        if (p.startsWith("*.")) d.shape = "extension";
        else if (p.startsWith("/") && p.endsWith("/**")) d.shape = d.kind === "folder" ? "exact" : "parent";
        else if (/[*?[]/.test(p)) d.shape = "custom";
        else if (p.startsWith("/")) d.shape = "exact";
        else if (p.includes("/")) d.shape = "custom";
        else d.shape = "name";
        if (d.shape === "custom") d.custom = p;
      }
      if (seq !== this._exOpenSeq) return;  // a newer openExclude owns the dialog now
      this.exLoadTargets();
    },

    // Owning policies for this path, ranked by sources covered. Re-called on every shape
    // change: the shape decides each target's pattern, and the pattern decides the matches.
    async exLoadTargets() {
      const d = this.exDialog;
      if (!d) return;
      const seq = ++this._exTargetsSeq;
      this._exClearPrice();
      d.loading = true;
      try {
        // Custom text has no backend shape; owners do not depend on the shape, so resolve them
        // with a neutral one and write the custom text to every selected target.
        const r = await getJson("/api/policies/exclusion-targets", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, path: d.path, source: d.src || null,
                                 is_folder: d.kind === "folder", shape: d.shape === "custom" ? "exact" : d.shape }) });
        if (!this.exDialog || seq !== this._exTargetsSeq) return;
        d.targetData = r;
        d.loading = false;
        const ts = this.exTargets();
        d.target = (ts[0] && ts[0].target) || "";
        this.exApplyDefaultTicks();
        this.exPriceAll();
      } catch (e) {
        if (!this.exDialog || seq !== this._exTargetsSeq) return;
        d.loading = false;
        d.targetData = { targets: [], sources: [], escape_hatches: [], error: e.message || String(e) };
      }
    },

    // Ranked by SOURCES COVERED (matched covers first - an anchored pattern genuinely reaches
    // fewer covered sources than the owner speaks for; that count is free from the matcher).
    exTargets() {
      const d = this.exDialog;
      if (!d || !d.targetData) return [];
      const ts = [...(d.targetData.targets || [])];
      const matched = (t) => (t.covers || []).filter((c) => c.matches).length;
      ts.sort((a, b) => matched(b) - matched(a)
        || (b.covers || []).length - (a.covers || []).length
        || String(a.target).localeCompare(String(b.target)));
      return ts;
    },

    // The default tick set, derived from the ownership resolution - NOT a blind tick-all.
    // "Everywhere": tick every owner whose write would NEWLY protect at least one source (a
    // pattern that would not reach, or that is already excluded there, adds nothing - ticking it
    // would be plain wrong). "Max impact": only the top-ranked target.
    exApplyDefaultTicks() {
      const d = this.exDialog;
      if (!d) return;
      d.ticked = {}; d.hatched = {};
      // Custom text: reach and already-excluded are computed for the backend shapes only, so
      // default to the top-ranked target and let the user tick more.
      if (d.reach === "max" || d.shape === "custom") {
        const first = this.exTargets()[0];
        if (first && this.exPatternOf(first)) d.ticked[first.target] = true;
        this._exClearPrice();
        return;
      }
      for (const t of this.exTargets()) {
        if (t.pattern && (t.protects || 0) > 0) d.ticked[t.target] = true;
      }
      this._exClearPrice();
    },

    exSetReach(reach) {
      const d = this.exDialog;
      if (!d || d.reach === reach) return;
      d.reach = reach;
      this.exApplyDefaultTicks();
      this.exPriceAll();
    },

    exToggleTarget(t) {
      const d = this.exDialog;
      if (!d) return;
      if (d.ticked[t.target]) delete d.ticked[t.target];
      else if (this.exPatternOf(t)) d.ticked[t.target] = true;
      this._exClearPrice();
      this.exPriceAll();
    },

    // escape hatch: writing into a policy that currently INHERITS its rules. Off by
    // default, collapsed, and every ticked hatch carries the shadowing consequence.
    exToggleHatch(h) {
      const d = this.exDialog;
      if (!d) return;
      if (d.hatched[h.target]) delete d.hatched[h.target];
      else if (this.exPatternOf(h)) {
        d.hatched[h.target] = true;
        if (d.copyDown[h.target] === undefined) d.copyDown[h.target] = true;
      }
      this._exClearPrice();
      this.exPriceAll();
    },

    exToggleCopyDown(h) {
      const d = this.exDialog;
      if (!d) return;
      d.copyDown[h.target] = !d.copyDown[h.target];
    },

    exHatches() {
      const d = this.exDialog;
      return (d && d.targetData && d.targetData.escape_hatches) || [];
    },

    exHasHatches() {
      return this.exHatches().some((h) => h.pattern);
    },

    // Targets with the hatch source's own pattern text for display (the backend recomputes it -
    // the frontend never rebuilds anchored text itself).
    exHatchPattern(h) {
      return this.exPatternOf(h);
    },

    // The pattern a target or hatch would receive: the user's text in Custom mode (the same for
    // every destination), otherwise the backend's per-target recomputed pattern.
    exPatternOf(t) {
      const d = this.exDialog;
      if (d && d.shape === "custom") return (d.custom || "").trim();
      return (t && t.pattern) || "";
    },

    // Every key the commit will write: ticked owning targets first, then escape hatches.
    // ORDER MATTERS: nested/specific writes go BEFORE the broad ones (deepest root first,
    // will_create first), Global last - one write must not flip another target's inheritance
    // underneath the next write.
    exWritePlan() {
      const d = this.exDialog;
      if (!d) return [];
      const depth = (target) => (target === "global" ? -1 : (target.split(":", 1)[0] === target ? 0 : (target.split(":").slice(1).join(":").split(/[/\\]/).length)));
      const owners = this.exTargets().filter((t) => d.ticked[t.target] && this.exPatternOf(t));
      owners.sort((a, b) => {
        if (a.will_create_policy !== b.will_create_policy) return a.will_create_policy ? -1 : 1;
        if (a.is_global !== b.is_global) return a.is_global ? 1 : -1;
        return depth(b.target) - depth(a.target);
      });
      const hatches = this.exHatches().filter((h) => d.hatched[h.target] && this.exPatternOf(h))
        .map((h) => ({ target: h.target, pattern: this.exPatternOf(h), is_hatch: true, h }));
      return [...owners.map((t) => ({ target: t.target, pattern: this.exPatternOf(t), t })), ...hatches];
    },

    exSelectedTarget() {
      const d = this.exDialog;
      if (!d || !d.targetData) return null;
      return (d.targetData.targets || []).find((t) => t.target === d.target) || null;
    },

    exCoversText(t) {
      const covers = t.covers || [];
      const n = covers.length;
      const matched = covers.filter((c) => c.matches).length;
      const base = n === 1 ? "covers 1 source" : `covers ${n} sources`;
      if (!n || matched === n || (this.exDialog && this.exDialog.shape === "custom")) return base;
      return `${base} · ${matched} match this shape`;
    },

    // The pattern that will be written for the RANKED-FIRST target (the preview block's
    // reference), or the user's custom text. Per-target patterns come from the backend.
    exPattern() {
      const d = this.exDialog;
      if (!d) return "";
      if (d.shape === "custom") return (d.custom || "").trim();
      const t = this.exSelectedTarget();
      return t ? (t.pattern || "") : "";
    },

    exSelectShape(shape) {
      const d = this.exDialog;
      if (!d || d.shape === shape) return;
      d.shape = shape;
      this.exLoadTargets();  // re-rank + recompute each target's pattern for the new shape
    },

    exCustomInput() {
      if (this._exCustomTimer) clearTimeout(this._exCustomTimer);
      this._exCustomTimer = setTimeout(() => {
        const d = this.exDialog;
        if (!d) return;
        // Typing into an empty selection ticks the top-ranked target, as opening Custom would.
        if (!Object.keys(d.ticked).length) this.exApplyDefaultTicks();
        this.exPriceAll();
      }, 400);
    },

    _exClearPrice() {
      const d = this.exDialog;
      if (!d) return;
      // The old price describes the OLD pattern/target - it must never sit beside a new
      // selection while the debounce runs (stale pricing with Commit enabled).
      d.price = null; d.priceFor = ""; d.priceTarget = ""; d.pricing = false;
      d.prices = {};
      if (this._exPriceTimer) { clearTimeout(this._exPriceTimer); this._exPriceTimer = null; }
    },

    // price EVERY write the commit would make - the Commit gate needs a current,
    // successful price per ticked target, since patterns (and, for custom text, anchoring) differ
    // per target.
    exPriceAll() {
      const d = this.exDialog;
      if (!d) return;
      this._exClearPrice();
      const plan = this.exWritePlan();
      if (!plan.length) return;
      const seq = ++this._exPriceSeq;
      d.pricing = true;
      this._exPriceTimer = setTimeout(async () => {
        const entries = plan.map((p) => ({ target: p.target, pattern: p.pattern, is_hatch: !!p.is_hatch }));
        for (const e of entries) {
          if (!this.exDialog || seq !== this._exPriceSeq) return;
          d.prices[e.target] = { pattern: e.pattern, pending: true, result: null };
        }
        await Promise.all(entries.map(async (e) => {
          const results = await this.exPricePatterns(e.target, [e.pattern], 120);
          if (!this.exDialog || seq !== this._exPriceSeq) return;
          d.prices[e.target] = {
            pattern: e.pattern, pending: false,
            result: results && results[0] ? results[0]
              : { pattern: e.pattern, files: null, error: "could not check the index" },
          };
        }));
        if (!this.exDialog || seq !== this._exPriceSeq) return;
        d.pricing = false;
        // The preview block shows the ranked-first target's price.
        const first = entries.find((e) => !e.is_hatch);
        if (first && d.prices[first.target] && d.prices[first.target].result) {
          d.price = d.prices[first.target].result;
          d.priceFor = first.pattern; d.priceTarget = first.target;
        }
      }, 250);
    },

    // The one place that talks to the pricing endpoint. Null on failure - an empty array
    // would read as "matches nothing", which is a lie the Commit button must not trust.
    async exPricePatterns(target, patterns, limit = 10) {
      try {
        const r = await getJson("/api/policies/price", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target, patterns, limit }) });
        return r.results || [];
      } catch (e) { return null; }
    },

    // True only when EVERY write the commit would make has a CURRENT, successful price for
    // exactly the pattern on screen. Pricing-before-commit is the guard against a too-broad
    // exclusion silently un-protecting files.
    exPriceReady() {
      const d = this.exDialog;
      if (!d) return false;
      if (d.pricing) return false;
      const plan = this.exWritePlan();
      if (!plan.length) return false;
      return plan.every((p) => {
        const rec = d.prices[p.target];
        return rec && !rec.pending && rec.pattern === p.pattern && rec.result
          && !rec.result.error;
      });
    },

    // coverage: of the sources that hold this path, how many the TICKED writes protect.
    // A cover is protected when its owner is ticked and the pattern matches there (or it is
    // hatched, where its own new rule protects it).
    exCoverage() {
      const d = this.exDialog;
      // Per-source matching is computed for the backend shapes only - not for custom text.
      if (!d || !d.targetData || d.shape === "custom") return null;
      const sources = d.targetData.sources || [];
      let covered = 0;
      const uncovered = [];
      const tickedTargets = this.exTargets().filter((t) => d.ticked[t.target]);
      for (const s of sources) {
        let ok = false;
        for (const t of tickedTargets) {
          const cover = (t.covers || []).find((c) => c.source === s.source);
          if (cover && cover.matches) { ok = true; break; }
        }
        if (!ok && d.hatched[s.source]) ok = true;
        if (ok) covered += 1;
        else uncovered.push(s.root || s.source);
      }
      return { total: sources.length, covered, uncovered };
    },

    exCoverageText() {
      const c = this.exCoverage();
      if (!c || !c.total) return "";
      if (c.covered >= c.total) return `all ${c.total} source${c.total === 1 ? "" : "s"} covered`;
      const roots = c.uncovered.slice(0, 3).map((r) => sourcePath(r) || r).join(", ")
        + (c.uncovered.length > 3 ? ` +${c.uncovered.length - 3} more` : "");
      return `${c.covered} of ${c.total} covered - ${roots} will keep backing this up`;
    },

    // Total keep-out across every priced write (per-target prices cover that target's full
    // reach, and owners are disjoint, so the sum is the honest union).
    exKeepoutText() {
      const d = this.exDialog;
      if (!d) return "";
      let files = 0, byts = 0, any = false, bad = false;
      for (const key of Object.keys(d.prices)) {
        const rec = d.prices[key];
        if (!rec || !rec.result) continue;
        if (rec.result.error) { bad = true; continue; }
        any = true;
        files += Number(rec.result.files || 0);
        byts += Number(rec.result.bytes || 0);
      }
      if (bad || !any) return "";
      return `${files.toLocaleString()} files · ${this.fmtB(byts)}`;
    },

    // Preview rows: examples are {path, size} objects, biggest first (from the ranked-first
    // target's price; the full picture is the keep-out total).
    exMatches() {
      const p = this.exDialog && this.exDialog.price;
      if (!p) return [];
      return (p.examples || []).map((m) => (typeof m === "string" ? { path: m, size: null } : m));
    },
    exPreviewCap() {
      const p = this.exDialog && this.exDialog.price;
      if (!p || p.error || p.files == null) return "";
      const shown = (p.examples || []).length;
      if (!p.capped && shown >= p.files) return "";
      return `showing ${shown} of ${Number(p.total_examples != null ? p.total_examples : p.files).toLocaleString()} matched files - biggest first`;
    },
    exPriceNote() {
      const d = this.exDialog;
      if (!d) return "";
      if (d.shape === "custom") {
        const p = d.price;
        if (!p) return "";
        if (p.error) return "That pattern could not be used: " + p.error;
        if (p.negated) return "A leading ! makes this a re-include rule - it cancels matching exclusion rules.";
        if (p.files === 0) return "Matches nothing in the current index - it may simply not match anything on disk today.";
        return "";
      }
      for (const key of Object.keys(d.prices || {})) {
        const rec = d.prices[key];
        if (rec && rec.result && rec.result.error) {
          return `Pricing failed for ${key === "global" ? "the Global policy" : key}: ${rec.result.error}`;
        }
      }
      return "";
    },
    exEmptyText() {
      const d = this.exDialog;
      if (!d || this.exWritePlan().length || this.exPattern()) return "";
      return d.shape === "custom" ? "Write a pattern above."
        : "Nothing would newly protect this selection here - pick another shape or switch the scope.";
    },

    exToSearch() {
      const d = this.exDialog;
      if (!d) return;
      // The index holds raw paths: a Windows source stores separators, so a cleaned pattern
      // with forward slashes would substring-match nothing.
      const pat = this.exPattern().replace(/\*\*/g, "").replace(/^\//, "").replace(/^\*/, "").replace(/\//g, "\\");
      this.showExclude = false;
      this.query = pat; this.searchSource = ""; this.searchPage = 1;
      this.active = "search";
      this.loadSearch();
    },

    // One policy write: synchronous /api/policies/save with confirmed=true (the confirm dialog
    // above is that flag's source). The server takes the fail-closed pre-write backup before
    // the write and refuses if the backup cannot be verified. Returns TRUE only when kopia
    // accepted the write - the delete half must never run otherwise.
    async exWriteRule(target, patterns) {
      let wrote = false;
      await this.wrap(async () => {
        const r = await getJson("/api/policies/save", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target, changes: { add: patterns, remove: [], delete_policy: false }, confirmed: true }) });
        if (r.ok) {
          wrote = true;
          this.actionText = `Rule written into ${target === "global" ? "the Global policy" : target}` +
            (r.backup && r.backup.name ? ` - backup ${r.backup.name}` : "");
        } else {
          this.error = `kopia exited ${r.returncode} - ${(r.output_tail || "").slice(-300)}`;
        }
        if (this.polData) this.loadPolicies(true);  // the write changed policies - catch the tab up
      }, "Writing exclusion rule...");
      return wrote;
    },

    _exTargetLabel(target) {
      return target === "global" ? "the Global policy" : sourcePath(target);
    },

    async exConfirm() {
      const d = this.exDialog;
      if (!d) return;
      const plan = this.exWritePlan();
      if (!plan.length) { this.error = "Pick at least one destination, or switch the shape."; return; }
      // Same gate the footer buttons hold (review round-2 #4): Enter must not commit while
      // any price is pending, failed, or belongs to a different pattern/target.
      if (!this.exPriceReady()) {
        this.error = d.pricing ? "Pricing is still running - wait a moment, then try again."
          : this.exPriceNote() || "Not priced for every selected destination yet.";
        return;
      }
      const keepout = this.exKeepoutText();
      const coverage = this.exCoverageText();
      const facts = [];
      if (plan.length === 1) {
        const p = plan[0];
        if (p.is_hatch) {
          facts.push(["Advanced", `write into ${this._exTargetLabel(p.target)} (it currently inherits its rules)`]);
        }
        facts.push(["Pattern", p.pattern]);
        facts.push(["Written to", this._exTargetLabel(p.target)]);
      } else {
        for (const p of plan) {
          facts.push([this._exTargetLabel(p.target), p.pattern + (p.is_hatch ? "  (advanced: replaces its inherited rules)" : "")]);
        }
      }
      facts.push(["Keeps out", keepout || "not priced yet"]);
      if (coverage) facts.push(["Coverage", coverage]);
      if (d.mode === "exclude") {
        const ok = await this.klConfirm({
          title: "Exclude this pattern?",
          facts,
          note: "Reversible - remove the rule any time in the Policies tab. Nothing is deleted.",
          command: "", confirmLabel: "Exclude", danger: false,
        });
        if (!ok) return;
        const writes = plan.map((p) => ({ target: p.target, patterns: this._exPatternsFor(p) }));
        this.showExclude = false; this.exDialog = null;
        await this._exWriteAll(writes);
        return;
      }
      facts.push(["Delete scope", "every source holding this path (the rule, not the delete, is what stops re-ingestion)"]);
      facts.push(["Then", "the standard delete flow opens for the matched files, with its own dry run before anything is removed"]);
      facts.push(["Space", "frees on the next full maintenance run (not immediately)"]);
      const ok = await this.klConfirm({
        title: "Exclude, then delete the existing copies?",
        facts,
        callout: coverage && !coverage.startsWith("all") ? `Uncovered sources will re-ingest what the delete removes - ${coverage}.` : "",
        note: "The exclusion goes first on purpose: without it, the next snapshot re-ingests what you delete.",
        command: "", confirmLabel: "Exclude and delete…", danger: true,
      });
      if (!ok) return;
      const route = { kind: d.kind, path: d.path, source: d.src };
      const writes = plan.map((p) => ({ target: p.target, patterns: this._exPatternsFor(p) }));
      this.showExclude = false; this.exDialog = null;
      const wrote = await this._exWriteAll(writes);
      if (wrote) this.exRouteDelete(route);
    },

    // The add list for one write: the target's own pattern, plus - for an escape-hatch write
    // with copy-down - the rules that would otherwise be shadowed, so nothing inherited is lost.
    _exPatternsFor(p) {
      const d = this.exDialog;
      const out = [p.pattern];
      if (p.is_hatch && d && d.copyDown[p.target] && p.h && p.h.inherited_rules) {
        for (const r of p.h.inherited_rules) if (!out.includes(r)) out.push(r);
      }
      return out;
    },

    async _exWriteAll(writes) {
      let any = false;
      for (const w of writes) any = (await this.exWriteRule(w.target, w.patterns)) || any;
      return any;
    },

    // Step 2 of the delete half: the flows that already exist, with their guards. Folder ->
    // the chunked delete plan (WIDE: no source filter - the plan covers every matched source);
    // file -> the detail panel's remove section, whose object-id scope already reaches every
    // source holding the content (never narrow the delete).
    exRouteDelete(route) {
      if (route.kind === "folder") {
        this.openRemoveFolderPath(route.path, null);
      } else {
        this.openFileDetail({ source: route.source, full_path: route.path }).then(() => {
          if (this.detail) { this.detail.panelTab = "actions"; this.rfEnsurePlan(); }
        });
      }
    },
};
