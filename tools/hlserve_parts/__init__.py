"""tools/hlserve_parts — Stage 75 (v0.94.0-alpha): the ``hls-serve``
webpack-dev-server-equivalent dev server, modularised.

The package mirrors the ``tools/hlwasm_parts/`` pattern (Stage 70): one
facade module (``tools/hlserve.py``) re-exports the original public API
(``main``, ``compile_bundle``, ``FileWatcher``, ``EventBus``,
``DevHTTPHandler``, ``DevServer``) for backward compatibility with the
Stage 24 ``make serve`` target and the existing test
(``suite_05_backends.sh`` line 483 ``import hlserve; assert
hasattr(hlserve, 'main')``).

Implementation split:

* ``hlserve_common``   — repo-root resolution, ANSI colour logger,
                          shared constants (HMR protocol version,
                          default ports, ignore-dirs).
* ``hlserve_config``   — ``hls.serve.toml`` reader + ``ServeConfig``
                          dataclass + CLI/config merge.
* ``hlserve_watcher``  — debounced multi-directory mtime poller with
                          ignore patterns (.git, node_modules, target).
* ``hlserve_compiler`` — wraps ``tools/hlwasm.compile_program``,
                          captures stdout/stderr, parses ``file:line:``
                          from compiler errors for the overlay.
* ``hlserve_hmr``      — RFC 6455 WebSocket server + HMR message bus
                          (replaces the Stage 24 SSE endpoint).
* ``hlserve_overlay``  — compile-error overlay (CSS+JS snippet
                          injected into every served HTML page).
* ``hlserve_proxy``    — minimal HTTP reverse proxy for ``--proxy
                          /api=http://localhost:3001``.
* ``hlserve_tls``      — optional self-signed cert (uses
                          ``cryptography`` if installed, else warns
                          and falls back to plain HTTP).
* ``hlserve_server``   — ``HlsDevHTTPHandler``: routes ``/``, ``/ws``,
                          ``/static/*``, SPA history fallback, proxy
                          dispatch, gzip Content-Encoding, public dir,
                          overlay injection.
* ``hlserve_cli``      — argparse + ``main()`` orchestrator.
"""
from __future__ import annotations
