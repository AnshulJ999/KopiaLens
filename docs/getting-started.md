# First-time setup

The first time you open KopiaLens, it walks you through three steps: **Connect**, **Protect** and **Start**. Everything you choose here can be changed later in Settings.

## 1. Connect your repositories

KopiaLens looks for kopia connections already on this computer, including the ones KopiaUI and the kopia command line use. Tick the ones you want and select **Use this repository**. You can pick several at once.

To add a repository that isn't listed, choose one of:

- **Connect to an existing one**: pick the storage type (a folder, SFTP, S3, Backblaze B2, Google Cloud Storage, Azure, WebDAV, Rclone, Google Drive or a kopia server) and fill in its details.
- **Create a new repository**: the same form, and kopia creates a new, empty repository there. The storage location must be empty.

Each repository asks for its **repository password**. This is kopia's own password for the repository, not your KopiaLens password. kopia remembers it after the first connection, so you enter it once.

Once connected, each repository shows how many sources and snapshots it holds. You can rename it and give it a short name, used where space is tight.

### Repositories reached through a kopia server

If a repository is reached through a kopia server rather than directly, kopia doesn't allow the detailed reads KopiaLens needs for space analysis and cleanup. For full analysis, install KopiaLens on the computer that runs the kopia server, or connect to the storage directly (for example the folder, B2 bucket or S3 bucket the server uses).

## 2. Protect KopiaLens

- **Sign-in**: choose a username and a KopiaLens password. Anyone opening KopiaLens then has to sign in. See [Sign-in and your account](sign-in.md).
- **Encryption**: choose how KopiaLens stores its index on disk. **Index** (recommended) encrypts file and folder names; **Full** also encrypts history, logs and policy backups; **Off** stores plain files. By default your KopiaLens password unlocks encryption too. See [Encryption](encryption.md).
- **Remote access** (server installs only): the domain names you open KopiaLens from, if any. IP addresses and localhost always work.

If you turn encryption on, KopiaLens shows a **recovery key** next. Save it somewhere safe: it's the only way to unlock the index if you forget the password.

You can also **skip protection**. KopiaLens then opens without a password. On a server install, anyone who can reach its address can use it.

### Setup code on server installs

On a server or Docker install, setup is open for 30 minutes after KopiaLens starts. After that, KopiaLens asks for a one-time setup code, printed in its log (`docker logs <container>` in Docker). Restarting KopiaLens also reopens setup.

## 3. Build the index

KopiaLens reads your snapshots once to build its index. After that, it only reads what changed. You can keep using KopiaLens while it runs; with several repositories, they're indexed one after another.

- **Update automatically every day at**: keeps the index current with one update a day. Change the time, or turn it off, here or in Settings → Indexing.
- **Start indexing** builds the index now.
- **Skip for now** leaves it for later. If automatic updates are on, indexing starts at the next scheduled time; otherwise, start it with **Index** in the top bar.

A repository that hasn't been indexed yet shows a notice at the top of the page with a **Start indexing** button.
