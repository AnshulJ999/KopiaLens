// exclusion dialog: the adder that lives in the file/folder panels (and the Churn/Tree
// right-clicks). Picks a pattern shape, prices it against the index BEFORE anything is written,
// previews the matched set biggest-first, then confirms. The exclude half writes a policy rule
// synchronously (/api/policies/save, fail-closed backup server-side); the delete half routes to
// the standard delete flow afterwards, which keeps its own dry run + confirm (nothing is ever
// removed straight from this dialog).
//
// Targets come from POST /api/policies/exclusion-targets (Option A): only
// policies that OWN an ignore rule list are offered - a source that merely inherits is never a
// write target, because kopia replaces (never stacks) the inherited list. The response carries
// each target's pattern ALREADY RECOMPUTED for that target (the anchor trap: inherited rules
// re-anchor to each covered source's walk root) - the frontend never rebuilds that text.
// Ranking is by SOURCES COVERED: free from the inheritance/matcher resolution,
// and it answers the targeting question. Bytes are left out - makes global pricing
// slow, and the endpoint still prices impact server-side per call (flagged to the orchestrator).
//
// Other endpoints (verified against app/api/policies.py):
//   POST /api/policies/price {repo, target, patterns, limit}
//     -> {"results": [{"pattern", "error", "files", "bytes", "dirs", "negated",
//                      "examples": [{"path", "size"}], "total_examples", "capped"}]}
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
        targetData: null,                   // /api/policies/exclusion-targets response
        target: "",                         // the selected target key
        price: null, priceFor: "", priceTarget: "",   // the price, and the pattern+target it describes
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
        const r = await getJson("/api/policies/exclusion-targets", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, path: d.path, source: d.src || null,
                                 is_folder: d.kind === "folder", shape: d.shape }) });
        if (!this.exDialog || seq !== this._exTargetsSeq) return;
        d.targetData = r;
        d.loading = false;
        const ts = this.exTargets();
        d.target = (ts[0] && ts[0].target) || "";
        this.exPriceNow();
      } catch (e) {
        if (!this.exDialog || seq !== this._exTargetsSeq) return;
        d.loading = false;
        d.targetData = { targets: [], sources: [], error: e.message || String(e) };
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
      if (!n || matched === n) return base;
      return `${base} · ${matched} match this shape`;
    },

    // The pattern that will be written: the backend's recomputed text for the selected target,
    // or the user's custom text. Never derived frontend-side for the built-in shapes.
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
      if (shape === "custom") { this._exClearPrice(); this.exPriceNow(); return; }
      this.exLoadTargets();  // re-rank + recompute each target's pattern for the new shape
    },

    exSetTarget(t) {
      const d = this.exDialog;
      if (!d) return;
      d.target = t;
      this.exPriceNow();  // the price must follow the target: patterns differ per target
    },

    exCustomInput() {
      if (this._exCustomTimer) clearTimeout(this._exCustomTimer);
      this._exCustomTimer = setTimeout(() => this.exPriceNow(), 400);
    },

    _exClearPrice() {
      const d = this.exDialog;
      if (!d) return;
      // The old price describes the OLD pattern/target - it must never sit beside a new
      // selection while the debounce runs (stale pricing with Commit enabled).
      d.price = null; d.priceFor = ""; d.priceTarget = ""; d.pricing = false;
      if (this._exPriceTimer) { clearTimeout(this._exPriceTimer); this._exPriceTimer = null; }
    },

    exPriceNow() {
      const d = this.exDialog;
      if (!d) return;
      const pat = this.exPattern();
      this._exClearPrice();
      if (!pat) return;
      const seq = ++this._exPriceSeq;
      d.pricing = true;
      this._exPriceTimer = setTimeout(async () => {
        const results = await this.exPricePatterns(d.target, [pat], 120);
        if (!this.exDialog || seq !== this._exPriceSeq) return;
        d.price = results && results[0] ? results[0]
          : { pattern: pat, files: null, error: "could not check the index" };
        d.priceFor = pat; d.priceTarget = d.target;
        d.pricing = false;
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

    // True only when a CURRENT, successful price exists for exactly the pattern + target on
    // screen. The footer buttons hold on this: pricing-before-commit is the guard against a
    // too-broad exclusion silently un-protecting files.
    exPriceReady() {
      const d = this.exDialog;
      return !!(d && d.price && !d.pricing && !d.price.error
        && d.priceFor === this.exPattern() && d.priceTarget === d.target);
    },

    // Preview rows: examples are {path, size} objects, biggest first.
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
    exKeepoutText() {
      const p = this.exDialog && this.exDialog.price;
      if (!p || p.error || p.files == null) return "";
      // this.fmtB: bare fmtB only resolves in Alpine templates, not from a method call -
      // the bare form made the Exclude confirm die on a ReferenceError before opening.
      return `${Number(p.files).toLocaleString()} files · ${this.fmtB(p.bytes || 0)}`;
    },
    exPriceNote() {
      const p = this.exDialog && this.exDialog.price;
      if (!p) return "";
      if (p.error) return "That pattern could not be used: " + p.error;
      if (p.negated) return "A leading ! makes this a re-include rule - it cancels matching exclusion rules.";
      if (p.files === 0) return "Matches nothing in the current index - it may simply not match anything on disk today.";
      return "";
    },
    exEmptyText() {
      const d = this.exDialog;
      if (!d || this.exPattern()) return "";
      return d.shape === "custom" ? "Write a pattern above."
        : "This shape does not apply to this selection - pick another.";
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

    // The policy write itself: synchronous /api/policies/save with confirmed=true (the
    // confirm dialog above is that flag's source). The server takes the fail-closed
    // pre-write backup before the write and refuses if the backup cannot be verified.
    // Returns TRUE only when kopia accepted the write - the delete half must never run
    // otherwise (exclude-then-delete used to proceed past a failed write).
    async exWriteRule(target, pat) {
      let wrote = false;
      await this.wrap(async () => {
        const r = await getJson("/api/policies/save", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target, changes: { add: [pat], remove: [], delete_policy: false }, confirmed: true }) });
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

    async exConfirm() {
      const d = this.exDialog;
      if (!d) return;
      const pat = this.exPattern();
      if (!pat) { this.error = "Enter a pattern first."; return; }
      // Same gate the footer buttons hold (review round-2 #4): Enter must not commit while
      // the price is pending, failed, or belongs to a different pattern/target.
      if (!this.exPriceReady()) {
        this.error = d.pricing ? "Pricing is still running - wait a moment, then try again."
          : d.price && d.price.error ? "The price failed - fix the pattern or pick another shape first."
          : "Not priced for this pattern and target yet.";
        return;
      }
      const where = d.target === "global" ? "the Global policy" : d.target;
      const keepout = this.exKeepoutText();
      // The delete half sweeps exactly what the rule protects: a write to one source's policy
      // scopes the delete to that source; only a Global write justifies the wider sweep
      // (the rule went to one policy while the delete swept every source).
      const deleteScope = d.target !== "global" ? d.target : d.src;
      if (d.mode === "exclude") {
        const ok = await this.klConfirm({
          title: "Exclude this pattern?",
          facts: [
            ["Pattern", pat],
            ["Written to", where],
            ["Keeps out", keepout || "not priced yet"],
          ],
          note: "Reversible - remove the rule any time in the Policies tab. Nothing is deleted.",
          command: "", confirmLabel: "Exclude", danger: false,
        });
        if (!ok) return;
        this.showExclude = false; this.exDialog = null;
        await this.exWriteRule(d.target, pat);
        return;
      }
      const ok = await this.klConfirm({
        title: "Exclude, then delete the existing copies?",
        facts: [
          ["Step 1", `Write the rule into ${where}, a verified backup taken first`],
          ["What it keeps out", keepout || "not priced yet"],
          ["Delete scope", deleteScope ? sourcePath(deleteScope) : "every source holding this path"],
          ["Step 2", "The standard delete flow opens for the matched files, with its own dry run before anything is removed"],
          ["Step 3", "Space frees on the next full maintenance run (not immediately)"],
        ],
        callout: d.target === "global" ? "A rule at Global applies to every source that inherits it - sources with their own rule lists are NOT covered." : "",
        note: "The exclusion goes first on purpose: without it, the next snapshot re-ingests what you delete.",
        command: "", confirmLabel: "Exclude and delete…", danger: true,
      });
      if (!ok) return;
      const route = { kind: d.kind, path: d.path, source: deleteScope };
      this.showExclude = false; this.exDialog = null;
      const wrote = await this.exWriteRule(d.target, pat);
      if (wrote) this.exRouteDelete(route);
    },

    // Step 2 of the delete half: the flows that already exist, with their guards. Folder ->
    // the chunked delete plan; file -> the detail panel's remove section with the plan ready.
    exRouteDelete(route) {
      if (route.kind === "folder") {
        this.openRemoveFolderPath(route.path, route.source || null);
      } else {
        this.openFileDetail({ source: route.source, full_path: route.path }).then(() => {
          if (this.detail) { this.detail.panelTab = "actions"; this.rfEnsurePlan(); }
        });
      }
    },
};
