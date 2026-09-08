"""Stage-0 type checker & effects analyzer for HLS. Conforms to SPEC.md
sections 3-12 (v0.3) — adds enum, match, generics monomorphisation, the `?`
operator, and struct default field values.

Side effect: annotates the AST so the evaluator can run quickly:
  - every expression has e['t'] = type (or 'never')
  - e['rc'] = ('user', key) | ('builtin', name) for function calls
  - e['rm'] = ('user', key) | ('builtin', op) for method calls
  - enum literals are rewritten in-place: e['k'] = 'enumlit',
    e['enum_name'], e['variant'], e['args'] (instantiated payloads)
  - match arms have arm['body_t'] = arm body type
  - program['edges'] = {fn_key: set(callees)} for effects analysis
"""
from .lexer import HLError
from . import proof as _proof

INT64_MAX = 9223372036854775807


# Stage 10-alpha: built-in taint wrapper `tainted[T]`.
# Single source of truth for taint-type predicates (BUG-SC-12: previously
# `is_taint`/`taint_inner` and `is_tainted_type`/`list_taint_inner` were
# duplicate implementations with slightly different behaviour — the `.strip()`]
# was only in one of them. Consolidated here.)
def is_taint(t):
    return t.startswith("tainted[")


def taint_inner(t):
    """For `tainted[T]` return T; otherwise return t unchanged (defensive —
    keeps callers safe if a non-tainted type slips through). The `.strip()`
    is harmless for parser-produced types (no interior spaces) but keeps the
    function robust if a future caller passes a hand-built type string."""
    if t.startswith("tainted["):
        return t[8:-1].strip()
    return t


# Backwards-compatible aliases (used by boot/boot.py and tools/).
# BUG-SC-12 (for real this time): the old duplicate definitions that used to
# live further down in this file (which shadowed these aliases at import
# time) have been removed — `is_tainted_type`/`list_taint_inner` now resolve
# to exactly these implementations. Single source of truth.
is_tainted_type = is_taint
list_taint_inner = taint_inner


def is_list(t):
    return t.startswith("list[")


def list_elem(t):
    return t[5:-1]


def is_map(t):
    return t.startswith("map[str, ")


def map_val(t):
    return t[9:-1]


# Stage 16 (v0.27.0-alpha): built-in concurrency wrappers.
# `Chan[T]` — a message-passing channel (MPMC, unbounded queue, blocking
# recv). `Task[R]` — a spawned task's join handle (R = spawned fn's return
# type). Both are recognised by the checker as built-in generics; no
# struct/enum definition is needed (same pattern as tainted[T]).
def is_chan(t):
    return t.startswith("Chan[")


def chan_inner(t):
    return t[5:-1]


def is_task(t):
    return t.startswith("Task[")


def task_inner(t):
    return t[5:-1]


# Stage 33 (v0.52.0-alpha): built-in async/await wrappers.
# `Future[T]` — a stackless state machine for asynchronous computation.
# Runtime representation: a `hl_chan*` of capacity 1 (bounded — backpressure
# ensures the producing task is not unbounded-buffered). The "ready" state
# is implicit: a future is ready when its underlying channel has a pending
# message. An immediately-ready future (future_ready(v)) simply has the
# value pre-sent on the channel.
#
# Distinct from Chan[T] at the type-system level so the user cannot
# accidentally call chan.send on a future. The runtime representation is
# identical (a channel is a channel), which keeps the codegen simple.
def is_future(t):
    return t.startswith("Future[")


def future_inner(t):
    return t[7:-1]


# Stage 34 (v0.53.0-alpha): built-in async stream type.
# `Stream[T]` — push-based async analogue of Chan[T]. Runtime representation:
# a `hl_chan*` (bounded — backpressure: a slow consumer blocks the producer).
# Distinct from Chan[T] and Future[T] at the type-system level.
def is_stream(t):
    return t.startswith("Stream[")


def stream_inner(t):
    return t[7:-1]


# NOTE: is_taint / taint_inner / is_tainted_type / list_taint_inner are
# defined ONCE near the top of this file (BUG-SC-12 consolidation). Do NOT
# add duplicate definitions below — they would silently shadow the aliases.


