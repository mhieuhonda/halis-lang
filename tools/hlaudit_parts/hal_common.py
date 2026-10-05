"""Shared shapes and constants for the audit log — Stage 112.

Two ledgers now live beside the tree, and keeping them apart in your
head is half the design:

  the TRANSPARENCY ledger  (`.hls-pkg-transparency.log`, Stage 13)
      records CLAIMS ABOUT PACKAGES: this version has this content
      hash, this SBOM lists these parts, this posture was pinned.
      Its records outlive the machine — they are what gossip carries.

  the AUDIT log            (`.hls-audit.log`, this stage)
      records OPERATIONS PERFORMED: on this machine, at this time,
      this actor ran this privileged operation and it succeeded or
      was REFUSED. Its records are the forensic trail — the answer
      to "who did what here, and what was tried and turned away".

A refused attempt is exactly as auditable as a success: the entry
vocabulary has two outcomes, "ok" and "refused", and a refusal is
never a reason to skip the record — it is the reason to write one.

An ENTRY is one JSON object per line with the same mandatory chain
fields every record in the family carries (`seq`, `timestamp`,
`prev_hash`, `chain_hash`), a `kind` of "op", and the operation's
own fields: `op` (the privilege inventory's dotted name), `actor`
("user@host"), `subject` (what the op acted on), `outcome`
("ok"/"refused"), `tool` + `tool_version` (who wrote it), and
`detail` (a small dict, the op's business).

The CHAIN HASH is the family's one definition — sha256(prev_hash +
canonical_json(record minus chain_hash)), byte-identical to
hpkg_log's writer since Stage 13. This module does not re-implement
it: it IMPORTS it (from hltlog_parts, which pinned the same promise
in Stage 107), so there is exactly one copy of the arithmetic in the
tree and this stage inherits its pinned test vectors.

Where the log lives: the repo root, next to the transparency ledger,
named `.hls-audit.log`. The environment overrides the path
(HLS_AUDIT_LOG) — a CI run or a test gate points every writer at a
scratch log without touching the tree's. Strict mode (HLS_AUDIT_STRICT
set to a non-empty value) flips the write contract from fail-open to
fail-closed: an unwritable audit log becomes a refused operation.
Fail-open is the default on purpose — the audit log is a forensic
record, not a gate; the toolchain stays usable when the disk says no,
and says so on stderr every time it had to.
"""
from __future__ import annotations

import getpass
import os
import socket
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_TOOLS_DIR, os.path.join(_TOOLS_DIR, "hpkg_parts"),
           os.path.join(_TOOLS_DIR, "hltlog_parts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hltlog_parts.hltlog_common import (          # noqa: E402,F401
    GENESIS, canon_bytes, chain_of, is_hex64,
)

TOOL = "hls-auditlog"
VERSION = "0.131.0-alpha"          # the toolchain release this run stamps

ENTRY_KIND = "op"
OUTCOME_OK = "ok"
OUTCOME_REFUSED = "refused"
OUTCOMES = (OUTCOME_OK, OUTCOME_REFUSED)

CHECKPOINT_SCHEMA = "hls-audit-checkpoint/v1"
CHECKPOINT_KIND = "halis-audit-checkpoint"
LIST_SCHEMA = "hls-audit-list/v1"
VERIFY_SCHEMA = "hls-audit-verify/v1"
SHOW_SCHEMA = "hls-audit-show/v1"
SIGN_SCHEMA = "hls-audit-sign/v1"
CHECKPOINT_VERIFY_SCHEMA = "hls-audit-verify-sign/v1"

CHECKPOINT_SUFFIX = ".ckpt.json"
SIG_SUFFIX = ".minisig"

REPO_ROOT = os.path.dirname(_TOOLS_DIR)
DEFAULT_LOG_NAME = ".hls-audit.log"


class AuditlogError(Exception):
    """A refusal the audit tooling reports on stderr with exit 1."""


def audit_log_path() -> str:
    """The audit log this run writes and reads: the environment's
    HLS_AUDIT_LOG when set, else the repo root's `.hls-audit.log` —
    the same fixed-by-convention path the transparency ledger uses,
    so every tool finds every other tool's entries."""
    return os.environ.get("HLS_AUDIT_LOG") or os.path.join(
        REPO_ROOT, DEFAULT_LOG_NAME)


def strict_mode() -> bool:
    """Fail-closed auditing is opt-in: HLS_AUDIT_STRICT set (to
    anything) makes an unwritable audit log refuse the operation
    instead of warning about it."""
    return bool(os.environ.get("HLS_AUDIT_STRICT"))


def current_actor() -> str:
    """The actor line, "user@host" — who the OS says is running the
    tool. HLS_AUDIT_ACTOR overrides it (a CI run stamps its job
    identity; the acceptance gate pins determinism with it). No
    secrets, no paths — an actor names a principal, not a machine."""
    env = os.environ.get("HLS_AUDIT_ACTOR")
    if env:
        return env
    try:
        user = getpass.getuser()
    except Exception:                             # pragma: no cover
        user = "<unknown-user>"
    try:
        host = socket.gethostname() or "<unknown-host>"
    except Exception:                             # pragma: no cover
        host = "<unknown-host>"
    return "%s@%s" % (user, host)


def entry_shape_error(entry, origin: str, line_no: int):
    """The entry shape beyond the family's mandatory chain fields:
    a `kind` of "op", a non-empty dotted `op` name, a non-empty
    `actor`, an outcome from the two-word vocabulary, and a `detail`
    that is an object when present. The chain arithmetic needs the
    four mandatory fields; the audit report needs these."""
    from hltlog_parts.hltlog_common import shape_error
    shape_error(entry, origin, line_no)            # the family's fields
    what = "%s: line %d" % (origin, line_no)
    if entry.get("kind") != ENTRY_KIND:
        raise AuditlogError("%s has kind %r, expected %r"
                            % (what, entry.get("kind"), ENTRY_KIND))
    op = entry.get("op")
    if not isinstance(op, str) or not op or not op.replace(".", "").replace(
            "_", "").isalnum() or op.startswith(".") or op.endswith("."):
        raise AuditlogError("%s has a bad op name (%r)" % (what, op))
    if not isinstance(entry.get("actor"), str) or not entry.get("actor"):
        raise AuditlogError("%s has no actor — an op without a principal "
                            "is not an audit record" % what)
    if entry.get("outcome") not in OUTCOMES:
        raise AuditlogError("%s has outcome %r, expected one of %s"
                            % (what, entry.get("outcome"),
                               "/".join(OUTCOMES)))
    if "detail" in entry and not isinstance(entry["detail"], dict):
        raise AuditlogError("%s has a non-object detail" % what)


__all__ = [
    "AuditlogError", "CHECKPOINT_KIND",
    "CHECKPOINT_SCHEMA", "CHECKPOINT_SUFFIX", "CHECKPOINT_VERIFY_SCHEMA",
    "ENTRY_KIND", "GENESIS", "LIST_SCHEMA", "OUTCOMES", "OUTCOME_OK",
    "OUTCOME_REFUSED", "REPO_ROOT", "SIG_SUFFIX", "SHOW_SCHEMA",
    "SIGN_SCHEMA", "TOOL", "VERIFY_SCHEMA", "VERSION",
    "audit_log_path", "canon_bytes", "chain_of", "current_actor",
    "entry_shape_error", "is_hex64", "strict_mode",
]
