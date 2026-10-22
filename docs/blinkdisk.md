# Using KopiaLens with BlinkDisk

BlinkDisk stores its vaults as standard kopia repositories, so KopiaLens can open them. You can keep using BlinkDisk for your backups and use KopiaLens to see what they hold, find what takes space, and clean up. BlinkDisk doesn't need to change anything.

## Connect a vault

1. In KopiaLens, add a repository. BlinkDisk vaults on this computer show under **Found on this computer**, labelled as BlinkDisk vaults.
2. Pick the vault and enter its password. BlinkDisk keeps the vault password in its own store, so KopiaLens asks for it once.
3. Index the vault. Its folders show as sources, like any other kopia repository.

BlinkDisk adds an empty "draft" source for each folder you set up. KopiaLens labels these **(BlinkDisk draft)**. They hold no snapshots.

## Who runs backups and maintenance

BlinkDisk runs the vault's schedules and its maintenance while it's open. Leave it that way unless you want KopiaLens to take over:

- KopiaLens shows **Backups: BlinkDisk** on the Dashboard. Open it to see what's running.
- To let KopiaLens run the backups instead, choose **KopiaLens** in **Who runs backups…**, then quit BlinkDisk. Two apps must never run backups for the same vault at once.
- Maintenance belongs to BlinkDisk's connection. KopiaLens never moves it, so BlinkDisk keeps cleaning up the vault.

## Changes show up late in BlinkDisk

BlinkDisk re-reads the vault every few hours. After you delete or clean up in KopiaLens, restart BlinkDisk to see the change straight away. Space is freed after BlinkDisk's next full maintenance.

## The computer name, and one history per folder

Kopia files every snapshot under a name like `you@your-computer`, and the name is case-sensitive. BlinkDisk writes the computer name with capitals (`Sam@Sam-PC`). A plain kopia connection usually writes it in lowercase (`Sam@sam-pc`). To kopia those are two different computers, so:

- A snapshot taken from a lowercase connection starts a **separate history** that BlinkDisk doesn't show.
- Maintenance from that connection is refused, because BlinkDisk's name owns it.

KopiaLens spots this in **Who runs backups…** and in the Repository tab's health checks. It keeps the spelling the vault's maintenance owner uses (BlinkDisk's), and the two steps work in either order:

1. **Connect as** the vault's name (for example `Sam@Sam-PC`). Later backups from KopiaLens then join BlinkDisk's history. Only the connection's name changes. The vault stays as it is.
2. If snapshots were already made under both names, **merge the histories**. Run the dry run first to see each snapshot that moves, then merge. Afterwards each folder has one history again. Moved snapshots get new IDs, and any policy set under the old name stays under that name (you can copy its settings in Policies).

If you connected with KopiaUI too, it shares the same connection file, so it uses the new name as well.

## Don't upgrade the vault's format

BlinkDisk runs its own copy of kopia, which can be older than the one KopiaLens uses. Never run a repository format upgrade (`kopia repository upgrade`) on a BlinkDisk vault, from KopiaLens or the kopia command line. BlinkDisk may no longer be able to open it.

## Related

- [Repositories](repositories.md)
- [Getting help](getting-help.md)
