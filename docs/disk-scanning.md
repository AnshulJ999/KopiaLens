# Disk scanning

KopiaLens reads your disks to show what no backup covers, to preview exclusion rules and to size a folder before a new snapshot. This page explains how it reads the disk and how to add other places to check, such as a network share.

## How KopiaLens reads the disk

Choose the method in **Settings → Disk scanning → Read the disk with**:

| Choice | What it does |
|---|---|
| **Automatic** (default) | Uses Everything when it's running, otherwise the KopiaLens scanner |
| **Everything only** | Windows only. Everything is a free file index by voidtools. It answers at once, but it must be installed and running |
| **KopiaLens scanner** | KopiaLens's own scanner. It works on Windows, Linux, macOS, in Docker and in Home Assistant |
| **Off** | KopiaLens doesn't read the disk. The Excluded tab and rule previews stay empty |

The KopiaLens scanner reads many folders at the same time, so it's fast on a solid-state drive. A hard disk or a network share takes longer the first time.

When a folder takes longer than the **Scan time limit** (15 seconds by default), the Excluded tab shows **Counting…** instead of a size. KopiaLens keeps counting that folder in the background and fills in the size when it's ready. You don't need to reopen the folder.

Like kopia, the scanner doesn't follow links (symbolic links and junctions), so it counts what a snapshot would hold. Everything counts the files behind a link, so its totals can be a little higher.

## Settings

| Setting | Default | What it does |
|---|---|---|
| **Read the disk with** | Automatic | See the table above |
| **Scan time limit** | 15 seconds | How long a folder can take before the Excluded tab shows **Counting…** |
| **Keep counting in the background** | On | Counts a folder that ran out of time, one folder at a time, so it's ready next time |
| **Scanner threads** | Automatic | How many folders the scanner reads at the same time. Automatic uses up to 8. Choose fewer for a hard disk or a network share |
| **Reuse folder sizes for** | 5 minutes | How long a counted folder's size is used again before KopiaLens counts it again |
| **Everything (es.exe) path** | | Where `es.exe` is, if KopiaLens doesn't find it |
| **Excluded-folder tolerance** | 5% | In the Excluded tab, a folder counts as backed up when less than this share of it is excluded |
| **Show this computer's drives** | On | Lists the drives of the computer running KopiaLens in the Excluded tab. Turn it off on a server that only checks shared folders |

## Check other places

The Excluded tab lists the drives of the computer running KopiaLens. You can add other places: a network share, a mounted folder, or a folder that Docker or Home Assistant gives to KopiaLens.

This is how a KopiaLens server checks the files of the computers it backs up. The server can't see their disks, but it can read a folder they share.

To add a place:

1. Open **Settings → Disk scanning**.
2. Select **Add a place**.
3. In **Folder on this computer**, enter the folder as the computer running KopiaLens sees it, for example `/mnt/sam-pc/Users` or `\\nas\photos`.
4. If the backups hold the same files under another path, enter that path in **Backed up as**, for example `C:\Users`.
5. If only one computer's backups hold those files, enter its name in **On computer**, for example `sam-pc`. KopiaLens suggests the computer names it finds in your backups.
6. Select **Save**.

The place now shows in the Excluded tab beside the drives. Inside it, KopiaLens compares each folder with the path in **Backed up as**, so a share of `C:\Users` on `sam-pc` is checked against that computer's backups of `C:\Users`.

**Exclude…** and **Policy…** in the Excluded tab use the backed-up path, so a rule you add there applies to the backups of that folder.

### In Docker

Mount the share into the container, then add the folder inside the container as a place. For example, mount the share at `/shares/sam-pc` and enter `/shares/sam-pc` in **Folder on this computer**. The KopiaLens scanner in Docker gets no notice when files change, so sizes are counted again after **Reuse folder sizes for**.

### In Home Assistant

Add the share in Home Assistant's **Settings → System → Storage → Add network storage**. It then appears under `/media`, `/share` or `/backup` in the add-on. Add that folder as a place.

## Limits

- On Windows, network drives and removable drives don't show as drives. Add a network share as a place instead.
- A folder on a network share takes longer to count than a local folder.
- KopiaLens only reads folders. It never changes, moves or deletes a file on disk.

## Related

- [Excluded](excluded.md)
- [Settings](settings.md)
- [Running KopiaLens in Docker](docker.md)
- [Home Assistant](home-assistant.md)
