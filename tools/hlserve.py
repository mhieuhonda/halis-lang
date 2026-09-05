#!/usr/bin/env python3
"""hlserve.py — Stage 24 (v0.43.0-alpha): ``hls serve`` dev server.

A lightweight HTTP server + file watcher that re-compiles an HLS
program to wasm on every ``.hls`` save and pushes the new bundle to
the browser via Server-Sent Events (SSE) for live reload.

Features:
  * Watches ``.hls`` files in the cwd (and ``std/`` if present) for
    changes; debounces 200 ms to avoid re-compiling mid-keystroke.
  * Re-runs ``hlwasm`` on change (with the same ``--wasm-opt`` /
    ``--glue`` / ``--target`` flags as the initial compile).
  * Serves the bundle (``.wasm``, ``.js``, ``.html``) and the source
    ``.hls`` files at ``/`` (browse to ``http://localhost:PORT/``).
  * An SSE endpoint at ``/events`` pushes a ``reload`` event whenever
    the bundle is re-compiled. The HTML runner auto-subscribes and
    reloads on the event.
  * Injects a small SSE-listener snippet into the served HTML so the
    page auto-reloads.

Usage:
  python3 tools/hlserve.py [--port PORT] [--bundle OUT_BASE] \\
                            [--input FILE] [--target TRIPLE] \\
                            [--wasm-opt auto|on|off] [--glue compact|verbose]

  --port PORT       listen on this port (default 8080)
  --bundle OUT_BASE  the output base path (default: ./out)
  --input FILE      the .hls file to compile (default: examples/hello.hls)
  --target TRIPLE   target triple (default: wasm32-unknown-unknown)
  --wasm-opt MODE    optimization mode (default: auto)
  --glue STYLE      glue style (default: compact)

The server runs in the foreground; press Ctrl+C to stop.
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import sys
import threading
import time
from typing import List, Optional

# Repo root for resolving tools.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_TOOLS_DIR = os.path.join(_REPO_ROOT, "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)


# ============================================================================
# File watcher — debounce + queue recompiles.
# ============================================================================

class FileWatcher(threading.Thread):
    """Watch .hls files in the cwd (and std/) and trigger a recompile
    when they change. Debounces 200 ms to batch multi-file saves."""

    def __init__(self, watch_dirs: List[str], on_change,
                 debounce_ms: int = 200):
        super().__init__(daemon=True)
        self.watch_dirs = watch_dirs
        self.on_change = on_change
        self.debounce = debounce_ms / 1000.0
        self._mtimes: dict = {}
        self._stop = False
        self._pending = False
        self._lock = threading.Lock()

    def run(self):
        while not self._stop:
            now = time.time()
            changed = self._scan()
            if changed:
                with self._lock:
                    self._pending = True
                    last_change = now
            # Check if we should fire (debounce elapsed).
            time.sleep(0.05)
            with self._lock:
                if self._pending and (time.time() - last_change) >= self.debounce:
                    self._pending = False
                    try:
                        self.on_change()
                    except Exception as e:
                        sys.stderr.write("hlserve: recompile failed: %s\n" % e)

    def _scan(self) -> bool:
        """Walk the watch dirs; return True if any .hls file's mtime changed."""
        changed = False
        for d in self.watch_dirs:
            if not os.path.isdir(d):
                continue
            for root, dirs, files in os.walk(d):
                # Skip hidden dirs (.git, .hls-pkg-cache, etc.).
                dirs[:] = [x for x in dirs if not x.startswith(".")]
                for fn in files:
                    if not fn.endswith(".hls"):
                        continue
                    full = os.path.join(root, fn)
                    try:
                        m = os.path.getmtime(full)
                    except OSError:
                        continue
                    prev = self._mtimes.get(full)
                    if prev is None:
                        self._mtimes[full] = m
                    elif m != prev:
                        self._mtimes[full] = m
                        changed = True
                        sys.stderr.write("hlserve: change detected: %s\n" % full)
        return changed

    def stop(self):
        self._stop = True


# ============================================================================
# Compiler — invoke hlwasm.compile_program.
# ============================================================================

