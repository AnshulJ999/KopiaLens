"""Mini-generation tree tables: the ``dir`` + ``anc`` rows folder scopes resolve through.

A published generation always carries ``dir`` (one row per folder) and ``anc`` (its ancestor
closure) beside ``pathx``, and folder-scoped queries read them instead of scanning
``lower(full_path) LIKE 'prefix%'``. A hand-built ``pathx`` fixture without them cannot run a
folder query at all - so fixtures that exercise one call this helper.
"""

from __future__ import annotations

SEP = chr(92)


def add_tree_tables(con, sources: list[str], paths: list[tuple[int, str]]) -> None:
    """Build ``dir``/``anc`` for a fixture, add ``src`` when absent, and point pathx at its folder.

    ``sources`` are the source keys in src_id order (1-based); an existing non-empty ``src`` table
    is left alone. ``paths`` are (src_id, full_path) pairs for every row the fixture inserted.
    Every folder on each path's way down gets a dir row (parent linked) plus closure rows for
    itself and each ancestor - the shape ``artifacts.build_repo`` publishes."""
    try:
        have = con.execute("SELECT src_id FROM src").fetchall()
    except Exception:  # noqa: BLE001 - no src table yet: this helper owns it
        have = None
    if have is None:
        con.execute("CREATE TABLE src(src_id INTEGER, source VARCHAR)")
        for i, key in enumerate(sources, 1):
            con.execute("INSERT INTO src VALUES (?, ?)", [i, key])

    con.execute("CREATE TABLE dir(dir_id INTEGER, src_id INTEGER, dir_path VARCHAR, "
                "depth SMALLINT, name VARCHAR, parent_id INTEGER)")
    con.execute("CREATE TABLE anc(dir_id INTEGER, depth SMALLINT, anc_id INTEGER)")

    def _split(folder: str) -> tuple[str, list[str]]:
        sep = SEP if SEP in folder else "/"
        return sep, folder.rstrip(SEP + "/").split(sep)

    folders: dict[tuple[int, str], None] = {}
    for src_id, full_path in paths:
        sep, parts = _split(full_path)
        for i in range(1, len(parts)):
            folder = sep.join(parts[:i])
            if parts[i - 1]:                      # skip the empty prefixes a UNC split produces
                folders.setdefault((src_id, folder), None)

    by_key: dict[tuple[int, str], tuple[int, int]] = {}     # (src_id, folder) -> (dir_id, depth)
    next_id = 1
    for src_id, folder in sorted(folders, key=lambda k: (k[0], len(k[1]))):
        sep, parts = _split(folder)
        depth = len(parts)
        parent = by_key.get((src_id, sep.join(parts[:-1])), (None, 0))[0] if depth > 1 else None
        by_key[(src_id, folder)] = (next_id, depth)
        con.execute("INSERT INTO dir VALUES (?, ?, ?, ?, ?, ?)",
                    [next_id, src_id, folder, depth, parts[-1], parent])
        next_id += 1
    for (src_id, folder), (dir_id, _depth) in by_key.items():
        sep, parts = _split(folder)
        for cut in range(1, len(parts) + 1):
            key = (src_id, sep.join(parts[:cut]))
            if key not in by_key:
                continue                     # the empty prefixes a UNC split produces
            anc_id, anc_depth = by_key[key]
            con.execute("INSERT INTO anc VALUES (?, ?, ?)", [dir_id, anc_depth, anc_id])

    cols = {r[0] for r in con.execute("SELECT name FROM pragma_table_info('pathx')").fetchall()}
    for col in ("dir_id", "name", "src_id"):
        if col not in cols:
            con.execute(f"ALTER TABLE pathx ADD COLUMN {col} "
                        + ("VARCHAR" if col == "name" else "INTEGER"))
    for src_id, full_path in dict.fromkeys(paths):
        sep, parts = _split(full_path)
        parent_id = by_key.get((src_id, sep.join(parts[:-1])), (None, 0))[0]
        if "src_id" in cols:   # two sources can back up the same path: match the row, not the path
            con.execute("UPDATE pathx SET dir_id = ?, name = ? WHERE full_path = ? AND src_id = ?",
                        [parent_id, parts[-1], full_path, src_id])
        else:
            con.execute("UPDATE pathx SET dir_id = ?, name = ?, src_id = ? WHERE full_path = ?",
                        [parent_id, parts[-1], src_id, full_path])
