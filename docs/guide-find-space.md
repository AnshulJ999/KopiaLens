# Find what's using the most space

Your repository is bigger than you expected, and you want to know why. This guide shows where to look, from the whole repository down to single files.

## 1. Find the biggest sources

1. Open the **Dashboard**.
2. Sort by **Physical (all)**: select the column header. The sources at the top take the most space.
3. Compare **Physical (all)** with **Physical**. When Physical (all) is much bigger, old versions cost more than the latest snapshot. **Accum** shows the ratio.

Right-click the column headers to show **Reclaimable** and **% of repo**. **Reclaimable** says what deleting every snapshot of the source would give back.

## 2. Find the biggest folders and files

**In the Tree:**

1. Open the **Tree** and pick the source, or **All sources**.
2. Set **Show** to **All snapshots**, so files you've since deleted from disk count too.
3. Sort by **Physical (all)**. Double-click a folder to go down a level.

**In Search:**

1. Open **Search** with an empty search box. It lists every file, biggest first.
2. Sort by **Physical (all)** to rank files by the space all their versions take.
3. Switch to **Folders** to rank folders instead. **Min depth** focuses on deeper folders.

## 3. Look for the usual causes

| What you see | Likely cause | Where to look |
|---|---|---|
| A file with many **Versions** and a big **Physical (all)** | A file that changes often, such as a database, a virtual disk or a log | [Find files that change every day](guide-find-churn.md) |
| **Copies** or **Src** above 1 | The same data at several paths, or in nested sources | The file panel's **Identical copies** |
| **On disk: deleted** with a big size | Files you deleted from disk that old snapshots still hold | Search with **Deleted from disk** ticked |
| A source with a high **Accum** | Old snapshots that hold a lot of changed data | [Simulate](simulate.md) a tighter retention |
| One huge folder that shouldn't be there | Something backed up by mistake | [Clean up after a huge accidental backup](guide-accidental-backup.md) |

## 4. Check what a file really costs

Right-click a file and select **Details**. The [file panel](file-panel.md) shows its versions, the data it shares with other files, and **Reclaimable**: what removing it would give back. A file can be large and still free nothing, because identical copies keep its data stored ([How KopiaLens counts space](space.md)).

## What to do next

- [Free up space safely](guide-free-space.md)
- [Exclude a folder and remove it from old snapshots](guide-exclude-and-delete.md)
