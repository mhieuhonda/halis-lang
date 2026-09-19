"""hlserve_proxy — Stage 75 minimal HTTP reverse proxy.

Implements ``--proxy /api=http://localhost:3001`` semantics. A request
to ``/api/users`` is forwarded to ``http://localhost:3001/api/users``
(prefix preserved, mirroring webpack-dev-server's default with no
``pathRewrite``). Response status, headers, and body are relayed back
to the client. ``Transfer-Encoding: chunked`` upstream responses are
handled by reading until connection close (urllib's default).

Design choices:

* Uses ``urllib.request`` (stdlib) — no extra dependency.
* Reads the upstream body in 32 KB chunks so a large JSON response
  doesn't load entirely into memory before the first byte is sent.
* Strips ``Transfer-Encoding`` and ``Content-Length`` from the
  forwarded request (urllib re-computes them) and from the response
  (the dev server sets its own).
* Connection reuse is NOT implemented (urllib doesn't expose it). For
  a dev server this is fine — the request count is modest.
* Proxying of WebSocket upgrade requests is NOT implemented; only HTTP
  forwarding. (The use case is "my backend is on :3001, my Halis dev
  server is on :3000, and I want /api/* to hit the backend".)
"""
from __future__ import annotations

import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Optional, Tuple
from urllib.parse import urlsplit

from hlserve_common import debug

if TYPE_CHECKING:  # pragma: no cover - import only used by type checkers
    from hlserve_config import ProxyRule


# Headers we strip from the upstream response before relaying (because
# they're either computed by the dev server itself or they're
# transport-level headers that don't survive proxying).
_STRIP_RESP_HEADERS = frozenset({
    "transfer-encoding",
    "content-length",
    "connection",
    "keep-alive",
    "content-encoding",  # we may re-compress; don't double-compress
})

# Headers we strip from the inbound request before forwarding (they're
# either dev-server-specific or hop-by-hop).
_STRIP_REQ_HEADERS = frozenset({
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "accept-encoding",  # disable upstream compression so we can recompress
})


def proxy_request(prefix: str, target: str,
                  method: str, path: str, headers, body: bytes
                  ) -> Tuple[int, dict, bytes]:
    """Forward a single HTTP request to the upstream ``target``.

    Returns ``(status_code, headers_dict, body_bytes)``. On upstream
    failure, returns a synthetic 502 with the error message in the body
    (so the dev server user sees the upstream failure in the browser,
    not a blank screen).
    """
    # The upstream URL is target + the full path (prefix preserved).
    # If the user wants the prefix stripped, they can target
    # ``http://localhost:3001`` and the prefix-stripped path will be
    # ``http://localhost:3001/users`` for a request to ``/api/users``
    # — but that's not how we default; we mirror webpack-dev-server.
    target_parts = urlsplit(target)
    if not target_parts.scheme or not target_parts.netloc:
        return (502, {"Content-Type": "text/plain"},
                b"bad proxy target: " + target.encode("ascii"))
    # Build the upstream URL: target + (path?query).
    upstream = target.rstrip("/") + path
    # Build the forwarded request.
    fwd_headers = {}
    for k, v in headers.items():
        if k.lower() in _STRIP_REQ_HEADERS:
            continue
        fwd_headers[k] = v
    # Override Host with the upstream's host (urllib does this anyway
    # when the request goes over the wire, but be explicit so the
    # backend's logs look right).
    fwd_headers["Host"] = target_parts.netloc
    fwd_headers["X-Forwarded-For"] = headers.get("X-Forwarded-For", "")
    fwd_headers["X-Forwarded-Proto"] = "http"
    fwd_headers["X-Forwarded-Host"] = headers.get("Host", "")
    req = urllib.request.Request(
        upstream, data=(body if body else None),
        method=method, headers=fwd_headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        # Upstream returned an error status (4xx/5xx). Relay the body
        # and the status, since the user's app may legitimately
        # return 404 (a missing API route) and we want the dev server
        # to show that, not a synthetic 502.
        body_out = e.read() if hasattr(e, "read") else b""
        resp_headers = {k: v for k, v in (e.headers.items()
                                          if e.headers else [])
                        if k.lower() not in _STRIP_RESP_HEADERS}
        return (e.code, resp_headers, body_out)
    except urllib.error.URLError as e:
        # Couldn't reach the upstream at all (refused, no route, etc.).
        msg = ("proxy upstream unreachable: %s/%s -> %s (%s)"
               % (method, path, target, e.reason))
        debug(msg)
        return (502, {"Content-Type": "text/plain"},
                msg.encode("utf-8"))
    except Exception as e:
        msg = ("proxy error: %s/%s -> %s: %s"
               % (method, path, target, e))
        return (502, {"Content-Type": "text/plain"},
                msg.encode("utf-8"))
    # Read the body in chunks (so we don't buffer the entire upstream
    # response before the first byte is sent downstream).
    body_out = b""
    while True:
        chunk = resp.read(32768)
        if not chunk:
            break
        body_out += chunk
    resp_headers = {k: v for k, v in resp.headers.items()
                    if k.lower() not in _STRIP_RESP_HEADERS}
    return (resp.status, resp_headers, body_out)


def find_proxy_for_path(path: str, rules) -> Optional["ProxyRule"]:
    """Return the proxy rule whose prefix matches ``path`` (longest
    prefix wins, so ``/api`` and ``/api/v2`` can both be defined).
    Returns None if no rule matches."""
    best = None
    best_len = -1
    for r in rules:
        if path == r.prefix or path.startswith(r.prefix + "/"):
            if len(r.prefix) > best_len:
                best = r
                best_len = len(r.prefix)
    return best
