# How indexing works

KopiaUI asks kopia for every answer while you wait. KopiaLens reads your snapshots ahead of time and keeps the answers in its own [index](glossary.md#index). This page explains what the index holds, how it's built, and what that means for your computer.

## What KopiaLens reads

An index run reads from kopia, through kopia's own commands, using the same repository connection KopiaUI or the kopia command line use:

1. **The snapshot list**: every snapshot of every source.
2. **The folders inside each snapshot**: names, sizes, times, and which pieces each file is made of.
3. **The pieces' stored sizes**: how much space each piece takes in the repository, after compression.

From these, KopiaLens works out each file's history across snapshots, and what each file, folder and snapshot costs in space ([How KopiaLens counts space](space.md)).

Indexing only reads. It never changes your snapshots, policies or repository.

## Why later runs are fast

Kopia stores a folder that didn't change between two snapshots only once. KopiaLens uses that: when it reaches a folder it has read before, it skips it. So after the first index, a run reads only the folders that changed in the new snapshots. On a typical repository, that takes minutes.

The first index reads everything once. For a repository on cloud storage, kopia downloads the folder listings during that first read and keeps them in its own cache, so later runs read locally.

## Where the index lives

The index is a set of files in KopiaLens's data folder, one set per repository. It's separate from your backups: deleting it loses nothing, and KopiaLens can always build it again from the repository.

Its size depends on how many files and versions your backups hold, not on how big the files are. For example, one repository with about 5.4 million paths and 19.5 million file versions has an index of about 1.35 GB.

You can encrypt the index ([Encryption](encryption.md)), keep older versions of it, and keep a copy on another drive ([Index backups](indexing.md#index-backups)).

## Using KopiaLens while it indexes

An index run builds a complete new index beside the current one, then switches to it in one step. Until then, every screen keeps reading the current index. So:

- KopiaLens stays usable during a run. The numbers change when the run finishes.
- A run that stops part-way leaves the current index untouched. What it had read is saved, and the next run continues from there.

## Your computer's resources

Indexing runs in a separate process that ends when the run is done. Its memory is released then, so KopiaLens stays light the rest of the time. Repositories are indexed one at a time.

**Settings → Advanced → Memory limit** caps the memory each repository's index may use. The default is based on your computer's memory ([Hardware and memory](hardware.md)).

## Snapshots taken after a run

A snapshot taken after the last index run isn't in the index yet. Kopia's own lists, such as the Snapshots tab's list, show it at once, but its space figures and its files in Search appear after the next run. The top bar shows how many snapshots are indexed ([How fresh the index is](indexing.md#how-fresh-the-index-is)).

## Repositories reached through a kopia server

Through a kopia server, kopia doesn't allow the reads an index needs. Such a repository is browse-only: snapshots, folders, restore and policies work, but there's no index ([Repositories](repositories.md#repositories-reached-through-a-kopia-server)).

## Related

- [Indexing](indexing.md)
- [How KopiaLens counts space](space.md)
- [Hardware and memory](hardware.md)
