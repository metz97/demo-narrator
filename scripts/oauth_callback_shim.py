"""Redirect an OAuth callback from one localhost port to another.

Some IdPs pin a client's redirect_uri to a fixed origin. The Hubexo staging IdP
only accepts http://localhost:4200/callback for the lca-tool client, but the app
now serves on :4201, so the browser lands on whatever else owns :4200. This shim
answers on the pinned port and 302s the code+state on to the real app origin,
which keeps the PKCE verifier (sessionStorage, per-origin) intact.

    python scripts/oauth_callback_shim.py            # 127.0.0.1:4200 -> localhost:4201

The browser must resolve the pinned host to this listener; see
DEMO_NARRATOR_BROWSER_ARGS in .env.example.
"""

from __future__ import annotations

import argparse
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _DualStackServer(ThreadingHTTPServer):
    """Serve IPv4 and IPv6 from one socket when the host is a wildcard."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        super().server_bind()


def _make_handler(target: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _redirect(self) -> None:
            dest = target.rstrip("/") + self.path
            self.send_response(302)
            self.send_header("Location", dest)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            print(f"302 {self.path} -> {dest}", file=sys.stderr, flush=True)

        do_GET = _redirect
        do_HEAD = _redirect

        def log_message(self, *_args: object) -> None:  # quiet; we log the redirect
            pass

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on")
    ap.add_argument("--port", type=int, default=4200, help="port the IdP redirects to")
    ap.add_argument("--target", default="http://localhost:4201", help="real app origin")
    args = ap.parse_args()

    try:
        srv = _DualStackServer((args.host, args.port), _make_handler(args.target))
    except OSError as exc:
        print(f"cannot listen on {args.host}:{args.port} — {exc.strerror}", file=sys.stderr)
        return 1
    print(f"callback shim: http://{args.host}:{args.port}/* -> {args.target}/*", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
