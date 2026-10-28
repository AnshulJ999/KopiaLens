# Hardware and memory

KopiaLens runs on an ordinary desktop, laptop or home server. This page says how much memory and disk space it needs, and what affects them.

## Memory

KopiaLens limits the memory its index may use to an eighth of the computer's memory, between 512 MB and 3 GB:

| Computer's memory | Index memory limit |
|---|---|
| 4 GB | 512 MB |
| 8 GB | 1 GB |
| 16 GB | 2 GB |
| 24 GB or more | 3 GB |

The limit is a ceiling, used only while big queries run: the memory is released after 30 minutes without use, and when an indexing run ends. It applies per repository, in the app and during indexing. To change it, set **Settings → Advanced → Memory limit** and restart KopiaLens.

While you browse, KopiaLens uses at most half the computer's processor cores, and fewer with a small memory limit, so the computer stays responsive. Indexing uses up to 4. To change either, set **Settings → Advanced → Processor threads** and restart KopiaLens.

Both repositories in [Tested setups](#tested-setups) built their full first index within the smallest limit, 512 MB. A smaller limit makes some work slower, not impossible.

Kopia itself also uses memory while KopiaLens reads from it, and while it runs backups. Indexing runs in its own process, which ends when the run is done, so its memory is released afterwards.

## Disk space

KopiaLens keeps its data in its data folder. What takes the space:

- **The index**: its size depends on how many files and versions your backups hold, not on how big the files are. As a guide, about 1 to 1.5 GB per 20 million file versions.
- **Index backups**: each older version kept takes up to the size of the index again. One is kept by default ([Index backups](indexing.md#index-backups)).
- **Kopia's cache**: kopia keeps a local cache for each repository connection, up to 5000 MB for file data and 5000 MB for metadata by default. Kopia sets these, not KopiaLens.
- **Logs**: small. Old job logs are cleaned up ([Logs](logs.md)).

First-time setup estimates the index size for each repository before the first index ([First-time setup](getting-started.md#3-build-the-index)).

An SSD makes indexing and searching faster. The data folder can live on any drive with enough space.

## Processor

Any recent processor works. Indexing uses up to 4 processor cores while it runs. After KopiaLens starts, it waits 5 minutes by default before any background work, so starting your computer isn't slowed down (**Settings → Advanced → Quiet period after startup**).

## Network

For a repository on cloud storage, the first index downloads its folder listings once. Later runs only download what changed. Some cloud services charge for downloads or for requests, so a first index of a large cloud repository can cost a little.

## Tested setups

KopiaLens is measured on these repositories, both on local disks:

| | Repository A | Repository B |
|---|---|---|
| Stored data | About 1 TB | About 4.1 TB |
| Snapshots | About 7,500 | About 3,500 |
| Paths | About 5.4 million | About 3.2 million |
| File versions | About 19.5 million | About 18.5 million |
| Index size | About 1.35 GB | About 1.06 GB |

## Related

- [How indexing works](how-indexing-works.md)
- [Indexing](indexing.md)
- [Installing KopiaLens](install.md)
