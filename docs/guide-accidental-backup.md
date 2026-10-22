# Clean up after a huge accidental backup

Something big got into your backups by mistake: a folder of virtual machines, a game library, a downloads folder. This guide stops it coming back and removes it from every snapshot that holds it. The removal is a [Pro](licensing.md) action. Every figure along the way is free.

## 1. Find it

1. Open **Search** and sort by **Physical (all)**, or open the **Tree** with **Show** set to **All snapshots**.
2. Right-click the folder and select **Details**.
3. In the [folder panel](folder-panel.md), check **Reclaimable** and **Backed up by**.

## 2. Choose how to remove it

| If | Use |
|---|---|
| The folder should never be backed up | **Exclude & delete…**: a rule for future snapshots, then removed from the old ones ([guide](guide-exclude-and-delete.md)) |
| The folder should stay backed up, but its old versions should go | **Prune snapshots…**: keep the newest and remove it from the rest ([Prune versions](prune.md)) |
| The folder is gone from disk and already excluded | **Delete folder…**: remove it from the snapshots ([Delete from backups](delete-files.md#delete-a-folder)) |
| It's a source of its own that you don't need at all | **Delete this source…** on the Dashboard ([Deleting snapshots and sources](delete-snapshots.md#delete-a-source)) |

## 3. Big folders: delete the biggest files first

A folder with millions of files can take a long time to plan and remove. Most of the space usually sits in a few hundred big files.

1. In the folder plan, set **Delete** to **Largest** and pick a number, for example 500.
2. Check **What gets removed** and the reclaimable figure.
3. Run the dry run, then the delete.
4. Repeat with the rest if you need to.

A plan holds at most 200000 files by default (**Settings → Advanced → Most files in one delete plan**). A bigger match is planned for its biggest files first.

## 4. Watch for copies

The same data can sit in more than one place:

- **Nested sources**: two sources that both cover the folder. The folder panel says so. Include both in the delete.
- **Identical copies at other paths**: the same files copied elsewhere. Match by **Exact content** to remove them too, or leave them, knowing they keep the data stored.

## 5. Get the space back

When the delete finishes, select **Run maintenance…** in the job panel and run a **Full** maintenance ([Maintenance](maintenance.md)). A full run after a large delete can take hours. Kopia needs a few runs to return all the space.

## Related

- [Free up space safely](guide-free-space.md)
- [Delete from backups](delete-files.md)
- [Why deleting frees space only after maintenance](deleting-and-space.md)
