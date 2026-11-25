"""Licence result model."""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class State(str, Enum):
    NONE = "none"
    INVALID = "invalid"
    BANNED = "banned"
    EXPIRED = "expired"
    NOT_COVERED = "not_covered"
    ACTIVE = "active"


# Preference when several stored licences are found: the most useful one wins.
RANK = {
    State.ACTIVE: 5,
    State.NOT_COVERED: 4,
    State.EXPIRED: 3,
    State.BANNED: 2,
    State.INVALID: 1,
    State.NONE: 0,
}


@dataclass(frozen=True)
class Licence:
    state: State
    reason: str = ""
    id: str = ""
    name: str = ""
    email: str = ""
    plan: str = ""
    capabilities: frozenset = frozenset()
    issued: datetime | None = None
    expires: datetime | None = None
    updates_through: int | None = None
    renewable: bool = False
    source: str = ""
    renew_due: bool = False
    origin: str = ""
    token: str = field(default="", repr=False)

    def has(self, capability: str) -> bool:
        """True only for an active licence granting the capability. Never raises."""
        try:
            return self.state is State.ACTIVE and (
                capability in self.capabilities or "*" in self.capabilities
            )
        except Exception:
            return False

    @property
    def licensee(self) -> str:
        if not self.name:
            return ""
        return f"{self.name} <{self.email}>" if self.email else self.name


NO_LICENCE = Licence(State.NONE)
