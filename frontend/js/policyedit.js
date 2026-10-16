// Policy editor: every kopia policy field on the Policies tab. Field edits stage in
// polStaged[target].fields (JSON path -> value, null = inherit) next to the rule edits; nothing
// reaches kopia without Review + Save. /api/policies/resolve previews the effective values and
// where each one comes from after the staged edits. Multi-policy editing and rule Preview live here too.
const POL_SECTIONS = [
  { id: "exclusions", label: "Exclusions" }, { id: "retention", label: "Retention" },
  { id: "schedule", label: "Schedule" }, { id: "files", label: "Files" },
  { id: "compression", label: "Compression" }, { id: "advanced", label: "Advanced" },
];
const POL_INTERVALS = [
  [600, "10 minutes"], [900, "15 minutes"], [1800, "30 minutes"], [3600, "1 hour"], [7200, "2 hours"],
  [10800, "3 hours"], [21600, "6 hours"], [43200, "12 hours"], [86400, "24 hours"], [172800, "2 days"],
];
const POL_UNITS = [["KB", 1e3], ["MB", 1e6], ["GB", 1e9], ["TB", 1e12]];
const POL_UNITS_BIN = [["KiB", 2 ** 10], ["MiB", 2 ** 20], ["GiB", 2 ** 30], ["TiB", 2 ** 40]];

