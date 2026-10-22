# Why deleting frees space only after maintenance

When you delete snapshots or remove files from them, the repository doesn't get smaller straight away. This page explains what kopia does with deleted data, and why the space comes back a day or two later.

## What a delete changes

Kopia stores your files as [pieces](glossary.md#piece), packed together into larger files in the repository's storage. A snapshot doesn't hold the pieces themselves. It holds a list of which pieces make up each file.

So a delete changes only those lists:

- **Deleting a snapshot** removes its list.
- **Removing files from snapshots** writes each snapshot again, without those files.

The pieces stay in storage. Some are still used by other snapshots, other paths or other computers' backups. Others are now unused, but kopia doesn't know that yet.

## What maintenance does

**Full maintenance** finds the unused pieces and removes them. It runs in steps:

1. It goes through every snapshot and marks the pieces that none of them uses.
2. On a later run, it checks again. Pieces that are still unused are dropped.
3. Packed files that now hold mostly unused pieces are rewritten, keeping only the pieces in use.
4. Packed files that hold nothing in use are deleted from storage. This is when the space comes back.

**Quick maintenance** only tidies kopia's own index and cache. It frees little space.

## Why kopia waits

Another computer, or another app, may be backing up into the same repository at the same moment. A backup that is still running can use a piece that looks unused, because its snapshot isn't saved yet. If kopia deleted that piece at once, the backup would be damaged.

So with kopia's default **Normal** safety:

- A piece is dropped only after two full maintenance runs, at least 4 hours apart, have both found it unused.
- Data written in the last 24 hours is never collected.
- A packed file is deleted from storage only when it's more than 24 hours old.

Full maintenance runs once a day by default. Together, these rules mean the space usually comes back a day or two after a delete.

## An example

You delete a 50 GB folder from all snapshots on Monday at 10:00.

| When | What happens | Repository size |
|---|---|---|
| Monday 10:05 | The delete finishes. The snapshots no longer list the folder | Unchanged |
| Monday night | Full maintenance marks the folder's pieces as unused | Unchanged |
| Tuesday night | Full maintenance finds them unused again, drops them and rewrites the packed files that held them | Shrinks as old packed files go |
| Wednesday | The last packed files, now old enough, are deleted | About 50 GB smaller |

The exact days depend on when maintenance runs on your repository.

## Getting it back sooner

- Run full maintenance by hand: **Run maintenance…** in the Repository tab ([Maintenance](maintenance.md)).
- With **Safety: None**, kopia skips its waiting rules and deletes unused data now. Use it only on a repository that only you use, when no backup is running.

## Why the freed space can differ from Reclaimable

KopiaLens's **Reclaimable** figure counts the pieces nothing else uses. The space that storage gives back can be slightly different:

- Kopia frees space packed file by packed file, so a few unused pieces can wait in a packed file that is still mostly in use.
- Snapshots taken after the last index run can use some of the same pieces.
- Kopia's own index and metadata take space too.

## Related

- [How KopiaLens counts space](space.md)
- [Maintenance](maintenance.md)
- [Free up space safely](guide-free-space.md)
