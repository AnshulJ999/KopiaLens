// Actions tab + shared ops: mounts, the restore panel, downloads.
const RESTORE_PRESETS = {
  safe: { skip_existing: true, overwrite_files: false, overwrite_directories: false, overwrite_symlinks: false, delete_extra: false },
  replace: { skip_existing: false, overwrite_files: true, overwrite_directories: true, overwrite_symlinks: true, delete_extra: false },
  exact: { skip_existing: false, overwrite_files: true, overwrite_directories: true, overwrite_symlinks: true, delete_extra: true },
};
const DOWNLOAD_WARN_BYTES = 5 * 1024 ** 3;

const actionsPart = {

    // ---- Mount + open in Explorer (auto drive letter) ----
    mountFileOpen(item) {  // the newest snapshot holding the path
      this.mountOpen(null, item.source, item.full_path);
    },
    mountOpen(snapshotId, source, fullPath) {
      this.wrap(async () => {
        const r = await getJson("/api/actions/mount-open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: snapshotId || null, source, full_path: fullPath || null }) });
        this.actionText = `Mounted at ${r.mountpoint}. Opening Explorer.`;
        this.klNotify(`Mounted at ${r.mountpoint}. Opening Explorer.`);
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
        facts: this.mounts.map((m) => [m.mountpoint, this.mountLabel(m) + (m.snap_time ? " · " + this.humanTime(m.snap_time) : "")]),
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
    openRestore(items, opts = {}) {
      const list = (Array.isArray(items) ? items : [items]).filter((i) => i && i.source && i.full_path);
      if (!list.length) { this.error = "Nothing to restore."; return; }
      this.restore = {
        items: list, mode: "folder", target: "", preset: "safe", plan: null, loading: false, error: "",
        label: opts.label || (list.length === 1 ? list[0].full_path.split(/[\\/]/).pop() : `${list.length} items`),
        options: {
          restore_owners: true, restore_permissions: true, restore_times: true, ignore_errors: false,
          write_files_atomically: false, write_sparse_files: false, ignore_permission_errors: true,
          parallel: 8, shallow: "", ...RESTORE_PRESETS.safe,
        },
      };
      this.showRestore = true;
      this.restoreLoad(false);
    },
    closeRestore() { this.showRestore = false; this.restore = null; },

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
      r.preset = this.restorePresetOf(r.options);
      if (r.plan && r.plan.previewed) this.restoreLoad(false);
    },
    restoreHasDirs() {
      const p = this.restore && this.restore.plan;
      return !!(p && p.items.some((i) => i.type === "d"));
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
        facts.push(["Restore", `${p.items.length} items`]);
        facts.push(["To", r.mode === "folder" ? r.target : r.mode === "original" ? "Where each came from" : "Next to each original"]);
      }
      facts.push(["Files", `${Number(t.files).toLocaleString()} · ${fmtBytes(t.size)}`]);
      facts.push(["Existing files", { safe: "Skipped", replace: "Replaced when different", exact: "Replaced when different", custom: "Custom options" }[r.preset]]);
      if (p.previewed) {
        facts.push(["On disk now", `${t.new.toLocaleString()} new · ${t.identical.toLocaleString()} identical · ${t.different.toLocaleString()} different`]);
        if (r.options.delete_extra) facts.push(["Deleted", `${t.extra.toLocaleString()} files not in the snapshot`]);
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
      if (r.options.delete_extra && t.extra) callout = `${t.extra.toLocaleString()} files at the destination are not in the snapshot and will be deleted.`;
      else if (r.options.overwrite_files && t.different) callout = `${t.different.toLocaleString()} existing files will be replaced.`;
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
      const r = await getJson("/api/actions/browse-folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ initial: this.restore && this.restore.target || null }) }).catch(() => null);
      if (r && r.path && this.restore) { this.restore.target = r.path; this.restoreLoad(false); }
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
      const items = this._selItems(scope).filter((i) => i.source).map((i) => ({ source: i.source, full_path: i.path }));
      if (!items.length) { this.error = "Select one or more rows first."; return; }
      this.openRestore(items);
    },

    // ---- Download through the browser ----
    // item: {source, full_path, snapshot_id?, content_id?}; size/kind known from a plan when available.
    async downloadItem(item) {
      let info = null;
      try {
        const plan = await getJson("/api/restore/plan", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repo: this.repo, items: [item], target_mode: "folder", target: "_", preview: false }) });
        info = plan.items[0];
      } catch (e) { this.error = e.message || String(e); return; }
      if (info.size > DOWNLOAD_WARN_BYTES) {
        const ok = await this.klConfirm({
          title: `Download ${info.name}?`,
          facts: [["Size", fmtBytes(info.size)], ["Files", Number(info.files).toLocaleString()], ["Snapshot", this.humanTime(info.snap_time)]],
          callout: info.type === "d" ? "The .zip is built on the server first, which needs this much free space." : "",
          confirmLabel: "Download",
        });
        if (!ok) return;
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
