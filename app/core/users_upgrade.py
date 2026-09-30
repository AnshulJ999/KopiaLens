"""Data-folder step v2 -> v3 (): the shared password becomes the admin account."""

from . import dataupgrade, users

dataupgrade.register(from_version=2, fn=users.migrate_legacy, name="accounts",
                     description="move the shared password into users.json as the admin account")
