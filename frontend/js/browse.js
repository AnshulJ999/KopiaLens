// Snapshot folder browser: one folder at a time, from the index when it holds the folder and from
// kopia otherwise. Works before the first index and for repos reached through a kopia server.
const browsePart = {
    browse: null, _browseSeq: 0,

    openBrowse(row) {
      this.browse = { source: row.source || this.snapSource, snapshot_id: row.id, start_time: row.start_time,
                      path: "", crumbs: [], entries: [], loading: false, error: "" };
      return this.browseTo("");
    },
    async browseTo(path) {
      const b = this.browse;
      if (!b) return;
      const seq = ++this._browseSeq;
      b.loading = true; b.error = "";
      try {
        const d = await getJson(`/api/snapshots/browse?repo=${encodeURIComponent(this.repo)}&source=${encodeURIComponent(b.source)}`
          + `&snapshot_id=${encodeURIComponent(b.snapshot_id)}&path=${encodeURIComponent(path)}`);
        if (seq !== this._browseSeq || this.browse !== b) return;
        b.path = d.path; b.crumbs = d.crumbs || []; b.entries = d.entries || [];
      } catch (e) {
        if (this.browse === b) b.error = e.message || String(e);
      } finally {
        if (seq === this._browseSeq && this.browse === b) b.loading = false;
      }
    },
    browseOpen(e) { if (e.type === "d") this.browseTo(e.full_path); },
    browseRestore(e) {
      const b = this.browse;
      if (!b) return;
      const name = e ? e.name : ((b.crumbs[b.crumbs.length - 1] || {}).name || "folder");
      this.openRestore([{ source: b.source, full_path: e ? e.full_path : b.path, snapshot_id: b.snapshot_id }], { label: name });
    },
    browseFoot() {
      const list = (this.browse && this.browse.entries) || [];
      const dirs = list.filter((e) => e.type === "d").length;
      return `${countOf(dirs, "folder")} · ${countOf(list.length - dirs, "file")}`;
    },
    closeBrowse() { this.browse = null; this._browseSeq++; },
    // Indexed repos keep the richer Tree view; the browser covers the rest.
    browseSnapshot(d) {
      if (this.repoKind === "server" || this._neverIndexed()) return this.openBrowse(d);
      this.selectedSource = d.source; this.selectedSnapshot = d.id; this.active = "tree"; this.treeMode = "full";
      this.treePrefix = this.sourceRoot(); this.loadTree();
    },
};
