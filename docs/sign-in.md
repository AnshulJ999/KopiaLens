# Sign-in and your account

KopiaLens can ask for a username and password before anyone opens it. Turn this on whenever other people or devices can reach KopiaLens, for example on a home server or in Docker.

Your **KopiaLens password** is separate from your **repository password**. The repository password belongs to kopia and unlocks your backups. The KopiaLens password only protects KopiaLens.

## Create an account

Open **Settings → Account** and select **Create account…**. Choose a username (`admin` is suggested) and a password of at least 8 characters. Sign-in turns on straight away, and the browser you used stays signed in.

### First-time setup on a server

When KopiaLens runs as a server (Docker, or any install other than the desktop app) and no account exists yet, anyone who can reach it could create one. To prevent that, setup is open for **30 minutes** after KopiaLens starts. After that, creating the account asks for a **setup code**.

The setup code is printed when KopiaLens starts. With Docker, find it with:

```
docker logs <container-name>
```

Look for the line that starts with `KopiaLens: Setup is open`. Restarting KopiaLens also reopens setup for another 30 minutes. The desktop app only accepts connections from the same computer, so it never asks for a code.

To create the account without the browser, set `KOPIALENS_PASSWORD` (and optionally `KOPIALENS_USERNAME`) in the environment before the first start. KopiaLens uses them only when no account exists yet.

## Sign in

The sign-in page asks for your username and password.

- **Keep me signed in**: when ticked, the browser stays signed in for the time set in **Settings → Account → Stay signed in for** (30 days by default). When unticked, you're signed out when the browser closes.
- If your KopiaLens password also unlocks encryption and the index is locked, the button reads **Sign in and unlock** and does both. See [Encryption](encryption.md).

After several wrong passwords, KopiaLens makes you wait a little longer before each new attempt, up to 30 seconds. It never locks the account.

## Manage your account

All of these are in **Settings → Account**.

| Setting | What it does |
|---|---|
| Sign-in | Turns the password requirement on or off. Turning it off keeps your account, so you can turn it back on without a new password. |
| Username | Changes the name you sign in with. This browser stays signed in. |
| Password | Changes your password. If the same password also unlocks encryption, both change together, and the dialog says so before you confirm. |
| This browser | Signs out this browser. |
| Other devices | Signs out every browser, including this one. Use it if a device is lost or you signed in somewhere you shouldn't have. |
| Stay signed in for | How long "Keep me signed in" lasts: 1 day to 1 year. A shorter time also signs out browsers that signed in longer ago. |

Changing your password also signs out every other browser.

## Forgot your password

On the sign-in page, select **Forgot password?**.

- **With a recovery key.** If your KopiaLens password also unlocks encryption, enter your [recovery key](encryption.md#the-recovery-key) and a new password. The new password replaces the old one for both signing in and encryption, and you're signed in.
- **On the computer running KopiaLens.** Anyone with access to that computer can set a new password:

  ```
  kopialens --reset-password
  ```

  When running from source, use `python main.py --reset-password`. KopiaLens can keep running while you do this. If your password also unlocked encryption, the encryption password stays as it was and becomes a separate password. You can join them again in **Settings → Encryption**.
