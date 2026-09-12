// exclusion dialog: the adder that lives in the file/folder panels (and the Churn/Tree
// right-clicks). Picks a pattern shape, prices it against the index BEFORE anything is written,
// previews the matched set biggest-first, then confirms. The exclude half writes a policy rule
// through the job runner; the delete half routes to the standard delete flow afterwards, which
// keeps its own dry run + confirm (nothing is ever removed straight from this dialog).
//
// The anchor trap (from the policy-surfaces design): an anchored pattern is relative to the
// POLICY it lives in, not to the source. Patterns here are recomputed whenever the target
// changes, and anchored shapes are dropped entirely for the Global target (a leading /
// there anchors to every source's own root, which is never what "this exact path" means).
// Target and pattern are always shown together.
//
// Assumed API (the backend, verified against app/api/policies.py):
//   POST /api/policies/price {repo, target, patterns, limit}
//     -> {"results": [{"pattern", "error", "files", "bytes", "dirs", "negated",
//                      "examples": [{"path", "size"}], "total_examples", "capped"}]}
//   POST /api/policies/save {repo, target, changes: {add, remove, delete_policy}, confirmed: true}
//     -> {ok, returncode, output_tail, command, backup}   // synchronous, fail-closed backup inside
const excludePart = {

    // ctx: {kind: "file"|"folder", path, source?, preset?}; mode: "exclude"|"delete".
    async openExclude(ctx, mode = "exclude") {
      this.showExclude = true;
      const d = this.exDialog = {
        kind: ctx.kind, path: ctx.path, mode,
        src: ctx.source || "",              // the original source, used to route the delete half
        preset: ctx.preset || "",
        targets: [], target: "", shapeId: "", custom: "",
        price: null, pricing: false, loading: false,
      };
      if (ctx.source) d.targets = [ctx.source];
      else if (ctx.sources && ctx.sources.length) d.targets = [...ctx.sources];
      else {
        // No source given (Churn right-click): ask the folder panel's fast summary which
        // sources cover this folder, so the target list is real policies, not a guess.
        d.loading = true;
        try {
          const s = await getJson(`/api/folder/summary?repo=${encodeURIComponent(this.repo)}&prefix=${encodeURIComponent(ctx.path)}`);
          if (this.exDialog === d) d.targets = (s.sources || []).filter((x) => x.relation === "covers").map((x) => x.source);
        } catch (e) { /* targets stay empty; the dialog still offers Global */ }
        d.loading = false;
      }
      d.targets = [...d.targets, "global"];
      d.target = d.targets[0] || "global";
      this.exAfterTargetChange();
    },

    exAfterTargetChange() {
      const d = this.exDialog;
      if (!d) return;
      const shapes = this.exShapes();
      const hit = d.preset ? shapes.find((s) => s.pattern === d.preset) : null;
      if (hit) { d.shapeId = hit.id; }
      else if (d.preset) { d.shapeId = "custom"; d.custom = d.preset; }
      else {
        const def = shapes.find((s) => s.id !== "custom" && s.pattern);
        d.shapeId = def ? def.id : "custom";
      }
      this.exPriceNow();
    },

    // Shape list for the current target. Anchored shapes exist only for a per-source target;
    // at Global the same text would anchor to a different root, so they are not offered.
    // No target yet (the covering-source fetch is in flight) -> no shapes at all, so a
    // half-resolved root can never show a wrong pattern.
    exShapes() {
      const d = this.exDialog;
      if (!d || !d.target) return [];
      const isG = d.target === "global";
      const full = (d.path || "").replace(/[\\/]+$/, "");
      const name = full.split("\\").pop().split("/").pop();
      const root = isG ? "" : sourcePath(d.target).replace(/[\\/]+$/, "");
      const rel = root && full.toLowerCase().startsWith(root.toLowerCase())
        ? full.slice(root.length).replace(/^[\\/]+/, "") : full;
      const relFwd = rel.replace(/\\/g, "/");
      const parentRel = relFwd.includes("/") ? relFwd.slice(0, relFwd.lastIndexOf("/")) : "";
      const parentName = parentRel ? parentRel.split("/").pop() : "";
      const dot = name.lastIndexOf(".");
      const out = [];
      if (d.kind === "folder") {
        if (!isG) out.push({ id: "exact", label: "This folder", pattern: "/" + relFwd + "/**" });
        out.push({ id: "name", label: "Any folder of this name", pattern: name });
        if (!isG && parentRel) out.push({ id: "up", label: "Its parent folder", pattern: "/" + parentRel + "/**" });
        if (parentName && parentName !== name) out.push({ id: "upname", label: "Any folder of the parent's name", pattern: parentName });
      } else {
        if (!isG) out.push({ id: "exact", label: "This exact file", pattern: "/" + relFwd });
        out.push({ id: "name", label: "Files with this name, any depth", pattern: name });
        if (!isG && parentRel) out.push({ id: "up", label: "Its containing folder", pattern: "/" + parentRel + "/**" });
        if (parentName && parentName !== name) out.push({ id: "upname", label: "Any folder of that name", pattern: parentName });
        if (dot > 0) out.push({ id: "ext", label: "By extension", pattern: "*" + name.slice(dot) });
      }
      out.push({ id: "custom", label: "Custom pattern", pattern: "" });
      return out;
    },

    exPattern() {
      const d = this.exDialog;
      if (!d) return "";
      if (d.shapeId === "custom") return (d.custom || "").trim();
      const s = this.exShapes().find((x) => x.id === d.shapeId);
      return s ? s.pattern : "";
    },

    exSelectShape(id) {
      const d = this.exDialog;
      if (!d) return;
      d.shapeId = id;
      this.exPriceNow();
    },

    exSetTarget(t) {
      const d = this.exDialog;
      if (!d) return;
      d.target = t;
      this.exAfterTargetChange();  // the anchor trap: the pattern text follows the target
    },

    exCustomInput() {
      if (this._exCustomTimer) clearTimeout(this._exCustomTimer);
      this._exCustomTimer = setTimeout(() => this.exPriceNow(), 400);
    },

    exPriceNow() {
      const d = this.exDialog;
      if (!d) return;
      const pat = this.exPattern();
      if (!pat) { d.price = null; d.pricing = false; return; }
      if (this._exPriceTimer) clearTimeout(this._exPriceTimer);
      // Seq is bumped at SCHEDULE time: a shape change inside the debounce window invalidates
      // the pending call, so a stale price can never land on a new selection.
      const seq = ++this._exPriceSeq;
      d.pricing = true;
      this._exPriceTimer = setTimeout(async () => {
        const results = await this.exPricePatterns(d.target, [pat], 120);
        if (!this.exDialog || seq !== this._exPriceSeq) return;
        d.price = results && results[0] ? results[0] : { pattern: pat, files: null, error: null };
        d.pricing = false;
      }, 250);
    },

    // The one place that talks to the pricing endpoint.
    async exPricePatterns(target, patterns, limit = 10) {
      try {
        const r = await getJson("/api/policies/price", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target, patterns, limit }) });
        return r.results || [];
      } catch (e) { return []; }
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
      return `${Number(p.files).toLocaleString()} files · ${fmtB(p.bytes || 0)}`;
    },
    exPriceNote() {
      const p = this.exDialog && this.exDialog.price;
      if (!p) return "";
      if (p.error) return "That pattern could not be used: " + p.error;
      if (p.negated) return "A leading ! makes this a re-include rule - it cancels matching exclusion rules.";
      if (p.files === 0) return "Matches nothing in the current index - it may simply not match anything on disk today.";
      return "";
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
    async exWriteRule(target, pat) {
      await this.wrap(async () => {
        const r = await getJson("/api/policies/save", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target, changes: { add: [pat], remove: [], delete_policy: false }, confirmed: true }) });
        if (r.ok) {
          this.actionText = `Rule written into ${target === "global" ? "the Global policy" : target}` +
            (r.backup && r.backup.name ? ` - backup ${r.backup.name}` : "");
        } else {
          this.error = `kopia exited ${r.returncode} - ${(r.output_tail || "").slice(-300)}`;
        }
        if (this.polData) this.loadPolicies(true);  // the write changed policies - catch the tab up
      }, "Writing exclusion rule...");
    },

    async exConfirm() {
      const d = this.exDialog;
      if (!d) return;
      const pat = this.exPattern();
      if (!pat) { this.error = "Enter a pattern first."; return; }
      const where = d.target === "global" ? "the Global policy" : d.target;
      const keepout = this.exKeepoutText();
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
          ["Step 2", "The standard delete flow opens for the matched files, with its own dry run before anything is removed"],
          ["Step 3", "Space frees on the next full maintenance run (not immediately)"],
        ],
        callout: d.target === "global" ? "A rule at Global applies to EVERY source in this repo." : "",
        note: "The exclusion goes first on purpose: without it, the next snapshot re-ingests what you delete.",
        command: "", confirmLabel: "Exclude and delete…", danger: true,
      });
      if (!ok) return;
      const route = { kind: d.kind, path: d.path, source: d.src };
      this.showExclude = false; this.exDialog = null;
      await this.exWriteRule(d.target, pat);
      this.exRouteDelete(route);
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
