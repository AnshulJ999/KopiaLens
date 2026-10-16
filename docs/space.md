# How KopiaLens counts space

KopiaLens shows four sizes for files, folders and snapshots: **Logical**, **Physical**, **Physical (all)** and **Reclaimable**. This page explains what each one means, why they differ, and why deleting something doesn't always free space. The short definitions are in the [Glossary](glossary.md#space).

## How kopia stores data

Kopia splits every file into [pieces](glossary.md#piece), compresses them, and stores each distinct piece only once. Two things follow from this:

- A file that didn't change between snapshots adds nothing to the repository. The new snapshot uses the pieces that are already there.
- Identical data is stored once, wherever it appears: in another snapshot, at another path, or in another computer's backups.

So when little changes, a repository that holds 300 snapshots of a 1 TB disk can take little more than 1 TB. KopiaUI shows the sizes as they are on disk. KopiaLens also reads which pieces each file uses, so it can show what each one really costs.

## The four sizes

| Size | Question it answers |
|---|---|
| **Logical** (**Size** in tables) | How big is it on my disk? |
| **Physical** | How much repository space does the newest version use? |
| **Physical (all)** | How much repository space do all its versions use together? |
| **Reclaimable** | How much space comes back if I delete it? |

Logical is the size before compression and deduplication. Physical and Physical (all) are after both compression and dedupe, and count each piece once. Reclaimable counts only the pieces that nothing you keep still needs.

## An example

Sam backs up `D:\Videos` every day and keeps 30 snapshots. Three files show how the sizes differ.

**A 10 GB video that never changed.** All 30 snapshots hold the same pieces.

| Logical | Physical | Physical (all) | Reclaimable (all versions) |
|---|---|---|---|
| 10 GB | 10 GB | 10 GB | 10 GB |

Video is already compressed, so Physical equals Logical. Thirty snapshots cost the space of one, so Physical (all) is still 10 GB. Deleting just one snapshot frees nothing, because the other 29 still use the same pieces.

**A 1 GB log file that changes every day.** Each day's version is different, and text compresses well.

| Logical | Physical | Physical (all) | Reclaimable (all versions) |
|---|---|---|---|
| 1 GB | 250 MB | 7.5 GB | 7.5 GB |

The newest version takes 250 MB. The 30 versions together take 7.5 GB, because each one is mostly new data. This is [churn](glossary.md#churn): a file like this is often worth leaving out of the backup.

**The same 10 GB video, copied to `D:\Archive`.** Both paths use the same pieces.

| Path | Physical | Reclaimable |
|---|---|---|
| `D:\Videos\trip.mp4` | 10 GB | 0 |
| `D:\Archive\trip.mp4` | 10 GB | 0 |

Each copy's Physical is 10 GB, because each one uses those pieces. Deleting either copy alone frees nothing, because the other copy still needs them. Delete both, and 10 GB comes back. KopiaLens lists the [identical copies](glossary.md#identical-copies) in the file panel and in the delete plans, so you can include them.

## What Reclaimable counts

Reclaimable always answers "what does this delete give back", so it depends on what the delete covers:

| Where you see it | What deleting means |
|---|---|
| Dashboard row | Every snapshot of that source |
| Snapshots tab | That snapshot alone |
| File panel | Removing that file, or every version of it, from the snapshots that hold it |
| Folder panel and delete plans | The files and snapshots the plan lists |

That is why Reclaimable is usually far smaller than Physical (all). Most data in a repository is shared between snapshots, and shared data comes back only when the last thing using it goes.

The numbers come from the last [index run](glossary.md#index-run). Pieces added by snapshots taken since then have no size yet, so a number can read low until the next index run.

## When the space comes back

Deleting snapshots or removing files doesn't make the repository smaller straight away. It removes the references to the pieces. Kopia deletes the unused pieces later, during **full maintenance**.

Kopia waits on purpose, so that a computer backing up at the same time can't lose data. With kopia's default settings, a piece is deleted only after two full maintenance runs, at least 4 hours apart, have both found it unused. Full maintenance runs once a day by default. So expect the space back a day or two after a delete.

To start maintenance by hand, select **Run maintenance…** in the Repository tab. Full maintenance can take hours on a large repository.

To get the space back sooner, run full maintenance with **Safety: None**. Kopia then skips its waiting rules and deletes unused pieces in that run. Use it only when nothing else is writing to the repository at the time: no backup running on this computer, and no other computer or app using the same repository. A backup that runs during it can lose data ([Maintenance](maintenance.md)).

## The other space columns

| Column | Meaning |
|---|---|
| **Dedup** | The sizes of all snapshots added up, divided by the space they take. 30x means 30 snapshots for the price of one |
| **Accum** | The space all versions of a source take, divided by the space of its latest snapshot. 1.0x means old versions cost nothing extra |
| **% of repo** | A source's share of the space the whole repository takes |
| **New data** | The pieces a snapshot added that no earlier snapshot had |
