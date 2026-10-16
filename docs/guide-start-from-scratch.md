# Start backing up from scratch

This guide is for you if you've never used kopia. By the end, KopiaLens backs up the folders you choose on a schedule, and you've checked that the first backup worked.

## Before you start

You need somewhere to keep the backups, away from the files themselves. A backup on the same drive is lost with the drive. Good choices:

- An external drive or a second internal drive.
- A network share on a NAS.
- Cloud storage: Backblaze B2, Amazon S3 or another S3-compatible service, SFTP, Google Cloud Storage, Azure or WebDAV.

Kopia stores your backups in a [repository](glossary.md#repository) there. It encrypts everything with a **repository password** that you choose.

**Warning:** without the repository password, nobody can read the backups, including you. Kopia can't reset it. Store it in a password manager or somewhere safe away from the computer.

## 1. Create a repository

1. Open KopiaLens. First-time setup starts at **Connect a repository** ([First-time setup](getting-started.md)).
2. Select **Create a new repository**.
3. Pick the **Storage type** and fill in its details, for example the folder on your external drive. The storage location must be empty.
4. Enter a **Repository password** and a **Name** for the repository.
5. Select **Create repository**.

To create one later, use **Add repository…** in the repository list at the top of the window ([Repositories](repositories.md)).

## 2. Finish setup

1. **Protect KopiaLens**: set a KopiaLens password and choose encryption ([Sign-in and your account](sign-in.md)). Save the recovery key if KopiaLens shows one.
2. **Build the index**: check that **Backups** says KopiaLens runs them. If it names another app, select **Change…** and choose **KopiaLens**, so KopiaLens takes the backups on schedule ([Who runs backups](backups.md#who-runs-backups)).
3. Select **Start indexing**. A new repository has nothing to read yet, so this takes a moment.

## 3. Choose when to back up

A new repository has no schedule. Set one in its Global policy, which every folder uses unless you give it its own:

1. Open the **Policies** tab and select **Global policy**.
2. Open **Schedule**. Set **Snapshot every** (for example 1 hour) or **Times of day** (for example 21:00).
3. Optional: in **Retention**, check how many snapshots to keep. Kopia's defaults keep a mix of recent, daily, weekly, monthly and yearly snapshots.
4. Select **Review and save…**, then **Save changes**.

## 4. Back up your first folder

1. Open the **Dashboard** and select **New snapshot…**.
2. Enter the folder, or select **Browse…**. For example your Documents folder.
3. Optional: select **Estimate size** to see how much kopia will back up.
4. Select **Snapshot now**.

The [job panel](job-panel.md) shows the backup's progress. The first backup of a folder copies everything, so it takes the longest. Later backups only add what changed.

Repeat this step for each folder you want to back up. From now on, KopiaLens backs them up on the schedule while it runs. The desktop app starts when you sign in to your computer and stays in the tray.

## 5. Check the backup

1. Select **Index** in the top bar, so KopiaLens reads the new snapshot.
2. Open the **Snapshots** tab. The folder's snapshot is listed with its size.
3. Open the **Tree** tab, pick the folder, and look inside the snapshot.
4. Right-click a small file and select **Download**. Open the file to check it.

## What to do next

- Keep junk out of your backups: caches and temporary files can cost a lot of space ([Exclude](exclude.md)).
- See what's not backed up yet ([Check your backups](guide-check-backups.md)).
- Learn how to get files back ([Restore files](guide-restore-files.md)).
- Get told when a backup fails ([Notifications](notifications.md)).
