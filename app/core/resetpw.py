"""Local password reset: ``kopialens --reset-password`` (from source: ``python main.py --reset-password``).

For a forgotten sign-in password when the recovery key cannot help. It needs access to the
machine running KopiaLens, which is the proof of ownership. The server may keep running: it
re-reads users.json when the file changes.
"""

from __future__ import annotations

import getpass

from . import auth, users


def _ask() -> str | None:
    while True:
        new = getpass.getpass("New password: ")
        if len(new) < auth.MIN_PASSWORD:
            print(f"Use a password of at least {auth.MIN_PASSWORD} characters.")
            continue
        if getpass.getpass("Confirm new password: ") != new:
            print("The passwords don't match. Try again.")
            continue
        return new


def main() -> int:
    user = users.primary()
    if user is None:
        print("KopiaLens has no account yet. Open KopiaLens in a browser to create one.")
        return 1
    print(f"Set a new sign-in password for {user['username']}.")
    try:
        new = _ask()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was changed.")
        return 1
    users.set_password_hash(user["username"], auth.hash_password(new))
    print("Password changed. Every browser is signed out.")
    if user.get("unlocks_encryption"):
        # The encryption password cannot change without the old password or the recovery key.
        users.set_unlocks_encryption(user["username"], False)
        print("Your encryption password is unchanged. It is now separate from your sign-in password.")
    return 0
