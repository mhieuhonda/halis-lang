"""log - verbatim segment of the original tools/hls-pkg.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hpkg_common import (
    Dict, List, Optional, TRANSPARENCY_LOG, hashlib, json, sys, time,
)

def transparency_log_append(record: Dict) -> Dict:
    """Append a record to the transparency log; return the entry with
    `seq`, `timestamp`, and `prev_hash` filled in.

    The log is append-only: we open the file in `a+b` mode so concurrent
    writers don't truncate. The chain hash makes any silent mutation of
    a past record detectable (rewriting line N breaks line N+1's
    prev_hash).

    Deep-scan-15 fix (MEDIUM severity, concurrency): the previous
    implementation read the previous head's hash, computed a new
    record, and appended — all WITHOUT holding a lock. Two concurrent
    `hls-pkg publish` (or `hls-pkg lock`) invocations would both
    read the same prev_hash, both compute new records chained from
    it, and both append — forking the chain. Verification would then
    flag one of them as a rollback. Fix: hold an exclusive `flock`
    (or `msvcrt.locking` on Windows) on a sidecar lock file for the
    whole read-compute-append critical section so only one process
    appends at a time.
    """
    import contextlib
    # Open the lock file (creating it if needed) and acquire an
    # exclusive lock for the whole critical section. The lock file
    # is separate from the log so the log itself can stay in `a+b`.
    lock_path = TRANSPARENCY_LOG + ".lock"
    with contextlib.closing(open(lock_path, "a+b")) as lockf:
        _acquire_exclusive_lock(lockf)
        try:
            prev_hash = _read_chain_head_hash()
            record = _build_chained_record(record, prev_hash)
            # Append atomically: open in `ab` mode (O_APPEND on POSIX
            # guarantees each write() is atomic up to PIPE_BUF).
            with open(TRANSPARENCY_LOG, "ab") as f:
                f.write((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            _release_exclusive_lock(lockf)
    return record


def _acquire_exclusive_lock(lockf) -> None:
    """Acquire an exclusive lock on `lockf` (cross-platform)."""
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.locking(lockf.fileno(), msvcrt.LK_LOCK, 1)
        except (ImportError, OSError):
            # Best-effort fallback on Windows without msvcrt — the
            # caller should still be correct because the file open
            # mode `a+b` doesn't truncate. The lock is advisory only.
            pass
    else:
        import fcntl
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)


def _release_exclusive_lock(lockf) -> None:
    """Release the exclusive lock previously acquired on `lockf`."""
    if sys.platform == "win32":
        try:
            import msvcrt
            msvcrt.locking(lockf.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            pass
    else:
        import fcntl
        fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def _read_chain_head_hash() -> str:
    """Return the chain_hash of the last record in the transparency
    log (or "0"*64 if the log is empty / corrupt)."""
    # Deep-scan-7 fix: the old code read only the last 4 KB of the log
    # and split by newlines — if the LAST record was >4 KB (e.g. a
    # large package hash with a long `deps` field), json.loads failed
    # silently and prev_hash stayed "0"*64, breaking the chain on
    # every subsequent entry. Read the WHOLE file (it's small — a
    # few KB per record is typical, a few thousand records is the
    # practical limit).
    #
    # Deep-scan-12 fix (DSS-T-07): the previous chunked-tail read
    # (8KB at a time, walking backward) could prematurely break
    # when it encountered the FIRST `\n` in the chunk — but the line
    # AFTER that `\n` might be the TAIL of a record whose HEAD is
    # earlier in the file. json.loads on that partial line failed,
    # the except swallowed it, and prev_hash stayed "0"*64, breaking
    # the chain on every subsequent entry whenever the last record
    # was >8KB. Reading the whole file is simpler and correct (the
    # log's own docstring says it's small).
    prev_hash = "0" * 64
    try:
        with open(TRANSPARENCY_LOG, "rb") as f:
            data = f.read()
        # Walk the lines from the end; the last non-empty line is the
        # most recent record (whose chain_hash is what we chain from).
        lines = [ln for ln in data.split(b"\n") if ln.strip()]
        if lines:
            last = json.loads(lines[-1].decode("utf-8"))
            prev_hash = last.get("chain_hash", prev_hash)
    except (FileNotFoundError, OSError, ValueError):
        # First record (or corrupted log) — chain from genesis.
        pass
    return prev_hash


def _build_chained_record(record: Dict, prev_hash: str) -> Dict:
    """Build a chained transparency-log record: assign seq/timestamp/
    prev_hash, then compute and attach the chain_hash field."""
    record = dict(record)
    record["seq"] = _next_seq()
    record["timestamp"] = int(time.time())
    record["prev_hash"] = prev_hash
    # Compute the chain hash: SHA-256 over prev_hash || canonical-JSON(record)
    # minus the chain_hash field (which we add last).
    canon = json.dumps(record, sort_keys=True, separators=(",", ":"))
    chain = hashlib.sha256((prev_hash + canon).encode("utf-8")).hexdigest()
    record["chain_hash"] = chain
    return record


def _next_seq() -> int:
    """Return the next sequence number for the transparency log."""
    try:
        with open(TRANSPARENCY_LOG, "rb") as f:
            count = 0
            for _ in f:
                count += 1
            return count + 1
    except (FileNotFoundError, OSError):
        return 1


def transparency_log_lookup(name: str, version: Optional[str] = None) -> Optional[Dict]:
    """Return the latest log entry for `name` (optionally matching
    `version`), or None if not found. Walks the log from the END backwards
    for efficiency on long logs."""
    try:
        with open(TRANSPARENCY_LOG, "rb") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return None
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln.decode("utf-8"))
        except ValueError:
            continue
        if rec.get("name") == name:
            if version is None or rec.get("version") == version:
                return rec
    return None


def transparency_log_verify_chain() -> List[str]:
    """Verify the chain hashes of the transparency log; return a list of
    error messages (empty list = log is sound)."""
    errors = []
    try:
        with open(TRANSPARENCY_LOG, "rb") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return []  # No log = nothing to verify.
    prev_hash = "0" * 64
    expected_seq = 1
    for i, ln in enumerate(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln.decode("utf-8"))
        except ValueError as ex:
            errors.append("line %d: invalid JSON (%s)" % (i + 1, ex))
            continue
        if rec.get("prev_hash") != prev_hash:
            errors.append("line %d (%s): prev_hash mismatch (expected %s, got %s)"
                          % (i + 1, rec.get("name", "?"), prev_hash[:12],
                             str(rec.get("prev_hash"))[:12]))
        if rec.get("seq") != expected_seq:
            errors.append("line %d (%s): seq mismatch (expected %d, got %s)"
                          % (i + 1, rec.get("name", "?"), expected_seq,
                             rec.get("seq")))
        # Recompute the chain hash over the record minus chain_hash.
        canon_rec = {k: v for k, v in rec.items() if k != "chain_hash"}
        canon = json.dumps(canon_rec, sort_keys=True, separators=(",", ":"))
        expected_chain = hashlib.sha256(
            (prev_hash + canon).encode("utf-8")).hexdigest()
        if rec.get("chain_hash") != expected_chain:
            errors.append("line %d (%s): chain_hash mismatch"
                          % (i + 1, rec.get("name", "?")))
        prev_hash = rec.get("chain_hash", prev_hash)
        expected_seq += 1
    return errors


# ---------------------------------------------------------------------------
# Manifest parsing (minimal TOML parser — supports the subset we use).
# ---------------------------------------------------------------------------



__all__ = [
    "_acquire_exclusive_lock",
    "_build_chained_record",
    "_next_seq",
    "_read_chain_head_hash",
    "_release_exclusive_lock",
    "transparency_log_append",
    "transparency_log_lookup",
    "transparency_log_verify_chain",
]
