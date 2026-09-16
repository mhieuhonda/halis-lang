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
from ..lexer import HLError
from .. import proof as _proof
from ..compat import zip_strict

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


def _type_mentions_typeparam(pt, typeparams):
    """True iff any type param name appears in `pt` as a whole identifier
    (word-boundary matched, so typeparam 'T' matches 'list[T]' and
    'map[T, int]' but not 'list[str]')."""
    idc = set("abcdefghijklmnopqrstuvwxyz"
              "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    for tp in typeparams:
        i = 0
        while True:
            j = pt.find(tp, i)
            if j < 0:
                break
            before_ok = j == 0 or pt[j - 1] not in idc
            k = j + len(tp)
            after_ok = k >= len(pt) or pt[k] not in idc
            if before_ok and after_ok:
                return True
            i = j + 1
    return False


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
        for p, a in zip_strict(pargs, aargs):
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
    # Stage 47 (v0.66.0-alpha): process management builtins.
    # proc_spawn forks+execs a child process with configurable stdio
    # (inherit / pipe / null). proc_wait blocks until the child exits
    # and returns the encoded exit status (0..255 normal, 128+signum
    # signal — same encoding as proc_exec). proc_kill sends SIGTERM.
    # proc_child_write / proc_child_read / proc_child_close manage
    # the parent end of the child's stdio pipes. All carry the Proc
    # effect; proc_spawn's program argument is a taint sink (a tainted
    # program enables command-injection — same threat as proc_exec).
    "proc_spawn", "proc_wait", "proc_kill",
    "proc_child_write", "proc_child_read", "proc_child_close",
    # Stage 48 (v0.67.0-alpha): environment + cwd builtins.
    # env_get(key) reads an env var (returns "" if not found). env_has
    # checks existence. env_set / env_unset mutate the env. cwd_get
    # reads the cwd. cwd_set mutates the cwd (path is a taint sink —
    # directory traversal). args_os is the os-string version of args
    # (returns list[str]; the stdlib env_args_os wraps it as
    # list[tainted[str]]). All carry the Proc effect. The key arguments
    # of env_get / env_has / env_set / env_unset are taint sinks (info
    # disclosure / env injection); the path argument of cwd_set is a
    # taint sink (directory traversal).
    # The stdlib (std/env.hls) provides the user-facing API names from
    # the roadmap: env_var (returns Option[tainted[str]] — built from
    # env_has + env_get), env_set_var, env_unset_var, env_current_dir
    # (returns tainted[str] — wraps cwd_get with taint_mark),
    # env_set_current_dir, env_args_os (returns list[tainted[str]] —
    # wraps args_os with taint_mark per element).
    "env_get", "env_has", "env_set", "env_unset",
    "cwd_get", "cwd_set", "args_os",
    # Stage 49 (v0.68.0-alpha): high-resolution time builtins.
    # instant_now_ns() returns monotonic nanoseconds (high-resolution,
    # unaffected by wall-clock changes — used for Instant::now() and
    # short-duration measurements). system_time_now_ms() returns wall-
    # clock milliseconds since the Unix epoch (1970-01-01 UTC) — used
    # for SystemTime::now() and timestamps). Both carry Clock.
    "instant_now_ns", "system_time_now_ms",
    # Stage 50 (v0.69.0-alpha): libm-backed math builtins.
    # All math_* builtins are pure (no effects, deterministic) — they
    # delegate to libm on the native side and to Python's math module
    # in the interpreter. NaN / Inf / signed-zero / subnormal handling
    # matches IEEE-754 (libm is the canonical implementation). The
    # stdlib (std/math.hls) provides the user-facing wrappers like
    # math_sin / math_cos / math_exp / etc. (already named the same
    # way at the stdlib level; the builtins are the low-level hooks).
    "math_sin", "math_cos", "math_tan",
    "math_asin", "math_acos", "math_atan", "math_atan2",
    "math_sinh", "math_cosh", "math_tanh",
    "math_exp", "math_log", "math_log10", "math_log2",
    "math_pow", "math_sqrt", "math_cbrt", "math_hypot", "math_fmod",
    "math_erf", "math_erfc", "math_tgamma", "math_lgamma",
    "math_isnan", "math_isinf", "math_isfinite", "math_signbit",
    "math_copysign",
    # Stage 56 (v0.75.0-alpha): stderr + TTY-detection builtins.
    # eprint / eprintln write to stderr (IO effect; taint sinks — a
    # tainted message lets an attacker inject ANSI escapes into the
    # terminal, the same threat print / println carry). isatty(fd)
    # reports whether a file descriptor is a terminal (IO effect — a
    # stream-state observation; this is the TTY detection that
    # std.color's v0.74.0-alpha limitations deferred to "Stage 56+
    # when std.progress lands and the spinner needs it"). The stdlib
    # std/progress.hls builds the user-facing API on all three.
    "eprint", "eprintln", "isatty",
    # Stage 57 (v0.76.0-alpha): process-identity builtins for std.log.
    # proc_pid() -> int (the syslog TAG[PID] field; never 0 for a live
    # process). sys_hostname() -> str (the syslog HOSTNAME field;
    # "localhost" fallback). Both carry Proc (process-state access —
    # the same family as env_get / cwd_get). No arguments, no sinks.
    # The values differ BETWEEN the interpreter process and the native
    # binary process (two different pids), so std.log's pure renderers
    # take them as explicit arguments; only the live emit path reads
    # them (differential tests use fixed synthetic values).
    "proc_pid", "sys_hostname",
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
    # Stage 56 (v0.75.0-alpha): stderr + TTY-detection builtins.
    # eprint / eprintln write to stderr — IO (the effect algebra does
    # not distinguish which stream, only that a stream is written).
    # isatty queries a file descriptor's stream state — an I/O
    # observation (same family as read_line observing stdin), NOT a
    # Proc process-state access (no env / cwd / subprocess involved).
    "eprint":      {"IO"},
    "eprintln":    {"IO"},
    "isatty":      {"IO"},
    # Stage 57 (v0.76.0-alpha): process-identity builtins — Proc
    # (the process's / host's identity is process-state access, the
    # same family as env_get / cwd_get).
    "proc_pid":     {"Proc"},
    "sys_hostname": {"Proc"},
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
    # Stage 47 (v0.66.0-alpha): process management builtins — all
    # carry the existing Proc effect (same as proc_exec). proc_wait
    # and proc_child_read are blocking I/O on the child; proc_kill is
    # a signal; proc_child_write / close are pipe I/O. None of them
    # carry Conc because they don't touch the channel runtime's
    # accounting — a thread blocked in proc_wait is blocked on the
    # child, not on a Halis channel (same rationale as thread_sleep_ms
    # not touching blocked accounting).
    "proc_spawn":            {"Proc"},
    "proc_wait":             {"Proc"},
    "proc_kill":             {"Proc"},
    "proc_child_write":      {"Proc"},
    "proc_child_read":       {"Proc"},
    "proc_child_close":      {"Proc"},
    # Stage 48 (v0.67.0-alpha): environment + cwd builtins — all
    # carry the Proc effect. Environment access is process-state
    # access; cwd access is process-state access. Using Proc keeps
    # the effect algebra simple (no new effect family) and matches
    # the existing convention that proc_exec is the canonical Proc
    # builtin (anything that interacts with the process's external
    # state — args, env, cwd, subprocess — carries Proc).
    "env_get":              {"Proc"},
    "env_has":              {"Proc"},
    "env_set":              {"Proc"},
    "env_unset":            {"Proc"},
    "cwd_get":              {"Proc"},
    "cwd_set":              {"Proc"},
    "args_os":              {"Proc"},
    # Stage 49 (v0.68.0-alpha): high-resolution time builtins — both
    # carry Clock. instant_now_ns reads the monotonic clock (same family
    # as clock_ms); system_time_now_ms reads the wall clock (which can
    # jump backwards on NTP adjustments — Instant should be used for
    # duration measurements, SystemTime for timestamps).
    "instant_now_ns":       {"Clock"},
    "system_time_now_ms":   {"Clock"},
    # NOTE: Stage 50 (v0.69.0-alpha) math_* builtins are PURE (no
    # effects) — they do not appear in BUILTIN_EFFECTS. libm is a pure
    # function library: sin(x) on the same x always returns the same
    # value, with no side effects on program state. NaN/Inf are values,
    # not effects.
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
    # Stage 56 (v0.75.0-alpha): stderr writes are sinks for the same
    # reason stdout writes are — a tainted message lets an attacker
    # inject ANSI escape sequences into the terminal (spoofing,
    # title reprogramming, clipboard theft on some terminals).
    "eprint":       (0,),
    "eprintln":     (0,),
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
    # Stage 47 (v0.66.0-alpha): proc_spawn's program (arg 0) is a
    # taint sink — a tainted program enables command injection (same
    # threat model as proc_exec). The args list (arg 1) is a
    # list[str] of command-line arguments; the elements are NOT
    # sink-checked (they're passed to the child as argv[1..], not
    # interpreted by a shell). The user must sanitise tainted args
    # before adding them to the list — same convention as proc_exec's
    # single shell-string argument. The stdio kind ints (args 2/3/4)
    # are primitive ints (untaintable by the type system).
    "proc_spawn":       (0,),    # program
    # Stage 48 (v0.67.0-alpha): env builtins as taint sinks.
    # env_get's KEY (arg 0) is a sink: looking up an attacker-controlled
    # env var name is an information disclosure (the attacker learns
    # which env vars exist by observing the program's behaviour).
    # env_has's KEY (arg 0) is a sink: same info disclosure threat.
    # env_set's KEY (arg 0) is a sink: setting an attacker-controlled
    # env var name enables env injection (LD_PRELOAD, PATH, IFS, ...).
    # The VALUE (arg 1) is NOT a sink: the user is the origin of the
    # value they're setting (same convention as net_write: the data
    # being sent is the user's data, not the attacker's).
    # env_unset's KEY (arg 0) is a sink: same threat as env_set.
    # cwd_set's PATH (arg 0) is a sink: a tainted path enables
    # directory traversal (the attacker could cd to /etc/passwd or a
    # sensitive directory).
    # cwd_get / args_os take no arguments — no sink.
    "env_get":               (0,),    # key
    "env_has":               (0,),    # key
    "env_set":               (0,),    # key
    "env_unset":             (0,),    # key
    "cwd_set":               (0,),    # path
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




__all__ = [
    "BOOL_M",
    "BUILTIN_EFFECTS",
    "BUILTIN_FNS",
    "FLOAT_M",
    "INT64_MAX",
    "INT_M",
    "SINK_BUILTINS",
    "STR_M",
    "_instantiate_type",
    "_type_mentions_typeparam",
    "chan_inner",
    "future_inner",
    "instantiate_type",
    "is_chan",
    "is_future",
    "is_list",
    "is_map",
    "is_owned_type",
    "is_stream",
    "is_taint",
    "is_tainted_type",
    "is_task",
    "list_elem",
    "list_taint_inner",
    "map_val",
    "split_type_args",
    "stream_inner",
    "taint_inner",
    "task_inner",
    "type_args",
    "type_base",
    "unify",
]
