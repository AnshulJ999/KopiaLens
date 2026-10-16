# KopiaLens alongside KopiaUI

KopiaLens and KopiaUI can use the same kopia repositories on the same computer. This page explains how they share a repository safely, and what to avoid.

## What each one does

Both apps use kopia underneath and the same repositories, so your snapshots, policies and history are the same in both.

- **KopiaUI** is kopia's own app. It runs backups on schedule, restores files and edits policies.
- **KopiaLens** does all of that too, and adds analysis: what takes the space, what is safe to delete, what isn't backed up, and tools to clean up.

You can keep KopiaUI for backups and use KopiaLens to look and clean up. Or you can let KopiaLens run the backups and stop using KopiaUI.

## One app runs the backups

The one rule: only one app may run a repository's backups. If both run them, every backup is taken twice, and both may run maintenance at the same time.

KopiaLens checks for this. The **Backups:** button on the Dashboard names the app that runs them, and **Who runs backups…** lets you choose ([Who runs backups](backups.md#who-runs-backups)). If KopiaUI starts running a repository that KopiaLens runs, KopiaLens stops its own schedule there and tells you.

To move the backups to KopiaLens, see [Move backups from KopiaUI to KopiaLens](guide-move-from-kopiaui.md).

## Connection files

Kopia keeps each repository connection in a connection file. KopiaUI opens every connection file in kopia's own folder, and runs backups and maintenance for each one.

- A repository you add in KopiaLens is kept in **KopiaLens's folder** by default, so KopiaUI doesn't see it ([Where the connection file goes](repositories.md#where-the-connection-file-goes)).
- If you choose **kopia's folder, shared with KopiaUI**, KopiaUI lists the repository too and may run its backups.
- A connection KopiaLens found on this computer stays where it is, and KopiaUI keeps using it.

**Warning:** don't use **Disconnect** in KopiaUI for a repository KopiaLens also uses. KopiaUI deletes the connection file, and KopiaLens loses the repository too. To stop KopiaUI using a repository, move it out of KopiaUI from **Who runs backups…** instead.

## The repository password

Kopia remembers a repository's password for each connection file. Two apps that share a connection file share its saved password. A copy of the connection file in another place doesn't have it, so KopiaLens asks for the password once when it moves a connection.

## Analysis while KopiaUI runs the backups

While KopiaUI runs the backups, KopiaLens still reads and analyses everything. Its index updates on its own schedule, so new snapshots from KopiaUI appear in KopiaLens after the next index run. Anything you start by hand in KopiaLens, such as a delete or a restore, runs straight away.

## The same applies to BlinkDisk

BlinkDisk is another app built on kopia. KopiaLens can share a BlinkDisk vault in the same way ([Using KopiaLens with BlinkDisk](blinkdisk.md)).

## Related

- [Backups](backups.md)
- [Repositories](repositories.md)
- [Move backups from KopiaUI to KopiaLens](guide-move-from-kopiaui.md)
