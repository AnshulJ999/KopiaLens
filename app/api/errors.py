"""Errors that API routes must re-raise untouched, so their dedicated handlers in ``main`` answer
(503 being indexed, 423 locked, 503 not indexed yet) instead of a route's catch-all 500."""

from ..index.crypto import StoreLocked
from ..index.schema import RepoOffline
from ..index.store import NoIndex

PASS_THROUGH = (RepoOffline, StoreLocked, NoIndex)
