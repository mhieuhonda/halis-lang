"""hlserve_common — shared helpers for the Stage 75 dev server.

Repo-root resolution, ANSI-colour timestamped logger, and the protocol
constants the HMR bus and the overlay JS both depend on (so the two
sides never drift).

Design notes:

* The Stage 24 dev server printed bare ``hlserve: ...`` lines. Stage 75
  keeps that prefix (so existing users' terminal greps still match) but
  adds an ISO-8601 timestamp + a level word, mirroring the structured
  logging style of ``std/log.hls`` (Stage 57).
* The ANSI colour codes are emitted only when stderr is a TTY (so log
  files don't fill up with escape noise); the ``--no-color`` CLI flag
  forces them off regardless.
* ``HMR_PROTOCOL_VERSION`` is bumped if the wire format of any client-
  visible message changes. The browser overlay checks this and refuses
  to act on a mismatched message (defensive — a stale browser tab could
  otherwise mis-parse a v2 message as v1 and crash).
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

# Repo root for resolving tools/hlwasm at runtime.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_TOOLS_DIR = os.path.join(_REPO_ROOT, "tools")

# HMR wire-protocol version (broadcast in the ``hello`` frame).
HMR_PROTOCOL_VERSION = 1

# Default ports / paths (used when neither CLI nor config override).
DEFAULT_PORT = 3000          # roadmap says "localhost:3000"
DEFAULT_BUNDLE = "out"
DEFAULT_INPUT = "examples/hello.hls"
DEFAULT_TARGET = "wasm32-unknown-unknown"
DEFAULT_PUBLIC_DIR = "public"
DEFAULT_DEBOUNCE_MS = 200

# Directories the watcher skips unconditionally (the Stage 24 watcher
# only skipped dot-dirs; Stage 75 also skips these well-known build
# output / dependency dirs which can be huge and would slow the mtime
# poll to a crawl).
WATCH_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".hls-pkg-cache",
    "target", "out", "bin",
    ".vscode", ".idea",
})

# Server banner string — also sent in the ``hello`` frame so a browser
# tab can display "Connected to <banner>".
SERVER_BANNER = "hls-serve v0.94.0-alpha (Stage 75)"


class _Ansi:
    """Tiny ANSI colour table; becomes a no-op when colours are off."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, code: str, s: str) -> str:
        if not self.enabled:
            return s
        return "\033[%sm%s\033[0m" % (code, s)

    def red(self, s: str) -> str:      return self("31", s)
    def green(self, s: str) -> str:    return self("32", s)
    def yellow(self, s: str) -> str:   return self("33", s)
    def blue(self, s: str) -> str:     return self("34", s)
    def magenta(self, s: str) -> str:  return self("35", s)
    def cyan(self, s: str) -> str:     return self("36", s)
    def bold(self, s: str) -> str:     return self("1", s)
    def dim(self, s: str) -> str:      return self("2", s)


_ANSI: Optional[_Ansi] = None


def _ansi() -> _Ansi:
    """Lazily compute the enabled flag once."""
    global _ANSI
    if _ANSI is None:
        # Colors only on a TTY, and only when not explicitly disabled.
        enabled = (sys.stderr.isatty()
                   and os.environ.get("HLSERVE_NO_COLOR", "") == ""
                   and os.environ.get("NO_COLOR", "") == "")
        _ANSI = _Ansi(enabled)
    return _ANSI


def disable_color() -> None:
    """Force colours off (called by ``--no-color``)."""
    global _ANSI
    _ANSI = _Ansi(False)


def enable_color() -> None:
    """Force colours on (called by ``--color``)."""
    global _ANSI
    _ANSI = _Ansi(True)


def log(level: str, msg: str, *, end: str = "\n",
        file=None) -> None:
    """Timestamped, level-prefixed logger.

    Mirrors the Stage 24 prefix ``hlserve:`` so existing greps keep
    working — the new format is::

        2026-09-17T15:42:01 hlserve INFO  message

    Levels: ``INFO`` (cyan), ``WARN`` (yellow), ``ERROR`` (red),
    ``OK`` (green), ``DEBUG`` (dim).
    """
    if file is None:
        file = sys.stderr
    a = _ansi()
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    level_colors = {
        "INFO":  a.cyan,
        "WARN":  a.yellow,
        "ERROR": a.red,
        "OK":    a.green,
        "DEBUG": a.dim,
    }
    color = level_colors.get(level, str)
    prefix = "%s %s %s " % (a.dim(ts), a.bold("hlserve"), color(level))
    file.write(prefix + msg + end)
    file.flush()


def info(msg: str)  -> None: log("INFO",  msg)
def warn(msg: str)  -> None: log("WARN",  msg)
def error(msg: str) -> None: log("ERROR", msg)
def ok(msg: str)    -> None: log("OK",    msg)
def debug(msg: str) -> None: log("DEBUG", msg)


def parse_addr_port(spec: str, default_port: int = DEFAULT_PORT) -> int:
    """Accept ``"8080"`` or ``"0.0.0.0:8080"`` or ``"localhost:8080"``;
    return the integer port. Used by the ``--port`` flag (which kept
    its Stage 24 semantics: integer only) and the new ``--listen`` flag
    (which accepts the full form)."""
    if ":" in spec:
        spec = spec.rsplit(":", 1)[1]
    try:
        return int(spec)
    except ValueError:
        return default_port


def parse_listen_addr(spec: str, default_port: int = DEFAULT_PORT):
    """Accept ``"8080"``, ``"0.0.0.0:8080"``, ``"localhost:8080"``;
    return the ``(host, port)`` tuple."""
    if ":" in spec:
        host, port_str = spec.rsplit(":", 1)
        if host in ("", "*"):
            host = "0.0.0.0"
        elif host == "localhost":
            host = "127.0.0.1"
        try:
            return (host, int(port_str))
        except ValueError:
            return ("0.0.0.0", default_port)
    try:
        return ("0.0.0.0", int(spec))
    except ValueError:
        return ("0.0.0.0", default_port)
