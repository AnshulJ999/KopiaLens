# Security

## Reporting a security problem

Please report it privately, not in a public issue, so a fix can ship before the problem is public:

- **GitHub:** the **Report a vulnerability** button on this repository's [Security tab](../../security/advisories/new)
- **Email:** support@kopialens.com

Include the KopiaLens version, how it's installed (desktop, Docker, Home Assistant), and the steps to reproduce. I reply within 7 days, often sooner.

A problem in kopia itself belongs to the [Kopia project](https://github.com/kopia/kopia/security).

## Supported versions

Only the latest release gets security fixes. The desktop app and the AppImage update themselves; Docker and Home Assistant update through their usual image and add-on updates.

## Security model

Read [ARCHITECTURE.md](ARCHITECTURE.md) to know more about how KopiaLens is built. Here are the parts that matter for security:

### Your backups

- KopiaLens reaches a repository only through the kopia program. Backups stay in Kopia's own format, encrypted by kopia with your repository password.
- KopiaLens doesn't store repository passwords or storage credentials in its own files, and never passes them on a command line. Kopia keeps the repository password in its connection file or your system's keychain.
- Nothing in a repository changes unless someone starts an action. Each one is previewed, confirmed and written to the audit log.

### Sign-in

- A KopiaLens account has its own password, stored as a salted PBKDF2-SHA256 hash (200,000 rounds).
- Wrong passwords cost an increasing wait. There is no lockout, so guessing can't lock the owner out.
- The session cookie is `HttpOnly` and `SameSite=Lax`, and `Secure` over HTTPS. You can sign out every other device at once.

More: [Sign-in and your account](docs/sign-in.md).

### Who can connect

| Install | Default |
|---|---|
| Desktop app | This computer only, plain HTTP on the loopback address |
| Docker, Home Assistant, servers | Your network, HTTPS with a certificate KopiaLens makes itself |

The setting can narrow this to this window only or to listed addresses, or widen it to anyone on the network. HTTPS can also use your own certificate or a trusted reverse proxy.

More: [Remote access and HTTPS](docs/remote-access.md).

### Scripts and AI agents

- Each script or agent gets its own API token, with a name, an expiry and a permission from Read-only to Full Access. KopiaLens stores only a SHA-256 hash of each token.
- Deleting anything, opening remote access, turning sign-in off and other risky requests wait for you to approve them in the app, whatever the token's permission. An approval covers one exact request and expires after an hour.
- Some things are never done with a token: setting a password, showing the recovery key, exporting accounts or keys, managing tokens and approving requests.

More: [API tokens](docs/api-tokens.md).

### Permissions

Four levels limit what KopiaLens itself may change, for the whole app or per repository: Read-only, Restores allowed, Non-destructive and Full Access. The stricter of the two wins. The server enforces them for the interface, the API and `klens`. Raising a level to Full Access asks for the KopiaLens password when sign-in is set up.

More: [Settings → Permissions](docs/settings.md#permissions).

### Data KopiaLens keeps

- **The index** holds file and folder names, sizes and dates from your snapshots, never file contents.
- **Encryption levels:** Off, Index (the setup default) or Full (the index plus history, logs, policy backups and Backblaze data).
- **How it's encrypted:** AES-256-GCM, with keys derived through HKDF from a data key. The data key is wrapped by a key made from your encryption password with Argon2id. A recovery key can unwrap it if the password is lost. The key stays in memory unless you choose to unlock automatically, which stores it in the system's credential store.
- **The audit log** is append-only and never rotated: every action, and whether a person, a token or an agent started it.

More: [Encryption](docs/encryption.md), [Logs](docs/logs.md).

### What leaves your computer

- A daily update check and anonymous usage numbers, both optional. Never file names, paths or contents ([Update checks and usage stats](docs/usage-stats.md)).
- An activated Pro licence checks in once a week and keeps working offline.
- Your backups go only to the storage you chose, sent by kopia.

### Updates and downloads

- Updates are signed, and the app checks the signature before installing one.
- Every release file has a SHA-256 checksum on the releases page.
- The Windows and macOS apps aren't code-signed, so the system warns on first open ([Why Windows warns you](https://kopialens.com/download/windows-warning)).
