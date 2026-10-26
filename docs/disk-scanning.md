# Disk scanning

KopiaLens reads your disks to show what no backup covers, to preview exclusion rules and to size a folder before a new snapshot. This page explains how it reads the disk, how to turn on the KopiaLens scanner, and how to add other places to check, such as a network share.

## How KopiaLens reads the disk

Choose the method in **Settings → Disk scanning → Read the disk with**:

| Choice | What it does |
|---|---|
| **Automatic** | Windows only. Uses Everything when it's running, otherwise the KopiaLens scanner |
| **Everything** (default on Windows) | Windows only. Everything is a free file index by voidtools. It answers at once and never reads the disk itself. KopiaLens includes Everything's command-line tool, so you only install Everything |
| **KopiaLens scanner** | KopiaLens's own scanner. It works on Windows, Linux, macOS, in Docker and in Home Assistant |
| **Off** (default on Linux and macOS) | KopiaLens doesn't read the disk. The Excluded tab and rule previews stay empty |

The KopiaLens scanner counts folders by reading the disk. It never changes, moves or deletes a file. A big drive stays busy while the scanner counts it, so the scanner is off until you choose it.

## What the KopiaLens scanner counts, and when

The scanner counts only what you ask for:

- When you open a folder in the Excluded tab, KopiaLens counts that folder. It counts for up to the **Scan time limit** (15 seconds by default) while you wait. A subfolder that needs longer shows **Not counted**.
- To count a folder or drive to the end, right-click it in the Excluded tab and select **Count size**. The count shows above the table with **Stop**.
- The drive list shows **Not counted** until you count a drive. Hold the pointer over **Not counted** to see the used space of that drive.
- When you leave the Excluded tab, KopiaLens stops the counts it started for you, unless **Keep counting after the time limit** is on. A **Count size** keeps going.

KopiaLens keeps each counted size until you count that folder again, or until KopiaLens restarts. **Keep counted sizes** changes that. The Excluded tab shows when the sizes were counted.

The scanner counts at most one folder at a time on each disk. It uses fewer threads on hard disks, USB drives and network shares. If a disk stops answering, KopiaLens stops the count.

Like kopia, the scanner doesn't follow links (symbolic links and junctions), so it counts what a snapshot holds. Everything counts the files behind a link, so its totals can be a little higher. To count what links point to, turn on **Follow links**.

On Linux and macOS, another disk can be mounted inside a folder. The scanner doesn't go into it, and the Excluded tab shows it as **other disk**. Open it to count it on its own.

The scanner never counts KopiaLens's own data folder or the folders where it mounts snapshots.

## Settings

| Setting | Default | What it does |
|---|---|---|
| **Read the disk with** | Everything (Windows), Off (Linux and macOS) | See the table above |
| **Scan time limit** | 15 seconds | How long a folder you open is counted while you wait |
| **Keep counting after the time limit** | Off | A folder you open is counted to the end, even after you leave it |
| **Count drives and places on their own** | Off | The Excluded tab counts each drive's total when it opens |
| **Look for ignore files when indexing** | Off | Each index run searches the backed-up folders of the computer running KopiaLens for ignore files, such as `.kopiaignore`. With Everything, this search always runs, because it costs nothing |
| **Follow links** | Off | Counts what links and junctions point to |
| **Scanner threads** | Automatic | How many folders the scanner reads at the same time. Automatic uses up to 8 on an SSD and 2 on a hard disk, a USB drive or a network share |
| **Keep counted sizes** | Until you recount | How long a counted size is used |
| **Everything (es.exe) path** | | Where `es.exe` is, if KopiaLens doesn't find it |
| **Excluded-folder tolerance** | 5% | In the Excluded tab, a folder counts as backed up when less than this share of it is excluded |
| **Show this computer's drives** | On | Lists the drives of the computer running KopiaLens in the Excluded tab. Turn it off on a server that only checks shared folders |

## Install Everything (Windows)

1. Select **Get Everything** in the Excluded tab or in **Settings → Disk scanning**. The voidtools download page opens.
2. Download and run the Everything installer.
3. Keep Everything running. It starts with Windows by default.
4. In KopiaLens, open the Excluded tab again.

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

Mount the share into the container, then add the folder inside the container as a place. For example, mount the share at `/shares/sam-pc` and enter `/shares/sam-pc` in **Folder on this computer**. Docker Desktop on Windows and macOS reads folders from the computer slowly, so a count there takes longer than on Linux.

### In Home Assistant

Add the share in Home Assistant's **Settings → System → Storage → Add network storage**. It then appears under `/media`, `/share` or `/backup` in the add-on. Add that folder as a place.

## Limits

- On Windows, network drives and removable drives don't show as drives. Add a network share as a place instead.
- A folder on a network share takes longer to count than a local folder.
- Everything counts only the places it indexes. For a place it doesn't index, the Excluded tab shows **Not indexed**.
- KopiaLens only reads folders. It never changes, moves or deletes a file on disk.

## Related

- [Excluded](excluded.md)
- [Settings](settings.md)
- [Running KopiaLens in Docker](docker.md)
- [Home Assistant](home-assistant.md)
