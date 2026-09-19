"""hlserve_server — Stage 75 HTTP server.

The HTTP layer that sits in front of the WebSocket HMR bus, the proxy,
the static file server, and the bundle. Replaces Stage 24's
``DevHTTPHandler`` with a richer ``HlsDevHTTPHandler`` that:

* Routes ``/ws`` to the WebSocket handshake (delegated to
  ``hlserve_hmr.HmrBus``).
* Routes ``/`` and ``/index.html`` to the bundle HTML (with the HMR
  overlay injected).
* Routes ``/static/*`` to the ``--public-dir`` directory (if set).
* Routes ``/source`` to the HLS source file (browser-visible code).
* Routes any path matching a ``--proxy`` rule to the proxy.
* Falls back to a static file from the bundle dir; if no file matches
  AND ``--history-fallback`` is on (default), serves ``index.html``
  (the SPA pattern — so client-side routes like ``/users/42`` work).
* Compresses responses with gzip when the client sends
  ``Accept-Encoding: gzip`` and the body is > 1024 bytes (avoids
  paying the gzip overhead on tiny responses).
* Sets ``Cache-Control: no-store`` on every response (the dev server
  is the source of truth; stale browser cache defeats HMR).
"""
from __future__ import annotations

import gzip
import http.server
import io
import os
import socketserver
import threading
import time

from hlserve_common import (
    debug,
    warn,
)
from hlserve_config import ServeConfig
from hlserve_hmr import (
    HmrBus,
    serve_client_loop,
    ws_handshake_response,
)
from hlserve_overlay import inject_overlay, inject_status_banner
from hlserve_proxy import proxy_request, find_proxy_for_path


# Content-type map (Stage 24 had 8 entries; Stage 75 adds woff/ttf/mjs
# for the modern webapp that ships fonts and ES modules).
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".mjs":  "application/javascript; charset=utf-8",
    ".wasm": "application/wasm",
    ".json": "application/json; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".svg":  "image/svg+xml",
    ".ico":  "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf":  "font/ttf",
    ".otf":  "font/otf",
    ".eot":  "application/vnd.ms-fontobject",
    ".map":  "application/json; charset=utf-8",
    ".webp": "image/webp",
    ".webmanifest": "application/manifest+json",
    ".xml":  "application/xml; charset=utf-8",
    ".pdf":  "application/pdf",
    ".csv":  "text/csv; charset=utf-8",
}


def guess_content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


