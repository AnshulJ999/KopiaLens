# Changelog

Every KopiaLens release, newest first. The release pipeline copies a version's section into its GitHub release notes, so each heading must read `## [X.Y.Z] - YYYY-MM-DD`. Sections use `### Added`, `### Changed`, `### Fixed` (and `### Important` above them when something needs the reader's attention). One short line per change, in the user's words.

## [1.0.0] - unreleased

KopiaLens 1.0 is the first public release: a companion app for [Kopia](https://kopia.io) that shows the real size of everything in your repository, finds the files you never meant to keep, and removes them from every snapshot that holds them, with a dry run first.

### What it does

- See where the space goes: every backed-up folder with its real cost after deduplication.
- Find any file in any snapshot, with every version of it.
- Rank snapshots by how much deleting them would free.
- Catch the folders that rewrite themselves on every backup.
- Browse any snapshot like a disk; restore, download or mount what you need.
- Try a retention policy before you use it, and write exclusions that do what you meant.
- Run backups on a schedule, beside KopiaUI and the kopia command line or on its own (kopia is included).
- Remove files and folders from existing snapshots (Pro).

### Free and Pro

Everything KopiaLens shows you is free, with no time limit. Pro adds the actions that remove data from inside your backups: [kopialens.com/pricing](https://kopialens.com/pricing).

### Downloads

Windows (installer and portable zip), macOS (Apple silicon), Linux (AppImage, .deb, .rpm; x64 and arm64), Docker (amd64, arm64) and a Home Assistant add-on. Install steps: [kopialens.com/download](https://kopialens.com/download). Checksums are in `SHA256SUMS.txt`.

### Privacy

Nothing in your repository changes unless you start an action. No file names, paths or backup contents leave your computer. A daily update check and anonymous usage stats can each be turned off in **Settings > Updates** ([what is sent](https://kopialens.com/docs/usage-stats)).

KopiaLens is an independent app for Kopia, not affiliated with or endorsed by the Kopia project.
