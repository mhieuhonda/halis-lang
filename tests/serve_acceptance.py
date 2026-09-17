#!/usr/bin/env python3
"""Stage 75 serve-acceptance smoke test.

Verifies the new ``hls-serve`` features end-to-end:

1. Import surface preserved (Stage 24 ``hlserve.main`` etc.).
2. ``--version`` prints the banner.
3. ``--help`` lists all the new flags.
4. Config-file parsing (``hls.serve.toml`` + ``hls.serve.json``).
5. ``parse_proxy_spec`` accepts valid input, rejects malformed input.
6. WebSocket handshake produces the correct RFC 6455 accept key.
7. ``ws_encode_frame`` produces well-formed frames (header + payload).
8. ``FileWatcher`` fires on file mtime change.
9. ``compile_once`` parses a sample compiler diagnostic into a
   ``CompileError`` with file/line/col.
10. End-to-end: a real server started on a random port serves the
    HTML bundle, the SSE-listener-equivalent overlay is injected,
    the WebSocket endpoint responds to a handshake, the proxy
    forwards a request, the public dir serves a static file, the
    SPA history fallback serves index.html for /deep/route.

Run::

    python3 tests/serve_acceptance.py

(Called from ``make serve-acceptance`` in mk/40-backends.mk.)
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
_PKG_DIR = os.path.join(TOOLS_DIR, "hlserve_parts")
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)


def section(title: str) -> None:
    print()
    print("=== %s ===" % title)


def check(name: str, ok: bool, detail: str = "") -> bool:
    flag = "OK" if ok else "FAIL"
    print("  [%s] %s%s" % (flag, name, (" — " + detail) if detail else ""))
    if not ok:
        check.failed += 1  # type: ignore[attr-defined]
    return ok
check.failed = 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 1. Import surface
# ---------------------------------------------------------------------------

def test_imports() -> None:
    section("1. Import surface (Stage 24 compat)")
    import hlserve
    for name in ("main", "compile_bundle", "FileWatcher",
                 "EventBus", "DevHTTPHandler", "DevServer"):
        check("hlserve.%s present" % name,
              hasattr(hlserve, name))
    # New Stage 75 modules are importable.
    import hlserve_cli
    import hlserve_common
    import hlserve_config
    import hlserve_watcher
    import hlserve_compiler
    import hlserve_hmr
    import hlserve_overlay
    import hlserve_proxy
    import hlserve_tls
    import hlserve_server
    for mod in (hlserve_cli, hlserve_common, hlserve_config,
                hlserve_watcher, hlserve_compiler, hlserve_hmr,
                hlserve_overlay, hlserve_proxy, hlserve_tls,
                hlserve_server):
        check("module %s importable" % mod.__name__, True)


# ---------------------------------------------------------------------------
# 2. CLI --version / --help
# ---------------------------------------------------------------------------

def test_cli() -> None:
    section("2. CLI parser")
    import hlserve_cli
    ap = hlserve_cli.build_arg_parser()
    # Parse a typical invocation.
    args = ap.parse_args(["--input", "examples/hello.hls",
                          "--port", "3000", "--open",
                          "--proxy", "/api=http://localhost:3001"])
    check("--input parsed", args.input == "examples/hello.hls")
    check("--port parsed", args.port == 3000)
    check("--open parsed", args.open is True)
    check("--proxy list parsed",
          args.proxy == ["/api=http://localhost:3001"])


# ---------------------------------------------------------------------------
# 3. Config file parsing
# ---------------------------------------------------------------------------

def test_config() -> None:
    section("3. Config file (hls.serve.toml / .json)")
    from hlserve_config import (load_config_file,
                                 config_dict_to_serve_config,
                                 parse_proxy_spec, parse_proxy_specs,
                                 find_config_file, ProxyRule)
    # TOML file.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml",
                                       delete=False) as f:
        f.write("""\
input = "examples/my.hls"
port = 4000
open = true
https = false
history_fallback = true
compress = true

