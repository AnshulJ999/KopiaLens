# Backups

KopiaLens can run your backups on their schedules, the way KopiaUI does, or leave that to the app you already use. Either way you can take a snapshot by hand, pause backups, and see every backup and maintenance run in the Tasks tab.

## Who runs backups

Only one app may run a repository's backups at a time. Two schedulers on one repository take every backup twice and can run maintenance at the same time.

The **Backups** button on the Dashboard shows who runs them now, for example "Backups: KopiaUI" or "Backups: KopiaLens · next 18:00". **Who runs backups…** shows what KopiaLens found on this computer and lets you choose:

- **Another app** (the default for a repository you already back up): KopiaUI, BlinkDisk, a kopia server or your own scheduled commands keep running backups. KopiaLens still analyses the repository and runs anything you start by hand.
- **KopiaLens**: KopiaLens runs a kopia server for this repository in the background. It takes backups on each policy's schedule, retries failed ones, and runs maintenance when this computer is the maintenance owner.

When KopiaUI uses the same repository, you can either keep sharing its connection (then KopiaUI must not run) or move the repository out of KopiaUI. Moving asks for the repository password once. Never use **Disconnect** in KopiaUI for this: it deletes the connection file, and KopiaLens loses the repository too. You can hand backups back the same way, from the same dialog.

If another app starts running a repository that KopiaLens runs, KopiaLens stops its own schedule there and tells you. When the other app stops, KopiaLens takes over again on its own. To wait for you instead, change **Settings → Advanced → When another app stops running backups**.

KopiaLens runs backups only while it's open. The desktop app starts at logon and stays in the tray. A Docker or server install is always on.

## Back up now

- **New snapshot…** on the Dashboard backs up any folder: pick it, check which policy applies (or give it its own), see a size estimate, then **Snapshot now**.
- **Snapshot now** on a Dashboard row backs up a folder that's already backed up. When KopiaLens runs the backups, the row shows live progress with a **Cancel** button.
- In the Snapshots tab, **Edit description…** changes a snapshot's description.

## Pausing

- **Pause backups** in the Backups button (or the tray) pauses every repository, or one, for 1 hour up to 1 week, or until you resume. **Custom…** picks an end time.
- Right-click a Dashboard row and choose **Pause backups** to pause one folder without changing its policy. The row then says "Paused" with a **Resume** button. Pausing stops a backup that's running. You can still back up a paused folder by hand. Its schedule stays paused.
- Pauses are saved, so they survive a restart. A folder paused for more than a week shows a warning.

Pausing works where KopiaLens runs the backups. Elsewhere the menu item is greyed out. Pause in the app that runs them, or set the policy's schedule to manual.

## Tasks

The Tasks tab lists every backup, maintenance run and KopiaLens job, running or finished. Click a row for its details: counters, start and finish times, and kopia's log, with **Stop** while it runs. History is kept for 90 days by default (**Settings → Advanced**). Anything mounted is listed at the top.

## Snapshot scripts

A policy can run your own commands before and after a backup, for example to stop a database or flush a cache. For safety, kopia runs them only on a computer whose connection allows it. KopiaUI can't turn this on. In the desktop app, open the policy's Advanced section and use **Turn on scripts**. KopiaLens keeps a copy of the connection file first. Restart KopiaUI afterwards if it uses the same repository.

## Several computers in one repository

When more than one computer backs up into a repository, the filter on the Dashboard and in the Snapshots tab shows all computers, this computer, or one other computer. KopiaLens only schedules backups for this computer's folders.

## Checking backups

**Verify backups…** in the Repository tab checks that every snapshot's folders and file lists can be read. Choose a percentage of files to also download and check their contents. That catches damaged data, but takes longer and costs download traffic on cloud storage.
