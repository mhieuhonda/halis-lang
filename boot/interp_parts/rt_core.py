"""Stage-0 evaluator for HLS (v0.3) — adds enum, match, `?` operator.

Runtime values:
  int   -> int (Python, kept within int64 range by checked arithmetic)
  float -> float, bool -> bool, str -> bytes
  list[T]      -> list
  map[str,T]   -> dict (insertion-ordered), struct -> dict {field: value}
  enum         -> dict {"enum": <name>, "var": <variant>, "data": [payloads]}
  Chan[T]      -> HLChan (Stage 16) — shared FIFO queue, GIL-guarded
  Task[R]      -> HLTask (Stage 16) — thread join handle
  Future[T]    -> HLChan (Stage 33) — same as Chan, cap 1, bounded
  Stream[T]    -> HLChan (Stage 34) — same as Chan, cap N, bounded
"""
import ctypes
import math
import os
import platform
import subprocess
import sys
import threading
import time

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
B_LOW = bytes(range(0x21))  # bytes <= 0x20 used by trim

# Stage 34 (v0.53.0-alpha): the end-of-stream sentinel for int streams.
# INT64_MIN is chosen because it is the least likely "real" value in a
# stream of computed integers (it only arises from INT64_MIN literal or
# overflow, both of which are rare and would be bugs to send through a
# stream anyway). The combinator workers and the user's producer both
# use this sentinel to signal "no more values".
INT64_MIN_SENTINEL = INT64_MIN

# Stage 10 release: process-global sandbox root. When non-None, all
# filesystem builtins (read_file, read_file_tainted, write_file,
# file_exists) reject any path that does not resolve INSIDE this
# directory. The sandbox is set via boot.py --sandbox DIR. The C
# runtime mirrors this with hl_set_sandbox_root().
SANDBOX_ROOT = None


def _set_sandbox_root(path):
    """Set the process-global sandbox root. Called by boot.py --sandbox."""
    global SANDBOX_ROOT
    if path is None:
        SANDBOX_ROOT = None
        return
    # Canonicalise to an absolute, symlink-resolved path so that
    # ../escape attempts are caught after resolution (NOT before,
    # which would still allow the symlink to point outside).
    SANDBOX_ROOT = os.path.realpath(path)


def _sandbox_check(path_bytes):
    """If SANDBOX_ROOT is set, verify that `path_bytes` resolves inside
    the sandbox. Raises HLPanic with a clean message otherwise.

    Returns the RESOLVED path (bytes) so callers can open() the
    already-resolved path — avoiding a TOCTOU race where a symlink
    inside the sandbox is swapped for one pointing outside between
    the check and the open().

    The check is performed AFTER realpath resolution, so paths like
    "../etc/passwd" or symlinks pointing outside the sandbox are
    rejected. We DO allow the path to NOT exist (e.g. file_exists
    probing) — we just check that IF it existed, it would be inside
    the sandbox.

    SCAN-A fix: decode bytes as latin-1 (a 1:1 byte->str mapping) so
    the realpath comparison runs on the SAME byte sequence as `open()`
    will use. The previous `errors="replace"` decoded non-UTF-8 bytes
    to U+FFFD, so the realpath check ran on a different path than the
    actual open() call — a non-UTF-8-named symlink inside the sandbox
    pointing outside wasn't caught by the check, but `open()` would
    still follow it.
    """
    if SANDBOX_ROOT is None:
        return path_bytes if isinstance(path_bytes, bytes) \
            else str(path_bytes).encode("utf-8")
    # Deep-scan-10 fix: the whole check now runs on BYTES (realpath
    # accepts and returns bytes — a byte-exact round trip). The old
    # code decoded bytes as latin-1 to str and called realpath on the
    # STR, which re-encoded via the UTF-8 filesystem encoding — for
    # non-UTF-8 path bytes (e.g. b"\xff"), the realpath check ran on a
    # DIFFERENT byte sequence than the open() call used, so a symlink
    # named with such bytes inside the sandbox and pointing outside
    # escaped the check while open() still followed it.
    p = path_bytes if isinstance(path_bytes, bytes) \
        else str(path_bytes).encode("utf-8")
    if not os.path.isabs(p):
        p = os.path.join(os.getcwd().encode("utf-8"), p)
    # realpath resolves symlinks; if the path does not exist, it
    # resolves as far as possible (the existing prefix) and leaves
    # the rest verbatim. That is enough: if any component of the
    # existing prefix points outside the sandbox, we reject.
    resolved = os.path.realpath(p)
    # Common-prefix check: SANDBOX_ROOT must be a prefix of `resolved`,
    # AND the byte after the prefix must be a separator (or end of
    # string) — otherwise "/sandbox_evil" would be allowed inside
    # "/sandbox".
    sb = SANDBOX_ROOT.encode("utf-8")
    if resolved == sb:
        return resolved
    if not resolved.startswith(sb + b"/"):
        # Late import: to_display lives in rt_num (which imports rt_core),
        # so a module-level import here would be circular. Only needed on
        # this error path, so resolve it lazily.
        from .rt_num import to_display
        raise HLPanic("sandbox violation: path '%s' resolves outside the sandbox"
                      % to_display(path_bytes), 0)
    return resolved


