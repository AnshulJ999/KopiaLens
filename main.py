"""Entrypoint: run KopiaLens (uvicorn) on 127.0.0.1:7123, with a single-instance guard.

A 2nd instance (e.g. an auto-start-on-boot colliding with a manual/tray launch) would start its bg
worker and hit DuckDB "Conflicting lock" errors BEFORE failing to bind the port - messy (though the
DuckDB lock + port both prevent real damage). The guard exits cleanly first, and logs it to the
normal log file so a boot-time collision is identifiable. It does NOT open a browser."""
import socket
import sys

import uvicorn

from app.core import auth
from app.core.applog import get_logger, setup_logging
from config import GUARD_HOST, PORT


def _already_running(host: str, port: int) -> bool:
    """True if something is already listening on host:port (an existing KopiaLens instance)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def _record_server() -> None:
    """``<data>/server.json``: this server's pid and port. The desktop app reads it to tell its own
    server from another program on the same port."""
    import json
    import os

    from config import DATA_DIR
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "server.json")
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "port": PORT}, fh)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def run() -> None:
    from app.core import tls
    tls.setup()
    setup_logging()
    log = get_logger("startup")
    log.info("HTTPS certificates: %s", tls.describe())
    if _already_running(GUARD_HOST, PORT):
        log.warning("port %s is already in use (another KopiaLens, or another program) - this instance is "
                    "exiting. Set another port in Settings > Advanced, or KOPIALENS_PORT.", PORT)
        print(f"Port {PORT} is already in use - exiting. Set another port in Settings > Advanced, "
              f"or KOPIALENS_PORT.")
        sys.exit(0)
    _record_server()
    from app.core import netaccess
    host = auth.bind_host()
    mode = netaccess.mode()
    if host != "127.0.0.1" and not auth.enabled() and mode != "addresses":
        log.warning("listening on %s with authentication DISABLED - anything that can reach "
                    "port %s can browse and delete from the backup repositories", host, PORT)
    tls = netaccess.tls_files()
    proxies = netaccess.trusted_proxies()
    opts: dict = {"proxy_headers": bool(proxies)}
    if proxies:
        opts["forwarded_allow_ips"] = ",".join(proxies)
    if tls:
        opts.update(ssl_certfile=tls[0], ssl_keyfile=tls[1])
    netaccess.STARTED.update(bind=host, mode=mode, https=bool(tls), cert=tls[0] if tls else None,
                             proxies=proxies)
    log.info("starting KopiaLens on %s://%s:%s (access %s, auth %s%s)", "https" if tls else "http",
             host, PORT, mode, "on" if auth.enabled() else "off",
             f", trusting proxies {', '.join(proxies)}" if proxies else "")
    from app.main import app  # the app object, not "app.main:app": import strings fail when compiled
    uvicorn.run(app, host=host, port=PORT, reload=False, **opts)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--reset-password"]:
        from app.core import resetpw
        sys.exit(resetpw.main())
    run()
