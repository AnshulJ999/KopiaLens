"""A reverse proxy that serves a KopiaLens server under a sub-path, the way Home Assistant ingress or
a proxy at ``https://host/kopialens/`` does: requests must start with the prefix, which is stripped
before forwarding. Anything without it answers 404, so a raw ``/api/`` or ``/static/`` URL anywhere in
the app fails the browser flow that used it.

Turned on for a whole browser-suite run with ``KOPIALENS_UI_PREFIX=/klprefix`` (_harness.Server)."""

from __future__ import annotations

import http.client
import http.server
import threading

_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection"}


class PrefixProxy:
    def __init__(self, prefix: str, target_port: int):
        self.prefix = prefix.rstrip("/")
        self.target_port = target_port
        self.misses: list[str] = []
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _forward(self):
                if self.path != proxy.prefix and not self.path.startswith(proxy.prefix + "/"):
                    proxy.misses.append(f"{self.command} {self.path}")
                    body = b"not under the prefix"
                    self.send_response(404)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                path = self.path[len(proxy.prefix):] or "/"
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
                conn = http.client.HTTPConnection("127.0.0.1", proxy.target_port, timeout=300)
                try:
                    conn.request(self.command, path, body=body, headers=headers)
                    resp = conn.getresponse()
                    data = resp.read()
                finally:
                    conn.close()
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in _HOP and k.lower() != "content-length":
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _forward

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
