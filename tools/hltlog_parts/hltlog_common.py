#!/usr/bin/env python3
"""Shared constants, errors and log reading for hls-tlog — Stage 107.

The transparency ledger (`.hls-pkg-transparency.log`, the file
`hls-pkg lock`/`publish`, the SBOM, repro and sign append to) has been
a single local JSON-lines file with a SHA-256 hash chain since Stage
13. One copy of the evidence is one point of failure: truncate it,
rewrite it, or fork it, and nothing else in the tree can tell.
Stage 107 turns the ledger into something CERTIFICATE-TRANSPARENCY
shaped: views that summarise a log, gossip that compares views across
sources, witnesses that sign a view with Stage 106's own Ed25519 keys,
and inclusion proofs that carry a record home from a log.

This module owns the shapes everything else agrees on:

  a RECORD        one JSON object per line: `seq` (int >= 1),
                  `timestamp` (int), `prev_hash` (64 hex), `chain_hash`
                  (64 hex) — mandatory, the chain arithmetic needs
                  nothing else. `kind` is OPTIONAL: Stage 13's own
                  lock records predate the kind convention and carry
                  none; when present it must be a non-empty string.
                  Every other field is the writer's business.
  the CHAIN HASH  sha256(prev_hash + canonical_json(record minus
                  chain_hash)), canonical = sorted keys, no spaces —
                  byte-identical to hpkg_log's `_build_chained_record`
                  (the one definition; the acceptance gate pins the
                  two tools to the same verdict on the same file).
  a VIEW          schema hls-tlog-view/v1: `length`, `head` (the last
                  record's chain_hash, "0"*64 for an empty log),
                  `log_sha256` (the raw bytes' digest — two mirrors
                  that served byte-identical files share it),
                  `kinds` (per-kind record counts, untyped records
                  counted as "untyped"), `first_seq` / `last_seq`.
                  Deterministic over an unchanged log, so a witness
                  over a view is a signed promise.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(TOOL_DIR, "hpkg_parts")
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

from hpkg_common import TRANSPARENCY_LOG  # noqa: E402  (the same ledger)

TOOL = "hls-tlog"
VERSION = "0.126.0-alpha"          # the toolchain release this run stamps

VIEW_SCHEMA = "hls-tlog-view/v1"
VERIFY_SCHEMA = "hls-tlog-verify/v1"
WITNESS_SCHEMA = "hls-tlog-witness/v1"
WITNESS_VERIFY_SCHEMA = "hls-tlog-witness-verify/v1"
GOSSIP_SCHEMA = "hls-tlog-gossip/v1"
PROOF_SCHEMA = "hls-tlog-proof/v1"
PROOF_VERIFY_SCHEMA = "hls-tlog-proof-verify/v1"

GENESIS = "0" * 64                 # the prev_hash every log starts from
HEX_LEN = 64
DEFAULT_MAX_BYTES = 64 * 1024 * 1024   # a source larger than this refuses
DEFAULT_TIMEOUT = 10.0                 # seconds, per HTTP source

WITNESS_SUFFIX = ".witness.json"
PROOF_SUFFIX = ".proof.json"


class TlogError(Exception):
    """A refusal the CLI reports on stderr with exit 1."""


# ---------------------------------------------------------------------------
# Canonical bytes — the one serialization every signed document uses.
# ---------------------------------------------------------------------------

def canon_bytes(obj) -> bytes:
    """Canonical document bytes: sorted keys, two-space indent, trailing
    newline — the release statement's contract (Stage 106), so a view,
    a witness and a proof are byte-reproducible over unchanged input."""
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def chain_of(prev_hash: str, record: dict) -> str:
    """The chain hash over `record` chained onto `prev_hash` — the SAME
    arithmetic hpkg_log._build_chained_record runs (canonical JSON with
    sorted keys and no whitespace, the chain_hash field itself left
    out). One definition; the gate pins the two tools together."""
    body = {k: v for k, v in record.items() if k != "chain_hash"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canon).encode("utf-8")).hexdigest()


def is_hex64(s) -> bool:
    return isinstance(s, str) and len(s) == HEX_LEN \
        and all(c in "0123456789abcdef" for c in s)


# ---------------------------------------------------------------------------
# Reading a log: lines -> records, with the shape named where it breaks.
# ---------------------------------------------------------------------------

def read_log_bytes(path: str) -> bytes:
    if not os.path.isfile(path):
        raise TlogError("no transparency log at %s — nothing to read"
                        % path)
    with open(path, "rb") as f:
        return f.read()


def parse_log(data: bytes, origin: str) -> list:
    """Parse JSON-lines bytes into records. Refusals name the line and
    the broken shape — a log this tool cannot parse is evidence of
    tampering or corruption, never something to skip past."""
    records = []
    text = data.decode("utf-8", errors="replace")
    for i, raw in enumerate(text.split("\n")):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError as ex:
            raise TlogError("%s: line %d is not JSON (%s)"
                            % (origin, i + 1, ex))
        shape_error(rec, origin, i + 1)
        records.append(rec)
    return records


def shape_error(rec, origin: str, line_no: int):
    """The record shape every writer agrees on. `seq`, `timestamp`,
    `prev_hash`, `chain_hash` are mandatory — the chain arithmetic
    needs nothing else. `kind` is optional: Stage 13's own lock
    records predate the kind convention and carry none. Everything
    beyond these is the writer's business."""
    what = "%s: line %d" % (origin, line_no)
    if not isinstance(rec, dict):
        raise TlogError("%s is a JSON value, not an object" % what)
    if "kind" in rec and (not isinstance(rec["kind"], str)
                          or not rec["kind"]):
        raise TlogError("%s has an empty kind" % what)
    seq = rec.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise TlogError("%s has a bad seq (%r)" % (what, seq))
    ts = rec.get("timestamp")
    if not isinstance(ts, int) or isinstance(ts, bool) or ts < 0:
        raise TlogError("%s has a bad timestamp (%r)" % (what, ts))
    for field in ("prev_hash", "chain_hash"):
        if not is_hex64(rec.get(field)):
            raise TlogError("%s has a bad %s (want 64 hex characters)"
                            % (what, field))


