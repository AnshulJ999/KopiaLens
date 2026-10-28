# Update checks and usage stats

Once a day, KopiaLens asks the KopiaLens update service whether a newer version is out. By default it also sends a few anonymous usage stats, so we know roughly how many people use KopiaLens and which features matter most.

Both are on by default, and you can turn off either one in **Settings → Updates**. With both off, KopiaLens sends nothing unless you click **Check now**, which sends the update-check details once. The same page shows exactly what the next check sends.

KopiaLens never installs anything by itself. When a newer version is out, it says so. The installed Windows app can install it when you click **Install and restart**. Other copies link to the download. Before installing, KopiaLens checks that the installer is signed by KopiaLens.

When you click **Install and restart**, the app asks the update service for the installer's details, sending only its version, platform (`windows-x86_64`) and, on the beta channel, `channel=beta`. If the service can't be reached, it asks GitHub instead.

## What's sent

**Update check:**

| Field | Example |
|---|---|
| KopiaLens version | `1.2.0` |
| Update channel | `stable` or `beta` |
| Install type | `windows`, `linux`, `macos`, `docker` or `source` |
| Operating system | `windows`, `linux` or `macos` |
| Usage stats | `off`, or `waiting` (an install's first day, or a Docker container whose data folder isn't on a volume). Not sent when usage stats are on. Once, after you turn usage stats off, it also says so. No install ID is sent with this. |

**Anonymous usage stats** add:

| Field | Example |
|---|---|
| Install ID | A random ID created by KopiaLens and kept in its data folder (`checkin.json`). It counts this install once instead of once a day. It isn't linked to you, your computer or your licence. Usage stats start on an install's second day, and never from a Docker container whose data folder isn't on a volume. |
| CPU type | `amd64`, `arm64` |
| Licence | `free`, or the licence plan (`pro`, `lifetime`, `business`) |
| Repositories | How many kopia repositories KopiaLens has |
| Storage types | kopia's storage type names: `filesystem`, `sftp`, `b2`, `s3`, ... |
| Backblaze | Whether the Backblaze view is set up |
| Opened | Whether KopiaLens was opened since the last check (not just running in the background) |
| Features used | The names of the features used since the last check: `search`, `tree`, `prune`, `restore`, `csv_export`, ... Names only, no counts. |
| Setup | `finished`, `skipped` or `not_yet` |
| Used kopia before | The answer on the welcome screen: `yes` or `no`. `unanswered` when setup was skipped |
| First index | Whether a repository has been indexed |
| kopia | kopia's version, and whether it's the copy that came with KopiaLens |
| Own kopia server | Whether KopiaLens runs the backups for a repository |
| Index size | A rough size class of KopiaLens's own index: under 100 MB, 100 MB-1 GB, 1-5 GB, 5-20 GB or over 20 GB |
| Memory | A rough size class of this computer's memory, or the container's limit if lower: under 4 GB, 4-8 GB, 8-16 GB or over 16 GB |
| Failed update | If an in-app update failed, the step it stopped at (`downloading`, `installing`, ...), sent once |

The service also records the **country** the request came from (worked out by Cloudflare), only when usage stats are on.

## What's never sent

- Anything from your backups: paths, file names, sizes, snapshot details
- Repository addresses, bucket names, passwords, keys or licence codes
- Your settings, user name or computer name
- How much you use a feature: no counts or times
- Exact sizes: only the rough classes above

## What's stored

- Your IP address is **not** stored.
- With usage stats on: one row per install with the fields above, the first and last day it checked in, and how many days it was opened. An install that hasn't checked in for 90 days is deleted.
- Daily totals: how many checks came in per version, install type, OS and licence, how many installs were opened, and how many used each feature. For checks without usage stats: whether stats were off or not started yet, and how many installs turned them off that day. The update channel is kept only in the install's row.

## Feedback

**? → Give feedback…** is separate from these checks. It sends only what you type, plus the KopiaLens version, install type and operating system, and only when you select **Send**. No install ID is sent with it. The update service uses your IP address only to limit how many messages one address can send in a minute, and doesn't store it.

## Revoked licences

Once a week, KopiaLens also fetches the list of licences that were refunded or misused, signed by the licence service. Without a licence, this sends nothing about you. With one, the same request is the weekly licence check (see [Licensing](licensing.md)). It runs even with both switches off, so a refunded licence stops working on every copy. When KopiaLens can't reach the service, nothing changes.

## Turning it off

- In the app: **Settings → Updates**.
- Docker or headless installs: set `KOPIALENS_NO_UPDATE_CHECK=1` and/or `KOPIALENS_NO_USAGE_STATS=1`.