def split_type_args(s):
    """Split a comma-separated list of type strings, respecting nested [ ]."""
    if s == "":
        return []
    parts = []
    depth = 0
    start = 0
    i = 0
    while i < len(s):
        c = s[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(s[start:i].strip())
            start = i + 1
        i += 1
    parts.append(s[start:].strip())
    return parts


def type_base(t):
    """For a type string, return the head identifier (before any `[`)."""
    if "[" in t:
        return t[: t.index("[")]
    return t


def type_args(t):
    """For an instantiated type, return the list of type-arg strings."""
    if "[" not in t:
        return []
    idx = t.index("[")
    return split_type_args(t[idx + 1: -1])


def instantiate_type(t, type_map):
    """Substitute type parameters in `t` according to `type_map`.

    BUG-31 fix: if `type_map[T]` itself contains type params, recurse to
    substitute them as well. The original implementation returned the
    first lookup result without recursing, leaving inner type params
    unbound in the output. To avoid infinite loops on (illegal) self-
    referential maps, we cap recursion at 16 levels.
    """
    return _instantiate_type(t, type_map, 0)


def _instantiate_type(t, type_map, depth):
    if depth > 16:
        return t  # safety cap — should never happen with valid input
    if t in type_map:
        # Recurse on the substituted value: it may itself contain type
        # params that need to be substituted (e.g. type_map[T] =
        # "list[U]", type_map[U] = "int" → "list[int]").
        return _instantiate_type(type_map[t], type_map, depth + 1)
    if t in ("int", "float", "bool", "str", "void"):
        return t
    if is_list(t):
        return "list[" + _instantiate_type(list_elem(t), type_map, depth + 1) + "]"
    if is_map(t):
        return "map[str, " + _instantiate_type(map_val(t), type_map, depth + 1) + "]"
    if is_taint(t):
        return "tainted[" + _instantiate_type(taint_inner(t), type_map, depth + 1) + "]"
    if "[" in t and t.endswith("]"):
        base = type_base(t)
        args = type_args(t)
        new_args = [_instantiate_type(a, type_map, depth + 1) for a in args]
        return base + "[" + ", ".join(new_args) + "]"
    return t


def unify(pt, at, typeparams, type_map):
    """Match a parameter type `pt` against an argument type `at`, binding
    any type params in `typeparams` to concrete types in `type_map`."""
    if pt in typeparams:
        if pt in type_map and type_map[pt] != at:
            return False
        type_map[pt] = at
        return True
    if pt.startswith("list[") and at.startswith("list["):
        return unify(list_elem(pt), list_elem(at), typeparams, type_map)
    if pt.startswith("map[str, ") and at.startswith("map[str, "):
        return unify(map_val(pt), map_val(at), typeparams, type_map)
    if is_taint(pt) and is_taint(at):
        return unify(taint_inner(pt), taint_inner(at), typeparams, type_map)
    if "[" in pt and "[" in at and type_base(pt) == type_base(at):
        pargs = type_args(pt)
        aargs = type_args(at)
        if len(pargs) != len(aargs):
            return False
        ok = True
        for p, a in zip(pargs, aargs):
            if not unify(p, a, typeparams, type_map):
                ok = False
        return ok
    return pt == at


BUILTIN_FNS = {
    "print", "println", "panic", "exit", "str", "int", "len", "range",
    "map_new", "read_file", "write_file", "args", "clock_ms", "chr",
    "file_exists",
    # Stage 36 (v0.55.0-alpha): filesystem metadata builtins.
    # fs_read_dir(path) -> list[str] — list directory entries (names
    #   only, not full paths; the caller can join with the parent
    #   path via std.fs). Panics on I/O error (mirrors read_file).
    # fs_size(path) -> int — file size in bytes. Panics on error.
    # fs_is_dir(path) -> bool — true iff path is a directory.
    #   Returns false for non-existent paths (mirrors file_exists
    #   semantics for files).
    # fs_set_perms(path, mode) -> void — chmod the path. `mode` is
    #   the POSIX permission bits (e.g. 0o644 = 420). Panics on error.
    "fs_read_dir", "fs_size", "fs_is_dir", "fs_set_perms",
    # Stage 37 (v0.56.0-alpha): networking builtins — TCP/UDP sockets
    # and a libcurl-backed HTTPS GET. All carry the Net effect (a
    # program must declare `uses Net` to call any of them). The host /
    # path arguments are taint sinks (see SINK_BUILTINS below): a
    # tainted host enables DNS rebinding + SSRF; a tainted path enables
    # request-smuggling path confusion.
    #
    # net_tcp_connect(host, port) -> int — fd (>=0) or -1 on failure.
    # net_tcp_listen(host, port, backlog) -> int — listen fd or -1.
    # net_tcp_accept(fd) -> int — client fd (blocks until a peer
    #   connects; returns -1 on error).
    # net_read(fd, n) -> str — read up to n bytes; "" at EOF or error.
    # net_write(fd, data) -> int — bytes written; -1 on error.
    # net_close(fd) -> void — close socket (idempotent).
    # net_udp_open() -> int — UDP socket fd or -1.
    # net_udp_send_to(fd, host, port, data) -> int — bytes sent or -1.
    # net_udp_recv_from(fd, n) -> str — received datagram (up to n
    #   bytes); "" if no data / error.
    # net_tls_get(host, port, path) -> str — HTTPS GET body via
    #   libcurl. Panics on transport/TLS error. The body is the raw
    #   response payload (no headers, no status line).
    "net_tcp_connect", "net_tcp_listen", "net_tcp_accept",
    "net_read", "net_write", "net_close",
    "net_udp_open", "net_udp_send_to", "net_udp_recv_from",
    "net_tls_get",
    # Stage 8-alpha ownership primitives (v0.4.0-alpha)
    "drop", "clone", "take",
    # Stage 10-alpha: taint-tracking primitives (v0.7.0-alpha)
    "tainted_args", "taint_mark", "taint_unwrap",
    # Stage 10-beta: more taint sources (v0.8.0-alpha)
    "read_file_tainted",
    # Stage 10 release: read_line — third taint source (stdin). Treats
    # any input read from stdin as untrusted by default, mirroring argv
    # and read_file_tainted. The program must sanitise before passing
    # the value to any sink.
    "read_line",
    # Stage 9 release (v0.20.0-alpha): Net / Rand / Proc builtins.
    "net_lookup", "rand_int", "rand_float", "rand_seed", "proc_exec",
    # Stage 16 (v0.27.0-alpha): concurrency builtins. Stage-16
    # perfection (v0.29.0-alpha) adds chan_new_bounded.
    "chan_new", "chan_new_bounded", "spawn", "select",
    # Stage 33 (v0.52.0-alpha): async/await builtins. async_spawn is
    # like spawn but returns Future[T] (a cap-1 bounded channel
    # carrying the result). await blocks on a future's result.
    # future_ready makes an immediately-ready future. future_poll is
    # non-blocking. future_select races multiple futures.
    "async_spawn", "await", "future_ready", "future_poll",
    "future_select",
    # Stage 34 (v0.53.0-alpha): async stream builtins. stream_new
    # creates a bounded stream. stream_send/recv/try_recv/len are the
    # primitives. stream_close signals end-of-stream. The combinators
    # (stream_map_int / stream_filter_int / stream_take_int /
    # stream_fold_int / stream_merge_int / stream_flat_map_int) are
    # also builtins because they spawn a worker that calls a named
    # function (HLS has no closures).
    "stream_new", "stream_send", "stream_recv", "stream_try_recv",
    "stream_len", "stream_close",
    "stream_map_int", "stream_filter_int", "stream_take_int",
    "stream_fold_int", "stream_merge_int", "stream_flat_map_int",
    "gen_spawn",
    # Stage 19 (v0.35.0-alpha): O(n) string join — the accumulating
    # `a + b` concat is quadratic when building large outputs, which
    # dominated the bootstrap's compile time. The builtin joins a
    # list[str] with a separator in a single allocation.
    "join",
    # Stage 21 (v0.37.0-alpha): SIMD feature dispatch builtins.
    "has_feature", "simd_cpu_supports",
    # Stage 32 (v0.51.0-alpha): native bitwise primitives. Halis has
    # no `&`, `|`, `^`, `~`, `<<`, `>>` operators in its grammar —
    # these builtins map to single C operations in the native backend,
    # eliminating the O(64) bit-by-bit emulation loops that std/bits.hls
    # previously used.
    "int_and", "int_or", "int_xor", "int_not",
    "int_shl", "int_shr", "int_sar",
    "int_popcount", "int_clz", "int_ctz",
    # Stage 46 (v0.65.0-alpha): thread / scheduling builtins.
    # thread_sleep_ms blocks the calling thread for N milliseconds
    # (carries Clock because it observes the wall clock for the sleep
    # duration, plus Conc because it interacts with the concurrency
    # runtime's blocked-thread counter). thread_yield hints the
    # scheduler to switch (no real effect on correctness; pure
    # scheduling optimisation). thread_current_id returns a non-zero
    # int identifying the calling thread (the main thread's ID is
    # consistent across the program; spawned threads get distinct
    # IDs). All three carry Conc.
    "thread_sleep_ms", "thread_yield", "thread_current_id",
}

# Stage 9 (v0.20.0-alpha — release): per-builtin effect mapping.
# Pure builtins (panic, str, int, len, range, map_new, chr, drop, clone,
# take) are absent — they contribute no effect. A builtin may in
# principle contribute multiple effects; using a set value future-proofs
# the design.
#
# Stage 9 release adds three new effect families with builtins:
#   - Net    : net_lookup (DNS resolution)
#   - Rand   : rand_int, rand_float, rand_seed
#   - Proc   : proc_exec (subprocess via system())
# These are NOT part of the IO family; a program must declare them
# explicitly to use the corresponding builtins.
BUILTIN_EFFECTS = {
    "print":       {"IO"},
    "println":     {"IO"},
    "read_file":   {"Fs"},
    "write_file":  {"Fs"},
    "file_exists": {"Fs"},
    # Stage 36 (v0.55.0-alpha): filesystem metadata builtins — all
    # carry the Fs effect (they touch the filesystem). fs_set_perms
    # is also a sink for path-traversal attacks (a tainted path
    # would chmod an attacker-chosen file), so it is registered in
    # SINK_BUILTINS below.
    "fs_read_dir":  {"Fs"},
    "fs_size":      {"Fs"},
    "fs_is_dir":    {"Fs"},
    "fs_set_perms": {"Fs"},
    "clock_ms":    {"Clock"},
    "args":        {"Args"},
    "exit":        {"Exit"},
    # Stage 10-alpha: tainted_args carries the Args effect (same as args).
    "tainted_args": {"Args"},
    # Stage 10-beta: read_file_tainted carries the Fs effect (same as
    # read_file) and returns a tainted[str].
    "read_file_tainted": {"Fs"},
    # Stage 10 release: read_line carries the IO effect (reads from stdin)
    # and returns a tainted[str]. This is the third taint source.
    "read_line": {"IO"},
    # taint_mark / taint_unwrap are pure (no side effect; just wrap/unwrap).
    # Stage 9 release: Net / Rand / Proc builtins.
    "net_lookup":  {"Net"},
    # Stage 37 (v0.56.0-alpha): TCP / UDP / TLS builtins — all carry
    # the same Net effect as net_lookup. A single `uses Net` clause
    # unlocks every networking primitive (DNS, TCP, UDP, TLS). Taint-
    # sink enforcement below prevents tainted hosts/paths from
    # reaching any of them — effect discipline controls CAPABILITY,
    # taint discipline controls DATA FLOW.
    "net_tcp_connect":   {"Net"},
    "net_tcp_listen":    {"Net"},
    "net_tcp_accept":    {"Net"},
    "net_read":          {"Net"},
    "net_write":         {"Net"},
    "net_close":         {"Net"},
    "net_udp_open":      {"Net"},
    "net_udp_send_to":   {"Net"},
    "net_udp_recv_from": {"Net"},
    "net_tls_get":       {"Net"},
    "rand_int":    {"Rand"},
    "rand_float":  {"Rand"},
    "rand_seed":   {"Rand"},
    "proc_exec":   {"Proc"},
    # Stage 16 (v0.27.0-alpha): the concurrency effect. Every task /
    # channel operation carries `Conc` — a function that spawns, joins,
    # sends, receives, or selects must declare `uses Conc`. This keeps the
    # "no uses clause => pure and deterministic" guarantee intact.
    "chan_new":         {"Conc"},
    "chan_new_bounded": {"Conc"},
    "spawn":            {"Conc"},
    "select":           {"Conc"},
    # Stage 33 (v0.52.0-alpha): async/await — all carry Conc (they
    # drive the same concurrency runtime as spawn/chan).
    "async_spawn":      {"Conc"},
    "await":            {"Conc"},
    "future_ready":     {"Conc"},
    "future_poll":      {"Conc"},
    "future_select":    {"Conc"},
    # Stage 34 (v0.53.0-alpha): async streams — same Conc effect.
    "stream_new":            {"Conc"},
    "stream_send":           {"Conc"},
    "stream_recv":           {"Conc"},
    "stream_try_recv":       {"Conc"},
    "stream_len":            {"Conc"},
    "stream_close":          {"Conc"},
    "stream_map_int":        {"Conc"},
    "stream_filter_int":     {"Conc"},
    "stream_take_int":       {"Conc"},
    "stream_fold_int":       {"Conc"},
    "stream_merge_int":      {"Conc"},
    "stream_flat_map_int":   {"Conc"},
    "gen_spawn":             {"Conc"},
    # Stage 46 (v0.65.0-alpha): thread / scheduling builtins.
    # thread_sleep_ms carries BOTH Clock (it observes the wall clock
    # for the sleep duration) AND Conc (it interacts with the
    # concurrency runtime's blocked-thread counter, so the deadlock
    # detector knows the thread is parked rather than runnable).
    # thread_yield and thread_current_id carry Conc only.
    "thread_sleep_ms":       {"Clock", "Conc"},
    "thread_yield":          {"Conc"},
    "thread_current_id":     {"Conc"},
    # Builtin METHODS with effects (the first method-level effects —
    # previously all I/O lived in builtin functions):
    "chan.send":     {"Conc"},
    "chan.try_send": {"Conc"},
    "chan.recv":     {"Conc"},
    "chan.recv_or":  {"Conc"},
    "chan.len":      {"Conc"},
    "task.join":     {"Conc"},
}

# Types whose values are "owned" heap allocations — subject to move tracking.
# Primitives (int/float/bool) are Copy: passing them never moves.
def is_owned_type(t):
    """True if values of this type are heap-owned and subject to move tracking."""
    if t in ("int", "float", "bool", "void", "never"):
        return False
    return True  # str, list, map, struct, enum — all heap-allocated in v0.4


# Stage 10-alpha: taint tracking.
# A value is "tainted" if its type is `tainted[T]` (the wrapper defined in
# std/taint.hls). The checker statically rejects passing a tainted value
# into a SINK (console output, filesystem, process exit). The user must
# explicitly untaint via a sanitizer (sanitize_html / sanitize_path /
# sanitize_sql_identifier / sanitize_sql_string / sanitize_command /
# sanitize_filename) or `taint_unwrap` (the "I know what I'm doing"
# escape hatch) before reaching the sink.
#
# SINK_BUILTINS is the SINGLE SOURCE OF TRUTH for taint sink enforcement.
# Each entry maps a sink builtin name to the tuple of tainted-rejecting
# argument indexes (0-based). The check_builtin_call function consults
# this table via reject_tainted_at_sink.
SINK_BUILTINS = {
    "print":        (0,),   # the message is the taint vector
    "println":      (0,),
    "read_file":    (0,),   # tainted path → path-traversal
    "write_file":   (0, 1),  # tainted path or content → both bad
    "file_exists":  (0,),   # tainted path → information disclosure / traversal
    "exit":         (0,),    # tainted exit code → behavior-injection
    # Stage 9 release (v0.20.0-alpha): Net / Proc builtins as sinks.
    # net_lookup's host is a sink because a tainted host enables DNS
    # rebinding attacks (an attacker who controls the host can make
    # the program connect to a different IP than the user intended).
    "net_lookup":   (0,),
    # proc_exec's command is a sink because a tainted command enables
    # shell injection (an attacker who controls the command can run
    # arbitrary shell code in the program's privilege context).
    "proc_exec":    (0,),
    # Stage 36 (v0.55.0-alpha): filesystem metadata builtins as sinks.
    # fs_read_dir's path is a sink because a tainted path enables
    # directory enumeration of attacker-chosen locations (information
    # disclosure). fs_size and fs_is_dir likewise disclose information
    # about attacker-chosen paths. fs_set_perms is the most dangerous:
    # a tainted path lets the attacker chmod an arbitrary file
    # (e.g. /etc/passwd → 0o666).
    "fs_read_dir":  (0,),
    "fs_size":      (0,),
    "fs_is_dir":    (0,),
    "fs_set_perms": (0,),
    # Stage 37 (v0.56.0-alpha): networking builtins as sinks.
    # A tainted HOST enables SSRF + DNS rebinding (attacker connects
    # the program to an internal service or redirects traffic to a
    # capture endpoint). A tainted PATH enables request smuggling
    # (path-confusion attacks where a front-end proxy and a back-end
    # server disagree about the resource). The DATA being sent is NOT
    # a sink — the user is the origin of the data, not the attacker.
    # The PORT is an int (untaintable) — the type system already
    # prevents taint flow through ints.
    "net_tcp_connect":  (0,),    # host
    "net_tcp_listen":   (0,),    # host
    "net_udp_send_to":  (1,),    # host (arg 1, after fd: arg 0)
    "net_tls_get":      (0, 2),  # host (arg 0) AND path (arg 2)
}

# NOTE: is_tainted_type / list_taint_inner are ALIASES defined once at the
# top of this file (see is_taint / taint_inner). The duplicate definitions
# that used to live here shadowed those aliases at import time (F811) and
# have been removed for real — do not reintroduce them.

STR_M = {
    "len": ([], "int"), "byte_at": (["int"], "int"),
    "slice": (["int", "int"], "str"), "find": (["str"], "int"),
    "contains": (["str"], "bool"), "starts_with": (["str"], "bool"),
    "ends_with": (["str"], "bool"), "split": (["str"], "list[str]"),
    "trim": ([], "str"), "to_int": ([], "int"), "to_str": ([], "str"),
    "to_float": ([], "float"),
}
INT_M = {"to_str": "str", "to_float": "float", "abs": "int"}
FLOAT_M = {"to_str": "str", "to_int": "int", "abs": "float"}
BOOL_M = {"to_str": "str"}


class Checker:
    def __init__(self, program):
        self.p = program
        self.structs = program["structs"]
        self.enums = program.get("enums", {})
        self.fns = program["fns"]
        self.edges = {}
        self.methods = {}  # struct -> {meth: key}
        self.cur_fn = None
        self.cur_fn_ret = "void"  # for `?` propagation
        self.cur_typeparams = set()  # type params valid in the current context
        # Structs that declare at least one defaulted field — constructing
        # one may evaluate the default expressions (side effects!), so
        # check_structlit adds an edge to the synthetic "@default.<Struct>"
        # call-graph node (BUG-DS4-2).
        self._structs_with_defaults = set()
        # Stage 8-beta: > 0 while checking a while/for condition or
        # iterable — take()/drop() are rejected there (a move would
        # re-execute on every iteration).
        self.loop_header = 0
        # Stage 17: seeded interval facts per fn (for --check --fast and
        # hlprove reporting).
        self.proof_facts = {}
        # Stage 31 (v0.48.0-alpha): fn key -> number of VERIFIED tail
        # self-call sites (filled by tail_call_check_fn).
        self.tail_sites = {}

    # ---------- utilities ----------
    def err(self, msg, node):
        # Deep-scan-7 fix: always pass col=0 to HLError, losing column
        # information that the AST already carries. The lexer / parser
        # report real columns; the checker should too. Use the node's
        # `col` if present, else fall back to 0.
        col = node.get("col", 0) if isinstance(node, dict) else 0
        raise HLError(msg, node.get("line", 0) if isinstance(node, dict) else 0, col)

    def type_exists(self, t, node):
        if t in ("int", "float", "bool", "str"):
            return True
        if t in self.cur_typeparams:
            return True
        if is_list(t):
            return self.type_exists(list_elem(t), node)
        if is_map(t):
            return self.type_exists(map_val(t), node)
        if is_taint(t):
            # Stage 10-alpha: tainted[T] is a built-in generic wrapper; the
            # inner type must exist but `tainted` itself needs no struct
            # or enum definition.
            return self.type_exists(taint_inner(t), node)
        if is_chan(t):
            # Stage 16: Chan[T] is a built-in generic wrapper.
            return self.type_exists(chan_inner(t), node)
        if is_task(t):
            # Stage 16: Task[R] is a built-in generic wrapper (R may be
            # void — only allowed as a join result type).
            inner = task_inner(t)
            if inner == "void":
                return True
            return self.type_exists(inner, node)
        if is_future(t):
            # Stage 33: Future[T] is a built-in generic wrapper. T must
            # exist and cannot be void (a future carries a value).
            return self.type_exists(future_inner(t), node)
        if is_stream(t):
            # Stage 34: Stream[T] is a built-in generic wrapper. T must
            # exist and cannot be void.
            return self.type_exists(stream_inner(t), node)
        base = type_base(t)
        args = type_args(t)
        if base in self.structs or base in self.enums:
            tp = self.structs[base]["typeparams"] if base in self.structs \
                else self.enums[base]["typeparams"]
            if len(tp) != len(args):
                return False
            for a in args:
                if not self.type_exists(a, node):
                    return False
            return True
        return False

    def require_type(self, t, node, what):
        if t == "void":
            self.err("cannot use 'void' as %s" % what, node)
        if not self.type_exists(t, node):
            self.err("type does not exist: %s" % t, node)

    def resolve_struct(self, t):
        """If t is a (possibly generic) struct type, return (StructInfo, type_map).
        Otherwise return None."""
        base = type_base(t)
        if base not in self.structs:
            return None
        st = self.structs[base]
        tp = st["typeparams"]
        args = type_args(t)
        if len(tp) != len(args):
            return None
        type_map = dict(zip(tp, args))
        return (st, type_map)

    def resolve_enum(self, t):
        """If t is a (possibly generic) enum type, return (EnumInfo, type_map).
        Otherwise return None."""
        base = type_base(t)
        if base not in self.enums:
            return None
        en = self.enums[base]
        tp = en["typeparams"]
        args = type_args(t)
        if len(tp) != len(args):
            return None
        type_map = dict(zip(tp, args))
        return (en, type_map)

    # ---------- lifecycle ----------
    def check(self):
        # 1. group methods by struct
        for key, fn in self.fns.items():
            if fn["struct"] is not None:
                m = self.methods.setdefault(fn["struct"], {})
                m[fn["name"]] = key
        # 2. check declarations (struct/enum field types, function signatures)
        for name, st in self.structs.items():
            self.cur_typeparams = set(st["typeparams"])
            # BUG-DS4-2 fix: struct field default expressions are evaluated
            # in the CALLING context at runtime (both eval_structlit in the
            # interpreter and gen_structlit in the C backend evaluate the
            # default expr per construction), and they may call other
            # functions. The call graph therefore needs a synthetic node per
            # struct ("@default.<Struct>") whose effects propagate to every
            # function that constructs that struct. Previously check_expr
            # inside a default crashed with `KeyError: None` because
            # self.cur_fn was None while self.edges[None] does not exist.
            default_key = "@default." + name
            self.edges[default_key] = set()
            saved_fn = self.cur_fn
            saved_ret = self.cur_fn_ret
            self.cur_fn = default_key
            self.cur_fn_ret = "void"
            # BUG-SC-7 fix: type-check struct field DEFAULT expressions.
            # Previously defaults were not checked, so `struct Foo { x: int = "hi" }`
            # compiled cleanly and only failed at runtime (type-safety hole).
            # We check each default in an empty env (no bindings in scope).
            has_defaults = False
            for fname, ftype, fdefault in st["fields"]:
                self.require_type(ftype, st, "struct field type")
                if fdefault is not None:
                    has_defaults = True
                    dv = self.check_expr(fdefault, [{}], ftype)
                    if dv != "never" and dv != ftype:
                        self.err("default value of field '%s' expects %s, got %s"
                                 % (fname, ftype, dv), st)
            if has_defaults:
                self._structs_with_defaults.add(name)
            self.cur_fn = saved_fn
            self.cur_fn_ret = saved_ret
            self.cur_typeparams = set()
        for ename, en in self.enums.items():
            self.cur_typeparams = set(en["typeparams"])
            for vname, payloads in en["variants"]:
                for pt in payloads:
                    self.require_type(pt, en, "variant payload type")
            self.cur_typeparams = set()
        for key, fn in self.fns.items():
            self.cur_typeparams = set(fn.get("typeparams", []))
            if fn["struct"] is None:
                if fn["name"] in BUILTIN_FNS:
                    self.err("cannot redefine builtin function: %s" % fn["name"], fn)
            else:
                if fn["struct"] not in self.structs:
                    self.err("impl for non-existent struct: %s" % fn["struct"], fn)
                if not fn["params"]:
                    self.err("method must have 'self' as first parameter", fn)
                sname, stype, _ = fn["params"][0]
                if sname != "self" or stype != fn["struct"]:
                    self.err("method must have first parameter 'self: %s'"
                             % fn["struct"], fn)
            for pn, pt, _ in fn["params"]:
                self.require_type(pt, fn, "parameter type")
            if fn["ret"] != "void" and not self.type_exists(fn["ret"], fn):
                self.err("return type does not exist: %s" % fn["ret"], fn)
            self.edges[key] = set()
            self.cur_typeparams = set()
        if "main" not in self.fns:
            self.err("missing main function", {"line": 1})
        mainf = self.fns["main"]
        if mainf["struct"] is not None:
            self.err("main cannot be a method", mainf)
        if mainf["params"]:
            self.err("main cannot have parameters", mainf)
        if mainf["ret"] not in ("int", "void"):
            self.err("main must return 'int' or have no return type", mainf)
        # 3. check function bodies
        for key, fn in self.fns.items():
            self.check_fn(key, fn)
        # 3.5 Stage 30 (v0.47.0-alpha): escape analysis (boxed-vs-stack
        # layout). Runs AFTER the bodies are checked: the walker reads
        # e["rm"] (the builtin-method tag check_method sets) to classify
        # borrow-safe receivers. Enforces the #[stack] soundness proof
        # (a stack-allocated value can never escape its creating frame)
        # and validates #[stack]/#[boxed] applicability.
        for key, fn in self.fns.items():
            self.escape_check_fn(key, fn)
        # 3.6 Stage 31 (v0.48.0-alpha): verified tail-call optimisation.
        # Runs after the bodies are checked (the walker reads the
        # expression TYPES check_expr annotated and the rc tags
        # check_call set). Verifies every #[tail_call] fn: tail position
        # of every self-call, primitive-only dataflow (the "no cleanup
        # needed between the call and the return" proof).
        for key, fn in self.fns.items():
            self.tail_call_check_fn(key, fn)
        # 4. effects analysis (fixpoint on the call graph)
        self.check_effects()

    # ---------- environment ----------
    # Bindings are now [type, mut, moved] (3-tuple) — `moved` is True after
    # `drop(x)` or `take(x)`, which forbids subsequent use of `x` until it is
    # re-assigned. See sections 16 (Stage 8-alpha) in SPEC.md.
    def new_env(self, fn):
        env = [{}]
        if fn["struct"] is not None:
            sname, stype, smut = fn["params"][0]
            env[0][sname] = [stype, smut, False]
            params = fn["params"][1:]
        else:
            params = fn["params"]
        for pn, pt, pm in params:
            if pn in env[0]:
                self.err("duplicate parameter name: %s" % pn, fn)
            env[0][pn] = [pt, pm, False]
        return env

    def lookup(self, env, name):
        for scope in reversed(env):
            if name in scope:
                return scope[name]
        return None

    def mark_moved(self, env, name):
        """Mark a binding as moved (drop/take). Returns True on success."""
        for scope in reversed(env):
            if name in scope:
                scope[name][2] = True
                return True
        return False

    def revive_binding(self, env, name):
        """Re-clear moved status on reassignment (binding becomes 're-owned')."""
        for scope in reversed(env):
            if name in scope:
                scope[name][2] = False
                return True
        return False

    def snapshot_moved(self, env):
        """Take a snapshot of moved-status for all bindings (for scope restore)."""
        snap = []
        for scope in env:
            row = {}
            for name, b in scope.items():
                row[name] = b[2]
            snap.append(row)
        return snap

    def restore_moved(self, env, snap):
        """Restore moved-status from a snapshot (used when child scope exits)."""
        for scope, snap_row in zip(env, snap):
            for name, moved in snap_row.items():
                if name in scope:
                    scope[name][2] = moved

    def union_moved(self, env, snap):
        """Deep-scan-16 HIGH-severity soundness fix: OR the moved-status
        from `snap` (representing ONE possible execution path) into `env`.
        A binding is treated as moved after the construct if it was moved
        along ANY path — e.g. inside either arm of an `if`, or after one
        iteration of a loop body (which re-executes at runtime).

        The previous behaviour (snapshot + full restore_moved) treated
        every if-arm / loop body as if it might execute ZERO times
        (post-construct state = pre-construct state). That is unsound:
        at runtime, an if-arm MAY execute and a loop body MAY iterate
        >= 1 time, leaving the binding nulled in the native codegen
        (drop/take push the local to null_after, so `u_x = NULL` is
        emitted at statement end). Post-construct uses of the binding
        then read NULL and segfault.

        The conservative union (a binding is moved if it COULD be moved
        along any path) is SOUND: it rejects post-construct uses whose
        runtime safety cannot be proven. The trade-off is that some
        valid programs are rejected (e.g. `while false { take(x) }; use(x)`
        — provably zero iterations, but the checker can't prove that
        without flow analysis). Reassignment via `x = ...` (a `mut`
        binding) is still allowed because check_assign's revive step
        clears the moved flag, so the idiomatic "drop then revive"
        pattern continues to work."""
        for scope, snap_row in zip(env, snap):
            for name, moved in snap_row.items():
                if name in scope and moved:
                    scope[name][2] = True

    def check_fn(self, key, fn):
        self.cur_fn = key
        self.cur_fn_ret = fn["ret"]
        saved_typeparams = self.cur_typeparams
        self.cur_typeparams = set(fn.get("typeparams", []))
        env = self.new_env(fn)
        # Stage 17 (v0.28.0-alpha): validate the contract clauses...
        self.check_contracts(key, fn, env)
        self.check_stmts(fn["body"], env, fn, False)
        # ...and AFTER the body is checked (annotations read e['t']),
        # run the interval proof pass.
        self.run_proof_pass(key, fn)
        # Stage 15 (v0.13.0-alpha): extern fns have NO body — they are
        # forward declarations. Skip the "must return on all paths" check.
        if (not fn.get("extern", False)
                and fn["ret"] != "void"
                and not self.all_return(fn["body"])):
            self.err("function '%s' does not return on all paths" % fn["name"], fn)
        self.cur_typeparams = saved_typeparams

    # ---------- Stage 30 (v0.47.0-alpha): escape analysis ----------
    # Boxed-vs-stack layout analysis for list[int] / list[float] /
    # list[bool] bindings. A binding whose EVERY use stays inside the
    # creating function (borrow-safe positions only: .get/.set/.len
    # receiver, xs[i] index base, for-in iterable) can be allocated on
    # the C stack by the self-hosted compiler — zero heap objects, zero
    # refcount traffic. #[stack] FORCES the stack layout (any escaping
    # use is a compile error — the value provably never outlives its
    # frame); #[boxed] FORCES the heap layout (opt-out of the automatic
    # analysis). The boot checker mirrors the hlc checker's analysis so
    # `boot.py --check` rejects exactly the same programs.

    # Borrow-safe builtin list methods (receiver only).
    ESC_SAFE_LIST_METHODS = ("list.get", "list.set", "list.len")
    ESC_PRIM_ELEMS = ("int", "float", "bool")

    def escape_check_fn(self, key, fn):
        """Run the Stage 30 escape analysis over one function body."""
        if fn.get("extern", False):
            return
        # Generic functions (and methods of generic structs) are
        # instantiated per type-argument set; their codegen runs under
        # mangled keys, so layout decisions taken on the source key would
        # not apply. Skip the automatic analysis; #[stack] there is a
        # clear, documented error rather than a silent no-op.
        generic = bool(fn.get("typeparams"))
        if fn["struct"] is not None and fn["struct"] in self.structs:
            if self.structs[fn["struct"]].get("typeparams"):
                generic = True
        if generic:
            self._escape_check_generic_attrs(fn)
            return
        # Phase 1 — collect candidate bindings + validate forced attrs.
        cand = {}  # name -> decision record
        self._esc_collect_stmts(fn["body"], cand)
        # Phase 2 — classify every use of every candidate.
        self._esc_walk_stmts(fn["body"], cand)
        # Phase 3 — decide layouts + report #[stack] violations.
        for name in cand:  # dict preserves discovery order (deterministic)
            c = cand[name]
            if c["forced_stack"]:
                if c["esc"]:
                    self.err("#[stack] violated: '%s' escapes its creating "
                             "frame — %s (line %d). A stack-allocated value "
                             "can NEVER outlive the function that created "
                             "it: remove the escaping use or drop the "
                             "attribute." % (name, c["esc"], c["esc_line"]),
                             {"line": c["line"]})
                if c["push"]:
                    self.err("#[stack] violated: '%s' uses push() (line %d) — "
                             "a stack-allocated list is fixed-capacity "
                             "(capacity %d, taken from its literal). Use "
                             ".get/.set within the capacity or drop the "
                             "attribute." % (name, c["push_line"], c["cap"]),
                             {"line": c["line"]})
                if c["pop"]:
                    self.err("#[stack] violated: '%s' uses pop() (line %d) — "
                             "a stack-allocated list is fixed-capacity; its "
                             "length is part of the layout and cannot "
                             "shrink. Drop the attribute to use pop()."
                             % (name, c["pop_line"]), {"line": c["line"]})
            # Final layout (consumed by the codegen side of the mirror
            # contract; the interpreter itself is layout-agnostic).
            if c["boxed"]:
                c["layout"] = "boxed"
            elif (not c["esc"]) and (not c["push"]) and (not c["pop"]):
                c["layout"] = "stack"
            else:
                c["layout"] = "heap"

    def _escape_check_generic_attrs(self, fn):
        """Reject #[stack]/#[boxed] inside generic functions / methods
        with a precise message (the automatic analysis is simply skipped
        there — layout is instantiation-dependent)."""
        def visit(stmts):
            for s in stmts:
                if s["k"] == "let" and (s.get("stack") or s.get("boxed")):
                    attr = "stack" if s.get("stack") else "boxed"
                    self.err("#[%s] is not supported inside generic "
                             "functions yet (the layout would depend on "
                             "the type instantiation); drop the attribute "
                             "here" % attr, s)
                elif s["k"] in ("if", "while"):
                    visit(s["body"])
                    if s["k"] == "if" and s.get("els"):
                        visit(s["els"])
                elif s["k"] == "for":
                    visit(s["body"])
        visit(fn["body"])

    def _esc_candidate(self, s):
        """Is this let-stmt a stack-layout candidate? Requires a
        list[primitive] binding initialised from a NON-EMPTY list
        literal (the capacity is fixed at compile time)."""
        t = s["t"]
        if not is_list(t):
            return False
        if list_elem(t) not in self.ESC_PRIM_ELEMS:
            return False
        v = s["value"]
        if v.get("k") != "listlit":
            return False
        return len(v["items"]) > 0

    def _esc_collect_stmts(self, stmts, cand):
        for s in stmts:
            k = s["k"]
            if k == "let":
                forced_stack = bool(s.get("stack"))
                boxed = bool(s.get("boxed"))
                if forced_stack or boxed:
                    attr = "stack" if forced_stack else "boxed"
                    t = s["t"]
                    if not is_list(t):
                        self.err("#[%s] applies to a list[T] binding; '%s' "
                                 "has type '%s'" % (attr, s["name"], t), s)
                    if forced_stack:
                        if list_elem(t) not in self.ESC_PRIM_ELEMS:
                            self.err("#[stack] requires a primitive element "
                                     "type (int / float / bool); '%s' is "
                                     "'%s' whose elements are reference-"
                                     "counted (boxed) values — a documented "
                                     "Stage 30 limitation" % (s["name"], t), s)
                        v = s["value"]
                        if v.get("k") != "listlit":
                            self.err("#[stack] requires a list-literal "
                                     "initializer (the capacity is fixed at "
                                     "compile time); '%s' is initialised "
                                     "from a call/expression" % s["name"], s)
                        elif len(v["items"]) == 0:
                            self.err("#[stack] on an empty list literal has "
                                     "no capacity — every .get() would "
                                     "panic; use a non-empty literal or "
                                     "drop the attribute", s)
                if self._esc_candidate(s) and not boxed:
                    v = s["value"]
                    cand[s["name"]] = {
                        "line": s["line"], "cap": len(v["items"]),
                        "forced_stack": forced_stack, "boxed": boxed,
                        "esc": None, "esc_line": 0,
                        "push": False, "push_line": 0,
                        "pop": False, "pop_line": 0,
                        "layout": "heap",
                    }
            elif k == "if":
                self._esc_collect_stmts(s["then"], cand)
                if s.get("els"):
                    self._esc_collect_stmts(s["els"], cand)
            elif k == "while":
                self._esc_collect_stmts(s["body"], cand)
            elif k == "for":
                self._esc_collect_stmts(s["body"], cand)

    def _esc_walk_stmts(self, stmts, cand):
        for s in stmts:
            k = s["k"]
            if k == "let":
                v = s["value"]
                if v.get("k") == "listlit":
                    # Element expressions of ANY list literal (including a
                    # candidate's own literal — its elements are primitive
                    # expressions and cannot alias a tracked binding, but
                    # walk them anyway for uniformity).
                    for it in v["items"]:
                        self._esc_expr(it, cand, False,
                                       "element of a list literal")
                else:
                    self._esc_expr(v, cand, False,
                                   "initializer of '%s'" % s["name"])
            elif k == "assign":
                tgt = s["target"]
                if tgt["k"] == "ident":
                    if tgt["name"] in cand:
                        c = cand[tgt["name"]]
                        if not c["esc"]:
                            c["esc"] = "reassigned (assignment target)"
                            c["esc_line"] = tgt.get("line", 0)
                elif tgt["k"] == "index" and tgt["target"].get("k") == "ident":
                    # xs[i] = v — the base binding is the index base (a
                    # borrow-safe position); only the index expr escapes.
                    self._esc_expr(tgt["idx"], cand, False,
                                   "index expression")
                else:
                    self._esc_expr(tgt, cand, False, "assignment target")
                self._esc_expr(s["value"], cand, False, "assigned value")
            elif k == "if":
                self._esc_expr(s["cond"], cand, False, "if condition")
                self._esc_walk_stmts(s["then"], cand)
                if s.get("els"):
                    self._esc_walk_stmts(s["els"], cand)
            elif k == "while":
                self._esc_expr(s["cond"], cand, False, "while condition")
                self._esc_walk_stmts(s["body"], cand)
            elif k == "for":
                it = s["iter"]
                if it.get("k") == "ident" and it["name"] in cand:
                    pass  # for-in iterable: borrow-safe position
                else:
                    self._esc_expr(it, cand, False, "for-in iterable")
                self._esc_walk_stmts(s["body"], cand)
            elif k == "return":
                if s["value"] is not None:
                    self._esc_expr(s["value"], cand, False, "return value")
            elif k == "expr":
                self._esc_expr(s["e"], cand, False,
                               "discarded expression value")

    def _esc_expr(self, e, cand, safe, why):
        """Walk an expression, classifying every occurrence of a tracked
        binding. `safe=True` means the CURRENT position is borrow-safe
        (receiver of .get/.set/.len, index base); method/index nodes
        re-derive the flag for their own children, so the ambient flag
        only matters when an ident node is reached directly."""
        if e is None or not isinstance(e, dict):
            return
        k = e.get("k")
        if k == "ident":
            name = e["name"]
            if name in cand:
                c = cand[name]
                if not safe and not c["esc"]:
                    c["esc"] = why
                    c["esc_line"] = e.get("line", 0)
            return
        if k == "method":
            rm = e.get("rm")
            mname = e.get("name", "?")
            tgt = e["target"]
            if rm and rm[0] == "builtin" and rm[1] in self.ESC_SAFE_LIST_METHODS:
                # .get/.set/.len receiver is borrow-safe.
                self._esc_expr(tgt, cand, True, "receiver")
            elif rm and rm[1] in ("list.push", "list.pop") and \
                    tgt.get("k") == "ident" and tgt["name"] in cand:
                # push/pop on a tracked binding: record the specific
                # growth/shrink use (a dedicated error class — fixed
                # capacity).
                c = cand[tgt["name"]]
                if rm[1] == "list.push" and not c["push"]:
                    c["push"] = True
                    c["push_line"] = tgt.get("line", 0)
                elif rm[1] == "list.pop" and not c["pop"]:
                    c["pop"] = True
                    c["pop_line"] = tgt.get("line", 0)
            else:
                self._esc_expr(tgt, cand, False,
                               "receiver of '%s()'" % mname)
            for a in e.get("args", []):
                self._esc_expr(a, cand, False,
                               "argument of '%s()'" % mname)
            return
        if k == "index":
            self._esc_expr(e["target"], cand, True, "index base")
            self._esc_expr(e["idx"], cand, False, "index expression")
            return
        if k == "call":
            for a in e.get("args", []):
                self._esc_expr(a, cand, False,
                               "argument of call '%s()'" % e.get("name", "?"))
            return
        # Generic children (bin: l/r, un/qmark: e, field/fieldcall:
        # target, match: scrut + arm bodies, structlit: fields).
        for ck in ("e", "l", "r", "scrut", "target"):
            child = e.get(ck)
            if isinstance(child, dict) and "k" in child:
                self._esc_expr(child, cand, False, why)
        if k == "match":
            for arm in e.get("arms", []):
                self._esc_expr(arm["body"], cand, False, "match arm body")
        elif k == "listlit":
            for it in e.get("items", []):
                self._esc_expr(it, cand, False, "element of a list literal")
        elif k == "structlit":
            for fname, fexpr in e.get("fields", []):
                self._esc_expr(fexpr, cand, False,
                               "field '%s' of a struct literal" % fname)
        for a in e.get("args", []):
            if isinstance(a, dict) and "k" in a:
                self._esc_expr(a, cand, False, why)

    # ---------- Stage 31 (v0.48.0-alpha): verified tail calls ----------
    # #[tail_call] on a function asserts that every recursive call to it
    # is in VERIFIED tail position. Mirrors the hlc.hls checker exactly
    # (same rules, same error messages) so `boot.py --check` rejects
    # exactly the same programs the self-hosted compiler rejects:
    #
    #   1. Position — every self-call must be the ENTIRE return
    #      expression (`return f(...)`), never nested inside an
    #      operator / argument / let binding / discarded statement.
    #
    #   2. No cleanup — the interpreter trampoline (and the native
    #      goto transform) rebind parameters in place, so nothing
    #      refcounted may flow through the function: parameters, return
    #      type, every let binding and every expression must be
    #      int / float / bool. The ONLY exception is a string LITERAL
    #      passed directly to panic / print / println (consume-builtins
    #      in both backends — the argument is transferred, never bound).
    #
    #   3. Scope — plain fns only (not generic, not a method, no
    #      contracts, not irq_handler / inline(always), primitive ret).
    #
    # The interpreter mirrors the native transform as a trampoline: a
    # verified tail `return f(...)` raises TailCallSig, and call_fn
    # rebinds the parameters and re-runs the body in the SAME Python
    # frame — fib_tail(1_000_000) needs no Python recursion either.

    TAIL_PRIM_TYPES = ("int", "float", "bool")

    def _tail_is_self_call(self, e, key):
        """Is expression node e a direct call to fn `key`?"""
        if not isinstance(e, dict) or e.get("k") != "call":
            return False
        rc = e.get("rc")
        return rc is not None and rc[0] == "user" and rc[1] == key

    def _tail_check_expr(self, e, key):
        """Verify one expression subtree of a #[tail_call] fn."""
        if e is None or not isinstance(e, dict):
            return
        k = e.get("k")
        if k in ("int", "float", "bool", "ident"):
            return
        if k == "str":
            self.err("#[tail_call] violated: '%s' cannot use string values "
                     "(line %d) — only int / float / bool may flow through "
                     "a tail-call function (a string would need release at "
                     "the loop jump; string LITERALS are only allowed as "
                     "the direct argument of panic / print / println)"
                     % (key, e.get("line", 0)), e)
            return
        if k == "call":
            # A self-call outside a return-value position is never tail.
            rc = e.get("rc")
            if rc is not None and rc[0] == "user" and rc[1] == key:
                self.err("#[tail_call] violated: the recursive call to "
                         "'%s' at line %d is not in tail position — every "
                         "recursive call must be the entire return "
                         "expression (`return %s(...)`)"
                         % (key, e.get("line", 0), key), e)
                return
            # Consume-builtin exception: panic / print / println with
            # ONE string literal argument.
            if rc is not None and rc[0] == "builtin" and \
                    rc[1] in ("panic", "print", "println"):
                args = e.get("args", [])
                if len(args) != 1 or args[0].get("k") != "str":
                    self.err("#[tail_call] violated: the %s call at line %d "
                             "must take exactly one string LITERAL — a "
                             "computed string would create a refcounted "
                             "temporary needing cleanup between the tail "
                             "call and the jump"
                             % (rc[1], e.get("line", 0)), e)
                return
            # Any other call: the RESULT must be primitive (void/never
            # allowed for statement position), then walk the arguments.
            t = e.get("t")
            if t not in ("void", "never") and t not in self.TAIL_PRIM_TYPES:
                self.err("#[tail_call] violated: '%s' contains a non-"
                         "primitive expression of type '%s' (line %d) — "
                         "only int / float / bool values may flow through "
                         "a tail-call function (refcounted values would "
                         "need cleanup between the tail call and the jump)"
                         % (key, t, e.get("line", 0)), e)
                return
            for a in e.get("args", []):
                self._tail_check_expr(a, key)
            return
        if k == "method":
            t = e.get("t")
            if t not in ("void", "never") and t not in self.TAIL_PRIM_TYPES:
                self.err("#[tail_call] violated: '%s' contains a non-"
                         "primitive expression of type '%s' (line %d) — "
                         "only int / float / bool values may flow through "
                         "a tail-call function" % (key, t, e.get("line", 0)), e)
                return
            self._tail_check_expr(e.get("target"), key)
            for a in e.get("args", []):
                self._tail_check_expr(a, key)
            return
        # Every other node kind: the node's own type must be primitive.
        t = e.get("t")
        if t not in ("void", "never") and t not in self.TAIL_PRIM_TYPES:
            self.err("#[tail_call] violated: '%s' contains a non-primitive "
                     "expression of type '%s' (line %d) — only int / "
                     "float / bool values may flow through a tail-call "
                     "function (refcounted values would need cleanup "
                     "between the tail call and the jump)"
                     % (key, t, e.get("line", 0)), e)
            return
        # Walk children (bin: l/r, un/qmark: e, field/fieldcall/index:
        # target/idx, match: scrut + arm bodies, listlit: items,
        # structlit: fields, enumlit: args).
        for ck in ("e", "l", "r", "scrut", "target", "idx"):
            child = e.get(ck)
            if isinstance(child, dict) and "k" in child:
                self._tail_check_expr(child, key)
        if k == "match":
            for arm in e.get("arms", []):
                self._tail_check_expr(arm.get("body"), key)
        elif k == "listlit":
            for it in e.get("items", []):
                self._tail_check_expr(it, key)
        elif k == "structlit":
            for fname, fexpr in e.get("fields", []):
                self._tail_check_expr(fexpr, key)
        for a in e.get("args", []):
            if isinstance(a, dict) and "k" in a:
                self._tail_check_expr(a, key)

    def _tail_check_stmts(self, stmts, key):
        """Walk the body. Returns the number of verified tail sites."""
        sites = 0
        for s in stmts:
            k = s["k"]
            if k == "return":
                v = s.get("value")
                if v is not None:
                    if self._tail_is_self_call(v, key):
                        # The verified tail site. Its ARGUMENTS still go
                        # through the primitive-dataflow check.
                        for a in v.get("args", []):
                            self._tail_check_expr(a, key)
                        sites += 1
                    else:
                        self._tail_check_expr(v, key)
            elif k == "let":
                if s["t"] not in self.TAIL_PRIM_TYPES:
                    self.err("#[tail_call] violated: '%s' cannot bind non-"
                             "primitive values — `let %s: %s` at line %d. "
                             "A refcounted binding would need release at "
                             "the loop jump, which the verified transform "
                             "forbids (keep the body to int / float / bool)"
                             % (key, s["name"], s["t"], s.get("line", 0)), s)
                self._tail_check_expr(s.get("value"), key)
            elif k == "assign":
                self._tail_check_expr(s.get("target"), key)
                self._tail_check_expr(s.get("value"), key)
            elif k == "if":
                self._tail_check_expr(s.get("cond"), key)
                sites += self._tail_check_stmts(s.get("then") or [], key)
                if s.get("els"):
                    sites += self._tail_check_stmts(s["els"], key)
            elif k == "while":
                self._tail_check_expr(s.get("cond"), key)
                sites += self._tail_check_stmts(s.get("body") or [], key)
            elif k == "for":
                # for-in iterates a list — never primitive. The iter
                # expression check reports it.
                self._tail_check_expr(s.get("iter"), key)
                sites += self._tail_check_stmts(s.get("body") or [], key)
            elif k == "expr":
                self._tail_check_expr(s.get("e"), key)
            # break / continue: no expressions.
        return sites

    def tail_call_check_fn(self, key, fn):
        """The Stage 31 entry point — verify one #[tail_call] fn."""
        attrs = fn.get("attrs", {})
        if not attrs.get("tail_call", False):
            return
        if fn.get("extern", False):
            self.err("#[tail_call] cannot be applied to an extern function", fn)
            return
        if fn["struct"] is not None:
            self.err("#[tail_call] is not supported on methods yet ('%s' is "
                     "a method of '%s')" % (fn["name"], fn["struct"]), fn)
            return
        if fn.get("typeparams"):
            self.err("#[tail_call] is not supported on generic functions yet "
                     "('%s' has type parameters) — instantiate a concrete "
                     "wrapper instead" % fn["name"], fn)
            return
        if fn.get("requires") is not None or fn.get("ensures") is not None:
            self.err("#[tail_call] and contracts (requires / ensures) are "
                     "mutually exclusive on '%s' — the postcondition check "
                     "would interpose cleanup between the tail call and "
                     "the jump" % fn["name"], fn)
            return
        # Parameters: primitive only. The transform rebinds them in
        # place; a refcounted parameter would need release at the jump.
        for pn, pt, _ in fn["params"]:
            if pt not in self.TAIL_PRIM_TYPES:
                self.err("#[tail_call] on '%s' requires all parameters to "
                         "be int / float / bool; parameter '%s: %s' is not "
                         "— refcounted values would need cleanup between "
                         "the tail call and the jump, which the verified "
                         "transform forbids" % (fn["name"], pn, pt), fn)
        # Return type: the loop feeds a value back; void has nothing.
        if fn["ret"] not in self.TAIL_PRIM_TYPES:
            self.err("#[tail_call] on '%s' requires the return type to be "
                     "int / float / bool, not '%s' (a tail call must feed "
                     "a value back through the loop)" % (fn["name"], fn["ret"]), fn)
        # Body: position + dataflow verification.
        sites = self._tail_check_stmts(fn["body"], key)
        if sites == 0:
            self.err("#[tail_call] on '%s' found no recursive call to "
                     "itself — remove the attribute (the tail-call "
                     "transform needs a self-call to turn into a loop)"
                     % fn["name"], fn)
            return
        self.tail_sites[key] = sites

    # ---------- Stage 17: contracts ----------
    def check_contracts(self, key, fn, env):
        """Validate `requires` / `ensures` expressions and, for functions
        with a requires clause, run the interval proof pass over the
        body (annotating provably-safe operations for -O fast)."""
        params = fn["params"]
        req = fn.get("requires")
        ens = fn.get("ensures")
        # Contract environment: ONLY the parameters (+ `result` for
        # ensures). Nothing else is in scope.
        cenv = [{}]
        for pn, pt, _ in params:
            cenv[0][pn] = [pt, False, False]
        if req is not None:
            t = self.check_expr(req, cenv, None)
            if t not in ("bool", "never"):
                self.err("requires clause of '%s' must be bool, got %s"
                         % (fn["name"], t), req)
            self.check_contract_purity(req, fn, "requires")
        if ens is not None:
            if fn["ret"] == "void":
                self.err("ensures clause on void function '%s' (there is "
                         "no result)" % fn["name"], ens)
            cenv[0]["result"] = [fn["ret"], False, False]
            t = self.check_expr(ens, cenv, None)
            if t not in ("bool", "never"):
                self.err("ensures clause of '%s' must be bool, got %s"
                         % (fn["name"], t), ens)
            self.check_contract_purity(ens, fn, "ensures")
    def run_proof_pass(self, key, fn):
        """Stage 17: seed interval facts from the (validated) requires
        clause and annotate the body's provably-safe operations. Runs
        AFTER the body was type-checked — the annotations read e['t']."""
        req = fn.get("requires")
        if req is None or fn.get("extern", False):
            return
        facts = _proof.seed_from_requires(req, fn["params"], 0)
        _proof.propagate_stmts(fn["body"], facts)
        for var, iv in facts.items():
            # Deep-scan-10: skip the engine's NUL-prefixed internal keys
            # (non-zero set, minimum-length map) — they are not variables.
            if not isinstance(var, str) or var.startswith("\x00"):
                continue
            if not isinstance(iv, _proof.Interval):
                continue
            self.proof_facts.setdefault(key, {})[var] = str(iv)

    def check_contract_purity(self, e, fn, which):
        """Contract expressions must be pure and side-effect-free: no
        calls to user functions or effectful builtins; only literals,
        params, arithmetic, comparisons, len() / .len() and field reads
        of params are allowed.

        Deep-scan-10 fix: the walk now descends into EVERY child shape
        (index targets/indices, field targets, match scrutinees and arm
        bodies, list literal items, struct/enum literal payloads, and
        the `?` operand). The old walk only recursed into l/r/e/args,
        so an impure call hidden under an index (`requires double(x)[0]
        > 0`) or inside a match arm compiled cleanly — a hole in SPEC
        §26.1's purity rule."""
        if not isinstance(e, dict):
            return
        k = e["k"]
        if k == "call":
            if e.get("rc") == ("builtin", "len"):
                for a in (e.get("args") or []):
                    self.check_contract_purity(a, fn, which)
                return
            name = e.get("name", "")
            self.err("contract expression calls '%s' — contracts must be "
                     "pure (only literals, parameters, arithmetic, "
                     "comparisons and len() are allowed)" % name, e)
        if k == "method":
            name = e.get("name", "")
            if name == "len":
                self.check_contract_purity(e.get("target"), fn, which)
                return
            self.err("contract expression calls method '%s' — contracts "
                     "must be pure" % name, e)
        for sub in (e.get("l"), e.get("r"), e.get("e")):
            self.check_contract_purity(sub, fn, which)
        for a in (e.get("args") or []):
            self.check_contract_purity(a, fn, which)
        if k == "index":
            self.check_contract_purity(e.get("target"), fn, which)
            self.check_contract_purity(e.get("idx"), fn, which)
        if k == "field":
            self.check_contract_purity(e.get("target"), fn, which)
        if k == "match":
            self.check_contract_purity(e.get("scrut"), fn, which)
            for arm in (e.get("arms") or []):
                self.check_contract_purity(arm.get("body"), fn, which)
        if k == "qmark":
            self.check_contract_purity(e.get("e"), fn, which)
        if k in ("listlit", "structlit", "enumlit"):
            for a in (e.get("items") or e.get("args") or []):
                self.check_contract_purity(a, fn, which)
            for _, fe in (e.get("fields") or []):
                self.check_contract_purity(fe, fn, which)

    def all_return(self, stmts):
        if not stmts:
            return False
        last = stmts[-1]
        if last["k"] == "return":
            return True
        if last["k"] == "expr" and last["e"].get("t") == "never":
            return True
        # BUG-SC-5 fix: a `let` or `assign` whose RHS is `never`-typed
        # (e.g. `let x: int = panic("...")` or `x = exit(0)`) always
        # diverges, so the function returns on all paths. Previously
        # this was rejected with a spurious "does not return on all
        # paths" error.
        if last["k"] == "let" and last["value"].get("t") == "never":
            return True
        if last["k"] == "assign" and last["value"].get("t") == "never":
            return True
        if last["k"] == "if" and last["els"] is not None:
            return self.all_return(last["then"]) and self.all_return(last["els"])
        if last["k"] == "expr" and last["e"]["k"] == "match" and \
           self.match_all_return(last["e"]):
            return True
        return False

    def match_all_return(self, e):
        # BUG-008 fix: arm["body"] is the result of parse_expr (parser.py:540)
        # — it's always an EXPRESSION node (call, match, bin, enumlit, …),
        # never a statement node ("return" or "expr"). The original check
        # therefore always returned False. The correct check is: every arm
        # must be `never`-typed (i.e. its body is something that doesn't
        # fall through, like panic() or exit()).
        for arm in e["arms"]:
            if arm["body"].get("t") != "never":
                return False
        return True

    def check_stmts(self, stmts, env, fn, in_loop):
        for s in stmts:
            self.check_stmt(s, env, fn, in_loop)

    def child(self, env):
        env.append({})
        return env

    # ---------- statements ----------
    def check_stmt(self, s, env, fn, in_loop):
        k = s["k"]
        if k == "let":
            self.require_type(s["t"], s, "variable type")
            if self.lookup(env, s["name"]) is not None:
                self.err("shadowing not allowed: %s" % s["name"], s)
            vt = self.check_expr(s["value"], env, s["t"])
            if vt == "never":
                env[-1][s["name"]] = [s["t"], s["mut"], False]
                return
            if vt != s["t"]:
                self.err("type mismatch: declared %s but got %s"
                         % (s["t"], vt), s)
            env[-1][s["name"]] = [s["t"], s["mut"], False]
        elif k == "assign":
            self.check_assign(s, env)
        elif k == "if":
            ct = self.check_expr(s["cond"], env, None)
            if ct != "bool":
                self.err("if condition must be bool, got %s" % ct, s)
            # Deep-scan-16 HIGH-severity soundness fix: a binding moved
            # inside EITHER arm may be moved at runtime (the arm could
            # have executed). The previous snapshot+full-restore treated
            # post-if state as pre-if state — a binding moved in an if-
            # arm compiled cleanly through subsequent uses, but at
            # runtime the native codegen had already nulled the local
            # (`u_x = NULL` after drop/take). Sound fix: UNION the moved-
            # status across both arms (a binding is moved if it was moved
            # along any path).
            snap = self.snapshot_moved(env)
            self.child(env)
            self.check_stmts(s["then"], env, fn, in_loop)
            env.pop()
            then_snap = self.snapshot_moved(env)
            self.restore_moved(env, snap)
            if s["els"] is not None:
                self.child(env)
                self.check_stmts(s["els"], env, fn, in_loop)
                env.pop()
                else_snap = self.snapshot_moved(env)
                self.restore_moved(env, snap)
                self.union_moved(env, then_snap)
                self.union_moved(env, else_snap)
            else:
                # No else arm — equivalent to an empty else (no moves).
                # Union just the then-arm state.
                self.union_moved(env, then_snap)
        elif k == "while":
            self.loop_header += 1
            ct = self.check_expr(s["cond"], env, None)
            self.loop_header -= 1
            if ct != "bool":
                self.err("while condition must be bool, got %s" % ct, s)
            # Deep-scan-16 HIGH-severity soundness fix: the loop body may
            # execute >= 1 time. A binding moved inside the body (drop/take)
            # is nulled at runtime after the first iteration. Post-loop
            # uses of the binding read NULL and segfault. The previous
            # snapshot+restore cleared moves done in the body, so
            #   `while true { take(xs); break } let y = xs[0]`
            # compiled cleanly but crashed at runtime.
            # Sound fix: do NOT restore moves done in the body. The
            # idiomatic "drop then revive via mut reassignment" pattern
            # still works because check_assign's revive step clears the
            # moved flag.
            #
            # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-09
            # fix. The previous `snap = self.snapshot_moved(env)` line
            # was dead code — the deep-scan-16 fix removed the matching
            # `restore_moved` call (soundness), but left the snapshot
            # allocation as a per-iteration dict that was immediately
            # discarded. Removed it for clarity (a reader thinks the
            # snapshot is used; it isn't).
            self.child(env)
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
            # No restore_moved — moves done in the body propagate to
            # post-loop state (soundness: assume the body executed).
        elif k == "for":
            self.loop_header += 1
            it = self.check_expr(s["iter"], env, None)
            self.loop_header -= 1
            if not is_list(it):
                self.err("for-in expression must be a list, got %s" % it, s)
            elem = list_elem(it)
            if s["vtype"] != elem:
                self.err("loop variable type %s does not match element %s"
                         % (s["vtype"], elem), s)
            # Deep-scan-16: same soundness fix as `while` — a binding moved
            # inside the for-body may be nulled at runtime after the first
            # iteration. Post-loop uses must be rejected.
            #
            # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-09
            # fix. Removed the dead `snap = self.snapshot_moved(env)`
            # line (no matching restore_moved; deep-scan-16 fix left it
            # as dead code).
            self.child(env)
            # BUG (deep-scan-5): the `let` branch rejects shadowing but the
            # `for` branch never checked — a loop variable could silently
            # shadow an outer binding (SPEC §4: no shadowing).
            if self.lookup(env, s["var"]) is not None:
                self.err("cannot shadow outer variable with the loop "
                         "variable: %s" % s["var"], s)
            env[-1][s["var"]] = [elem, False, False]
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
            # No restore_moved — moves done in the body propagate.
        elif k == "return":
            if fn["ret"] == "void":
                if s["value"] is not None:
                    self.err("void function cannot return a value", s)
            else:
                if s["value"] is None:
                    self.err("function returning %s must return a value" % fn["ret"], s)
                vt = self.check_expr(s["value"], env, fn["ret"])
                if vt != fn["ret"] and vt != "never":
                    self.err("return type mismatch: expected %s, got %s"
                             % (fn["ret"], vt), s)
        elif k == "break":
            if not in_loop:
                self.err("break only allowed inside a loop", s)
        elif k == "continue":
            if not in_loop:
                self.err("continue only allowed inside a loop", s)
        elif k == "expr":
            self.check_expr(s["e"], env, None)
        elif k == "asm":
            # Stage 27 (v0.50.0-alpha): inline-assembly statement.
            # The boot checker accepts the AST (the boot interpreter
            # cannot execute asm, but the program is still parseable
            # and the self-hosted compiler hlc.hls is the canonical
            # path that lowers asm! to GCC extended asm). Validation:
            #   * `in` operands must be primitive (int/float/bool).
            #   * `out`/`inout`/`late_out` operands must be a mutable
            #     lvalue of a primitive type.
            #   * `{N}` template placeholders must reference an
            #     existing operand (N in [0, len(operands))).
            #   * `pure` requires at least one output operand (a
            #     pure asm with no output is dead code).
            #   * `noreturn` requires no `out`/`inout`/`late_out`
            #     (no point writing outputs that are never observed).
            self.check_asm(s, env)
        else:
            self.err("unknown statement: %s" % k, s)

    # Stage 27 (v0.50.0-alpha): asm! checker. See the boot parser's
    # parse_asm_stmt for the AST node shape.
    def check_asm(self, s, env):
        template = s["template"]
        operands = s["operands"]
        options = s["options"]
        # Validate the template's {N} placeholders.
        n_ops = len(operands)
        i = 0
        while i < len(template):
            ch = ord(template[i])
            # `{` starts a placeholder; `{{` is a literal `{`.
            if ch == 123:  # '{'
                if i + 1 < len(template) and ord(template[i + 1]) == 123:
                    i = i + 2
                    continue
                # Find the matching `}`.
                j = i + 1
                while j < len(template) and ord(template[j]) != 125:
                    j = j + 1
                if j >= len(template):
                    self.err("asm! template has unterminated '{' (no "
                             "matching '}'): %r" % template, s)
                body = template[i + 1:j]
                # body should be a non-negative integer.
                try:
                    n = int(body)
                except ValueError:
                    self.err("asm! template placeholder {%s} is not a "
                             "number (expected {0}, {1}, ...)" % body, s)
                    return  # unreachable — err raises
                if n < 0 or n >= n_ops:
                    self.err("asm! template placeholder {%d} is out of "
                             "range (have %d operand(s))" % (n, n_ops), s)
                i = j + 1
            elif ch == 125:  # '}'
                if i + 1 < len(template) and ord(template[i + 1]) == 125:
                    i = i + 2
                    continue
                self.err("asm! template has lone '}' (use '}}' for a "
                         "literal '}'): %r" % template, s)
            else:
                i = i + 1
        # Validate each operand.
        has_output = False
        for op in operands:
            d = op["dir"]
            if d not in ("in", "out", "inout", "late_out"):
                self.err("asm! operand direction '%s' is unknown" % d, s)
            if d != "in":
                has_output = True
            # Constraint validation (lightweight — the codegen re-validates).
            c = op["constraint"]
            if not op["is_constraint_str"]:
                if c not in ("reg", "mem", "imm"):
                    self.err("asm! constraint '%s' is unknown (bare "
                             "constraints: reg, mem, imm; for a "
                             "specific register use a string like "
                             "\"eax\")" % c, s)
                # Stage 27 perfection (v0.50.2-alpha) deep-scan-17:
                # `imm` is an immediate-constant constraint — it cannot
                # be used as an output (you can't write to a constant).
                # `in(imm) expr` is technically valid in GCC but requires
                # `expr` to be a compile-time constant; the HLS compiler
                # does not constant-fold in general, so reject `imm` on
                # outputs unconditionally.
                #
                # Stage 27 perfection (v0.50.3-alpha) deep-scan-18:
                # BUG-10 fix. Also reject `in(imm)` on a NON-literal
                # expression — GCC's `i` constraint requires a compile-
                # time constant. Without this check, `in(imm) some_var`
                # was accepted by the HLS checker and reached GCC as
                # `"i"(some_var)`, producing an opaque "impossible
                # constraint" error with no source-line attribution.
                # Now the user gets a clear HLS error pointing at the
                # asm! line, suggesting `reg` instead.
                if c == "imm" and d != "in":
                    self.err("asm! constraint 'imm' cannot be used with "
                             "direction '%s' — an immediate is a compile-"
                             "time constant and cannot be written to" % d,
                             s)
                if c == "imm" and d == "in":
                    in_expr = op["expr"]
                    in_kind = in_expr["k"] if isinstance(in_expr, dict) else None
                    # Accept integer literals, bool literals, and
                    # unary-minus of integer literals (the common
                    # constant-expr forms). Reject everything else —
                    # the user should use `reg` for runtime values.
                    if in_kind == "un" and in_expr.get("op") == "-":
                        inner = in_expr.get("e")
                        if isinstance(inner, dict) and inner.get("k") == "int":
                            in_kind = "int"  # treat -5 as a constant
                    if in_kind not in ("int", "bool"):
                        self.err("asm! `in(imm)` requires a compile-time "
                                 "constant (int/bool literal or unary-"
                                 "minus-of-int-literal), got a runtime "
                                 "expression — use `reg` instead for "
                                 "runtime values", s)
            else:
                # Stage 27 perfection (v0.50.2-alpha) deep-scan-17:
                # String-form constraint (a register name like "eax" or
                # a GCC constraint letter like "a"). Validate against an
                # allowlist of known x86-64 register names + GCC single-
                # letter constraints; reject anything else with a clear
                # HLS error (otherwise the malformed constraint silently
                # reaches GCC, which produces an opaque C compile error
                # with no source-line attribution).
                cl = c.lower() if isinstance(c, str) else c
                # GCC single-letter constraint letters we accept.
                known_single = ("a", "b", "c", "d", "D", "S", "q", "r",
                                 "f", "t", "u", "y", "x", "A", "Q", "R")
                # x86-64 register names we accept (the codegen translates
                # these to GCC constraint letters; see translate_asm_constraint).
                known_reg = ("eax", "rax", "ebx", "rbx", "ecx", "rcx",
                             "edx", "rdx", "esi", "rsi", "edi", "rdi",
                             "al", "bl", "cl", "dl", "ah", "bh", "ch", "dh",
                             "ax", "bx", "cx", "dx", "si", "di", "bp", "sp",
                             "rax", "rbx", "rcx", "rdx", "rsi", "rdi",
                             "rbp", "rsp", "r8", "r9", "r10", "r11",
                             "r12", "r13", "r14", "r15")
                # If the user already prefixed the constraint with `=` or
                # `+` (e.g. `out("=r") x`), validate the BODY (after the
                # prefix). The codegen has special-case logic to skip
                # re-adding the prefix when one is already present.
                body = cl
                prefix = ""
                if isinstance(c, str) and len(c) >= 1 and c[0] in ("=", "+"):
                    prefix = c[0]
                    body = c[1:].lower()
                    # `&` is the late-clobber marker; strip it for the
                    # body check.
                    if len(body) >= 1 and body[0] == "&":
                        body = body[1:]
                if body not in known_single and body not in known_reg:
                    self.err("asm! constraint '%s' is not a recognised "
                             "x86-64 register name or GCC constraint "
                             "letter (known: eax/rax/ebx/rbx/ecx/rcx/"
                             "edx/rdx/esi/rsi/edi/rdi, or single-letter "
                             "GCC constraints a/b/c/d/D/S/q/r/A)" % c, s)
                # Direction-vs-prefix consistency: `in("=r") x` is wrong
                # (`=` means write-only output). `out("r") x` is OK (the
                # codegen adds `=`). `in("+r") x` is also wrong.
                if d == "in" and prefix in ("=", "+"):
                    self.err("asm! constraint '%s' has a direction prefix "
                             "'%s' but is used with `in` (input operands "
                             "have no prefix — drop the '%s')" %
                             (c, prefix, prefix), s)
                if d == "out" and prefix == "+":
                    self.err("asm! constraint '%s' has prefix '+' (read-"
                             "write) but is used with `out` (write-only) "
                             "— use `inout` if you want read-write" % c, s)
            # Validate the operand's value/lvalue type.
            if d == "in":
                vt = self.check_expr(op["expr"], env, None)
                if vt not in ("int", "float", "bool"):
                    self.err("asm! `in` operand must be a primitive "
                             "(int/float/bool), got %s" % vt, s)
            else:
                # out/inout/late_out — must be a mutable lvalue.
                if op["var_kind"] != "ident" and op["var_kind"] != "field" \
                        and op["var_kind"] != "index":
                    self.err("asm! %s operand must be an lvalue" % d, s)
                # Use check_lvalue to annotate + verify the binding.
                tgt = op["expr"]
                # the parser stored the lvalue as `expr`; treat it as
                # an assignment-target to enforce mutability.
                root = tgt
                while root["k"] in ("field", "index"):
                    root = root["target"]
                binding = self.lookup(env, root["name"])
                if binding is None:
                    self.err("variable does not exist: %s" % root["name"], s)
                # `out(reg) x` writes to x — x must be mut OR be a
                # field/index target (mutation through a reference).
                # We allow `out` on a non-mut lvalue ONLY when the
                # lvalue is a field/index (writing through a borrowed
                # reference). For an ident, `mut` is required.
                if tgt["k"] == "ident" and not binding[1]:
                    self.err("asm! %s operand must be a `mut` binding "
                             "(cannot write to immutable variable %s)"
                             % (d, root["name"]), s)
                if d == "inout":
                    if len(binding) >= 3 and binding[2]:
                        self.err("asm! inout operand uses a moved value: "
                                 "%s" % root["name"], s)
                tt = self.check_lvalue(tgt, env)
                if tt not in ("int", "float", "bool"):
                    self.err("asm! %s operand must be a primitive "
                             "(int/float/bool), got %s" % (d, tt), s)
                # Annotate the operand with the resolved type for
                # the codegen (matches the self-hosted checker).
                op["t"] = tt
        # Option validation.
        # Stage 27 perfection (v0.50.2-alpha) deep-scan-17: reject
        # duplicate option names (e.g. `options(pure, pure)` is a
        # user mistake — the asm accepts the duplicate silently, which
        # is a foot-gun: a user who fat-fingers `options(nomem, nomem)`
        # gets no signal that they probably meant `options(nomem,
        # preserves_flags)`).
        seen_opts = set()
        for opt in options:
            if opt in seen_opts:
                self.err("asm! option '%s' appears more than once in "
                         "the options list (duplicates are a no-op but "
                         "usually indicate a typo)" % opt, s)
            seen_opts.add(opt)
        # Stage 27 perfection (v0.50.2-alpha) deep-scan-17:
        # `pure` + `noreturn` are contradictory — `pure` says the asm
        # may be elided if outputs are unused, but `noreturn` says it
        # doesn't fall through (eliding a noreturn silently turns it
        # into a return). Check this BEFORE the pure-requires-output
        # and noreturn-forbids-outputs checks so the user gets the
        # clearest error message for this contradiction (otherwise
        # they'd get a side-effect error like "pure requires outputs"
        # which doesn't explain the underlying contradiction).
        if "pure" in options and "noreturn" in options:
            self.err("asm! options `pure` and `noreturn` are "
                     "contradictory — `pure` says the asm may be "
                     "elided if its outputs are unused, but `noreturn` "
                     "says it doesn't fall through (eliding it would "
                     "silently turn a noreturn into a return)", s)
        # Stage 27 perfection (v0.50.2-alpha) deep-scan-17:
        # `pure` without `nomem` is contradictory — `pure` says the
        # asm has no side effects, but the default `"memory"` clobber
        # says it may read/write memory (a memory side effect). Rust's
        # `asm!` requires `pure` to be paired with `nomem`; we follow
        # the same rule for soundness.
        if "pure" in options and "nomem" not in options:
            self.err("asm! with `options(pure)` requires `nomem` too — "
                     "a pure asm must not have memory side effects (the "
                     "default `\"memory\"` clobber would contradict "
                     "`pure`)", s)
        if "pure" in options and not has_output:
            self.err("asm! with `options(pure)` requires at least one "
                     "output operand (a pure asm with no outputs is dead "
                     "code — gcc would legitimately delete it)", s)
        if "noreturn" in options and has_output:
            self.err("asm! with `options(noreturn)` cannot have output "
                     "operands (the asm does not fall through, so the "
                     "outputs would never be observed)", s)

    def check_assign(self, s, env):
        tgt = s["target"]
        # find root binding
        root = tgt
        while root["k"] in ("field", "index"):
            root = root["target"]
        binding = self.lookup(env, root["name"])
        if binding is None:
            self.err("variable does not exist: %s" % root["name"], s)
        # Stage 8-alpha: use-after-move check on the LHS root.
        # Reading a moved binding (even to reassign) requires `mut` first.
        # For `mut x = ...`, the binding is "revived" — its moved flag is cleared.
        # For `x.field = ...` / `xs[i] = ...`, the binding itself is being read
        # (to obtain the reference), so moved bindings cannot be used.
        if len(binding) >= 3 and binding[2]:
            if tgt["k"] == "ident":
                # Whole-binding assignment: this is allowed on `let mut` only.
                # The binding will be revived below after the RHS is checked.
                if not binding[1]:
                    self.err("cannot reassign immutable variable: %s" % root["name"], s)
            else:
                # Field/index assignment — uses the binding's value (reference).
                self.err("use of moved value: %s" % root["name"], s)
        else:
            # 'mut' only governs REASSIGNMENT of the binding (name = v).
            # Field/index assignment mutates CONTENTS through a reference — no mut needed.
            if tgt["k"] == "ident" and not binding[1]:
                self.err("cannot reassign immutable variable: %s" % root["name"], s)
        tt = self.check_lvalue(tgt, env)
        vt = self.check_expr(s["value"], env, tt)
        if vt == "never":
            return
        if vt != tt:
            self.err("type mismatch on assignment: expected %s, got %s" % (tt, vt), s)
        # Stage 8-alpha: revive the binding (clear moved) on whole-binding
        # assignment. The binding now owns a fresh value.
        if tgt["k"] == "ident":
            self.revive_binding(env, root["name"])

    def check_lvalue(self, e, env):
        # Stage 8-beta: every branch annotates e["t"] so downstream
        # consumers (codegen, linters) can read the lvalue's static type.
        if e["k"] == "ident":
            b = self.lookup(env, e["name"])
            e["t"] = b[0]
            return b[0]
        if e["k"] == "field":
            bt = self.check_expr(e["target"], env, None)
            info = self.resolve_struct(bt)
            if info is None:
                self.err("cannot access field on type %s" % bt, e)
            st, type_map = info
            for fname, ftype, _ in st["fields"]:
                if fname == e["name"]:
                    ft = instantiate_type(ftype, type_map) if type_map else ftype
                    e["t"] = ft
                    return ft
            self.err("struct %s has no field %s" % (type_base(bt), e["name"]), e)
        if e["k"] == "index":
            tt = self.check_expr(e["target"], env, None)
            if not is_list(tt):
                self.err("cannot use index on type %s" % tt, e)
            it = self.check_expr(e["idx"], env, None)
            if it != "int":
                self.err("index must be int, got %s" % it, e)
            et = list_elem(tt)
            e["t"] = et
            return et
        self.err("invalid lvalue", e)

    # ---------- expressions ----------
    def check_expr(self, e, env, expected):
        k = e["k"]
        if k == "int":
            if e["v"] > INT64_MAX:
                self.err("integer literal too large (exceeds int64)", e)
            e["t"] = "int"
        elif k == "float":
            e["t"] = "float"
        elif k == "bool":
            e["t"] = "bool"
        elif k == "str":
            e["t"] = "str"
        elif k == "ident":
            b = self.lookup(env, e["name"])
            if b is not None:
                # Stage 8-alpha: use-after-move check
                if len(b) >= 3 and b[2]:
                    self.err("use of moved value: %s" % e["name"], e)
                e["t"] = b[0]
            elif e["name"] in self.enums:
                # bare enum name used as a value — only valid inside an enum
                # variant literal `Enum.Variant`; otherwise it's a type error.
                self.err("enum name '%s' cannot be used as a value" % e["name"], e)
            elif e["name"] in self.structs:
                self.err("struct name '%s' cannot be used as a value" % e["name"], e)
            else:
                self.err("variable does not exist: %s" % e["name"], e)
        elif k == "bin":
            e["t"] = self.check_bin(e, env)
        elif k == "un":
            vt = self.check_expr(e["e"], env, None)
            if e["op"] == "!":
                if vt != "bool":
                    self.err("! operator requires bool, got %s" % vt, e)
                e["t"] = "bool"
            else:
                # Stage 43 deep-scan fix: allow `never` as operand (e.g.
                # `-panic("...")`), propagating `never` as the result type.
                if vt == "never":
                    e["t"] = "never"
                elif vt not in ("int", "float"):
                    self.err("- operator requires int/float, got %s" % vt, e)
                else:
                    e["t"] = vt
        elif k == "index":
            tt = self.check_expr(e["target"], env, None)
            if tt == "never":
                self.err("never value cannot be used in expression", e)
            if not is_list(tt):
                self.err("cannot use index on type %s" % tt, e)
            it = self.check_expr(e["idx"], env, None)
            if it != "int":
                self.err("index must be int, got %s" % it, e)
            e["t"] = list_elem(tt)
        elif k == "field":
            # Disambiguate: if target is `ident` matching an enum name AND no
            # variable with that name exists in scope, this is an enum variant
            # with no payload.
            if e["target"]["k"] == "ident" and \
               e["target"]["name"] in self.enums and \
               self.lookup(env, e["target"]["name"]) is None:
                self.check_enum_variant(e, env, expected, with_args=False)
            else:
                tt = self.check_expr(e["target"], env, None)
                if tt == "never":
                    self.err("never value cannot be used in expression", e)
                info = self.resolve_struct(tt)
                if info is None:
                    self.err("cannot access field on type %s" % tt, e)
                st, type_map = info
                e["t"] = None
                for fname, ftype, _ in st["fields"]:
                    if fname == e["name"]:
                        e["t"] = instantiate_type(ftype, type_map) if type_map else ftype
                        break
                if e["t"] is None:
                    self.err("struct %s has no field %s" % (type_base(tt), e["name"]), e)
        elif k == "fieldcall":
            # Disambiguate: if target is `ident` matching an enum name AND no
            # variable with that name exists, this is an enum variant with
            # payload. Otherwise it's a method call.
            if e["target"]["k"] == "ident" and \
               e["target"]["name"] in self.enums and \
               self.lookup(env, e["target"]["name"]) is None:
                self.check_enum_variant(e, env, expected, with_args=True)
            else:
                # Rewrite as a method call.
                e["k"] = "method"
                e["t"] = self.check_method(e, env)
        elif k == "method":
            e["t"] = self.check_method(e, env)
        elif k == "call":
            e["t"] = self.check_call(e, env, expected)
        elif k == "listlit":
            e["t"] = self.check_listlit(e, env, expected)
        elif k == "structlit":
            e["t"] = self.check_structlit(e, env, expected)
        elif k == "match":
            e["t"] = self.check_match(e, env, expected)
        elif k == "qmark":
            e["t"] = self.check_qmark(e, env, expected)
        # BUG-SC-10 fix: removed the dead `elif k == "mapnew":` branch.
        # The parser never produces a `mapnew` AST node — `map_new()` is
        # parsed as a `call` node with name="map_new" and dispatched via
        # check_call -> check_builtin_call. This branch was unreachable.
        elif k == "enumlit":
            # Already rewritten during a previous visit (e.g. nested); re-evaluate.
            self.check_enum_variant(e, env, expected, with_args=len(e.get("args", [])) > 0)
        else:
            self.err("unknown expression: %s" % k, e)
        return e["t"]

    def check_enum_variant(self, e, env, expected, with_args):
        """Check an enum variant literal. The node `e` is either:
          - {k: 'field', target: {k:'ident', name: EnumName}, name: VariantName}
            — no payload
          - {k: 'fieldcall', target: {...}, name: VariantName, args: [...]}
            — with payload
        The node is rewritten in-place to k='enumlit'.

        SCAN-A fix: the old code re-ran `check_expr` on type disagreement,
        re-executing `drop`/`take` move-marking and producing spurious
        'use of moved value' errors. The first pass now uses the
        contextual expected payload type (inferred from the enum's
        declared payload types) so a re-check is unnecessary in nearly
        every case. The fallback re-check is restricted to the case
        where the first pass returned a placeholder type (containing '?')
        — for those, we re-check but with the contextual type passed in
        so it should produce the same answer (no second-pass divergence).
        """
        ename = e["target"]["name"]
        vname = e["name"]
        if ename not in self.enums:
            self.err("enum does not exist: %s" % ename, e)
        edef = self.enums[ename]
        variant = None
        for v, payloads in edef["variants"]:
            if v == vname:
                variant = (v, payloads)
                break
        if variant is None:
            self.err("enum %s has no variant %s" % (ename, vname), e)
        v, payloads = variant
        args = e.get("args", []) if with_args else []
        if len(args) != len(payloads):
            self.err("variant %s of enum %s expects %d payloads, got %d"
                     % (vname, ename, len(payloads), len(args)), e)
        typeparams = edef["typeparams"]
        type_map = {}
        # First pass: try to infer type args from arguments using the
        # contextual expected type (if available). Pass the declared
        # payload type as the contextual hint when possible — this lets
        # `[]` literals resolve to `list[T]` without a second check_expr
        # call (the source of the BUG-A spurious 'use of moved value').
        # If typeparams is non-empty, we don't yet know the instantiated
        # payload types, so the first pass uses `None`.
        if typeparams and expected is not None and type_base(expected) == ename:
            eargs = type_args(expected)
            if len(eargs) == len(typeparams):
                for tp, ea in zip(typeparams, eargs):
                    type_map[tp] = ea
        first_at = []
        # Compute instantiated payload types for the first pass.
        if typeparams and type_map:
            inst_payloads_first = [instantiate_type(pt, type_map) for pt in payloads]
        elif not typeparams:
            inst_payloads_first = list(payloads)
        else:
            inst_payloads_first = [None] * len(payloads)
        for a, pt, hint in zip(args, payloads, inst_payloads_first):
            at = self.check_expr(a, env, hint)
            if at == "never":
                self.err("never value cannot be used as an enum payload", e)
            first_at.append(at)
            if typeparams:
                unify(pt, at, typeparams, type_map)
            else:
                if at != pt:
                    self.err("payload type mismatch: expected %s, got %s" % (pt, at), e)
        # If any type params are still unbound, error.
        if typeparams:
            for tp in typeparams:
                if tp not in type_map:
                    self.err("cannot infer type argument for %s; provide a contextual type"
                             % tp, e)
            # Build the instantiated type.
            result_type = ename + "[" + ", ".join(type_map[tp] for tp in typeparams) + "]"
        else:
            result_type = ename
        # Compute the final instantiated payload types.
        if typeparams:
            inst_payloads = [instantiate_type(pt, type_map) for pt in payloads]
        else:
            inst_payloads = list(payloads)
        # SCAN-A fix: do NOT re-run check_expr on type disagreement.
        # The first pass already used the contextual hint, so a
        # disagreement is a real type error. If the first-pass type
        # contained a placeholder ('?'), the contextual hint wasn't
        # available — error out with a clearer message.
        for i, (a, pt) in enumerate(zip(args, inst_payloads)):
            at = first_at[i]
            if at == "never":
                continue
            if at != pt:
                if "?" in at:
                    self.err("payload type mismatch: expected %s, got %s "
                             "(could not infer the contextual type)"
                             % (pt, at), e)
                else:
                    self.err("payload type mismatch: expected %s, got %s"
                             % (pt, at), e)
        # Rewrite the node in-place so the interpreter can dispatch on `enumlit`.
        e["k"] = "enumlit"
        e["enum_name"] = ename
        e["variant"] = vname
        e["payload_types"] = inst_payloads
        e["t"] = result_type
        # Stage 12 release: annotate the variant's declaration index and
        # payload type so the LLVM backend can lower the literal via
        # hl_enum_new_variant(idx) + hl_struct_set_* for the payload.
        for i, (vname2, _) in enumerate(edef["variants"]):
            if vname2 == vname:
                e["variant_idx"] = i
                break
        if inst_payloads:
            e["payload_type"] = inst_payloads[0]
        else:
            e["payload_type"] = None

    def check_bin(self, e, env):
        lt = self.check_expr(e["l"], env, None)
        rt = self.check_expr(e["r"], env, None)
        # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-06 fix.
        # `never` is the bottom type (the expression diverges — e.g.
        # `panic("...")` returns `never`). It should be compatible with
        # EVERY type (the rest of the expression is unreachable). The
        # previous code rejected `panic("...") + 1` with "never value
        # cannot be used in expression" — a false positive that broke
        # dead-code patterns like `let x: int = if c { 5 } else {
        # panic("...") }` (which works via the let-binding path) but
        # not `let x: int = panic("...") + 1` (which goes through
        # check_bin). The fix: propagate `never` instead of erroring.
        # The `+1` is dead code (panic diverges), but the checker
        # should not reject the whole expression — the codegen emits
        # the RHS as a no-op (it's unreachable).
        if lt == "never" or rt == "never":
            return "never"
        op = e["op"]
        if op in ("||", "&&"):
            if lt != "bool" or rt != "bool":
                self.err("%s operator requires bool, got %s and %s" % (op, lt, rt), e)
            return "bool"
        if op in ("==", "!="):
            if lt != rt or lt not in ("int", "float", "bool", "str"):
                self.err("cannot compare == between %s and %s" % (lt, rt), e)
            return "bool"
        if op in ("<", "<=", ">", ">="):
            if lt != rt or lt not in ("int", "float", "str"):
                self.err("cannot compare ordering between %s and %s" % (lt, rt), e)
            return "bool"
        if op == "+":
            if lt == "int" and rt == "int":
                return "int"
            if lt == "float" and rt == "float":
                return "float"
            if lt == "str" and rt == "str":
                return "str"
            self.err("+ operator does not support %s and %s" % (lt, rt), e)
        if op in ("-", "*", "/", "%"):
            if lt == "int" and rt == "int":
                return "int"
            if lt == "float" and rt == "float":
                return "float"
            self.err("%s operator does not support %s and %s" % (op, lt, rt), e)
        self.err("unknown operator: %s" % op, e)

    def check_listlit(self, e, env, expected):
        elem = None
        first_vt = None  # BUG-SC-3 fix: cache first element's type
        if expected is not None and is_list(expected):
            elem = list_elem(expected)
        if elem is None:
            if not e["items"]:
                self.err("empty list literal requires a type in the surrounding context", e)
            # Check the first element ONCE to infer the element type, then
            # reuse its cached type below. Previously the loop re-checked
            # every item (including items[0]), which re-executed side
            # effects (drop/take move-marking) and produced spurious
            # "use of moved value" errors. Same class of bug as BUG-A/BUG-A2
            # in check_call/check_structlit.
            first_vt = self.check_expr(e["items"][0], env, None)
            elem = first_vt
            if elem in ("void", "never"):
                self.err("list element cannot have type %s" % elem, e)
        for i, it in enumerate(e["items"]):
            # Reuse the cached first-pass type for item 0 when available;
            # avoids re-running check_expr (which would re-execute side
            # effects like drop/take move-marking).
            if i == 0 and first_vt is not None:
                vt = first_vt
            else:
                vt = self.check_expr(it, env, elem)
            if vt == "never":
                continue
            if vt != elem:
                self.err("list element mismatch: expected %s, got %s"
                         % (elem, vt), it)
        return "list[%s]" % elem

    def check_structlit(self, e, env, expected):
        name = type_base(e["name"]) if "[" in e["name"] else e["name"]
        if name not in self.structs:
            self.err("struct does not exist: %s" % e["name"], e)
        st = self.structs[name]
        # BUG-DS4-2: constructing this struct may evaluate defaulted field
        # expressions (side effects live in the synthetic "@default.<S>"
        # call-graph node). Add the edge so the effects fixpoint propagates
        # the default's effects to the constructing function.
        #
        # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-04 fix.
        # The previous code added the @default.<S> edge UNCONDITIONALLY
        # whenever the struct had ANY defaulted field — even when the
        # literal provided ALL fields (no default is evaluated at this
        # call site). This was a false-positive in effect inference: a
        # pure function constructing a fully-specified struct whose
        # sibling default calls read_file would be rejected as "pure
        # but transitively uses Fs". The fix: only add the edge when at
        # least one defaulted field is ACTUALLY OMITTED from the literal.
        # We compute `provided_names` below (after the edge check) to
        # avoid duplicating the field-list walk; defer the edge add
        # until we know whether a default is being evaluated.
        typeparams = st["typeparams"]
        type_map = {}
        # If generic, infer type args from contextual expected type if available.
        if typeparams and expected is not None and type_base(expected) == name:
            eargs = type_args(expected)
            if len(eargs) == len(typeparams):
                for tp, ea in zip(typeparams, eargs):
                    type_map[tp] = ea
        # Determine the struct's effective field types (instantiate or not).
        fields_with_defaults = st["fields"]
        # Fields provided in the literal must come in declaration order, and
        # any fields with defaults may be omitted. We allow the user to omit
        # only trailing fields that have defaults.
        provided_names = [fname for fname, _ in e["fields"]]
        # Validate names against declared fields.
        decl_names = [fname for fname, _, _ in fields_with_defaults]
        for i, fname in enumerate(provided_names):
            if i >= len(decl_names) or decl_names[i] != fname:
                self.err("struct literal field order mismatch at position %d: expected '%s', got '%s'"
                         % (i, decl_names[i] if i < len(decl_names) else "<end>", fname), e)
        # Determine if all non-defaulted fields are present.
        # (F841 cleanup: the previous `defaulted` set was computed but never
        # used — the required-field check below subsumes it.)
        required = [fname for fname, _, d in fields_with_defaults if d is None]
        for r in required:
            if r not in provided_names:
                self.err("struct literal missing required field '%s'" % r, e)
        # If we've provided fewer fields than declared, the rest must have
        # defaults — checked above. Any extra fields beyond declared?
        if len(provided_names) > len(decl_names):
            self.err("struct literal %s has too many fields" % name, e)
        # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-04 fix
        # (deferred from the top of check_structlit). Now that we know
        # `provided_names` and `decl_names`, add the @default.<S> edge
        # ONLY if at least one defaulted field is actually OMITTED from
        # the literal (i.e. a default expression is being evaluated at
        # this call site). This prevents a pure function that constructs
        # a fully-specified struct from being falsely attributed the
        # sibling default's effects.
        if name in self._structs_with_defaults:
            if len(provided_names) < len(decl_names):
                # Only add the edge if at least one omitted field has a
                # default (the required-field check above guarantees
                # this, but we double-check defensively).
                omitted_have_default = False
                for i in range(len(provided_names), len(decl_names)):
                    if fields_with_defaults[i][2] is not None:
                        omitted_have_default = True
                        break
                if omitted_have_default:
                    self.edges[self.cur_fn].add("@default." + name)
        # Now type-check each provided field. We do a SINGLE pass that
        # records the resulting type for each field expression, then
        # optionally infers remaining type params from those types. This
        # avoids re-running check_expr (BUG-A: drop/take would re-execute
        # their move-marking side effects and spuriously fail with
        # "use of moved value" on the second pass).
        first_vt = []
        for i, (fname, fexpr) in enumerate(e["fields"]):
            decl_fname, decl_ftype, _ = fields_with_defaults[i]
            if typeparams and type_map:
                inst_ftype = instantiate_type(decl_ftype, type_map)
            else:
                inst_ftype = decl_ftype
            vt = self.check_expr(fexpr, env, inst_ftype)
            first_vt.append(vt)
            # If the expected type is a concrete (non-typeparam) type,
            # enforce the match. If it's an uninstantiated typeparam, defer
            # — the second pass below will infer.
            if vt == "never":
                continue
            if typeparams and not type_map:
                # Cannot enforce type when the type param is still unbound.
                continue
            if vt != inst_ftype:
                self.err("field type %s mismatch: expected %s, got %s"
                         % (fname, inst_ftype, vt), e)
        # If generic and we couldn't infer all type params from context, try
        # to infer from provided field types — reusing the first-pass types.
        if typeparams:
            for i, (fname, fexpr) in enumerate(e["fields"]):
                decl_ftype = fields_with_defaults[i][1]
                ft = first_vt[i] if i < len(first_vt) else None
                if ft is None:
                    # No first-pass result (shouldn't happen). Fall back to
                    # a single non-contextual check; this path is only
                    # reached when first_vt was not collected, e.g. if the
                    # field expression was added by the parser after the
                    # first pass.
                    ft = self.check_expr(fexpr, env, None)
                if ft == "never":
                    continue
                unify(decl_ftype, ft, typeparams, type_map)
            for tp in typeparams:
                if tp not in type_map:
                    self.err("cannot infer type argument for struct %s; provide a contextual type"
                             % name, e)
            result_type = name + "[" + ", ".join(type_map[tp] for tp in typeparams) + "]"
            # Now that we have the final type_map, re-verify the field types
            # against the INSTANTIATED types — but only on the cached
            # first-pass results, never by re-calling check_expr.
            for i, (fname, fexpr) in enumerate(e["fields"]):
                decl_ftype = fields_with_defaults[i][1]
                inst_ftype = instantiate_type(decl_ftype, type_map)
                ft = first_vt[i]
                if ft == "never":
                    continue
                if ft != inst_ftype:
                    self.err("field type %s mismatch: expected %s, got %s"
                             % (fname, inst_ftype, ft), e)
        else:
            result_type = name
        return result_type

    def check_call(self, e, env, expected):
        name = e["name"]
        args = e["args"]
        if name in BUILTIN_FNS:
            e["rc"] = ("builtin", name)
            return self.check_builtin_call(name, e, env, expected)
        if name in self.fns:
            fn = self.fns[name]
            if fn["struct"] is not None:
                self.err("method must be called through an object: %s" % name, e)
            e["rc"] = ("user", name)
            self.edges[self.cur_fn].add(name)
            if len(args) != len(fn["params"]):
                self.err("function %s expects %d arguments, got %d"
                         % (name, len(fn["params"]), len(args)), e)
            # Stage 15 release: ownership-across-boundary check.
            # Extern fns cross the FFI boundary into C, where the C
            # side may hold a pointer to the argument data after the
            # call returns. The interpreter rejects complex types
            # (list/map/struct/enum/tainted) at runtime — we mirror
            # that here at check time so the user gets a clean error
            # before the program runs.
            #
            # The `str` argument is the dangerous case: a C function
            # like `system()` accepts a null-terminated C string, so
            # a tainted[str] is a shell-injection vector. Reject any
            # tainted argument to an extern fn as a soundness rule.
            # BUG-A2 fix (carried over from BUG-A): cache the first-pass
            # type per argument so we don't re-run check_expr on the same
            # argument in the second pass. Calling check_expr twice would
            # re-execute side effects (drop/take move-marking) and produce
            # spurious "use of moved value" errors. (Deep-scan-10: the
            # declaration moved ABOVE the extern early-check loop, which
            # now also caches into it.)
            first_at = [None] * len(args)
            if fn.get("extern", False):
                for i, (a, (pn, pt, _)) in enumerate(zip(args, fn["params"])):
                    # The argument type is checked below in the
                    # generic / non-generic loop. We do an EARLY
                    # check here only for the tainted-wrap case so
                    # we can produce a targeted error message before
                    # the generic "expected X, got Y" error fires.
                    # Deep-scan-10 fix: CACHE the early type in
                    # first_at — the second-pass loop re-ran
                    # check_expr for NON-generic externs (first_at[i]
                    # was None), re-executing take/drop move-marking
                    # and producing spurious "use of moved value"
                    # errors on legal programs like
                    # `puts(take(s))` (the very BUG-A class the loop
                    # comments claim was fixed).
                    at = self.check_expr(a, env, None)
                    first_at[i] = at
                    if at == "never":
                        continue
                    if is_taint(at):
                        self.err(
                            "extern call to '%s': argument %d ('%s') is "
                            "tainted[%s] — passing tainted data across the "
                            "FFI boundary is forbidden (C functions like "
                            "system() can shell-inject; reject tainted "
                            "values or sanitise before calling externs)"
                            % (name, i + 1, pn, type_args(at)[0] if type_args(at) else "?"),
                            a)
                    # Reject any non-primitive type. Extern fns
                    # support only int / float / bool / str.
                    if pt not in ("int", "float", "bool", "str", "void"):
                        self.err(
                            "extern call to '%s': parameter '%s' has type "
                            "%s — extern fn params must be int / float / "
                            "bool / str (use a string-encoded form for "
                            "complex data)" % (name, pn, pt),
                            e)
            # Generic function: infer type args from argument types.
            typeparams = fn.get("typeparams", [])
            type_map = {}
            if typeparams:
                for i, (a, (pn, pt, _)) in enumerate(zip(args, fn["params"])):
                    at = self.check_expr(a, env, None)
                    first_at[i] = at
                    if at == "never":
                        continue
                    unify(pt, at, typeparams, type_map)
                for tp in typeparams:
                    if tp not in type_map:
                        self.err("cannot infer type argument for %s; provide explicit types"
                                 % tp, e)
            # Type-check arguments against instantiated parameter types.
            for i, (a, (pn, pt, _)) in enumerate(zip(args, fn["params"])):
                if typeparams:
                    inst_pt = instantiate_type(pt, type_map)
                else:
                    inst_pt = pt
                # Reuse the first-pass type whenever it already matches the
                # instantiated parameter type — avoids re-running check_expr
                # (which would re-execute side effects like drop/take).
                at = first_at[i]
                if at is None:
                    at = self.check_expr(a, env, inst_pt)
                if at == "never":
                    continue
                if at != inst_pt:
                    self.err("argument '%s' of %s expects %s, got %s"
                             % (pn, name, inst_pt, at), a)
            # Stage 17 (v0.28.0-alpha): call-site contract checking. If
            # the callee declares `requires` and every argument is a
            # literal, the precondition is CONSTANT-EVALUATED here: a
            # provably-false requires is a compile error at the call
            # site (catches div(10, 0)-style bugs before the program
            # ever runs). Unknown values defer to runtime.
            req = fn.get("requires")
            # Deep-scan-10 fix: `and args` excluded ZERO-argument
            # contracted fns — "every argument is a literal" is
            # vacuously true with no arguments, so
            # `fn f() requires false { ... } f()` was never
            # constant-evaluated (the provably-false precondition
            # compiled cleanly).
            if req is not None:
                consts = _proof.args_are_const(fn, args)
                if consts is not None:
                    verdict = _proof.const_eval(req, consts)
                    if verdict is False:
                        self.err(
                            "contract violation at call site: requires of "
                            "'%s' evaluates to FALSE for these literal "
                            "arguments (%s)" % (
                                name,
                                ", ".join("%s=%r" % (pn, consts[pn])
                                          for pn in consts)), e)
            if typeparams:
                return instantiate_type(fn["ret"], type_map)
            return fn["ret"]
        self.err("function does not exist: %s" % name, e)

    def check_builtin_call(self, name, e, env, expected):
        args = e["args"]

        def need(n):
            if len(args) != n:
                self.err("%s expects %d arguments, got %d" % (name, n, len(args)), e)

        def argt(i, want):
            at = self.check_expr(args[i], env, want)
            if at == "never":
                self.err("never value cannot be used as an argument", e)
            if want is not None and at != want:
                self.err("%s expects argument %s, got %s" % (name, want, at), e)
            return at

        # Stage 10-alpha: taint-sink enforcement.
        # For each SINK builtin, before we type-check the args, run a pass
        # that rejects `tainted[T]` values being passed to sink argument
        # positions. We do this AFTER the regular argt() call so the
        # underlying type error message takes precedence (e.g. passing
        # an int to print already errors as "expected str, got int"; we
        # only need the taint error for the case where the type is
        # otherwise fine but the value is tainted).
        #
        # Deep-scan-8 fix: the original implementation ONLY checked for
        # taint — it did not validate that the argument's type matched
        # `want`. So `print(42)` compiled cleanly and crashed at runtime
        # with `TypeError: a bytes-like object is required, not 'int'`.
        # The fix: also enforce the type when `want` is not None.
        def reject_tainted_at_sink(arg_idx, want):
            arg = args[arg_idx]
            # Check the expression to get its type. Pass `want` so we
            # don't break type inference (e.g. for `map_new()`).
            at = self.check_expr(arg, env, want)
            if at == "never":
                return at  # never propagates; let the caller handle
            # Deep-scan-8: enforce the expected type. Previously only
            # taint was checked, so `print(42)` / `read_file(42)` /
            # `exit("foo")` compiled and crashed at runtime.
            if want is not None and at != want:
                self.err("%s expects argument %d to be %s, got %s"
                         % (name, arg_idx + 1, want, at), e)
            if is_tainted_type(at):
                self.err(
                    "taint-sink violation: %s argument %d is tainted[%s] "
                    "(tainted values must be sanitised before reaching "
                    "a sink — use sanitize_html / sanitize_path / "
                    "sanitize_sql_identifier / sanitize_sql_string / "
                    "sanitize_command / sanitize_filename from "
                    "std.sanitize, or taint_unwrap() if you accept the "
                    "risk)" %
                    (name, arg_idx + 1, list_taint_inner(at)), e)
            return at

        if name in ("print", "println"):
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:print")
            return "void"
        if name == "panic":
            need(1)
            # panic is NOT a taint sink — panicking with a tainted message
            # is fine; panic is for programming bugs, not user output.
            argt(0, "str")
            # Deep-scan-7 fix: add panic to the call-graph edges so the
            # --audit output lists every function that calls panic()
            # (useful for security audits — programs that panic in
            # unexpected places warrant review).
            self.edges[self.cur_fn].add("b:panic")
            return "never"
        if name == "exit":
            need(1)
            reject_tainted_at_sink(0, "int")
            self.edges[self.cur_fn].add("b:exit")
            return "never"
        if name == "str":
            need(1)
            at = argt(0, None)
            if at not in ("int", "float", "bool", "str"):
                self.err("str() does not support type %s" % at, e)
            return "str"
        if name == "int":
            need(1)
            argt(0, "str")
            return "int"
        if name == "len":
            need(1)
            at = argt(0, None)
            if at not in ("str",) and not is_list(at) and not is_map(at):
                self.err("len() does not support type %s" % at, e)
            return "int"
        if name == "range":
            need(2)
            argt(0, "int")
            argt(1, "int")
            return "list[int]"
        # Stage 21 (v0.37.0-alpha): has_feature("avx2") -> bool — a
        # COMPILE-TIME constant folded from the --target-feature flag
        # (cfg(feature) dispatch). Pure; the argument must be a string
        # LITERAL (the native codegen folds it).
        if name == "has_feature":
            need(1)
            if e["args"][0].get("k") != "str":
                self.err("has_feature() expects a string literal "
                         "(it is a compile-time constant)", e)
            self.check_expr(args[0], env, "str")
            return "bool"
        # Stage 21: simd_cpu_supports("avx2") -> bool — runtime CPU
        # probe (CPUID on x86, NEON compile check on aarch64). Pure.
        if name == "simd_cpu_supports":
            need(1)
            self.check_expr(args[0], env, "str")
            return "bool"
        # Stage 19 (v0.35.0-alpha): join(list[str], sep) -> str — the
        # O(n) whole-list join (single allocation + single copy per
        # element). Pure (no effects).
        if name == "join":
            need(2)
            # Expected type on arg 1 so an empty list literal `[]`
            # infers list[str] from the call itself.
            at = argt(0, "list[str]")
            if not is_list(at) or list_elem(at) != "str":
                self.err("join() expects a list[str] as argument 1, got %s" % at, e)
            argt(1, "str")
            return "str"
        # Stage 32 (v0.51.0-alpha): native bitwise primitives.
        # All take int args and return int; they are pure (no effects).
        if name in ("int_and", "int_or", "int_xor",
                    "int_shl", "int_shr", "int_sar"):
            need(2)
            at0 = argt(0, "int")
            at1 = argt(1, "int")
            if at0 != "int":
                self.err("%s() expects int as argument 1, got %s" % (name, at0), e)
            if at1 != "int":
                self.err("%s() expects int as argument 2, got %s" % (name, at1), e)
            return "int"
        if name in ("int_not", "int_popcount", "int_clz", "int_ctz"):
            need(1)
            at0 = argt(0, "int")
            if at0 != "int":
                self.err("%s() expects int, got %s" % (name, at0), e)
            return "int"
        if name == "map_new":
            need(0)
            if expected is None or not is_map(expected):
                self.err("map_new() requires a 'map[str, T]' type in the surrounding context", e)
            return expected
        if name == "read_file":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:read_file")
            return "str"
        # Stage 10-beta: read_file_tainted(path) — like read_file but the
        # returned str is wrapped as tainted[str]. This is the second taint
        # source (after tainted_args) and covers the common case where
        # user-controlled content is read from a file.
        if name == "read_file_tainted":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:read_file_tainted")
            return "tainted[str]"
        # Stage 10 release: read_line() -> tainted[str] — the third taint
        # source. Reads a single line from stdin (up to and including the
        # newline, which is stripped). The result is always tainted because
        # stdin is untrusted input. Carries the IO effect.
        if name == "read_line":
            need(0)
            self.edges[self.cur_fn].add("b:read_line")
            return "tainted[str]"
        if name == "write_file":
            need(2)
            reject_tainted_at_sink(0, "str")
            reject_tainted_at_sink(1, "str")
            self.edges[self.cur_fn].add("b:write_file")
            return "void"
        if name == "args":
            need(0)
            self.edges[self.cur_fn].add("b:args")
            return "list[str]"
        # Stage 10-alpha: tainted_args — like `args` but each element is
        # wrapped as `tainted[str]` (defined in std/taint.hls).
        if name == "tainted_args":
            need(0)
            self.edges[self.cur_fn].add("b:tainted_args")
            return "list[tainted[str]]"
        # Stage 10-alpha: taint_mark / taint_unwrap — generic wrap/unwrap.
        # Both are pure (no effect); taint_unwrap is the "I know what I'm
        # doing" escape hatch — it is the explicit untaint operation.
        if name == "taint_mark":
            need(1)
            at = argt(0, None)
            if at == "void":
                self.err("taint_mark() does not support void", e)
            return "tainted[%s]" % at
        if name == "taint_unwrap":
            need(1)
            at = argt(0, None)
            if not is_tainted_type(at):
                self.err(
                    "taint_unwrap() expects a tainted[T] value, got %s" % at, e)
            return list_taint_inner(at)
        if name == "chr":
            need(1)
            argt(0, "int")
            return "str"
        if name == "clock_ms":
            need(0)
            self.edges[self.cur_fn].add("b:clock_ms")
            return "int"
        # ----- Stage 46 (v0.65.0-alpha): thread / scheduling builtins -----
        # thread_sleep_ms(ms: int) -> void — blocks the calling thread.
        # The ms argument must be a non-negative int (negative sleep is
        # a programming error — the runtime panics). The effect set
        # (Clock + Conc) is set in BUILTIN_EFFECTS above.
        if name == "thread_sleep_ms":
            need(1)
            at = argt(0, "int")
            if at != "int":
                self.err("thread_sleep_ms() expects an int, got %s" % at, e)
            arg = args[0]
            if arg["k"] == "int" and arg["v"] < 0:
                self.err("thread_sleep_ms() duration must be >= 0, got "
                         "literal %d" % arg["v"], e)
            self.edges[self.cur_fn].add("b:thread_sleep_ms")
            return "void"
        # thread_yield() -> void — hint the scheduler to switch.
        if name == "thread_yield":
            need(0)
            self.edges[self.cur_fn].add("b:thread_yield")
            return "void"
        # thread_current_id() -> int — non-zero thread identifier.
        # The main thread and each spawned task get distinct IDs; the
        # actual VALUE is implementation-defined (Python's
        # threading.get_ident() vs C's pthread_self() cast) and may
        # differ between interpreter and native. Callers must NOT
        # rely on a specific value — only on the property "different
        # threads get different IDs".
        if name == "thread_current_id":
            need(0)
            self.edges[self.cur_fn].add("b:thread_current_id")
            return "int"
        if name == "file_exists":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:file_exists")
            return "bool"
        # ----- Stage 36 (v0.55.0-alpha): filesystem metadata builtins -----
        if name == "fs_read_dir":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:fs_read_dir")
            return "list[str]"
        if name == "fs_size":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:fs_size")
            return "int"
        if name == "fs_is_dir":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:fs_is_dir")
            return "bool"
        if name == "fs_set_perms":
            need(2)
            reject_tainted_at_sink(0, "str")
            argt(1, "int")
            self.edges[self.cur_fn].add("b:fs_set_perms")
            return "void"
        # ----- Stage 8-alpha: ownership primitives (drop / clone / take) -----
        if name == "drop":
            need(1)
            at = argt(0, None)
            if not is_owned_type(at):
                self.err("drop() requires an owned (heap) type, got %s" % at, e)
            # Stage 8-beta: a move inside a while/for condition or iterable
            # would re-execute on every iteration — reject it up front.
            if self.loop_header > 0:
                self.err("drop() cannot be used inside a loop condition or "
                         "iterable (the binding would be moved on every "
                         "iteration)", e)
            # The argument must be a simple `ident` lvalue — we need a binding
            # to mark as moved. Complex expressions are not allowed.
            arg = args[0]
            if arg["k"] != "ident":
                self.err("drop() requires a variable name (not an expression)", e)
            # Mark the binding as moved.
            if not self.mark_moved(env, arg["name"]):
                self.err("drop() argument is not a binding: %s" % arg["name"], e)
            return "void"
        if name == "clone":
            need(1)
            at = argt(0, None)
            if not is_owned_type(at):
                self.err("clone() requires an owned (heap) type, got %s" % at, e)
            if not self.clone_supported(at):
                self.err("clone() on type %s is not supported (Task join "
                         "handles and composites containing them cannot "
                         "be cloned)" % at, e)
            # Argument is consumed by value (read), not moved.
            return at
        if name == "take":
            need(1)
            at = argt(0, None)
            if not is_owned_type(at):
                self.err("take() requires an owned (heap) type, got %s" % at, e)
            # Stage 8-beta: same loop-header restriction as drop().
            if self.loop_header > 0:
                self.err("take() cannot be used inside a loop condition or "
                         "iterable (the binding would be moved on every "
                         "iteration)", e)
            arg = args[0]
            if arg["k"] != "ident":
                self.err("take() requires a variable name (not an expression)", e)
            if not self.mark_moved(env, arg["name"]):
                self.err("take() argument is not a binding: %s" % arg["name"], e)
            return at
        # ----- Stage 9 release (v0.20.0-alpha): Net / Rand / Proc builtins -----
        # All five new builtins are SINK-free (no taint-sink enforcement) but
        # they DO carry their respective effects; the fixpoint in
        # check_effects() will reject a caller whose declared effects do not
        # cover them. The error message names the function, the missing
        # effect, the violating builtin, and the declared set.
        # net_lookup(host: str) -> str — DNS resolution of an A record.
        # Returns the first IPv4 address as a string. Panics on DNS failure.
        if name == "net_lookup":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:net_lookup")
            return "str"
        # ----- Stage 37 (v0.56.0-alpha): TCP / UDP / TLS builtins -----
        # See SINK_BUILTINS above for the taint-sink rationale (host /
        # path; NOT data, NOT port). All return / accept primitive int
        # fd values; the stdlib (std.net) wraps them in TcpStream /
        # TcpListener / UdpSocket structs.
        if name == "net_tcp_connect":
            need(2)
            reject_tainted_at_sink(0, "str")
            argt(1, "int")
            self.edges[self.cur_fn].add("b:net_tcp_connect")
            return "int"
        if name == "net_tcp_listen":
            need(3)
            reject_tainted_at_sink(0, "str")
            argt(1, "int")
            argt(2, "int")
            self.edges[self.cur_fn].add("b:net_tcp_listen")
            return "int"
        if name == "net_tcp_accept":
            need(1)
            argt(0, "int")
            self.edges[self.cur_fn].add("b:net_tcp_accept")
            return "int"
        if name == "net_read":
            need(2)
            argt(0, "int")
            argt(1, "int")
            self.edges[self.cur_fn].add("b:net_read")
            return "str"
        if name == "net_write":
            need(2)
            argt(0, "int")
            argt(1, "str")
            self.edges[self.cur_fn].add("b:net_write")
            return "int"
        if name == "net_close":
            need(1)
            argt(0, "int")
            self.edges[self.cur_fn].add("b:net_close")
            return "void"
        if name == "net_udp_open":
            need(0)
            self.edges[self.cur_fn].add("b:net_udp_open")
            return "int"
        if name == "net_udp_send_to":
            need(4)
            argt(0, "int")
            reject_tainted_at_sink(1, "str")
            argt(2, "int")
            argt(3, "str")
            self.edges[self.cur_fn].add("b:net_udp_send_to")
            return "int"
        if name == "net_udp_recv_from":
            need(2)
            argt(0, "int")
            argt(1, "int")
            self.edges[self.cur_fn].add("b:net_udp_recv_from")
            return "str"
        if name == "net_tls_get":
            need(3)
            reject_tainted_at_sink(0, "str")
            argt(1, "int")
            reject_tainted_at_sink(2, "str")
            self.edges[self.cur_fn].add("b:net_tls_get")
            return "str"
        # rand_int(max: int) -> int — uniform random int in [0, max).
        # Panics if max <= 0 (so the bound is always positive and the
        # modulo bias is bounded by the caller's choice of max).
        if name == "rand_int":
            need(1)
            argt(0, "int")
            self.edges[self.cur_fn].add("b:rand_int")
            return "int"
        # rand_float() -> float — uniform random float in [0.0, 1.0).
        if name == "rand_float":
            need(0)
            self.edges[self.cur_fn].add("b:rand_float")
            return "float"
        # rand_seed(s: int) -> void — seed the PRNG. Deterministic when
        # the same seed is used (useful for testing / reproducible runs).
        if name == "rand_seed":
            need(1)
            argt(0, "int")
            self.edges[self.cur_fn].add("b:rand_seed")
            return "void"
        # proc_exec(cmd: str) -> int — run a shell command via system().
        # Returns the exit code (0 on success, non-zero on failure). The
        # command runs in a subshell — callers MUST sanitise any tainted
        # input before constructing the command string.
        if name == "proc_exec":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:proc_exec")
            return "int"
        # ----- Stage 16 (v0.27.0-alpha): concurrency builtins -----
        # chan_new() -> Chan[T] — contextual typing (same pattern as
        # map_new()): the surrounding let/param/return type supplies T.
        if name == "chan_new":
            need(0)
            if expected is None or not is_chan(expected):
                self.err("chan_new() requires a 'Chan[T]' type in the "
                         "surrounding context", e)
            return expected
        # chan_new_bounded(cap: int) -> Chan[T] — Stage-16 perfection
        # (v0.29.0-alpha): a bounded channel whose send blocks while it
        # holds `cap` messages (backpressure). Contextual typing like
        # chan_new. A literal capacity < 1 is a COMPILE error (dynamic
        # capacities are validated at runtime — clean panic 101).
        if name == "chan_new_bounded":
            need(1)
            at = argt(0, "int")
            if at != "int":
                self.err("chan_new_bounded() capacity expects int, got %s"
                         % at, e)
            cap_arg = args[0]
            if cap_arg["k"] == "int" and cap_arg["v"] < 1:
                self.err("chan_new_bounded() capacity must be >= 1, got "
                         "literal %d" % cap_arg["v"], e)
            if expected is None or not is_chan(expected):
                self.err("chan_new_bounded() requires a 'Chan[T]' type in "
                         "the surrounding context", e)
            self.edges[self.cur_fn].add("b:chan_new_bounded")
            return expected
        # spawn(f, a1, ..., aN) -> Task[R] — start a task running function
        # f with the given arguments; returns a join handle. The FIRST
        # argument is a function NAME (identifier), not a value.
        if name == "spawn":
            if len(args) < 1:
                self.err("spawn() expects a function name followed by its "
                         "arguments", e)
            fn_arg = args[0]
            if fn_arg["k"] != "ident":
                self.err("spawn() argument 1 must be a function name "
                         "(an identifier), got a %s expression" % fn_arg["k"], e)
            fname = fn_arg["name"]
            if fname in BUILTIN_FNS:
                self.err("spawn() target cannot be a builtin function: %s" % fname, e)
            if fname not in self.fns:
                self.err("spawn() target function does not exist: %s" % fname, e)
            tfn = self.fns[fname]
            if tfn["struct"] is not None:
                self.err("spawn() target cannot be a method: %s (methods "
                         "need a self receiver — spawn a free function)" % fname, e)
            if tfn.get("typeparams"):
                self.err("spawn() of generic functions is not supported "
                         "yet: %s (wrap it in a non-generic fn)" % fname, e)
            if tfn.get("extern", False):
                self.err("spawn() of extern (FFI) functions is not "
                         "supported", e)
            params = tfn["params"]
            vargs = args[1:]
            if len(vargs) != len(params):
                self.err("spawn() target %s expects %d arguments, got %d"
                         % (fname, len(params), len(vargs)), e)
            for i, (a, (pn, pt, _)) in enumerate(zip(vargs, params)):
                at = self.check_expr(a, env, pt)
                if at == "never":
                    continue
                if at != pt:
                    self.err("spawn() argument '%s' of %s expects %s, got %s"
                             % (pn, fname, pt, at), a)
                # Send rule: values crossing a task boundary must be of a
                # Send type. Task[R] is the first non-Send type (a join
                # handle must stay with the task that spawned it).
                if not self.type_is_send(pt):
                    self.err("type %s is not Send: values of this type "
                             "cannot cross a task boundary (a Task join "
                             "handle must stay with its spawner)" % pt, a)
                # Data-race-freedom rule (Stage 16 acceptance): a bare
                # variable / field / index read of an OWNED type is a
                # cross-thread refcount race (the value may be aliased by
                # the sender thread) — reject it. Pass clone(x) (private
                # deep copy) or take(x) (transfer) instead.
                if is_owned_type(pt) and a["k"] in ("ident", "field", "index"):
                    what = ("variable '" + a["name"] + "'") if a["k"] == "ident" \
                        else "a borrowed value"
                    self.err(
                        "cannot share %s across tasks: spawn() argument %d "
                        "reads it directly — pass clone(x) (private copy) or "
                        "take(x) (ownership transfer). Data-race freedom: no "
                        "owned value may be simultaneously released by two "
                        "threads." % (what, i + 2), a)
            self.edges[self.cur_fn].add("b:spawn")
            # Deep-scan-10 soundness fix: the spawned function IS a
            # callee — its effects must reach the spawner's computed
            # set (SPEC §17.3: "computed set = union over all
            # callees"; the spawner CAUSES the task's effects).
            # Previously only `b:spawn` was added, so a `uses Conc`
            # main could transitively perform IO/Fs/Proc through a
            # spawned task while --audit reported a clean Conc-only
            # tree (struct defaults already get synthetic edges for
            # exactly this reason — BUG-DS4-2).
            self.edges[self.cur_fn].add(fname)
            # Rewrite the node: drop the fn-name argument and record the
            # target so the interpreter / codegen never evaluates the
            # function name as a value.
            e["args"] = vargs
            e["spawn_fn"] = fname
            return "Task[%s]" % tfn["ret"]
        # select(chs: list[Chan[T]]) -> int — blocks until at least one
        # channel in the list has a pending message; returns the index of
        # the first ready channel (list order).
        if name == "select":
            need(1)
            at = argt(0, None)
            if not is_list(at) or not is_chan(list_elem(at)):
                self.err("select() expects a list[Chan[T]], got %s" % at, e)
            if not self.type_is_send(list_elem(at)):
                self.err("select() channel element type %s is not Send"
                         % list_elem(at), e)
            self.edges[self.cur_fn].add("b:select")
            return "int"
        # ----- Stage 33 (v0.52.0-alpha): async/await builtins -----
        # async_spawn(f, a1, ..., aN) -> Future[R] — like spawn, but
        # returns a Future (a cap-1 bounded channel carrying the result).
        # The first argument is a function NAME (identifier), not a value.
        # The target function must NOT return void — a future carries a
        # value. (For a void-returning task, use plain spawn().)
        if name == "async_spawn":
            if len(args) < 1:
                self.err("async_spawn() expects a function name followed "
                         "by its arguments", e)
            fn_arg = args[0]
            if fn_arg["k"] != "ident":
                self.err("async_spawn() argument 1 must be a function name "
                         "(an identifier), got a %s expression" % fn_arg["k"], e)
            fname = fn_arg["name"]
            if fname in BUILTIN_FNS:
                self.err("async_spawn() target cannot be a builtin function: "
                         "%s" % fname, e)
            if fname not in self.fns:
                self.err("async_spawn() target function does not exist: %s"
                         % fname, e)
            tfn = self.fns[fname]
            if tfn["struct"] is not None:
                self.err("async_spawn() target cannot be a method: %s (methods "
                         "need a self receiver — async_spawn a free function)"
                         % fname, e)
            if tfn.get("typeparams"):
                self.err("async_spawn() of generic functions is not supported "
                         "yet: %s (wrap it in a non-generic fn)" % fname, e)
            if tfn.get("extern", False):
                self.err("async_spawn() of extern (FFI) functions is not "
                         "supported", e)
            if tfn["ret"] == "void":
                self.err("async_spawn() target %s returns void — a Future "
                         "must carry a value (use plain spawn() for void "
                         "tasks)" % fname, e)
            params = tfn["params"]
            vargs = args[1:]
            if len(vargs) != len(params):
                self.err("async_spawn() target %s expects %d arguments, got %d"
                         % (fname, len(params), len(vargs)), e)
            for i, (a, (pn, pt, _)) in enumerate(zip(vargs, params)):
                at = self.check_expr(a, env, pt)
                if at == "never":
                    continue
                if at != pt:
                    self.err("async_spawn() argument '%s' of %s expects %s, "
                             "got %s" % (pn, fname, pt, at), a)
                if not self.type_is_send(pt):
                    self.err("type %s is not Send: values of this type cannot "
                             "cross a task boundary (async_spawn)" % pt, a)
                if is_owned_type(pt) and a["k"] in ("ident", "field", "index"):
                    what = ("variable '" + a["name"] + "'") if a["k"] == "ident" \
                        else "a borrowed value"
                    self.err(
                        "cannot share %s across tasks: async_spawn() argument "
                        "%d reads it directly — pass clone(x) or take(x). "
                        "Data-race freedom: no owned value may be simultaneously "
                        "released by two threads." % (what, i + 2), a)
            self.edges[self.cur_fn].add("b:async_spawn")
            # The spawned function's effects reach the spawner (same
            # soundness rule as spawn).
            self.edges[self.cur_fn].add(fname)
            # Rewrite the node: drop the fn-name argument, record the
            # target so the interpreter / codegen never evaluates the
            # function name as a value. mkey "async_spawn" dispatches to
            # the async_spawn codegen path.
            e["args"] = vargs
            e["spawn_fn"] = fname
            return "Future[%s]" % tfn["ret"]
        # await(fut: Future[T]) -> T — block until the future is ready,
        # return its value. The argument must be a Future[T].
        if name == "await":
            need(1)
            at = argt(0, None)
            if not is_future(at):
                self.err("await() expects a Future[T], got %s" % at, e)
            self.edges[self.cur_fn].add("b:await")
            return future_inner(at)
        # future_ready(v: T) -> Future[T] — make an immediately-ready
        # future. Contextual typing: T is inferred from the surrounding
        # Future[T] type (like chan_new).
        if name == "future_ready":
            need(1)
            if expected is None or not is_future(expected):
                self.err("future_ready() requires a 'Future[T]' type in the "
                         "surrounding context", e)
            want_t = future_inner(expected)
            at = argt(0, want_t)
            if at != want_t:
                self.err("future_ready() argument expects %s, got %s"
                         % (want_t, at), e)
            if not self.type_is_send(want_t):
                self.err("type %s is not Send: a future's value crosses a "
                         "task boundary" % want_t, e)
            self.edges[self.cur_fn].add("b:future_ready")
            return expected
        # future_poll(fut: Future[T]) -> Option[T] — non-blocking poll.
        # Returns Some(v) if the future is ready, None otherwise.
        if name == "future_poll":
            need(1)
            at = argt(0, None)
            if not is_future(at):
                self.err("future_poll() expects a Future[T], got %s" % at, e)
            self.edges[self.cur_fn].add("b:future_poll")
            return "Option[%s]" % future_inner(at)
        # future_select(futs: list[Future[T]]) -> int — race multiple
        # futures; return the index of the first ready one.
        if name == "future_select":
            need(1)
            at = argt(0, None)
            if not is_list(at) or not is_future(list_elem(at)):
                self.err("future_select() expects a list[Future[T]], got %s"
                         % at, e)
            if not self.type_is_send(future_inner(list_elem(at))):
                self.err("future_select() future element type %s is not Send"
                         % future_inner(list_elem(at)), e)
            self.edges[self.cur_fn].add("b:future_select")
            return "int"
        # ----- Stage 34 (v0.53.0-alpha): async stream builtins -----
        # stream_new(cap: int) -> Stream[T] — bounded stream (backpressure).
        # Contextual typing: T from the surrounding Stream[T] type.
        if name == "stream_new":
            need(1)
            at = argt(0, "int")
            if at != "int":
                self.err("stream_new() capacity expects int, got %s" % at, e)
            cap_arg = args[0]
            if cap_arg["k"] == "int" and cap_arg["v"] < 1:
                self.err("stream_new() capacity must be >= 1, got literal %d"
                         % cap_arg["v"], e)
            if expected is None or not is_stream(expected):
                self.err("stream_new() requires a 'Stream[T]' type in the "
                         "surrounding context", e)
            if not self.type_is_send(stream_inner(expected)):
                self.err("stream_new() element type %s is not Send"
                         % stream_inner(expected), e)
            self.edges[self.cur_fn].add("b:stream_new")
            return expected
        # stream_send(s: Stream[T], v: T) — push a value (backpressure).
        if name == "stream_send":
            need(2)
            st = argt(0, None)
            if not is_stream(st):
                self.err("stream_send() argument 1 expects Stream[T], got %s"
                         % st, e)
            et = stream_inner(st)
            at = argt(1, et)
            if at != et:
                self.err("stream_send() argument 2 expects %s, got %s"
                         % (et, at), e)
            if not self.type_is_send(et):
                self.err("type %s is not Send: stream values cross a task "
                         "boundary" % et, args[1])
            if is_owned_type(et) and args[1]["k"] in ("ident", "field", "index"):
                what = ("variable '" + args[1]["name"] + "'") \
                    if args[1]["k"] == "ident" else "a borrowed value"
                self.err("cannot share %s across tasks: stream_send() reads "
                         "it directly — pass clone(x) or take(x)" % what, args[1])
            self.edges[self.cur_fn].add("b:stream_send")
            return "void"
        # stream_recv(s: Stream[T]) -> T — block until a value is available.
        if name == "stream_recv":
            need(1)
            st = argt(0, None)
            if not is_stream(st):
                self.err("stream_recv() expects Stream[T], got %s" % st, e)
            self.edges[self.cur_fn].add("b:stream_recv")
            return stream_inner(st)
        # stream_try_recv(s: Stream[T], default: T) -> T — non-blocking.
        if name == "stream_try_recv":
            need(2)
            st = argt(0, None)
            if not is_stream(st):
                self.err("stream_try_recv() argument 1 expects Stream[T], "
                         "got %s" % st, e)
            et = stream_inner(st)
            at = argt(1, et)
            if at != et:
                self.err("stream_try_recv() argument 2 expects %s, got %s"
                         % (et, at), e)
            self.edges[self.cur_fn].add("b:stream_try_recv")
            return et
        # stream_len(s: Stream[T]) -> int — pending message count.
        if name == "stream_len":
            need(1)
            st = argt(0, None)
            if not is_stream(st):
                self.err("stream_len() expects Stream[T], got %s" % st, e)
            self.edges[self.cur_fn].add("b:stream_len")
            return "int"
        # stream_close(s: Stream[T]) — signal end-of-stream by sending a
        # sentinel. The sentinel convention is per-element-type (documented
        # in std/stream.hls). For int streams, the sentinel is INT64_MIN.
        # The builtin itself just records the edge; the actual sentinel
        # send happens in the producer (the user calls stream_send with
        # the sentinel value). This builtin is a no-op at the runtime
        # level — it exists so the type checker can validate that the
        # user remembered to close the stream (a stream without a close
        # call leaks the producer task). Lint stage 35+ will enforce this.
        if name == "stream_close":
            need(1)
            st = argt(0, None)
            if not is_stream(st):
                self.err("stream_close() expects Stream[T], got %s" % st, e)
            self.edges[self.cur_fn].add("b:stream_close")
            return "void"
        # stream_map_int(in_s: Stream[int], fn_name) -> Stream[int] —
        # spawn a worker that applies fn_name to each element.
        # fn_name is a function identifier (like spawn's first arg).
        # The target fn must have signature `fn(int) -> int`.
        if name == "stream_map_int" or name == "stream_filter_int":
            need(2)
            st = argt(0, None)
            if not is_stream(st) or stream_inner(st) != "int":
                self.err("%s() argument 1 expects Stream[int], got %s"
                         % (name, st), e)
            fn_arg = args[1]
            if fn_arg["k"] != "ident":
                self.err("%s() argument 2 must be a function name "
                         "(an identifier), got a %s expression"
                         % (name, fn_arg["k"]), e)
            fname = fn_arg["name"]
            if fname in BUILTIN_FNS:
                self.err("%s() target cannot be a builtin function: %s"
                         % (name, fname), e)
            if fname not in self.fns:
                self.err("%s() target function does not exist: %s"
                         % (name, fname), e)
            tfn = self.fns[fname]
            if tfn["struct"] is not None:
                self.err("%s() target cannot be a method: %s"
                         % (name, fname), e)
            if tfn.get("typeparams") or tfn.get("extern", False):
                self.err("%s() target must be a non-generic, non-extern "
                         "function: %s" % (name, fname), e)
            params = tfn["params"]
            if len(params) != 1 or params[0][1] != "int" or tfn["ret"] != "int":
                self.err("%s() target %s must have signature "
                         "fn(int) -> int" % (name, fname), e)
            if expected is None or not is_stream(expected) \
                    or stream_inner(expected) != "int":
                self.err("%s() requires a 'Stream[int]' type in the "
                         "surrounding context" % name, e)
            self.edges[self.cur_fn].add("b:" + name)
            self.edges[self.cur_fn].add(fname)
            # Rewrite: drop the fn-name argument, record the target.
            e["args"] = [args[0]]
            e["spawn_fn"] = fname
            return expected
        # stream_take_int(in_s: Stream[int], n: int) -> Stream[int] —
        # take the first n values, then signal end-of-stream.
        if name == "stream_take_int":
            need(2)
            st = argt(0, None)
            if not is_stream(st) or stream_inner(st) != "int":
                self.err("stream_take_int() argument 1 expects Stream[int], "
                         "got %s" % st, e)
            nt = argt(1, "int")
            if nt != "int":
                self.err("stream_take_int() argument 2 expects int, got %s"
                         % nt, e)
            if expected is None or not is_stream(expected) \
                    or stream_inner(expected) != "int":
                self.err("stream_take_int() requires a 'Stream[int]' type in "
                         "the surrounding context", e)
            self.edges[self.cur_fn].add("b:stream_take_int")
            return expected
        # stream_fold_int(in_s: Stream[int], init: int, fn_name) -> int —
        # blocking fold. fn_name must have signature `fn(int, int) -> int`.
        # Returns the final accumulator value.
        if name == "stream_fold_int":
            need(3)
            st = argt(0, None)
            if not is_stream(st) or stream_inner(st) != "int":
                self.err("stream_fold_int() argument 1 expects Stream[int], "
                         "got %s" % st, e)
            it = argt(1, "int")
            if it != "int":
                self.err("stream_fold_int() argument 2 expects int, got %s"
                         % it, e)
            fn_arg = args[2]
            if fn_arg["k"] != "ident":
                self.err("stream_fold_int() argument 3 must be a function name"
                         ", got a %s expression" % fn_arg["k"], e)
            fname = fn_arg["name"]
            if fname in BUILTIN_FNS:
                self.err("stream_fold_int() target cannot be a builtin: %s"
                         % fname, e)
            if fname not in self.fns:
                self.err("stream_fold_int() target function does not exist: %s"
                         % fname, e)
            tfn = self.fns[fname]
            if tfn["struct"] is not None or tfn.get("typeparams") \
                    or tfn.get("extern", False):
                self.err("stream_fold_int() target must be a non-generic, "
                         "non-extern free function: %s" % fname, e)
            params = tfn["params"]
            if len(params) != 2 or params[0][1] != "int" \
                    or params[1][1] != "int" or tfn["ret"] != "int":
                self.err("stream_fold_int() target %s must have signature "
                         "fn(int, int) -> int" % fname, e)
            self.edges[self.cur_fn].add("b:stream_fold_int")
            self.edges[self.cur_fn].add(fname)
            # Rewrite: drop the fn-name argument, record the target.
            e["args"] = [args[0], args[1]]
            e["spawn_fn"] = fname
            return "int"
        # stream_merge_int(a: Stream[int], b: Stream[int]) -> Stream[int] —
        # interleave two int streams into one.
        if name == "stream_merge_int":
            need(2)
            at = argt(0, None)
            bt = argt(1, None)
            if not is_stream(at) or stream_inner(at) != "int":
                self.err("stream_merge_int() argument 1 expects Stream[int], "
                         "got %s" % at, e)
            if not is_stream(bt) or stream_inner(bt) != "int":
                self.err("stream_merge_int() argument 2 expects Stream[int], "
                         "got %s" % bt, e)
            if expected is None or not is_stream(expected) \
                    or stream_inner(expected) != "int":
                self.err("stream_merge_int() requires a 'Stream[int]' type in "
                         "the surrounding context", e)
            self.edges[self.cur_fn].add("b:stream_merge_int")
            return expected
        # stream_flat_map_int(in_s: Stream[int], fn_name) -> Stream[int] —
        # fn_name takes int, returns Stream[int] (the inner stream).
        # The worker reads each value from in_s, calls fn_name to get an
        # inner stream, then forwards each value from the inner stream to
        # the output.
        if name == "stream_flat_map_int":
            need(2)
            st = argt(0, None)
            if not is_stream(st) or stream_inner(st) != "int":
                self.err("stream_flat_map_int() argument 1 expects "
                         "Stream[int], got %s" % st, e)
            fn_arg = args[1]
            if fn_arg["k"] != "ident":
                self.err("stream_flat_map_int() argument 2 must be a function "
                         "name, got a %s expression" % fn_arg["k"], e)
            fname = fn_arg["name"]
            if fname in BUILTIN_FNS:
                self.err("stream_flat_map_int() target cannot be a builtin: %s"
                         % fname, e)
            if fname not in self.fns:
                self.err("stream_flat_map_int() target function does not "
                         "exist: %s" % fname, e)
            tfn = self.fns[fname]
            if tfn["struct"] is not None or tfn.get("typeparams") \
                    or tfn.get("extern", False):
                self.err("stream_flat_map_int() target must be a non-generic, "
                         "non-extern free function: %s" % fname, e)
            params = tfn["params"]
            if len(params) != 1 or params[0][1] != "int" \
                    or tfn["ret"] != "Stream[int]":
                self.err("stream_flat_map_int() target %s must have signature "
                         "fn(int) -> Stream[int]" % fname, e)
            if expected is None or not is_stream(expected) \
                    or stream_inner(expected) != "int":
                self.err("stream_flat_map_int() requires a 'Stream[int]' type "
                         "in the surrounding context", e)
            self.edges[self.cur_fn].add("b:stream_flat_map_int")
            self.edges[self.cur_fn].add(fname)
            # Rewrite: drop the fn-name argument, record the target.
            e["args"] = [args[0]]
            e["spawn_fn"] = fname
            return expected
        # gen_spawn(f, args...) -> Stream[T] — like async_spawn, but for
        # streams. The target function must take a Stream[T] as its FIRST
        # parameter (the generator writes values into this stream), then
        # any user args. The builtin creates the stream, spawns f with
        # (stream, args...), and returns the stream.
        # The expected type's element T must match the stream parameter
        # of f (the FIRST parameter).
        if name == "gen_spawn":
            if len(args) < 1:
                self.err("gen_spawn() expects a function name followed by "
                         "its arguments", e)
            fn_arg = args[0]
            if fn_arg["k"] != "ident":
                self.err("gen_spawn() argument 1 must be a function name "
                         "(an identifier), got a %s expression" % fn_arg["k"], e)
            fname = fn_arg["name"]
            if fname in BUILTIN_FNS:
                self.err("gen_spawn() target cannot be a builtin function: %s"
                         % fname, e)
            if fname not in self.fns:
                self.err("gen_spawn() target function does not exist: %s"
                         % fname, e)
            tfn = self.fns[fname]
            if tfn["struct"] is not None or tfn.get("typeparams") \
                    or tfn.get("extern", False):
                self.err("gen_spawn() target must be a non-generic, "
                         "non-extern free function: %s" % fname, e)
            if expected is None or not is_stream(expected):
                self.err("gen_spawn() requires a 'Stream[T]' type in the "
                         "surrounding context", e)
            stream_t = expected
            params = tfn["params"]
            if len(params) < 1 or params[0][1] != stream_t:
                self.err("gen_spawn() target %s must take %s as its first "
                         "parameter (the generator writes into this stream)"
                         % (fname, stream_t), e)
            vargs = args[1:]
            if len(vargs) != len(params) - 1:
                self.err("gen_spawn() target %s expects %d arguments after "
                         "the stream, got %d"
                         % (fname, len(params) - 1, len(vargs)), e)
            # Check the non-stream arguments.
            for i, (a, (pn, pt, _)) in enumerate(zip(vargs, params[1:])):
                at = self.check_expr(a, env, pt)
                if at == "never":
                    continue
                if at != pt:
                    self.err("gen_spawn() argument '%s' of %s expects %s, "
                             "got %s" % (pn, fname, pt, at), a)
                if not self.type_is_send(pt):
                    self.err("type %s is not Send: gen_spawn() arguments "
                             "cross a task boundary" % pt, a)
                if is_owned_type(pt) and a["k"] in ("ident", "field", "index"):
                    what = ("variable '" + a["name"] + "'") if a["k"] == "ident" \
                        else "a borrowed value"
                    self.err("cannot share %s across tasks: gen_spawn() "
                             "argument %d reads it directly — pass clone(x) "
                             "or take(x)" % (what, i + 2), a)
            self.edges[self.cur_fn].add("b:gen_spawn")
            self.edges[self.cur_fn].add(fname)
            # Rewrite: drop the fn-name argument; codegen prepends the
            # created stream to the remaining args.
            e["args"] = vargs
            e["spawn_fn"] = fname
            return expected
        self.err("unknown builtin function: %s" % name, e)

    @staticmethod
    def is_clone_supported(t):
        """Types supported by clone(). Stage 8-beta (v0.19.0-alpha) expands
        clone() to EVERY owned type — str, list, map, struct, enum,
        tainted[...] — via the interpreter's deep_clone and per-
        instantiation codegen helpers in the native compiler."""
        if t == "str":
            return True
        if is_list(t):
            return Checker.is_clone_supported(list_elem(t))
        if is_map(t):
            return Checker.is_clone_supported(map_val(t))
        if is_taint(t):
            return Checker.is_clone_supported(taint_inner(t))
        # struct / enum / any other owned type
        return True

    def clone_supported(self, t, _seen=()):
        """Stage 16: struct-aware clone support. A Chan clones by SHARING
        (atomic refcount +1 — that is the point of a channel). A Task[R]
        join handle cannot be cloned (it is single-consumer: join() exactly
        once), and any composite containing a Task is not cloneable either.
        Mirrors hlc.hls's clone_supported."""
        if t in _seen:
            return True
        if t in ("int", "float", "bool", "str", "void", "never"):
            return True
        if is_chan(t):
            return True
        if is_task(t):
            return False
        # Stage 33: Future[T] clones by SHARING (atomic refcount +1 —
        # same as a channel; a future IS a channel under the hood).
        # Stage 34: Stream[T] clones by SHARING (same).
        if is_future(t) or is_stream(t):
            return True
        _seen = _seen + (t,)
        if is_list(t):
            return self.clone_supported(list_elem(t), _seen)
        if is_map(t):
            return self.clone_supported(map_val(t), _seen)
        if is_taint(t):
            return self.clone_supported(taint_inner(t), _seen)
        base = type_base(t)
        args = type_args(t)
        if base in self.structs:
            st = self.structs[base]
            if len(st["typeparams"]) != len(args):
                return False
            tmap = dict(zip(st["typeparams"], args))
            for _, ftype, _ in st["fields"]:
                ft = instantiate_type(ftype, tmap) if tmap else ftype
                if not self.clone_supported(ft, _seen):
                    return False
            return True
        if base in self.enums:
            en = self.enums[base]
            if len(en["typeparams"]) != len(args):
                return False
            tmap = dict(zip(en["typeparams"], args))
            for _, payloads in en["variants"]:
                for pt in payloads:
                    pti = instantiate_type(pt, tmap) if tmap else pt
                    if not self.clone_supported(pti, _seen):
                        return False
            return True
        return True

    def type_is_send(self, t, _seen=()):
        """Stage 16: the Send rule set (the `Send`/`Sync` equivalent,
        layered on the Stage 8 ownership system).

        A type is Send iff its values may cross a task boundary:
          - primitives (int/float/bool) and str: Send
          - Chan[T]: Send iff T is Send (channels are the sharing
            primitive — internally synchronized, atomic refcount)
          - Task[R]: NOT Send (a join handle must stay with its spawner)
          - list/map/tainted: Send iff the element/value type is Send
          - struct/enum: Send iff every field/payload type is Send
        """
        if t in _seen:
            return True
        if t in ("int", "float", "bool", "str", "void", "never"):
            return True
        if t in self.cur_typeparams:
            # Deep-scan-10 soundness fix: an UNRESOLVED type parameter is
            # NOT provably Send — a generic fn can be instantiated with
            # Task[R] (explicitly non-Send) or any non-Send struct, and
            # the generic body is never re-checked at the instantiation.
            # The old code returned True here ("conservative" — actually
            # UNSOUND), so `fn leak[T](ch: Chan[T], v: T) { ch.send(take(v)) }`
            # compiled cleanly and sent a Task join handle across a
            # channel. Conservative-DENY is the only sound default.
            return False
        if is_chan(t):
            return self.type_is_send(chan_inner(t), _seen)
        if is_task(t):
            return False
        # Stage 33: Future[T] is Send iff T is Send (a future is a
        # cap-1 channel under the hood — channels are Send).
        if is_future(t):
            return self.type_is_send(future_inner(t), _seen)
        # Stage 34: Stream[T] is Send iff T is Send (same as Chan).
        if is_stream(t):
            return self.type_is_send(stream_inner(t), _seen)
        _seen = _seen + (t,)
        if is_list(t):
            return self.type_is_send(list_elem(t), _seen)
        if is_map(t):
            return self.type_is_send(map_val(t), _seen)
        if is_taint(t):
            return self.type_is_send(taint_inner(t), _seen)
        base = type_base(t)
        args = type_args(t)
        if base in self.structs:
            st = self.structs[base]
            if len(st["typeparams"]) != len(args):
                return False
            tmap = dict(zip(st["typeparams"], args))
            for _, ftype, _ in st["fields"]:
                ft = instantiate_type(ftype, tmap) if tmap else ftype
                if not self.type_is_send(ft, _seen):
                    return False
            return True
        if base in self.enums:
            en = self.enums[base]
            if len(en["typeparams"]) != len(args):
                return False
            tmap = dict(zip(en["typeparams"], args))
            for _, payloads in en["variants"]:
                for pt in payloads:
                    pti = instantiate_type(pt, tmap) if tmap else pt
                    if not self.type_is_send(pti, _seen):
                        return False
            return True
        return True

    def check_method(self, e, env):
        tt = self.check_expr(e["target"], env, None)
        if tt == "never":
            self.err("never value cannot be used in expression", e)
        name = e["name"]
        args = e["args"]
        info = self.resolve_struct(tt)
        if info is not None:
            # User-defined struct method. The struct is the base name (e.g.,
            # "Box" for "Box[int]"). Methods are registered under the base
            # name in self.methods.
            tt_base = type_base(tt)
            m = self.methods.get(tt_base, {})
            if name not in m:
                self.err("struct %s has no method %s" % (tt_base, name), e)
            key = m[name]
            e["rm"] = ("user", key)
            # BUG-DS4-1 fix (SOUNDNESS): method calls were NOT added to the
            # call graph, so the effects fixpoint never traversed them. A
            # function calling an IO-using method without declaring IO —
            # or a `pure` function calling an effectful method — compiled
            # cleanly, completely bypassing the capability system. Add the
            # edge like check_call does for plain calls.
            self.edges[self.cur_fn].add(key)
            fn = self.fns[key]
            params = fn["params"][1:]
            if len(args) != len(params):
                self.err("%s.%s expects %d arguments, got %d"
                         % (tt_base, name, len(params), len(args)), e)
            # Generic method: infer type args from arguments.
            typeparams = fn.get("typeparams", [])
            # If the struct is itself generic, propagate its type args.
            st, struct_type_map = info
            type_map = dict(struct_type_map)
            # BUG-A2 fix (same fix as check_call): cache the first-pass type
            # per argument so we don't re-run check_expr on the same
            # argument in the second pass. Calling check_expr twice would
            # re-execute side effects (drop/take move-marking) and produce
            # spurious "use of moved value" errors.
            first_at = [None] * len(args)
            if typeparams:
                for i, (a, (pn, pt, _)) in enumerate(zip(args, params)):
                    at = self.check_expr(a, env, None)
                    first_at[i] = at
                    if at == "never":
                        continue
                    unify(pt, at, typeparams, type_map)
                for tp in typeparams:
                    if tp not in type_map:
                        self.err("cannot infer type argument for %s.%s" % (tt_base, name), e)
            for i, (a, (pn, pt, _)) in enumerate(zip(args, params)):
                if type_map:
                    inst_pt = instantiate_type(pt, type_map)
                else:
                    inst_pt = pt
                at = first_at[i]
                if at is None:
                    at = self.check_expr(a, env, inst_pt)
                if at == "never":
                    continue
                if at != inst_pt:
                    self.err("argument '%s' of %s.%s expects %s, got %s"
                             % (pn, tt_base, name, inst_pt, at), a)
            if type_map:
                return instantiate_type(fn["ret"], type_map)
            return fn["ret"]
        if tt == "str":
            if name not in STR_M:
                self.err("str has no method %s" % name, e)
            ptypes, ret = STR_M[name]
            e["rm"] = ("builtin", "str." + name)
        elif tt == "int":
            if name not in INT_M:
                self.err("int has no method %s" % name, e)
            # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-14
            # fix. The previous `isinstance(INT_M[name], str)` check
            # was dead code — INT_M is `{"to_str": "str", "to_float":
            # "float", "abs": "int"}` and ALL its values are str (never
            # a tuple). Simplified to direct assignment, matching
            # FLOAT_M / BOOL_M / STR_M which all use the (ptuples, ret)
            # form.
            ptypes, ret = [], INT_M[name]
            e["rm"] = ("builtin", "int." + name)
        elif tt == "float":
            if name not in FLOAT_M:
                self.err("float has no method %s" % name, e)
            ptypes, ret = ([], FLOAT_M[name])
            e["rm"] = ("builtin", "float." + name)
        elif tt == "bool":
            if name not in BOOL_M:
                self.err("bool has no method %s" % name, e)
            ptypes, ret = ([], BOOL_M[name])
            e["rm"] = ("builtin", "bool." + name)
        elif is_list(tt):
            elem = list_elem(tt)
            tbl = {
                "len": ([], "int"),
                "push": ([elem], "void"),
                "get": (["int"], elem),
                "set": (["int", elem], "void"),
                "pop": ([], elem),
            }
            if name not in tbl:
                self.err("list has no method %s" % name, e)
            ptypes, ret = tbl[name]
            e["rm"] = ("builtin", "list." + name)
        elif is_map(tt):
            vt = map_val(tt)
            tbl = {
                "len": ([], "int"),
                "set": (["str", vt], "void"),
                "get_or": (["str", vt], vt),
                "has": (["str"], "bool"),
                "keys": ([], "list[str]"),
            }
            if name not in tbl:
                self.err("map has no method %s" % name, e)
            ptypes, ret = tbl[name]
            e["rm"] = ("builtin", "map." + name)
        elif is_chan(tt):
            # Stage 16 (v0.27.0-alpha): Chan[T] methods. Stage-16
            # perfection (v0.29.0-alpha) adds the non-blocking pair:
            #   send(v: T) -> void       — enqueue (blocks while a bounded channel is full)
            #   try_send(v: T) -> bool   — non-blocking send (False iff full)
            #   recv() -> T              — blocks while empty; deadlock-detected
            #   recv_or(default: T) -> T — non-blocking recv (default if empty)
            #   len() -> int             — pending message count
            elem = chan_inner(tt)
            tbl = {
                "send": ([elem], "void"),
                "try_send": ([elem], "bool"),
                "recv": ([], elem),
                "recv_or": ([elem], elem),
                "len": ([], "int"),
            }
            if name not in tbl:
                self.err("Chan has no method %s (available: send, "
                         "try_send, recv, recv_or, len)" % name, e)
            ptypes, ret = tbl[name]
            e["rm"] = ("builtin", "chan." + name)
        elif is_task(tt):
            # Stage 16 (v0.27.0-alpha): Task[R].join() -> R — wait for the
            # task to finish and return its result. join() exactly once
            # per handle (a second join is a runtime panic).
            if name != "join":
                self.err("Task has no method %s (available: join)" % name, e)
            ptypes, ret = ([], task_inner(tt))
            e["rm"] = ("builtin", "task.join")
        elif "[" in tt and type_base(tt) in self.enums:
            # Method on a generic enum instantiation — currently we do not
            # support user-defined methods on enums. Future: enable `impl`.
            self.err("enum %s has no method %s (enum impl not supported yet)"
                     % (type_base(tt), name), e)
            # Deep-scan-15 defensive: see the matching sentinel in the
            # `else` branch below — same rationale (self.err raises,
            # but analyzers can't prove that).
            ptypes, ret = [], "void"
        else:
            self.err("cannot call method on type %s" % tt, e)
            # Deep-scan-15 defensive: every failing branch above calls
            # self.err() which raises HLError, so ptypes/ret are
            # guaranteed to be assigned when we reach the post-branch
            # code at line ~2630. Initialize sentinels here so static
            # analyzers (pylint E0606) don't flag the post-branch use
            # of `ptypes`/`ret` as possibly-unassigned. If err() ever
            # stops raising, the sentinel values produce a clean
            # type-mismatch error instead of an UnboundLocalError.
            ptypes, ret = [], "void"
        if len(args) != len(ptypes):
            self.err("%s.%s expects %d arguments, got %d"
                     % (tt, name, len(ptypes), len(args)), e)
        for a, pt in zip(args, ptypes):
            at = self.check_expr(a, env, pt)
            if at == "never":
                continue
            if at != pt:
                self.err("argument of %s.%s expects %s, got %s" % (tt, name, pt, at), a)
        # Stage 16: method-level effects + the channel-send data-race rule.
        if e["rm"][0] == "builtin":
            op = e["rm"][1]
            if op in ("chan.send", "chan.try_send", "chan.recv",
                      "chan.recv_or", "chan.len", "task.join"):
                self.edges[self.cur_fn].add("b:" + op)
            if op == "chan.send" or op == "chan.try_send":
                v = args[0]
                vt = ptypes[0]
                if not self.type_is_send(vt):
                    self.err("type %s is not Send: values of this type "
                             "cannot cross a task boundary" % vt, v)
                if is_owned_type(vt) and v["k"] in ("ident", "field", "index"):
                    what = ("variable '" + v["name"] + "'") if v["k"] == "ident" \
                        else "a borrowed value"
                    self.err(
                        "cannot share %s across tasks: %s(...) reads it "
                        "directly — pass clone(x) (private copy) or take(x) "
                        "(ownership transfer). Data-race freedom: no owned "
                        "value may be simultaneously released by two threads."
                        % (what, op), v)
        return ret

    # ---------- match ----------
    def check_match(self, e, env, expected):
        scrut_t = self.check_expr(e["scrut"], env, None)
        if scrut_t == "never":
            self.err("never value cannot be used in match", e)
        if "[" in scrut_t:
            ename = type_base(scrut_t)
        else:
            ename = scrut_t
        if ename not in self.enums:
            self.err("match scrutinee must be an enum, got %s" % scrut_t, e)
        edef = self.enums[ename]
        # For generic enums, instantiate variant payloads from scrut_t.
        typeparams = edef["typeparams"]
        type_map = {}
        if typeparams:
            sargs = type_args(scrut_t)
            if len(sargs) != len(typeparams):
                self.err("invalid enum instantiation: %s" % scrut_t, e)
            for tp, sa in zip(typeparams, sargs):
                type_map[tp] = sa
        # Check exhaustiveness and arm types.
        covered = set()
        has_wildcard = False
        arm_types = []
        for arm in e["arms"]:
            pat = arm["pattern"]
            if pat["k"] == "wildcard":
                has_wildcard = True
            else:
                pen = pat["enum"]
                pv = pat["variant"]
                # Deep-scan-10: a BARE pattern (no `Enum.` prefix —
                # accepted per the SPEC §5 grammar) resolves against the
                # scrutinee's enum here.
                if pen == "":
                    pen = ename
                    pat["enum"] = ename
                if pen != ename:
                    self.err("match arm pattern belongs to enum %s, not %s"
                             % (pen, ename), arm)
                # find the variant
                variant = None
                for v, payloads in edef["variants"]:
                    if v == pv:
                        variant = (v, payloads)
                        break
                if variant is None:
                    self.err("enum %s has no variant %s" % (ename, pv), arm)
                v, payloads = variant
                covered.add(v)
                # Check binding count.
                if pat["has_paren"] and len(pat["bindings"]) != len(payloads):
                    self.err("variant %s has %d payloads, but pattern binds %d"
                             % (pv, len(payloads), len(pat["bindings"])), arm)
                if not pat["has_paren"] and len(payloads) != 0:
                    self.err("variant %s requires %d payload bindings"
                             % (pv, len(payloads)), arm)
            # Check arm body in a new scope with the bindings.
            self.child(env)
            if pat["k"] != "wildcard":
                # Bind payload values.
                if pat["has_paren"]:
                    # find variant payloads
                    for v, payloads in edef["variants"]:
                        if v == pat["variant"]:
                            inst_payloads = [instantiate_type(p, type_map) if typeparams
                                             else p for p in payloads]
                            for bname, btype in zip(pat["bindings"], inst_payloads):
                                if bname == "_":
                                    continue
                                # BUG-SC-2 fix: SPEC.md section 5 states that
                                # match-arm bindings INTRODUCE a new scope and
                                # may shadow outer bindings for the duration of
                                # the arm (the one and only shadowing exception).
                                # Previously `self.lookup(env, bname)` searched
                                # ALL scopes and rejected any name already
                                # bound anywhere — contradicting the SPEC. We
                                # now only reject duplicates WITHIN the same
                                # arm (e.g. `E.Foo(a, a)`), which is the only
                                # real error.
                                if bname in env[-1]:
                                    self.err("duplicate binding name in match arm: %s" % bname, arm)
                                env[-1][bname] = [btype, False, False]
                            break
            body_t = self.check_expr(arm["body"], env, expected)
            arm["body_t"] = body_t
            env.pop()
            if body_t == "never":
                arm_types.append(None)
            else:
                arm_types.append(body_t)
        # Exhaustiveness check.
        all_variants = {v for v, _ in edef["variants"]}
        if not has_wildcard:
            missing = all_variants - covered
            if missing:
                self.err("match is not exhaustive; missing: %s"
                         % ", ".join(sorted(missing)), e)
        # All arm types must agree.
        non_never = [t for t in arm_types if t is not None]
        if not non_never:
            # All arms are `never` — match type is never.
            e["t"] = "never"
            return "never"
        first = non_never[0]
        for t in non_never:
            if t != first:
                self.err("match arms have different types: %s and %s" % (first, t), e)
        e["t"] = first
        return first

    # ---------- `?` operator ----------
    def check_qmark(self, e, env, expected):
        inner_t = self.check_expr(e["e"], env, None)
        if inner_t == "never":
            self.err("never value cannot be used in expression", e)
        ename = type_base(inner_t) if "[" in inner_t else inner_t
        if ename not in self.enums:
            self.err("? operator requires an enum type, got %s" % inner_t, e)
        edef = self.enums[ename]
        # Find the "error" variant: Err (with one payload) or None (no payload).
        err_variant = None
        ok_variant = None
        n_err_candidates = 0
        for v, payloads in edef["variants"]:
            if v == "Err" and len(payloads) == 1:
                err_variant = (v, payloads)
                n_err_candidates += 1
            elif v == "None" and len(payloads) == 0:
                err_variant = (v, payloads)
                n_err_candidates += 1
            elif v == "Ok" and len(payloads) == 1:
                ok_variant = (v, payloads)
            elif v == "Some" and len(payloads) == 1:
                ok_variant = (v, payloads)
            else:
                # BUG (deep-scan-5): a third variant beyond the ok/err pair
                # can match NEITHER arm at runtime — the interpreter would
                # panic ("matched neither ok nor err variant") on a
                # checker-clean program. Reject at check time.
                self.err("? operator requires enum %s to have exactly the "
                         "ok variant (Ok/Some) and error variant (Err/None); "
                         "found extra variant '%s'" % (ename, v), e)
        # BUG (deep-scan-5): if the enum declares BOTH 'Err' and 'None',
        # the loop above silently keeps only the LAST one — `?` on the
        # other one panics at runtime. Reject the ambiguity.
        if n_err_candidates > 1:
            self.err("? operator requires enum %s to declare EITHER 'Err' "
                     "OR 'None' as its error variant, not both" % ename, e)
        if err_variant is None:
            self.err("? operator requires enum %s to have an 'Err' (1 payload) or 'None' variant"
                     % ename, e)
        if ok_variant is None:
            self.err("? operator requires enum %s to have an 'Ok' or 'Some' variant (1 payload)"
                     % ename, e)
        # The success value type is the (instantiated) payload type of the
        # success variant.
        typeparams = edef["typeparams"]
        if typeparams:
            type_map = {}
            iargs = type_args(inner_t)
            if len(iargs) != len(typeparams):
                self.err("invalid enum instantiation: %s" % inner_t, e)
            for tp, ia in zip(typeparams, iargs):
                type_map[tp] = ia
            ok_payload = ok_variant[1][0]
            success_t = instantiate_type(ok_payload, type_map)
            # BUG-3 fix: verify the error payload type matches the enclosing
            # function's error type argument. Without this check, `?` on a
            # `Result[int, int]` inside a function returning `Result[int, str]`
            # would silently propagate an `Err(int)` as if it were `Err(str)`.
            if err_variant[1]:  # has a payload (Err, not None)
                err_payload_t = instantiate_type(err_variant[1][0], type_map)
                cur_ret_args = type_args(self.cur_fn_ret) if "[" in self.cur_fn_ret else []
                # The error type argument is the LAST type arg of the return.
                if cur_ret_args and err_payload_t != cur_ret_args[-1]:
                    self.err("? operator: error payload type %s does not match "
                             "enclosing function's error type %s (in return type %s)"
                             % (err_payload_t, cur_ret_args[-1], self.cur_fn_ret), e)
        else:
            success_t = ok_variant[1][0]
        # Check that the enclosing function's return type is compatible.
        cur_ret = self.cur_fn_ret
        if cur_ret == "void":
            self.err("? operator cannot be used in a void function", e)
        cur_ret_base = type_base(cur_ret) if "[" in cur_ret else cur_ret
        if cur_ret_base != ename:
            self.err("? operator: enclosing function returns %s, cannot propagate %s"
                     % (cur_ret, ename), e)
        e["t"] = success_t
        e["err_variant"] = err_variant[0]
        e["ok_variant"] = ok_variant[0]
        return success_t

    # ---------- effects (Stage 9-alpha: fine-grained, set-based) ----------
    def check_effects(self):
        """Fixpoint on the static call graph that computes, per function, the
        SET of effects its body transitively requires. A function passes iff
        its declared effect set is a superset of the computed set.

        Stage 9-beta: after the fixpoint converges, the per-function computed
        effect set is stored in self.computed_effects so an external auditor
        (e.g. boot.py --audit) can print the full capability tree.
        """
        # eff[key] = computed set of effects required by `key`'s body.
        # BUG-DS4-2: initialise from ALL call-graph nodes — self.fns keys PLUS
        # the synthetic "@default.<Struct>" nodes added while checking
        # struct field defaults (their effects must reach constructors).
        eff = {key: set() for key in self.edges}

        # Monotone fixpoint: union in each callee's computed effect set.
        changed = True
        while changed:
            changed = False
            for key, callees in self.edges.items():
                for c in callees:
                    if c.startswith("b:"):
                        new_eff = BUILTIN_EFFECTS.get(c[2:], set())
                    else:
                        # BUG-SC-1 fix (SOUNDNESS): extern fns have no body
                        # (so no outgoing edges), but their DECLARED effects
                        # are part of the capability surface. A caller of an
                        # extern fn must declare a superset of the extern's
                        # declared `uses` set. Previously the fixpoint used
                        # `eff[c]` (always empty for externs), so callers
                        # silently bypassed the capability check — a function
                        # calling `puts` (declared `uses IO`) without declaring
                        # `uses IO` itself would compile cleanly.
                        callee_fn = self.fns.get(c)
                        if callee_fn is not None and callee_fn.get("extern", False):
                            new_eff = set(callee_fn["effects"])
                        else:
                            new_eff = eff.get(c, set())
                    before = len(eff[key])
                    eff[key] |= new_eff
                    if len(eff[key]) != before:
                        changed = True

        # Publish the computed effect sets for downstream audit.
        self.computed_effects = eff

        # Capability check: declared ⊇ computed.
        # Also enforce the `pure` keyword (Stage 9-beta): a function declared
        # `pure` must have BOTH an empty declared set AND an empty computed
        # set (it transitively calls nothing effectful).
        for key, fn in self.fns.items():
            declared = fn["effects"]
            computed = eff[key]
            missing = computed - declared
            if not missing:
                # If the function is marked `pure`, verify it actually is.
                if fn.get("pure", False):
                    if computed:
                        self.err(
                            "function '%s' is declared 'pure' but transitively "
                            "uses effects %s (declared pure but callee chain "
                            "is not pure)" % (fn["name"], ", ".join(sorted(computed))),
                            fn)
                continue
            # Find a witness edge for one of the missing effects and report it.
            # Deep-scan-10 fix: iterate the edges in SORTED order — the
            # old code iterated a Python set, so with two violating
            # callees the REPORTED one varied with PYTHONHASHSEED
            # (SPEC §17.6 promises deterministic Stage-0 behaviour).
            for c in sorted(self.edges.get(key, ())):
                if c.startswith("b:"):
                    c_eff = BUILTIN_EFFECTS.get(c[2:], set())
                    callee_disp = c[2:]
                else:
                    # BUG (deep-scan-5): extern fns have no body, so their
                    # `eff` entry is empty — the witness check silently
                    # passed even though the fixpoint had already unioned
                    # the extern's DECLARED effects into the caller's
                    # computed set (the --audit output showed VIOLATION
                    # while --check said OK). Mirror the fixpoint here:
                    # use the extern's declared effects as the requirement.
                    callee_fn = self.fns.get(c)
                    if callee_fn is not None and callee_fn.get("extern", False):
                        c_eff = set(callee_fn["effects"])
                    else:
                        c_eff = eff.get(c, set())
                    callee_disp = c
                violated = c_eff - declared
                if not violated:
                    continue
                miss = sorted(violated)[0]
                self.err(
                    "function '%s' calls '%s' which requires effect '%s' "
                    "not declared (declared: %s; missing: %s)"
                    % (fn["name"], callee_disp, miss,
                       ", ".join(sorted(declared)) or "(none - pure)",
                       ", ".join(sorted(violated))),
                    fn)


def check(program):
    """Type-check + effects-check a program. Returns the Checker instance
    so callers (e.g. boot.py --audit) can inspect program['computed_effects']
    and the per-function declared effects."""
    c = Checker(program)
    c.check()
    return c
