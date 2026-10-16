# Restore files

You've lost a file, changed it by mistake, or need last week's version. This guide finds the file in your backups and gets it back.

## 1. Find the file

**If you know its name**, type part of it in the search box at the top of the window. Search looks through every snapshot, including files you've since deleted from disk. Tick **Deleted from disk** to see only those.

**If you know where it was**, open the **Tree**, pick the source and go to the folder. Set **Show** to **All snapshots** to see files that the latest snapshot no longer holds.

## 2. Pick the version

Right-click the file and select **Details**. The [file panel](file-panel.md) lists every version under **Versions**, newest first, with its date and size.

## 3. Get it back

Choose one way:

| Way | Best for |
|---|---|
| **Download** | One file, or a small folder. It downloads through your browser. A folder comes as a zip file |
| **Restore** | Putting files back on disk, or a big folder. You choose where they go |
| **Mount** | Looking around a whole snapshot with your own tools, and copying what you need |

To restore:

1. Select **Restore** on the version, or right-click a file or folder and select **Restore…**.
2. Under **Restore to**, choose **Next to the original**. KopiaLens writes a dated copy beside the original, for example `report (2026-09-01).docx`, so nothing is overwritten.
3. Select **Preview** to see what would happen.
4. Select **Restore…** and confirm.

**Warning:** **Where it came from** with **Replace different** or **Match snapshot** overwrites the files on disk with the backed-up versions. Use **Next to the original** or a separate folder unless you mean to replace them.

## A whole folder or snapshot

- For a folder, right-click it in the Tree and select **Restore…**.
- For a whole snapshot, right-click it in the **Snapshots** tab and select **Restore this snapshot…**.

Restore writes to the computer running KopiaLens. In Docker, the destination must be a folder mounted into the container. [Restore, download and mount](restore.md) describes every option.

## Related

- [Restore, download and mount](restore.md)
- [Search](search.md)
- [File panel](file-panel.md)
