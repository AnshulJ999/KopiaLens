# Repositories

Open the repository menu at the top left to switch repositories, add one, or manage them. **Manage repositories…** opens Settings → Repositories.

## Adding a repository

Select **Add repository…**. It works the same way as [first-time setup](getting-started.md#1-connect-your-repositories): pick connections found on this computer, connect to an existing repository, or create a new one.

A new repository is indexed at the next scheduled time, or when you select **Index now**.

## Managing repositories

Each repository in Settings → Repositories shows where it's stored and when it was last indexed.

- **Order**: the arrows set the order of the repository menu.
- **Check connection**: asks kopia whether the repository can be reached. If kopia no longer remembers the repository password, enter it here.
- **Index now**: updates the index for that repository.
- **Edit…**: rename the repository or change its short name.
- **Counts as a backup**: when on, the Excluded view treats files in this repository as backed up. Turn it off for a test or scratch repository.
- **Remove…**: see below.

## Removing a repository

Removing a repository deletes what KopiaLens stores for it: its index and history. **The kopia repository and your backups are not touched**, and you can add the repository again later.

If KopiaLens created the connection file, you can delete it too. Keep it if KopiaUI or the kopia command line use that connection.

## Browsing a snapshot

In the Snapshots view, double-click a snapshot (or select **Browse files** from its menu) to look inside it. An indexed repository opens it in the Tree view. A repository that isn't indexed yet, or one reached through a kopia server, opens a folder browser that reads one folder at a time from kopia. Select **Restore…** on any file or folder to restore it.

## Repositories reached through a kopia server

A repository you connect to through a kopia server (KopiaUI or `kopia server start` on another computer) is browse-only:

- **Works**: the snapshot list, browsing and restoring files, pins, comparing two snapshots, mounting a snapshot, and the policies your server account may change.
- **Not available**: the index, and everything built on it (dashboard, space analysis, dedup, search, churn and cleanup). kopia doesn't allow those reads through a server.
- **You see the snapshots your server account may see.** By default that's the snapshots made as that account (`user@host`). If yours aren't listed, open **More options** when connecting and enter the user name and host the snapshots were made with.

For full analysis, install KopiaLens on the computer that runs the kopia server, or connect to the storage directly.

## The kopia program

The bottom of Settings → Repositories shows which kopia program KopiaLens uses and its version. Select **Change…** to point to a different kopia, or **Find automatically** to go back to the default.
