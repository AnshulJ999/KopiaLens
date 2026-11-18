# Backblaze

KopiaLens can show what Backblaze Personal Backup backs up on this computer, or on another computer whose data folder it can read, next to your kopia backups. It reads Backblaze's own file lists and never writes to them.

![Screenshot: the Backblaze dashboard](images/backblaze.png)

## Add Backblaze

Backblaze Personal Backup runs on Windows and Mac. KopiaLens reads its data on this computer, or a shared folder from another computer.

1. Open **Settings → Backblaze**.
2. Select **Add Backblaze**. KopiaLens finds the client's data folder on its own.

Backblaze then appears in the repository list at the top of the window. Select it there to open its tabs. If Backblaze isn't on this computer, select **Backblaze on another computer?** and enter the shared **bzdata** folder. You can also point at a folder by hand in **Backblaze data folder**.

KopiaLens only reads the Backblaze folder. It never writes there, and it can't delete, restore or change anything in Backblaze.

On a Mac, macOS can stop KopiaLens from reading Backblaze's data folder. Setup then says so: allow KopiaLens in **System Settings → Privacy & Security → Full Disk Access**, and select **Check again**.

## The tabs

| Tab | What it shows |
|---|---|
| **Dashboard** | How many files and how much data Backblaze has selected, what is left to upload, coverage per drive, recently added folders and a breakdown by file type |
| **Search** | Every path Backblaze knows, backed up or excluded. Filter by drive, status and extension. Use `*` as a wildcard, for example `report*2024`. **By folder** groups the results |
| **Tree** | Backblaze's backup by folder, with each folder's status: backed up, excluded or mixed |
| **Excluded** | What on disk Backblaze doesn't back up, worked out from Backblaze's own scan and its exclusion rules. **Also in Kopia** shows folders that a kopia repository backs up too |
| **Exclusions** | Backblaze's exclusion rules: your folder rules, excluded extensions, and its built-in rules. Shows what each rule keeps out, and rules that match nothing on disk now |
| **Churn** | Recently added files by folder, to spot folders that keep changing |
| **Queue** | Files waiting to be uploaded, by folder and by drive |
| **Sessions** | What Backblaze uploaded each day, its backup runs, and its own count of uploads that worked or failed ([more below](#sessions)) |
| **Health** | Signs of trouble in Backblaze's own records, the installed Backblaze version and whether a newer one is out ([more below](#health)) |

Backblaze writes a new file list about once or twice a day, so most numbers change about that often. The upload queue changes as Backblaze uploads; the Queue page shows when Backblaze last updated it.

## Backblaze on another computer

KopiaLens doesn't have to run on the computer that has Backblaze. It only needs to read that computer's Backblaze data folder:

| Backblaze runs on | Its data folder |
|---|---|
| Windows | `C:\ProgramData\Backblaze\bzdata` |
| Mac | `/Library/Backblaze.bzpkg/bzdata` |

1. On the Backblaze computer, share that folder **read-only**. It lists every file name on that computer, so share it only with your own user.
2. Where KopiaLens runs, reach the share: a network path on Windows, **Connect to Server** in Finder on a Mac, a read-only volume in Docker, or **Settings → System → Storage → Network storage** in Home Assistant.
3. Enter the folder in **Backblaze data folder** (or **Backblaze on another computer?** during setup). KopiaLens indexes it straight away.

KopiaLens tells another computer's data apart by the computer name Backblaze stores, and shows it as **Backblaze on** that name. For another computer, the comparison with this computer's Kopia backups (**Also in Kopia**) is turned off, since that computer's files aren't on this one's disks. If the guess is ever wrong, change **Backblaze data is from** in Settings.

Switching to a different computer's folder starts a fresh Backblaze index and keeps the old one's history aside, so switching back brings it back.

## Naming-gap hunter

Backblaze's rules match exact paths, so the same kind of data can be excluded in one place and backed up in another. In **Exclusions**, type part of a folder name and select **Find gaps**. KopiaLens lists every folder with that name and whether Backblaze backs it up.

## Updates

KopiaLens reads Backblaze's lists again when they change. It waits a while after Backblaze's own scan before it does, so it doesn't read the lists halfway through a scan. These settings are in **Settings → Backblaze**:

- **Backblaze updates**: **When Backblaze's file lists change**, or **Off, only when I start it**.
- **Reindex minimum gap**: how long to wait after Backblaze's scan changes.
- **History retention**: how many days of folder history to keep for the trends.

To read the lists now, select **Index BZ** in the header.

## Related

- [Excluded](excluded.md)
- [Settings](settings.md#backblaze)
