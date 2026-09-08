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
class HLChan:
    """A channel value: FIFO message queue (interpreter representation).

    cap == 0 means unbounded (the default); cap >= 1 is a bounded
    channel whose send blocks while `cap` messages are pending.
    waiters = number of threads currently blocked in recv/select on
    THIS channel (bookkeeping for the deadlock detector — see
    ConcRuntime.deadlock_check).
    """

    __slots__ = ("q", "cap", "waiters", "send_waiters", "_registry")

    def __init__(self, cap=0):
        self.q = []  # list of values (messages); guarded by the runtime lock
        self.cap = cap  # 0 = unbounded; >= 1 = bounded (blocking send)
        self.waiters = 0  # recv/select waiters on this channel
        self.send_waiters = 0  # senders blocked on `full` (bounded only)
        self._registry = None  # ConcRuntime.chans list (set on register)

    def __del__(self):
        # Deregister from the runtime's live-channel registry. Removal
        # during another thread's registry iteration can make that
        # iterator skip an element — but a channel being collected here
        # holds no stack references, hence has no waiters, hence cannot
        # affect the deadlock scan's verdict (only channels with BOTH
        # pending messages AND waiters matter).
        reg = getattr(self, "_registry", None)
        if reg is not None:
            try:
                reg.remove(self)
            except (ValueError, AttributeError):
                pass


class HLTask:
    """A spawned task: Python thread + join state."""

    __slots__ = ("thread", "done", "joined", "result")

    def __init__(self, thread):
        self.thread = thread
        self.done = False
        self.joined = False
        self.result = None


class ConcRuntime:
    """Global state for spawn / channel operations (one per Interp)."""

    def __init__(self):
        self.mu = threading.Lock()
        self.cv = threading.Condition(self.mu)
        self.tasks_alive = 0   # spawned tasks not yet finished (main excluded)
        self.blocked = 0       # threads currently blocked in recv/select/join/send
        self.msgs = 0          # total pending messages across all channels
        self.next_id = 0
        # Live-channel registry (for the deadlock scan). HLChan.__del__
        # deregisters; see the soundness note there.
        self.chans = []

    def register(self, chan):
        with self.cv:
            self.chans.append(chan)
            chan._registry = self.chans

    def deadlock_check(self):
        """Call with the lock HELD, before blocking. Soundness contract:

        1. A thread counts itself as `blocked` ONLY while it holds no
           progress opportunity: every wait loop re-checks its condition
           under the same lock before blocking, and the check itself runs
           with the lock held (so no other thread can be mid-operation
           and uncounted at that instant).
        2. A woken-but-not-yet-rescheduled receiver is STILL counted in
           `blocked` (its decrement happens only after wait() re-acquires
           the lock) — which is exactly why `blocked == alive` alone is
           NOT sufficient: it can fire while a receiver's message is
           already pending but the receiver has not been scheduled yet.
           The waiter counters close this hole in BOTH directions:
           a channel with pending messages AND recv waiters, or a
           not-full bounded channel with send waiters, means some
           thread WILL make progress as soon as the lock is released.

        Deadlock iff: every thread is blocked AND no channel has a
        progress opportunity (pending message with a recv waiter, or
        free capacity with a send waiter). This also catches cycles the
        pre-v0.29 `msgs == 0` guard missed (e.g. a producer blocked on a
        full channel that nobody consumes). Mirrors
        hl_rt_deadlock_check() in C. Raises HLPanic (the `with self.cv:`
        caller releases the lock on unwind; the process is halting
        anyway)."""
        alive = self.tasks_alive + 1  # +1: the main thread
        if self.blocked != alive:
            return
        for c in self.chans:
            if c.q and c.waiters > 0:
                return  # a consumer can make progress once scheduled
            if c.send_waiters > 0 and not self._full(c):
                return  # a woken sender can proceed (capacity freed)
        raise HLPanic(
            "deadlock: all tasks are blocked on channel operations "
            "(no possible progress)", 0)

    def _full(self, chan):
        return chan.cap > 0 and len(chan.q) >= chan.cap

    def send(self, chan, value):
        """Blocking send: on a bounded channel, waits while full.
        Unbounded channels never block the sender."""
        with self.cv:
            while self._full(chan):
                chan.send_waiters += 1
                self.blocked += 1
                self.deadlock_check()
                self.cv.wait()
                self.blocked -= 1
                chan.send_waiters -= 1
            chan.q.append(value)
            self.msgs += 1
            self.cv.notify_all()

    def try_send(self, chan, value):
        """Non-blocking send: False iff a bounded channel is full (the
        value is NOT enqueued in that case); True otherwise."""
        with self.cv:
            if self._full(chan):
                return False
            chan.q.append(value)
            self.msgs += 1
            self.cv.notify_all()
            return True

    def recv(self, chan, line):
        with self.cv:
            while not chan.q:
                chan.waiters += 1
                self.blocked += 1
                self.deadlock_check()
                self.cv.wait()
                self.blocked -= 1
                chan.waiters -= 1
            value = chan.q.pop(0)
            self.msgs -= 1
            # A dequeue frees capacity on a bounded channel — wake any
            # sender blocked on `full` (v0.29.0-alpha: recv previously
            # never notified; with unbounded-only channels that was fine,
            # but bounded senders wait for exactly this signal).
            self.cv.notify_all()
            return value

    def recv_or(self, chan, default):
        """Non-blocking recv: the pending message if one exists, else
        `default` (ownership of the unused default stays with the
        caller in the interpreter — GC makes the release a no-op)."""
        with self.cv:
            if chan.q:
                value = chan.q.pop(0)
                self.msgs -= 1
                self.cv.notify_all()  # free capacity -> wake blocked senders
                return value
            return default

    def select(self, chans, line):
        with self.cv:
            if not chans:
                raise HLPanic("select() on empty channel list", line)
            while True:
                for i, c in enumerate(chans):
                    if c.q:
                        return i
                for c in chans:
                    c.waiters += 1
                self.blocked += 1
                self.deadlock_check()
                self.cv.wait()
                self.blocked -= 1
                for c in chans:
                    c.waiters -= 1

    def join(self, task, line):
        with self.cv:
            if task.joined:
                raise HLPanic("task already joined", line)
            while not task.done:
                self.blocked += 1
                self.deadlock_check()
                self.cv.wait()
                self.blocked -= 1
            task.joined = True
            return task.result

    def task_finished(self, task, result):
        with self.cv:
            task.result = result
            task.done = True
            self.tasks_alive -= 1
            self.cv.notify_all()


# ---------- int64 checked arithmetic ----------
def i64_add(a, b, line):
    r = a + b
    if r < INT64_MIN or r > INT64_MAX:
        raise HLPanic("integer overflow", line)
    return r


def i64_sub(a, b, line):
    r = a - b
    if r < INT64_MIN or r > INT64_MAX:
        raise HLPanic("integer overflow", line)
    return r


def i64_mul(a, b, line):
    r = a * b
    if r < INT64_MIN or r > INT64_MAX:
        raise HLPanic("integer overflow", line)
    return r


def i64_neg(a, line):
    if a == INT64_MIN:
        raise HLPanic("integer overflow", line)
    return -a


def i64_div(a, b, line):
    if b == 0:
        raise HLPanic("division by zero", line)
    if a == INT64_MIN and b == -1:
        raise HLPanic("integer overflow", line)
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def i64_mod(a, b, line):
    if b == 0:
        raise HLPanic("division by zero", line)
    if a == INT64_MIN and b == -1:
        raise HLPanic("integer overflow", line)
    r = abs(a) % abs(b)
    return r if a >= 0 else -r