def load_log(path: str) -> list:
    """Read + parse a log file in one call."""
    return parse_log(read_log_bytes(path), path)


# ---------------------------------------------------------------------------
# The view — the summary every source, witness and gossip exchange runs
# on. Deterministic over an unchanged log.
# ---------------------------------------------------------------------------

def compute_view(records: list, log_sha256: str) -> dict:
    kinds = {}
    for rec in records:
        kind = rec.get("kind") or "untyped"
        kinds[kind] = kinds.get(kind, 0) + 1
    head = records[-1]["chain_hash"] if records else GENESIS
    return {
        "schema": VIEW_SCHEMA,
        "length": len(records),
        "head": head,
        "log_sha256": log_sha256,
        "kinds": dict(sorted(kinds.items())),
        "first_seq": records[0]["seq"] if records else 0,
        "last_seq": records[-1]["seq"] if records else 0,
    }


def view_for_bytes(data: bytes) -> dict:
    records = parse_log(data, "<bytes>")
    return compute_view(records, hashlib.sha256(data).hexdigest())


def view_for_path(path: str) -> dict:
    data = read_log_bytes(path)
    records = parse_log(data, path)
    return compute_view(records, hashlib.sha256(data).hexdigest())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "DEFAULT_MAX_BYTES", "DEFAULT_TIMEOUT", "GENESIS", "GOSSIP_SCHEMA",
    "HEX_LEN", "PROOF_SCHEMA", "PROOF_SUFFIX", "PROOF_VERIFY_SCHEMA",
    "TOOL", "TOOL_DIR", "TRANSPARENCY_LOG", "VERSION", "VERIFY_SCHEMA",
    "VIEW_SCHEMA", "WITNESS_SCHEMA", "WITNESS_SUFFIX",
    "WITNESS_VERIFY_SCHEMA", "TlogError", "canon_bytes", "chain_of",
    "compute_view", "is_hex64", "load_log", "parse_log", "read_log_bytes",
    "sha256_bytes", "shape_error", "view_for_bytes", "view_for_path",
]
