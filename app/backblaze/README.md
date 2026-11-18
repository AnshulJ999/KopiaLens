# KopiaLens Backblaze engine

This folder is the part of [KopiaLens](https://kopialens.com) that reads Backblaze Personal Backup (Computer Backup). It's published so anyone can read exactly what KopiaLens does with Backblaze's files.

KopiaLens is not affiliated with Backblaze.

## What it does

- **Reads only.** It never writes, renames, or deletes anything in Backblaze's data folder. Every read goes through `safety.py`, which refuses any write mode and any write under Backblaze's folders.
- **Finds what Backblaze backs up.** It reads Backblaze's per-drive file lists and applies Backblaze's own exclusion rules (mandatory rules, your exclusions, file types, and folder choices), so the result matches Backblaze's own counts.
- **Reads Windows and Mac data**, on this computer or from another computer's shared `bzdata` folder.
- **Builds an index** of every file (Parquet files read with DuckDB), plus small history tables in SQLite: the upload queue, upload days, folder history, and Backblaze's own problem reports.

## Files

| File | What it holds |
|---|---|
| `paths.py` | Where Backblaze keeps its data, drives and volumes, Windows or Mac |
| `parsers.py` | Readers for Backblaze's file lists, XML files, upload records, and logs |
| `rules.py` | Backblaze's exclusion rules, applied the way Backblaze applies them |
| `indexer.py` | Builds and refreshes the index |
| `pq.py` | The Parquet store |
| `bbdb.py` | The SQLite tables |
| `queries.py` | The answers the KopiaLens screens show |
| `checks.py` | Problems read from Backblaze's own reports, for the Health tab |
| `safety.py` | The read-only guard |
| `worker.py`, `bz_verify.py` | The background worker, and a check against Backblaze's own counts |
| `tests/` | The tests |

`app/core/textmatch.py` (search matching) and `app/core/atomicfile.py` (safe file writes) are published with it.

## What's not here

The code imports a few parts of KopiaLens that aren't published yet: its settings, configuration, logging, encryption, path helpers, and scheduler. So this folder is for reading, not for running on its own.

## Licence

Copyright (C) 2025-2026 Anshul Jain.

GNU Affero General Public License v3.0 (`LICENSE`), for the files in this folder and the two files above. The rest of KopiaLens has its own licence.

## Contributing

Bug reports and questions are welcome in the [KopiaLens issues](https://github.com/AnshulJ999/KopiaLens/issues). Pull requests aren't open yet: they'll need a short contributor agreement, which isn't set up.
