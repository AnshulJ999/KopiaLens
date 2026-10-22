# Free up space safely

This guide frees space in a repository without losing anything you want to keep. It goes from the safest steps to the strongest. Stop as soon as you've freed enough.

## Before you start

- Find out where the space goes first ([Find what's using the most space](guide-find-space.md)).
- Deleting from backups can't be undone. Every step below shows exactly what goes and runs a dry run first. Read it.
- The space comes back only after kopia's next full maintenance, usually a day or two later ([When the space comes back](space.md#when-the-space-comes-back)).

## 1. Stop the growth

If a folder you don't need keeps adding data, exclude it first. Otherwise every later snapshot adds it again.

1. Right-click the folder or file and select **Exclude…**.
2. Check **Leaves out**, then select **Exclude…** ([Exclude](exclude.md)).

## 2. Let retention do its job

Snapshots that no retention rule and no pin keeps are deleted by kopia anyway, the next time it backs up that source. If your retention keeps more than you need, try a tighter one:

1. In the **Snapshots** tab, select **Simulate retention…**.
2. Try smaller numbers and select **Run**. Simulate shows what it would delete and free, without changing anything ([Simulate](simulate.md)).
3. When you're happy, set the same numbers in the policy's **Retention** section and save ([Policy editor](policy-editor.md#retention)).

## 3. Delete snapshots you don't need

1. In the **Snapshots** tab, right-click and choose **Select identical duplicates** or **Select unretained snapshots**, or select snapshots yourself.
2. Select **Delete snapshot…**. Check **Reclaimable** and the list of biggest files.
3. Select **Dry run**, then **Delete** ([Deleting snapshots and sources](delete-snapshots.md)).

Pinned snapshots are restore points you chose to keep. The panel lists them separately.

## 4. Remove single files or folders from your snapshots

When a few big files cause most of the cost, remove just them and keep the snapshots ([Pro](licensing.md)):

- **Prune versions…** keeps the newest versions of a file and removes the old ones ([Prune versions](prune.md)).
- **Delete from backups…** removes a file or folder from every snapshot that holds it ([Delete from backups](delete-files.md)).

Check the file panel's **Reclaimable** first. If identical copies elsewhere keep the data stored, removing one copy frees nothing.

## 5. Get the space back

1. Open the **Repository** tab and select **Run maintenance…**.
2. Choose **Full** and **Normal** safety, and start it ([Maintenance](maintenance.md)).

Kopia needs a few full runs to return all the space. When KopiaLens or KopiaUI runs the backups on the maintenance owner's computer, full maintenance also runs on its own, once a day by default.

## Related

- [How KopiaLens counts space](space.md)
- [Why deleting frees space only after maintenance](deleting-and-space.md)
