#!/usr/bin/env python3
"""hlserve.py — Stage 75 (v0.94.0-alpha): ``hls serve`` dev server.

The webpack-dev-server equivalent for Halis web apps. Watches ``.hls``
files, recompiles on change, serves the result on ``localhost:3000``,
hot-reloads the browser tab.

This is the public entrypoint. Implementation split into the
``tools/hlserve_parts/`` package (mirrors the Stage 70 ``tools/
hlwasm_parts/`` pattern). This module re-exports the original Stage 24
public API (``main``, ``compile_bundle``, ``FileWatcher``, ``EventBus``,
``DevHTTPHandler``, ``DevServer``) so existing callers (e.g. the Stage
24 ``make serve`` target, the ``suite_05_backends.sh`` import test,
``tools/hlwasm_parts/hwasm_glue_cli._start_dev_server``) keep working
unchanged.

Stage 75 features (new since Stage 24):

* **WebSocket HMR** — replaces the SSE endpoint with a bi-directional
  RFC 6455 socket at ``/ws``. The browser can push ``console.log``
  and uncaught errors back to the dev server.
* **Hot reload** — the wasm module is hot-swapped in-place (the JS
  glue keeps its state). Full reload only when the glue JS itself
  changes.
* **Compile-error overlay** — a self-contained CSS+JS overlay is
  injected into every served HTML page; pops up on compile failure
  with file:line + severity, auto-dismisses on the next clean
  compile.
* **SPA history fallback** — non-asset paths serve ``index.html``
  (so client-side routes like ``/users/42`` work in a Halis SPA).
* **HTTP reverse proxy** — ``--proxy /api=http://localhost:3001``
  forwards matching requests to a backend dev server.
* **HTTPS with self-signed cert** — ``--https`` generates an
  ephemeral RSA-2048 cert (requires the optional ``cryptography``
  package).
* **gzip compression** — responses > 1 KB are gzipped when the client
  advertises ``Accept-Encoding: gzip``.
* **Public static dir** — ``--public-dir ./public`` serves static
  assets at ``/static/*``.
* **Auto-open browser** — ``--open`` opens a browser tab once the
  server is ready.
* **TOML config** — ``hls.serve.toml`` (or ``hls.serve.json``) next
  to the project root holds all options; CLI flags override.

Stage 24 surface (preserved exactly):

* ``python3 tools/hlserve.py --input F --bundle out --port P``
* ``--target``, ``--wasm-opt``, ``--glue``, ``--watch``
* Class exports: ``main``, ``compile_bundle``, ``FileWatcher``,
  ``EventBus``, ``DevHTTPHandler``, ``DevServer``.
"""
from __future__ import annotations

import os
import sys

# Re-export the modular implementation.
_PKG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "hlserve_parts")
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from hlserve_cli import main  # noqa: F401,E402

# Stage 24 API surface (re-exported for backward compatibility).
# The new equivalents are in hlserve_parts; these thin wrappers exist
# so ``tools/hlwasm_parts/hwasm_glue_cli._start_dev_server`` and any
# downstream user code that imports them still works.
from hlserve_watcher import FileWatcher  # noqa: F401,E402
from hlserve_compiler import compile_once as compile_bundle  # noqa: F401,E402

# ``EventBus`` was the Stage 24 SSE bus. Stage 75 replaces it with the
# WebSocket ``HmrBus``; we provide a thin compatibility shim so old
# imports work — the shim simply re-exposes ``HmrBus`` as ``EventBus``.
from hlserve_hmr import HmrBus as EventBus  # noqa: F401,E402

# ``DevHTTPHandler`` and ``DevServer`` were the Stage 24 classes. The
# Stage 75 equivalents are ``HlsDevHTTPHandler`` and ``HlsDevServer``;
# the old names are aliased so existing code keeps importing.
from hlserve_server import HlsDevHTTPHandler as DevHTTPHandler  # noqa: F401,E402
from hlserve_server import HlsDevServer as DevServer  # noqa: F401,E402


if __name__ == "__main__":
    sys.exit(main())