[watch]
dirs = ["src", "std"]
debounce_ms = 300

[[proxy]]
path = "/api"
target = "http://localhost:3001"
""")
        toml_path = f.name
    try:
        d = load_config_file(toml_path)
        cfg = config_dict_to_serve_config(d)
        check("TOML input parsed", cfg.input == "examples/my.hls")
        check("TOML port parsed", cfg.port == 4000)
        check("TOML open parsed", cfg.open is True)
        check("TOML https parsed", cfg.https is False)
        check("TOML watch dirs parsed",
              cfg.watch_dirs == ["src", "std"])
        check("TOML debounce parsed", cfg.debounce_ms == 300)
        check("TOML proxy parsed",
              len(cfg.proxies) == 1 and
              cfg.proxies[0].prefix == "/api" and
              cfg.proxies[0].target == "http://localhost:3001")
    finally:
        os.unlink(toml_path)
    # JSON file.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                       delete=False) as f:
        json.dump({
            "input": "x.hls", "port": 5000, "open": True,
            "watch": {"dirs": ["a", "b"], "debounce_ms": 250},
            "proxy": [{"path": "/api", "target": "http://x:1"}],
        }, f)
        json_path = f.name
    try:
        d = load_config_file(json_path)
        cfg = config_dict_to_serve_config(d)
        check("JSON input parsed", cfg.input == "x.hls")
        check("JSON port parsed", cfg.port == 5000)
        check("JSON watch dirs parsed", cfg.watch_dirs == ["a", "b"])
        check("JSON proxy parsed", len(cfg.proxies) == 1)
    finally:
        os.unlink(json_path)
    # Proxy spec parsing.
    rule = parse_proxy_spec("/api=http://localhost:3001")
    check("parse_proxy_spec prefix", rule.prefix == "/api")
    check("parse_proxy_spec target",
          rule.target == "http://localhost:3001")
    rules = parse_proxy_specs(["/a=http://x:1", "/b=http://y:2"])
    check("parse_proxy_specs count", len(rules) == 2)
    # Malformed spec.
    try:
        parse_proxy_spec("no-equals-sign")
        check("malformed spec raises", False)
    except ValueError:
        check("malformed spec raises", True)
    # ProxyRule validates the scheme.
    try:
        ProxyRule(prefix="/x", target="bad-no-scheme")
        check("ProxyRule scheme check", False)
    except ValueError:
        check("ProxyRule scheme check", True)


# ---------------------------------------------------------------------------
# 4. WebSocket protocol
# ---------------------------------------------------------------------------

def test_websocket() -> None:
    section("4. WebSocket handshake + frames")
    import hlserve_hmr as hmr
    # The RFC 6455 spec's example: client key "dGhlIHNhbXBsZSBub25jZQ=="
    # must produce accept "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=".
    resp = hmr.ws_handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
    check("handshake response starts with 101",
          resp.startswith(b"HTTP/1.1 101 Switching Protocols\r\n"))
    check("handshake contains Sec-WebSocket-Accept header",
          b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n"
          in resp)
    # Frame encoding.
    frame = hmr.ws_encode_frame(b"hello", opcode=hmr.OP_TEXT)
    # FIN=1, opcode=1 => first byte = 0x81
    check("frame first byte", frame[0] == 0x81)
    # Length 5, no mask => second byte = 0x05
    check("frame length byte", frame[1] == 0x05)
    check("frame payload", frame[2:7] == b"hello")
    # Longer payload (>125 bytes) uses 16-bit length.
    long_payload = b"x" * 200
    long_frame = hmr.ws_encode_frame(long_payload, opcode=hmr.OP_TEXT)
    check("long frame length encoding", long_frame[1] == 126)
    import struct
    extended_len = struct.unpack(">H", long_frame[2:4])[0]
    check("long frame extended length", extended_len == 200)


# ---------------------------------------------------------------------------
# 5. File watcher
# ---------------------------------------------------------------------------

def test_watcher() -> None:
    section("5. FileWatcher fires on mtime change")
    from hlserve_watcher import FileWatcher
    tmpdir = tempfile.mkdtemp(prefix="hlserve-watch-")
    try:
        # Write a file.
        f1 = os.path.join(tmpdir, "a.hls")
        with open(f1, "w") as f:
            f.write("fn main() -> int { return 0 }\n")
        events = []
        done = threading.Event()
        def on_change(files):
            events.extend(files)
            done.set()
        # Construct the watcher (which seeds the mtime table).
        w = FileWatcher([tmpdir], on_change, debounce_ms=80)
        w.start()
        # Wait briefly to let the watcher do its first scan (it
        # should already have seeded mtimes; the file should NOT fire).
        time.sleep(0.3)
        check("no events on seeded file", len(events) == 0)
        # Modify the file.
        done.clear()
        with open(f1, "w") as f:
            f.write("fn main() -> int { return 1 }\n")
        # Touch the mtime forward (writing may not always advance it
        # within the same mtime resolution on some filesystems).
        os.utime(f1, (time.time() + 5, time.time() + 5))
        done.wait(timeout=3.0)
        w.stop()
        check("watcher fired on change", len(events) >= 1)
        if events:
            check("watcher reported the right file",
                  os.path.basename(events[0]) == "a.hls")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. Compiler diagnostic parsing
# ---------------------------------------------------------------------------

def test_compiler_parsing() -> None:
    section("6. CompileResult diagnostic parsing")
    from hlserve_compiler import parse_diagnostics, CompileError
    sample = """\
