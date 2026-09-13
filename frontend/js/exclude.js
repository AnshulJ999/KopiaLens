// Exclusion dialog (file/folder panels, Churn and Tree right-clicks). Targets and their
// before/after coverage come from POST /api/policies/exclusion-targets; every write is priced
// against the index before Exclude is enabled, then saved through /api/policies/save (which takes
// the policy backup first). The delete half routes to the existing delete flow afterwards.
//
//   POST /api/policies/exclusion-targets {repo, path, source, is_folder, shape, refresh, suggest}
//     -> {sources: [{source, origin, owner, already_excluded}], targets: [{target, role, smart,
//         pattern, covers: [{source, matches, already_excluded}], inherited_rules, ...}], smart_shape}
//   POST /api/policies/price {repo, target, patterns, limit} -> {results: [{pattern, files, bytes, ...}]}
//   POST /api/policies/save {repo, target, changes: {add, remove, delete_policy}, confirmed: true}
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
        userShape: false,                   // the user picked a shape; Smart stops switching it
        autoShaped: false,
        custom: "",
        reach: "smart",                     // smart | manual
        targetData: null,
        target: "",                         // the first-ranked target (drives the preview block)
        ticked: {},                         // target -> true
        copyDown: {},                       // child target -> keep its inherited rules (default true)
        prices: {},                         // target -> {pattern, pending, result}
        price: null, priceFor: "", priceTarget: "",
        pricing: false, loading: false,
      };
      // A preset maps onto a backend shape so the endpoint recomputes the pattern per target.
      // Anything globby stays custom text: mapping it would silently rewrite the pattern.
      if (d.preset) {
        const p = d.preset;
        if (p.startsWith("*.")) d.shape = "extension";
        else if (p.startsWith("/") && p.endsWith("/**")) d.shape = d.kind === "folder" ? "exact" : "parent";
        else if (/[*?[]/.test(p)) d.shape = "custom";
        else if (p.startsWith("/")) d.shape = "exact";
        else if (p.includes("/")) d.shape = "custom";
        else d.shape = "name";
        if (d.shape === "custom") d.custom = p;
        d.userShape = true;
      }
      if (seq !== this._exOpenSeq) return;  // a newer openExclude owns the dialog now
      this.exLoadTargets(true);
    },

    // Targets for the current shape. `refresh` re-reads policies from kopia (first load only).
    async exLoadTargets(refresh = false) {
      const d = this.exDialog;
      if (!d) return;
      const seq = ++this._exTargetsSeq;
      this._exClearPrice();
      d.loading = true;
      const suggest = d.reach === "smart" && !d.userShape && !d.autoShaped && d.shape !== "custom";
      try {
        // Custom text has no backend shape: resolve targets with a neutral one.
        const r = await getJson("/api/policies/exclusion-targets", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, path: d.path, source: d.src || null,
                                 is_folder: d.kind === "folder", shape: d.shape === "custom" ? "exact" : d.shape,
                                 refresh, suggest }) });
        if (!this.exDialog || seq !== this._exTargetsSeq) return;
        if (suggest && r.smart_shape && r.smart_shape !== d.shape) {
          d.autoShaped = true;
          d.shape = r.smart_shape;
          return this.exLoadTargets(false);
        }
        d.targetData = r;
        d.loading = false;
        const ts = this.exTargets();
        d.target = (ts[0] && ts[0].target) || "";
        this.exApplyDefaultTicks();
        this.exPriceAll();
      } catch (e) {
        if (!this.exDialog || seq !== this._exTargetsSeq) return;
        d.loading = false;
        d.targetData = { targets: [], sources: [], error: e.message || String(e) };
      }
    },

    exTargets() {  // backend order: Smart picks first
      const d = this.exDialog;
      return (d && d.targetData && d.targetData.targets) || [];
    },

    // Smart ticks the policies the sources already get their rules from. Custom text has no
    // computed coverage, so Smart ticks those same owners.
    exApplyDefaultTicks() {
      const d = this.exDialog;
      if (!d) return;
      if (d.reach !== "smart" && Object.keys(d.ticked).length) return;
      d.ticked = {};
      for (const t of this.exTargets()) {
        const pick = d.shape === "custom" ? (t.role === "owner" || t.role === "start") : t.smart;
        if (pick && this.exPatternOf(t)) d.ticked[t.target] = true;
      }
      this._exClearPrice();
    },

    exSetReach(reach) {
      const d = this.exDialog;
      if (!d || d.reach === reach) return;
      d.reach = reach;
      if (reach === "smart") {
        this.exApplyDefaultTicks();
        this.exPriceAll();
      }
    },

    exToggleTarget(t) {
      const d = this.exDialog;
      if (!d || !this.exPatternOf(t)) return;
      d.reach = "manual";
      if (d.ticked[t.target]) delete d.ticked[t.target];
      else {
        d.ticked[t.target] = true;
        if (t.role === "child" && d.copyDown[t.target] === undefined) d.copyDown[t.target] = true;
      }
      this._exClearPrice();
      this.exPriceAll();
    },

    exToggleCopyDown(t) {
      const d = this.exDialog;
      if (!d) return;
      d.copyDown[t.target] = !this.exCopies(t);
    },

    exCopies(t) {
      const d = this.exDialog;
      return !!(d && t && t.role === "child" && d.copyDown[t.target] !== false);
    },

    // The pattern a target would receive: the custom text, or the backend's per-target pattern.
    exPatternOf(t) {
      const d = this.exDialog;
      if (d && d.shape === "custom") return (d.custom || "").trim();
      return (t && t.pattern) || "";
    },

    exLabel(target) {
      if (target === "global") return "Global";
      return target.includes(":") ? (sourcePath(target) || target) : target;
    },

    exRuleWord(n) {
      return `${n} rule${n === 1 ? "" : "s"}`;
    },

    exWhy(t) {
      const used = t.uses_count || 0;
      const usedText = used ? `used by ${used} of these backups` : "no backup here uses it";
      if (t.role === "start") return "No rules yet · this starts its list";
      if (t.role === "child") return `Gets its rules from ${this.exLabel(t.inherits_from || "global")}`;
      if (!t.existing_rule_count) return `No rules · ${usedText}`;
      return `${this.exRuleWord(t.existing_rule_count)} · ${usedText}`;
    },

    exAlreadyOut(t) {
      const covers = (t && t.covers) || [];
      return covers.length > 0 && covers.every((c) => c.already_excluded);
    },

    // Writes in order: deepest path policy first, host-level next, Global last - one write must
    // not change another target's inheritance before that target is written.
    exWritePlan() {
      const d = this.exDialog;
      if (!d) return [];
      const rank = (target) => {
        if (target === "global") return [2, 0];
        if (!target.includes(":")) return [1, 0];
        return [0, -target.split(":").slice(1).join(":").split(/[/\\]/).length];
      };
      return this.exTargets()
        .filter((t) => d.ticked[t.target] && this.exPatternOf(t))
        .sort((a, b) => { const ra = rank(a.target), rb = rank(b.target); return ra[0] - rb[0] || ra[1] - rb[1]; })
        .map((t) => ({ target: t.target, pattern: this.exPatternOf(t), t, is_child: t.role === "child" }));
    },

    exShapeLabel() {
      const d = this.exDialog;
      return d && d.autoShaped && !d.userShape ? "Smart picked this so the rule reaches every backup holding it." : "";
    },

    exSelectShape(shape) {
      const d = this.exDialog;
      if (!d || d.shape === shape) return;
      d.shape = shape;
      d.userShape = true;
      this.exLoadTargets();
    },

    exCustomInput() {
      if (this._exCustomTimer) clearTimeout(this._exCustomTimer);
      this._exCustomTimer = setTimeout(() => {
        const d = this.exDialog;
        if (!d) return;
        if (!Object.keys(d.ticked).length) this.exApplyDefaultTicks();
        this.exPriceAll();
      }, 400);
    },

    _exClearPrice() {
      const d = this.exDialog;
      if (!d) return;
      // An old price describes the old pattern/target and must never sit beside a new selection.
      d.price = null; d.priceFor = ""; d.priceTarget = ""; d.pricing = false;
      d.prices = {};
      if (this._exPriceTimer) { clearTimeout(this._exPriceTimer); this._exPriceTimer = null; }
    },

    // Price every write the commit would make; Exclude needs a current price per ticked target.
    exPriceAll() {
      const d = this.exDialog;
      if (!d) return;
      this._exClearPrice();
      const plan = this.exWritePlan();
      if (!plan.length) return;
      const seq = ++this._exPriceSeq;
      d.pricing = true;
      this._exPriceTimer = setTimeout(async () => {
        for (const e of plan) {
          if (!this.exDialog || seq !== this._exPriceSeq) return;
          d.prices[e.target] = { pattern: e.pattern, pending: true, result: null };
        }
        await Promise.all(plan.map(async (e) => {
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
        const first = plan[0];
        if (first && d.prices[first.target] && d.prices[first.target].result) {
          d.price = d.prices[first.target].result;
          d.priceFor = first.pattern; d.priceTarget = first.target;
        }
      }, 250);
    },

    // Null on failure: an empty array would read as "matches nothing".
    async exPricePatterns(target, patterns, limit = 10) {
      try {
        const r = await getJson("/api/policies/price", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target, patterns, limit }) });
        return r.results || [];
      } catch (e) { return null; }
    },

    exPriceReady() {
      const d = this.exDialog;
      if (!d || d.pricing) return false;
      const plan = this.exWritePlan();
      if (!plan.length) return false;
      return plan.every((p) => {
        const rec = d.prices[p.target];
        return rec && !rec.pending && rec.pattern === p.pattern && rec.result && !rec.result.error;
      });
    },

    // Of the sources holding this path, how many leave it out once the ticked writes land.
    exCoverage() {
      const d = this.exDialog;
      if (!d || !d.targetData || d.shape === "custom") return null;
      const sources = d.targetData.sources || [];
      const ticked = this.exTargets().filter((t) => d.ticked[t.target]);
      let covered = 0;
      const still = [];
      sources.forEach((s, idx) => {
        const ok = s.already_excluded || ticked.some((t) => t.covers[idx] && t.covers[idx].matches);
        if (ok) covered += 1;
        else still.push(s.root || s.source);
      });
      return { total: sources.length, covered, still, already: sources.every((s) => s.already_excluded) };
    },

    exCoverageText() {
      const c = this.exCoverage();
      if (!c || !c.total) return "";
      if (c.already) return "Already left out of every backup that holds it.";
      const word = c.total === 1 ? "backup" : "backups";
      if (c.covered >= c.total) {
        return c.total === 1 ? "The backup stops backing this up." : `${c.total === 2 ? "Both" : "All " + c.total} backups stop backing this up.`;
      }
      const names = c.still.slice(0, 3).join(", ")
        + (c.still.length > 3 ? ` +${c.still.length - 3} more` : "");
      return `${c.covered} of ${c.total} ${word} stop backing this up. Still backed up by ${names}.`;
    },

    // Ticked policies can reach the same files: count each matched path once when every price
    // listed all its matches, otherwise sum (an upper bound).
    exKeepoutText() {
      const d = this.exDialog;
      if (!d) return "";
      let files = 0, byts = 0, any = false, bad = false, complete = true;
      const seen = new Map();
      for (const key of Object.keys(d.prices)) {
        const rec = d.prices[key];
        if (!rec || !rec.result) continue;
        if (rec.result.error) { bad = true; continue; }
        any = true;
        const ex = rec.result.examples || [];
        if (rec.result.capped || ex.length < Number(rec.result.files || 0)) complete = false;
        for (const m of ex) if (m && typeof m === "object") seen.set(m.path, Number(m.size || 0));
        files += Number(rec.result.files || 0);
        byts += Number(rec.result.bytes || 0);
      }
      if (bad || !any) return "";
      if (complete) {
        files = seen.size;
        byts = [...seen.values()].reduce((a, b) => a + b, 0);
      }
      return `${complete ? "" : "up to "}${files.toLocaleString()} file${files === 1 ? "" : "s"} · ${this.fmtB(byts)}`;
    },

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
      return `Showing ${shown} of ${Number(p.total_examples != null ? p.total_examples : p.files).toLocaleString()}, biggest first`;
    },
    exPriceNote() {
      const d = this.exDialog;
      if (!d) return "";
      if (d.shape === "custom") {
        const p = d.price;
        if (!p) return "";
        if (p.error) return "That pattern can't be used: " + p.error;
        if (p.negated) return "A leading ! brings matching files back instead of leaving them out.";
        if (p.files === 0) return "Matches nothing in the index.";
        return "";
      }
      for (const key of Object.keys(d.prices || {})) {
        const rec = d.prices[key];
        if (rec && rec.result && rec.result.error) return `Couldn't check ${this.exLabel(key)}: ${rec.result.error}`;
      }
      return "";
    },
    exEmptyText() {
      const d = this.exDialog;
      if (!d || this.exWritePlan().length) return "";
      if (d.shape === "custom") return "Write a pattern above.";
      const c = this.exCoverage();
      if (c && c.already) return "Nothing to add.";
      return "Pick where the rule goes.";
    },

    exToSearch() {
      const d = this.exDialog;
      if (!d) return;
      // The index stores raw paths: a Windows source keeps backslashes.
      const first = this.exWritePlan()[0];
      const pat = (first ? first.pattern : "").replace(/\*\*/g, "").replace(/^\//, "").replace(/^\*/, "").replace(/\//g, "\\");
      this.showExclude = false;
      this.query = pat; this.searchSource = ""; this.searchPage = 1;
      this.active = "search";
      this.loadSearch();
    },

    // One policy write through /api/policies/save. Returns true only when kopia accepted it.
    async exWriteRule(target, patterns) {
      let wrote = false;
      await this.wrap(async () => {
        const r = await getJson("/api/policies/save", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target, changes: { add: patterns, remove: [], delete_policy: false }, confirmed: true }) });
        if (r.ok) {
          wrote = true;
          this.actionText = `Rule added to ${this.exLabel(target)}` + (r.backup && r.backup.name ? ` · backup ${r.backup.name}` : "");
        } else {
          this.error = `kopia exited ${r.returncode} - ${(r.output_tail || "").slice(-300)}`;
        }
        if (this.polData) this.loadPolicies(true);
      }, "Adding the rule...");
      return wrote;
    },

    exPlanFact(p) {
      if (!p.is_child) return p.pattern;
      const n = (p.t.inherited_rules || []).length;
      const from = this.exLabel(p.t.inherits_from || "global");
      return this.exCopies(p.t)
        ? `${p.pattern} (copies ${this.exRuleWord(n)} from ${from})`
        : `${p.pattern} (stops using ${this.exRuleWord(n)} from ${from})`;
    },

    async exConfirm() {
      const d = this.exDialog;
      if (!d) return;
      const plan = this.exWritePlan();
      if (!plan.length) { this.error = "Pick where the rule goes."; return; }
      if (!this.exPriceReady()) {
        this.error = d.pricing ? "Still checking the index. Try again in a moment." : (this.exPriceNote() || "Not checked yet.");
        return;
      }
      const facts = plan.length === 1
        ? [["Rule", this.exPlanFact(plan[0])], ["Added to", this.exLabel(plan[0].target)]]
        : plan.map((p) => [this.exLabel(p.target), this.exPlanFact(p)]);
      facts.push(["Leaves out", this.exKeepoutText() || "not checked"]);
      const coverage = this.exCoverageText();
      if (coverage) facts.push(["Result", coverage]);
      const drops = plan.filter((p) => p.is_child && !this.exCopies(p.t));
      const dropText = drops.map((p) => `${this.exLabel(p.target)} stops using ${this.exRuleWord((p.t.inherited_rules || []).length)} from ${this.exLabel(p.t.inherits_from || "global")}. Files those rules leave out start being backed up again.`).join(" ");
      const writes = plan.map((p) => ({ target: p.target, patterns: this._exPatternsFor(p) }));
      if (d.mode === "exclude") {
        const ok = await this.klConfirm({
          title: plan.length === 1 ? "Add this exclusion rule?" : `Add ${plan.length} exclusion rules?`,
          facts, callout: dropText,
          note: "You can remove it later in the Policies tab.",
          command: "", confirmLabel: plan.length === 1 ? "Add rule" : `Add ${plan.length} rules`, danger: !!drops.length,
        });
        if (!ok) return;
        this.showExclude = false; this.exDialog = null;
        await this._exWriteAll(writes);
        return;
      }
      facts.push(["Then", "The delete flow opens for what's already backed up"]);
      const ok = await this.klConfirm({
        title: "Exclude, then delete what's backed up?",
        facts, callout: dropText,
        note: "Space frees after the next full maintenance.",
        command: "", confirmLabel: "Exclude and delete…", danger: true,
      });
      if (!ok) return;
      const route = { kind: d.kind, path: d.path, source: d.src };
      this.showExclude = false; this.exDialog = null;
      const wrote = await this._exWriteAll(writes);
      if (wrote) this.exRouteDelete(route);
    },

    // A child write keeps its inherited rules (in their order) with the new rule at the bottom.
    _exPatternsFor(p) {
      if (!p.is_child || !this.exCopies(p.t)) return [p.pattern];
      const out = [...(p.t.inherited_rules || [])].filter((r) => r !== p.pattern);
      out.push(p.pattern);
      return out;
    },

    async _exWriteAll(writes) {
      let any = false;
      for (const w of writes) any = (await this.exWriteRule(w.target, w.patterns)) || any;
      return any;
    },

    // The delete half: folder -> the folder removal plan across every source; file -> the file
    // panel's remove section.
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
