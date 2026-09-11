// Policies tab: every kopia policy for the repo in one view. Left rail = Global +
// every source, indented along parent-path chains; right pane = the selected policy with
// Excluded files first. Edits stage locally (`polStaged`) and reach kopia only through
// Review (the diff) + Save (a policy_write job through the shared runner - a backup is
// taken before the write, fail-closed, on the backend).
//
// Assumed API (every call below is wrapped in one small method,
// so adapting to the real shapes is a single-site edit):
//   GET  /api/policies?repo=<id>
//        -> {"global": <raw policy|null>, "ts": <epoch>,
//            "policies": [{"target": "user@host:path"|"global", "raw": {...},
//                          "summary": {...},                       // core.policy.summarize()
//                          "rules": [{"pattern", "files", "bytes"}],   // own rules, priced (nullable)
//                          "inherited": [{"pattern", "owner"}],        // effective rules owned elsewhere
//                          "parent": "target|null"}]}
//   POST /api/policies/price   {repo, target, patterns: [..]}
//        -> {"priced": [{"pattern", "files", "bytes", "examples": [..], "capped": bool}]}
//   POST /api/policies/diff    {repo, changes: [..]} | {repo, backup: "<file>"}
//        -> {"groups": [{"target", "items": [{"kind", "pattern"?, "from"?, "to"?,
//                                             "impact_files"?, "impact_bytes"?}]}]}
//   GET  /api/policies/backups?repo=<id> -> {"backups": [{"file", "ts", "reason", "size", "changed"}]}
//   POST /api/policies/backup  {repo} -> {"ok": bool}
//   Job kinds through the shared runner: "policy_write" {changes}, "policy_restore" {file, mirror}.
const policiesPart = {

    // ---------- load ----------
    async loadPolicies(refresh = false) {
      const seq = ++this._polSeq;
      try {
        const data = await this.wrap(async () =>
          getJson(`/api/policies?repo=${encodeURIComponent(this.repo)}`), "Loading policies...");
        if (seq !== this._polSeq || !data) return;
        this.polData = data;
        const live = new Set(this.polTargets().map((t) => t.target));
        if (!live.has(this.polSel)) this.polSel = "global";
      } catch (e) { /* wrap() already surfaced the error */ }
    },

    // ---------- left rail ----------
    // Global first, then sources sorted (UNC last), each indented one step per parent-policy
    // hop along same-host path prefixes. Two sources from different hosts never nest.
    polTargets() {
      if (!this.polData) return [];
      const pols = this.polData.policies || [];
      const host = (t) => (t.includes(":") ? t.slice(0, t.indexOf(":")) : "");
      const path = (t) => (t.includes(":") ? t.slice(t.indexOf(":") + 1) : "");
      const norm = (p) => p.replace(/[\\/]+$/, "").toLowerCase();
      const sorted = [...pols].sort((a, b) => srcSortKey(a.target).localeCompare(srcSortKey(b.target)));
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
                      rules: g ? (g.summary ? g.summary.ignore : (g.files ? g.files.ignore : [])) || [] : null }];
      for (const t of this.polTargets()) rows.push({ ...t, isGlobal: false });
      const f = this.polFilter.trim().toLowerCase();
      return f ? rows.filter((r) => r.isGlobal || sourcePath(r.target).toLowerCase().includes(f)) : rows;
    },

    polRailLabel(t) {
      if (t.isGlobal) return "Global policy";
      const p = sourcePath(t.target).replace(/[\\/]+$/, "");
      return p || t.target;
    },

    polSelect(target) { this.polSel = target; this.polAddText = ""; this.polAddPrice = null; },

    polSelected() {
      if (!this.polData) return null;
      if (this.polSel === "global") {
        return this.polData.global ? { target: "global", isGlobal: true, ...this.polData.global } : null;
      }
      return (this.polData.policies || []).find((p) => p.target === this.polSel) || null;
    },

    polOwnPatterns(row) {
      return row.summary ? (row.summary.ignore || [])
        : (row.raw && row.raw.files ? row.raw.files.ignore || [] : []);
    },

    // ---------- rules ----------
    // Own rules + staged adds/removes merged into display rows. Pricing rides the row when
    // the backend supplied it (rules[]), else from a per-policy "Price rules" call.
    polPriceFor(row, pat) {
      for (const r of row.rules || []) if (r.pattern === pat) return r;
      const m = (this.polRowPrices || {})[row.target];
      return m ? m[pat] || null : null;
    },
    polRuleRows() {
      const row = this.polSelected();
      if (!row) return [];
      const st = this.polStaged[row.target] || { add: [], remove: [] };
      const own = this.polOwnPatterns(row).map((pat) => {
        const p = this.polPriceFor(row, pat);
        return {
          pattern: pat,
          files: p ? p.files : null,
          bytes: p ? p.bytes : null,
          state: st.remove.includes(pat) ? "staged-remove" : "own",
        };
      });
      const adds = st.add.map((pat) => {
        const p = (this.polAddPrices || {})[row.target + "|" + pat];
        return { pattern: pat, files: p ? p.files : null, bytes: p ? p.bytes : null, state: "staged-add" };
      });
      return [...adds, ...own];
    },

    polInheritedRules() {
      const row = this.polSelected();
      return row ? (row.inherited || []) : [];
    },

    // Keeping-out totals over the displayed own rules (staged removes excluded). Priced may
    // be below total until "Price rules" runs or the backend ships pricing in the payload.
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
      const row = this.polSelected();
      if (!row) return;
      const pats = this.polOwnPatterns(row);
      if (!pats.length) return;
      const seq = ++this._polRowPriceSeq;
      const priced = await this.polPricePatterns(row.target, pats);
      if (seq !== this._polRowPriceSeq || !priced) return;
      const m = (this.polRowPrices = this.polRowPrices || {})[row.target] = (this.polRowPrices[row.target] || {});
      for (const p of priced) m[p.pattern] = p;
    },

    // The one place that talks to the pricing endpoint; shapes adapt here.
    async polPricePatterns(target, patterns) {
      try {
        const r = await getJson("/api/policies/price", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target, patterns }) });
        return r.priced || [];
      } catch (e) { return []; }
    },

    polNoParentIgnore() {
      const row = this.polSelected();
      return !!(row && row.raw && row.raw.files && row.raw.files.noParentIgnore);
    },

    polTargetNote() {
      const row = this.polSelected();
      if (!row) return "";
      if (row.isGlobal) return "Bare patterns at Global match at any depth, in every source.";
      return "Anchored patterns (leading /) are relative to this policy's root: " + sourcePath(row.target);
    },

    // Render helpers: x-show does not stop x-text from evaluating, so anything derefing an
    // optional field lives here and returns "" instead of throwing.
    polAlsoLine() {
      const s = this.polSelected() && this.polSelected().summary;
      if (!s) return "";
      const bits = [];
      if ((s.ignore_dot_files || []).length) bits.push("dot-files: " + s.ignore_dot_files.join(", "));
      if (s.ignore_cache_dirs) bits.push("cache dirs");
      if (s.max_file_size) bits.push("files over " + fmtB(s.max_file_size));
      return bits.join(" · ");
    },
    polAddPreviewText() {
      const p = this.polAddPrice;
      if (!p || p.files == null) return "";
      return "Would exclude " + Number(p.files).toLocaleString() + " files · " + fmtB(p.bytes);
    },
    polAddExamplesText() {
      const p = this.polAddPrice;
      if (!p || !p.examples || !p.examples.length) return "";
      return "  e.g. " + p.examples.slice(0, 2).join(", ");
    },
    polImpactText(it) {
      if (!it || it.impact_files == null) return "";
      return Number(it.impact_files).toLocaleString() + " files · " + fmtB(it.impact_bytes || 0);
    },
    polRangeText(it) {
      if (!it) return "";
      return (it.from != null ? it.from + " → " : "") + (it.to != null ? it.to : "");
    },

    // ---------- staging ----------
    polStagedFor(target) {
      if (!this.polStaged[target]) this.polStaged[target] = { add: [], remove: [], removePolicy: false };
      return this.polStaged[target];
    },
    polStageAdd() {
      const row = this.polSelected();
      const pat = (this.polAddText || "").trim();
      if (!row || !pat) return;
      const st = this.polStagedFor(row.target);
      // Blocks duplicates AND patterns the policy already defines (even ones staged for
      // removal - staging add+remove of the same pattern is a contradiction).
      if (st.add.includes(pat) || this.polOwnPatterns(row).includes(pat)) return;
      st.add.push(pat);
      this.polAddText = ""; this.polAddPrice = null;
      this.polPriceStaged(row.target, pat);
    },
    polStageRemove(pat) {
      const row = this.polSelected();
      if (!row) return;
      const st = this.polStagedFor(row.target);
      st.remove.push(pat);
    },
    polStageRemovePolicy() {
      const row = this.polSelected();
      if (!row || row.isGlobal) return;
      this.polStagedFor(row.target).removePolicy = true;
    },
    polUndo(target, kind, pat) {
      const st = this.polStaged[target];
      if (!st) return;
      if (kind === "add") st.add = st.add.filter((p) => p !== pat);
      if (kind === "remove") st.remove = st.remove.filter((p) => p !== pat);
      if (kind === "policy") st.removePolicy = false;
    },
    polStageCount() {
      let n = 0;
      for (const st of Object.values(this.polStaged)) {
        n += st.add.length + st.remove.length + (st.removePolicy ? 1 : 0);
      }
      return n;
    },
    polDiscard() { this.polStaged = {}; },

    polHasStaged(target) {
      const st = this.polStaged[target];
      return !!(st && (st.add.length || st.remove.length || st.removePolicy));
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
      // Seq at call time: a slow answer for an older keystroke must never overwrite the
      // preview of the pattern now in the box (polSelect clears it, this guards races).
      const seq = ++this._polAddPriceSeq;
      const priced = await this.polPricePatterns(row.target, [pat]);
      if (seq !== this._polAddPriceSeq) return;
      this.polAddPrice = priced && priced[0] ? priced[0] : null;
    },
    async polPriceStaged(target, pat) {
      const priced = await this.polPricePatterns(target, [pat]);
      if (priced && priced[0]) {
        if (!this.polAddPrices) this.polAddPrices = {};
        this.polAddPrices[target + "|" + pat] = priced[0];
      }
    },

    // ---------- diff + save ----------
    polChanges() {
      const out = [];
      for (const [target, st] of Object.entries(this.polStaged)) {
        if (st.add.length || st.remove.length || st.removePolicy) {
          out.push({ target, add: st.add, remove: st.remove, remove_policy: st.removePolicy });
        }
      }
      return out;
    },
    async polOpenDiff() {
      const changes = this.polChanges();
      if (!changes.length) return;
      this.showPolDiff = true;
      this.polDiffBusy = true;
      this.polDiff = null;
      this.polDiffMode = "save";
      try {
        const r = await getJson("/api/policies/diff", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, changes }) });
        this.polDiff = r;
      } catch (e) {
        this.polDiff = { groups: [], error: e.message || String(e) };
      } finally {
        this.polDiffBusy = false;
      }
    },
    async polSave() {
      const changes = this.polChanges();
      if (!changes.length) { this.showPolDiff = false; return; }
      const nPols = changes.length;
      const nRules = changes.reduce((n, c) => n + c.add.length + c.remove.length + (c.remove_policy ? 1 : 0), 0);
      const ok = await this.klConfirm({
        title: `Save policy changes?`,
        facts: [
          ["What", `${nRules} change(s) across ${nPols} polic${nPols === 1 ? "y" : "ies"}`],
          ["Where", changes.map((c) => (c.target === "global" ? "Global" : sourcePath(c.target))).join(", ")],
          ["How", "kopia policy set / delete, one write per policy"],
          ["Safety", "A backup is taken before the write; if the backup fails, nothing is written"],
        ],
        note: "Kopia merges policy writes: fields you set take effect, fields you do not mention stay as they are.",
        command: "", confirmLabel: "Save changes", danger: false,
      });
      if (!ok) return;
      this.showPolDiff = false;
      const started = await this.startJob("policy_write", { changes }, "Policy write");
      if (started) {
        this.polDiscard();
        this.actionText = "Policy write running - live output in the job panel.";
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
        await getJson("/api/policies/backup", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo }) });
        this.actionText = "Policy backup written.";
        this.polOpenBackups();
      }, "Backing up policies...");
    },
    async polRestoreDiff(file) {
      this.showPolDiff = true;
      this.polDiffBusy = true;
      this.polDiff = null;
      this.polDiffMode = "restore";
      this.polRestoreFile = file;
      try {
        const r = await getJson("/api/policies/diff", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, backup: file }) });
        this.polDiff = r;
      } catch (e) {
        this.polDiff = { groups: [], error: e.message || String(e) };
      } finally {
        this.polDiffBusy = false;
      }
    },
    async polRestoreExecute(mirror) {
      const file = this.polRestoreFile;
      if (!file) return;
      const ok = await this.klConfirm({
        title: "Restore this policy backup?",
        facts: [
          ["What", mirror ? "Full mirror-restore: policies not in the backup are deleted" : "Merge-restore: policies in the backup overwrite their targets; others stay"],
          ["From", file],
          ["How", "kopia policy import" + (mirror ? " --delete-other-policies" : " (merge)")],
        ],
        callout: mirror ? "Mirror-restore replaces the whole policy set for this repo." : "",
        note: "KopiaLens restores only backups it made itself (data/policy-backups).",
        command: "", confirmLabel: mirror ? "Mirror-restore" : "Restore (merge)", danger: mirror,
      });
      if (!ok) return;
      this.showPolDiff = false;
      const started = await this.startJob("policy_restore", { file, mirror }, "Policy restore");
      if (started) this.actionText = "Policy restore running - live output in the job panel.";
    },
};