class HLPanic(Exception):
    def __init__(self, msg, line):
        super().__init__(msg)
        self.msg = msg
        self.line = line


class ReturnSig(Exception):
    def __init__(self, value):
        self.value = value


class TailCallSig(Exception):
    """Stage 31 (v0.48.0-alpha): a VERIFIED tail self-call raised by the
    return handler of a #[tail_call] function. call_fn catches it,
    rebinds the parameters and re-runs the body in the SAME Python
    frame — the interpreter-side mirror of the native parameter-
    rebinding goto. A 1,000,000-deep tail recursion costs ZERO Python
    stack frames.

    Only self-calls raise it (the return handler checks the call target
    against the fn on top of the thread-local fn stack), so tc.key is
    always the currently-executing function.
    """

    def __init__(self, key, args, line=0):
        super().__init__("tail call to %s" % key)
        self.key = key
        self.args = args
        self.line = line


class BreakSig(Exception):
    pass


class ContinueSig(Exception):
    pass


# ----------------------------------------------------------------------------
# Stage 16 (v0.27.0-alpha): the concurrency runtime (interpreter side).
#
# `spawn` creates a REAL Python thread per task. Channels are MPMC FIFO
# queues: unbounded by default, or BOUNDED via chan_new_bounded(cap) —
# a bounded channel's send blocks while it holds `cap` messages
# (backpressure; Stage-16 perfection, v0.29.0-alpha). All channel/task
# state is guarded by one global mutex + condition variable (the
# interpreter's equivalent of the native C runtime's g_rt_mu / g_rt_cv).
#
# Deadlock detection (perfected in v0.29.0-alpha): the detector fires
# when every thread that could produce work is blocked (in
# recv/select/join — and now also in a full-channel send). The old
# additional condition `no messages pending anywhere` was both
# unnecessary and incomplete: a blocked receiver's channel is provably
# empty at block time (it re-checks under the same lock before every
# wait), so pending messages can only sit on channels with NO waiter —
# and all potential producers are blocked. `blocked == alive` is
# therefore a sound AND complete deadlock condition in this design:
#   * it cannot fire spuriously — deadlock_check runs with the lock
#     held, so no thread can be mid-operation (uncounted) at that
#     moment, and a thread whose wait condition is already satisfied
#     never increments `blocked`;
#   * it now catches deadlock cycles that the old `msgs == 0` guard
#     missed (e.g. a task blocked sending to a full channel nobody
#     consumes).
# The native C runtime mirrors this condition exactly.
#
# Panics and exit()s inside a task halt the WHOLE process (safe-halt
# semantics: tasks share the process fate). The interpreter achieves
# this with os._exit after flushing, because an uncaught exception
# would otherwise only kill the thread.
# ----------------------------------------------------------------------------


__all__ = [
    "B_LOW",
    "BreakSig",
    "ContinueSig",
    "HLPanic",
    "INT64_MAX",
    "INT64_MIN",
    "INT64_MIN_SENTINEL",
    "ReturnSig",
    "SANDBOX_ROOT",
    "TailCallSig",
    "_sandbox_check",
    "_set_sandbox_root",
    "ctypes",
    "math",
    "os",
    "platform",
    "subprocess",
    "sys",
    "threading",
    "time",
]
