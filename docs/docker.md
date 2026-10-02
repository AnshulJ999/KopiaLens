# Running KopiaLens in Docker

KopiaLens runs as a single container with kopia built in. You open it in a browser on port 7123, from the same machine or any other on your network.

## Quick start

1. Copy `packaging/docker/compose.example.yml` to a folder of your own as `compose.yml`.
2. Change the paths under `volumes` (see below).
3. Run `docker compose up -d` and open `http://<server address>:7123`.

The first run shows setup: connect your repositories, choose a password, and start the first index (see [First-time setup](getting-started.md)). For the first 30 minutes after a start, setup is open to anyone who can reach the port. After that it asks for a **setup code**, printed in the container log (`docker logs kopialens`).

Until the image is published, build it from the KopiaLens folder:

```bash
docker build -f packaging/docker/Dockerfile -t kopialens:local .
```

## Folders

| In the container | What it's for | Needed |
|---|---|---|
| `/config` | Settings, the index, kopia's cache and connection files | Yes |
| Your repository's path | The kopia repository, if it's a folder or a network share | For folder repositories |
| Your backed-up folders | Lets the Excluded browser compare the disk with your backups | Optional |
| `/mounts` | Mounted snapshots | Optional |

**Mount `/config` on a volume.** Without it the index, settings and sign-in are lost whenever the container is recreated, and KopiaLens warns you about this.

**Use the same paths as on the host.** kopia records each folder by the path it was backed up from. Mount your repository and your backed-up folders at the same paths inside the container (for example `/home:/home:ro`), so KopiaLens finds them where your snapshots say they are.

Cloud repositories (S3, B2, SFTP and the rest) need no folder: connect them in setup.

## Running as your own user

By default KopiaLens runs as root inside the container, like kopia's own image. To have the files in `/config` belong to your user instead, set `PUID` and `PGID` to that user's ids (`id -u` and `id -g` on the host). KopiaLens changes the owner of `/config` and `/mounts` to match on start.

kopia writes repository files that only their owner can read. So with `PUID` set, use the same user that writes your repository (the user kopia or KopiaUI backs up as). If kopia runs as root, for example in kopia's own Docker image, leave `PUID` unset. Your backed-up folders must be readable by that user too.

## Mounting snapshots

Mounting shows a snapshot as a read-only folder. In Docker this needs FUSE, which the example file enables with three settings: the `/dev/fuse` device, `SYS_ADMIN` and `apparmor:unconfined`. These are the same settings kopia's own Docker image uses.

Mounts appear inside the container under `/mounts`. To see them on the host, mount a host folder there with `:shared` (as in the example), then enter that host folder in Settings, **Mount folder on the host**, so KopiaLens shows host paths.

Without FUSE everything else works, and the Mount buttons say what is missing.

Docker Desktop (Windows and macOS) runs containers inside a hidden Linux machine, so mounts work inside the container but can't appear in Explorer or Finder.

## Restarting and updating

- **Restart** in Settings stops the container, and Docker starts it again through `restart: unless-stopped`.
- `docker compose stop` stops KopiaLens cleanly. An index pass that was running is picked up again later; the previous index stays usable.
- To update, pull or build the new image and run `docker compose up -d`. Your `/config` folder carries everything over.

## Settings that differ in Docker

- **Browse…** shows KopiaLens's own folder list (folders inside the container), since there is no desktop to open a dialog on.
- **Open folder** and **Open in Explorer** buttons are hidden: there is no screen in the container.
- **Disk lookup** uses a folder scan. Everything (es.exe) is Windows-only.
