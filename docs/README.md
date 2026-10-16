# KopiaLens documentation

KopiaLens is an app for exploring, analysing and managing [Kopia](https://kopia.io) backup repositories. It runs in its own window or in your browser. It can run your backups the way KopiaUI does, or work alongside KopiaUI and the kopia command line. It never changes how kopia stores your data.

## Getting started

- [Installing KopiaLens](install.md): Windows installer or portable, the macOS app, Linux AppImage, deb and rpm.
- [First-time setup](getting-started.md): connecting your repositories, protecting KopiaLens, and building the first index.
- [Repositories](repositories.md): adding, renaming, reordering, checking and removing repositories.
- [Running in Docker](docker.md): the container, its folders, running as your own user, and mounting snapshots.
- [Backups](backups.md): who runs a repository's backups, snapshots by hand, pausing, Tasks, snapshot scripts and checking backups.
- [Notifications](notifications.md): alerts in the app and tray, KopiaLens's webhook (works with ntfy), and kopia's email, Pushover and webhook profiles.
- [Using KopiaLens with BlinkDisk](blinkdisk.md): connecting a vault, who runs backups and maintenance, the computer name, and merging split histories.

## Understanding KopiaLens

- [How KopiaLens counts space](space.md): logical, physical and reclaimable sizes, deduplication, and why deleting doesn't always free space.
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

- [Getting help](getting-help.md): reporting a problem, the diagnostics bundle, checking for updates, and the desktop app's zoom.
- [Update checks and usage stats](usage-stats.md): what the daily check sends, what's never sent, and how to turn it off.

## Pro

- [Free and Pro](licensing.md): what Pro adds, adding your licence, renewals, and licences in Docker.

## Screens

- [Dashboard](dashboard.md): page to come.
- [Snapshots](snapshots.md): page to come.
- [Simulate](simulate.md): page to come.
- [Tree](tree.md): page to come.
- [Search](search.md): page to come.
- [Churn](churn.md): page to come.
- [Repository](repository.md): page to come.
- [Excluded](excluded.md): page to come.
- [Policies](policies.md): page to come.
- [Tasks](tasks.md): page to come.
- [Logs](logs.md): page to come.
- [Backblaze](backblaze.md): page to come.
- [Indexing](indexing.md): stopping an index and index backups (more to come).
- [Settings](settings.md): the logo and tray icon (more to come).

More pages are added as features are documented.
