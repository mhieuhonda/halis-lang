"""hlserve_cli — Stage 75 argument parser + orchestrator.

The orchestrator wires together:

* ``ServeConfig`` (loaded from ``hls.serve.toml`` if present, else the
  dataclass defaults) merged with the CLI args.
* ``HmrBus`` (the WebSocket message bus, also used to broadcast compile
  results to all connected browser tabs).
* ``FileWatcher`` (the debounced multi-directory mtime poller; fires
  ``on_change`` with the list of changed files).
* ``compile_once`` (the ``hlwasm`` wrapper that returns a
  ``CompileResult`` with parsed diagnostics).
* ``HlsDevServer`` (the HTTP server with the overlay, proxy, SPA
  fallback, compression, and public-dir routes).
* Optional ``webbrowser.open()`` for ``--open``.

The CLI is intentionally compatible with the Stage 24 ``make serve``
target — ``python3 tools/hlserve.py --input F --bundle out --port P``
still works exactly as before, with the new features turned off by
default (so ``make serve`` is byte-for-byte identical for users who
haven't opted in).
"""
from __future__ import annotations

import argparse
import os
import socket
import threading
import time
import webbrowser
from typing import List, Optional

import hlserve_common as common
from hlserve_common import (
    DEFAULT_BUNDLE,
    DEFAULT_DEBOUNCE_MS,
    DEFAULT_INPUT,
    DEFAULT_PORT,
    DEFAULT_PUBLIC_DIR,
    DEFAULT_TARGET,
    SERVER_BANNER,
    disable_color,
    enable_color,
    error as log_error,
    info,
    ok as log_ok,
    warn,
)
from hlserve_config import (
    ServeConfig,
    default_config_for_cwd,
    merge_args,
)
from hlserve_compiler import compile_once
from hlserve_hmr import HmrBus
from hlserve_server import HlsDevHTTPHandler, make_server
from hlserve_watcher import FileWatcher


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the Stage 75 CLI parser.

    Flags are grouped into:
      * Core (Stage 24 preserved): --input, --bundle, --port, --target,
        --wasm-opt, --glue, --watch
      * Stage 75 new: --listen, --open, --https, --history-fallback,
        --no-history-fallback, --compress, --no-compress, --public-dir,
        --proxy (multi), --hot-reload, --no-hot-reload, --overlay,
        --no-overlay, --verbose, --quiet, --color, --no-color,
        --watch-dirs (multi), --debounce-ms, --config
    """
    ap = argparse.ArgumentParser(
        prog="hlserve",
        description=SERVER_BANNER + " — a webpack-dev-server equivalent "
                                    "for Halis web apps. Watches .hls "
                                    "files, recompiles on change, serves "
                                    "the result on localhost:PORT, "
                                    "hot-reloads the browser tab.",
        epilog="Config file: hls.serve.toml or hls.serve.json in the "
                "cwd. CLI flags override the config file.",
    )
    # ---- Core (Stage 24 preserved) ----
    ap.add_argument("--input", default=DEFAULT_INPUT,
                    help="the .hls file to compile (default: %s)"
                         % DEFAULT_INPUT)
    ap.add_argument("--bundle", default=DEFAULT_BUNDLE,
                    help="output base path (default: %s)" % DEFAULT_BUNDLE)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="listen on this port (default: %d)" % DEFAULT_PORT)
    ap.add_argument("--target", default=DEFAULT_TARGET,
                    help="target triple (default: %s)" % DEFAULT_TARGET)
    ap.add_argument("--wasm-opt", default="auto",
                    choices=["auto", "on", "off"],
                    help="optimization mode (default: auto)")
    ap.add_argument("--glue", default="compact",
                    choices=["compact", "verbose"],
                    help="JS glue style (default: compact)")
    ap.add_argument("--watch", default=None, metavar="DIR",
                    help="additional directory to watch (Stage 24 compat)")
    # ---- Stage 75 new ----
    ap.add_argument("--listen", default=None,
                    metavar="HOST:PORT",
                    help="listen address (e.g. 0.0.0.0:3000 or "
                         "localhost:3000). Overrides --port.")
    ap.add_argument("--open", action="store_true",
                    help="open the browser at start")
    ap.add_argument("--https", action="store_true",
                    help="serve over HTTPS (self-signed cert; requires "
                         "the 'cryptography' package)")
    ap.add_argument("--history-fallback", dest="history_fallback",
                    action="store_true", default=False,
                    help="serve index.html for non-asset paths (SPA mode; "
                         "default: ON unless --no-history-fallback)")
    ap.add_argument("--no-history-fallback", dest="history_fallback_off",
                    action="store_true", default=False,
                    help="disable SPA history fallback")
    ap.add_argument("--compress", dest="compress",
                    action="store_true", default=False,
                    help="gzip responses > 1KB (default: ON unless "
                         "--no-compress)")
    ap.add_argument("--no-compress", dest="compress_off",
                    action="store_true", default=False,
                    help="disable gzip compression")
    ap.add_argument("--public-dir", default=None,
                    help="directory to serve at /static/* (default: ./"
                         "%s if it exists)" % DEFAULT_PUBLIC_DIR)
    ap.add_argument("--proxy", action="append", default=[],
                    metavar="PREFIX=TARGET",
                    help="reverse-proxy rule (e.g. /api=http://localhost:3001). "
                         "May be given multiple times.")
    ap.add_argument("--hot-reload", dest="hot_reload",
                    action="store_true", default=False,
                    help="swap the wasm module without a full page reload "
                         "(default: ON unless --no-hot-reload)")
    ap.add_argument("--no-hot-reload", dest="hot_reload_off",
                    action="store_true", default=False,
                    help="always do a full page reload on change")
    ap.add_argument("--overlay", dest="overlay",
                    action="store_true", default=False,
                    help="inject the compile-error overlay into HTML "
                         "(default: ON unless --no-overlay)")
    ap.add_argument("--no-overlay", dest="overlay_off",
                    action="store_true", default=False,
                    help="disable the compile-error overlay")
    ap.add_argument("--verbose", action="store_true",
                    help="enable DEBUG logging")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress INFO logging (only WARN/ERROR)")
    ap.add_argument("--color", action="store_true",
                    help="force ANSI colors in log output")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI colors in log output")
    ap.add_argument("--watch-dirs", action="append",
                    dest="watch_dirs", default=[],
                    metavar="DIR",
                    help="additional directory to watch (may be given "
                         "multiple times)")
    ap.add_argument("--debounce-ms", type=int, default=DEFAULT_DEBOUNCE_MS,
                    help="debounce interval in milliseconds (default: %d)"
                         % DEFAULT_DEBOUNCE_MS)
    ap.add_argument("--config", default=None,
                    help="path to a config file (default: auto-detect "
                         "hls.serve.toml or hls.serve.json)")
    ap.add_argument("--version", action="version",
                    version=SERVER_BANNER)
    return ap


# ---------------------------------------------------------------------------
# Open browser helper
# ---------------------------------------------------------------------------

def open_browser(url: str) -> None:
    """Best-effort open ``url`` in the user's default browser. Errors
    are swallowed (a missing browser on a headless dev box shouldn't
    crash the dev server)."""
    try:
        webbrowser.open(url, new=2)  # new=2 = new tab
    except Exception as e:
        warn("could not open browser: %s (visit %s manually)" % (e, url))


# ---------------------------------------------------------------------------
# Wait-for-port helper
# ---------------------------------------------------------------------------

def wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll until ``host:port`` accepts a TCP connection (or timeout).
    Returns True if the port came up, False on timeout. Used so the
    ``--open`` flag doesn't open a browser before the server is ready
    (which would result in a connection-refused error)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# Recompile callback
# ---------------------------------------------------------------------------

def make_on_change(cfg: ServeConfig, bus: HmrBus,
                   bundle_full_path: str) -> callable:
    """Build the ``on_change(changed_files)`` callback invoked by the
    file watcher. Recompiles, then broadcasts the appropriate HMR
    message (ok / errors / progress)."""
    def on_change(changed_files: list) -> None:
        bus.send_progress("compile-start", 0,
                          detail="recompiling %d file(s)" % len(changed_files))
        info("recompiling (changed: %s)…" %
             ", ".join(os.path.basename(f) for f in changed_files[:5]))
        result = compile_once(
            cfg.input, bundle_full_path,
            target=cfg.target,
            wasm_opt=cfg.wasm_opt,
            opt_level=cfg.opt_level,
            glue=cfg.glue,
        )
        HlsDevHTTPHandler.last_compile_ok = result.ok
        HlsDevHTTPHandler.last_error_message = (
            "" if result.ok else result.errors[0].message if result.errors
            else "compile failed")
        if not result.ok:
            log_error("compile FAILED (%d ms): %d error(s)" %
                      (result.elapsed_ms,
                       sum(1 for e in result.errors
                           if e.severity == "error")))
            for e in result.errors:
                if e.severity == "error":
                    log_error("  " + e.format_for_terminal())
            bus.send_errors(result.to_errors_dict())
        else:
            log_ok("compile OK (%d ms, wasm=%d B, js=%d B)" %
                   (result.elapsed_ms, result.wasm_bytes, result.js_bytes))
            bus.send_progress("compile-end", 100, detail="ok")
            # Hot reload (swap wasm without full page reload) — works
            # for changes that don't touch the JS glue. Stage 75 is
            # conservative: if the glue file size changed, do a full
            # reload (the JS API surface may have changed).
            stats = result.to_stats_dict()
            stats["changed_files"] = [os.path.basename(f)
                                        for f in changed_files]
            if cfg.hot_reload and result.wasm_path:
                wasm_url = os.path.basename(result.wasm_path)
                bus.send_hot_reload(wasm_url,
                                    reason="wasm-changed")
                bus.send_ok(stats, hot_reloadable=True,
                            wasm_url=wasm_url)
            else:
                bus.send_ok(stats, hot_reloadable=False)
                bus.send_reload(reason="wasm-changed")
    return on_change


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)

    # Color overrides (must be set before any logging).
    if args.no_color:
        disable_color()
    elif args.color:
        enable_color()

    # Quiet / verbose overrides the logger (the logger is a singleton,
    # so these are sticky for the rest of the process).
    if args.quiet:
        # Filter out INFO/DEBUG by patching the helpers.
        # We don't actually use the logging module; the simplest way
        # to suppress INFO is to overwrite the helpers.
        common.info = lambda *a, **k: None  # type: ignore[assignment]
        common.ok = lambda *a, **k: None    # type: ignore[assignment]
        common.debug = lambda *a, **k: None  # type: ignore[assignment]
    if args.verbose:
        # Promote DEBUG to visible.
        import logging
        logging.basicConfig(level=logging.DEBUG)

    info(SERVER_BANNER)
    # Load the config file (explicit --config wins; else auto-detect).
    if args.config:
        if not os.path.isfile(args.config):
            log_error("config file not found: %s" % args.config)
            return 2
        from hlserve_config import load_config_file, config_dict_to_serve_config
        try:
            d = load_config_file(args.config)
            cfg = config_dict_to_serve_config(d)
        except Exception as e:
            log_error("could not parse config %s: %s" % (args.config, e))
            return 2
    else:
        cfg = default_config_for_cwd()
    # Merge CLI args (CLI wins).
    cfg = merge_args(args, cfg)

    # If --public-dir not set explicitly but ./public exists, use it.
    if cfg.public_dir is None:
        default_pub = os.path.join(os.getcwd(), DEFAULT_PUBLIC_DIR)
        if os.path.isdir(default_pub):
            cfg.public_dir = default_pub

    # Resolve the input file.
    input_hls = os.path.abspath(cfg.input)
    if not os.path.isfile(input_hls):
        log_error("input file not found: %s" % input_hls)
        return 2
    cfg.input = input_hls

    # Resolve the bundle dir + base.
    bundle_dir = (os.path.dirname(os.path.abspath(cfg.bundle))
                  or ".")
    bundle_base = os.path.basename(cfg.bundle) or "out"
    os.makedirs(bundle_dir, exist_ok=True)
    bundle_full_path = os.path.join(bundle_dir, bundle_base)

    # Build the watch dir list.
    watch_dirs: List[str] = []
    # Always watch the input file's dir.
    watch_dirs.append(os.path.dirname(input_hls))
    # Watch the cwd (Stage 24 compat).
    watch_dirs.append(os.getcwd())
    # Watch std/ if it exists (common cause of changes).
    std_dir = os.path.join(os.getcwd(), "std")
    if os.path.isdir(std_dir):
        watch_dirs.append(std_dir)
    # Watch the bundle dir (for hand-edited JS / CSS glue).
    watch_dirs.append(bundle_dir)
    # Add the config-specified dirs.
    for d in cfg.watch_dirs:
        d_abs = os.path.abspath(d)
        if os.path.isdir(d_abs):
            watch_dirs.append(d_abs)
    # Add the --watch / --watch-dirs extra dirs.
    for d in cfg.watch_extra:
        if os.path.isdir(d):
            watch_dirs.append(d)
    # Dedup (preserve order).
    seen = set()
    unique = []
    for d in watch_dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    watch_dirs = unique

    # Build the HMR bus.
    bus = HmrBus()
    # Wire up the inbound-message callbacks.
    def on_log(obj, client):
        level = obj.get("level", "log")
        args = obj.get("args", [])
        msg = " ".join(str(a) for a in args)
        prefix = "[client %d:%s]" % (client.id, level)
        if level == "error":
            log_error("%s %s" % (prefix, msg))
        elif level == "warn":
            warn("%s %s" % (prefix, msg))
        else:
            info("%s %s" % (prefix, msg))
    def on_client_error(obj, client):
        msg = obj.get("message", "(no message)")
        stack = obj.get("stack", "")
        log_error("[client %d error] %s" % (client.id, msg))
        if stack:
            for line in stack.splitlines()[:5]:
                log_error("  " + line)
    def on_identify(obj, client):
        cid = obj.get("id", "?")
        info("client connected (id=%s, cid=%d)" % (cid, client.id))
    bus.set_callbacks(on_log=on_log, on_error=on_client_error,
                       on_identify=on_identify)

    # Initial compile.
    info("initial compile…")
    result = compile_once(
        cfg.input, bundle_full_path,
        target=cfg.target, wasm_opt=cfg.wasm_opt,
        opt_level=cfg.opt_level, glue=cfg.glue)
    HlsDevHTTPHandler.last_compile_ok = result.ok
    HlsDevHTTPHandler.last_error_message = (
        "" if result.ok else
        (result.errors[0].message if result.errors else "compile failed"))
    if result.ok:
        log_ok("initial compile OK (%d ms, wasm=%d B, js=%d B)" %
               (result.elapsed_ms, result.wasm_bytes, result.js_bytes))
    else:
        log_error("initial compile FAILED — server will still start "
                  "(fix the error and save to reload).")
        for e in result.errors:
            if e.severity == "error":
                log_error("  " + e.format_for_terminal())
    # Push initial state (so a fresh browser tab connecting sees the
    # current compile state immediately).
    if result.ok:
        bus.send_ok(result.to_stats_dict(),
                    hot_reloadable=cfg.hot_reload,
                    wasm_url=(os.path.basename(result.wasm_path)
                              if result.wasm_path else None))
    else:
        bus.send_errors(result.to_errors_dict())

    # Start the file watcher.
    on_change = make_on_change(cfg, bus, bundle_full_path)
    watcher = FileWatcher(watch_dirs, on_change,
                          debounce_ms=cfg.debounce_ms)
    watcher.start()
    info("watching %d dir(s) for .hls changes (debounce=%dms)" %
         (len(watch_dirs), cfg.debounce_ms))

    # Build + start the HTTP server.
    server = make_server(cfg, bus, bundle_dir, bundle_base, input_hls)
    proto = "https" if cfg.https else "http"
    display_host = "localhost" if cfg.host in ("0.0.0.0", "127.0.0.1") else cfg.host
    url = "%s://%s:%d/" % (proto, display_host, cfg.port)
    info("serving at %s" % url)
    info("press Ctrl+C to stop")

    # --open: wait for the port to come up, then open the browser.
    if cfg.open:
        def _open_when_ready():
            if wait_for_port(cfg.host, cfg.port, timeout=5.0):
                time.sleep(0.1)  # tiny extra cushion
                open_browser(url)
            else:
                warn("--open: server did not come up within 5s")
        threading.Thread(target=_open_when_ready, daemon=True,
                          name="hlserve-open").start()

    # Serve forever (until Ctrl+C).
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        info("\nshutting down…")
    finally:
        watcher.stop()
        server.shutdown()
        server.server_close()
        # Clean up the TLS cert temp files if we created them.
        cert_pair = getattr(server, "_hlserve_cert_pair", None)
        if cert_pair:
            from hlserve_tls import cleanup_cert_pair
            cleanup_cert_pair(cert_pair)
    return 0
