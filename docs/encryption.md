# Encryption

KopiaLens keeps an index of your repositories on disk: every file and folder name, sizes and history. Encryption protects that index, so someone who copies the KopiaLens data folder can't read the names of your files.

This is separate from kopia's own encryption. Your kopia repository is always encrypted by kopia, whatever you choose here.

## Levels

| Level | What's encrypted |
|---|---|
| Off | Nothing. The index is stored as plain files. |
| Index (recommended) | The index: file and folder names, sizes and snapshot contents. |
| Full | The index, plus history, logs, policy backups and the Backblaze data. |

Change the level at any time in **Settings → Encryption → Change level…**. KopiaLens rewrites the stored data in the background, and progress shows in the jobs panel. Everything stays readable while it runs, and a failed rewrite can simply be started again.

## Turn on encryption

1. Open **Settings → Encryption** and select **Turn on…**.
2. Choose a level.
3. Choose the password. **Use my KopiaLens password** is ticked by default, so one password signs you in and unlocks encryption. Untick it to use a separate encryption password.
4. Choose whether to **unlock automatically on this computer** (see below).
5. Save the [recovery key](#the-recovery-key) that appears next. It's shown only once.

## One password or two

By default, your KopiaLens password also unlocks encryption:

- Signing in also unlocks the index.
- Changing the password in **Settings → Account** changes both.

To use two passwords, select **Use a separate password…** in **Settings → Encryption**. To go back to one, select **Use my sign-in password…**.

## Unlocking after a restart

The encryption key lives in memory. When KopiaLens restarts, the index is locked until someone enters the password, and KopiaLens shows an unlock box instead of your data. Scheduled indexing waits until the index is unlocked.

**Unlock automatically on this computer** stores the key in the operating system's credential store (Windows Credential Manager, macOS Keychain or the Linux secret service), so KopiaLens unlocks itself when it starts. This is convenient, but anyone who can sign in to that computer's user account can then read the index. Leave it off if that matters to you.

**Lock now** locks the index immediately, until someone enters the password.

## The recovery key

The recovery key is a long code that unlocks the index if you forget the password. Copy it or download it as a file, and store it somewhere safe, away from the computer running KopiaLens: a password manager works well.

With the recovery key you can:

- unlock the index: select **Use recovery key** on the unlock box.
- set a new password: select **Forgot password?** on the sign-in page (when your KopiaLens password also unlocks encryption).

**New recovery key…** in **Settings → Encryption** creates a new key. The old one stops working.

## Lost the password and the recovery key

Without either, the encrypted data can't be read, not even by KopiaLens. **Start over…** in **Settings → Encryption** (or **Lost the recovery key too?** on the unlock box) resets KopiaLens:

- you choose what happens to the encrypted files: **delete them permanently**, or **move them aside** into a new `encryption-reset-…` folder in the data folder in case the password turns up. **Settings → Encryption** shows moved files until you delete that folder.
- encryption is turned off.
- the index is rebuilt from your kopia repositories on the next index pass.

Your kopia repositories are never touched. Encrypted history, such as past index runs and the audit log at the Full level, can't be recovered.
