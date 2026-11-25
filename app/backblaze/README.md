# KopiaLens Backblaze engine

This folder is the part of [KopiaLens](https://kopialens.com) that reads Backblaze Personal Backup (Computer Backup).

KopiaLens is not affiliated with Backblaze.

## What it does

- **Reads only.** It never writes, renames, or deletes anything in Backblaze's data folder. Every read goes through `safety.py`, which refuses any write mode and any write under Backblaze's folders.
- **Finds what Backblaze backs up.** Backblaze's file lists include files it skips, so the engine applies Backblaze's own exclusion rules (mandatory rules, your exclusions, file types, and folder choices) to tell them apart. Its counts match Backblaze's own on every drive: checked on 7 drives and about 1.1 million files.
- **Reads Windows and Mac data**, on this computer or from another computer's shared `bzdata` folder.
- **Builds an index** of every file (Parquet files read with DuckDB), plus small history tables in SQLite: the upload queue, upload days, folder history, and Backblaze's own problem reports.
- **Checks Backblaze's health** from Backblaze's own records: a drive that suddenly lists no backed-up files, an interrupted drive scan, a mass deletion, files Backblaze lost track of and queued again, and an outdated Backblaze version.

## What it reads

All inside Backblaze's data folder (`C:\ProgramData\Backblaze\bzdata` on Windows, `/Library/Backblaze.bzpkg/bzdata` on a Mac):

| What | Why |
|---|---|
| `bzfilelists/` | Every file on each drive that Backblaze scanned |
| `bzvolumes.xml`, `bzinfo.xml` | Drives, this computer's name, your exclusions and folder choices |
| `bzexcluderules_*.xml` | Backblaze's exclusion rules |
| `bzbackup/bzdatacenter/bz_done_*.dat`, `bz_todo_*.dat` | What was uploaded, and what's waiting |
| `bzbackup/bzfileids.dat` | File history, for the folders that change most |
| `bzreports/` | Backblaze's own counts and problem reports |
| `bzlogs/` | Backup sessions, the installed version, and self-heal results |
| `bzupdates/clientversion.xml` | The newest Backblaze version |

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

It also uses `app/core/textmatch.py` (search matching) and `app/core/atomicfile.py` (safe file writes), and runs as part of KopiaLens rather than on its own.

## Licence

Copyright (C) 2026 Anshul Jain.

GNU Affero General Public License v3.0 (`LICENSE`)

## Contributing

Bug reports and questions are welcome in the [KopiaLens issues](https://github.com/AnshulJ999/KopiaLens/issues). For code changes, see `CONTRIBUTING.md` at the root of the repository.
