#!/usr/bin/env python3
"""Sources — where a log comes from in a gossip conversation.

A source is one of three things, and the prefix decides:

  PATH       a log file (JSON-lines) — the local case, the common one.
  DIR        a directory: the ledger inside it is DIR/.hls-pkg-
             transparency.log (the name every checkout shares).
  http(s):// an HTTP(S) URL served as JSON-lines. urllib does the
             fetching (stdlib only, like every tool here); the read
             is capped (--max-bytes, default 64 MB) so a hostile or
             broken source cannot exhaust memory, and the timeout is
             per source (--timeout, default 10 s). Redirects are the
             library's business; nothing is executed, evaluated or
             written — gossip is read-only end to end.

Every source resolves to (label, bytes) or a refused (label, reason).
A refused source never kills the conversation: it is REPORTED — the
gossip verdict is computed over the sources that answered, and the
exit code says whether what came back was enough.
"""
from __future__ import annotations

import os
import urllib.request

from hltlog_parts.hltlog_common import (
    DEFAULT_MAX_BYTES, DEFAULT_TIMEOUT, TlogError,
)

LEDGER_NAME = ".hls-pkg-transparency.log"


def resolve_path_source(source: str) -> str:
    """A file or directory source -> the log file path. A directory
    resolves to its ledger; anything else must be a file."""
    if os.path.isdir(source):
        inside = os.path.join(source, LEDGER_NAME)
        if not os.path.isfile(inside):
            raise TlogError("%s is a directory with no %s in it"
                            % (source, LEDGER_NAME))
        return inside
    return source


def fetch(source: str, timeout: float = DEFAULT_TIMEOUT,
          max_bytes: int = DEFAULT_MAX_BYTES):
    """Fetch one source. Returns (label, data_or_None, reason_or_None)
    — a refusal is data, never an exception that kills the gossip:
    the caller reports it and keeps comparing the sources that spoke.
    (Direct misuse — a bad --timeout, an unreachable scheme — still
    raises TlogError: the CLI's job to refuse before gossiping.)"""
    label = source
    try:
        if source.startswith("http://") or source.startswith("https://"):
            with urllib.request.urlopen(source, timeout=timeout) as resp:
                data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return label, None, ("the source is larger than the "
                                     "%d byte cap" % max_bytes)
            return label, data, None
        path = resolve_path_source(source)
        label = path
        if not os.path.isfile(path):
            return label, None, "no such file: %s" % path
        size = os.path.getsize(path)
        if size > max_bytes:
            return label, None, ("the file is larger than the %d byte "
                                 "cap" % max_bytes)
        with open(path, "rb") as f:
            return label, f.read(), None
    except TlogError as ex:
        return label, None, str(ex)
    except Exception as ex:                      # noqa: BLE001
        # Every fetch failure — URLError, timeout, HTTP error, socket
        # error, permission error — is one refused source with a short
        # reason. Gossip continues with the sources that answered.
        return label, None, "%s: %s" % (type(ex).__name__, ex)


__all__ = ["LEDGER_NAME", "fetch", "resolve_path_source"]
