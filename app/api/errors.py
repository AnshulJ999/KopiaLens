"""Errors that API routes must re-raise untouched, so their dedicated handlers in ``main`` answer
(503 being indexed, 423 locked, 503 not indexed yet or not listed, 499 the client went away) instead of a route's catch-all 500."""

from ..core.sources import ListingFailed
from ..index.crypto import StoreLocked
from ..index.lanes import RequestCancelled
from ..index.schema import RepoOffline
from ..index.store import NoIndex

PASS_THROUGH = (RepoOffline, StoreLocked, NoIndex, RequestCancelled, ListingFailed)
