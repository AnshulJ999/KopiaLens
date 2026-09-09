// Actions tab + shared ops: mounts registry, restore dialog, mount-and-open helpers.
const actionsPart = {

    async mountLatestFor(item) {
      const v = await getJson(`/api/search/versions?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`).catch(() => null);
      const snap = v && v.versions && v.versions[0] && v.versions[0].snapshot_id;
      if (snap) this.mountRow(snap); else window.alert("No snapshot found for this file.");
    },

    // ---- Mount + open in Explorer (auto drive letter, registered in the mount manager) ----
    async mountFileOpen(item) {  // mount the latest snapshot carrying a file and open its folder
      const v = await getJson(`/api/search/versions?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(item.source)}&full_path=${encodeURIComponent(item.full_path)}`).catch(() => null);
      const snap = v && v.versions && v.versions[0] && v.versions[0].snapshot_id;
      if (!snap) { window.alert("No snapshot found for this file."); return; }
      this.mountOpen(snap, item.source, item.full_path);
    },
    mountOpen(snapshotId, source, fullPath) {
      this.wrap(async () => {
        const r = await getJson("/api/actions/mount-open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: snapshotId, source, full_path: fullPath || null }) });
        this.actionText = `Mounted at ${r.mountpoint} (pid ${r.pid}); opening Explorer…\n${(r.command || []).join(" ")}`;
        this.loadMounts();
      }, "Mounting + opening…");
    },
    mountVersionOpen(v) { if (this.detail) this.mountOpen(v.snapshot_id, this.detail.source, this.detail.title); },
    mountTreeSnapshot() {  // tree control-bar: mount the selected snapshot + open the current folder
      if (!this.selectedSnapshot) { window.alert("Pick a snapshot first."); return; }
      this.mountOpen(this.selectedSnapshot, this.selectedSource, this.treePrefix || this.sourceRoot());
    },

    // ---- Restore (accessible everywhere) ----
    openRestore(sourceId, name) {
      this.restore = {
        source_id: sourceId, name: name || sourceId, target: "", command: "",
        options: {
          restore_owners: true, restore_permissions: true, restore_times: true, skip_existing: true,
          overwrite_files: false, overwrite_directories: false, overwrite_symlinks: false,
          ignore_errors: false, write_files_atomically: false, write_sparse_files: false,
          ignore_permission_errors: false, delete_extra: false, parallel: 8, shallow: "",
        },
      };
      this.showRestore = true;
    },
    async restorePreview() {
      if (!this.restore || !this.restore.target) { window.alert("Enter a destination (folder, or a .zip/.tar path)."); return; }
      const r = await getJson("/api/actions/restore", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source_id: this.restore.source_id, target: this.restore.target, options: this.restore.options, confirm: false }) }).catch((e) => { this.error = e.message; return null; });
      if (r) this.restore.command = r.command;
    },
    async restoreExecute() {
      if (!this.restore || !this.restore.target) { window.alert("Enter a destination."); return; }
      const o = this.restore.options;
      if (o.overwrite_files || o.overwrite_directories || o.delete_extra) {
        const flags = [o.overwrite_files && "overwrite files", o.overwrite_directories && "overwrite directories", o.delete_extra && "delete extra files"].filter(Boolean).join(", ");
        const ok = await this.klConfirm({
          title: "Restore over existing files?",
          facts: [
            ["What", `Restore to ${this.restore.target}`],
            ["What it can do", flags],
            ["How", "kopia restore (writes to the destination)"],
          ],
          callout: "This restore can overwrite or remove files at the destination.",
          command: this.restore.command, confirmLabel: "Restore", danger: true,
        });
        if (!ok) return;
      }
      await this.wrap(async () => {
        const started = await this.startJob("restore", { source_id: this.restore.source_id, target: this.restore.target, options: o });
        if (started) {
          this.actionText = "Restore running - progress and live output in the job panel.";
          this.showRestore = false;
        }
      }, "Restoring (large restores can take a while)…");
    },
    async browseFolder() {
      const r = await getJson("/api/actions/browse-folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ initial: this.restore && this.restore.target || null }) }).catch(() => null);
      if (r && r.path) this.restore.target = r.path;
    },
    restoreFromDetail() { if (this.detail) this.openRestore(this.detail.content_id || this.detail.title, this.detail.title); },

    // ---------- Mounts / actions ----------
    async loadMounts() {
      const r = await getJson(`/api/actions/mounts`).catch(() => ({ mounts: [] }));
      this.mounts = r.mounts || [];
    },
    mountRow(snapId) {
      const mp = window.prompt("Mount snapshot (read-only) to drive/folder:", "Z:");
      if (!mp) return;
      this.wrap(async () => {
        const r = await getJson("/api/actions/mount", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: snapId, mountpoint: mp }) });
        this.actionText = `Mount started (pid ${r.pid}):\n${(r.command || []).join(" ")}`;
        this.loadMounts();
      }, "Starting mount...");
    },
    async mount() {
      await this.wrap(async () => {
        const r = await getJson("/api/actions/mount", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, snapshot_id: this.mountSnapshot, mountpoint: this.mountPoint }) });
        this.actionText = `Mount started (pid ${r.pid}):\n${(r.command || []).join(" ")}`;
        this.loadMounts();
      }, "Starting mount...");
    },
    async unmount(pid) {
      await this.wrap(async () => {
        await getJson("/api/actions/unmount", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pid }) });
        this.loadMounts();
      }, "Unmounting...");
    },
    async restorePreviewLegacy() {  // Actions-tab command preview (the restore dialog has its own restorePreview above)
      await this.wrap(async () => {
        const r = await getJson("/api/actions/restore-preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo: this.repo, source: this.restoreSource, destination: this.restoreDest }) });
        this.actionText = `Restore command (preview - run it yourself):\n${(r.command || []).join(" ")}`;
      });
    },
};
