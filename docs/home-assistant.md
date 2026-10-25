# Home Assistant

KopiaLens runs as a Home Assistant add-on, so you can manage the kopia repositories that hold your Home Assistant backups, or any others, from inside Home Assistant. It's the same KopiaLens as the Docker image, packaged for Home Assistant.

## Install the add-on

1. In Home Assistant, open **Settings → Add-ons → Add-on store**.
2. Open the **⋮** menu, select **Repositories**, and add the KopiaLens add-on repository.
3. Find **KopiaLens** in the store and select **Install**.
4. Select **Start**. Turn on **Show in sidebar** if you'd like it there.

The add-on runs on 64-bit Intel and AMD (`amd64`) and 64-bit ARM (`aarch64`) systems.

## Open it

**From the sidebar.** Open **KopiaLens** in the Home Assistant sidebar. Home Assistant signs you in, so KopiaLens doesn't ask again. Only Home Assistant administrators see it.

**On its own port.** To open KopiaLens from another device without Home Assistant, enter a port under **Network** in the add-on's **Configuration** tab, for example `7123`. Then open `http://<home-assistant-address>:7123`. This address doesn't go through Home Assistant's sign-in. If you set up KopiaLens's own sign-in in first-time setup, it asks for that. Without it, anyone who can reach the port can open KopiaLens ([Sign-in and your account](sign-in.md)).

The port is off until you enter a number.

## Your repositories

KopiaLens can reach these Home Assistant folders:

| Folder | Access |
|---|---|
| `/share` | Read and write |
| `/media` | Read and write |
| `/backup` | Read and write |
| `/homeassistant` | Read only |

- A repository on a NAS works when its network share is added in Home Assistant under **Settings → System → Storage**. It then appears under `/share`, `/media` or `/backup`.
- To check what your other computers leave out of their backups, add their shares the same way, then add each one as a place in **Settings → Disk scanning** ([Check other places](disk-scanning.md#in-home-assistant)).
- Cloud repositories (S3, B2, SFTP and the others) need no folder.

Add repositories in first-time setup or in **Settings → Repositories** ([Repositories](repositories.md)).

## Options

The add-on's **Configuration** tab has these options:

| Option | What it does |
|---|---|
| **Computer name for backups** | The computer name in `user@computer` for snapshots made here. `homeassistant` by default |
| **User name for backups** | The user name. `kopialens` by default |
| **Snapshot mounting** | Lets KopiaLens open a snapshot as a folder. Off by default |

Kopia names every snapshot `user@computer`. Fixed names keep one history for each folder, even when you reinstall the add-on.

**Snapshot mounting** needs extra permissions (FUSE and system administration rights). That is why Home Assistant shows a lower security rating for this add-on. KopiaLens mounts snapshots only while the option is on.

## Home Assistant backups

Home Assistant's own backups include KopiaLens's settings and repository connections, but not its index. The index can be large, and KopiaLens rebuilds it from your repositories after a restore.

## What's different from other installs

- Through the sidebar, Home Assistant keeps you signed in, so **Sign out** isn't shown there. **Sign out all devices…** in **Settings → Account** still signs out every browser that uses the direct port.
- KopiaLens's own HTTPS is off. Through the sidebar, Home Assistant's own address and HTTPS apply. The direct port uses plain HTTP, so use it only on your home network.
- The data folder is the add-on's own `/data` folder.

## Related

- [Running in Docker](docker.md)
- [Remote access and HTTPS](remote-access.md)
- [Repositories](repositories.md)
