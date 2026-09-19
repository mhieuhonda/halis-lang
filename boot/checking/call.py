"""Checker mixin (call) - verbatim segment of the original
boot/checker.py Checker class (lines 2676..4431), split for
maintainability. The final Checker class assembles all mixins in
boot/checking/checker.py - behavior is unchanged."""
from .. import proof as _proof
from .helpers import (
    BOOL_M, BUILTIN_FNS, FLOAT_M, FREESTANDING_DENY_BUILTINS,
    FREESTANDING_DENY_MSG, INT_M, STR_M, _type_mentions_typeparam,
    chan_inner, future_inner,
    instantiate_type, is_chan, is_future, is_list, is_map, is_owned_type, is_stream, is_taint,
    is_tainted_type, is_task, list_elem, list_taint_inner, map_val, stream_inner, taint_inner, task_inner,
    type_args, type_base, unify,
)
from ..compat import zip_strict

class CheckerCall(object):
    def check_call(self, e, env, expected):
        name = e["name"]
        args = e["args"]
        # Stage 77 (v0.96.0-alpha): freestanding denylist — pure
        # builtins with no freestanding lowering (libm, strtod).
        if name in FREESTANDING_DENY_BUILTINS and self.is_freestanding():
            self.err(FREESTANDING_DENY_MSG % (name + "()"), e)
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
                for i, (a, (pn, pt, _)) in enumerate(zip_strict(args, fn["params"])):
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
                for i, (a, (pn, pt, _)) in enumerate(zip_strict(args, fn["params"])):
                    # Deep-scan-24 fix: pass the parameter type as the
                    # contextual hint when it does not mention any type
                    # parameter — mirroring the non-generic path (which
                    # checks every arg against inst_pt). The old code
                    # checked EVERY arg with hint=None, so a concrete
                    # `list[int]` parameter of a generic fn rejected an
                    # empty literal `[]` ("empty list literal requires a
                    # type in the surrounding context") even though the
                    # non-generic call of the same shape was accepted —
                    # an inconsistent false rejection. A param whose type
                    # carries a typeparam (e.g. list[T]) still gets no
                    # hint: it cannot be resolved from the literal alone.
                    hint = None if _type_mentions_typeparam(pt, typeparams) else pt
                    at = self.check_expr(a, env, hint)
                    first_at[i] = at
                    if at == "never":
                        continue
                    unify(pt, at, typeparams, type_map)
                for tp in typeparams:
                    if tp not in type_map:
                        self.err("cannot infer type argument for %s; provide explicit types"
                                 % tp, e)
            # Type-check arguments against instantiated parameter types.
            for i, (a, (pn, pt, _)) in enumerate(zip_strict(args, fn["params"])):
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
        # positions. This replaces (not wraps) the regular argt() call at
        # every sink site: it enforces the expected type AND the taint
        # rule in one place. Deep-scan-24 fix: the taint diagnostic is
        # checked FIRST — since every sink wants a concrete untainted
        # type, checking the plain type match first made the actionable
        # taint-sink message (with sanitize_* guidance) unreachable.
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
            # Deep-scan-24 fix: check taint BEFORE the plain type
            # mismatch. Every sink passes a concrete untainted `want`
            # (e.g. "str") and `tainted[str] != str`, so the old order
            # ("wants str, got tainted[str]") fired first and the
            # detailed taint-sink diagnostic below — with its
            # sanitize_* / taint_unwrap() guidance — was UNREACHABLE for
            # every sink call site (verified: fail_taint_print.hls and
            # 20+ other sink tests all reported the generic message).
            # Both orders reject the program; this order reports the
            # actionable one. A non-tainted type mismatch (e.g. int)
            # still falls through to the plain message.
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
            if want is not None and at != want:
                self.err("%s expects argument %d to be %s, got %s"
                         % (name, arg_idx + 1, want, at), e)
            return at

        if name in ("print", "println", "eprint", "eprintln"):
            need(1)
            reject_tainted_at_sink(0, "str")
            # Edge name reflects the ACTUAL builtin (b:print / b:eprint /
            # ...) so the --audit output lists which stream family was
            # written. BUILTIN_EFFECTS resolves all four to {"IO"}.
            self.edges[self.cur_fn].add("b:" + name)
            return "void"
        # Stage 56 (v0.75.0-alpha): isatty(fd: int) -> bool — true when
        # the file descriptor refers to a terminal. Carries IO. The fd
        # is a program-controlled constant (0 / 1 / 2), not attacker
        # data — NOT a taint sink.
        if name == "isatty":
            need(1)
            argt(0, "int")
            self.edges[self.cur_fn].add("b:isatty")
            return "bool"
        # Stage 57 (v0.76.0-alpha): process-identity builtins (no args).
        # proc_pid() -> int — the OS process id (syslog TAG[PID]).
        # sys_hostname() -> str — the host name (syslog HOSTNAME).
        if name == "proc_pid":
            need(0)
            self.edges[self.cur_fn].add("b:proc_pid")
            return "int"
        if name == "sys_hostname":
            need(0)
            self.edges[self.cur_fn].add("b:sys_hostname")
            return "str"
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
            # Stage 77: str(float) needs snprintf %.6f (no
            # freestanding implementation).
            if at == "float" and self.is_freestanding():
                self.err(FREESTANDING_DENY_MSG % "str(float)", e)
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
        # ----- Stage 47 (v0.66.0-alpha): process management builtins -----
        # proc_spawn(program, args, stdin_kind, stdout_kind, stderr_kind)
        #   -> int (pid >= 1 on success, -1 on failure). The program is a
        #   taint sink (command injection). The args list is passed as
        #   argv[1..] (NOT shell-interpreted). The stdio kinds are 0=Inherit,
        #   1=Pipe, 2=Null (the Stdio enum from std.process).
        if name == "proc_spawn":
            need(5)
            reject_tainted_at_sink(0, "str")
            at_args = argt(1, "list[str]")
            if at_args != "list[str]":
                self.err("proc_spawn() argument 2 must be list[str], got %s"
                         % at_args, e)
            argt(2, "int")
            argt(3, "int")
            argt(4, "int")
            # Validate the stdio kind literals if they are int literals.
            for kind_idx in (2, 3, 4):
                karg = args[kind_idx]
                if karg["k"] == "int" and (karg["v"] < 0 or karg["v"] > 2):
                    self.err("proc_spawn() stdio kind (arg %d) must be 0, 1, "
                             "or 2 (Inherit/Pipe/Null), got literal %d"
                             % (kind_idx + 1, karg["v"]), e)
            self.edges[self.cur_fn].add("b:proc_spawn")
            return "int"
        # proc_wait(pid: int) -> int — block until child exits; return
        # the encoded exit status (0..255 normal, 128+signum signal,
        # -1 on error). The encoding matches proc_exec.
        if name == "proc_wait":
            need(1)
            argt(0, "int")
            self.edges[self.cur_fn].add("b:proc_wait")
            return "int"
        # proc_kill(pid: int) -> int — send SIGTERM. Returns 0 on
        # success, -1 on error. No effect if the child has already
        # exited (the runtime treats this as success).
        if name == "proc_kill":
            need(1)
            argt(0, "int")
            self.edges[self.cur_fn].add("b:proc_kill")
            return "int"
        # proc_child_write(pid: int, data: str) -> int — write to the
        # child's stdin pipe. Returns bytes written, -1 on error or if
        # the stdin pipe is not open (kind != Pipe). The data argument
        # is NOT a taint sink — the user is the origin of the data
        # being sent to the child's stdin (same convention as
        # net_write, which doesn't sink-check its data arg).
        if name == "proc_child_write":
            need(2)
            argt(0, "int")
            argt(1, "str")
            self.edges[self.cur_fn].add("b:proc_child_write")
            return "int"
        # proc_child_read(pid: int, fd_kind: int, n: int) -> str —
        # read up to n bytes from the child's stdout (fd_kind=1) or
        # stderr (fd_kind=2). Returns "" at EOF or error. fd_kind=0
        # (stdin) is rejected at runtime (the parent cannot read from
        # its own write end). n must be >= 0.
        if name == "proc_child_read":
            need(3)
            argt(0, "int")
            argt(1, "int")
            argt(2, "int")
            self.edges[self.cur_fn].add("b:proc_child_read")
            return "str"
        # proc_child_close(pid: int, fd_kind: int) -> int — close the
        # parent end of the child's stdio pipe. fd_kind: 0=stdin,
        # 1=stdout, 2=stderr. Returns 0 on success, -1 on error or if
        # the pipe is already closed (idempotent — closing a closed
        # pipe returns 0).
        if name == "proc_child_close":
            need(2)
            argt(0, "int")
            argt(1, "int")
            self.edges[self.cur_fn].add("b:proc_child_close")
            return "int"
        # ----- Stage 48 (v0.67.0-alpha): environment + cwd builtins -----
        # env_get(key: str) -> str — read an env var. Returns the value
        # or "" if not found. The key is a taint sink (info disclosure).
        # The stdlib wrapper env_var(key) builds Option[tainted[str]]
        # from env_has + env_get + taint_mark.
        if name == "env_get":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:env_get")
            return "str"
        # env_has(key: str) -> bool — check if an env var exists.
        if name == "env_has":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:env_has")
            return "bool"
        # env_set(key: str, value: str) -> void — set an env var.
        # Key is a taint sink (env injection); value is NOT a sink.
        if name == "env_set":
            need(2)
            reject_tainted_at_sink(0, "str")
            argt(1, "str")
            self.edges[self.cur_fn].add("b:env_set")
            return "void"
        # env_unset(key: str) -> void — unset an env var.
        if name == "env_unset":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:env_unset")
            return "void"
        # cwd_get() -> str — read the cwd. No args. The stdlib wrapper
        # env_current_dir() applies taint_mark to the result.
        if name == "cwd_get":
            need(0)
            self.edges[self.cur_fn].add("b:cwd_get")
            return "str"
        # cwd_set(path: str) -> int — change the cwd. Returns 0 on
        # success, -1 on error. Path is a taint sink.
        if name == "cwd_set":
            need(1)
            reject_tainted_at_sink(0, "str")
            self.edges[self.cur_fn].add("b:cwd_set")
            return "int"
        # args_os() -> list[str] — the os-string version of args.
        # The stdlib wrapper env_args_os() applies taint_mark per
        # element to return list[tainted[str]].
        if name == "args_os":
            need(0)
            self.edges[self.cur_fn].add("b:args_os")
            return "list[str]"
        # ----- Stage 49 (v0.68.0-alpha): high-resolution time builtins -----
        # instant_now_ns() -> int — monotonic nanoseconds (high-res).
        # Used by Instant::now(). Pure-HLS Duration arithmetic is
        # layered on top (Duration is a struct in std/time.hls).
        if name == "instant_now_ns":
            need(0)
            self.edges[self.cur_fn].add("b:instant_now_ns")
            return "int"
        # system_time_now_ms() -> int — wall-clock milliseconds since
        # the Unix epoch (1970-01-01 UTC). Used by SystemTime::now().
        # The wall clock can jump on NTP adjustments — use Instant for
        # duration measurements.
        if name == "system_time_now_ms":
            need(0)
            self.edges[self.cur_fn].add("b:system_time_now_ms")
            return "int"
        # ----- Stage 50 (v0.69.0-alpha): libm-backed math builtins -----
        # All math_* builtins take float args and return float (or bool
        # for the predicates isnan/isinf/isfinite/signbit). math_atan2
        # and math_hypot and math_fmod and math_copysign take 2 floats;
        # math_pow takes (base, exp) as 2 floats. The rest take 1 float.
        # math_lgamma is special — it returns the SIGN of the gamma via
        # a separate call (the C library has lgamma_r that takes an
        # int* sign argument). For HLS we return only the LOG of the
        # absolute value (callers who need the sign can compute it via
        # tgamma() if x is positive — lgamma is for large magnitudes
        # where gamma overflows). math_lgamma here == Python's
        # math.lgamma == C's lgamma (without the sign — the sign is
        # available separately via the signbit of tgamma(x)).
        if name in ("math_sin", "math_cos", "math_tan",
                    "math_asin", "math_acos", "math_atan",
                    "math_sinh", "math_cosh", "math_tanh",
                    "math_exp", "math_log", "math_log10", "math_log2",
                    "math_sqrt", "math_cbrt", "math_erf", "math_erfc",
                    "math_tgamma", "math_lgamma"):
            need(1)
            argt(0, "float")
            self.edges[self.cur_fn].add("b:" + name)
            return "float"
        # Two-argument math builtins.
        if name == "math_atan2":
            need(2)
            argt(0, "float")
            argt(1, "float")
            self.edges[self.cur_fn].add("b:math_atan2")
            return "float"
        if name == "math_pow":
            need(2)
            argt(0, "float")
            argt(1, "float")
            self.edges[self.cur_fn].add("b:math_pow")
            return "float"
        if name == "math_hypot":
            need(2)
            argt(0, "float")
            argt(1, "float")
            self.edges[self.cur_fn].add("b:math_hypot")
            return "float"
        if name == "math_fmod":
            need(2)
            argt(0, "float")
            argt(1, "float")
            self.edges[self.cur_fn].add("b:math_fmod")
            return "float"
        if name == "math_copysign":
            need(2)
            argt(0, "float")
            argt(1, "float")
            self.edges[self.cur_fn].add("b:math_copysign")
            return "float"
        # IEEE-754 predicates — pure, take a float, return a bool.
        if name in ("math_isnan", "math_isinf", "math_isfinite",
                    "math_signbit"):
            need(1)
            argt(0, "float")
            self.edges[self.cur_fn].add("b:" + name)
            return "bool"
        # ----- Stage 16 (v0.27.0-alpha): concurrency builtins -----
        # chan_new() -> Chan[T] — contextual typing (same pattern as
        # map_new()): the surrounding let/param/return type supplies T.
        # Deep-scan-25 fix (soundness): register the Conc effect edge.
        # The BUILTIN_EFFECTS entry existed but the call-graph edge was
        # never added, so `fn mk() -> Chan[int] { return chan_new() }`
        # compiled as PURE — a hole the native compiler (hlc.hls, which
        # does add the b:chan_new edge) never had.
        if name == "chan_new":
            need(0)
            if expected is None or not is_chan(expected):
                self.err("chan_new() requires a 'Chan[T]' type in the "
                         "surrounding context", e)
            self.edges[self.cur_fn].add("b:chan_new")
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
            for i, (a, (pn, pt, _)) in enumerate(zip_strict(vargs, params)):
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
            # Deep-scan-20 fix (HIGH, soundness): the spawned function's
            # RETURN value crosses the task boundary at join() — it must
            # be Send too. Arguments were checked but the return wasn't,
            # so `spawn(make_task)` with `fn make_task() -> Task[int]`
            # smuggled a non-Send join handle across the boundary that
            # argument checking would have rejected (mirrors the native
            # checker in hlc.hls).
            if not self.type_is_send(tfn["ret"]):
                self.err("spawn() target %s returns %s, which is not Send: "
                         "the result crosses the task boundary at join() "
                         "(a Task join handle must stay with its spawner)"
                         % (fname, tfn["ret"]), e)
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
            for i, (a, (pn, pt, _)) in enumerate(zip_strict(vargs, params)):
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
            # Deep-scan-20 fix (HIGH, soundness): same rule as spawn —
            # the future's result value crosses the task boundary at
            # await(), so the return type must be Send.
            if not self.type_is_send(tfn["ret"]):
                self.err("async_spawn() target %s returns %s, which is not "
                         "Send: the result crosses the task boundary at "
                         "await()" % (fname, tfn["ret"]), e)
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
        # Deep-scan-25 fix (soundness): the runtime hard-codes the
        # built-in Option shape ({"enum": "Option", "var": "Some"/"None"}).
        # A user-defined enum Option[T] with a different shape used to
        # pass the check and then never match at runtime ("match: no arm
        # matched"). Validate the shape whenever an Option enum exists.
        if name == "future_poll":
            need(1)
            at = argt(0, None)
            if not is_future(at):
                self.err("future_poll() expects a Future[T], got %s" % at, e)
            if "Option" in self.enums:
                odef = self.enums["Option"]
                has_some = False
                has_none = False
                for vname, payloads in odef["variants"]:
                    if vname == "Some" and len(payloads) == 1:
                        has_some = True
                    if vname == "None" and len(payloads) == 0:
                        has_none = True
                if not (has_some and has_none) or len(odef["variants"]) != 2:
                    self.err("future_poll() requires the built-in Option "
                             "shape 'enum Option[T] { None, Some(T) }' — the "
                             "program's Option enum does not match it", e)
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
        # Deep-scan-25 fix (soundness): the builtin — in BOTH backends
        # (boot interp and the native codegen, which emits
        # hl_chan_send_i64(s, INT64_MIN)) — sends the INT64_MIN sentinel
        # unconditionally. On a non-int stream that value is read back
        # as the element type: the interpreter then type-confuses (raw
        # Python traceback) and the native binary hands a bogus integer
        # to the release helper of a pointer type (memory corruption).
        # Reject stream_close on non-int streams: non-int producers
        # signal end-of-stream by sending their own sentinel value via
        # stream_send, exactly as std/stream.hls documents.
        if name == "stream_close":
            need(1)
            st = argt(0, None)
            if not is_stream(st):
                self.err("stream_close() expects Stream[T], got %s" % st, e)
            if stream_inner(st) != "int":
                self.err("stream_close() only supports Stream[int] (it sends "
                         "the INT64_MIN sentinel); for Stream[%s] send your "
                         "own end-of-stream value via stream_send instead"
                         % stream_inner(st), e)
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
            for i, (a, (pn, pt, _)) in enumerate(zip_strict(vargs, params[1:])):
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
            # Deep-scan-20 fix (LOW/MED, consistency): stream_new()
            # rejects non-Send element types, but gen_spawn — which also
            # creates the stream and hands it to the spawned generator
            # task — performed no such check. Apply the same rule.
            if not self.type_is_send(stream_inner(stream_t)):
                self.err("gen_spawn() stream element type %s is not Send: "
                         "values cross the task boundary through the "
                         "stream" % stream_inner(stream_t), e)
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
            return CheckerCall.is_clone_supported(list_elem(t))
        if is_map(t):
            return CheckerCall.is_clone_supported(map_val(t))
        if is_taint(t):
            return CheckerCall.is_clone_supported(taint_inner(t))
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
            tmap = dict(zip_strict(st["typeparams"], args))
            for _, ftype, _ in st["fields"]:
                ft = instantiate_type(ftype, tmap) if tmap else ftype
                if not self.clone_supported(ft, _seen):
                    return False
            return True
        if base in self.enums:
            en = self.enums[base]
            if len(en["typeparams"]) != len(args):
                return False
            tmap = dict(zip_strict(en["typeparams"], args))
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
            tmap = dict(zip_strict(st["typeparams"], args))
            for _, ftype, _ in st["fields"]:
                ft = instantiate_type(ftype, tmap) if tmap else ftype
                if not self.type_is_send(ft, _seen):
                    return False
            return True
        if base in self.enums:
            en = self.enums[base]
            if len(en["typeparams"]) != len(args):
                return False
            tmap = dict(zip_strict(en["typeparams"], args))
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
                for i, (a, (pn, pt, _)) in enumerate(zip_strict(args, params)):
                    at = self.check_expr(a, env, None)
                    first_at[i] = at
                    if at == "never":
                        continue
                    unify(pt, at, typeparams, type_map)
                for tp in typeparams:
                    if tp not in type_map:
                        self.err("cannot infer type argument for %s.%s" % (tt_base, name), e)
            for i, (a, (pn, pt, _)) in enumerate(zip_strict(args, params)):
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
            # Stage 77: str.to_float() needs strtod (no freestanding
            # implementation).
            if name == "to_float" and self.is_freestanding():
                self.err(FREESTANDING_DENY_MSG % "str.to_float()", e)
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
            # Stage 77: float.to_str() needs snprintf %.6f (no
            # freestanding implementation).
            if name == "to_str" and self.is_freestanding():
                self.err(FREESTANDING_DENY_MSG % "float.to_str()", e)
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
        for a, pt in zip_strict(args, ptypes):
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
