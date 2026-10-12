// Shared ops: mounts (header chip + Tasks), the restore panel, downloads.
const RESTORE_PRESETS = {
  safe: { skip_existing: true, overwrite_files: false, overwrite_directories: false, overwrite_symlinks: false, delete_extra: false },
  replace: { skip_existing: false, overwrite_files: true, overwrite_directories: true, overwrite_symlinks: true, delete_extra: false },
  exact: { skip_existing: false, overwrite_files: true, overwrite_directories: true, overwrite_symlinks: true, delete_extra: true },
};
const DOWNLOAD_WARN_BYTES = 5 * 1024 ** 3;

const actionsPart = {

    // ---- Mount + open in the file manager (auto drive letter / mount folder) ----
    mountOpenLabel() { return this.host.can_open ? `Mount + open in ${this.host.file_manager}` : "Mount"; },
    mountFileOpen(item) {  // the newest snapshot holding the path
      this.mountOpen(null, item.source, item.full_path);
    },
    mountOpen(snapshotId, source, fullPath) {
      if (this.isDemo()) { this.klNotify("Mounting isn't available in the demo.", "warn"); return; }
      this.wrap(async () => {
        const r = await getJson("/api/actions/mount-open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: snapshotId || null, source, full_path: fullPath || null }) });
        const msg = `Mounted at ${r.shown || r.mountpoint}.` + (this.host.can_open ? ` Opening ${this.host.file_manager}.` : "");
        this.actionText = msg;
        this.klNotify(msg);
        this.loadMounts();
      }, "Mounting…");
    },
    mountVersionOpen(v) { if (this.detail) this.mountOpen(v.snapshot_id, this.detail.source, this.detail.title); },
    mountTreeSnapshot() {  // tree control-bar: mount the selected snapshot + open the current folder
      if (!this.selectedSnapshot) { this.error = "Pick a snapshot first."; return; }
      this.mountOpen(this.selectedSnapshot, this.selectedSource, this.treePrefix || this.sourceRoot());
    },

    async loadMounts() {
      const r = await getJson(`/api/actions/mounts`).catch(() => ({ mounts: [] }));
      this.mounts = r.mounts || [];
    },
    mountLabel(m) {
      return sourcePath(m.source || "") || `snapshot ${String(m.snapshot_id).slice(0, 8)}`;
    },
    async unmount(pid) {
      await this.wrap(async () => {
        await getJson("/api/actions/unmount", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pid }) });
        this.loadMounts();
      }, "Unmounting…");
    },
    async unmountAll() {
      const n = this.mounts.length;
      if (!n) return;
      const ok = await this.klConfirm({
        title: `Unmount ${n} snapshot${n === 1 ? "" : "s"}?`,
        facts: this.mounts.map((m) => [m.shown || m.mountpoint, this.mountLabel(m) + (m.snap_time ? " · " + this.humanTime(m.snap_time) : "")]),
        confirmLabel: `Unmount ${n}`,
      });
      if (!ok) return;
      await this.wrap(async () => {
        await getJson("/api/actions/unmount-all", { method: "POST" });
        this.loadMounts();
      }, "Unmounting…");
    },
    async mountBrowse(pid) {
      await getJson("/api/actions/mount-browse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pid }) })
        .catch((e) => { this.error = e.message; this.loadMounts(); });
    },

    // ---- Restore ----
    // items: [{source, full_path, snapshot_id?, content_id?}]; several items restore flat into one folder.
    // An item without a source (a path held by several sources) gets its holders looked up; the
    // newest backup is picked and the panel offers the others.
    async openRestore(items, opts = {}) {
      if (!this.wmGuard("restores")) return;
      const list = (Array.isArray(items) ? items : [items]).filter((i) => i && i.full_path)
        .map((i) => ({ ...i, source: i.source || null }));
      if (!list.length) { this.error = "Nothing to restore."; return; }
      this.restore = {
        items: list, holders: {}, mode: "folder", target: "", preset: "safe", plan: null, loading: false, error: "", check: null,
        label: opts.label || (list.length === 1 ? baseName(list[0].full_path) : `${list.length} items`),
        options: {
          restore_owners: true, restore_permissions: true, restore_times: true, ignore_errors: false,
          write_files_atomically: false, write_sparse_files: false, ignore_permission_errors: true,
          parallel: 8, shallow: "", shallow_minsize_kb: "", zip_uncompressed: false, ...RESTORE_PRESETS.safe,
        },
      };
      this.showRestore = true;
      if (list.some((i) => !i.source) && !(await this.restoreResolveSources())) return;
      this.restoreLoad(false);
    },
    closeRestore() { this.restoreCheckStop(); this.showRestore = false; this.restore = null; },

    async restoreResolveSources() {
      const r = this.restore;
      const seq = ++this._restoreFromSeq;
      const paths = [...new Set(r.items.filter((i) => !i.source).map((i) => i.full_path))];
      r.loading = true;
      let res = null;
      try {
        res = await getJson("/api/tree/holders", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, full_paths: paths, detail: paths.length <= 25, folders: true }) });
      } catch (e) {
        if (seq === this._restoreFromSeq && this.restore) { r.loading = false; r.error = e.message || String(e); }
        return false;
      }
      if (seq !== this._restoreFromSeq || !this.restore) return false;
      const when = (h) => (h.latest ? Math.floor(+toDate(h.latest) / 60000) : 0);   // same minute = same run: a tie
      const order = (x, y) => (when(y) - when(x)) || (sourcePath(y.source).length - sourcePath(x.source).length);
      const items = [], holders = {};
      for (const it of r.items) {
        if (!it.source) {
          const hs = ((res.paths || {})[it.full_path] || []).slice().sort(order);
          if (!hs.length) continue;
          it.source = hs[0].source;
          if (hs.length > 1) holders[items.length] = hs;
        }
        items.push(it);
      }
      r.items = items; r.holders = holders; r.loading = false;
      if (!items.length) { r.error = "No indexed source holds this."; return false; }
      return true;
    },
    restoreFromRows() {
      const r = this.restore;
      return r ? Object.keys(r.holders || {}).map((i) => ({ i: +i, path: r.items[i].full_path, holders: r.holders[i] })) : [];
    },
    restoreHolderLabel(h, all) {
      const hosts = new Set((all || []).map((x) => x.source.split(":")[0]));
      const parts = [hosts.size > 1 ? h.source : sourcePath(h.source)];
      if (h.snapshots) parts.push(countOf(h.snapshots, "snapshot"));
      if (h.latest) parts.push("newest " + this.humanTime(h.latest));
      return parts.join(" · ");
    },
    restorePickSource(i, source) {
      const r = this.restore;
      if (!r || !r.items[i] || r.items[i].source === source) return;
      r.items[i].source = source;
      this.restoreLoad(!!(r.plan && r.plan.previewed));
    },

    restorePresetOf(o) {
      for (const [name, p] of Object.entries(RESTORE_PRESETS)) {
        if (Object.entries(p).every(([k, v]) => !!o[k] === v)) return name;
      }
      return "custom";
    },
    restoreSetPreset(name) {
      const r = this.restore;
      if (!r || !RESTORE_PRESETS[name]) return;
      Object.assign(r.options, RESTORE_PRESETS[name]);
      this.restoreChanged();
    },
    restoreSetMode(mode) {
      const r = this.restore;
      if (!r || r.mode === mode) return;
      r.mode = mode;
      this.restoreChanged();
    },
    restoreChanged() {  // any option change invalidates the preview
      const r = this.restore;
      if (!r) return;
      this.restoreCheckStop();
      r.preset = this.restorePresetOf(r.options);
      if (r.plan && r.plan.previewed) this.restoreLoad(false);
    },
    restoreHasDirs() {
      const p = this.restore && this.restore.plan;
      return !!(p && p.items.some((i) => i.type === "d"));
    },

    // Above 100,000 files the preview checks the first 100,000; this compares every file in the
    // background, with progress and Stop, and replaces the preview when it finishes.
    restoreChecking() { const c = this.restore && this.restore.check; return !!(c && c.state === "running"); },
    async restoreCheckAll() {
      const r = this.restore;
      if (!r || this.restoreChecking()) return;
      r.check = { id: "", state: "running", listed: 0, error: "" };
      const check = r.check;
      try {
        const st = await getJson("/api/restore/check", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, items: r.items, target_mode: r.mode, target: r.target, options: r.options, preview: true }) });
        check.id = st.check_id;
        while (this.restore === r && r.check === check && check.state === "running") {
          await new Promise((ok) => setTimeout(ok, 1000));
          if (this.restore !== r || r.check !== check) break;
          const s = await getJson(`/api/restore/check/${check.id}`);
          check.listed = s.listed; check.state = s.state; check.error = s.error || "";
          if (s.state === "done" && s.plan) { s.plan.previewed = true; r.plan = s.plan; }
        }
        if (check.state === "stopped" && r.check === check) r.check = null;
      } catch (e) {
        check.state = "failed"; check.error = e.message || String(e);
      }
    },
    restoreCheckStop() {
      const r = this.restore, c = r && r.check;
      if (!c) return;
      if (c.id && c.state === "running") {
        getJson(`/api/restore/check/${c.id}/stop`, { method: "POST" }).catch(() => {});
      }
      r.check = null;
    },

    // preview=false: resolve items and targets only (cheap); true: also compare with the destination.
    async restoreLoad(preview) {
      const r = this.restore;
      if (!r) return;
      if (r.mode === "folder" && !r.target.trim()) {
        if (!preview) return this.restoreResolveOnly();
        r.error = "Pick a destination folder."; return;
      }
      const seq = ++this._restoreSeq;
      r.loading = true; r.error = "";
      try {
        const plan = await getJson("/api/restore/plan", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, items: r.items, target_mode: r.mode, target: r.target, options: r.options, preview }) });
        if (seq !== this._restoreSeq || !this.restore) return;
        plan.previewed = preview;
        r.plan = plan;
      } catch (e) {
        if (seq === this._restoreSeq && this.restore) { r.error = e.message || String(e); r.plan = null; }
      } finally {
        if (seq === this._restoreSeq && this.restore) r.loading = false;
      }
    },
    async restoreResolveOnly() {  // item facts before a destination is chosen
      const r = this.restore;
      const seq = ++this._restoreSeq;
      r.loading = true; r.error = "";
      try {
        const plan = await getJson("/api/restore/plan", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, items: r.items, target_mode: "folder", target: "_", options: r.options, preview: false }) });
        if (seq !== this._restoreSeq || !this.restore) return;
        plan.previewed = false;
        plan.items.forEach((i) => { i.target = ""; });
        r.plan = plan;
      } catch (e) {
        if (seq === this._restoreSeq && this.restore) r.error = e.message || String(e);
      } finally {
        if (seq === this._restoreSeq && this.restore) r.loading = false;
      }
    },
    restoreTargetInput() {
      const r = this.restore;
      if (!r) return;
      clearTimeout(this._restoreTimer);
      this._restoreTimer = setTimeout(() => this.restoreLoad(false), 400);
    },

    restoreDanger() {
      const r = this.restore, p = r && r.plan;
      if (!r) return false;
      return r.mode === "original" || !!r.options.delete_extra
        || !!(p && p.previewed && r.options.overwrite_files && p.totals.different);
    },

    restoreFacts() {
      const r = this.restore, p = r.plan, t = p.totals;
      const facts = [];
      if (p.items.length === 1) {
        const i = p.items[0];
        facts.push(["Restore", i.full_path]);
        if (i.snap_time) facts.push(["Snapshot", this.humanTime(i.snap_time)]);
        facts.push(["To", i.target]);
      } else {
        facts.push(["Restore", countOf(p.items.length, "item")]);
        facts.push(["To", r.mode === "folder" ? r.target : r.mode === "original" ? "Where each came from" : "Next to each original"]);
      }
      facts.push(["Files", `${Number(t.files).toLocaleString(NUM_LOCALE)} · ${fmtBytes(t.size)}`]);
      const expired = p.items.filter((i) => i.expired).length;
      if (expired) {
        facts.push(["Modified time", p.items.length === 1 ? "Not restored: the snapshot has expired"
          : `Not restored for ${countOf(expired, "item")}: their snapshots have expired`]);
      }
      facts.push(["Existing files", { safe: "Skipped", replace: "Replaced when different", exact: "Replaced when different", custom: "Custom options" }[r.preset]]);
      if (p.previewed) {
        const part = t.capped && !p.full_check ? " · first 100,000 files" : "";
        facts.push(["On disk now", `${t.new.toLocaleString(NUM_LOCALE)} new · ${t.identical.toLocaleString(NUM_LOCALE)} identical · ${t.different.toLocaleString(NUM_LOCALE)} different${part}`]);
        if (r.options.delete_extra) facts.push(["Deleted", t.extra_unchecked ? "Not checked" : `${countOf(t.extra, "file")} not in the snapshot`]);
      }
      return facts;
    },

    async restoreExecute() {
      const r = this.restore;
      if (!r || r.loading) return;
      if (!r.plan || (r.mode === "folder" && !r.target.trim())) { r.error = "Pick a destination folder."; return; }
      if (!r.plan.previewed) await this.restoreLoad(true);
      if (!this.restore || !r.plan || r.error) return;
      const t = r.plan.totals;
      let callout = "";
      if (r.options.delete_extra && t.extra_unchecked) callout = "Not checked: every file at the destination that isn't in the snapshot gets deleted.";
      else if (r.options.delete_extra && t.extra) callout = `${countOf(t.extra, "file")} at the destination ${t.extra === 1 ? "is" : "are"} not in the snapshot and will be deleted.`;
      else if (r.options.overwrite_files && t.different) callout = `${t.different.toLocaleString(NUM_LOCALE)} existing files will be replaced.`;
      else if (r.mode === "original") callout = "Restores over the original location.";
      const ok = await this.klConfirm({
        title: `Restore ${r.label}?`, facts: this.restoreFacts(), callout,
        confirmLabel: "Restore", danger: this.restoreDanger(),
      });
      if (!ok || !this.restore) return;
      const items = r.plan.items.map((i) => ({ source_id: i.source_id, target: i.target }));
      const labelTarget = r.mode === "folder" ? r.target : items[0].target;
      const started = await this.startJob("restore", { items, options: r.options, label_target: labelTarget }, `${r.label} to ${labelTarget}`);
      if (started) {
        this._restoreTargets[started] = labelTarget;
        this.closeRestore();
      }
    },

    async browseFolder() {
      const path = await this.pickFolder(this.restore && this.restore.target || null);
      if (path && this.restore) { this.restore.target = path; this.restoreLoad(false); }
    },
    // A folder on the computer KopiaLens runs on: the system picker in the desktop app (or on
    // this machine), the in-app browser otherwise.
    async pickFolder(initial) {
      const dialog = window.__TAURI__ && window.__TAURI__.dialog;
      if (dialog) return dialog.open({ directory: true, defaultPath: initial || undefined }).catch(() => null);
      if (this.host.folder_picker === "native") {
        const r = await getJson("/api/actions/browse-folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ initial }) }).catch(() => null);
        return r && r.path;
      }
      return this.fbOpen(initial);
    },

    // ---- In-app folder browser: the server's folders, for a browser on another machine or in Docker ----
    fbOpen(initial) {
      return new Promise((resolve) => {
        this.fb = { path: "", input: "", parent: "", folders: [], truncated: false, loading: false, error: "", resolve };
        this.fbGo(initial || "");
      });
    },
    async fbGo(path) {
      const fb = this.fb;
      if (!fb) return;
      fb.loading = true; fb.error = "";
      try {
        const r = await getJson("/api/actions/folders?path=" + encodeURIComponent(path || ""));
        Object.assign(fb, { path: r.path, input: r.path, parent: r.parent, folders: r.folders, truncated: r.truncated });
      } catch (e) {
        fb.error = e.message || String(e);
        if (!fb.path && path) { fb.input = path; }
      } finally { fb.loading = false; }
    },
    fbClose(path) {
      const fb = this.fb;
      this.fb = null;
      if (fb) fb.resolve(path || null);
    },
    restoreFromDetail() { if (this.detail) this.openRestore([{ source: this.detail.source, full_path: this.detail.title }]); },
    restoreVersion(v) {
      if (this.detail) this.openRestore([{ source: this.detail.source, full_path: this.detail.title, content_id: v.content_id || null, snapshot_id: v.snapshot_id }]);
    },
    async openRestoredPath(path) {
      await getJson("/api/restore/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) })
        .catch((e) => { this.error = e.message; });
    },

    // Search / Tree selections: files restore flat into one folder.
    selRestore(scope) {
      const items = this._selItems(scope).map((i) => ({ source: i.source || null, full_path: i.path }));
      if (!items.length) { this.klNotify("Select one or more rows first.", "warn"); return; }
      this.openRestore(items);
    },

    // ---- Download through the browser ----
    // item: {source, full_path, snapshot_id?, content_id?}; size/kind known from a plan when available.
    async downloadItem(item) {
      if (this.isDemo()) { this.klNotify("Downloading isn't available in the demo.", "warn"); return; }
      let info = null;
      try {
        const plan = await getJson("/api/restore/plan", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, items: [item], target_mode: "folder", target: "_", preview: false }) });
        info = plan.items[0];
      } catch (e) { this.error = e.message || String(e); return; }
      if (info.size > DOWNLOAD_WARN_BYTES) {
        const facts = [["Size", fmtBytes(info.size)], ["Files", Number(info.files).toLocaleString(NUM_LOCALE)], ["Snapshot", this.humanTime(info.snap_time)]];
        if (info.expired) facts.push(["Modified time", "Not restored: the snapshot has expired"]);
        const ok = await this.klConfirm({
          title: `Download ${info.name}?`, facts,
          callout: info.type === "d" ? "The .zip is built on the server first, which needs this much free space." : "",
          confirmLabel: "Download",
        });
        if (!ok) return;
      } else if (info.expired) {
        this.klNotify("Modified time not restored: the snapshot has expired.", "warn");
      }
      const q = new URLSearchParams({ repo: this.repo, source: item.source, full_path: item.full_path });
      if (item.snapshot_id) q.set("snapshot_id", item.snapshot_id);
      if (item.content_id) q.set("content_id", item.content_id);
      const a = document.createElement("a");
      a.href = `/api/restore/download?${q}`;
      // A new tab, so a JSON error response (expired version, kopia failure) cannot navigate the
      // whole app away from itself.
      a.target = "_blank";
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
      if (info.type === "d") {
        this.actionText = `Building ${info.name}.zip. The download starts when it is ready.`;
        this.klNotify(`Building ${info.name}.zip - the download starts when it is ready.`);
      }
    },
    downloadVersion(v) {
      if (this.detail) this.downloadItem({ source: this.detail.source, full_path: this.detail.title, content_id: v.content_id || null, snapshot_id: v.snapshot_id });
    },
};