def f64_div(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        # BUG-SC-6 fix: handle -0.0 divisor correctly. Previously the
        # sign test `(a > 0) == (b >= 0)` treated +0.0 and -0.0 the same
        # (both pass `>= 0`), producing the wrong sign of infinity when
        # the divisor was -0.0. Use math.copysign to distinguish them.
        if a == 0 or a != a:
            return float("nan")
        # copysign(1.0, x) returns 1.0 for +x (incl. +0.0) and -1.0 for -x
        # (incl. -0.0). Result is +inf iff signs of a and b agree.
        same_sign = math.copysign(1.0, a) == math.copysign(1.0, b)
        return float("inf") if same_sign else float("-inf")


def f64_mod(a, b):
    try:
        return math.fmod(a, b)
    except ValueError:
        return float("nan")


def parse_int(s, line):
    """Convert bytes -> int, matching the C semantics of builtin int()/to_int()."""
    n = len(s)
    i = 0
    neg = False
    if n > 0 and s[0:1] == b"-":
        neg = True
        i = 1
    if i >= n:
        raise HLPanic("cannot convert string to int", line)
    v = 0
    while i < n:
        c = s[i]
        if c < 48 or c > 57:
            raise HLPanic("cannot convert string to int", line)
        v = v * 10 + (c - 48)
        if v > 2 ** 63:
            raise HLPanic("integer too large when converting string", line)
        i += 1
    if neg:
        return -v
    if v > INT64_MAX:
        raise HLPanic("integer too large when converting string", line)
    return v


def fmt_float(v):
    return ("%.6f" % v).encode("ascii")


def to_display(b):
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return str(b)


# Stage 9 release (v0.20.0-alpha): HalisRNG — a 64-bit LCG shared between
# the Stage-0 interpreter and the native C runtime. Same constants, same
# bit-mask → same sequence for the same seed. Critical for differential
# testing (a test using rand_seed + rand_int / rand_float must produce
# identical output in both backends; otherwise the suite would fail).
#
# Algorithm: Knuth's LCG with the glibc/MMIX Taussian-Lewis constants.
#   state = state * 6364136223846793005 + 1442695040888963407   (mod 2^64)
#   rand_int(max) = state % max   (max > 0)
#   rand_float() = (state >> 11) / 2^53   (53 bits of randomness)
# The state is masked to 64 bits with & 0xFFFFFFFFFFFFFFFF to mirror C's
# uint64_t overflow. Seed 0 is normalised to 1 because xorshift-style
# alternatives would not — but the LCG actually accepts 0 (it just stays
# at 0x...407 forever); we normalise anyway so the seed "0" does not
# produce a degenerate sequence.
class HalisRNG:
    MASK = (1 << 64) - 1
    A = 6364136223846793005
    C = 1442695040888963407

    def __init__(self):
        self.state = 1  # nonzero default; same as native runtime

    def seed(self, s):
        # HLS ints are 64-bit signed; mask to 64 bits to mirror C uint64.
        self.state = s & self.MASK
        if self.state == 0:
            self.state = 1

    def _next(self):
        self.state = (self.state * self.A + self.C) & self.MASK
        return self.state

    def randrange(self, max):
        # Caller guarantees max > 0 (the checker raises otherwise).
        return self._next() % max

    def random(self):
        # 53 bits of randomness — full precision of an IEEE double's
        # significand. Matches the native runtime's calculation.
        return (self._next() >> 11) / (1 << 53)


# Stage 21 (v0.37.0-alpha): target-feature state for has_feature() and
# the runtime CPU probe for simd_cpu_supports().
_TARGET_FEATURES = set()


def _set_target_feature(feat):
    """Set the active --target-feature (normalised, no leading '+')."""
    _TARGET_FEATURES.clear()
    if feat:
        _TARGET_FEATURES.add(feat)


def _cpu_supports(feat):
    """Probe the host CPU for a SIMD feature (interpreter side; the
    native side uses __builtin_cpu_supports). /proc/cpuinfo on Linux
    x86; NEON is baseline on aarch64; rvv checks __riscv_v macro on
    riscv64; False otherwise."""
    try:
        feat = feat.decode("utf-8") if isinstance(feat, bytes) else feat
    except Exception:
        return False
    import platform
    machine = platform.machine().lower()
    if feat == "neon":
        return machine in ("aarch64", "arm64", "armv8l")
    # Stage 26 (v0.49.0-alpha): RISC-V Vector extension. The probe is
    # based on the `Features` line in /proc/cpuinfo (Linux RISC-V
    # exposes the V extension as `v` in the Features list) plus the
    # platform machine check. The __riscv_v macro is defined by the C
    # compiler when -march includes the V extension; the interpreter
    # has no equivalent compile-time probe, so the runtime probe is
    # the only signal (best-effort: returns False if /proc/cpuinfo is
    # unreadable, e.g. on macOS/BSD where the V extension would be
    # queried differently).
    if feat == "rvv":
        if machine not in ("riscv64", "rv64", "riscv"):
            return False
        try:
            with open("/proc/cpuinfo", "rb") as f:
                for line in f:
                    if line.startswith(b"Features") or line.startswith(b"isa"):
                        # Linux RISC-V /proc/cpuinfo exposes the ISA
                        # extensions as `isa: rv64imafdv` (lowercase,
                        # one string). The V extension is present
                        # when 'v' appears in the isa string after
                        # the base extensions. We check for "_v" or
                        # "v" as a standalone token.
                        isa = line.decode("utf-8", "replace").lower()
                        # Strip the "isa:" / "features:" prefix.
                        isa = isa.split(":", 1)[-1].strip()
                        # The V extension is denoted by 'v' in the
                        # isa string (e.g. "rv64imafdcv"). Check that
                        # it appears as a suffix token (not inside
                        # another extension name).
                        if "v" in isa:
                            return True
                        break
        except OSError:
            return False
        return False
    flags = ""
    try:
        with open("/proc/cpuinfo", "rb") as f:
            for line in f:
                if line.startswith(b"flags") or line.startswith(b"Features"):
                    flags = line.decode("utf-8", "replace").lower()
                    break
    except OSError:
        return False
    return (" %s " % feat) in (" %s " % flags.replace(",", " ")
                               .replace("       ", " ").replace(":", " "))


class Interp:
    def __init__(self, program, argv, out, contracts=False):
        self.p = program
        self.fns = program["fns"]
        self.structs = program["structs"]
        self.enums = program.get("enums", {})
        self.argv = argv  # list[bytes]
        self.out = out
        # Deep-scan-10 fix: `line` is now THREAD-LOCAL. It was a plain
        # attribute mutated by every thread's exec_stmt — with tasks
        # running on other threads, a panic raised by thread A could be
        # attributed with the line number thread B was currently
        # executing (GIL-interleaved), producing wrong panic locations
        # in concurrent programs. A property over threading.local keeps
        # the attribute protocol (self.line = X reads/writes) while
        # giving each thread its own view.
        self._tls = threading.local()
        self._tls.line = 0
        # Stage 31 (v0.48.0-alpha): thread-local stack of fn keys —
        # the return handler consults the top entry to detect a tail
        # SELF-call inside a #[tail_call] fn. Thread-local because tasks
        # run on real Python threads (same discipline as `line`).
        self._tls.fn_stack = []
        # Stage 17 (v0.28.0-alpha): runtime contract checking mode.
        # When True, `requires` is asserted at every fn entry and
        # `ensures` at every return (violations are clean panics).
        self.contracts = contracts
        # Stage 9 release (v0.20.0-alpha): process-wide PRNG state for the
        # Rand effect. Uses a 64-bit LCG with the same constants as the
        # native runtime (Knuth LCG: state = state * 6364136223846793005
        # + 1442695040888963407, masked to 64 bits). This makes the
        # sequence DETERMINISTIC across implementations: the same seed
        # produces the same sequence of ints and floats in both Stage-0
        # (Python) and the native binary (C). Crucial for differential
        # testing — tests using rand_seed + rand_int/rand_float produce
        # identical output in both backends.
        self.rand_state = HalisRNG()
        # Stage 16 (v0.27.0-alpha): the concurrency runtime — real Python
        # threads, one global mutex + condvar (mirrors the native C
        # runtime's design so differential behaviour matches).
        self.conc = ConcRuntime()
        # Stage 37 (v0.56.0-alpha): socket-fd table for the net_tcp_* /
        # net_udp_* builtins. The fd namespace is a monotonic counter
        # (starting at 1) so Halis fds do NOT collide with the C
        # runtime's stdin/stdout/stderr (0/1/2). The dict maps
        # int fd -> Python socket object. net_close pops the entry;
        # double-close is a silent no-op (matches the C runtime).
        self.net_fds = {}
        self._net_next_fd = 1

    @property
    def line(self):
        return getattr(self._tls, "line", 0)

    @line.setter
    def line(self, value):
        self._tls.line = value

    # ---------- Stage 37: socket-fd registration ----------
    def _net_register(self, sock):
        """Register a Python socket under a fresh Halis fd and return the
        fd. The fd namespace starts at 1 (0 is reserved — never returned
        so a Halis program can use `if fd > 0` as the success check).
        Thread-safe: net_fds is mutated under the concurrency mutex
        (the same mutex that protects ConcRuntime state)."""
        with self.conc.mu:
            fd = self._net_next_fd
            self._net_next_fd = self._net_next_fd + 1
            self.net_fds[fd] = sock
        return fd

    # ---------- lifecycle ----------
    def run(self):
        """Run main(); return exit code."""
        try:
            r = self.call_fn("main", [])
        except HLPanic as ex:
            self.out.flush()
            sys.stderr.write("panic: %s (at line %d)\n" % (to_display(ex.msg), ex.line))
            return 101
        if r is None:
            return 0
        return int(r) & 0xFF

    def call_fn(self, key, args):
        fn = self.fns[key]
        # Stage 15 (v0.13.0-alpha): extern fn — call via ctypes.
        if fn.get("extern", False):
            return self.call_extern(fn, args)
        # Stage 31 (v0.48.0-alpha): #[tail_call] trampoline. The checker
        # verified every self-call is in tail position with primitive-
        # only dataflow, so a tail `return f(...)` arrives here as
        # TailCallSig: rebind the parameters and re-run the body in the
        # SAME Python frame (constant Python stack, mirroring the
        # native goto loop). The fn-key stack is pushed per iteration
        # so nested (non-tail) calls from the body still resolve the
        # correct "current fn".
        while True:
            env = [{}]
            if fn["struct"] is not None:
                sn, _, sm = fn["params"][0]
                env[0][sn] = [args[0], sm, False]
                params = fn["params"][1:]
                call_args = args[1:]
            else:
                params = fn["params"]
                call_args = args
            for (pn, _, _), v in zip(params, call_args):
                env[0][pn] = [v, False, False]
            # Stage 17: runtime `requires` assertion (enabled by --contracts).
            if self.contracts and fn.get("requires") is not None:
                if not self._truthy(self.eval_expr(fn["requires"], env)):
                    raise HLPanic("contract violation: requires of '%s' "
                                  "(function precondition failed at runtime)"
                                  % fn["name"], self.line)
            try:
                # Stage 31: lazily create the thread-local fn stack — a
                # TASK thread starts inside the spawn trampoline and
                # never passes through __init__, so its fresh
                # threading.local() has no fn_stack attribute yet (the
                # same discipline the `line` property uses with
                # getattr defaults).
                fn_stack = getattr(self._tls, "fn_stack", None)
                if fn_stack is None:
                    fn_stack = []
                    self._tls.fn_stack = fn_stack
                fn_stack.append(key)
                self.exec_stmts(fn["body"], env)
            except TailCallSig as tc:
                if tc.key == key:
                    # The verified tail self-call: rebind + loop.
                    args = tc.args
                    continue
                # Defensive: only self-calls raise TailCallSig (the
                # return handler checks the target); a foreign key
                # degrades to a normal call.
                return self.call_fn(tc.key, tc.args)
            except ReturnSig as r:
                # Stage 17: runtime `ensures` assertion (enabled by --contracts).
                if self.contracts and fn.get("ensures") is not None:
                    env[0]["result"] = [r.value, False, False]
                    if not self._truthy(self.eval_expr(fn["ensures"], env)):
                        raise HLPanic("contract violation: ensures of '%s' "
                                      "(function postcondition failed at "
                                      "runtime)" % fn["name"], self.line)
                return r.value
            finally:
                # Deep-scan-15 fix (LOW severity / defensive): the prior
                # `getattr(self._tls, "fn_stack", []).pop()` would raise
                # IndexError on a fresh `[]` default if the attribute
                # were somehow missing — which would then mask the real
                # in-flight exception (TailCallSig / ReturnSig). The try
                # block above guarantees the attribute exists, but be
                # defensive: only pop when the list is non-empty.
                _fs = getattr(self._tls, "fn_stack", None)
                if _fs:
                    _fs.pop()
            # Implicit void return: still check ensures (result is None).
            if self.contracts and fn.get("ensures") is not None:
                env[0]["result"] = [None, False, False]
                if not self._truthy(self.eval_expr(fn["ensures"], env)):
                    raise HLPanic("contract violation: ensures of '%s' "
                                  "(function postcondition failed at runtime)"
                                  % fn["name"], self.line)
            return None

    @staticmethod
    def _truthy(v):
        return bool(v)

    # ---------- extern (Stage 15) ----------
    _libc = None
    # Deep-scan-10 fix: per-signature extern wrapper cache. Previously
    # call_extern mutated the SHARED CDLL symbol's argtypes/restype on
    # every call — two tasks calling different extern functions (or the
    # same symbol with different declared signatures) raced the ABI,
    # corrupting marshalling. A CFUNCTYPE prototype bound to a fresh
    # _FuncPtr leaves the shared symbol untouched; the cache is guarded
    # by a lock because tasks call externs from multiple threads.
    _extern_cache = {}
    _extern_lock = threading.Lock()
    # ctypes restype per declared HLS return type (deep-scan-10: the
    # per-signature wrapper needs this mapping up front).
    _EXTERN_RESTYPES = {
        "int": ctypes.c_int64,
        "float": ctypes.c_double,
        "bool": ctypes.c_bool,
        "str": ctypes.c_char_p,
        "void": None,
        # any other type (list/map/struct/enum/tainted/...) -> opaque ptr
    }

    def _get_libc(self):
        """Lazily load libc for extern calls."""
        if self._libc is None:
            try:
                # `None` loads the default C library (libc on Linux,
                # msvcrt on Windows, libSystem on macOS).
                Interp._libc = ctypes.CDLL(None)
            except OSError as ex:
                raise HLPanic("cannot load libc for extern call: %s" % ex,
                              getattr(self, "line", 0))
        return self._libc

    def call_extern(self, fn, args):
        """Call a C function via ctypes (Stage 15-alpha).

        The function signature is taken from the HLS declaration:
          - int -> c_int64
          - float -> c_double
          - bool -> c_bool
          - str -> c_char_p (passed as a null-terminated C string;
            HLS bytes are passed as-is; the caller is responsible for
            ensuring no embedded NUL bytes)
          - void -> no return
          - any other type (list/map/struct/enum/tainted/ptr) -> ptr
            (treated as an opaque pointer; the caller must ensure
            ABI compatibility)

        For the alpha, only int/str args are fully supported. Float
        and bool work via automatic ctypes conversion. Opaque pointers
        are NOT derefenced by the interpreter — they're passed as
        raw addresses.
        """
        libc = self._get_libc()
        name = fn["name"]
        ret = fn["ret"]
        try:
            c_fn = getattr(libc, name)
        except AttributeError:
            raise HLPanic("extern function not found in libc: %s" % name,
                          getattr(self, "line", 0))
        # Set up the argument types.
        c_argtypes = []
        c_args = []
        for (pn, pt, _), v in zip(fn["params"], args):
            if pt == "int":
                c_argtypes.append(ctypes.c_int64)
                c_args.append(int(v))
            elif pt == "float":
                c_argtypes.append(ctypes.c_double)
                c_args.append(float(v))
            elif pt == "bool":
                c_argtypes.append(ctypes.c_bool)
                c_args.append(bool(v))
            elif pt == "str":
                # HLS str is bytes. Pass as a null-terminated C string.
                # Deep-scan-19 fix (LOW, defence-in-depth): reject
                # embedded NUL bytes. C functions interpret the string
                # only up to the first NUL — passing "ls\0; rm -rf /"
                # to system() would execute only "ls" (silent
                # truncation). The caller is responsible for ensuring
                # no embedded NULs; this guard surfaces the bug cleanly.
                c_argtypes.append(ctypes.c_char_p)
                if isinstance(v, bytes):
                    if b"\x00" in v:
                        raise HLPanic("extern str argument contains embedded NUL byte "
                                      "(C would truncate at the NUL)", getattr(self, "line", 0))
                    c_args.append(v)
                else:
                    encoded = str(v).encode("utf-8")
                    if b"\x00" in encoded:
                        raise HLPanic("extern str argument contains embedded NUL byte "
                                      "(C would truncate at the NUL)", getattr(self, "line", 0))
                    c_args.append(encoded)
            else:
                # Deep-scan fix (C8): the previous code passed `id(v)` for
                # list/map/struct args. That's a raw CPython heap address,
                # which the C function would dereference as garbage — a
                # soundness hole. Now we panic with a clean error: opaque
                # pointer args are NOT supported (they require a real
                # ABI/marshalling layer that Stage 15-alpha doesn't have).
                # The user must declare extern fns with primitive types only
                # (int, float, bool, str) and marshal complex types via str.
                raise HLPanic(
                    "extern call to '%s': argument of type %s is not "
                    "supported (only int, float, bool, str args are "
                    "allowed in extern FFI; use a string-encoded form "
                    "for complex data)" % (name, pt),
                    getattr(self, "line", 0))
        # Deep-scan-10 fix: build (or reuse) a per-signature wrapper instead
        # of mutating the shared CDLL symbol — see the class comment on
        # _extern_cache. The key is (name, argtypes, restype) so every
        # declared signature gets its own immutable prototype.
        cache_key = (name, tuple(c_argtypes), ret)
        with Interp._extern_lock:
            c_fn = Interp._extern_cache.get(cache_key)
            if c_fn is None:
                proto = ctypes.CFUNCTYPE(
                    Interp._EXTERN_RESTYPES.get(ret, ctypes.c_void_p),
                    *c_argtypes)
                try:
                    c_fn = proto((name, libc))
                except AttributeError:
                    raise HLPanic(
                        "extern function not found in libc: %s" % name,
                        getattr(self, "line", 0))
                Interp._extern_cache[cache_key] = c_fn
        # Call.
        try:
            result = c_fn(*c_args)
        except Exception as ex:
            raise HLPanic("extern call to '%s' failed: %s" % (name, ex),
                          getattr(self, "line", 0))
        # Convert the return value back to HLS runtime values.
        if ret == "int":
            return int(result) if result is not None else 0
        if ret == "float":
            return float(result) if result is not None else 0.0
        if ret == "bool":
            return bool(result) if result is not None else False
        if ret == "str":
            # c_char_p returns bytes (null-terminated).
            if result is None:
                return b""
            if isinstance(result, bytes):
                return result
            return bytes(result)
        if ret == "void":
            return None
        # Opaque pointer -> int (the raw address).
        return int(result) if result is not None else 0

    # ---------- statements ----------
    def exec_stmts(self, stmts, env):
        for s in stmts:
            self.exec_stmt(s, env)

    def exec_stmt(self, s, env):
        self.line = s.get("line", 0)
        k = s["k"]
        if k == "let":
            env[-1][s["name"]] = [self.eval_expr(s["value"], env), s["mut"], False]
        elif k == "assign":
            self.exec_assign(s, env)
        elif k == "if":
            if self.eval_expr(s["cond"], env):
                env.append({})
                try:
                    self.exec_stmts(s["then"], env)
                finally:
                    env.pop()
            elif s["els"] is not None:
                env.append({})
                try:
                    self.exec_stmts(s["els"], env)
                finally:
                    env.pop()
        elif k == "while":
            while self.eval_expr(s["cond"], env):
                env.append({})
                try:
                    self.exec_stmts(s["body"], env)
                except BreakSig:
                    break
                except ContinueSig:
                    continue
                finally:
                    env.pop()
        elif k == "for":
            lst = self.eval_expr(s["iter"], env)
            n = len(lst)  # snapshot length once (SPEC section 5)
            i = 0
            while i < n:
                # BUG-SC-4 fix: if the loop body shrinks the list (e.g.
                # `xs.pop()`), `lst[i]` would raise a Python IndexError,
                # crashing the interpreter with a traceback instead of a
                # clean HLPanic. Bounds-check before access and stop
                # iterating once the list is shorter than the snapshot.
                # The SPEC only guarantees that appended elements are not
                # visited; shrinking during iteration is undefined, so we
                # stop cleanly rather than crash.
                if i >= len(lst):
                    break
                # BUG-22 fix: use a 3-element binding [value, mut, moved]
                # to match all other bindings in the interpreter. The
                # previous 2-element form was internally inconsistent and
                # would have crashed any future code that indexed [2].
                env.append({s["var"]: [lst[i], False, False]})
                try:
                    self.exec_stmts(s["body"], env)
                except BreakSig:
                    break
                except ContinueSig:
                    pass
                finally:
                    env.pop()
                i += 1
        elif k == "return":
            v = s["value"]
            if v is not None:
                # Stage 31 (v0.48.0-alpha): a VERIFIED tail self-call in
                # a #[tail_call] fn raises TailCallSig instead — call_fn
                # rebinds the parameters and re-runs the body in the
                # SAME Python frame (the trampoline mirror of the native
                # parameter-rebinding goto). The checker guarantees the
                # call target is the CURRENT fn and the dataflow is
                # primitive-only, so evaluating the argument list here
                # has no refcount side effects.
                fn_stack = getattr(self._tls, "fn_stack", None)
                if fn_stack:
                    cur = fn_stack[-1] if fn_stack else None
                    rc = v.get("rc")
                    if (rc is not None and rc[0] == "user" and rc[1] == cur
                            and cur in self.fns
                            and self.fns[cur].get("attrs", {}).get("tail_call", False)):
                        args = [self.eval_expr(a, env) for a in v.get("args", [])]
                        raise TailCallSig(cur, args, self.line)
            raise ReturnSig(self.eval_expr(v, env) if v is not None else None)
        elif k == "break":
            raise BreakSig()
        elif k == "continue":
            raise ContinueSig()
        elif k == "expr":
            self.eval_expr(s["e"], env)
        elif k == "asm":
            # Stage 27 (v0.50.0-alpha): inline-assembly statement.
            # The boot interpreter is a pure-Python reference and
            # cannot execute native asm. Raise a clean error rather
            # than silently no-op'ing (a silent no-op would mislead
            # users — `asm!("mov $0, {0}", out(reg) x); print(x)`
            # would print the original x and look like the asm ran).
            # Programs that declare `asm!` blocks but do NOT execute
            # them at interpreter runtime (e.g. an `asm!` inside a
            # kernel IRQ handler never called from `main`) run fine.
            raise HLPanic(
                "asm! cannot be executed by the boot interpreter — "
                "use the native compiler `hlc` (which lowers asm! to "
                "GCC extended asm) or wrap the asm! call in a path "
                "the interpreter does not take (e.g. an extern \"C\" "
                "fn or a kernel-only entrypoint)",
                self.line)
        else:
            raise HLPanic("unknown statement: %s" % k, self.line)

    def exec_assign(self, s, env):
        val = self.eval_expr(s["value"], env)
        t = s["target"]
        if t["k"] == "ident":
            for scope in reversed(env):
                if t["name"] in scope:
                    scope[t["name"]][0] = val
                    return
            raise HLPanic("variable does not exist: %s" % t["name"], self.line)
        base = self.eval_expr(t["target"], env)
        if t["k"] == "field":
            base[t["name"]] = val
        elif t["k"] == "index":
            i = self.eval_expr(t["idx"], env)
            if i < 0 or i >= len(base):
                raise HLPanic("array access out of bounds", self.line)
            base[i] = val
        else:
            raise HLPanic("invalid lvalue", self.line)

    # ---------- expressions ----------
    def eval_expr(self, e, env):
        k = e["k"]
        if k == "ident":
            name = e["name"]
            for scope in reversed(env):
                if name in scope:
                    return scope[name][0]
            raise HLPanic("variable does not exist: %s" % name, self.line)
        if k == "bin":
            return self.eval_bin(e, env)
        if k == "int" or k == "float" or k == "bool" or k == "str":
            return e["v"]
        if k == "call":
            rc = e["rc"]
            # Stage 16: spawn(f, args...) — the checker rewrote the node:
            # the fn-name argument was removed and recorded in e["spawn_fn"].
            # The target must NOT be evaluated as a value; do_spawn applies
            # the boundary ownership rule to each argument node.
            if rc[0] == "builtin" and rc[1] == "spawn":
                return self.do_spawn(e["spawn_fn"], e["args"], env)
            # Stage 33: async_spawn(f, args...) — like spawn, but creates
            # a cap-1 bounded channel and returns it as a Future. The
            # spawned task calls f(args...) and sends the result on the
            # channel.
            if rc[0] == "builtin" and rc[1] == "async_spawn":
                return self.do_async_spawn(e["spawn_fn"], e["args"], env)
            # Stage 34: gen_spawn(f, args...) -> Stream[T] — creates a
            # bounded stream, spawns f(stream, args...), returns the stream.
            if rc[0] == "builtin" and rc[1] == "gen_spawn":
                return self.do_gen_spawn(e["spawn_fn"], e["args"], env)
            # Stage 34: stream_map_int / stream_filter_int / stream_fold_int /
            # stream_flat_map_int — each takes a Stream and a function name
            # (recorded in spawn_fn by the checker). The worker is spawned
            # with (in_stream, out_stream [, extra]) and calls the function.
            if rc[0] == "builtin" and rc[1] in (
                    "stream_map_int", "stream_filter_int",
                    "stream_fold_int", "stream_flat_map_int"):
                return self.do_stream_combinator(rc[1], e["spawn_fn"],
                                                  e["args"], env)
            args = [self.eval_expr(a, env) for a in e["args"]]
            if rc[0] == "user":
                return self.call_fn(rc[1], args)
            return self.builtin(rc[1], args)
        if k == "field":
            return self.eval_expr(e["target"], env)[e["name"]]
        if k == "method":
            tgt = self.eval_expr(e["target"], env)
            args = [self.eval_expr(a, env) for a in e["args"]]
            rm = e["rm"]
            if rm[0] == "user":
                return self.call_fn(rm[1], [tgt] + args)
            return self.builtin_method(rm[1], tgt, args, e["args"])
        if k == "index":
            lst = self.eval_expr(e["target"], env)
            i = self.eval_expr(e["idx"], env)
            if i < 0 or i >= len(lst):
                raise HLPanic("array access out of bounds", self.line)
            return lst[i]
        if k == "un":
            v = self.eval_expr(e["e"], env)
            if e["op"] == "!":
                return not v
            if type(v) is int:
                return i64_neg(v, self.line)
            return -v
        if k == "listlit":
            return [self.eval_expr(it, env) for it in e["items"]]
        if k == "structlit":
            return self.eval_structlit(e, env)
        if k == "enumlit":
            return self.eval_enumlit(e, env)
        if k == "match":
            return self.eval_match(e, env)
        if k == "qmark":
            return self.eval_qmark(e, env)
        # BUG-SC-10 fix: removed the dead `if k == "mapnew": return {}`
        # branch. The parser never produces a `mapnew` AST node; `map_new()`
        # is a `call` node handled by the `builtin` method.
        raise HLPanic("unknown expression: %s" % k, self.line)

    def eval_structlit(self, e, env):
        name = e["name"]
        # In case of a generic struct, the parser keeps the base name; we use
        # the type from `e["t"]` which has the instantiation. But the field
        # values are determined by `e["fields"]` (in declaration order, may
        # omit defaulted trailing fields). We fill defaults from the struct
        # definition.
        st = self.structs[name]
        decl_fields = st["fields"]  # [(name, type, default_expr_or_None)]
        result = {}
        # Map provided field names → values.
        provided = {}
        for fname, fe in e["fields"]:
            provided[fname] = self.eval_expr(fe, env)
        # Iterate declared fields in order; use provided value or default.
        for fname, ftype, fdefault in decl_fields:
            if fname in provided:
                result[fname] = provided[fname]
            elif fdefault is not None:
                # Evaluate default expression in the calling environment.
                result[fname] = self.eval_expr(fdefault, env)
            else:
                # Should have been caught by the checker.
                raise HLPanic("struct literal missing required field: %s" % fname,
                              self.line)
        return result

    def eval_enumlit(self, e, env):
        # e["enum_name"], e["variant"], e["args"]
        args = [self.eval_expr(a, env) for a in e.get("args", [])]
        return {"enum": e["enum_name"], "var": e["variant"], "data": args}

    def eval_match(self, e, env):
        scrut = self.eval_expr(e["scrut"], env)
        if not isinstance(scrut, dict) or "enum" not in scrut:
            raise HLPanic("match on non-enum value", self.line)
        s_enum = scrut["enum"]
        s_var = scrut["var"]
        s_data = scrut["data"]
        for arm in e["arms"]:
            pat = arm["pattern"]
            if pat["k"] == "wildcard":
                return self.eval_expr(arm["body"], env)
            if pat["enum"] != s_enum:
                continue
            if pat["variant"] != s_var:
                continue
            # Bind payload values.
            env.append({})
            try:
                for i, bname in enumerate(pat["bindings"]):
                    if bname == "_":
                        continue
                    # Stage 27 perfection (v0.50.3-alpha) deep-scan-18:
                    # BUG-13 fix. The previous `else None` fallback
                    # silently set the binding to Python `None` when the
                    # pattern had more bindings than the enum variant's
                    # data (i >= len(s_data)). The checker validates
                    # len(bindings) == len(payloads), so this branch
                    # should be unreachable — but if a checker bug ever
                    # allowed a mismatch, the runtime would set a
                    # binding to None and the next use would raise a
                    # confusing Python TypeError (e.g. `None + 5`).
                    # Now: a clean HLPanic surfaces the bug.
                    if i >= len(s_data):
                        raise HLPanic("match: binding count exceeds "
                                      "payload count (internal "
                                      "checker bug — please report)",
                                      self.line)
                    env[-1][bname] = [s_data[i], False, False]
                return self.eval_expr(arm["body"], env)
            finally:
                env.pop()
        # No arm matched (shouldn't happen if exhaustive).
        raise HLPanic("match: no arm matched (non-exhaustive?)", self.line)

    def eval_qmark(self, e, env):
        v = self.eval_expr(e["e"], env)
        if not isinstance(v, dict) or "enum" not in v:
            raise HLPanic("? on non-enum value", self.line)
        if v["var"] == e["ok_variant"]:
            # Success — yield the single payload value.
            if len(v["data"]) != 1:
                raise HLPanic("? operator: success variant must have exactly one payload",
                              self.line)
            return v["data"][0]
        if v["var"] == e["err_variant"]:
            # Propagate the error: re-wrap and return from the enclosing fn.
            raise ReturnSig(v)
        raise HLPanic("? operator: enum value matched neither ok nor err variant", self.line)

    def eval_bin(self, e, env):
        op = e["op"]
        if op == "||":
            if self.eval_expr(e["l"], env):
                return True
            return self.eval_expr(e["r"], env)
        if op == "&&":
            if not self.eval_expr(e["l"], env):
                return False
            return self.eval_expr(e["r"], env)
        a = self.eval_expr(e["l"], env)
        b = self.eval_expr(e["r"], env)
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "+":
            if type(a) is int:
                return i64_add(a, b, self.line)
            return a + b  # float or str
        if op == "-":
            if type(a) is int:
                return i64_sub(a, b, self.line)
            return a - b
        if op == "*":
            if type(a) is int:
                return i64_mul(a, b, self.line)
            return a * b
        if op == "/":
            if type(a) is int:
                return i64_div(a, b, self.line)
            return f64_div(a, b)
        if op == "%":
            if type(a) is int:
                return i64_mod(a, b, self.line)
            return f64_mod(a, b)
        raise HLPanic("unknown operator: %s" % op, self.line)

    # ---------- builtins ----------
    def builtin(self, name, args):
        line = self.line
        if name == "print":
            self.out.write(args[0])
            return None
        if name == "println":
            self.out.write(args[0] + b"\n")
            return None
        if name == "panic":
            raise HLPanic(args[0], line)
        if name == "exit":
            self.out.flush()
            raise SystemExit(int(args[0]) & 0xFF)
        if name == "str":
            v = args[0]
            if type(v) is bool:
                return b"true" if v else b"false"
            if type(v) is int:
                return str(v).encode("ascii")
            if type(v) is float:
                return fmt_float(v)
            return v
        if name == "int":
            return parse_int(args[0], line)
        if name == "len":
            return len(args[0])
        if name == "range":
            # Deep-scan-7 fix: a malicious or buggy program calling
            # `range(0, INT64_MAX)` would attempt to materialise a
            # 9-quintillion-element list, exhausting memory. Cap at
            # a reasonable limit (1M elements) and panic with a clear
            # message otherwise. boot.py's main thread catches
            # MemoryError, but list(range(...)) raises MemoryError
            # AT THE PYTHON LEVEL — we want a clean HLPanic instead.
            a, b = int(args[0]), int(args[1])
            count = b - a if b > a else 0
            RANGE_MAX = 1_000_000
            if count > RANGE_MAX:
                raise HLPanic(
                    "range(%d, %d) would produce %d elements (limit %d) — "
                    "use an explicit counter loop for large ranges"
                    % (a, b, count, RANGE_MAX), line)
            return list(range(a, b))
        # Stage 21 (v0.37.0-alpha): has_feature — compile-time constant
        # from the --target-feature flag (set via _set_target_feature).
        if name == "has_feature":
            feat = args[0]
            if isinstance(feat, bytes):
                feat = feat.decode("utf-8", "replace")
            return feat in _TARGET_FEATURES
        # Stage 21: simd_cpu_supports — runtime CPU probe (/proc/cpuinfo
        # on Linux; conservative False elsewhere). Matches the C
        # runtime's __builtin_cpu_supports for the probed names.
        if name == "simd_cpu_supports":
            return _cpu_supports(args[0])
        # Stage 19 (v0.35.0-alpha): O(n) join(list[str], sep) -> str.
        # Matches the C runtime's hl_str_join (single allocation, one
        # copy per element). The interpreter's str.join is likewise
        # linear, so differential outputs stay byte-identical.
        if name == "join":
            parts = args[0]
            sep = args[1]
            if not isinstance(parts, list):
                raise HLPanic("join() expects a list[str]", line)
            return sep.join(parts)
        # Stage 32 (v0.51.0-alpha): native bitwise primitives — the
        # interpreter mirrors the C semantics exactly. Shifts are
        # masked to 6 bits (matching the C `& 63`); popcount/clz/ctz
        # use Python's bit_length so they match __builtin_clzll on
        # 64-bit values. All operands are int64 (Python ints; Halis
        # invariant: every int value is in [-2^63, 2^63)).
        #
        # i64_wrap: bitwise ops in Python produce arbitrary-precision
        # ints; we wrap back to signed int64 to match the C semantics
        # (where the result of `(int64_t)(uint64_t)x >> n` is the
        # two's-complement reinterpretation of the unsigned result).
        def _i64_wrap(v):
            v &= (1 << 64) - 1
            if v >= (1 << 63):
                v -= (1 << 64)
            return v
        if name == "int_and":
            return _i64_wrap(args[0] & args[1])
        if name == "int_or":
            return _i64_wrap(args[0] | args[1])
        if name == "int_xor":
            return _i64_wrap(args[0] ^ args[1])
        if name == "int_not":
            return _i64_wrap(~args[0])
        if name == "int_shl":
            n = args[1] & 63
            return _i64_wrap(args[0] << n)
        if name == "int_shr":
            n = args[1] & 63
            # logical right shift on unsigned 64-bit
            x = args[0] & ((1 << 64) - 1)
            return _i64_wrap(x >> n)
        if name == "int_sar":
            n = args[1] & 63
            # arithmetic right shift: Python's >> on negative ints
            # already does sign-extension (floor division by 2^n).
            return _i64_wrap(args[0] >> n)
        if name == "int_popcount":
            x = args[0] & ((1 << 64) - 1)
            return bin(x).count("1")
        if name == "int_clz":
            x = args[0] & ((1 << 64) - 1)
            if x == 0:
                return 64
            return 64 - x.bit_length()
        if name == "int_ctz":
            x = args[0] & ((1 << 64) - 1)
            if x == 0:
                return 64
            n = 0
            while (x & 1) == 0:
                x >>= 1
                n += 1
            return n
        if name == "map_new":
            return {}
        if name == "read_file":
            # Deep-scan-19 fix (MEDIUM, TOCTOU): open the RESOLVED path
            # returned by _sandbox_check, not the original. Prevents a
            # race where a symlink inside the sandbox is swapped for one
            # pointing outside between the check and the open.
            resolved = _sandbox_check(args[0])
            try:
                with open(resolved, "rb") as f:
                    return f.read()
            except OSError:
                raise HLPanic("cannot open file: %s" % to_display(args[0]), line)
        # Stage 10-beta: read_file_tainted(path) — same as read_file but
        # the returned str is wrapped as tainted[str]. The wrapper dict
        # format is identical to taint_mark's output.
        if name == "read_file_tainted":
            resolved = _sandbox_check(args[0])
            try:
                with open(resolved, "rb") as f:
                    content = f.read()
                return {"tainted": True, "value": content}
            except OSError:
                raise HLPanic("cannot open file: %s" % to_display(args[0]), line)
        # Stage 10 release: read_line() -> tainted[str] — third taint source.
        # Reads one line from stdin (newline stripped). The result is always
        # tainted because stdin is untrusted input. EOF returns an empty
        # tainted string (mirrors fgets() semantics in the C runtime).
        if name == "read_line":
            raw = sys.stdin.buffer.readline()
            # Strip a trailing newline (matches the C runtime's hl_read_line).
            if raw.endswith(b"\n"):
                raw = raw[:-1]
                # Also strip a trailing \r if present (CRLF line endings).
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
            return {"tainted": True, "value": raw}
        if name == "write_file":
            resolved = _sandbox_check(args[0])
            try:
                with open(resolved, "wb") as f:
                    f.write(args[1])
                return None
            except OSError:
                raise HLPanic("cannot write file: %s" % to_display(args[0]), line)
        if name == "args":
            # BUG (deep-scan-5): this returned a fresh list COPY on every
            # call, but the native runtime returns THE process-global list
            # — mutating the result is observable in native code but
            # not under Stage-0 (a differential divergence). Return the
            # actual list so both implementations alias identically.
            return self.argv
        if name == "chr":
            if args[0] < 0 or args[0] > 255:
                raise HLPanic("chr out of range 0..255", line)
            return bytes([args[0]])
        if name == "clock_ms":
            return int(time.monotonic() * 1000)
        # ----- Stage 46 (v0.65.0-alpha): thread / scheduling builtins -----
        # thread_sleep_ms(ms) — block the calling thread for ms
        # milliseconds. Uses time.sleep (which yields the GIL).
        #
        # Note: we do NOT touch self.conc.blocked here. The deadlock
        # detector counts only threads blocked on CHANNEL operations
        # (chan.recv with no sender, chan.send on a full bounded
        # channel). A thread in time.sleep is NOT blocked on a
        # channel — it WILL wake up after the sleep duration — so
        # counting it as blocked would cause spurious deadlock
        # panics. The detector's contract is "no thread can ever
        # make progress", which is false while a sleep is pending.
        if name == "thread_sleep_ms":
            ms = args[0]
            if ms < 0:
                raise HLPanic("thread_sleep_ms: duration must be >= 0, "
                              "got %d" % ms, line)
            time.sleep(max(0.0, ms / 1000.0))
            return None
        # thread_yield() — hint the scheduler to switch. Python's
        # time.sleep(0) yields the GIL and lets another thread run.
        if name == "thread_yield":
            time.sleep(0)
            return None
        # thread_current_id() — a non-zero int identifying the
        # calling thread. Uses threading.get_ident() which returns
        # a non-zero int on CPython (the main thread gets a non-zero
        # ID; spawned threads get distinct non-zero IDs). The actual
        # VALUE is implementation-defined and may differ between
        # the interpreter and the native runtime — callers must
        # only rely on the property "different threads get different
        # IDs", not on any specific value.
        if name == "thread_current_id":
            return threading.get_ident()
        if name == "file_exists":
            # Deep-scan-15 cleanup: removed redundant `import os` —
            # `os` is already imported at module top (line 14), and
            # Python caches it in sys.modules so the local import was
            # a no-op besides raising a pylint W0404 reimport warning.
            # Deep-scan-19: use the RESOLVED path returned by _sandbox_check
            # (TOCTOU consistency with read_file / write_file).
            resolved = _sandbox_check(args[0])
            # Deep-scan fix (H2): pass bytes directly to os.path.isfile
            # (Python's os.path.isfile accepts bytes). The old code
            # decoded with errors="replace", which substituted U+FFFD
            # for non-UTF-8 bytes, so the interpreter checked a DIFFERENT
            # path than the native runtime (which passes raw bytes to
            # stat()). Files with non-UTF-8 names diverged.
            return os.path.isfile(resolved)
        # ----- Stage 36 (v0.55.0-alpha): filesystem metadata builtins -----
        # All four builtins route through _sandbox_check (so a sandboxed
        # interpreter can't escape) and use bytes-exact paths (so non-
        # UTF-8 names behave identically in the interpreter and the C
        # runtime — the same invariant the H2 deep-scan fix established
        # for file_exists).
        if name == "fs_read_dir":
            resolved = _sandbox_check(args[0])
            try:
                # os.listdir returns names (NOT full paths) — the
                # caller joins with the parent. Sort the result so
                # the interpreter and the C runtime (which uses
                # opendir/readdir) agree on the iteration order
                # (readdir order is filesystem-dependent; sort by
                # bytes for cross-implementation determinism).
                entries = sorted(os.listdir(resolved))
                # Each entry is a bytes object (because `resolved`
                # is bytes); return the list of bytes directly
                # (HLS str = Python bytes).
                return [e if isinstance(e, bytes)
                        else str(e).encode("utf-8") for e in entries]
            except OSError:
                raise HLPanic("fs_read_dir: cannot list directory: %s"
                              % to_display(args[0]), line)
        if name == "fs_size":
            resolved = _sandbox_check(args[0])
            try:
                return os.path.getsize(resolved)
            except OSError:
                raise HLPanic("fs_size: cannot stat: %s"
                              % to_display(args[0]), line)
        if name == "fs_is_dir":
            resolved = _sandbox_check(args[0])
            # os.path.isdir returns False for non-existent paths
            # (matches the C runtime's stat-based check: a missing
            # path is not a directory).
            return os.path.isdir(resolved)
        if name == "fs_set_perms":
            resolved = _sandbox_check(args[0])
            mode = args[1]
            try:
                os.chmod(resolved, mode)
            except OSError:
                raise HLPanic("fs_set_perms: cannot chmod: %s"
                              % to_display(args[0]), line)
            return None
        # ----- Stage 8-alpha: ownership primitives -----
        # drop(x): semantically releases x. In Stage-0 (Python), the underlying
        # value is left for Python's GC. The binding is marked moved at compile
        # time, so this runtime path just needs to be a no-op that returns None.
        if name == "drop":
            return None
        # clone(x): deep-copy a heap value.
        if name == "clone":
            return self.deep_clone(args[0])
        # take(x): returns x's value (binding is marked moved at compile time).
        if name == "take":
            return args[0]
        # ----- Stage 10-alpha: taint tracking -----
        # tainted_args() — like args() but each element is wrapped in the
        # `tainted[str]` runtime representation: a dict {"tainted": True,
        # "value": <bytes>}. The wrapper is created here; the std.taint /
        # std.sanitize helpers consume it. See std/taint.hls.
        if name == "tainted_args":
            return [{"tainted": True, "value": a} for a in self.argv]
        # taint_mark(x) — wrap any value as tainted. The wrapper is a dict
        # so it's distinguishable from raw values (especially strings,
        # which are bytes).
        if name == "taint_mark":
            return {"tainted": True, "value": args[0]}
        # taint_unwrap(x) — extract the inner value, dropping taint.
        # The checker rejects taint_unwrap on non-tainted values, so by
        # the time we get here, args[0] is guaranteed to be a tainted
        # wrapper dict.
        if name == "taint_unwrap":
            v = args[0]
            # Deep-scan-7 fix: the previous check `"tainted" in v` matched
            # any dict with a field literally named "tainted" — including
            # user structs with a `tainted: int` field. Tighten the check:
            # require BOTH "tainted" AND "value" keys, AND "tainted" must
            # be the boolean True (the taint-mark builtin sets it to True).
            if (isinstance(v, dict) and "tainted" in v and "value" in v
                    and v["tainted"] is True):
                return v["value"]
            # BUG-23 fix: the previous defensive fallback returned the
            # "value" field of ANY dict (including user structs that
            # happen to have a field named "value"). Since the checker
            # guarantees args[0] is a tainted[T], we panic if we get here
            # without the taint wrapper — that indicates a checker bug.
            raise HLPanic("taint_unwrap: expected tainted[T] wrapper, "
                          "got a non-tainted value (got %s)"
                          % type(v).__name__, line)
        # ----- Stage 9 release (v0.20.0-alpha): Net / Rand / Proc builtins -----
        # The interpreter implementations mirror the native runtime in
        # src/hlc.hls exactly, so differential testing passes.
        # rand_int(max: int) -> int — uniform random int in [0, max).
        # Panics on max <= 0 to keep the bound well-defined. Uses the
        # shared HalisRNG LCG so the sequence is identical to the native
        # runtime for the same seed.
        if name == "rand_int":
            if args[0] <= 0:
                raise HLPanic("rand_int() requires a positive max (got %d)"
                              % args[0], line)
            return self.rand_state.randrange(args[0])
        # rand_float() -> float — uniform random float in [0.0, 1.0).
        # Uses the same PRNG state as rand_int; 53 bits of randomness.
        if name == "rand_float":
            return self.rand_state.random()
        # rand_seed(s: int) -> void — seed the PRNG. Same seed produces
        # the same sequence in both the interpreter and the native
        # runtime (the constants and bit-masking are identical).
        if name == "rand_seed":
            self.rand_state.seed(args[0])
            return None
        # net_lookup(host: str) -> str — DNS resolution. Returns the
        # first IPv4 address as a string. Panics on failure (DNS error
        # or no A records). The interpreter uses Python's socket module
        # — the native runtime uses getaddrinfo directly.
        if name == "net_lookup":
            import socket
            host = args[0].decode("utf-8", "replace")
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET)
                for fam, _, _, _, sa in infos:
                    if fam == socket.AF_INET:
                        return sa[0].encode("ascii")
                raise HLPanic("net_lookup: no A records for %s"
                              % to_display(args[0]), line)
            except socket.gaierror as ex:
                raise HLPanic("net_lookup: DNS resolution failed for %s: %s"
                              % (to_display(args[0]), str(ex)), line)
            except OSError as ex:
                # Deep-scan fix (C2): connection timeouts, refused
                # connections, and other non-gaierror OSErrors used to
                # propagate as raw Python tracebacks while the native
                # runtime panicked cleanly. Catch the broader OSError
                # family for differential parity.
                raise HLPanic("net_lookup: network error for %s: %s"
                              % (to_display(args[0]), str(ex)), line)
        # ----- Stage 37 (v0.56.0-alpha): TCP / UDP / TLS builtins -----
        # The interpreter mirrors the C runtime exactly so differential
        # testing (interpreter == native) is byte-exact. All builtins
        # return / accept primitive int fd values; the stdlib wraps them
        # in TcpStream / TcpListener / UdpSocket structs.
        #
        # Socket fds are tracked in self.net_fds (a dict[int, socket])
        # so the interpreter can mirror close() and detect double-
        # close. The fd namespace is a monotonic counter starting at 1
        # (0 is never used; the C runtime uses 0/1/2 for stdin/stdout/
        # stderr, so a Halis fd of 0 would be ambiguous). Negative
        # values are errors.
        if name == "net_tcp_connect":
            import socket as _sock
            host = args[0].decode("utf-8", "replace")
            port = int(args[1])
            try:
                infos = _sock.getaddrinfo(host, port, _sock.AF_INET,
                                          _sock.SOCK_STREAM)
                if not infos:
                    return -1
                fam, ty, pr, _, sa = infos[0]
                s = _sock.socket(fam, ty, pr)
                s.settimeout(5)
                s.connect(sa)
                return self._net_register(s)
            except OSError:
                return -1
        if name == "net_tcp_listen":
            import socket as _sock
            host = args[0].decode("utf-8", "replace")
            port = int(args[1])
            backlog = int(args[2])
            try:
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                s.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
                s.bind((host, port))
                s.listen(backlog)
                return self._net_register(s)
            except OSError:
                return -1
        if name == "net_tcp_accept":
            fd = int(args[0])
            s = self.net_fds.get(fd)
            if s is None:
                return -1
            try:
                s.settimeout(30)
                conn, _addr = s.accept()
                return self._net_register(conn)
            except OSError:
                return -1
        if name == "net_read":
            fd = int(args[0])
            n = int(args[1])
            if n <= 0:
                return b""
            s = self.net_fds.get(fd)
            if s is None:
                return b""
            try:
                s.settimeout(5)
                data = s.recv(n)
                return data if data else b""
            except OSError:
                return b""
        if name == "net_write":
            fd = int(args[0])
            data = args[1]
            s = self.net_fds.get(fd)
            if s is None:
                return -1
            try:
                s.settimeout(5)
                # sendall blocks until all bytes are written (or the
                # socket errors). Return the number of bytes written
                # (== len(data) on success). On error return -1.
                s.sendall(data)
                return len(data)
            except OSError:
                return -1
        if name == "net_close":
            fd = int(args[0])
            s = self.net_fds.pop(fd, None)
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
            return None
        if name == "net_udp_open":
            import socket as _sock
            try:
                s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
                s.settimeout(5)
                return self._net_register(s)
            except OSError:
                return -1
        if name == "net_udp_send_to":
            fd = int(args[0])
            host = args[1].decode("utf-8", "replace")
            port = int(args[2])
            data = args[3]
            s = self.net_fds.get(fd)
            if s is None:
                return -1
            try:
                import socket as _sock
                infos = _sock.getaddrinfo(host, port, _sock.AF_INET,
                                          _sock.SOCK_DGRAM)
                if not infos:
                    return -1
                _f, _t, _p, _c, sa = infos[0]
                n = s.sendto(data, sa)
                return n
            except OSError:
                return -1
        if name == "net_udp_recv_from":
            fd = int(args[0])
            n = int(args[1])
            if n <= 0:
                return b""
            s = self.net_fds.get(fd)
            if s is None:
                return b""
            try:
                s.settimeout(5)
                data, _addr = s.recvfrom(n)
                return data if data else b""
            except OSError:
                return b""
        if name == "net_tls_get":
            # HTTPS GET via libcurl. The interpreter shells out to the
            # `curl` command-line tool (universally available on dev
            # machines) to avoid requiring the Python pycurl binding.
            # The native runtime links libcurl directly. Both produce
            # the same response body byte-for-byte for the same URL.
            #
            # Build the URL: https://host:port/path
            host = args[0].decode("utf-8", "replace")
            port = int(args[1])
            path = args[2].decode("utf-8", "replace")
            if len(path) == 0 or path[0:1] != "/":
                path = "/" + path
            url = "https://" + host + ":" + str(port) + path
            try:
                # -s silent, -S show errors, -L follow redirects,
                # --max-time 30 (avoid hanging on slow networks),
                # --fail panic on HTTP >= 400.
                proc = subprocess.run(
                    ["curl", "-sS", "-L", "--max-time", "30",
                     "--fail", url],
                    capture_output=True, timeout=35)
                if proc.returncode != 0:
                    err = proc.stderr.decode("utf-8", "replace")
                    raise HLPanic("net_tls_get: curl failed for %s: %s"
                                  % (url, err), line)
                body = proc.stdout
                return body if isinstance(body, bytes) else body.encode("utf-8")
            except FileNotFoundError:
                raise HLPanic("net_tls_get: curl not installed (libcurl "
                              "backend requires the curl CLI for the "
                              "interpreter; the native runtime links "
                              "libcurl directly)", line)
            except subprocess.TimeoutExpired:
                raise HLPanic("net_tls_get: timeout for %s" % url, line)
        # proc_exec(cmd: str) -> int — run a shell command. Returns the
        # exit code (0 on success, 1..255 on failure). Uses os.system()
        # so the command runs in a subshell, matching the C runtime's
        # system() call. Tainted command strings are rejected at
        # check time (proc_exec is a taint sink for argument 0).
        if name == "proc_exec":
            # Deep-scan-15 cleanup: removed redundant `import os` (see
            # the file_exists branch above for the rationale).
            cmd = args[0].decode("utf-8", "replace")
            rc = os.system(cmd)
            # Deep-scan-7 fix: os.WIFEXITED / WEXITSTATUS / WTERMSIG
            # are POSIX-only macros. On Windows, os.system returns the
            # exit code directly (not a status word). Detect the
            # platform and handle both cases so the interpreter
            # produces the same result as the C runtime on every OS.
            if sys.platform == "win32":
                # Windows: rc is already the exit code (0..255).
                # Encode signal-like values as 128 + signum for parity.
                if rc < 0:
                    return 128 + (-rc)
                return rc & 0xFF
            # POSIX: os.system returns a status word; the exit code is
            # the high byte (WEXITSTATUS).
            if os.WIFEXITED(rc):
                return os.WEXITSTATUS(rc)
            # Killed by signal — encode as 128 + signum, like shells.
            return 128 + os.WTERMSIG(rc)
        # ----- Stage 47 (v0.66.0-alpha): process management builtins -----
        # The interpreter mirrors the C runtime exactly so differential
        # testing (interpreter == native) is byte-exact. Child processes
        # are tracked in self.proc_children (a dict[int, subprocess.Popen])
        # keyed by an int pid (a monotonic counter starting at 1, NOT
        # the OS pid — the OS pid differs between interpreter and native,
        # so we use our own namespace). The Halis-level pid is what
        # proc_wait / proc_kill / proc_child_* use; the runtime translates
        # it to the OS pid internally.
        if name == "proc_spawn":
            program = args[0].decode("utf-8", "replace")
            arg_list = args[1]  # list of bytes
            stdin_kind = int(args[2])
            stdout_kind = int(args[3])
            stderr_kind = int(args[4])
            if stdin_kind < 0 or stdin_kind > 2:
                raise HLPanic("proc_spawn: stdin_kind must be 0/1/2", line)
            if stdout_kind < 0 or stdout_kind > 2:
                raise HLPanic("proc_spawn: stdout_kind must be 0/1/2", line)
            if stderr_kind < 0 or stderr_kind > 2:
                raise HLPanic("proc_spawn: stderr_kind must be 0/1/2", line)
            # Translate stdio kinds to subprocess constants.
            def _translate_stdio(kind):
                if kind == 0:
                    return None  # inherit
                if kind == 1:
                    return subprocess.PIPE
                return subprocess.DEVNULL  # null
            try:
                argv = [program] + [a.decode("utf-8", "replace") for a in arg_list]
                proc = subprocess.Popen(
                    argv,
                    stdin=_translate_stdio(stdin_kind),
                    stdout=_translate_stdio(stdout_kind),
                    stderr=_translate_stdio(stderr_kind),
                    close_fds=True,
                )
            except FileNotFoundError:
                raise HLPanic("proc_spawn: program not found: %s"
                              % to_display(args[0]), line)
            except OSError as ex:
                raise HLPanic("proc_spawn: %s: %s"
                              % (to_display(args[0]), str(ex)), line)
            # Allocate a Halis-level pid (monotonic counter, starting at 1).
            if not hasattr(self, "proc_next_pid") or self.proc_next_pid < 1:
                self.proc_next_pid = 1
            if not hasattr(self, "proc_children"):
                self.proc_children = {}
            hl_pid = self.proc_next_pid
            self.proc_next_pid += 1
            self.proc_children[hl_pid] = proc
            return hl_pid
        if name == "proc_wait":
            hl_pid = int(args[0])
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_wait: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            try:
                rc = proc.wait()
            except OSError as ex:
                return -1
            # rc is already the exit code (subprocess encodes signals
            # as -N; translate to 128 + signum for parity with proc_exec).
            if rc < 0:
                return 128 + (-rc)
            return rc & 0xFF
        if name == "proc_kill":
            hl_pid = int(args[0])
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_kill: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            try:
                proc.terminate()
                return 0
            except OSError:
                return -1
        if name == "proc_child_write":
            hl_pid = int(args[0])
            data = args[1]
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_child_write: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            if proc.stdin is None:
                return -1
            try:
                proc.stdin.write(data if isinstance(data, bytes)
                                 else bytes(data))
                proc.stdin.flush()
                return len(data)
            except (OSError, BrokenPipeError, ValueError):
                return -1
        if name == "proc_child_read":
            hl_pid = int(args[0])
            fd_kind = int(args[1])
            n = int(args[2])
            if fd_kind not in (1, 2):
                raise HLPanic("proc_child_read: fd_kind must be 1 (stdout) "
                              "or 2 (stderr), got %d" % fd_kind, line)
            if n < 0:
                raise HLPanic("proc_child_read: n must be >= 0, got %d" % n, line)
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_child_read: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            stream = proc.stdout if fd_kind == 1 else proc.stderr
            if stream is None:
                return b""
            try:
                return stream.read(n) if n > 0 else b""
            except OSError:
                return b""
        if name == "proc_child_close":
            hl_pid = int(args[0])
            fd_kind = int(args[1])
            if fd_kind not in (0, 1, 2):
                raise HLPanic("proc_child_close: fd_kind must be 0/1/2, "
                              "got %d" % fd_kind, line)
            if not hasattr(self, "proc_children") or hl_pid not in self.proc_children:
                raise HLPanic("proc_child_close: unknown pid %d" % hl_pid, line)
            proc = self.proc_children[hl_pid]
            stream = {0: proc.stdin, 1: proc.stdout, 2: proc.stderr}[fd_kind]
            if stream is None:
                return 0  # idempotent on already-closed / not-opened
            try:
                stream.close()
                return 0
            except OSError:
                return -1
        # ----- Stage 48 (v0.67.0-alpha): environment + cwd builtins -----
        # The interpreter uses Python's os.environ, os.getcwd, os.chdir.
        # All return / consume bytes-exact str (HLS str = Python bytes)
        # so non-UTF-8 env vars and cwd paths behave identically to the
        # C runtime (which uses getenv / setenv / unsetenv / getcwd /
        # chdir with raw char*).
        #
        # These are LOW-LEVEL builtins returning plain str / bool / etc.
        # The stdlib std/env.hls wrapper applies the taint wrappers to
        # produce the user-facing API (env_var -> Option[tainted[str]],
        # env_current_dir -> tainted[str], env_args_os -> list[tainted[str]]).
        if name == "env_get":
            key = args[0].decode("utf-8", "replace")
            val = os.environ.get(key, "")
            return val.encode("utf-8") if isinstance(val, str) else bytes(val)
        if name == "env_has":
            key = args[0].decode("utf-8", "replace")
            return key in os.environ
        if name == "env_set":
            key = args[0].decode("utf-8", "replace")
            val = args[1].decode("utf-8", "replace")
            os.environ[key] = val
            return None
        if name == "env_unset":
            key = args[0].decode("utf-8", "replace")
            if key in os.environ:
                del os.environ[key]
            return None
        if name == "cwd_get":
            cwd = os.getcwd()
            return cwd.encode("utf-8") if isinstance(cwd, str) else bytes(cwd)
        if name == "cwd_set":
            path = args[0].decode("utf-8", "replace")
            try:
                os.chdir(path)
                return 0
            except OSError:
                return -1
        if name == "args_os":
            # Same as args() — the OS-string version. The roadmap
            # distinguishes args_os from tainted_args semantically
            # (args_os is the raw bytes from the OS; tainted_args is
            # the logical str version) but in Halis str = bytes
            # already, so they currently alias.
            return self.argv
        # ----- Stage 49 (v0.68.0-alpha): high-resolution time builtins -----
        # instant_now_ns() — monotonic nanoseconds. Python's
        # time.monotonic_ns() returns an int that never decreases
        # (modulo wrap at int64 — ~292 years). Used by Instant::now().
        if name == "instant_now_ns":
            return int(time.monotonic_ns())
        # system_time_now_ms() — wall-clock milliseconds since the
        # Unix epoch (1970-01-01 UTC). Python's time.time() returns a
        # float; we multiply by 1000 and truncate to int. The wall
        # clock can jump on NTP adjustments.
        if name == "system_time_now_ms":
            return int(time.time() * 1000.0)
        # ----- Stage 50 (v0.69.0-alpha): libm-backed math builtins -----
        # All delegate to Python's math module (which wraps libm on
        # CPython). NaN/Inf/signed-zero/subnormal handling matches
        # IEEE-754 because Python's float is C double, and math.*
        # calls libm directly. The native Halis runtime links -lm
        # and calls the same libm functions, so differential parity
        # (interpreter == native) holds bit-for-bit on the same
        # platform (libm is platform-consistent on Linux x86-64 /
        # aarch64 / riscv64 — we don't claim cross-platform parity).
        if name == "math_sin":
            return math.sin(args[0])
        if name == "math_cos":
            return math.cos(args[0])
        if name == "math_tan":
            return math.tan(args[0])
        if name == "math_asin":
            return math.asin(args[0])
        if name == "math_acos":
            return math.acos(args[0])
        if name == "math_atan":
            return math.atan(args[0])
        if name == "math_atan2":
            return math.atan2(args[0], args[1])
        if name == "math_sinh":
            return math.sinh(args[0])
        if name == "math_cosh":
            return math.cosh(args[0])
        if name == "math_tanh":
            return math.tanh(args[0])
        if name == "math_exp":
            return math.exp(args[0])
        if name == "math_log":
            return math.log(args[0])
        if name == "math_log10":
            return math.log10(args[0])
        if name == "math_log2":
            return math.log2(args[0])
        if name == "math_pow":
            return math.pow(args[0], args[1])
        if name == "math_sqrt":
            return math.sqrt(args[0])
        if name == "math_cbrt":
            # Python's math module added cbrt in 3.11; we require
            # 3.12+ (boot.py guard), so it's available.
            return math.cbrt(args[0])
        if name == "math_hypot":
            return math.hypot(args[0], args[1])
        if name == "math_fmod":
            return math.fmod(args[0], args[1])
        if name == "math_erf":
            return math.erf(args[0])
        if name == "math_erfc":
            return math.erfc(args[0])
        if name == "math_tgamma":
            return math.gamma(args[0])
        if name == "math_lgamma":
            return math.lgamma(args[0])
        if name == "math_isnan":
            return math.isnan(args[0])
        if name == "math_isinf":
            return math.isinf(args[0])
        if name == "math_isfinite":
            return math.isfinite(args[0])
        if name == "math_signbit":
            # Python's math.copysign(1.0, x) returns 1.0 for positive
            # (incl +0.0) and -1.0 for negative (incl -0.0). signbit
            # is true iff the sign bit is set (negative or -0.0).
            return math.copysign(1.0, args[0]) < 0.0
        if name == "math_copysign":
            return math.copysign(args[0], args[1])
        # ----- Stage 16 (v0.27.0-alpha): concurrency builtins -----
        # chan_new() -> Chan[T] — a fresh, empty channel.
        if name == "chan_new":
            ch = HLChan()
            self.conc.register(ch)
            return ch
        # chan_new_bounded(cap: int) -> Chan[T] — a fresh bounded channel
        # (Stage-16 perfection, v0.29.0-alpha): send blocks while the
        # channel holds `cap` messages (backpressure). The checker
        # rejects literal capacities < 1; dynamic ones are validated here.
        if name == "chan_new_bounded":
            cap = args[0]
            if cap < 1:
                raise HLPanic(
                    "chan_new_bounded() capacity must be >= 1, got %d" % cap,
                    line)
            ch = HLChan(cap)
            self.conc.register(ch)
            return ch
        # select(chs: list[Chan[T]]) -> int — block until any channel is
        # ready; return the index (in list order) of the first ready one.
        if name == "select":
            chans = args[0]
            return self.conc.select(chans, line)
        # ----- Stage 33 (v0.52.0-alpha): async/await builtins -----
        # await(fut: Future[T]) -> T — block until the future is ready,
        # return its value. The Future is a cap-1 bounded HLChan; await
        # is just chan.recv() on it.
        if name == "await":
            fut = args[0]
            return self.conc.recv(fut, line)
        # future_ready(v: T) -> Future[T] — make an immediately-ready
        # future. Create a cap-1 chan and send the value; the recv()
        # in await() will find it instantly.
        if name == "future_ready":
            v = args[0]
            ch = HLChan(1)
            self.conc.register(ch)
            self.conc.send(ch, v)
            return ch
        # future_poll(fut: Future[T]) -> Option[T] — non-blocking poll.
        # Returns Some(v) if the future is ready, None otherwise.
        if name == "future_poll":
            fut = args[0]
            with self.conc.cv:
                if fut.q:
                    v = fut.q.pop(0)
                    self.conc.msgs -= 1
                    self.conc.cv.notify_all()
                    return {"enum": "Option", "var": "Some", "data": [v]}
            return {"enum": "Option", "var": "None", "data": []}
        # future_select(futs: list[Future[T]]) -> int — race multiple
        # futures; return the index of the first ready one. Same as
        # select() but on Futures (which are channels under the hood).
        if name == "future_select":
            futs = args[0]
            return self.conc.select(futs, line)
        # ----- Stage 34 (v0.53.0-alpha): async stream builtins -----
        # stream_new(cap: int) -> Stream[T] — bounded stream.
        if name == "stream_new":
            cap = args[0]
            if cap < 1:
                raise HLPanic(
                    "stream_new() capacity must be >= 1, got %d" % cap,
                    line)
            ch = HLChan(cap)
            self.conc.register(ch)
            return ch
        # stream_send(s: Stream[T], v: T) — push a value (backpressure).
        if name == "stream_send":
            s = args[0]
            v = args[1]
            # Boundary ownership: deep-clone owned values unless fresh.
            # The checker already rejected bare ident reads of owned
            # types; if we reach here, the value is either a primitive,
            # a clone(...) result, or a fresh expression result.
            self.conc.send(s, v)
            return None
        # stream_recv(s: Stream[T]) -> T — block until a value is available.
        if name == "stream_recv":
            s = args[0]
            return self.conc.recv(s, line)
        # stream_try_recv(s: Stream[T], default: T) -> T — non-blocking.
        if name == "stream_try_recv":
            s = args[0]
            default = args[1]
            return self.conc.recv_or(s, default)
        # stream_len(s: Stream[T]) -> int — pending message count.
        if name == "stream_len":
            s = args[0]
            with self.conc.cv:
                return len(s.q)
        # stream_close(s: Stream[T]) — signal end-of-stream.
        # For int streams, send INT64_MIN (the sentinel). For other
        # element types, this is a no-op (the user must send the
        # appropriate sentinel via stream_send). The builtin exists so
        # the type checker can validate the user remembered to close.
        # We send the int sentinel regardless — it's harmless on
        # non-int streams (the value is just a sentinel the consumer
        # checks for, and non-int consumers don't check).
        if name == "stream_close":
            s = args[0]
            self.conc.send(s, INT64_MIN_SENTINEL)
            return None
        # stream_take_int(in_s, n) -> Stream[int] — create an output
        # stream, spawn a worker that forwards the first n values then
        # sends the sentinel. The checker did NOT rewrite this node
        # (no fn-name argument), so we handle it here directly.
        if name == "stream_take_int":
            in_s = args[0]
            n = args[1]
            out_s = HLChan(16)
            self.conc.register(out_s)
            conc = self.conc
            interp = self

            def take_runner():
                try:
                    count = 0
                    while count < n:
                        v = conc.recv(in_s, interp.line)
                        if v == INT64_MIN_SENTINEL:
                            break
                        conc.send(out_s, v)
                        count = count + 1
                    conc.send(out_s, INT64_MIN_SENTINEL)
                except HLPanic as ex:
                    interp.out.flush()
                    sys.stderr.write("panic: %s (at line %d)\n"
                                     % (to_display(ex.msg), ex.line))
                    os._exit(101)
                except BaseException as ex:
                    interp.out.flush()
                    sys.stderr.write("panic: %s (in stream_take)\n" % ex)
                    os._exit(101)
                with conc.cv:
                    conc.tasks_alive -= 1
                    conc.cv.notify_all()

            with self.conc.cv:
                self.conc.tasks_alive += 1
            t = threading.Thread(target=take_runner)
            t.daemon = True
            t.start()
            return out_s
        # stream_merge_int(a, b) -> Stream[int] — interleave two streams.
        if name == "stream_merge_int":
            a = args[0]
            b = args[1]
            out_s = HLChan(16)
            self.conc.register(out_s)
            conc = self.conc
            interp = self

            def merge_runner():
                try:
                    a_done = False
                    b_done = False
                    while not a_done or not b_done:
                        if not a_done:
                            v = conc.recv(a, interp.line)
                            if v == INT64_MIN_SENTINEL:
                                a_done = True
                            else:
                                conc.send(out_s, v)
                        if not b_done:
                            v = conc.recv(b, interp.line)
                            if v == INT64_MIN_SENTINEL:
                                b_done = True
                            else:
                                conc.send(out_s, v)
                    conc.send(out_s, INT64_MIN_SENTINEL)
                except HLPanic as ex:
                    interp.out.flush()
                    sys.stderr.write("panic: %s (at line %d)\n"
                                     % (to_display(ex.msg), ex.line))
                    os._exit(101)
                except BaseException as ex:
                    interp.out.flush()
                    sys.stderr.write("panic: %s (in stream_merge)\n" % ex)
                    os._exit(101)
                with conc.cv:
                    conc.tasks_alive -= 1
                    conc.cv.notify_all()

            with self.conc.cv:
                self.conc.tasks_alive += 1
            t = threading.Thread(target=merge_runner)
            t.daemon = True
            t.start()
            return out_s
        raise HLPanic("unknown builtin function: %s" % name, line)

    # ---------- Stage 16: spawn ----------
    def do_spawn(self, fn_key, arg_nodes, env):
        """spawn(f, a1..aN) — start a task running f(args).

        Boundary ownership rule (mirrors the native codegen exactly):
        an owned value crossing the task boundary is deep-copied unless
        it is syntactically clone(...) (already a private deep copy).
        Channels cross by sharing (their internal state is guarded by the
        runtime lock). This guarantees no mutable value is visible to two
        threads at once — the interpreter-side mirror of the native
        refcount discipline."""
        values = []
        for a in arg_nodes:
            v = self.eval_expr(a, env)
            if isinstance(v, (list, dict)) and not (
                    a.get("k") == "call" and a.get("name") == "clone"):
                v = self.deep_clone(v)
            values.append(v)
        conc = self.conc
        task = HLTask(None)
        interp = self

        def runner():
            try:
                result = interp.call_fn(fn_key, values)
            except HLPanic as ex:
                # Safe-halt semantics: a panic in ANY task halts the whole
                # process (tasks share the process fate).
                interp.out.flush()
                sys.stderr.write("panic: %s (at line %d)\n"
                                 % (to_display(ex.msg), ex.line))
                os._exit(101)
            except SystemExit as ex:
                interp.out.flush()
                code = ex.code if isinstance(ex.code, int) else 0
                os._exit(code & 0xFF)
            except BaseException as ex:
                # Deep-scan-10 fix: an unexpected interpreter-level error
                # inside a task (e.g. RecursionError from runaway HLS
                # recursion) previously killed only the Python thread —
                # task_finished was never called, so join() waited
                # forever and the deadlock detector could never fire
                # (blocked < alive forever): the process HUNG instead of
                # safe-halting. Any unexpected task-side failure now
                # halts the whole process with a clean panic (101), the
                # same policy main-thread recursion already follows.
                interp.out.flush()
                sys.stderr.write("panic: %s (in task)\n" % ex)
                os._exit(101)
            conc.task_finished(task, result)

        with conc.cv:
            conc.tasks_alive += 1
        # Daemon thread: the process must be able to exit while a task is
        # still blocked in recv/select (mirrors the native semantics where
        # main() returning terminates the whole process, threads included).
        # Without daemon=True a deadlocked-at-exit task would hang the
        # interpreter at shutdown (Python waits for non-daemon threads).
        t = threading.Thread(target=runner)
        t.daemon = True
        task.thread = t
        t.start()
        return task

    # ---------- Stage 33 (v0.52.0-alpha): async_spawn ----------
    def do_async_spawn(self, fn_key, arg_nodes, env):
        """async_spawn(f, a1..aN) -> Future[R] — like spawn, but the
        result is delivered via a cap-1 bounded channel (the Future).

        The Future is represented at runtime as an HLChan with cap=1
        (bounded — backpressure: the producing task blocks at send time
        until the consumer takes the value, ensuring no unbounded
        buffering). The spawned task calls f(args...) and sends the
        result on the channel; await(fut) is a chan.recv().

        Boundary ownership rule: same as do_spawn (deep-copy owned
        values unless syntactically clone(...) or fresh).
        """
        # Evaluate + boundary-clone the arguments (same logic as do_spawn).
        values = []
        for a in arg_nodes:
            v = self.eval_expr(a, env)
            if isinstance(v, (list, dict)) and not (
                    a.get("k") == "call" and a.get("name") == "clone"):
                v = self.deep_clone(v)
            values.append(v)
        # Create the result channel (cap 1, bounded).
        result_chan = HLChan(1)
        self.conc.register(result_chan)
        conc = self.conc
        interp = self

        def runner():
            try:
                result = interp.call_fn(fn_key, values)
                # Send the result on the channel. On a cap-1 bounded
                # channel this blocks until the consumer takes it
                # (backpressure — the producer does not race ahead).
                # The result may be a primitive (int/float/bool/str)
                # or a heap value (list/dict). Channels deep-clone
                # owned values at the send boundary (same rule as
                # chan.send) — but our `result` is FRESH (the callee
                # just returned it), so no defensive clone is needed.
                conc.send(result_chan, result)
            except HLPanic as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (at line %d)\n"
                                 % (to_display(ex.msg), ex.line))
                os._exit(101)
            except SystemExit as ex:
                interp.out.flush()
                code = ex.code if isinstance(ex.code, int) else 0
                os._exit(code & 0xFF)
            except BaseException as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (in async task)\n" % ex)
                os._exit(101)
            # Note: we do NOT call conc.task_finished here because
            # async_spawn does not return a Task join handle — the
            # caller awaits the Future (the result channel) instead.
            # But we MUST decrement tasks_alive so the deadlock
            # detector's "alive" count stays correct.
            with conc.cv:
                conc.tasks_alive -= 1
                conc.cv.notify_all()

        with conc.cv:
            conc.tasks_alive += 1
        t = threading.Thread(target=runner)
        t.daemon = True
        t.start()
        # The Future IS the result channel (same runtime representation).
        return result_chan

    # ---------- Stage 34 (v0.53.0-alpha): gen_spawn ----------
    def do_gen_spawn(self, fn_key, arg_nodes, env):
        """gen_spawn(f, args...) -> Stream[T] — create a bounded stream,
        spawn f(stream, args...), return the stream.

        The target function f must take the Stream as its FIRST parameter
        (the checker enforces this). The generator writes values into the
        stream via stream_send and signals end-of-stream via stream_close
        (which sends a sentinel — for int streams, INT64_MIN).
        """
        # Default capacity for generator streams: 16 (bounded — backpressure
        # without excessive latency). The user can override by creating the
        # stream manually with stream_new(cap) and spawning the generator
        # with plain spawn().
        stream = HLChan(16)
        self.conc.register(stream)
        # Build the argument list: stream first, then the user's args.
        values = [stream]
        for a in arg_nodes:
            v = self.eval_expr(a, env)
            if isinstance(v, (list, dict)) and not (
                    a.get("k") == "call" and a.get("name") == "clone"):
                v = self.deep_clone(v)
            values.append(v)
        conc = self.conc
        interp = self

        def runner():
            try:
                # The generator function takes (stream, args...) and
                # returns void (it just writes to the stream).
                interp.call_fn(fn_key, values)
            except HLPanic as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (at line %d)\n"
                                 % (to_display(ex.msg), ex.line))
                os._exit(101)
            except SystemExit as ex:
                interp.out.flush()
                code = ex.code if isinstance(ex.code, int) else 0
                os._exit(code & 0xFF)
            except BaseException as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (in generator task)\n" % ex)
                os._exit(101)
            with conc.cv:
                conc.tasks_alive -= 1
                conc.cv.notify_all()

        with conc.cv:
            conc.tasks_alive += 1
        t = threading.Thread(target=runner)
        t.daemon = True
        t.start()
        return stream

    # ---------- Stage 34: stream combinators ----------
    def do_stream_combinator(self, kind, fn_key, arg_nodes, env):
        """stream_map_int / stream_filter_int / stream_fold_int /
        stream_flat_map_int — each spawns a worker that reads from the
        input stream, applies fn_key, and writes to the output stream
        (or, for fold, accumulates and returns the final value).

        For map/filter/flat_map: creates an output Stream[int] (cap 16),
        spawns a worker, returns the output stream. The pipeline runs
        concurrently — the consumer of the output stream pulls values
        as needed, and backpressure propagates through the bounded
        channels.

        For fold: BLOCKS the caller (drains the input stream to
        completion, applying fn_key to accumulate). Returns int.
        """
        # Evaluate the input stream argument.
        in_stream = self.eval_expr(arg_nodes[0], env)
        if kind == "stream_fold_int":
            # Blocking fold: drain the stream, apply fn_key(acc, v), return acc.
            init = self.eval_expr(arg_nodes[1], env)
            acc = init
            while True:
                v = self.conc.recv(in_stream, self.line)
                # Sentinel check: INT64_MIN signals end-of-stream.
                if v == INT64_MIN_SENTINEL:
                    break
                acc = self.call_fn(fn_key, [acc, v])
            return acc
        # map / filter / flat_map: create output stream, spawn worker.
        out_stream = HLChan(16)
        self.conc.register(out_stream)
        conc = self.conc
        interp = self

        def runner():
            try:
                if kind == "stream_map_int":
                    while True:
                        v = conc.recv(in_stream, interp.line)
                        if v == INT64_MIN_SENTINEL:
                            conc.send(out_stream, INT64_MIN_SENTINEL)
                            return
                        r = interp.call_fn(fn_key, [v])
                        conc.send(out_stream, r)
                elif kind == "stream_filter_int":
                    while True:
                        v = conc.recv(in_stream, interp.line)
                        if v == INT64_MIN_SENTINEL:
                            conc.send(out_stream, INT64_MIN_SENTINEL)
                            return
                        keep = interp.call_fn(fn_key, [v])
                        if keep:
                            conc.send(out_stream, v)
                elif kind == "stream_flat_map_int":
                    while True:
                        v = conc.recv(in_stream, interp.line)
                        if v == INT64_MIN_SENTINEL:
                            conc.send(out_stream, INT64_MIN_SENTINEL)
                            return
                        # fn_key(v) returns a Stream[int] (an HLChan).
                        inner = interp.call_fn(fn_key, [v])
                        # Forward all values from the inner stream.
                        while True:
                            iv = conc.recv(inner, interp.line)
                            if iv == INT64_MIN_SENTINEL:
                                break
                            conc.send(out_stream, iv)
            except HLPanic as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (at line %d)\n"
                                 % (to_display(ex.msg), ex.line))
                os._exit(101)
            except SystemExit as ex:
                interp.out.flush()
                code = ex.code if isinstance(ex.code, int) else 0
                os._exit(code & 0xFF)
            except BaseException as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (in stream combinator)\n" % ex)
                os._exit(101)
            with conc.cv:
                conc.tasks_alive -= 1
                conc.cv.notify_all()

        with conc.cv:
            conc.tasks_alive += 1
        t = threading.Thread(target=runner)
        t.daemon = True
        t.start()
        return out_stream

    def deep_clone(self, v, _seen=None):
        """Deep-copy an HLS runtime value (Stage-0 / Python)."""
        # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-03 fix.
        # Add cycle detection via a `_seen` set keyed by id(v). A user-
        # constructed cyclic struct (e.g. `let mut n = Node{children:[]};
        # n.children.push(n)` — lists have reference semantics, so push
        # aliases the dict) previously caused unbounded Python recursion
        # and a RecursionError caught by boot.py as "stack overflow"
        # (exit 101). The checker's `clone_supported` already has a
        # `_seen` guard and ACCEPTS cyclic types — so the runtime HANG
        # was a soundness gap between checker and runtime. The fix:
        # on revisiting a seen object, return the already-cloned copy
        # (breaking the cycle by aliasing the clone — matches what the
        # native codegen would do via the typed hl_clone_<Struct> helper
        # if/when it grows the same cycle support).
        if _seen is None:
            _seen = {}
        if isinstance(v, (dict, list)):
            vid = id(v)
            if vid in _seen:
                return _seen[vid]
        if isinstance(v, HLChan):
            # Stage 16: a channel clones by SHARING (that is its purpose —
            # the queue is guarded by the runtime lock). Mirrors the
            # native hl_chan_clone (atomic refcount + 1).
            return v
        if isinstance(v, HLTask):
            # Stage 16: a Task join handle is single-consumer and cannot
            # be cloned (the checker rejects this; defensive halt here).
            raise HLPanic("cannot clone a Task join handle", self.line)
        if isinstance(v, bytes):
            return bytes(v)  # strings are immutable, shallow copy is fine
        if isinstance(v, list):
            new_list = []
            _seen[id(v)] = new_list
            for x in v:
                new_list.append(self.deep_clone(x, _seen))
            return new_list
        if isinstance(v, dict):
            # SCAN-A fix: distinguish enum values from struct values. An
            # enum value is `{"enum": name, "var": variant, "data": [...]}` —
            # check for ALL THREE keys. A struct value with a field literally
            # named "enum" would be `{"enum": value}` — missing "var" and
            # "data" — so it must be treated as a struct (a plain dict).
            if "enum" in v and "var" in v and "data" in v:
                new_enum = {"enum": v["enum"], "var": v["var"], "data": []}
                _seen[id(v)] = new_enum
                new_enum["data"] = [self.deep_clone(x, _seen) for x in v["data"]]
                return new_enum
            # map[str, T] — copy insertion-ordered dict. Also covers
            # struct values (which are dicts of field_name -> value).
            new = {}
            _seen[id(v)] = new
            for k in v:
                new[k] = self.deep_clone(v[k], _seen)
            return new
        # primitives (int, float, bool, None)
        return v

    def builtin_method(self, op, t, args, arg_nodes=None):
        line = self.line
        if op == "str.len":
            return len(t)
        if op == "str.byte_at":
            if args[0] < 0 or args[0] >= len(t):
                raise HLPanic("string access out of bounds", line)
            return t[args[0]]
        if op == "str.slice":
            a, b = args
            if a < 0 or b < a or b > len(t):
                raise HLPanic("invalid string slice", line)
            return t[a:b]
        if op == "str.find":
            return t.find(args[0])
        if op == "str.contains":
            return t.find(args[0]) >= 0
        if op == "str.starts_with":
            return t.startswith(args[0])
        if op == "str.ends_with":
            return t.endswith(args[0])
        if op == "str.split":
            if len(args[0]) == 0:
                raise HLPanic("empty separator not allowed", line)
            return t.split(args[0])
        if op == "str.trim":
            return t.strip(B_LOW)
        if op == "str.to_int":
            return parse_int(t, line)
        if op == "str.to_float":
            # strict: ^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$ — matches the C version
            # BUG-007 fix: must require at least one digit; "." alone is invalid.
            # BUG-4 fix (Stage 10-beta): accept optional scientific notation
            # exponent so JSON parsers can produce floats like "1e5", "1.5e-3",
            # etc. Previously the function rejected any non-digit/non-dot char
            # (including 'e'/'E'), which made json_parse("1e5") panic.
            # Deep-scan-7 fix: also accept leading `+` for parity with C's
            # strtod() and Python's float() — both accept "+1.5". The old
            # code only stripped a leading `-`, so "+1.5".to_float() panicked
            # on the `+`. This caused a differential testing divergence
            # between the interpreter and the C runtime.
            i = 0
            if t[0:1] == b"-":
                i = 1
            elif t[0:1] == b"+":
                i = 1
            if i >= len(t):
                raise HLPanic("cannot convert string to float", line)
            dots = 0
            digits = 0
            saw_exp = False
            exp_digits = 0
            while i < len(t):
                c = t[i]
                if c == 46:  # '.'
                    if saw_exp:
                        raise HLPanic("cannot convert string to float", line)
                    dots += 1
                elif 48 <= c <= 57:
                    if saw_exp:
                        exp_digits += 1
                    digits += 1
                elif c == 101 or c == 69:  # 'e' or 'E'
                    if saw_exp or digits == 0:
                        raise HLPanic("cannot convert string to float", line)
                    saw_exp = True
                    # Optional sign after e/E.
                    if i + 1 < len(t) and (t[i + 1] == 43 or t[i + 1] == 45):
                        i += 1
                    exp_digits = 0
                else:
                    raise HLPanic("cannot convert string to float", line)
                i += 1
            if dots > 1 or digits == 0:
                raise HLPanic("cannot convert string to float", line)
            if saw_exp and exp_digits == 0:
                raise HLPanic("cannot convert string to float", line)
            return float(t)
        if op == "str.to_str":
            return t
        if op == "int.to_str":
            return str(t).encode("ascii")
        if op == "int.to_float":
            return float(t)
        if op == "int.abs":
            return i64_neg(t, line) if t < 0 else t
        if op == "float.to_str":
            return fmt_float(t)
        if op == "float.to_int":
            # BUG-15 fix: range-check the conversion. Python's int() on a
            # large float (e.g. 1e20) returns a Python int exceeding int64
            # range, which would then propagate as a "valid" int and only
            # trip the next arithmetic op. Panic early here so the error
            # points to the actual source.
            # BUG (deep-scan-5): int() raises OverflowError on inf and
            # ValueError on NaN BEFORE the range check runs — the
            # interpreter crashed with a raw Python traceback while the
            # native runtime panicked cleanly. Check non-finiteness first.
            if t != t or t in (float("inf"), float("-inf")):
                raise HLPanic("float.to_int out of int64 range", line)
            r = int(t)
            if r < INT64_MIN or r > INT64_MAX:
                raise HLPanic("float.to_int out of int64 range", line)
            return r
        if op == "float.abs":
            return abs(t)
        if op == "bool.to_str":
            return b"true" if t else b"false"
        if op == "list.len":
            return len(t)
        if op == "list.push":
            t.append(args[0])
            return None
        if op == "list.get":
            if args[0] < 0 or args[0] >= len(t):
                raise HLPanic("array access out of bounds", line)
            return t[args[0]]
        if op == "list.pop":
            if len(t) == 0:
                # BUG-SC-9 fix: "array access out of bounds" is misleading
                # for pop() — the user called pop() on an empty list, not
                # an index operation. Report the actual problem.
                raise HLPanic("pop from empty list", line)
            return t.pop()
        if op == "list.set":
            if args[0] < 0 or args[0] >= len(t):
                raise HLPanic("array access out of bounds", line)
            t[args[0]] = args[1]
            return None
        if op == "map.len":
            return len(t)
        if op == "map.set":
            t[args[0]] = args[1]
            return None
        if op == "map.get_or":
            v = t.get(args[0])
            return args[1] if v is None else v
        if op == "map.has":
            return args[0] in t
        if op == "map.keys":
            return list(t.keys())
        # ----- Stage 16 (v0.27.0-alpha): Chan / Task methods -----
        if op == "chan.send" or op == "chan.try_send":
            # Boundary ownership rule: an owned message is deep-copied at
            # the send boundary unless it is syntactically clone(...)
            # (already a private deep copy) — the checker already
            # rejected bare variable reads. This mirrors the native
            # codegen's gen path for chan.send/chan.try_send exactly
            # (deep-scan-10: the interpreter previously deep-copied even
            # clone(...) results — harmless but asymmetric with the
            # native move semantics, and wasteful).
            v = args[0]
            is_clone = False
            if arg_nodes:
                a0 = arg_nodes[0]
                if a0.get("k") == "call" and a0.get("name") == "clone":
                    is_clone = True
            if not is_clone and isinstance(v, (list, dict)):
                v = self.deep_clone(v)
            if op == "chan.send":
                self.conc.send(t, v)
                return None
            return self.conc.try_send(t, v)
        if op == "chan.recv":
            return self.conc.recv(t, line)
        if op == "chan.recv_or":
            # Non-blocking recv: message if pending, else the caller's
            # default. The default never crosses a task boundary.
            return self.conc.recv_or(t, args[0])
        if op == "chan.len":
            with self.conc.cv:
                return len(t.q)
        if op == "task.join":
            return self.conc.join(t, line)
        raise HLPanic("unknown builtin method: %s" % op, line)
