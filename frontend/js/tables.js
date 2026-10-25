// Table layout choices: the "Table layout…" popup, the app-wide alignment (Settings > Appearance),
// per-column times from the header button, and rebuilding tables after a reset. The column header
// menu and the saved `kl` choices live in util.js.
const TABLE_NAMES = {
  dashboardTable: "Dashboard", snapshotsTable: "Snapshots", treeTable: "Tree", searchTable: "Search",
  groupTable: "Search: folders", searchViewTable: "Search: old versions", churnTable_source: "Churn: sources", churnTable_folder: "Churn: folders",
  churnTable_ext: "Churn: file types", excludedTable: "Excluded", simulateTable: "Simulate", tasksTable: "Tasks",
  fullListTable: "Full list",
};
const tableName = (key) => TABLE_NAMES[String(key).replace(/^tabulator-/, "")] || String(key).replace(/^tabulator-/, "");

const tablesPart = {
  // "Table layout…" for the table with this selector (a tab's … menu) or prefs key (column menu).
  openTableLayout(target) {
    let key = target;
    if (String(target).startsWith("#")) {
      const el = document.querySelector(target);
      if (!el || !el._tabulator) { this.klNotify("Open the table first.", "warn"); return; }
      key = tableKey(el._tabulator);
    }
    const kl = tableKl(key);
    this.tblLayout = { key, name: tableName(key), align: kl.tableAlign || "", layout: kl.layout === "fit" ? "fit" : "fill" };
  },
  tblLayoutSet(patch) {
    const d = this.tblLayout;
    if (!d) return;
    Object.assign(d, patch);
    saveTableKl(d.key, { tableAlign: d.align || undefined, layout: d.layout === "fit" ? "fit" : undefined });
    const t = _tablesByPid[d.key];
    if (t && t.element && document.contains(t.element)) applyTableKl(t);
  },
  tblLayoutReset() {
    const d = this.tblLayout;
    this.tblLayout = null;
    if (d) resetTableLayout(d.key);
  },
  alignLabel(a) { return (ALIGNS.find((x) => x[0] === a) || ALIGNS[0])[1]; },
  // Settings > Appearance: the alignment every table starts from.
  setTableAlign(v) {
    this.tableAlign = ALIGNS.some((a) => a[0] === v) ? v : "auto";
    TABLE_ALIGN = this.tableAlign;
    this.saveKl("table_align");
    for (const t of Object.values(_tablesByPid)) if (t.element && document.contains(t.element)) applyTableKl(t);
  },
  // A reset (here or in another browser tab) drops the saved layout: build the open tab again.
  rebuildTables() {
    this._dashSig = "";
    this.activate();
  },
  // Right-click on the header's time button: the columns that keep their own choice.
  timeOverrideList() {
    const labels = { rel: "How long ago", abs: "Date", both: "Date and how long ago" };
    return timeOverrides().map((o) => ({ ...o, where: tableName(o.key) + ": " + o.title, label: labels[o.mode] || "Date" }));
  },
  resetTimeOverrides() { clearTimeOverrides(); this.timeMenu = false; },
  dropTimeOverride(o) { clearTimeOverride(o.key, o.field); },
  timeFmtChoices: [["auto", "Automatic"], ["relative", "How long ago"], ["absolute", "Dates"]],
  timeFmtLabel() {
    return { relative: "how long ago", absolute: "dates" }[this.timeFmt]
      || "automatic (how long ago for recent activity, dates for history)";
  },
};
