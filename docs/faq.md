# FAQ and troubleshooting

Answers to common questions, and what to do when something doesn't work. If your problem isn't here, see [Getting help](getting-help.md).

## About KopiaLens

### Can KopiaLens damage my backups?

KopiaLens only reads your repository unless you start an action that changes it. Every such action shows exactly what it does first, runs a dry run where kopia has one, asks you to confirm, and is recorded in **Logs → Audit**. Indexing never changes the repository. [Permissions](settings.md#permissions) can limit what KopiaLens may change in each repository.

### Do I need KopiaUI?

No. KopiaLens includes kopia, and can create repositories, run backups on schedule, restore and edit policies. You can also keep using KopiaUI beside it ([KopiaLens alongside KopiaUI](kopiaui.md)).

### Does KopiaLens send my data anywhere?

No file names, paths or backup contents ever leave your computer. KopiaLens checks for updates once a day and sends anonymous usage numbers with it. Both can be turned off ([Update checks and usage stats](usage-stats.md)).

### Why does KopiaLens start kopia with `--insecure`?

When KopiaLens runs backups for a repository, or when **Listing backend** is set to keep a kopia server running, it starts a kopia server in the background. That server listens only on this computer (`127.0.0.1`), on a random port, with a random password made for each start, and nothing on your network can reach it. `--insecure` means the server doesn't use HTTPS, which a connection that never leaves the computer doesn't need. `--disable-csrf-token-checks` turns off a protection for kopia's own web page in a browser. KopiaLens talks to the server directly, not through a browser. Your repository stays encrypted as always.

### Where does KopiaLens keep its data?

In its data folder. **Settings → About** shows where it is. It holds the settings, the index, logs and history, never your backups.

### How do I move KopiaLens to another computer?

Use **Export to a file** in **Settings → Config** on the old computer, and **Import from a file** on the new one ([Config](settings.md#config)). The new install builds its index again from the repositories.

## Space

### Why didn't the repository get smaller after a delete?

Kopia frees the space during its full maintenance runs, usually a few days later ([Why deleting frees space only after maintenance](deleting-and-space.md)).

### I deleted a big file, but Reclaimable said it frees almost nothing. Why?

Its data is still used elsewhere: by other snapshots, identical copies at other paths, or other sources. The file panel lists where. Removing every copy frees the space ([How KopiaLens counts space](space.md#an-example)).

### Why are the sizes different from KopiaUI's?

KopiaUI shows sizes as they are on disk (logical). KopiaLens also shows what the data takes in the repository after compression and deduplication, and what deleting it gives back ([How KopiaLens counts space](space.md)).

## Indexing

### A new snapshot doesn't show in Search or the Tree.

Search and the Tree read the index, which updates on its schedule. Select **Index** in the top bar to read new snapshots now ([Indexing](indexing.md)).

### The first index is taking hours.

That's normal for a large repository, and slower on cloud storage. You can keep using KopiaLens, and if the run stops, the next one continues where it was ([First index](indexing.md#first-index)).

### An index run failed or stopped.

Open **Logs → Indexing** to see which stage failed and why. Common causes:

- The repository couldn't be reached, for example a drive was unplugged. The run waits a while, then gives up and keeps its progress.
- The run hit its time limit (**Settings → Advanced → Indexing time limit**). The next run continues.
- The run made no progress at all for a long time and was stopped as stuck.

A failed run is tried again on its own ([Time limits and stuck runs](indexing.md#time-limits-and-stuck-runs)).

### A repository says "Browse-only".

It's reached through a kopia server, which doesn't allow the reads an index needs. Connect to its storage directly instead ([Repositories](repositories.md#repositories-reached-through-a-kopia-server)).

## Backups

### Backups don't run.

Check these in order:

1. The **Backups:** button on the Dashboard names the app that runs them. If it's another app, that app must be running.
2. If it's KopiaLens, KopiaLens must be open. The desktop app runs in the tray.
3. The folder's policy needs a schedule. A new repository has none until you set one ([Policy editor](policy-editor.md#schedule)).
4. Backups may be paused. The top bar shows **Backups paused** with a **Resume** button.

### Every backup is taken twice.

Two apps run the same repository's backups, for example KopiaLens and KopiaUI. Choose one in **Who runs backups…** ([Who runs backups](backups.md#who-runs-backups)).

### Snapshots show up under two computer names.

Kopia treats `Sam@Sam-PC` and `sam@sam-pc` as different computers. The Repository tab's **Health** card warns about it, and **Who runs backups…** offers to merge the two histories ([The computer name](blinkdisk.md#the-computer-name-and-one-history-per-folder)).

### Maintenance won't run: "not the maintenance owner".

Kopia lets only one user and computer run maintenance on a repository ([Who can run it](maintenance.md#who-can-run-it)).

## Passwords

### I forgot my KopiaLens password.

See [Forgot your password](sign-in.md#forgot-your-password).

### I forgot my repository password.

Nobody can recover it, not kopia and not KopiaLens. Without it, the backups can't be read. A computer whose connection already has the password saved keeps working, so don't remove that connection. Store the password somewhere safe, such as a password manager.

### I lost the encryption password and the recovery key.

See [Lost the password and the recovery key](encryption.md#lost-the-password-and-the-recovery-key). Your backups aren't affected. Only KopiaLens's index is.

## Installing and connecting

### Windows says "Windows protected your PC", or macOS can't verify the app.

KopiaLens isn't signed by Microsoft or Apple. See [Installing KopiaLens](install.md) for the steps to open it.

### The desktop app says the port is in use.

Another program uses port 7123. The desktop app offers the next free port and saves it. To pick one yourself, use **Settings → Advanced → Ports**.

### I can't open KopiaLens from another device.

Check **Settings → Remote access → Who can open KopiaLens**. The desktop app allows only this computer by default. Change it, then restart KopiaLens ([Remote access and HTTPS](remote-access.md)).

### The Excluded tab shows sizes as "not measured".

The KopiaLens scanner counts a folder for at most 15 seconds by default while you wait. A subfolder that needs longer shows **Not counted**: right-click it and select **Count size**. On Windows, install and run Everything, and KopiaLens answers at once ([Disk scanning](disk-scanning.md)).
