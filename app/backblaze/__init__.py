"""Backblaze (BZ) backend for KopiaLens.

A read-only provider that indexes the local Backblaze Personal Backup state
(`C:\\ProgramData\\Backblaze\\bzdata`) into SQLite and exposes the same kinds of
views KopiaLens gives Kopia repos (search / tree / dashboard / excluded coverage)
plus BZ-only tabs (exclusions / queue / sessions / health).


HARD RULE: this package only ever READS bzdata. Never writes/edits/deletes there.
See safety.py for the write-guard.
"""
