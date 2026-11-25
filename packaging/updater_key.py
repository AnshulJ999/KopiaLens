"""The updater signing key for release builds and signed notices.

The key file is private/updater.key (password-protected, gitignored). Its password can be remembered in
Windows Credential Manager, so a release asks for it only once on this PC.

    python packaging\\updater_key.py      check, save or forget the remembered password
"""
import getpass
import os
import subprocess
import sys
import tempfile

import keyring
import keyring.errors

KL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_FILE = os.path.join(KL_ROOT, "private", "updater.key")
SERVICE, ACCOUNT = "KopiaLens updater key", "updater.key"


def key_path() -> str | None:
    """TAURI_SIGNING_PRIVATE_KEY (a path or the key itself) when set, else private/updater.key."""
    return os.environ.get("TAURI_SIGNING_PRIVATE_KEY") or (KEY_FILE if os.path.isfile(KEY_FILE) else None)


def unlocks(password: str) -> bool:
    """True when the password opens the key: signs a scratch file with Tauri's signer."""
    key = key_path()
    if not key:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        probe = os.path.join(tmp, "probe.txt")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("probe")
        flag = "-f" if os.path.isfile(key) else "-k"
        env = {**os.environ, "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": password}
        env.pop("TAURI_SIGNING_PRIVATE_KEY", None)
        rc = subprocess.run(["cargo", "tauri", "signer", "sign", flag, key, probe], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            shell=sys.platform == "win32").returncode
        return rc == 0 and os.path.isfile(probe + ".sig")


def password() -> str | None:
    """The key's password: TAURI_SIGNING_PRIVATE_KEY_PASSWORD, else the remembered one, else asks
    (and offers to remember it). None when it doesn't open the key."""
    pw = os.environ.get("TAURI_SIGNING_PRIVATE_KEY_PASSWORD")
    if pw:
        return pw if unlocks(pw) else None
    saved = keyring.get_password(SERVICE, ACCOUNT)
    if saved:
        if unlocks(saved):
            return saved
        print("The remembered updater key password no longer opens the key.")
    pw = getpass.getpass("Updater key password: ")
    if not unlocks(pw):
        print("That password doesn't open the updater key.")
        return None
    if input("Remember it on this PC (Windows Credential Manager)? [y/N] ").strip().lower() == "y":
        keyring.set_password(SERVICE, ACCOUNT, pw)
        print("Remembered.")
    return pw


def forget() -> bool:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
        return True
    except keyring.errors.PasswordDeleteError:
        return False


def main() -> None:
    import questionary
    print(f"Key file: {key_path() or 'not found'}")
    print("Password remembered on this PC: " + ("yes" if keyring.get_password(SERVICE, ACCOUNT) else "no"))
    choice = questionary.select("Updater key", choices=[
        "Check the password (asks, and offers to remember it)", "Forget the remembered password", "Quit"]).ask()
    if choice and choice.startswith("Check"):
        print("The key opens." if password() else "Not checked.")
    elif choice and choice.startswith("Forget"):
        print("Forgotten." if forget() else "No password was remembered.")
    input("\nPress Enter to close.")


if __name__ == "__main__":
    main()
