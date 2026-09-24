"""Data-folder step v1 -> v2 ( +): the encryption boolean becomes a level."""

from . import dataupgrade, filecrypto

dataupgrade.register(from_version=1, fn=filecrypto.upgrade_step, name="encryption-level",
                     description="fold the old encryption boolean into encryption_level")
