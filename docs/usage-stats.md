# Update checks and usage stats

Once a day, KopiaLens asks the KopiaLens update service whether a newer version is out. By default it also sends a few anonymous usage stats, so we know roughly how many people use KopiaLens and which features matter most.

Both are on by default, and you can turn off either one in **Settings → Updates**. With both off, KopiaLens sends nothing unless you click **Check now**, which sends the update-check details once. The same page shows exactly what the next check sends.

KopiaLens never installs anything by itself. When a newer version is out, it says so. The installed Windows app can install it when you click **Install and restart**. Other copies link to the download. Before installing, KopiaLens checks that the installer is signed by KopiaLens.

When you click **Install and restart**, the app asks the update service for the installer's details, sending only its version and platform (`windows-x86_64`). If the service can't be reached, it asks GitHub instead.

## What's sent

**Update check:**

| Field | Example |
|---|---|
| KopiaLens version | `1.2.0` |
| Install type | `windows`, `linux`, `macos`, `docker` or `source` |
| Operating system | `windows`, `linux` or `macos` |

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

The service also records the **country** the request came from (worked out by Cloudflare), only when usage stats are on.

## What's never sent

- Anything from your backups: paths, file names, sizes, snapshot details
- Repository addresses, bucket names, passwords, keys or licence codes
- Your settings, user name or computer name
- How much you use a feature: no counts or times

## What's stored

- Your IP address is **not** stored.
- With usage stats on: one row per install with the fields above, the first and last day it checked in, and how many days it was opened. An install that hasn't checked in for 90 days is deleted.
- Daily totals: how many checks came in per version, install type, OS and licence, how many installs were opened, and how many used each feature.

## Revoked licences

Once a week, KopiaLens also fetches the list of licences that were refunded or misused, signed by the licence service. Without a licence, this sends nothing about you. With one, the same request is the weekly licence check (see [Licensing](licensing.md)). It runs even with both switches off, so a refunded licence stops working on every copy. When KopiaLens can't reach the service, nothing changes.

## Turning it off

- In the app: **Settings → Updates**.
- Docker or headless installs: set `KOPIALENS_NO_UPDATE_CHECK=1` and/or `KOPIALENS_NO_USAGE_STATS=1`.
