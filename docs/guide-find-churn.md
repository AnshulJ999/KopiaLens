# Find files that change every day

Some files are rewritten all the time: browser profiles, chat app caches, databases, virtual disks, logs. Each version adds new data to your backups, so they can cost many times their size. This guide finds them and decides what to do with each one.

## 1. Open Churn

1. Open the **Churn** tab.
2. Choose a window, for example **Last 30 days**.
3. Start with **By source** to see which sources add the most new data. **Mostly from** names the folder behind it.

## 2. Narrow it down

1. Switch to **By folder**. Set **Depth** to group deeper or shallower.
2. Sort by **New data** or **Per day**.
3. Look at **Rewrite**: versions divided by files. A high number means the same files are written again and again.
4. Switch to **By file type** to see whether one kind of file causes it, such as `.db` or `.log`.

To see the files themselves, open **Search**, open **More filters** and set **At least … versions**, then sort by **Physical (all)**.

## 3. Decide what to do

| What it is | What to do |
|---|---|
| A cache, temporary files or logs you don't need | Exclude it, and remove it from the old snapshots ([guide](guide-exclude-and-delete.md)) |
| A database or app data you need, but not every version | Keep backing it up, and prune its old versions now and then ([Prune versions](prune.md)) |
| Data you need, with every version | Keep it. Check that your retention keeps no more than you need ([Simulate](simulate.md)) |
| A folder covered by two sources | The churn counts twice. Remove the overlap in your sources or policies |

A row marked **inside** another source is backed up twice, once by each source.

## 4. Check the effect later

After the next index runs, open Churn again with the same window. The excluded folders no longer add new data.

## Related

- [Churn](churn.md)
- [Exclude](exclude.md)
- [How KopiaLens counts space](space.md)
