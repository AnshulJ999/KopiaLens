"""Backblaze (BZ) backend for KopiaLens.

A read-only provider that indexes the local Backblaze Personal Backup state
(`C:\\ProgramData\\Backblaze\\bzdata`) and exposes the same kinds of
views KopiaLens gives Kopia repos (search / tree / dashboard / excluded coverage)
plus BZ-only tabs (exclusions / queue / sessions / health).

Storage (): the big `files` table lives in the store chosen by the
`backblaze.store` setting - "sqlite" (kopialens_bz.sqlite + FTS5, the original) or
"parquet" (per-drive Parquet generations under data/bz/, read via DuckDB; pq.py).
The small history/state tables (folder_snapshots, events_archive, backup_days, kv,
ingest, queue, events, churn, folder_rollup) always stay in the slim SQLite.


HARD RULE: this package only ever READS bzdata. Never writes/edits/deletes there.
See safety.py for the write-guard.
"""
