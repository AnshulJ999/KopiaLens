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

## The kopia program

The bottom of Settings → Repositories shows which kopia program KopiaLens uses and its version. Select **Change…** to point to a different kopia, or **Find automatically** to go back to the default.
