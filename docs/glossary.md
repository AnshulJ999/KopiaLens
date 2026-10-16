# Glossary

The terms KopiaLens uses, in one place. Where kopia has a word for something, KopiaLens uses kopia's word. For how the space terms fit together, see [How KopiaLens counts space](space.md).

## Backups and kopia

### Kopia

The open-source backup program KopiaLens works with. Every download of KopiaLens includes kopia. In these docs, `kopia` in lowercase means the program and its command line.

### Repository

The place where kopia stores your backups: a folder, a network share, or cloud storage such as S3, B2 or SFTP. One repository can hold the backups of many folders and many computers.

### Repository password

Kopia's password for a repository. It encrypts and unlocks the backups themselves. It is not your [KopiaLens password](#kopialens-password).

### Connection file

A small kopia file that says where a repository is and how to reach it. KopiaUI, the kopia command line and KopiaLens each use one per repository, and they can share one. See [Repositories](repositories.md#where-the-connection-file-goes).

### Source

A folder that kopia backs up, together with the user and computer that back it up, for example `sam@sam-pc:D:\Photos`. Kopia keeps a separate history of snapshots for each source.

### Owner

The `user@computer` part of a source, for example `sam@sam-pc`. Two computers that back up the same path are two different sources.

### Snapshot

One backup of a source at one moment. It records every file and folder as they were then. Unchanged data is not stored again, so a new snapshot usually adds little.

### Policy

The rules kopia applies to a source, a computer or the whole repository: what to leave out, how many snapshots to keep, when to back up, and how to compress. A folder's own policy overrides the policies above it.

### Retention

The part of a policy that says how many snapshots to keep: the latest few, plus some per hour, day, week, month and year. Kopia deletes the snapshots that no rule keeps.

### Pin

A mark on a snapshot that keeps it whatever retention says. Kopia never deletes a pinned snapshot on its own.

### Exclusion rule

A rule in a policy, or in a `.kopiaignore` file inside a folder, that tells kopia to leave files out of future snapshots. Existing snapshots keep those files until you remove them.

### Maintenance

Kopia's housekeeping for a repository. Quick maintenance runs often and tidies kopia's own records. **Full maintenance** also deletes data that no snapshot uses any more. Deleting snapshots or files frees space only when full maintenance runs.

### Maintenance owner

The one `user@computer` allowed to run maintenance on a repository. Kopia refuses maintenance from anyone else, so two computers never run it at the same time.

### Mount

Shows a snapshot as a read-only folder on your computer, so you can open its files with any program. Nothing is copied until you open a file.

### Restore

Copies files or folders from a snapshot back to your disk, to their original place or to a folder you choose.

## Space

### Piece

A part of a file as kopia stores it. Kopia splits files into pieces and stores each distinct piece once. Files and snapshots that hold the same data share the same pieces.

### Deduplication

Storing identical pieces only once. A file that didn't change between snapshots, or two identical copies of a file, take the space of one. The **Dedup** column shows the sizes of all snapshots added up, divided by the space they take.

### Logical

How big a file or folder is on your disk, before kopia compresses or deduplicates it. For a file with several versions, this is the newest version. Tables call it **Size**.

### Physical

The space the newest version takes in the repository, after compression. Pieces it uses more than once count once. Pieces it shares with other files still count in full here, because this file uses them too.

### Physical (all)

The space every version together takes in the repository, after compression and deduplication. Pieces shared between the versions count once.

### Reclaimable

The space a delete gives back: the pieces that nothing you keep still uses. Pieces that another snapshot, version or file still needs stay. The space comes back when kopia's full maintenance runs, not at the moment you delete. What the delete covers depends on where you see the number: see [What Reclaimable counts](space.md#what-reclaimable-counts).

### Accum

The space all versions of a source take, divided by the space its latest snapshot takes. **1.0x** means old versions cost nothing extra. A high value means old versions hold a lot of data that the latest snapshot doesn't.

### Churn

The new data a snapshot added: pieces no earlier snapshot of that source had. The Churn tab shows which folders and files keep adding new data over a period you choose. Files that change all the time, such as caches and logs, are often worth leaving out.

### Versions

The different contents a file had over time. A file that never changed has one version, however many snapshots hold it.

### Identical copies

Files with exactly the same content at other paths, or from other sources. They share their pieces, so deleting one copy frees nothing while another copy stays.

## KopiaLens

### Index

KopiaLens's own record of what each repository holds: every file and folder in every snapshot, with the sizes of their pieces. KopiaLens builds it by reading kopia, keeps it in its data folder, and never writes into the repository. Most numbers in KopiaLens come from the index. See [Indexing](indexing.md).

### Index run

One update of the index. The first run reads every snapshot. Later runs read only what changed since. By default KopiaLens runs one every day. **Index** in the top bar starts one now (**Index now** for a repository that has no index yet).

### Who runs backups

The app that takes a repository's scheduled snapshots and runs its maintenance: KopiaLens, or another app such as KopiaUI or BlinkDisk. Only one app may do it at a time. See [Backups](backups.md#who-runs-backups).

### Browse-only

A repository reached through a kopia server instead of directly. Kopia doesn't allow the reads an index needs over a server connection, so you can browse, restore and mount, but there are no sizes or space analysis. See [Repositories](repositories.md#repositories-reached-through-a-kopia-server).

### Read-only connection

Kopia's own lock on a connection: kopia refuses every change made through it. See [Repositories](repositories.md#read-only-connections).

### Permissions

How much KopiaLens may change, for the whole app or for one repository: **Read-only**, **Restores allowed**, **Non-destructive** or **Full Access**. Set them in **Settings → Permissions**. [API tokens](api-tokens.md#permissions) use the same four levels.

### Dry run

A trial of a delete or removal that changes nothing. Kopia reports exactly what it would remove. Where kopia has no dry run, KopiaLens shows a **Preview** instead.

### Plan

The exact list of files or snapshots a delete or removal will act on, with what it frees. KopiaLens deletes exactly what the plan showed, even if the index changes in between.

### Prune

Removing chosen versions of a file, or a folder from chosen snapshots, while keeping the rest. Part of Pro.

### Exclude & delete

Adding an exclusion rule so future snapshots leave something out, then removing it from the existing snapshots in the same step. Part of Pro.

### Job

An action you start that KopiaLens runs in the background, such as a dry run, a delete, a restore or a pin. Jobs show their progress and log, and most can be stopped.

### Task

A scheduled or manual backup or maintenance run. The Tasks tab lists tasks and jobs, running and finished.

### Simulate

Shows which snapshots a retention policy would keep and delete, and how much space that frees, before you change the policy.

### Excluded

The tab that compares a disk with your backups. It shows which files and folders are backed up, left out by a rule, or never backed up.

### Pro

The paid part of KopiaLens: the actions that remove data from inside your backups. Every number and preview is free. See [Free and Pro](licensing.md).

## Security

### KopiaLens password

The password you sign in to KopiaLens with. It protects KopiaLens only. By default it also unlocks encryption. See [Sign-in and your account](sign-in.md).

### Encryption

How KopiaLens protects its own stored data on disk: **Off**, **Index** (file and folder names, sizes and snapshot contents) or **Full** (also history, logs, policy backups and the Backblaze data). Your kopia repository is always encrypted by kopia, whatever you choose here. See [Encryption](encryption.md).

### Encryption password

The password that unlocks KopiaLens's encrypted data. By default it is your KopiaLens password. You can set a separate one.

### Recovery key

A long code that unlocks KopiaLens's encrypted data if you forget the encryption password. KopiaLens shows it once, when you turn encryption on. See [Encryption](encryption.md#the-recovery-key).

### API token

A key that lets a script, the command line or an AI agent use KopiaLens without your password. Each token has a name, a permission and an expiry date. See [API tokens](api-tokens.md).

### Approval

Your yes, in KopiaLens, to one risky request from an API token, such as a delete. An approval works once, for that exact request. See [API tokens](api-tokens.md#approvals).

### Setup code

A one-time code that a server or Docker install asks for when someone creates the first account more than 30 minutes after KopiaLens started. KopiaLens prints it in its log. See [Sign-in and your account](sign-in.md#first-time-setup-on-a-server).
