# KopiaLens documentation

KopiaLens is an app for exploring, analysing and managing [Kopia](https://kopia.io) backup repositories. It runs in its own window or in your browser. It can run your backups the way KopiaUI does, or work alongside KopiaUI and the kopia command line. It never changes how kopia stores your data.

## Getting started

- [Installing KopiaLens](install.md): Windows installer or portable, the macOS app, Linux AppImage, deb and rpm.
- [First-time setup](getting-started.md): connecting your repositories, protecting KopiaLens, and building the first index.
- [Repositories](repositories.md): adding, renaming, reordering, checking and removing repositories.
- [Running in Docker](docker.md): the container, its folders, running as your own user, and mounting snapshots.
- [Home Assistant](home-assistant.md): the add-on, opening it from the sidebar or its own port, and its folders.
- [Backups](backups.md): who runs a repository's backups, snapshots by hand, pausing, Tasks, snapshot scripts and checking backups.
- [Notifications](notifications.md): alerts in the app and tray, KopiaLens's webhook (works with ntfy), and kopia's email, Pushover and webhook profiles.

## Guides

- [Start backing up from scratch](guide-start-from-scratch.md): new to kopia? Create a repository, set a schedule and back up your first folder.
- [Find what's using the most space](guide-find-space.md): from the whole repository down to single files.
- [Free up space safely](guide-free-space.md): from the safest steps to the strongest.
- [Exclude a folder and remove it from old snapshots](guide-exclude-and-delete.md): stop backing it up and take it out of the past.
- [Restore files](guide-restore-files.md): find a file in your backups and get it back.
- [Check your backups](guide-check-backups.md): are they running, complete and readable?
- [Move backups from KopiaUI to KopiaLens](guide-move-from-kopiaui.md): hand over schedules and maintenance without losing history.
- [Clean up after a huge accidental backup](guide-accidental-backup.md): remove something big from every snapshot.
- [Find files that change every day](guide-find-churn.md): the files that cost far more than their size.
- [Test a retention policy before using it](guide-test-retention.md): see what it keeps and frees first.
- [Use KopiaLens with an AI agent](guide-ai-agent.md): safe access for agents, with your approval for deletes.
- [Run KopiaLens behind a reverse proxy](guide-reverse-proxy.md): your own address, with HTTPS from the proxy.

## Understanding KopiaLens

- [How KopiaLens counts space](space.md): logical, physical and reclaimable sizes, deduplication, and why deleting doesn't always free space.
- [Why deleting frees space only after maintenance](deleting-and-space.md): what kopia does with deleted data, and when the space comes back.
- [How indexing works](how-indexing-works.md): what the index holds, how it's built, and what it costs your computer.
- [KopiaLens alongside KopiaUI](kopiaui.md): sharing repositories with KopiaUI safely.
- [Using KopiaLens with BlinkDisk](blinkdisk.md): connecting a vault, who runs backups and maintenance, the computer name, and merging split histories.
- [Hardware and memory](hardware.md): memory, disk space and the setups KopiaLens is measured on.
- [Glossary](glossary.md): every term KopiaLens uses, in one place.

## Security

- [Sign-in and your account](sign-in.md): the KopiaLens password, staying signed in, signing out other devices, and resetting a forgotten password.
- [Encryption](encryption.md): encrypting what KopiaLens stores on disk, the recovery key, and unlocking after a restart.
- [Remote access and HTTPS](remote-access.md): who can open KopiaLens, HTTPS certificates, and running it behind a reverse proxy.
- [API tokens](api-tokens.md): access for scripts, the command line and AI agents, what a token can and can't do, and revoking one.

## For scripts and agents

- [Command line](cli.md): `klens` for scripts and AI agents, signing in once, removing files safely, and approvals.
- [API](api.md): calling KopiaLens over HTTP, the stable routes, refusals, and approvals.

## Help and privacy

- [FAQ and troubleshooting](faq.md): common questions, and what to do when something doesn't work.
- [Getting help](getting-help.md): reporting a problem, the diagnostics bundle, checking for updates, and the desktop app's zoom.
- [Update checks and usage stats](usage-stats.md): what the daily check sends, what's never sent, and how to turn it off.

## Pro

- [Free and Pro](licensing.md): what Pro adds, adding your licence, renewals, and licences in Docker.

## Screens

- [Dashboard](dashboard.md): every source, what it holds and what it costs.
- [Snapshots](snapshots.md): pin, compare, restore and delete snapshots.
- [Simulate](simulate.md): what a retention policy would keep and free.
- [Tree](tree.md): folders and files by size, in any snapshot.
- [Search](search.md): find files across every snapshot.
- [Churn](churn.md): what keeps adding new data.
- [Repository](repository.md): health, storage, maintenance and settings.
- [Excluded](excluded.md): what on your disks no backup covers.
- [Policies](policies.md): every policy, and how they apply.
- [Tasks](tasks.md): backups, maintenance and jobs, running and finished.
- [Logs](logs.md): what KopiaLens did, and its own log.
- [Backblaze](backblaze.md): what Backblaze Personal Backup backs up.
- [Indexing](indexing.md): when the index updates, the indexing panel, and index backups.
- [Disk scanning](disk-scanning.md): how KopiaLens reads your disks, and checking network shares and other computers.
- [Settings](settings.md): every section and its defaults.
- [Tables](tables.md): columns, alignment, widths, times and rows per page in any table.

## Panels

- [File panel](file-panel.md): one file's versions, space and copies.
- [Folder panel](folder-panel.md): one folder's contents, space and sources.
- [Delete from backups](delete-files.md): remove files and folders from your snapshots.
- [Deleting snapshots and sources](delete-snapshots.md): delete whole snapshots or every snapshot of a folder.
- [Prune versions](prune.md): keep the versions you want and remove the rest.
- [Exclude](exclude.md): keep a file or folder out of future snapshots.
- [Restore, download and mount](restore.md): get files back from a snapshot.
- [Policy editor](policy-editor.md): every policy setting.
- [Maintenance](maintenance.md): run kopia's maintenance by hand.
- [New snapshot](new-snapshot.md): back up any folder now.
- [Job panel](job-panel.md): a running action's progress and result.

## Thanks to Kopia

Thank you to the Kopia team and contributors. KopiaLens wouldn't exist without Kopia. It's one of the best backup engines out there: fast, secure and dependable, and I'm grateful to be able to build on it. KopiaLens is an independent project, not affiliated with or endorsed by the Kopia project.
