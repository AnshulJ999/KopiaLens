"""Public keys this build trusts, and licence ids it refuses. Compiled into the program on
purpose: a loose file next to it could be swapped for someone's own key."""

PRODUCT = "kopialens"

# kid -> PASERK public key. m1 = master key (manual licences). Add the service key when it exists.
PUBLIC_KEYS = {
    "m1": "k4.public.CA5tbNuOoF-1TmwqggfVSQrY_jkvDJFKXjo39QwYtDo",
}

# Refunded or leaked licence ids (the issuing tool exports this list for each release).
BANNED_IDS: frozenset = frozenset()
