# Getting help

Everything below lives in the **?** menu at the top right of KopiaLens.

## Report a problem

**? → Report a problem** builds a diagnostics bundle, lets you check what's in it, and opens a pre-filled GitHub issue. Pro users can also email support from the same place.

Before reporting, check whether the same thing happens in KopiaUI or the `kopia` command. If it does, it's most likely a kopia problem, and the [kopia project](https://github.com/kopia/kopia/issues) is the right place for it.

## Diagnostics bundle

**? → Create diagnostics bundle** (also in **Settings → About**) collects what support needs to understand a problem:

- **Always included:** KopiaLens version, install type, operating system, memory and free disk, kopia's version, your licence (Free or Pro, its plan and when it ends) and each repository's type and last index.
- **Your choice:** the app log, the indexing logs, the action history (deletes, restores, prunes) and your settings.

**Anonymise** is on by default. It replaces folder paths, user and computer names, email addresses and network addresses with short codes such as `<path-3f2a91>`. The same folder gets the same code in every bundle from your install, so support can follow a problem across several bundles without seeing the real names.

A bundle never contains passwords, keys, licence codes or the encryption key. You see the whole bundle before you download it.

## Updates

**? → Check for updates** checks now. KopiaLens also checks once a day. See [Update checks and usage stats](usage-stats.md) for what that sends and how to turn it off.

When an update is out, **Update available** in the top bar opens its details.

- **Installed Windows app:** click **Install and restart**, or **Install update** in the tray menu. If an index or a job is running, KopiaLens waits until it finishes. It then closes, installs the update and opens again.
- **Portable copy, Docker and other installs:** click **Get it** to open the download page, then install it the way you installed KopiaLens.

If your licence covers an earlier major version (for example a lifetime licence for 2.x), KopiaLens tells you before you update to a version it doesn't cover, and points you to the newest version it does.

### Beta versions

To try new versions before they're released to everyone, set **Settings → Updates → Update channel** to **Beta**. KopiaLens then also offers beta versions (for example `1.3.0-beta.1`), which may have more bugs. Switching back to **Stable** keeps the version you have. You move back onto stable versions with the next stable release that is newer than it. Docker users pick the `beta` image tag instead of `latest`.

## Desktop app

In the desktop app, the **?** menu also has:

- **Zoom:** smaller, larger and back to 100%. Ctrl+minus, Ctrl+plus, Ctrl+0 and Ctrl+mouse wheel do the same.
- **Open in browser:** the same KopiaLens in your web browser. It isn't shown when **Settings → Remote access** is set to **This window only**.
