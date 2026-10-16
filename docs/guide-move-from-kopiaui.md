# Move backups from KopiaUI to KopiaLens

You back up with KopiaUI today and want KopiaLens to run your backups instead. This guide hands a repository's schedules and maintenance over to KopiaLens, without losing any history. You can hand them back the same way at any time.

## Before you start

- KopiaLens uses the same repository and the same snapshots. Nothing is copied or converted.
- Only one app may run a repository's backups at a time. Two schedulers take every backup twice ([Who runs backups](backups.md#who-runs-backups)).
- Have the repository password ready. Moving the repository asks for it once.

**Warning:** never use **Disconnect** in KopiaUI for this. It deletes the connection file, and KopiaLens loses the repository too.

## 1. Connect the repository

If KopiaLens doesn't list the repository yet, add it. First-time setup and **Add repository…** both list the repositories KopiaUI uses, under **Found on this computer** ([Repositories](repositories.md)).

## 2. Hand the backups to KopiaLens

1. Open the **Dashboard** and select the **Backups:** button. It says **Backups: KopiaUI** while KopiaUI runs them.
2. Under **Who runs backups**, choose **KopiaLens**.
3. Choose how to share the repository with KopiaUI:
   - **Move this repository out of KopiaUI** (the usual choice): KopiaUI keeps its other repositories and stops loading this one after it restarts. Enter the repository password when asked.
   - **Keep sharing the connection with KopiaUI**: both apps keep the same connection. Then KopiaUI must not run: quit it and turn off its **Launch At Startup**. Otherwise KopiaLens waits until KopiaUI is closed.
4. Select **Save**.

If KopiaUI is open, KopiaLens takes over when KopiaUI restarts. Quit KopiaUI to take over now. For kopia's default connection, which KopiaUI always opens, sharing is the only choice, so KopiaUI must stay closed.

## 3. Check it worked

1. The **Backups:** button now says **Backups: KopiaLens**, with the next backup time.
2. Each source's **Next snapshot** on the Dashboard shows when it runs.
3. After the next scheduled time, the **Tasks** tab lists the backup.

Your policies, schedules and retention stay as they were. KopiaLens follows the same policies KopiaUI did. Maintenance runs as before when this computer is the [maintenance owner](glossary.md#maintenance-owner).

## Keep KopiaLens running

KopiaLens runs backups only while it's open. The desktop app starts when you sign in to your computer and stays in the tray. A Docker or server install is always on.

## Hand it back

Open **Who runs backups** again and choose **Another app, or nothing**. If you moved the repository out of KopiaUI, KopiaLens moves it back. Restart KopiaUI afterwards.

## Related

- [Backups](backups.md)
- [KopiaLens alongside KopiaUI](kopiaui.md)
- [Using KopiaLens with BlinkDisk](blinkdisk.md)
