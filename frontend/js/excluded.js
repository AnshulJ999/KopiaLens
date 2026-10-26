// Excluded browser: disk coverage gaps vs both repos (the disk lookup's side: Everything or the scanner).
const excludedPart = {

    // ---- Excluded browser (disk coverage gaps vs both repos) ----
    async loadExcluded() {
      this._excludedSeen = true;
      if (!this.excludedDrives.length) {
        const d = await getJson("/api/excluded/drives").catch(() => null);
        if (d) {
          this.excludedDrives = d.drives || []; this.excludedOk = !!d.everything_ok;
          this.diskLookup = d.disk_lookup || null; this.excludedScans = (d.disk_lookup && d.disk_lookup.scans) || [];
        }
      }
      if (this.excludedPath) return this.listExcluded(this.excludedPath);
      this.renderExcludedDrives();
    },
    // Asked again while something is being counted; asking again only reads, it never starts a count.
    _excludedAgain(fn) {
      clearTimeout(this._excludedTimer);
      this._excludedTimer = setTimeout(fn, 3000);
    },
    // Leaving the tab stops the counts it started; a Count you asked for keeps going.
    excludedLeft() {
      clearTimeout(this._excludedTimer);
      if (this.diskLookup && this.diskLookup.provider === "scan") this._diskPost("/api/disk-lookup/left", {}).catch(() => {});
    },
    renderExcludedDrives() {
      this.excludedPath = "";
      this._excludedAs = null;
      this._excludedNotes = null;
      this._excludedNotIndexed = false;
      this.excludedMeta = this.excludedOk ? countOf(this.excludedDrives.length, "location") : "";
      this.renderExcluded(this.excludedDrives.map((dr) => ({
        name: dr.path.replace(/\\$/, ""), path: dr.path, is_dir: true, status: "drive",
        state: dr.state || "counted", counting: !!dr.counting, backed_up_as: dr.backed_up_as || null, used: dr.used || 0,
        total_size: dr.total_size || 0, disk_bytes: dr.total_size || 0,
        backed_bytes: dr.backed_bytes || 0, excluded_bytes: dr.excluded_bytes || 0,
        pct_excluded: dr.total_size ? Math.round((dr.excluded_bytes / dr.total_size) * 1000) / 10 : null,
        total_files: dr.total_files || 0, backed_files: dr.backed_files || 0, excluded_files: dr.excluded_files || 0,
        bz_backed_bytes: dr.bz_backed_bytes || 0, bz_backed_files: dr.bz_backed_files || 0,
        last_backed: "", repo: "", mtime: "",
      })));
      if (this.excludedDrives.some((dr) => dr.counting) || this.excludedScans.length) {
        this._excludedAgain(async () => {
          if (this.active !== "excluded" || this.excludedPath) return;
          const d = await getJson("/api/excluded/drives").catch(() => null);
          if (!d || this.excludedPath) return;
          this.excludedDrives = d.drives || [];
          this.excludedScans = (d.disk_lookup && d.disk_lookup.scans) || [];
          this.renderExcludedDrives();
        });
      }
    },
    excludedNav(path) { if (!path) return this.renderExcludedDrives(); this.listExcluded(path); },
    async listExcluded(path, quiet = false) {
      const seq = ++this._excludedSeq;
      this.excludedPath = path;
      const load = async () => {
        const r = await getJson(`/api/excluded/list?path=${encodeURIComponent(path)}`);
        if (seq !== this._excludedSeq) return;  // a newer navigation superseded this one
        this._excludedAs = r.backed_up_as || null;
        this._excludedNotes = r.notes || null;
        this._excludedNotIndexed = !!r.not_indexed;
        this._excludedCountedAt = r.counted_at || null;
        this.excludedScans = r.scans || [];
        const as = r.backed_up_as ? ` · backed up as ${r.backed_up_as.path}${r.backed_up_as.computer ? " on " + r.backed_up_as.computer : ""}` : "";
        const when = r.counted_at ? ` · counted ${fmtRel(r.counted_at)}` : "";
        const bad = r.notes && r.notes.unreadable && r.notes.unreadable.length ? ` · ${countOf(r.notes.unreadable.length, "folder")} couldn't be read` : "";
        this.excludedMeta = `${countOf(r.count, "item")} · ${fmtBytes(r.total_excluded)} not backed up${as}${when}${bad}`;
        this.renderExcluded(r.items || []);
        if (r.counting || this.excludedScans.length) {
          this._excludedAgain(() => {
            if (this.active === "excluded" && this.excludedPath === path) this.listExcluded(path, true);
          });
        }
      };
      if (quiet) return load().catch(() => {});
      await this.wrap(load, "Listing…");
    },
    excludedRowOpen(row) {
      if (row.status === "drive") return this.listExcluded(row.path);
      if (!row.is_dir) return;
      this.listExcluded(joinPath(this.excludedPath, row.name));
    },
    // The drive or added place a folder sits in (the longest match), so the path bar starts there.
    _excludedRoot(path) {
      const t = trimSep(path || "");
      let best = null;
      for (const d of this.excludedDrives) {
        const r = trimSep(d.path);
        const s = pathSep(r);
        const fold = s === "\\";
        const a = fold ? t.toLowerCase() : t;
        const b = fold ? r.toLowerCase() : r;
        const inside = a === b || a.startsWith(b.endsWith(s) ? b : b + s);
        if (inside && (!best || r.length > trimSep(best.path).length)) best = d;
      }
      return best;
    },
    excludedUp() {
      const p = trimSep(this.excludedPath || "");
      if (!p) return;
      const root = this._excludedRoot(p);
      if (root) {
        const r = trimSep(root.path);
        if (r === p) return this.renderExcludedDrives();
        const up = p.slice(0, p.lastIndexOf(pathSep(p)));
        return this.listExcluded(up.length <= r.length ? root.path : up);
      }
      if (/^[A-Za-z]:$/.test(p) || p === "/") return this.renderExcludedDrives();
      if (pathSep(p) === "/") return this.listExcluded(parentPath(p));
      const idx = p.lastIndexOf("\\");
      this.listExcluded(idx <= 2 ? p.slice(0, 2) + "\\" : p.slice(0, idx));
    },
    excludedCrumbs() {
      const out = [{ label: "Locations", path: "" }];
      if (!this.excludedPath) return out;
      const p = trimSep(this.excludedPath);
      const root = this._excludedRoot(p);
      if (root) {
        const r = trimSep(root.path);
        const s = pathSep(r);
        out.push({ label: r, path: root.path });
        let at = r === s ? "" : r;
        p.slice(r.length).split(s).filter(Boolean).forEach((seg) => { at += s + seg; out.push({ label: seg, path: at }); });
        return out;
      }
      if (pathSep(p) === "/") {
        out.push({ label: "/", path: "/" });
        let at = "";
        p.split("/").filter(Boolean).forEach((seg) => { at += "/" + seg; out.push({ label: seg, path: at }); });
        return out;
      }
      let acc = "";
      p.split("\\").forEach((seg, i) => {
        acc = i === 0 ? seg + "\\" : acc + (acc.endsWith("\\") ? "" : "\\") + seg;
        out.push({ label: seg, path: acc });
      });
      return out;
    },
    // ---- counting with the KopiaLens scanner ----
    _diskPost(url, body) {
      return getJson(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    },
    excludedScanning() { return !!(this.diskLookup && this.diskLookup.provider === "scan"); },
    excludedScanText(s) {
      const what = s.state === "queued" ? "Waiting to count" : "Counting";
      const done = s.state === "queued" ? "" : ` · ${countOf(s.files, "file")} · ${fmtBytes(s.bytes)} · ${fmtDur(s.seconds)}`;
      return `${what} ${s.path}${done}`;
    },
    async excludedCount(path) {
      const r = await this._diskPost("/api/disk-lookup/count", { path }).catch((e) => { this.klNotify(e.message, "warn"); return null; });
      if (!r) return;
      if (this.excludedPath) return this.listExcluded(this.excludedPath, true);
      this.excludedDrives = [];
      return this.loadExcluded();
    },
    async excludedStop(id) {
      await this._diskPost("/api/disk-lookup/stop", { id }).catch(() => null);
      this.excludedScans = this.excludedScans.filter((s) => s.id !== id);
      if (this.excludedPath) return this.listExcluded(this.excludedPath, true);
      this.excludedDrives = [];
      return this.loadExcluded();
    },
    // What to do when nothing can read the disk here.
    excludedHelpText() {
      const s = this.diskLookup;
      if (!s) return "";
      const ev = s.everything || {};
      if (s.mode === "off" || !ev.supported) return s.mode === "off" && ev.supported
        ? "Disk scanning is off. Turn it on to see what isn't backed up."
        : "Turn on the KopiaLens scanner to compare your disks with your backups.";
      if (s.mode === "everything" && ev.installed && !ev.es_found) return "Everything's command-line tool (es.exe) wasn't found. Set its path in Disk scanning settings.";
      if (s.mode === "everything" && ev.installed) return "Everything isn't running. Start it, or turn on the KopiaLens scanner.";
      if (s.mode === "everything") return "Install Everything to see what isn't backed up, or turn on the KopiaLens scanner.";
      return s.note || "Disk scanning isn't available.";
    },
    excludedNeedsEverything() {
      const s = this.diskLookup;
      return !!(s && s.everything && s.everything.supported && !s.everything.installed && s.mode !== "off");
    },
    async openDiskSettings() {
      await this.openSettings();
      this.settingsTab = "diskscan";
    },
    setExcludedFilter(f) { this.excludedFilter = f; this._drawExcluded(); },
    renderExcluded(items) { this._excludedItems = items; this._drawExcluded(); },
    _drawExcluded() {
      const f = this.excludedFilter;
      const items = (this._excludedItems || []).filter((it) => {
        if (f === "all" || it.status === "drive") return true;
        if (it.status === "empty") return false;   // nothing on disk to back up: shown under All only
        if (f === "backed") return it.status === "backed" || it.status === "partial";
        return it.status !== "backed";  // "Not backed up" view = excluded / partial / new / not counted yet
      });
      const pill = (s) => {
        const m = {
          backed: ["var(--ok-bg)", "var(--ok)", "backed up"], excluded: ["var(--danger-bg)", "var(--danger)", "NOT backed up"],
          partial: ["var(--warn-bg)", "var(--warn)", "partial"], new: ["var(--accent-bg)", "var(--accent)", "new (not yet)"],
          drive: ["var(--surface2)", "var(--muted)", "drive"], counting: ["var(--surface2)", "var(--muted)", "counting…"],
          not_counted: ["var(--surface2)", "var(--muted)", "not counted"], other_disk: ["var(--surface2)", "var(--muted)", "other disk"],
          empty: ["var(--surface2)", "var(--muted)", "empty"],
        };
        const [bg, fg, label] = m[s] || ["var(--surface2)", "var(--muted)", s];
        return `<span class="pill" style="background:${bg};color:${fg}">${label}</span>`;
      };
      const muted = (t, tip = "") => `<span style='color:var(--muted)'${tip ? ` title="${escHtml(tip)}"` : ""}>${t}</span>`;
      const bytes = (c) => c.getValue() ? fmtBytes(c.getValue()) : "";
      const total = (c) => {
        const d = c.getRow().getData();
        const state = d.status === "drive" ? d.state : d.status;
        if (state === "counting" || d.counting) return muted("Counting…");
        if (state === "not_counted") return muted("Not counted", d.status === "drive" && d.used ? `${fmtBytes(d.used)} used on this drive` : "");
        if (state === "other_disk") return muted("Other disk", "Another disk mounted here. Open it to count it.");
        if (state === "not_indexed") return muted("Not indexed", "Everything doesn't index this location");
        return bytes(c);
      };
      const num = (c) => c.getValue() ? Number(c.getValue()).toLocaleString(NUM_LOCALE) : "";
      const name = (c) => {
        const d = c.getRow().getData();
        const as = d.backed_up_as ? `<span style="color:var(--muted)"> · backed up as ${escHtml(d.backed_up_as.path)}${d.backed_up_as.computer ? " on " + escHtml(d.backed_up_as.computer) : ""}</span>` : "";
        return itemIconHtml(c.getValue(), d.is_dir) + escHtml(c.getValue()) + as;
      };
      const cols = [
        { title: "Name", field: "name", widthGrow: 4, minWidth: 220, formatter: name },
        { title: "Status", field: "status", width: 115, formatter: (c) => pill(c.getValue()) },
        { title: "Total", field: "total_size", width: 100, hozAlign: "right", sorter: "number", formatter: total },
        { title: "Backed Up", field: "backed_bytes", width: 105, hozAlign: "right", sorter: "number", formatter: bytes },
        { title: "Excluded", field: "excluded_bytes", width: 95, hozAlign: "right", sorter: "number", formatter: bytes },
        { title: "% Excl", field: "pct_excluded", width: 70, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() != null ? c.getValue() + "%" : "" },
        { title: "Files", field: "total_files", width: 80, hozAlign: "right", sorter: "number", formatter: num },
        { title: "Backed Files", field: "backed_files", width: 110, hozAlign: "right", sorter: "number", formatter: num },
        { title: "Excl Files", field: "excluded_files", width: 95, hozAlign: "right", sorter: "number", formatter: num },
        { title: "Last backed", field: "last_backed", width: 145, formatter: (c) => (c.getValue() ? timeCell(c.getValue(), c) : "") },
        { title: "Repo", field: "repo", width: 80 },
        { title: "Also in BZ", field: "bz_backed_bytes", width: 100, hozAlign: "right", sorter: "number", formatter: (c) => c.getValue() ? fmtBytes(c.getValue()) : "<span style='color:var(--faint)'>—</span>" },
        { title: "Modified", field: "mtime", width: 145, formatter: (c) => (c.getValue() ? timeCell(c.getValue(), c) : "") },
      ];
      const isDrives = this.excludedPath === "";
      const menu = [
        { label: "Open", action: (_e, row) => this.excludedRowOpen(row.getData()) },
        { label: "Details", action: (_e, row) => this.openExcludedDetail(row.getData()) },
      ];
      if (this.excludedScanning()) {
        menu.push({ label: "Count size", disabled: (row) => !row.getData().is_dir,
                    action: (_e, row) => this.excludedCount(this._excludedPath(row.getData())) });
      }
      menu.push(
        { label: "Back this up…", action: (_e, row) => this.excludedInclude(row.getData()) },
        { label: "Exclude…", action: (_e, row) => this.excludedExclude(row.getData()) },
        { label: "Policy…", action: (_e, row) => this.polPopFor("", this._excludedBackupPath(row.getData())) },
        { label: "Copy path", action: (_e, row) => this.copyText(this._excludedPath(row.getData())) },
      );
      setTable("#excludedTable", items, cols, {
        persistSort: !isDrives,  // drives view = always alphabetical; folders keep your persisted sort
        initialSort: isDrives ? [{ column: "name", dir: "asc" }] : undefined,
        onRowDblClick: (_e, row) => this.excludedRowOpen(row.getData()),
        rowMenu: menu,
      });
    },
    _excludedPath(row) {
      if (row.status === "drive") return row.path;
      return joinPath(this.excludedPath, row.name);
    },
    // The same item as the backups name it: an added place can be backed up under another path.
    _excludedBackupPath(row) {
      if (row.status === "drive") return row.backed_up_as ? row.backed_up_as.path : row.path;
      return this._excludedAs ? joinPath(this._excludedAs.path, row.name) : this._excludedPath(row);
    },
    async excludedInclude(row) {
      const path = this._excludedPath(row);
      const r = await getJson(`/api/excluded/include-cmds?path=${encodeURIComponent(path)}`).catch((e) => { this.error = e.message; return null; });
      if (!r) return;
      const txt = (r.commands || []).map((c) => `# ${c.name}\n${c.command}`).join("\n\n");
      this.copyText(txt);
      this.actionText = "Include commands (copied — run the one for the repo you want):\n" + txt;
      this.klNotify("Include commands copied - run the one for the backup you want.");
    },
    excludedExclude(row) {
      // The real Exclude dialog, not clipboard text: it resolves the backups holding the path and
      // writes the rule through the same review as everywhere else.
      if (row.status === "drive") { this.klNotify("Open a folder or file first - a whole drive is not a rule target.", "warn"); return; }
      const path = this._excludedBackupPath(row);
      this.showExcludedDetail = false;
      this.openExclude({ kind: row.is_dir ? "folder" : "file", path });
    },
    openExcludedDetail(row) {
      this.excludedDetail = { ...row, full_path: this._excludedPath(row) };
      this.showExcludedDetail = true;
    },
};
