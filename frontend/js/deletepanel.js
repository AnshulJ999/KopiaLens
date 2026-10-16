// Delete panel (files) and the Exclude & delete flow (Exclude -> Delete -> Review -> one job).
// Plan data: POST /api/delete/plan - exactly what each option touches, before anything runs.
const deletePart = {

    // files: [{source, full_path}]; opts: {method, by, every, copies, flow}
    openDeletePanel(files, opts = {}) {
      if (!this.wmGuard("full")) return;
      const list = (files || []).filter((f) => f && f.source && f.full_path);
      if (!list.length) { this.klNotify("Select one or more files first.", "warn"); return; }
      this.delPanel = {
        files: list, method: opts.method || "remove", by: opts.by || "object-id", sources: null,
        every: !!opts.every, copies: !!opts.copies, plan: null, loading: true, error: "", stats: null,
        statsLoading: false, dry: this.klDryState(), showCmd: false, flow: !!opts.flow,
      };
      this.showDel = true;
      this.delLoad();
    },

    closeDelete() {
      if (this.flow) return this.flowCancel();
      this.showDel = false;
      this.delPanel = null;
    },

    async delLoad() {
      const p = this.delPanel;
      if (!p) return;
      const seq = ++this._delSeq;
      ++this._delStatsSeq;   // a Frees figure still loading belongs to the old options
      p.loading = true; p.error = ""; p.stats = null; p.statsLoading = false;
      this.klDryClear(p.dry);
      try {
        const r = await getJson("/api/delete/plan", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, files: p.files, method: p.method, by: p.by,
                                 sources: p.sources, copies: p.copies }) });
        if (seq !== this._delSeq || !this.delPanel) return;
        p.plan = r;
        // "Every source" picked before the candidates were known: widen the scope once they are.
        if (p.every && !p.sources && (r.candidates || []).length > (r.scope || []).length) {
          p.sources = [...r.candidates].sort();
          return this.delLoad();
        }
      } catch (e) {
        if (seq !== this._delSeq || !this.delPanel) return;
        p.error = e.message || String(e);
      } finally {
        if (seq === this._delSeq && this.delPanel) p.loading = false;
      }
      if (seq === this._delSeq && p.plan && p.method === "remove") this.delLoadStats();
    },

    // Every match in the full-list popup (the panel lists the first 500), with Download CSV.
    delShowAll() {
      const p = this.delPanel;
      if (!p || !p.plan) return;
      const body = { repo: this.repo, files: p.files, method: p.method, by: p.by, sources: p.sources, copies: p.copies, list_cap: 1000000 };
      this.openFullList({
        title: "What gets removed", name: "delete-plan", about: aboutOf((p.files || []).map((f) => f.full_path)),
        columns: [["full_path", "Path"], ["tag", "Match"], ["sources_text", "Sources"], ["on_disk", "On disk"], ["size", "Size", "bytes"]],
        load: async () => {
          const r = await getJson("/api/delete/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
          return (r.matches || []).map((m) => ({ full_path: m.full_path, tag: this.delRowTag(m) || "", sources_text: this.fileSources(m),
            on_disk: m.in_latest ? "yes" : "deleted", size: m.size }));
        },
      });
    },

    // Reclaimable over every match (the list stops at 500), and the other match method's figure.
    async delLoadStats() {
      const p = this.delPanel;
      if (!p || !p.plan || !(p.plan.matches || []).length) return;
      const seq = ++this._delStatsSeq;
      p.statsLoading = true;
      const r = await getJson("/api/delete/stats", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: this.repo, files: p.files, method: "remove", by: p.by, sources: p.sources }) }).catch(() => null);
      if (seq !== this._delStatsSeq || !this.delPanel) return;
      p.stats = r;
      p.statsLoading = false;
    },
    // Reclaimable for a match method, shown in the switch so both can be compared.
    delByFig(by) {
      const st = this.delPanel && this.delPanel.stats;
      if (!st) return "";
      const v = by === this.delPanel.by ? st.freeable : st.alt_freeable;
      return v == null ? "" : fmtPhys(v);
    },

    delSetMethod(m) {
      const p = this.delPanel;
      if (!p || p.method === m) return;
      p.method = m; p.sources = null; p.every = false;
      this.delLoad();
    },
    delSetBy(by) {
      const p = this.delPanel;
      if (!p || p.by === by) return;
      p.by = by; p.sources = null; p.every = false;
      this.delLoad();
    },
    delSetEvery(on) {
      const p = this.delPanel;
      if (!p || !p.plan || p.every === !!on) return;
      p.every = !!on;
      if (on) { p.sources = [...(p.plan.candidates || [])].sort(); this.delLoad(); }
    },
    delToggleCopies() {
      const p = this.delPanel;
      if (!p) return;
      p.copies = !p.copies; p.sources = null;
      this.delLoad();
    },
    delTicked(src) {
      const p = this.delPanel;
      return !!(p && p.plan && (p.plan.scope || []).includes(src));
    },
    delToggleSource(src) {
      const p = this.delPanel;
      if (!p || !p.plan) return;
      const cur = new Set(p.plan.scope || []);
      if (cur.has(src)) { if (cur.size === 1) return; cur.delete(src); } else cur.add(src);
      p.sources = [...cur].sort();
      this.delLoad();
    },

    // Sources the user can tick: holders of the selection, plus sources sharing its content.
    delCandidates() {
      const plan = this.delPanel && this.delPanel.plan;
      if (!plan) return [];
      return (plan.candidates || []).map((s) => ({
        source: s, holds: (plan.holders || []).includes(s),
      }));
    },

    delFileLabel() {
      const p = this.delPanel;
      if (!p) return "";
      if (p.files.length > 1) return `${p.files.length} files`;
      return baseName(p.files[0].full_path);
    },
    delTitle() {
      const p = this.delPanel;
      if (!p) return "";
      return p.flow ? "Exclude and delete" : `Delete ${this.delFileLabel()} from backups`;
    },
    delCopyCommand(ev) { if (this.proGate(this.delFeature(), ev)) this.copyText(this.delPanel.plan.command); },
    delPathLine() {
      const f = (this.delPanel && this.delPanel.files) || [];
      if (!f.length) return "";
      return f.length === 1 ? f[0].full_path : `${f[0].full_path} and ${f.length - 1} more`;
    },

    delRowTag(r) {
      const p = this.delPanel;
      if (r.selected) return "";
      return p && p.by === "filename" ? "same name" : "identical copy";
    },

    delCount() {
      const plan = this.delPanel && this.delPanel.plan;
      if (!plan) return 0;
      return this.delPanel.method === "remove" ? (plan.match_total || 0) : (plan.snapshots || []).length;
    },

    delScopeSources() {
      const p = this.delPanel;
      if (!p || !p.plan) return [];
      if (p.method === "remove") return p.plan.scope || [];
      return [...new Set((p.plan.snapshots || []).map((s) => s.source))];
    },

    delReady() {
      const p = this.delPanel;
      return !!(p && p.plan && !p.loading && this.delCount() > 0
        && !(p.method === "snapshots" && p.plan.capped));
    },

    // Remove = Pro "remove"; deleting the snapshots that hold the files = Pro "purge".
    delFeature() { return this.delPanel && this.delPanel.method === "snapshots" ? "purge" : "remove"; },
    delDry(ev) {
      const p = this.delPanel;
      if (!this.delReady() || !this.proGate(this.delFeature(), ev)) return;
      if (p.method === "remove") {
        return this.klDry("/api/actions/remove-files-dry-run",
          { repo: this.repo, files: p.files, by: p.by, sources: p.plan.scope }, p.dry);
      }
      return this.klDry("/api/actions/snapshot-delete-dry-run",
        { repo: this.repo, snapshot_ids: p.plan.snapshots.map((s) => s.snapshot_id) }, p.dry);
    },
    delDryChecked() {
      const st = this.delPanel && this.delPanel.dry;
      return !!(st && st.done && st.ok && !st.running);
    },

    // The job a confirmed delete runs (also the Exclude & delete flow's second step).
    delJobSpec() {
      const p = this.delPanel;
      if (p.method === "remove") {
        return { kind: "remove_files", params: { form: "files", files: p.files, by: p.by, sources: p.plan.scope } };
      }
      return { kind: "snapshot_delete", params: { snapshot_ids: p.plan.snapshots.map((s) => s.snapshot_id) } };
    },

    delFacts() {
      const p = this.delPanel, plan = p.plan;
      const sources = this.delScopeSources().map((s) => sourcePath(s)).join(", ");
      const facts = [[p.files.length > 1 ? "Files" : "File",
        p.files.length > 1 ? `${p.files.length} selected` : p.files[0].full_path]];
      if (p.method === "remove") {
        facts.push(["Match", p.by === "filename" ? `File name: ${plan.names.join(", ")}` : "Exact content, every version"]);
        facts.push(["Sources", sources]);
        facts.push(["Removes", countOf(plan.match_total, "path") + (plan.other_count ? ` · ${plan.other_count} not selected` : "")]);
        if (p.stats) facts.push([term("reclaimable"), fmtPhys(p.stats.freeable) + " after maintenance"]);
        if (p.stats && p.stats.kept_count) {
          facts.push(["Stays stored", `${fmtPhys(p.stats.kept)} · kept by ${countOf(p.stats.kept_count, "identical file")} with other names`]);
        }
      } else {
        const snaps = plan.snapshots || [];
        facts.push(["Sources", sources]);
        facts.push(["Snapshots", `${snaps.length} · ${fmtDate(snaps[snaps.length - 1].start_time)} to ${fmtDate(snaps[0].start_time)}`]);
        if (plan.pinned_count || plan.retained_count) {
          facts.push(["Kept", `${plan.pinned_count} pinned · ${plan.retained_count} kept by retention`]);
        }
      }
      return facts;
    },

    delCallout() {
      const p = this.delPanel, plan = p.plan;
      if (p.method === "snapshots") return "Every other file in these snapshots is deleted with them.";
      if (!plan.other_count) return "";
      if (p.by === "object-id" && (plan.matches || []).every((m) => !Number(m.size))) {
        return `Every empty file shares the same content. ${plan.other_count} other empty file${plan.other_count === 1 ? " is" : "s are"} removed too.`;
      }
      return p.by === "filename"
        ? `${plan.other_count} other file${plan.other_count === 1 ? "" : "s"} with the same name ${plan.other_count === 1 ? "is" : "are"} removed too.`
        : `${plan.other_count} identical cop${plan.other_count === 1 ? "y" : "ies"} at other paths ${plan.other_count === 1 ? "is" : "are"} removed too.`;
    },

    delConfirmLabel() {
      const p = this.delPanel;
      if (!p || !p.plan) return "Delete";
      if (p.method === "remove") {
        return `Remove ${countOf(p.plan.match_total || 0, "file")} from ${countOf(this.delScopeSources().length, "source")}`;
      }
      return `Delete ${countOf((p.plan.snapshots || []).length, "snapshot")}`;
    },

    async delExecute(ev) {
      const p = this.delPanel;
      if (!this.delReady() || !this.proGate(this.delFeature(), ev)) return;
      const ok = await this.klConfirm({
        title: p.method === "remove" ? `Remove ${this.delFileLabel()} from snapshots?` : `Delete ${countOf(p.plan.snapshots.length, "snapshot")}?`,
        facts: this.delFacts(), callout: this.delCallout(),
        note: (this.delDryChecked() ? "" : "No dry run yet. ") + "Space comes back after the next full maintenance.",
        command: p.plan.command, confirmLabel: this.delConfirmLabel(), danger: true,
      });
      if (!ok) return;
      const spec = this.delJobSpec();
      const started = await this.startJob(spec.kind, spec.params, `${this.delFileLabel()} · ${this.delConfirmLabel().toLowerCase()}`);
      if (started) { this.showDel = false; this.delPanel = null; if (this.detail) this.detail = null; }
    },

    // ---- Selections (Search, Tree) ----
    _selItems(scope) {
      const src = scope === "tree" ? (this.selectedSource !== "*ALL*" ? this.selectedSource : "")
        : (this.searchAllRepos() ? "" : this.searchSource || "");
      return this.selectedRows(scope).map((r) => ({
        kind: this.isFolderRow(r) ? "folder" : "file",
        path: r.full_path || r.path, source: r.source || src,
      })).filter((i) => i.path);
    },

    selExclude(scope, mode = "exclude", ev = null) {
      const items = this._selItems(scope);
      if (!items.length) { this.klNotify("Select one or more rows first.", "warn"); return; }
      this.openExclude({ items }, mode, ev);
    },

    // Files always go to the Delete panel, folders (or a mix) to the folder plan.
    async selDelete(scope) {
      const rows = this.selectedRows(scope);
      const items = this._selItems(scope);
      if (!items.length) { this.klNotify("Select one or more rows first.", "warn"); return; }
      const folders = rows.filter((r) => this.isFolderRow(r));
      if (folders.length === 1 && rows.length === 1) {
        return scope === "tree" ? this.openRemoveFolder(folders[0]) : this.openRemoveFolderPath(folders[0].full_path);
      }
      if (folders.length) {
        const filters = scope === "tree" ? { deleted_only: this.treeDeletedOnly, over_gb: this.treeOverGb ? 1 : 0, ext: this.treeExt } : {};
        return this.openRemoveParts(this._partsOf(items), this._partsLabel(items), filters);
      }
      const missing = items.filter((i) => !i.source).map((i) => i.path);
      if (missing.length) {
        const held = await this._holdersOf(missing);
        if (!held) return;
        for (const i of items) if (!i.source) i.source = (held[i.path] || [])[0] || "";
      }
      const files = items.filter((i) => i.source).map((i) => ({ source: i.source, full_path: i.path }));
      if (files.length < items.length) { const n = items.length - files.length; this.klNotify(`${countOf(n, "file")} ${n === 1 ? "isn't" : "aren't"} held by any indexed source, so ${n === 1 ? "it was" : "they were"} left out.`, "warn"); }
      this.openDeletePanel(files);
    },

    // Sources holding each path, for All-sources rows that carry none. One batched call.
    async _holdersOf(paths) {
      try {
        const r = await getJson("/api/tree/holders", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, full_paths: paths }) });
        const out = {};
        for (const p of paths) out[p] = ((r && r.paths && r.paths[p]) || []).map((h) => h.source);
        return out;
      } catch (e) {
        this.klNotify("Couldn't read which sources hold these files. Pick a source first.", "warn");
        return null;
      }
    },

    _partsOf(items) {
      return items.map((i) => ({ source: i.source || null, prefix: i.path }));
    },
    _partsLabel(items) {
      const folders = items.filter((i) => i.kind === "folder").length;
      const files = items.length - folders;
      return [folders && countOf(folders, "folder"), files && countOf(files, "file")].filter(Boolean).join(" and ");
    },

    // ---- Exclude & delete flow ----
    // Step 1 is the Exclude panel (exDialog), step 2 the Delete panel (files) or the folder
    // removal plan, step 3 the Review. Each step keeps its own state, so Back is lossless.
    flowStart() {
      const d = this.exDialog;
      if (!d) return;
      const items = d.multi ? d.items.filter((i) => i.on) : [{ kind: d.kind, path: d.path, src: d.src }];
      const folders = items.filter((i) => i.kind === "folder");
      this.flow = { step: 2, kind: folders.length ? "folder" : "files", diffs: [] };
      this.showExclude = false;
      if (this.flow.kind === "folder") {
        if (this.removePlan && this.removePlan.flow) { this.showRemove = true; return; }
        if (items.length === 1) this.openRemoveFolderPath(folders[0].path, null);
        else this.openRemoveParts(items.map((i) => ({ source: i.kind === "folder" ? null : (i.src || this._exSourceFor(i) || null), prefix: i.path })), this._partsLabel(items));
        if (this.removePlan) this.removePlan.flow = true;
        return;
      }
      if (this.delPanel && this.delPanel.flow) { this.showDel = true; return; }
      const files = items.map((i) => ({ source: i.src || this._exSourceFor(i), full_path: i.path }));
      this.openDeletePanel(files, { flow: true });
    },

    flowBackToExclude() {
      if (!this.flow) return;
      this.flow.step = 1;
      this.showDel = false;
      this.showRemove = false;
      this.showExclude = true;
    },

    async flowReview() {
      if (!this.flow) return;
      const writes = this.exWrites();
      if (!writes.length) { this.error = "Pick where the rule goes."; return; }
      if (this.flow.kind === "files" && !this.delReady()) return;
      if (this.flow.kind === "folder" && !(this.removePlan && !this.removePlan.loading && this.removePlan.item_count)) return;
      this.flow.step = 3;
      this.flow.writes = writes;
      this.showDel = false;
      this.showRemove = false;
      this.showReview = true;
      this.flow.diffs = writes.map((w) => ({ target: w.target, label: this.exLabel(w.target), before: null, after: null, error: "" }));
      await Promise.all(writes.map(async (w, i) => {
        try {
          const r = await getJson("/api/policies/diff", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ repo: this.repo, target: w.target, changes: { add: w.add, remove: [] }, price: false }) });
          if (this.flow) Object.assign(this.flow.diffs[i], { before: r.current_rules, after: r.resulting_rules });
        } catch (e) {
          if (this.flow) this.flow.diffs[i].error = e.message || String(e);
        }
      }));
    },

    flowBackToDelete() {
      if (!this.flow) return;
      this.flow.step = 2;
      this.showReview = false;
      if (this.flow.kind === "folder") this.showRemove = true;
      else this.showDel = true;
    },

    flowCancel() {
      this.flow = null;
      this.showReview = false;
      this.showDel = false; this.delPanel = null;
      this.showExclude = false; this.exDialog = null;
      if (this.removePlan && this.removePlan.flow) this.closeRemove();
    },

    flowDeleteSpec() {
      if (!this.flow) return null;
      if (this.flow.kind === "folder") {
        // the ticket travels with the delete step (including policy_write's `then`), so the
        // job removes exactly the previewed list - an expired one refuses server-side.
        return { kind: "remove_files",
                 params: { ...this._removeBody(), form: "plan",
                           plan_token: this.removePlan && this.removePlan.plan_token } };
      }
      return this.delJobSpec();
    },

    flowDeleteSources() {
      if (!this.flow) return [];
      if (this.flow.kind === "folder") return (this.removePlan && this.removePlan.sources_scope) || [];
      return this.delScopeSources();
    },

    // Backups the delete covers that will still back the path up after the rule.
    flowStillBackedUp() {
      const still = new Set(this.exStillSources());
      return this.flowDeleteSources().filter((s) => still.has(s));
    },

    flowDeleteFacts() {
      if (!this.flow) return [];
      if (this.flow.kind === "files") return this.delFacts().slice(1);
      const p = this.removePlan;
      return [
        ["Folder", p.label],
        ["Match", p.by === "filename" ? "File name" : "Exact content"],
        ["Removes", `${this.removeFilesSplit() || countOf(this.removeFileTotal(), "file")} · ${this.removeAllVersions() ? "every version" : "latest version"}`],
        ["Sources", p.everywhere ? "Every source in the repository" : (p.sources_scope || []).map((s) => sourcePath(s)).join(", ")],
        [term("reclaimable"), this.removeFreesText()],
      ];
    },

    flowDryState() {
      if (!this.flow) return null;
      return this.flow.kind === "folder" ? (this.removePlan && this.removePlan.dry) : (this.delPanel && this.delPanel.dry);
    },

    flowDry() {
      if (!this.flow) return;
      return this.flow.kind === "folder" ? this.removePlanDry() : this.delDry();
    },

    async flowExecute() {
      if (!this.flow || !this.flow.writes) return;
      const then = this.flowDeleteSpec();
      const still = this.flowStillBackedUp();
      const dry = this.flowDryState();
      const facts = [
        ["Rules", this.flow.writes.map((w) => `${this.exLabel(w.target)}: ${w.add.length === 1 ? w.add[0] : countOf(w.add.length, "rule")}`).join(" · ")],
        ...this.flowDeleteFacts(),
      ];
      const ok = await this.klConfirm({
        title: "Exclude and delete?",
        facts,
        callout: still.length ? `Still backed up by ${still.map((s) => sourcePath(s)).join(", ")}. Its next snapshot backs this up again.` : "",
        note: (dry && dry.done && dry.ok ? "" : "No dry run yet. ") + "Rules are added first. If one fails, the delete waits for you.",
        command: "", confirmLabel: "Exclude and delete", danger: true,
      });
      if (!ok) return;
      const writes = this.flow.writes;
      const jobId = await this.startJob("policy_write", { writes, then }, this.flow.kind === "folder" ? this.removePlan.label : this.delFileLabel());
      if (!jobId) {
        // The folder flow's delete step is a previewed plan: an expired ticket refuses, so show
        // the fresh preview instead of leaving the review step pointing at a stale list.
        if (this.flow && this.flow.kind === "folder" && this.removePlan) {
          this.klDryClear(this.removePlan.dry);   // the old dry run described the old list
          this._fetchRemovePlan();
        }
        return;
      }
      this._jobThen[jobId] = then;
      if (this.detail) this.detail = null;
      this.flowCancel();
    },
};
