# Check your backups

A backup you never check can fail without telling you. This guide checks the four things that matter: backups are running, they hold what you think, the repository is healthy, and you can get files back.

## 1. Are backups running?

1. Open the **Dashboard** and sort by **Last snapshot**.
2. Look at **Next snapshot**. **overdue** means a scheduled backup didn't happen.
3. Open the **Tasks** tab and select **Failed** to see backups that failed or finished with errors. Open one to read kopia's log.

If backups don't run at all, check who runs them: the **Backups** button on the Dashboard names the app ([Who runs backups](backups.md#who-runs-backups)). KopiaLens runs backups only while it's open.

To get told when a backup fails, set up a notification ([Notifications](notifications.md)).

## 2. Is everything backed up?

1. Open the **Excluded** tab.
2. Keep **Not backed up** selected and open each drive.
3. Look for folders marked **NOT backed up** or **partial** that you expected to be in your backups ([Excluded](excluded.md)).

To back up a missing folder, use **New snapshot…** on the Dashboard. To see why something is left out, open its **Details**: the panel lists the exclusion rules that apply.

## 3. Is the repository healthy?

1. Open the **Repository** tab. The **Health** card lists any problem it finds, such as maintenance that hasn't run for a long time ([Repository](repository.md)).
2. Select **Verify backups…**. Choose **Every source** and a share of files to download and check, for example **5% of files**.
3. Start it. The verify runs as a job, and its result shows in the job panel and the Tasks tab.

Verifying more files catches more damage but takes longer. On cloud storage it also costs download traffic ([Checking backups](backups.md#checking-backups)).

## 4. Can you get files back?

Try a restore now, before you need one:

1. Pick a file you know, in Search or the Tree.
2. Right-click it and select **Download**, or restore it **Next to the original** ([Restore files](guide-restore-files.md)).
3. Open it and check it.

## How often

- Look at the Dashboard and Tasks once a week, or let notifications tell you.
- Check the Excluded tab after you add new folders or drives.
- Verify backups now and then, for example once a month.

## Related

- [Backups](backups.md)
- [Excluded](excluded.md)
- [Repository](repository.md)