def compile_bundle(input_hls: str, output_base: str, target: str,
                   wasm_opt: str, glue: str) -> bool:
    """Compile the bundle. Returns True on success, False on failure."""
    try:
        from hlwasm import compile_program  # type: ignore
    except ImportError:
        sys.stderr.write("hlserve: cannot import hlwasm\n")
        return False
    # Suppress hlwasm's stderr noise during recompile — we'll print our own.
    import io
    import contextlib
    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(err_buf):
            rc = compile_program(
                input_hls, output_base,
                target=target,
                wasm_opt=wasm_opt,
                glue_style=glue)
        if rc != 0:
            sys.stderr.write("hlserve: compile failed (rc=%d):\n" % rc)
            sys.stderr.write(err_buf.getvalue())
            return False
        sys.stderr.write(err_buf.getvalue())
        return True
    except Exception as e:
        sys.stderr.write("hlserve: compile failed: %s\n" % e)
        return False


# ============================================================================
# SSE event bus — push reload events to subscribed clients.
# ============================================================================

class EventBus:
    """A simple SSE event bus. Subscribers are HTTP handlers; the bus
    pushes ``reload`` events to all subscribers."""

    def __init__(self):
        self.subscribers: List = []
        self._lock = threading.Lock()

    def subscribe(self, handler):
        with self._lock:
            self.subscribers.append(handler)

    def unsubscribe(self, handler):
        with self._lock:
            try:
                self.subscribers.remove(handler)
            except ValueError:
                pass

    def push(self, event: str, data: str = ""):
        with self._lock:
            subs = list(self.subscribers)
        for s in subs:
            try:
                s.send(event, data)
            except Exception:
                pass


# ============================================================================
# HTTP server — serve the bundle + SSE endpoint + injected HTML.
# ============================================================================

SSE_LISTENER_SNIPPET = """<script>
// Stage 24 hls serve — live reload via SSE
(function(){
  if (typeof EventSource === 'undefined') return; // browser doesn't support SSE
  var src = new EventSource('/events');
  src.addEventListener('reload', function(ev){
    console.log('[hls serve] reload event received');
    location.reload();
  });
  src.addEventListener('compile-error', function(ev){
    console.error('[hls serve] compile error:', ev.data);
  });
})();
</script>
"""


class SSEHandler:
    """A per-client SSE handler. Sends events on the open connection."""

    def __init__(self, wfile):
        self.wfile = wfile
        self.alive = True

    def send(self, event: str, data: str = ""):
        if not self.alive:
            return
        try:
            payload = "event: %s\n" % event
            for line in data.split("\n"):
                payload += "data: %s\n" % line
            payload += "\n"
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, OSError):
            self.alive = False


class DevHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that serves the bundle + the SSE endpoint."""

    # Class-level (shared) event bus, set by DevServer.
    event_bus: EventBus = EventBus()
    bundle_dir: str = "."
    bundle_base: str = "out"
    input_hls: str = "examples/hello.hls"
    last_compile_ok: bool = True

    def log_message(self, fmt, *args):
        # Suppress default access logging; print our own prefix.
        sys.stderr.write("[hls serve] %s - %s\n" % (self.address_string(),
                                                   fmt % args))

    def do_GET(self):
        if self.path == "/events":
            return self._handle_sse()
        if self.path == "/" or self.path == "/index.html":
            return self._serve_html()
        if self.path == "/source":
            return self._serve_source()
        # Serve a file from the bundle dir.
        return self._serve_file()

    def _serve_html(self):
        html_path = os.path.join(self.bundle_dir,
                                 self.bundle_base + ".html")
        if not os.path.isfile(html_path):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404: bundle .html not found. Has the "
                             b"initial compile completed?\n")
            return
        with open(html_path, "rb") as f:
            data = f.read()
        # Inject the SSE listener snippet before </body>.
        if b"</body>" in data:
            data = data.replace(b"</body>",
                                SSE_LISTENER_SNIPPET.encode("utf-8")
                                + b"</body>")
        else:
            data += SSE_LISTENER_SNIPPET.encode("utf-8")
        # Add a status banner if the last compile failed.
        if not self.last_compile_ok:
            banner = (b'<div style="background:#fee;color:#800;border-bottom:'
                      b'1px solid #800;padding:0.5rem;">[hls serve] last '
                      b'compile FAILED - fix the error and save to reload.'
                      b'</div>')
            if b"<body" in data:
                idx = data.find(b">", data.find(b"<body")) + 1
                data = data[:idx] + banner + data[idx:]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self):
        # Strip leading /.
        path = self.path.lstrip("/")
        # Disallow path traversal.
        if ".." in path.split("/"):
            self.send_response(400)
            self.end_headers()
            return
        full = os.path.join(self.bundle_dir, path)
        if not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(("404: %s not found in bundle dir\n" % path)
                             .encode("utf-8"))
            return
        with open(full, "rb") as f:
            data = f.read()
        ct = self._guess_content_type(path)
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_source(self):
        # Serve the HLS source file as plain text (for in-browser viewing).
        full = self.input_hls
        if not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        handler = SSEHandler(self.wfile)
        DevHTTPHandler.event_bus.subscribe(handler)
        # Send an initial "hello" event so the client knows we're connected.
        handler.send("hello", "hls serve connected")
        try:
            while handler.alive:
                time.sleep(0.5)
        except (BrokenPipeError, OSError):
            pass
        finally:
            DevHTTPHandler.event_bus.unsubscribe(handler)

    def _guess_content_type(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        return {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".wasm": "application/wasm",
            ".json": "application/json; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")


class DevServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ============================================================================
# Orchestrator.
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 24 dev server (hls serve): watch .hls files, "
                    "recompile on save, live-reload the browser.")
    ap.add_argument("--port", type=int, default=8080,
                    help="listen on this port (default: 8080)")
    ap.add_argument("--bundle", default="out",
                    help="output base path (default: ./out)")
    ap.add_argument("--input", default="examples/hello.hls",
                    help="the .hls file to compile (default: examples/hello.hls)")
    ap.add_argument("--target", default="wasm32-unknown-unknown",
                    help="target triple (default: wasm32-unknown-unknown)")
    ap.add_argument("--wasm-opt", default="auto",
                    choices=["auto", "on", "off"],
                    help="optimization mode (default: auto)")
    ap.add_argument("--glue", default="compact",
                    choices=["compact", "verbose"],
                    help="JS glue style (default: compact)")
    ap.add_argument("--watch", default=None, metavar="DIR",
                    help="additional directory to watch (default: std/ if it "
                         "exists, plus the dir of --input)")
    args = ap.parse_args()

    # Resolve the input file relative to the cwd.
    input_hls = os.path.abspath(args.input)
    if not os.path.isfile(input_hls):
        sys.stderr.write("hlserve: input file not found: %s\n" % input_hls)
        return 2

    # Resolve the bundle directory.
    bundle_dir = os.path.dirname(os.path.abspath(args.bundle)) or "."
    bundle_base = os.path.basename(args.bundle) or "out"
    os.makedirs(bundle_dir, exist_ok=True)

    # Configure the HTTP handler.
    DevHTTPHandler.bundle_dir = bundle_dir
    DevHTTPHandler.bundle_base = bundle_base
    DevHTTPHandler.input_hls = input_hls

    # Initial compile.
    sys.stderr.write("hlserve: initial compile...\n")
    ok = compile_bundle(input_hls, os.path.join(bundle_dir, bundle_base),
                        args.target, args.wasm_opt, args.glue)
    DevHTTPHandler.last_compile_ok = ok
    if not ok:
        sys.stderr.write("hlserve: initial compile FAILED — server will "
                         "still start (fix the error and save to reload).\n")

    # Watch dirs: cwd, std/, dir of input, dir of bundle.
    watch_dirs = [os.getcwd()]
    std_dir = os.path.join(os.getcwd(), "std")
    if os.path.isdir(std_dir):
        watch_dirs.append(std_dir)
    watch_dirs.append(os.path.dirname(input_hls))
    watch_dirs.append(bundle_dir)
    if args.watch:
        watch_dirs.append(os.path.abspath(args.watch))

    # The recompile callback.
    def on_change():
        sys.stderr.write("hlserve: recompiling %s...\n" % input_hls)
        ok = compile_bundle(input_hls,
                            os.path.join(bundle_dir, bundle_base),
                            args.target, args.wasm_opt, args.glue)
        DevHTTPHandler.last_compile_ok = ok
        if ok:
            sys.stderr.write("hlserve: reload event pushed\n")
            DevHTTPHandler.event_bus.push("reload", "recompiled")
        else:
            DevHTTPHandler.event_bus.push("compile-error",
                                          "see hlserve stderr for details")

    watcher = FileWatcher(watch_dirs, on_change)
    watcher.start()

    # Start the HTTP server.
    httpd = DevServer(("0.0.0.0", args.port), DevHTTPHandler)
    sys.stderr.write("hlserve: serving at http://localhost:%d/\n" % args.port)
    sys.stderr.write("hlserve: watching %d dirs for .hls changes\n"
                     % len(watch_dirs))
    sys.stderr.write("hlserve: press Ctrl+C to stop\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nhlserve: shutting down...\n")
    finally:
        watcher.stop()
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
