"""Entrypoint: run KopiaLens (uvicorn) on 127.0.0.1:7123, with a single-instance guard.

A 2nd instance (e.g. an auto-start-on-boot colliding with a manual/tray launch) would start its bg
worker and hit DuckDB "Conflicting lock" errors BEFORE failing to bind the port - messy (though the
DuckDB lock + port both prevent real damage). The guard exits cleanly first, and logs it to the
normal log file so a boot-time collision is identifiable. It does NOT open a browser."""
import socket
import sys

import uvicorn

from app.core.applog import get_logger, setup_logging
from config import GUARD_HOST, HOST, PORT


def _already_running(host: str, port: int) -> bool:
    """True if something is already listening on host:port (an existing KopiaLens instance)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


if __name__ == "__main__":
    setup_logging()
    log = get_logger("startup")
    if _already_running(GUARD_HOST, PORT):
        log.warning("single-instance guard: KopiaLens already listening on %s:%s - this instance is exiting", GUARD_HOST, PORT)
        print(f"KopiaLens already running on {GUARD_HOST}:{PORT} - exiting (single-instance guard).")
        sys.exit(0)
    log.info("starting KopiaLens on %s:%s", HOST, PORT)
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
