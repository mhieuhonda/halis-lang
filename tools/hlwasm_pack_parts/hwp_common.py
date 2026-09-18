"""hwp_common — shared helpers for the Stage 76 ``hls-wasm-pack`` tool.

Repo-root resolution, a small stderr logger (mirroring the Stage 75
``hlserve_common`` style), pack constants, and the realpath-based
path-containment defence (the same defence Stage 27 added to the dev
server and Stage 75 carried over: a symlink inside the output dir
pointing outside must be rejected, not followed).

Design notes:

* ``hls-wasm-pack`` is the ``wasm-pack`` equivalent for Halis web apps.
  It turns a ``.hls`` program into a publish-ready npm package
  (``pkg/``): ``.wasm`` + target-specific ``.js`` glue + ``.d.ts``
  + ``package.json`` + ``README.md`` + ``.pack-manifest.json``.
  No new compiler builtins; this is a pure tooling stage on top of
  the Stage 23/24 ``hlwasm`` backend + the Stage 24 ``hlwasm_opt``
  size optimizer + the Stage 73 ``std.jsffi`` extern surface.
* ``PACK_FORMAT_VERSION`` is bumped if the on-disk layout of ``pkg/``
  changes (a stale ``pkg/`` from v1 must never validate as v2).
* All sizes are measured in bytes; the acceptance gates live in
  ``tests/wasm_pack_acceptance.py`` (``make wasm-pack-acceptance``).
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

# Repo root / tools dir for resolving hlwasm + hlwasm_opt at runtime.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_TOOLS_DIR = os.path.join(_REPO_ROOT, "tools")

# On-disk pkg/ layout version (stamped into .pack-manifest.json).
PACK_FORMAT_VERSION = 1

# Tool banner (``--version`` prints this).
PACK_BANNER = "hls-wasm-pack v0.95.0-alpha (Stage 76)"

# Publish-ready targets (the wasm-pack ``--target`` surface, adapted).
#   bundler   ESM ``import`` — webpack / rollup / vite (default).
#   web       ESM bare — no bundler (``<script type=module>``).
#   nodejs    CommonJS ``require()`` — Node.js services / tests.
#   deno      ESM + deno.json hint — ``deno run`` / ``deno publish``.
#   no-modules  IIFE global ``HalisPack`` — plain ``<script>`` tag.
PACK_TARGETS = ("bundler", "web", "nodejs", "deno", "no-modules")

DEFAULT_TARGET = "bundler"
DEFAULT_OUT_DIR = "pkg"
DEFAULT_OPT_LEVEL = "O3"

# Size gates (documented in the acceptance test, not enforced by build:
# a huge program still packs — ``check`` warns, it does not fail).
WARN_WASM_BYTES = 102400      # 100 KB — same as the Stage 24 webapp gate
WARN_JS_BYTES = 16384         # 16 KB — Stage 73 compact-glue ceiling


class _Ansi:
    GREEN = "\x1b[32m"
    YELLOW = "\x1b[33m"
    RED = "\x1b[31m"
    BOLD = "\x1b[1m"
    RESET = "\x1b[0m"


def _use_color(force: Optional[bool] = None) -> bool:
    if force is True:
        return True
    if force is False:
        return False
    return sys.stderr.isatty()


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def log_info(msg: str, color: Optional[bool] = None) -> None:
    if _use_color(color):
        sys.stderr.write("%s%s%s [INFO] %s\n"
                         % (_Ansi.GREEN, _ts(), _Ansi.RESET, msg))
    else:
        sys.stderr.write("hls-wasm-pack [%s] INFO: %s\n" % (_ts(), msg))


def log_warn(msg: str, color: Optional[bool] = None) -> None:
    if _use_color(color):
        sys.stderr.write("%s%s%s [WARN] %s\n"
                         % (_Ansi.YELLOW, _ts(), _Ansi.RESET, msg))
    else:
        sys.stderr.write("hls-wasm-pack [%s] WARN: %s\n" % (_ts(), msg))


def log_error(msg: str, color: Optional[bool] = None) -> None:
    if _use_color(color):
        sys.stderr.write("%s%s%s [ERROR] %s\n"
                         % (_Ansi.RED, _ts(), _Ansi.RESET, msg))
    else:
        sys.stderr.write("hls-wasm-pack [%s] ERROR: %s\n" % (_ts(), msg))


def inside_dir(path: str, directory: str) -> bool:
    """Return True iff ``path`` resolves INSIDE ``directory``.

    The Stage 27 realpath-based path-traversal defence: symlinks are
    resolved on BOTH sides before comparison, so ``pkg/foo -> /etc``
    can never exfiltrate or overwrite host files during pack/unpack.
    """
    try:
        real_path = os.path.realpath(os.path.abspath(path))
        real_dir = os.path.realpath(os.path.abspath(directory))
    except OSError:
        return False
    if real_path == real_dir:
        return True
    return real_path.startswith(real_dir + os.sep)


def ensure_inside(path: str, directory: str, what: str = "path") -> str:
    """Raise ValueError if ``path`` escapes ``directory``; else return it."""
    if not inside_dir(path, directory):
        raise ValueError(
            "%s escapes the output directory: %r (rejected: "
            "path-traversal defence)" % (what, path))
    return path


__all__ = [
    "DEFAULT_OPT_LEVEL",
    "DEFAULT_OUT_DIR",
    "DEFAULT_TARGET",
    "PACK_BANNER",
    "PACK_FORMAT_VERSION",
    "PACK_TARGETS",
    "WARN_JS_BYTES",
    "WARN_WASM_BYTES",
    "_REPO_ROOT",
    "_TOOLS_DIR",
    "ensure_inside",
    "inside_dir",
    "log_error",
    "log_info",
    "log_warn",
]