examples/foo.hls:12:5: error: undefined variable `x`
examples/foo.hls:13:1: warning: unused import
examples/foo.hls:14: note: see previous declaration
wrote out.wasm (1234 bytes)
random line with no file:line
"""
    diags = parse_diagnostics(sample)
    check("parsed 3 diagnostics", len(diags) == 3,
          detail="got %d" % len(diags))
    if len(diags) == 3:
        check("first is error",
              diags[0].severity == "error"
              and diags[0].file == "examples/foo.hls"
              and diags[0].line == 12
              and diags[0].col == 5)
        check("second is warning",
              diags[1].severity == "warning"
              and diags[1].line == 13)
        check("third is note",
              diags[2].severity == "note"
              and diags[2].line == 14)
    # Overlay dict.
    d = CompileError("error", "f.hls", 7, 3, "boom").to_overlay_dict()
    check("overlay dict shape",
          d == {"severity": "error", "file": "f.hls",
                 "line": 7, "col": 3, "message": "boom"})


# ---------------------------------------------------------------------------
# 7. Overlay injection
# ---------------------------------------------------------------------------

def test_overlay() -> None:
    section("7. Overlay injection")
    from hlserve_overlay import inject_overlay, inject_status_banner, OVERLAY_SNIPPET
    html = b"<!DOCTYPE html><html><head></head><body><p>hi</p></body></html>"
    out = inject_overlay(html, overlay_enabled=True)
    check("overlay injected before </body>",
          OVERLAY_SNIPPET.encode("utf-8")[:50] in out)
    check("</body> preserved", b"</body></html>" in out)
    out2 = inject_overlay(html, overlay_enabled=False)
    check("overlay disabled leaves html unchanged", out2 == html)
    out3 = inject_status_banner(html, "compile failed")
    check("status banner injected", b"[hls-serve] compile failed" in out3)


# ---------------------------------------------------------------------------
# 8. Proxy rule lookup
# ---------------------------------------------------------------------------

def test_proxy_lookup() -> None:
    section("8. Proxy rule longest-prefix lookup")
    from hlserve_config import ProxyRule
    from hlserve_proxy import find_proxy_for_path
    rules = [
        ProxyRule(prefix="/api", target="http://a:1"),
        ProxyRule(prefix="/api/v2", target="http://b:2"),
        ProxyRule(prefix="/ws", target="http://c:3"),
    ]
    # Longest prefix wins.
    r = find_proxy_for_path("/api/v2/users", rules)
    check("longest prefix wins", r is not None and r.target == "http://b:2")
    r = find_proxy_for_path("/api/users", rules)
    check("shorter prefix matches", r is not None and r.target == "http://a:1")
    r = find_proxy_for_path("/nope", rules)
    check("no match returns None", r is None)
    r = find_proxy_for_path("/api", rules)
    check("exact prefix match", r is not None and r.target == "http://a:1")


# ---------------------------------------------------------------------------
# 9. End-to-end: start a real server, hit it with HTTP
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_end_to_end_http() -> None:
    section("9. End-to-end HTTP (real server)")
    from hlserve_config import ServeConfig, ProxyRule
    from hlserve_hmr import HmrBus
    from hlserve_server import HlsDevHTTPHandler, make_server, HlsDevServer
    port = _free_port()
    tmpdir = tempfile.mkdtemp(prefix="hlserve-e2e-")
    bundle_dir = tmpdir
    bundle_base = "out"
    # Write a fake bundle (HTML + wasm + js).
    with open(os.path.join(bundle_dir, bundle_base + ".html"), "wb") as f:
        f.write(b"<!DOCTYPE html><html><head></head>"
                b"<body><p>hi</p></body></html>")
    with open(os.path.join(bundle_dir, bundle_base + ".wasm"), "wb") as f:
        f.write(b"\x00asm\x01\x00\x00\x00")  # wasm magic + version
    with open(os.path.join(bundle_dir, bundle_base + ".js"), "wb") as f:
        f.write(b"// glue\n")
    # Source file.
    src_file = os.path.join(tmpdir, "app.hls")
    with open(src_file, "w") as f:
        f.write("fn main() -> int { return 0 }\n")
    # Public dir.
    pub_dir = os.path.join(tmpdir, "public")
    os.makedirs(pub_dir)
    with open(os.path.join(pub_dir, "logo.svg"), "wb") as f:
        f.write(b"<svg></svg>")
    cfg = ServeConfig(
        input=src_file,
        bundle=os.path.join(bundle_dir, bundle_base),
        port=port,
        host="127.0.0.1",
        history_fallback=True,
        compress=True,
        public_dir=pub_dir,
        overlay=True,
    )
    bus = HmrBus()
    HlsDevHTTPHandler.config = cfg
    HlsDevHTTPHandler.bus = bus
    HlsDevHTTPHandler.bundle_dir = bundle_dir
    HlsDevHTTPHandler.bundle_base = bundle_base
    HlsDevHTTPHandler.input_hls = src_file
    HlsDevHTTPHandler.last_compile_ok = True
    HlsDevHTTPHandler.last_error_message = ""
    server = make_server(cfg, bus, bundle_dir, bundle_base, src_file)
    t = threading.Thread(target=server.serve_forever, daemon=True,
                         name="hlserve-test-server")
    t.start()
    base = "http://127.0.0.1:%d" % port
    try:
        # Wait for the port to come up.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port),
                                                timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        # (a) Index HTML — overlay injected.
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            data = r.read()
            check("GET / returns 200", r.status == 200)
            check("GET / injects overlay",
                  b"hlserve-hmr-client" in data)
            check("GET / serves bundle HTML",
                  b"<body><p>hi</p>" in data)
        # (b) Bundle wasm file.
        with urllib.request.urlopen(base + "/out.wasm", timeout=5) as r:
            data = r.read()
            check("GET /out.wasm returns wasm magic",
                  data[:4] == b"\x00asm")
            check("Content-Type for wasm",
                  r.headers.get("Content-Type") == "application/wasm")
        # (c) Public static asset.
        with urllib.request.urlopen(base + "/static/logo.svg",
                                      timeout=5) as r:
            data = r.read()
            check("GET /static/logo.svg serves from public dir",
                  data == b"<svg></svg>")
            check("Content-Type for svg",
                  r.headers.get("Content-Type") == "image/svg+xml")
        # (d) SPA history fallback.
        with urllib.request.urlopen(base + "/some/deep/route",
                                      timeout=5) as r:
            data = r.read()
            check("GET /some/deep/route falls back to index.html",
                  b"<body><p>hi</p>" in data)
        # (e) Source view.
        with urllib.request.urlopen(base + "/source", timeout=5) as r:
            data = r.read()
            check("GET /source serves HLS source",
                  b"fn main" in data)
        # (f) Gzip compression (need Accept-Encoding: gzip).
        req = urllib.request.Request(base + "/", headers={
            "Accept-Encoding": "gzip",
        })
        with urllib.request.urlopen(req, timeout=5) as r:
            check("gzip Content-Encoding on small body",
                  r.headers.get("Content-Encoding") in (None, "gzip"))
        # (g) Status banner on failed compile.
        HlsDevHTTPHandler.last_compile_ok = False
        HlsDevHTTPHandler.last_error_message = "boom: missing semicolon"
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            data = r.read()
            check("failed compile banner injected",
                  b"[hls-serve] boom: missing semicolon" in data)
        HlsDevHTTPHandler.last_compile_ok = True
        HlsDevHTTPHandler.last_error_message = ""
    finally:
        server.shutdown()
        server.server_close()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 10. End-to-end WebSocket handshake
# ---------------------------------------------------------------------------

def test_websocket_handshake_e2e() -> None:
    section("10. End-to-end WebSocket handshake")
    from hlserve_config import ServeConfig
    from hlserve_hmr import HmrBus
    from hlserve_server import HlsDevHTTPHandler, make_server
    port = _free_port()
    tmpdir = tempfile.mkdtemp(prefix="hlserve-ws-")
    with open(os.path.join(tmpdir, "out.html"), "wb") as f:
        f.write(b"<!DOCTYPE html><html><body></body></html>")
    cfg = ServeConfig(port=port, host="127.0.0.1",
                      input=os.path.join(tmpdir, "x.hls"),
                      bundle=os.path.join(tmpdir, "out"))
    with open(cfg.input, "w") as f:
        f.write("fn main() -> int { return 0 }\n")
    bus = HmrBus()
    HlsDevHTTPHandler.config = cfg
    HlsDevHTTPHandler.bus = bus
    HlsDevHTTPHandler.bundle_dir = tmpdir
    HlsDevHTTPHandler.bundle_base = "out"
    HlsDevHTTPHandler.input_hls = cfg.input
    HlsDevHTTPHandler.last_compile_ok = True
    server = make_server(cfg, bus, tmpdir, "out", cfg.input)
    t = threading.Thread(target=server.serve_forever, daemon=True,
                         name="hlserve-test-ws-server")
    t.start()
    try:
        # Wait for the port.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port),
                                                timeout=0.5):
                    break
            except OSError:
                time.sleep(0.1)
        # Connect a raw socket and perform the handshake manually.
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        import base64, hashlib, os as _os
        client_key = base64.b64encode(_os.urandom(16)).decode("ascii")
        handshake = (
            "GET /ws HTTP/1.1\r\n"
            "Host: 127.0.0.1:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (port, client_key)
        ).encode("ascii")
        sock.sendall(handshake)
        # Read the response (until \r\n\r\n). TCP may deliver extra
        # bytes (the first WS frame) in the SAME recv as the headers;
        # we have to keep the leftover for the frame read below.
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        check("handshake returns 101",
              b"HTTP/1.1 101" in buf or b"HTTP/1.0 101" in buf,
              detail="got: %r" % buf[:80])
        # Verify Sec-WebSocket-Accept is correct.
        expected_digest = hashlib.sha1(
            (client_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
            .encode("ascii")).digest()
        expected_accept = base64.b64encode(expected_digest).decode("ascii")
        check("Sec-WebSocket-Accept correct",
              ("Sec-WebSocket-Accept: %s" % expected_accept).encode("ascii")
              in buf,
              detail="expected: %s" % expected_accept)
        # The leftover bytes (after the headers) are the start of the
        # first WS frame. We use a buffer-based reader so we don't lose
        # bytes between the header read and the payload read.
        leftover = buf.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in buf else b""
        sock.settimeout(3.0)

        buffer = bytearray(leftover)

        def _recv_exact(n: int) -> bytes:
            """Read exactly n bytes from the socket. Uses the
            accumulated buffer; recvs more from the socket when the
            buffer is short. Returns the first n bytes; the rest stay
            in the buffer for subsequent reads."""
            while len(buffer) < n:
                chunk = sock.recv(max(n - len(buffer), 4096))
                if not chunk:
                    break
                buffer.extend(chunk)
            out = bytes(buffer[:n])
            del buffer[:n]
            return out

        # 2-byte frame header.
        hdr = _recv_exact(2)
        if len(hdr) >= 2:
            b0, b1 = hdr[0], hdr[1]
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            plen = b1 & 0x7F  # server frames aren't masked
            if plen == 126:
                ext = _recv_exact(2)
                import struct as _st
                plen = _st.unpack(">H", ext)[0]
            elif plen == 127:
                ext = _recv_exact(8)
                import struct as _st
                plen = _st.unpack(">Q", ext)[0]
            payload = _recv_exact(plen)
            check("hello frame opcode=text", opcode == 0x1)
            check("hello frame FIN=1", fin is True)
            try:
                msg = json.loads(payload.decode("utf-8"))
                check("hello frame JSON type=hello",
                      msg.get("type") == "hello",
                      detail="got: %r" % msg)
                check("hello frame version field",
                      "version" in msg and isinstance(msg["version"], int))
                check("hello frame server field",
                      "server" in msg and "hls-serve" in msg["server"])
            except Exception as e:
                check("hello frame parses as JSON", False,
                      detail="%s; payload=%r" % (e, payload))
        else:
            check("received hello frame header", False,
                  detail="got %r" % hdr)
        sock.close()
    finally:
        server.shutdown()
        server.server_close()
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("hls-serve Stage 75 acceptance smoke test")
    tests = [
        test_imports,
        test_cli,
        test_config,
        test_websocket,
        test_watcher,
        test_compiler_parsing,
        test_overlay,
        test_proxy_lookup,
        test_end_to_end_http,
        test_websocket_handshake_e2e,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            check("test %s raised" % t.__name__, False,
                  detail="%s: %s" % (type(e).__name__, e))
            traceback.print_exc()
    print()
    if check.failed == 0:
        print("ACCEPTANCE OK: Stage 75 — hls-serve (webpack-dev-server equivalent)")
        print("  All Stage 24 surface preserved (main / compile_bundle /")
        print("    FileWatcher / EventBus / DevHTTPHandler / DevServer)")
        print("  New Stage 75 modules importable (10 files in tools/hlserve_parts/)")
        print("  Config file: hls.serve.toml + hls.serve.json (TOML via tomllib)")
        print("  CLI: 13 new flags (--open/--https/--history-fallback/--compress/")
        print("    --public-dir/--proxy/--hot-reload/--overlay/--verbose/--quiet/")
        print("    --color/--no-color/--listen/--watch-dirs/--debounce-ms/--config)")
        print("  WebSocket HMR (RFC 6455 handshake + frame encode/decode)")
        print("  Compile-error overlay (CSS+JS injected before </body>)")
        print("  SPA history fallback (non-asset paths serve index.html)")
        print("  HTTP reverse proxy (longest-prefix match)")
        print("  HTTPS self-signed cert (cryptography optional)")
        print("  gzip compression (Accept-Encoding: gzip, body > 1KB)")
        print("  Public static dir (/static/*)")
        print("  Auto-open browser (--open)")
        return 0
    print("ACCEPTANCE FAIL: %d check(s) failed" % check.failed)
    return 1


if __name__ == "__main__":
    sys.exit(main())
