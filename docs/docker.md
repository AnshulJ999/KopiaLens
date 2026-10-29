# Running KopiaLens in Docker

KopiaLens runs as a single container with kopia built in. You open it in a browser on port 7123, from the same machine or any other on your network.

## Quick start

1. Download [`compose.example.yml`](https://github.com/AnshulJ999/KopiaLens/blob/main/compose.example.yml) into a folder of your own and rename it `compose.yml`. This is the file:

   ```yaml
   # KopiaLens with Docker Compose. Save as compose.yml, change the time zone and paths, then: docker compose up -d
   services:
     kopialens:
       image: anshulj99/kopialens:latest
       container_name: kopialens
       hostname: kopialens
       restart: unless-stopped         # also how the Restart button in Settings works
       stop_grace_period: 30s
       ports:
         - "7123:7123"
       environment:
         TZ: "Europe/London"           # your time zone, for the nightly index time
         # PUID: "1000"                # run as this user and group (files in /config belong to them)
         # PGID: "1000"
       volumes:
         # Required: settings, the index and kopia's cache. Without it everything is lost on recreate.
         - ./kopialens:/config
         # Your repository, at the same path kopia uses for it.
         - /mnt/backups/kopia:/mnt/backups/kopia
         # Optional: backed-up folders, at the same paths as in your snapshots. Read-only is enough.
         - /home:/home:ro
         # Optional: snapshot mounts, visible on the host in this folder.
         - /mnt/kopialens-mounts:/mounts:shared
       # Optional: needed only for mounting snapshots.
       devices:
         - /dev/fuse:/dev/fuse
       cap_add:
         - SYS_ADMIN
       security_opt:
         - apparmor:unconfined
   ```

2. Change the time zone and the paths under `volumes` (see below).
3. Run `docker compose up -d` and open `https://<server address>:7123`.

KopiaLens serves HTTPS with a certificate it makes itself, so the browser warns about the certificate once. Accept it, or use your own certificate or a reverse proxy instead (see [Remote access and HTTPS](remote-access.md)). To serve plain HTTP, set `KOPIALENS_HTTPS=off`.

The first run shows setup: connect your repositories, choose a password, and start the first index (see [First-time setup](getting-started.md)). For the first 30 minutes after a start, setup is open to anyone who can reach the port. After that it asks for a **setup code**, printed in the container log (`docker logs kopialens`).

The image is on Docker Hub as `anshulj99/kopialens` and on GitHub as `ghcr.io/anshulj999/kopialens`, for amd64 and arm64. `latest` is the newest release; a version tag such as `1.0.0` stays on that release.

## Folders

| In the container | What it's for | Needed |
|---|---|---|
| `/config` | Settings, the index, kopia's cache and connection files | Yes |
| Your repository's path | The kopia repository, if it's a folder or a network share | For folder repositories |
| Your backed-up folders | Lets the Excluded tab compare the disk with your backups | Optional |
| `/mounts` | Mounted snapshots | Optional |

**Mount `/config` on a volume.** Without it the index, settings and sign-in are lost whenever the container is recreated, and KopiaLens warns you about this.

**Use the same paths as on the host.** kopia records each folder by the path it was backed up from. Mount your repository and your backed-up folders at the same paths inside the container (for example `/home:/home:ro`), so KopiaLens finds them where your snapshots say they are.

Cloud repositories (S3, B2, SFTP and the rest) need no folder: connect them in setup.

## Backing up from the container

KopiaLens can run your backups from the container (choose **KopiaLens** in **Who runs backups…**). It backs up only folders mounted into the container, so:

- **Mount each folder at its host path**, as above. kopia files a snapshot under the path it read, so `/home/me/photos` inside the container continues the host's `/home/me/photos` history.
- **Connect as the host's name.** kopia also files every snapshot under a `user@computer` name. Inside a container that would be `root@kopialens`, a separate history from the host's. When you connect the repository, open **More options** and fill **Connect as user** and **Connect as computer** with the name your existing snapshots use (it shows in the Snapshots tab, for example `me@my-server`). To fix an existing connection, KopiaLens offers **Connect as** in **Who runs backups…** when the names differ only in capitals. Otherwise, reconnect with the right name.
- **Read access.** The container's user (see below) must be able to read every folder it backs up. Restores also need write access.

**Docker Desktop on Windows can't continue Windows backups.** A Linux container sees `/data/photos`, not `D:\Photos`, so its snapshots start a new history. Back up Windows folders with the KopiaLens desktop app instead. Docker on Windows is fine for analysis.

Other computers can't be backed up from here: each computer runs its own backups (KopiaLens, KopiaUI or the kopia command line), and KopiaLens analyses all of them.

## Running as your own user

By default KopiaLens runs as root inside the container, like kopia's own image. To have the files in `/config` belong to your user instead, set `PUID` and `PGID` to that user's ids (`id -u` and `id -g` on the host). KopiaLens changes the owner of `/config` and `/mounts` to match on start.

kopia writes repository files that only their owner can read. So with `PUID` set, use the same user that writes your repository (the user kopia or KopiaUI backs up as). If kopia runs as root, for example in kopia's own Docker image, leave `PUID` unset. Your backed-up folders must be readable by that user too.

## Mounting snapshots

Mounting shows a snapshot as a read-only folder. In Docker this needs FUSE, which the example file enables with three settings: the `/dev/fuse` device, `SYS_ADMIN` and `apparmor:unconfined`. These are the same settings kopia's own Docker image uses.

Mounts appear inside the container under `/mounts`. To see them on the host, mount a host folder there with `:shared` (as in the example), then enter that host folder in **Settings → General → Mount folder on the host**, so KopiaLens shows host paths.

Without FUSE everything else works, and the Mount buttons say what is missing.

Docker Desktop (Windows and macOS) runs containers inside a hidden Linux machine, so mounts work inside the container but can't appear in Explorer or Finder.

## Restarting and updating

- **Restart…** at the bottom of Settings, next to Close, stops the container, and Docker starts it again through `restart: unless-stopped`.
- `docker compose stop` stops KopiaLens cleanly. An index pass that was running is picked up again later. The previous index stays usable.
- To update, pull or build the new image and run `docker compose up -d`. Your `/config` folder carries everything over.

## Who can reach it

Without sign-in, anyone who can reach the port can open KopiaLens. Sign-in is your choice: set it up in first-time setup or **Settings → Account**. To limit it to some addresses, set `KOPIALENS_ACCESS=addresses` and `KOPIALENS_ACCESS_ADDRESSES` (for example `192.168.1.0/24`), or choose **These addresses** in **Settings → Remote access**. Behind a reverse proxy, list the proxy under **Trusted proxy addresses**. Details: [Remote access and HTTPS](remote-access.md).

## Encryption in Docker

A container has no keychain to remember the encryption password. With encryption on, the index stays locked after each restart until someone enters the password, and the nightly index waits until then. If that's a problem, choose **Off** in setup, or give the container the password in a file so it unlocks on start:

```yaml
services:
  kopialens:
    environment:
      KOPIALENS_KEY_FILE: /run/secrets/kopialens_key
    secrets:
      - kopialens_key
secrets:
  kopialens_key:
    file: ./kopialens_key.txt
```

The file holds the encryption password (your KopiaLens password if you chose to use it for encryption too). Anyone who can read the file can unlock the index.

## Settings that differ in Docker

- **Browse…** shows KopiaLens's own folder list (folders inside the container), since there is no desktop to open a dialog on.
- **Open folder** and **Open in Explorer** buttons are hidden: there is no screen in the container.
- **Disk scanning** uses the KopiaLens scanner, which is off until you choose it in **Settings → Disk scanning**. Everything is Windows-only. To check the computers you back up, mount their shares and add them as places ([Disk scanning](disk-scanning.md#in-docker)). Turn off **Show this computer's drives** so the container's own folders don't show.
- **Update checks and usage stats** are on, as on the desktop. Set `KOPIALENS_NO_UPDATE_CHECK=1` and/or `KOPIALENS_NO_USAGE_STATS=1` to turn them off. Details: [Update checks and usage stats](usage-stats.md).
