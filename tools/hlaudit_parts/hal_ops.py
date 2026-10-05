"""The privilege inventory and the one `record()` call — Stage 112.

A privileged operation is one that (a) writes toolchain state the
user did not name as a direct output — a lockfile, a manifest edit,
a publish claim; (b) mints or exercises signing authority — a
keypair, a release signature, a witness, a checkpoint; or (c) runs
code under enforcement — the sandbox launcher and its release gate.
Everything else in the toolchain is read-only, and read-only is
exactly what stays out of this log: verification never audits
itself.

The inventory is a CLOSED vocabulary — every op name that exists is
a constant here, dotted `family.action` by the tool that owns it.
A new privileged operation joins by adding its constant and calling
`record()` at the point the op completes; the audit report groups by
these names, so ad-hoc strings would shred the summary.

The contract every tool runs at its privileged boundary:

  record(OP, "ok", subject=..., detail=...)        # the op completed
  record(OP, "refused", detail={"reason": ...})    # the op did not

`record` never raises by default: the audit log is forensic, not a
gate, and a disk that says no must not make the toolchain unusable —
it warns on stderr, once, with the reason (HLS_AUDIT_STRICT flips
this to fail-closed for the environments that want it). The refusal
path is the interesting one: a refused op gets the SAME hash-chained
entry as a success, outcome "refused", the gate's reason in the
detail. Probing shows up in the log as probing.
"""
from __future__ import annotations

import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_TOOLS_DIR,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hlaudit_parts import hal_log                     # noqa: E402
from hlaudit_parts.hal_common import (                # noqa: E402
    AuditlogError, ENTRY_KIND, OUTCOME_OK, OUTCOME_REFUSED, TOOL,
    VERSION, audit_log_path, current_actor, strict_mode,
)

# ---------------------------------------------------------------------------
# The inventory — closed, dotted, one constant per privileged op.
# ---------------------------------------------------------------------------

PKG_INIT = "pkg.init"                # hls-pkg init — creates a package tree
PKG_ADD = "pkg.add"                  # hls-pkg add — edits the manifest
PKG_LOCK = "pkg.lock"                # hls-pkg lock — writes the lockfile
PKG_PUBLISH = "pkg.publish"          # hls-pkg publish — chains the claim
SIGN_KEYGEN = "sign.keygen"          # hls-sign keygen — mints a keypair
SIGN_SIGN = "sign.sign"              # hls-sign sign — signs a payload
SIGN_RELEASE = "sign.release"        # hls-sign release — signs a package
SANDBOX_RUN = "sandbox.run"          # hls-sandbox run — armed execution
SANDBOX_RELEASE = "sandbox.release"  # hls-sandbox release — pins posture
TLOG_WITNESS = "tlog.witness"        # hls-tlog witness — signs a view
AUDIT_CHECKPOINT = "auditlog.checkpoint"  # hls-auditlog sign — pins head

ALL_OPS = (
    PKG_INIT, PKG_ADD, PKG_LOCK, PKG_PUBLISH,
    SIGN_KEYGEN, SIGN_SIGN, SIGN_RELEASE,
    SANDBOX_RUN, SANDBOX_RELEASE, TLOG_WITNESS, AUDIT_CHECKPOINT,
)


def record(op: str, outcome: str, subject: str = None, detail: dict = None,
           tool: str = None, tool_version: str = None,
           log_path: str = None, actor: str = None) -> bool:
    """Write one privileged-op entry. Returns True when the entry
    landed. The default contract is fail-open: an OSError warns on
    stderr and returns False; strict mode raises. A refused op is
    recorded exactly like an ok one — the outcome field is the only
    difference — so the caller's control flow never branches on
    whether the audit write worked (except in strict mode, where it
    must)."""
    if op not in ALL_OPS:
        # Not a caller-facing refusal: a programming error at the
        # hook site. Say so loudly on stderr and move on — the op
        # itself must not crash for the log's sake (fail-open).
        sys.stderr.write("hls-auditlog: %r is not in the privilege "
                         "inventory — the entry was not written\n" % op)
        return False
    entry = {
        "kind": ENTRY_KIND,
        "op": op,
        "actor": actor or current_actor(),
        "outcome": outcome,
        "tool": tool or _caller_tool(),
        "tool_version": tool_version or VERSION,
    }
    if subject is not None:
        entry["subject"] = str(subject)
    if detail:
        entry["detail"] = _small(detail)
    path = log_path or audit_log_path()
    try:
        hal_log.append_entry(path, entry)
        return True
    except OSError as ex:
        if strict_mode():
            raise AuditlogError(
                "the audit entry for %s was not written: %s" % (op, ex))
        sys.stderr.write("hls-auditlog: warning: the audit entry for "
                         "%s was not written (%s)\n" % (op, ex))
        return False


def record_refused(op: str, reason: str, subject: str = None,
                   detail: dict = None, **kw) -> bool:
    """The refusal shorthand every CLI handler calls from its except
    block. The reason is capped (a traceback in an audit line helps
    nobody) and the outcome is fixed — this is the vocabulary's
    second word, not a free string."""
    d = {"reason": (reason or "<no reason given>")[:400]}
    if detail:
        d.update(_small(detail))
    return record(op, OUTCOME_REFUSED, subject=subject, detail=d, **kw)


def _caller_tool() -> str:
    """The tool that called record(): the first frame outside this
    package whose file is a tool script (`hls-*.py`, `hl*.py`). A
    hook site never passes its own name — the log reads it off the
    stack, one definition of tool identity."""
    import inspect
    frame = inspect.currentframe()
    try:
        while frame is not None:
            fn = frame.f_globals.get("__file__", "")
            base = os.path.basename(fn)
            stem = base[:-3] if base.endswith(".py") else base
            if stem and "hlaudit_parts" not in fn \
                    and (base.startswith("hls-") or base.startswith("hl")) \
                    and stem not in ("hal_ops", "hal_log", "hal_common",
                                     "hal_checkpoint", "__init__"):
                return stem
            frame = frame.f_back
        return TOOL
    except Exception:                             # pragma: no cover
        return TOOL
    finally:
        del frame


def _small(detail: dict, cap: int = 32) -> dict:
    """The detail dict, kept small and deterministic: string keys,
    JSON-scalars or short string lists, capped size — an audit entry
    is a fact, not a dump."""
    out = {}
    for k in sorted(detail.keys(), key=str)[:cap]:
        v = detail[k]
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
        elif isinstance(v, (list, tuple, set)):
            vals = [str(x) for x in list(v)[:16]]
            out[str(k)] = vals
        elif isinstance(v, dict):
            out[str(k)] = {str(kk): _scalar(vv) for kk, vv
                           in list(v.items())[:16]}
        else:
            out[str(k)] = str(v)
    return out


def _scalar(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in list(v)[:16]]
    return str(v)


__all__ = [
    "ALL_OPS", "AUDIT_CHECKPOINT", "PKG_ADD", "PKG_INIT", "PKG_LOCK",
    "PKG_PUBLISH", "SANDBOX_RELEASE", "SANDBOX_RUN", "SIGN_KEYGEN",
    "SIGN_RELEASE", "SIGN_SIGN", "TLOG_WITNESS", "record",
    "record_refused",
]