const policyEditPart = {

    // ---------- meta + resolve ----------
    async polLoadMeta() {
      if (this.polMeta && this.polMeta.repo === this.repo) return;
      const m = await getJson(`/api/policies/meta?repo=${encodeURIComponent(this.repo)}`).catch(() => null);
      if (m) this.polMeta = { ...m, repo: this.repo };
    },

    polSections() { return POL_SECTIONS; },

    polStagedChanges(target) {
      const st = this.polStaged[target] || {};
      return { add: st.add || [], remove: st.remove || [], delete_policy: !!st.removePolicy, fields: st.fields || {}, order: st.order || null };
    },

    polResolve(target, immediate = false) {
      target = target || this.polSel;
      if (!target) return;
      if (this._polResolveTimer) clearTimeout(this._polResolveTimer);
      const run = async () => {
        const seq = ++this._polResolveSeq;
        const changes = this.polStagedChanges(target);
        const impact = Object.keys(changes.fields).some((p) => p.startsWith("retention."));
        try {
          const r = await getJson("/api/policies/resolve", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ repo: this.repo, target, changes: { ...changes, delete_policy: false }, impact }) });
          if (seq !== this._polResolveSeq) return;
          this.polResolved = { ...this.polResolved, [target]: r };
        } catch (e) {
          if (seq === this._polResolveSeq) this.polResolveError = e.message || String(e);
        }
      };
      if (immediate) run(); else this._polResolveTimer = setTimeout(run, 300);
    },

    polRes() { return (this.polResolved || {})[this.polSel] || null; },

    // ---------- field access (single policy or the multi-edit draft) ----------
    polPath(obj, path) {
      let cur = obj;
      for (const part of path.split(".")) {
        if (!cur || typeof cur !== "object" || !(part in cur)) return undefined;
        cur = cur[part];
      }
      return cur;
    },
    polFields(section, group) {
      const f = (this.polMeta && this.polMeta.fields) || [];
      return f.filter((x) => x.section === section && (group === undefined || (x.group || "") === group));
    },
    polAdvGroups() {
      const seen = [];
      for (const f of this.polFields("advanced")) if (!seen.includes(f.group)) seen.push(f.group);
      return seen;
    },
    edStaged(path) {
      if (this.polMulti.on) return path in this.polMulti.fields;
      const st = this.polStaged[this.polSel];
      return !!(st && st.fields && path in st.fields);
    },
    edGet(path) {
      if (this.polMulti.on) return this.polMulti.fields[path];
      const st = this.polStaged[this.polSel];
      if (st && st.fields && path in st.fields) return st.fields[path];
      const r = this.polRes();
      return r ? this.polPath(r.defined, path) : undefined;
    },
    edSet(path, value) {
      if (value === "" || (Array.isArray(value) && !value.length)) value = null;
      if (this.polMulti.on) {
        if (value === undefined) delete this.polMulti.fields[path]; else this.polMulti.fields[path] = value;
        this.polMulti.fields = { ...this.polMulti.fields };
        this.polMultiPlan();
        return;
      }
      const st = this.polStagedFor(this.polSel);
      if (!st.fields) st.fields = {};
      const r = this.polRes();
      const saved = r ? this.polPath(r.defined, path) : undefined;
      const same = JSON.stringify(saved === undefined ? null : saved) === JSON.stringify(value === undefined ? null : value);
      if (value === undefined || same) delete st.fields[path]; else st.fields[path] = value;
      st.fields = { ...st.fields };
      this.polResolve();
    },
    edUndo(path) { this.edSet(path, undefined); },

    polEff(path) { const r = this.polRes(); return r ? this.polPath(r.effective, path) : undefined; },
    polEffBefore(path) { const r = this.polRes(); return r ? this.polPath(r.effective_before, path) : undefined; },
    polFrom(path) { const r = this.polRes(); return r ? (r.definition[path] || "(global)") : ""; },
    polFromText(path) {
      const r = this.polRes();
      if (!r) return "";
      const src = this.polFrom(path);
      const globalHas = this.polPath(r.global_defined, path) !== undefined;
      if (src === r.target && (src !== "(global)" || globalHas)) return "set here";
      if (src === "(global)") return globalHas ? "from Global" : "kopia default";
      return "from " + this.polTargetLabel(src);
    },
    polStageCountFor(target) {
      const st = this.polStaged[target];
      if (!st) return 0;
      return (st.add || []).length + (st.remove || []).length + (st.removePolicy ? 1 : 0) + Object.keys(st.fields || {}).length + (st.order ? 1 : 0);
    },
    polReviewLabel() {
      const n = this.polStageCount();
      const m = Object.keys(this.polStaged).filter((t) => this.polHasStaged(t)).length;
      return `Review ${n} change${n === 1 ? "" : "s"}` + (m > 1 ? ` in ${m} policies` : "") + "…";
    },
    polRailOthers(kind) {
      const f = this.polFilter.trim().toLowerCase();
      return ((this.polData && this.polData.others) || [])
        .filter((o) => o.kind === kind && (!f || o.target.toLowerCase().includes(f)))
        .map((o) => ({ ...o, depth: 0 }));
    },
    polFromTarget(path) {
      const src = this.polFrom(path);
      const r = this.polRes();
      if (!r || src === r.target) return "";
      if (src === "(global)") return this.polPath(r.global_defined, path) === undefined || r.target === "(global)" ? "" : "global";
      return src;
    },
    polTargetLabel(key) {
      if (!key || key === "(global)" || key === "global") return "Global";
      return key.includes(":") ? sourcePath(key) : key;
    },

    // A plain field (zero = unset) that an ancestor set can be changed here but never cleared.
    polCantClear(f) {
      if (this.polMulti.on || !["plain", "list"].includes(f.kind)) return "";
      const r = this.polRes();
      if (!r) return "";
      const own = this.edGet(f.path);
      const eff = this.polEff(f.path);
      const src = this.polFrom(f.path);
      const hasEff = Array.isArray(eff) ? eff.length > 0 : !!eff;
      if ((own && !(Array.isArray(own) && !own.length)) || !hasEff || src === r.target) return "";
      const by = this.polFromText(f.path).replace(/^from /, "");
      if (f.type === "flag") return `Turned on by ${by}. It can't be turned off here.`;
      return `Set by ${by}. You can set another value here, but not clear it.`;
    },
    polGlobalOnly(f) { return !!f.global_only && !this.polMulti.on && (this.polSel || "").includes(":"); },

    // ---------- value display ----------
    polShow(f, v) {
      if ((v === undefined || v === null) && f.default !== undefined) v = f.default;
      if (v === undefined || v === null || (Array.isArray(v) && !v.length)) {
        if (f.type === "flag") return "Off";
        if (f.path === "splitter.algorithm") return "Repository default";
        if (f.path === "upload.maxParallelFileReads") return "Automatic";
        return "Not set";
      }
      switch (f.type) {
        case "bool": return v ? "Yes" : "No";
        case "flag": return v ? "On" : "Off";
        case "bytes": return fmtBytes(v);
        case "interval": return this.polIntervalText(v);
        case "tod": return v.map((t) => `${String(t.hour).padStart(2, "0")}:${String(t.min).padStart(2, "0")}`).join(", ");
        case "cron": case "strlist": return v.join(", ");
        case "vss": return { 0: "Never", 1: "Always", 2: "When available" }[v] || "Never";
        case "action": return v.script ? "Embedded script" : [v.path, ...(v.args || [])].join(" ");
        default: return String(v);
      }
    },
    polIntervalText(sec) {
      const hit = POL_INTERVALS.find(([s]) => s === sec);
      if (hit) return "every " + hit[1];
      if (sec % 3600 === 0) return `every ${sec / 3600} hours`;
      if (sec % 60 === 0) return `every ${sec / 60} minutes`;
      return `every ${sec} seconds`;
    },
    polIntervals() { return POL_INTERVALS; },
    polUnits() { return _byteUnits === "binary" ? POL_UNITS_BIN : POL_UNITS; },
    polOptions(f) { return f.options === "compressors" ? (this.polMeta.compressors || []) : f.options === "splitters" ? (this.polMeta.splitters || []) : []; },

    // ---------- typed editors ----------
    edNum(path, raw) {
      const t = String(raw).trim();
      if (t === "") return this.edSet(path, null);
      const n = Number(t);
      if (Number.isFinite(n) && n >= 0) this.edSet(path, Math.round(n));
    },
    edBytesParts(path) {
      const v = this.edGet(path);
      const units = this.polUnits();
      if (!v) return { num: "", unit: units[2][0] };
      const unit = [...units].reverse().find(([, m]) => v >= m && v % (m / 10) === 0)
        || [...units].reverse().find(([, m]) => v >= m) || units[1];
      return { num: String(Math.round((v / unit[1]) * 100) / 100), unit: unit[0] };
    },
    edBytes(path, num, unit) {
      const t = String(num).trim();
      if (t === "") return this.edSet(path, null);
      const units = this.polUnits();
      const m = (units.find(([u]) => u === unit) || units[2])[1];
      const n = Number(t);
      if (Number.isFinite(n) && n >= 0) this.edSet(path, Math.round(n * m));
    },
    edList(path, raw) {
      const items = String(raw).split(",").map((s) => s.trim()).filter(Boolean);
      this.edSet(path, [...new Set(items)]);
    },
    edTodAdd(path, raw) {
      const m = /^(\d{1,2}):(\d{2})$/.exec(String(raw).trim());
      if (!m || +m[1] > 23 || +m[2] > 59) { this.error = "Enter a time as HH:MM, for example 22:30."; return false; }
      const cur = [...(this.edGet(path) || [])];
      if (!cur.some((t) => t.hour === +m[1] && t.min === +m[2])) cur.push({ hour: +m[1], min: +m[2] });
      cur.sort((a, b) => a.hour - b.hour || a.min - b.min);
      this.edSet(path, cur);
      return true;
    },
    edTodDel(path, i) { const cur = [...(this.edGet(path) || [])]; cur.splice(i, 1); this.edSet(path, cur); },
    edCron(path, raw) {
      this.edSet(path, String(raw).split(/\r?\n/).map((s) => s.trim()).filter(Boolean));
    },
    edAction(path, key, raw) {
      const cur = { ...(this.edGet(path) || {}) };
      if (key === "args") cur.args = String(raw).trim() ? String(raw).trim().split(/\s+/) : undefined;
      else if (key === "timeout") cur.timeout = String(raw).trim() ? Math.max(0, Math.round(Number(raw))) : undefined;
      else cur[key] = String(raw).trim() || undefined;
      Object.keys(cur).forEach((k) => cur[k] === undefined && delete cur[k]);
      this.edSet(path, (cur.path || cur.script) ? cur : null);
    },

    // ---------- summary + section counts ----------
    polSecCount(sec) {
      if (this.polMulti.on) return Object.keys(this.polMulti.fields).filter((p) => (this.polFieldSpec(p) || {}).section === sec).length;
      const st = this.polStaged[this.polSel];
      if (!st) return 0;
      if (sec === "exclusions") return (st.add || []).length + (st.remove || []).length + (st.removePolicy ? 1 : 0) + (st.order ? 1 : 0);
      return Object.keys(st.fields || {}).filter((p) => (this.polFieldSpec(p) || {}).section === sec).length;
    },
    polFieldSpec(path) { return ((this.polMeta && this.polMeta.fields) || []).find((f) => f.path === path); },
    polSetSection(id) {
      this.polSection = id;
      if (this.polLayout === "scroll") this.$nextTick(() => { const el = document.getElementById("polsec-" + id); if (el) el.scrollIntoView({ block: "start", behavior: "smooth" }); });
    },
    polScrollSpy(el) {
      if (this.polLayout !== "scroll" || !el) return;
      let cur = POL_SECTIONS[0].id;
      for (const s of POL_SECTIONS) {
        const node = document.getElementById("polsec-" + s.id);
        if (node && node.offsetTop - el.offsetTop - 70 <= el.scrollTop) cur = s.id;
      }
      this.polSection = cur;
    },
    polSummaryRetention() {
      const r = this.polRes();
      if (!r) return "";
      const ret = r.effective.retention || {};
      const parts = [["keepLatest", "latest"], ["keepHourly", "hourly"], ["keepDaily", "daily"], ["keepWeekly", "weekly"], ["keepMonthly", "monthly"], ["keepAnnual", "yearly"]]
        .filter(([k]) => ret[k]).map(([k, s]) => `${ret[k]} ${s}`);
      return parts.length ? "Keeps " + parts.join(", ") : "Keeps everything";
    },
    // The summary under the policy name: retention, schedule, rules, compression.
    polHeaderLine() {
      const r = this.polRes();
      if (!r) return "";
      const inh = this.polInheritedRules().length;
      const cname = ((r.effective || {}).compression || {}).compressorName;
      const comp = (r.effective || {}).compression ? (cname && cname !== "none" ? cname + " compression" : "no compression") : "";
      return [this.polSummaryRetention(), this.polSummarySchedule(),
        countOf(this.polRuleRows().length, "own rule") + (inh ? ` · ${inh} inherited` : ""), comp].filter(Boolean).join(" · ");
    },
    // A cron line's meaning with its times in the chosen clock style; "" when there is none.
    polCronMeaning(c) {
      const m = c && c.meaning && c.meaning !== "Custom schedule" ? c.meaning : "";
      return m.replace(/\b([01]?\d|2[0-3]):([0-5]\d)\b/g, (_, h, mi) => { const d = new Date(); d.setHours(+h, +mi, 0, 0); return _clockFmt.format(d); });
    },
    // "in 21 hours", "in 2 days": rounded to the nearest unit, the way KopiaUI words it.
    polRel(t) {
      const mins = (toDate(t).getTime() - Date.now()) / 60000;
      if (!(mins > 0)) return "now";
      if (mins < 60) return `in ${countOf(Math.max(1, Math.round(mins)), "minute")}`;
      const hours = mins / 60;
      if (hours < 22) return `in ${countOf(Math.round(hours), "hour")}`;
      if (hours < 36) return "in a day";
      return `in ${countOf(Math.round(hours / 24), "day")}`;
    },
    polSummarySchedule() {
      const r = this.polRes();
      if (!r) return "";
      const s = r.effective.scheduling || {};
      if (s.manual) return "Manual only";
      const bits = [];
      if (s.intervalSeconds) bits.push(this.polIntervalText(s.intervalSeconds));
      if ((s.timeOfDay || []).length) bits.push("at " + this.polShow({ type: "tod" }, s.timeOfDay));
      for (const c of (r.cron || [])) bits.push(this.polCronMeaning(c) || "cron " + c.line);
      if (!r.cron.length && (s.cron || []).length) bits.push("cron " + s.cron.join(", "));
      return bits.length ? bits.join(" · ") : "No schedule";
    },
    polParent() { const r = this.polRes(); return r && r.chain.length > 1 ? r.chain[1] : ""; },
    polNextRuns() {
      const r = this.polRes();
      if (!r || (r.effective.scheduling || {}).manual) return [];
      return (r.next_snapshots || []).slice(0, 5).map((t) => ({ when: fmtAbs(t), rel: this.polRel(t) }));
    },
    polNextEmpty() {
      const r = this.polRes();
      if (!r) return "";
      return (r.effective.scheduling || {}).manual ? "Manual only: no scheduled snapshots." : "No scheduled snapshots.";
    },
    polActionsOff() { return !!(this.polMeta && this.polMeta.client && this.polMeta.client.host && !this.polMeta.client.enable_actions); },
    polActionsOn() { return !!(this.polMeta && this.polMeta.client && this.polMeta.client.enable_actions); },
    async polSetScripts(enable) {
      if (!this.wmGuard("non_destructive")) return;
      const ok = await this.klConfirm({
        title: enable ? "Turn on snapshot scripts for this computer?" : "Turn off snapshot scripts for this computer?",
        facts: [["Repository", this.repoName(this.repo)], ["Computer", this.myOwner() || "This computer"]],
        note: enable ? "Scripts set in this repository's policies will run on this computer before and after backups." : "Scripts in this repository's policies stop running on this computer.",
        confirmLabel: enable ? "Turn on scripts" : "Turn off scripts", danger: false,
      });
      if (!ok) return;
      try {
        const r = await getJson("/api/client/scripts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, enable }) });
        this.polMeta = null;
        await this.polLoadMeta();
        const apps = r.restart_apps || [];
        this.klNotify((enable ? "Scripts turned on" : "Scripts turned off") + (apps.length ? `. Restart ${apps.join(" and ")} so it uses the new setting.` : ""), apps.length ? "warn" : "ok");
      } catch (e) { this.klNotify(e.message, "error"); }
    },

    // ---------- leaving a policy with unsaved changes ----------
    polChangeLines(target) {
      const st = this.polStaged[target];
      if (!st) return [];
      const lines = [];
      for (const [p, v] of Object.entries(st.fields || {})) {
        const f = this.polFieldSpec(p) || { label: p, type: "" };
        lines.push(`${f.label}: ${v === null ? "inherit" : this.polShow(f, v)}`);
      }
      for (const p of (st.add || [])) lines.push("Add rule " + p);
      for (const p of (st.remove || [])) lines.push("Remove rule " + p);
      if (st.order) lines.push("New rule order");
      if (st.removePolicy) lines.push("Remove this policy");
      return lines;
    },
    polTryLeave(target) {
      if (target === this.polSel) return;
      if (this.polLeave === "ask" && this.polHasStaged(this.polSel)) {
        this.polLeaveBox = { from: this.polSel, to: target, lines: this.polChangeLines(this.polSel) };
        return;
      }
      this.polSelect(target);
    },
    polLeaveChoice(choice) {
      const box = this.polLeaveBox;
      this.polLeaveBox = null;
      if (!box) return;
      if (choice === "discard") {
        delete this.polStaged[box.from];
        this.polStaged = { ...this.polStaged };
        this.polSelect(box.to);
      } else if (choice === "save") {
        this.polOpenDiff([box.from]);
      }
    },
    async polConfirmDiscard(reason) {
      const n = this.polStageCount();
      if (!n) return true;
      const targets = Object.keys(this.polStaged).filter((t) => this.polHasStaged(t));
      return this.klConfirm({
        title: `Discard ${n} unsaved change${n === 1 ? "" : "s"}?`,
        facts: targets.slice(0, 6).map((t) => [this.polTargetLabel(t), this.polChangeLines(t).slice(0, 4).join("; ") + (this.polChangeLines(t).length > 4 ? "; …" : "")]),
        note: reason || "", confirmLabel: `Discard ${n} change${n === 1 ? "" : "s"}`, danger: true,
      });
    },
    async polDiscardAsk() {
      if (await this.polConfirmDiscard()) { this.polDiscard(); this.polResolve(this.polSel, true); }
    },

    // ---------- new policy ----------
    polOpenNew() {
      const c = (this.polMeta && this.polMeta.client) || {};
      const hosts = [...new Set((this.polData.sources || []).map((s) => s.source.split(":")[0]))];
      const mine = c.user && c.host ? `${c.user}@${c.host}` : (hosts[0] || "");
      this.polNewBox = { owner: hosts.includes(mine) ? mine : (hosts[0] || mine), owners: hosts.length ? hosts : [mine], path: "", error: "" };
    },
    polCreateNew() {
      const box = this.polNewBox;
      let p = (box.path || "").trim();
      const winRoot = /^[A-Za-z]:[\\/]?$/.test(p);
      if (!p || !(/^[A-Za-z]:[\\/]/.test(p) || winRoot || p.startsWith("/") || p.startsWith("\\\\"))) { box.error = "Enter a full folder path."; return; }
      if (winRoot) p = p.slice(0, 2) + "\\";
      else if (p.length > 1) p = p.replace(/[\\/]+$/, "");
      const target = `${box.owner}:${p}`;
      const known = [...(this.polData.sources || []).map((s) => s.source), ...(this.polData.others || []).map((o) => o.target)];
      this.polNewBox = null;
      if (!known.includes(target) && !this.polNewTargets.includes(target)) this.polNewTargets = [...this.polNewTargets, target];
      this.polSelect(target);
    },

    // ---------- rule Preview ----------
    async polOpenPreview(pattern, state) {
      const mode = state === "staged-add" ? "index" : "disk";
      this.polPreview = { pattern, mode, target: this.polSel, source: null, data: null, busy: true };
      await this.polRunPreview();
    },
    async polRunPreview() {
      const pv = this.polPreview;
      if (!pv) return;
      pv.busy = true;
      const seq = ++this._polPreviewSeq;
      const rules = pv.mode === "disk" ? this.polOwnPatterns() : null;
      try {
        const r = await getJson("/api/policies/preview", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, target: pv.target === "global" ? "global" : pv.target, pattern: pv.pattern,
            source: pv.source, mode: pv.mode, rules: rules && rules.length ? rules : null, limit: 500 }) });
        if (seq !== this._polPreviewSeq || !this.polPreview) return;
        this.polPreview = { ...this.polPreview, data: r, source: r.source, busy: false };
      } catch (e) {
        if (seq === this._polPreviewSeq && this.polPreview) this.polPreview = { ...this.polPreview, data: { items: [], note: e.message || String(e) }, busy: false };
      }
    },
    polPreviewGroups() {
      const d = this.polPreview && this.polPreview.data;
      if (!d || !(d.items || []).length) return [];
      const src = d.source || "";
      const sep = pathSep(sourcePath(src));
      const groups = {};
      for (const it of d.items) {
        const i = it.path.lastIndexOf(sep);
        const folder = i > 0 ? it.path.slice(0, i) : it.path;
        (groups[folder] = groups[folder] || { folder, items: [], bytes: 0 }).items.push(it);
        groups[folder].bytes += it.size || 0;
      }
      return Object.values(groups).sort((a, b) => b.bytes - a.bytes);
    },
    polPreviewName(it) {
      const sep = pathSep(it.path);
      return it.path.slice(it.path.lastIndexOf(sep) + 1) + (it.is_dir ? sep : "");
    },

    // ---------- multi-policy editing ----------
    polMultiToggle() {
      if (!this.polMulti.on && this.polStageCount()) {
        this.error = "Save or discard the unsaved changes before editing several policies.";
        return;
      }
      this.polMulti = { on: !this.polMulti.on, picked: [], fields: {}, plan: null, ticks: {}, busy: false };
    },
    polMultiPick(target) {
      const picked = this.polMulti.picked.includes(target)
        ? this.polMulti.picked.filter((t) => t !== target) : [...this.polMulti.picked, target];
      this.polMulti = { ...this.polMulti, picked, plan: null, ticks: {} };
      this.polMultiPlan();
    },
    polMultiPlan() {
      if (this._polMultiTimer) clearTimeout(this._polMultiTimer);
      this._polMultiTimer = setTimeout(async () => {
        const m = this.polMulti;
        if (!m.on || m.picked.length < 2 || !Object.keys(m.fields).length) { this.polMulti = { ...m, plan: null }; return; }
        const seq = ++this._polMultiSeq;
        try {
          const r = await getJson("/api/policies/multi-plan", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ repo: this.repo, targets: m.picked, fields: m.fields }) });
          if (seq !== this._polMultiSeq) return;
          const ticks = { ...m.ticks };
          for (const p of r.plans) for (const row of p.rows) {
            const k = p.field + "|" + row.target;
            if (!(k in ticks)) ticks[k] = row.inherits;
          }
          this.polMulti = { ...this.polMulti, plan: r.plans, ticks };
        } catch (e) { this.error = e.message || String(e); }
      }, 300);
    },
    polMultiTick(field, target) {
      const k = field + "|" + target;
      this.polMulti = { ...this.polMulti, ticks: { ...this.polMulti.ticks, [k]: !this.polMulti.ticks[k] } };
    },
    polMultiSummary(path) {
      const plan = (this.polMulti.plan || []).find((p) => p.field === path);
      if (!plan) return "";
      const vals = [...new Set(plan.rows.map((r) => JSON.stringify(r.effective === undefined ? null : r.effective)))];
      const f = this.polFieldSpec(path) || {};
      return vals.length === 1 ? this.polShow(f, JSON.parse(vals[0])) : "mixed";
    },
    polMultiUseParent(field, parent) {
      const plan = (this.polMulti.plan || []).find((p) => p.field === field);
      if (!plan) return;
      const ticks = { ...this.polMulti.ticks };
      for (const row of plan.rows) ticks[field + "|" + row.target] = false;
      ticks[field + "|parent:" + parent] = true;
      this.polMulti = { ...this.polMulti, ticks };
    },
    polMultiWrites() {
      const byTarget = {};
      for (const p of (this.polMulti.plan || [])) {
        for (const row of p.rows) {
          if (this.polMulti.ticks[p.field + "|" + row.target]) (byTarget[row.target] = byTarget[row.target] || {})[p.field] = p.value;
        }
        if (p.common_parent && this.polMulti.ticks[p.field + "|parent:" + p.common_parent]) {
          (byTarget[p.common_parent] = byTarget[p.common_parent] || {})[p.field] = p.value;
        }
      }
      return Object.entries(byTarget).map(([target, fields]) => ({ target: target === "(global)" ? "global" : target, fields }));
    },
    async polMultiSave() {
      const writes = this.polMultiWrites();
      if (!writes.length) { this.error = "Tick at least one policy to write to."; return; }
      const nFields = writes.reduce((n, w) => n + Object.keys(w.fields).length, 0);
      const ok = await this.klConfirm({
        title: `Save to ${writes.length} polic${writes.length === 1 ? "y" : "ies"}?`,
        facts: writes.slice(0, 8).map((w) => [this.polTargetLabel(w.target), Object.entries(w.fields).map(([p, v]) => `${(this.polFieldSpec(p) || {}).label || p}: ${v === null ? "inherit" : this.polShow(this.polFieldSpec(p) || {}, v)}`).join("; ")]),
        note: `${nFields} setting${nFields === 1 ? "" : "s"} in total. A policy backup is taken before each write.`,
        confirmLabel: `Save ${writes.length} polic${writes.length === 1 ? "y" : "ies"}`, danger: false,
      });
      if (!ok) return;
      const started = await this.startJob("policy_write", { writes }, countOf(writes.length, "policy", "policies"));
      if (started) this.polMulti = { on: false, picked: [], fields: {}, plan: null, ticks: {}, busy: false };
    },

    // ---------- review lines ----------
    polFieldLine(fc) {
      const f = this.polFieldSpec(fc.field) || { label: fc.field, type: "" };
      const before = fc.before === null || fc.before === undefined
        ? `${this.polShow(f, fc.effective_before)} (${this.polReviewFrom(fc.from_before)})` : `${this.polShow(f, fc.before)} (set here)`;
      const after = fc.after === null || fc.after === undefined
        ? `${this.polShow(f, fc.effective_after)} (${this.polReviewFrom(fc.from_after)})` : `${this.polShow(f, fc.after)} (set here)`;
      return { label: f.label, before, after };
    },
    polReviewFrom(src) { return src === "(global)" ? "Global or kopia default" : "from " + this.polTargetLabel(src); },
};
