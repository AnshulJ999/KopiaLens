"""Backblaze (BZ) backend for KopiaLens.

A read-only provider that indexes Backblaze Backup state: this computer's own data folder, or
a shared folder from another computer, on Windows or Mac. It exposes the same kinds of
views KopiaLens gives Kopia repos (search / tree / dashboard / excluded coverage)
plus BZ-only tabs (exclusions / queue / sessions / health).

Storage: the big `files` table lives in the store chosen by the
`backblaze.store` setting - "parquet" (per-drive Parquet generations under data/bz/,
read via DuckDB; pq.py - the DEFAULT) or "sqlite" (kopialens_bz.sqlite + FTS5, the
legacy path kept for rollback). The small history/state tables (folder_snapshots,
events_archive, backup_days, kv, ingest, queue, events, churn, folder_rollup) always
stay in the slim SQLite.

HARD RULE: this package only ever READS bzdata. Never writes/edits/deletes there.
See safety.py for the write-guard.
"""