class HlsDevHTTPHandler(http.server.BaseHTTPRequestHandler):
    """The Stage 75 HTTP handler.

    The class-level attributes (``config``, ``bus``, ``bundle_dir``,
    etc.) are populated by the orchestrator before the server starts.
    The handler is stateless per-request — every request reads the
    current state from these class attributes.
    """

    # Populated by the orchestrator (``hlserve_cli.main``).
    config: ServeConfig = ServeConfig()  # type: ignore[assignment]
    bus: HmrBus = HmrBus()                # type: ignore[assignment]
    bundle_dir: str = "."
    bundle_base: str = "out"
    input_hls: str = "examples/hello.hls"
    last_compile_ok: bool = True
    last_error_message: str = ""

    # Override the default protocol_version for HTTP/1.1 keep-alive
    # (Stage 24 used the default HTTP/1.0; Stage 75 wants keep-alive
    # so the WebSocket upgrade handshake and the HMR client both
    # reuse the same TCP connection pool).
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_message(self, fmt, *args):
        # Stage 24: "[hls serve] <addr> - <message>"
        # Stage 75: same prefix, but routed through the structured
        # logger so timestamps + colors are consistent.
        msg = fmt % args
        # Don't spam the log with /ws upgrades or /events polls.
        if " /ws " in msg or " /events " in msg:
            return
        debug("%s %s" % (self.address_string(), msg))

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        # 1. WebSocket upgrade?
        if (path == "/ws"
                and self.headers.get("Upgrade", "").lower() == "websocket"):
            return self._handle_ws()
        # 2. Proxy?
        rule = find_proxy_for_path(path, self.config.proxies)
        if rule is not None:
            return self._handle_proxy(rule)
        # 3. Index HTML.
        if path in ("/", "/index.html"):
            return self._serve_html()
        # 4. Source view.
        if path == "/source":
            return self._serve_source()
        # 5. Static asset from the public dir.
        if self.config.public_dir and path.startswith("/static/"):
            return self._serve_public(path[len("/static/"):])
        # 6. File from the bundle dir.
        served = self._serve_bundle_file(path)
        if served:
            return
        # 7. SPA history fallback (if enabled).
        if self.config.history_fallback and self._looks_like_route(path):
            return self._serve_html()
        # 8. 404.
        return self._send_text(404, "text/plain; charset=utf-8",
                                "404: %s not found\n" % path)

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html_path = os.path.join(self.bundle_dir,
                                     self.bundle_base + ".html")
            if not os.path.isfile(html_path):
                return self._send_text(404, "text/plain", "404\n")
            return self._send_head(200, "text/html; charset=utf-8",
                                     os.path.getsize(html_path))
        # For other paths, just do a GET and discard the body.
        # (BaseHTTPRequestHandler doesn't separate HEAD cleanly.)
        return self.do_GET()

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_ws(self):
        """Perform the WebSocket handshake and start the per-client
        read loop in a separate thread."""
        ws_key = self.headers.get("Sec-WebSocket-Key", "")
        if not ws_key:
            return self._send_text(400, "text/plain",
                                    "missing Sec-WebSocket-Key\n")
        # Send the 101 Switching Protocols response.
        resp = ws_handshake_response(ws_key)
        try:
            self.wfile.write(resp)
            self.wfile.flush()
        except (BrokenPipeError, OSError):
            return
        # Hand off the raw socket to the HMR bus. The socket is
        # self.request (for a ThreadingTCPServer, it's a plain socket
        # once the request line is consumed). We need to read any
        # remaining buffered bytes — but BaseHTTPRequestHandler reads
        # the request fully before calling do_GET, so the socket is
        # at the start of the WS frames.
        client = self.bus.subscribe(self.request, self.client_address)
        # Run the read loop in a thread so this handler can return
        # (the BaseHTTPRequestHandler will close self.wfile, but the
        # underlying socket stays open as long as we hold a reference).
        t = threading.Thread(
            target=serve_client_loop,
            args=(self.bus, client),
            daemon=True,
            name="hlserve-ws-%d" % client.id,
        )
        t.start()
        # Hold this handler thread alive until the client disconnects.
        # This is a hack — we sleep until the client's socket is gone.
        # The alternative is to NOT use ThreadingHTTPServer for WS,
        # but the cost of holding the thread is negligible for a dev
        # server.
        try:
            while client.alive:
                time.sleep(0.5)
        except (KeyboardInterrupt, OSError):
            pass

    def _handle_proxy(self, rule):
        """Forward the request to the upstream target."""
        body = b""
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                body = self.rfile.read(int(cl))
            except (OSError, ValueError):
                body = b""
        # Read the request headers as a plain dict.
        req_headers = {k: v for k, v in self.headers.items()}
        status, resp_headers, resp_body = proxy_request(
            rule.prefix, rule.target, self.command, self.path,
            req_headers, body)
        # Send the response.
        self.send_response(status)
        for k, v in resp_headers.items():
            self.send_header(k, v)
        # Always set Cache-Control: no-store (dev server).
        self.send_header("Cache-Control", "no-store")
        # Don't compress proxied responses — the upstream may have
        # already compressed (we stripped Accept-Encoding so it didn't,
        # but be defensive).
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp_body)

    def _serve_html(self):
        """Serve the bundle HTML with the overlay injected."""
        html_path = os.path.join(self.bundle_dir,
                                 self.bundle_base + ".html")
        if not os.path.isfile(html_path):
            return self._send_text(
                404, "text/plain; charset=utf-8",
                "404: bundle .html not found. Has the initial compile "
                "completed?\n")
        with open(html_path, "rb") as f:
            data = f.read()
        # Inject the overlay (unless disabled).
        data = inject_overlay(data, overlay_enabled=self.config.overlay)
        # Inject a status banner if the last compile failed.
        if not self.last_compile_ok and self.last_error_message:
            data = inject_status_banner(data, self.last_error_message)
        # Compress + send.
        self._send_compressed(200, "text/html; charset=utf-8", data)

    def _serve_source(self):
        """Serve the input .hls source as plain text (browser viewer)."""
        full = self.input_hls
        if not os.path.isfile(full):
            return self._send_text(404, "text/plain; charset=utf-8",
                                    "404: source not found\n")
        with open(full, "rb") as f:
            data = f.read()
        self._send_compressed(200, "text/plain; charset=utf-8", data)

    def _serve_bundle_file(self, path: str) -> bool:
        """Try to serve ``path`` from the bundle dir. Returns True if
        a file was served (or 403/404 returned); False if no file
        matched and the caller should try the next route."""
        # Strip leading /.
        rel = path.lstrip("/")
        if not rel:
            return False
        # Disallow path traversal (literal ".." check).
        if ".." in rel.split("/"):
            self._send_text(400, "text/plain", "400: bad path\n")
            return True
        full = os.path.join(self.bundle_dir, rel)
        # Stage 27 perfection (deep-scan-18 BUG-11): resolve realpaths
        # to defeat symlink traversal. A symlink inside bundle_dir
        # pointing outside (e.g. bundle_dir/foo -> /etc) must NOT be
        # followed.
        try:
            real_full = os.path.realpath(full)
            real_bundle = os.path.realpath(self.bundle_dir)
        except OSError:
            return False
        if (real_full != real_bundle
                and not real_full.startswith(real_bundle + os.sep)):
            self._send_text(403, "text/plain; charset=utf-8",
                            "403: path resolves outside bundle dir\n")
            return True
        if not os.path.isfile(real_full):
            return False
        with open(real_full, "rb") as f:
            data = f.read()
        ct = guess_content_type(rel)
        self._send_compressed(200, ct, data)
        return True

    def _serve_public(self, rel: str):
        """Serve a file from the public static dir."""
        if not self.config.public_dir:
            return self._send_text(404, "text/plain", "404\n")
        if ".." in rel.split("/"):
            return self._send_text(400, "text/plain", "400: bad path\n")
        full = os.path.join(self.config.public_dir, rel)
        try:
            real_full = os.path.realpath(full)
            real_pub = os.path.realpath(self.config.public_dir)
        except OSError:
            return self._send_text(404, "text/plain", "404\n")
        if (real_full != real_pub
                and not real_full.startswith(real_pub + os.sep)):
            return self._send_text(403, "text/plain; charset=utf-8",
                                    "403: outside public dir\n")
        if not os.path.isfile(real_full):
            return self._send_text(404, "text/plain; charset=utf-8",
                                    "404: %s not found\n" % rel)
        with open(real_full, "rb") as f:
            data = f.read()
        self._send_compressed(200, guess_content_type(rel), data)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _looks_like_route(self, path: str) -> bool:
        """Heuristic: a path is a client-side route if it has no file
        extension (e.g. ``/users/42``) AND doesn't start with ``/ws``,
        ``/static``, ``/source``. This is what enables SPA history
        fallback — a deep link in a client-side-routed app gets
        index.html instead of a 404."""
        if path.startswith(("/ws", "/static", "/source")):
            return False
        last = path.rsplit("/", 1)[-1]
        if "." in last:
            # Looks like a filename (e.g. /bundle.js).
            return False
        return True

    def _send_text(self, status: int, content_type: str, body: str) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self._send_compressed(status, content_type, data)

    def _send_compressed(self, status: int, content_type: str,
                         data: bytes) -> None:
        """Send a response, gzipping the body if the client supports
        it AND the body is > 1024 bytes (avoid gzip overhead on small
        responses)."""
        accept_enc = self.headers.get("Accept-Encoding", "")
        use_gzip = (self.config.compress
                    and "gzip" in accept_enc.lower()
                    and len(data) > 1024
                    and not content_type.startswith("application/wasm"))
        # Note: we do NOT gzip application/wasm — the JS glue already
        # decodes the wasm via fetch() and ArrayBuffer; double-
        # compression of a binary asset hurts dev server latency.
        if use_gzip:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb",
                                compresslevel=6, mtime=0) as gz:
                gz.write(data)
            data = buf.getvalue()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        else:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_head(self, status: int, content_type: str,
                   content_length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


class HlsDevServer(socketserver.ThreadingTCPServer):
    """Stage 75 threaded TCP server. Allows address reuse (so a
    restart doesn't TIME_WAIT-block the port) and daemonises every
    request thread (so Ctrl+C exits cleanly)."""
    allow_reuse_address = True
    daemon_threads = True


def make_server(config: ServeConfig, bus: HmrBus, bundle_dir: str,
                bundle_base: str, input_hls: str) -> HlsDevServer:
    """Build a configured ``HlsDevServer`` ready to ``serve_forever()``.

    Side-effect: populates ``HlsDevHTTPHandler``'s class-level state
    (config, bus, bundle_dir, bundle_base, input_hls). This is the
    same pattern as Stage 24 — the http.server framework requires
    handler classes, not instances.
    """
    HlsDevHTTPHandler.config = config
    HlsDevHTTPHandler.bus = bus
    HlsDevHTTPHandler.bundle_dir = bundle_dir
    HlsDevHTTPHandler.bundle_base = bundle_base
    HlsDevHTTPHandler.input_hls = input_hls
    HlsDevHTTPHandler.last_compile_ok = True
    HlsDevHTTPHandler.last_error_message = ""
    server = HlsDevServer((config.host, config.port), HlsDevHTTPHandler)
    # If HTTPS, wrap the socket with TLS.
    if config.https:
        from hlserve_tls import (
            generate_self_signed_cert, make_ssl_context)
        pair = generate_self_signed_cert()
        if pair is not None:
            ctx = make_ssl_context(pair[0], pair[1])
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            server._hlserve_cert_pair = pair  # type: ignore[attr-defined]
        else:
            # cryptography missing; fall back to plain HTTP.
            config.https = False
            warn("--https requested but TLS could not be enabled; "
                 "serving plain HTTP")
    return server
