"""The audit log's chain: append, load, verify — Stage 112.

The writer runs the SAME critical section the transparency ledger has
run since Stage 13: an exclusive flock on a sidecar lock file for the
whole read-compute-append (the deep-scan-15 fix, inherited), the
previous head read from the WHOLE file (the deep-scan-7 and -12
lessons, inherited — a chunked tail read breaks when the last record
is bigger than the chunk), an O_APPEND write, and the chain hash over
the family's one definition. None of that is re-derived here: the
hash is `chain_of` imported from hltlog_parts, and the locking is
modeled on hpkg_log's, so a power user comparing the two writers sees
one discipline, not two.

Verification is the transparency log's too: replay from genesis, seq
counting 1..N with no gaps, every prev_hash chaining, every chain_hash
recomputing. `verify` returns the head and every break with its line,
and keeps replaying after a break so one report names every break in
the file — a log that was attacked in three places is reported once,
completely.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_TOOLS_DIR, os.path.join(_TOOLS_DIR, "hpkg_parts"),
           os.path.join(_TOOLS_DIR, "hltlog_parts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hlaudit_parts.hal_common import (                   # noqa: E402
    AuditlogError, GENESIS, ENTRY_KIND, entry_shape_error,
)
from hltlog_parts.hltlog_common import (                 # noqa: E402
    canon_bytes, chain_of, is_hex64,
)


def read_log_bytes(path: str) -> bytes:
    if not os.path.isfile(path):
        raise AuditlogError("no audit log at %s — nothing to read" % path)
    with open(path, "rb") as f:
        return f.read()


def parse_log(data: bytes, origin: str) -> list:
    """Parse JSON-lines into audit entries. A line this tool cannot
    parse is evidence of tampering or corruption, never something to
    skip past — the same rule the transparency log applies."""
    records = []
    text = data.decode("utf-8", errors="replace")
    for i, raw in enumerate(text.split("\n")):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json_loads(line)
        except ValueError as ex:
            raise AuditlogError("%s: line %d is not JSON (%s)"
                                % (origin, i + 1, ex))
        entry_shape_error(rec, origin, i + 1)
        records.append(rec)
    return records


def load_log(path: str) -> list:
    """Read + parse an audit log in one call."""
    return parse_log(read_log_bytes(path), path)


def _read_chain_head_hash(path: str) -> str:
    """The last record's chain_hash, from the WHOLE file — genesis
    when the log is empty (or not there yet; the writer creates it)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (FileNotFoundError, OSError):
        return GENESIS
    lines = [ln for ln in data.split(b"\n") if ln.strip()]
    if not lines:
        return GENESIS
    try:
        last = json_loads(lines[-1].decode("utf-8"))
        return last.get("chain_hash") or GENESIS
    except (ValueError, UnicodeDecodeError):
        return GENESIS


def _next_seq(path: str) -> int:
    try:
        with open(path, "rb") as f:
            count = 0
            for _ in f:
                count += 1
            return count + 1
    except (FileNotFoundError, OSError):
        return 1


def _acquire_exclusive_lock(lockf) -> None:
    if sys.platform == "win32":                   # pragma: no cover
        try:
            import msvcrt
            msvcrt.locking(lockf.fileno(), msvcrt.LK_LOCK, 1)
        except (ImportError, OSError):
            pass
    else:
        import fcntl
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)


def _release_exclusive_lock(lockf) -> None:
    if sys.platform == "win32":                   # pragma: no cover
        try:
            import msvcrt
            msvcrt.locking(lockf.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            pass
    else:
        import fcntl
        fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def append_entry(path: str, entry: dict) -> dict:
    """Append one audit entry, chained. The writer fills `seq`,
    `timestamp`, `prev_hash` and `chain_hash`; the caller's fields
    (`kind`, `op`, `actor`, `subject`, `outcome`, `tool`,
    `tool_version`, `detail`) are copied, never aliased. Concurrent
    writers serialize on the sidecar lock file, exactly like the
    transparency ledger's."""
    entry = dict(entry)
    if "chain_hash" in entry:
        raise AuditlogError("the caller does not mint chain hashes")
    lock_path = path + ".lock"
    # No directory pre-check: a missing directory surfaces as the
    # OS's own FileNotFoundError from the lock-file open — the
    # caller's fail-open/strict contract handles it either way.
    with contextlib.closing(open(lock_path, "a+b")) as lockf:
        _acquire_exclusive_lock(lockf)
        try:
            prev = _read_chain_head_hash(path)
            entry["seq"] = _next_seq(path)
            entry["timestamp"] = int(time.time())
            entry["prev_hash"] = prev
            entry["chain_hash"] = chain_of(prev, entry)
            with open(path, "ab") as f:
                f.write((json_dumps_line(entry) + "\n").encode("utf-8"))
        finally:
            _release_exclusive_lock(lockf)
    return entry


def verify_log(records: list, origin: str = "<audit log>"):
    """Replay the chain. Returns (head, errors): head is the last
    record's chain_hash (GENESIS for an empty log); errors is a list
    of (where, message) — empty when the log is sound. The replay
    continues after a break so one report names every break."""
    errors = []
    prev = GENESIS
    expect_seq = 1
    head = GENESIS
    for i, rec in enumerate(records):
        where = "%s: line %d" % (origin, i + 1)
        if rec.get("seq") != expect_seq:
            errors.append((where, "seq %r out of order (want %d)"
                           % (rec.get("seq"), expect_seq)))
            if isinstance(rec.get("seq"), int) \
                    and not isinstance(rec.get("seq"), bool):
                expect_seq = rec["seq"]
        if rec.get("prev_hash") != prev:
            errors.append((where, "prev_hash does not chain to the "
                           "record before it"))
        want = chain_of(rec.get("prev_hash", GENESIS), rec)
        if rec.get("chain_hash") != want:
            errors.append((where, "chain_hash does not match the "
                           "entry's content (recomputed %s...)"
                           % want[:12]))
        prev = rec.get("chain_hash", prev)
        head = rec.get("chain_hash", head)
        expect_seq += 1
    return head, errors


def summarize(records: list) -> dict:
    """The inventory a report (and a checkpoint) is made of: per-op
    counts, per-outcome counts, per-actor counts, the first and last
    op times. Deterministic — sorted keys everywhere."""
    ops, outcomes, actors = {}, {}, {}
    for rec in records:
        op = rec.get("op") or "<none>"
        ops[op] = ops.get(op, 0) + 1
        outcome = rec.get("outcome") or "<none>"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        actor = rec.get("actor") or "<none>"
        actors[actor] = actors.get(actor, 0) + 1
    stamps = [r["timestamp"] for r in records
              if isinstance(r.get("timestamp"), int)]
    return {
        "ops": dict(sorted(ops.items())),
        "outcomes": dict(sorted(outcomes.items())),
        "actors": dict(sorted(actors.items())),
        "first_timestamp": min(stamps) if stamps else None,
        "last_timestamp": max(stamps) if stamps else None,
    }


# JSON helpers — one line per entry, sorted keys, no spaces (the
# family's on-disk shape).

def json_loads(line: str) -> dict:
    return json.loads(line)


def json_dumps_line(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def entry_line(entry: dict) -> str:
    """The exact bytes one entry occupies in the log — the digest a
    checkpoint's detail pins for its own record."""
    return json_dumps_line(entry)


__all__ = [
    "append_entry", "entry_line", "load_log", "parse_log",
    "read_log_bytes", "summarize", "verify_log",
]
