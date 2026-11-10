"""Interp mixin (core) - verbatim segment of the original
boot/interp.py Interp class (lines 691..859), split for
maintainability. The final Interp class assembles all mixins in
boot/interp_parts/interp.py - behavior is unchanged."""
from .rt import (
    ConcRuntime, HLPanic, HalisRNG, ReturnSig, TailCallSig, sys, threading, to_display,
)
from .resolve import annotate_program
from ..compat import zip_strict

class InterpCore(object):
    def __init__(self, program, argv, out, contracts=False, det=False,
                 det_trace=False):
        self.p = program
        self.fns = program["fns"]
        self.structs = program["structs"]
        self.enums = program.get("enums", {})
        # Static frame-index resolution: every resolvable ident node
        # gets its absolute env-frame index (resolve.py). The eval
        # sites treat the annotation as a pure fast path — any miss
        # falls through to the chain walk — so this can only change
        # speed, never results. Runs once per Interp, before execution.
        annotate_program(program)
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
        # Stage 127 (v0.145.0-alpha): det=True arms the deterministic
        # scheduler (the mode the native runtime arms through
        # HL_DET_SCHED) — the FIFO baton rotation that makes a
        # concurrent program's output and interleaving identical on
        # every run (SPEC section 67).
        self.conc = ConcRuntime(det=det, trace=det_trace)
        # Stage 37 (v0.56.0-alpha): socket-fd table for the net_tcp_* /
        # net_udp_* builtins. The fd namespace is a monotonic counter
        # (starting at 1) so Halis fds do NOT collide with the C
        # runtime's stdin/stdout/stderr (0/1/2). The dict maps
        # int fd -> Python socket object. net_close pops the entry;
        # double-close is a silent no-op (matches the C runtime).
        self.net_fds = {}
        self._net_next_fd = 1
        # Stage 81 (v0.100.0-alpha): reentrancy guard for the panic
        # handler. While the handler itself runs, a nested panic must
        # NOT re-enter it (the allocator may be broken) — it falls
        # straight through to the default report in run().
        self._in_panic_hook = False
        # Hot-path precomputation over the (already checked, immutable)
        # program table. Both structures exist so call_fn and the
        # return handler stop paying per-call dict.get chains:
        #   _fn_plan[key]  -> (is_extern, recv, param_names, body,
        #                     param_preds, requires, ensures, ret_pred)
        #                     with recv = (name, mut) for struct methods,
        #                     None otherwise
        #   _tail_fns      -> set of fn keys carrying #[tail_call]
        self._fn_plan = {}
        self._tail_fns = set()
        for key, fn in self.fns.items():
            attrs = fn.get("attrs") or {}
            if attrs.get("tail_call", False):
                self._tail_fns.add(key)
            params = fn["params"]
            if fn["struct"] is not None:
                recv = (params[0][0], params[0][2])
                rest = params[1:]
            else:
                recv = None
                rest = params
            self._fn_plan[key] = (
                fn.get("extern", False),
                recv,
                tuple(p[0] for p in rest),
                fn["body"],
                fn.get("param_preds"),
                fn.get("requires"),
                fn.get("ensures"),
                fn.get("ret_pred"),
            )

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
            # Stage 81 (v0.100.0-alpha): invoke the program's panic
            # handler first (if one is declared and we are not already
            # inside it). The handler runs with the panic message; a
            # nested panic inside the handler is swallowed (the guard
            # above); an exit() inside the handler propagates (the
            # handler chooses the halt action). When the handler
            # returns, the default report below still runs — mirroring
            # the native hl_panic_hook contract exactly.
            h = self.fns.get("panic_handler")
            if (h is not None
                    and h.get("attrs", {}).get("panic_handler", False)
                    and not self._in_panic_hook):
                self._in_panic_hook = True
                try:
                    self.call_fn("panic_handler", [ex.msg])
                except HLPanic:
                    # A nested panic inside the handler falls through
                    # to the default report (mirrors the native
                    # hl_in_panic guard).
                    pass
                except Exception:
                    # A buggy handler must not mask the original fault.
                    # SystemExit is NOT an Exception subclass — an
                    # exit() inside the handler (its chosen halt
                    # action) propagates untouched.
                    pass
                finally:
                    self._in_panic_hook = False
            self.out.flush()
            sys.stderr.write("panic: %s (at line %d)\n" % (to_display(ex.msg), ex.line))
            return 101
        if r is None:
            return 0
        return int(r) & 0xFF

    def call_fn(self, key, args):
        # The per-fn plan was precomputed in __init__ (extern flag,
        # receiver slot, parameter names, body, contract/refinement
        # hooks): a single tuple unpack replaces the per-call dict.get
        # chains the original paid on every user call.
        (is_extern, recv, param_names, body, param_preds,
         requires, ensures, ret_pred) = self._fn_plan[key]
        if is_extern:
            return self.call_extern(self.fns[key], args)
        # Stage 31 (v0.48.0-alpha): #[tail_call] trampoline. The checker
        # verified every self-call is in tail position with primitive-
        # only dataflow, so a tail `return f(...)` arrives here as
        # TailCallSig: rebind the parameters and re-run the body in the
        # SAME Python frame (constant Python stack, mirroring the
        # native goto loop). The fn-key stack is pushed per iteration
        # so nested (non-tail) calls from the body still resolve the
        # correct "current fn".
        tls = self._tls
        fn_stack = tls.fn_stack
        contracts = self.contracts
        while True:
            env0 = {}
            env = [env0]
            if recv is not None:
                env0[recv[0]] = [args[0], recv[1], False]
                call_args = args[1:]
            else:
                call_args = args
            # Zip-strict semantics without the generator: the checker
            # guarantees call arity, so the mismatch guard is the same
            # ValueError zip(strict=True) would raise, just earlier.
            if len(param_names) != len(call_args):
                raise ValueError(
                    "zip(): iterators have different lengths "
                    "(strict mode requires equal lengths)")
            for pn, cv in zip(param_names, call_args):
                env0[pn] = [cv, False, False]
            # Stage 98: refinement guards on refined parameters — the
            # callee-side boundary of the type invariant. Evaluated
            # unconditionally (refinements are always-on semantics,
            # unlike --contracts); the native compiler emits the same
            # guards at fn entry.
            if param_preds:
                # Parameter order (not pred-dict order): a violation in
                # the first refined parameter must be the one reported,
                # exactly as the zip_strict loop reported it.
                for pn in param_names:
                    ref = param_preds.get(pn)
                    if ref is not None:
                        self.check_refine(
                            ref, env0[pn][0],
                            "parameter '%s' of '%s'" % (pn, key))
            # Stage 17: runtime `requires` assertion (enabled by --contracts).
            if contracts and requires is not None:
                if not self._truthy(self.eval_expr(requires, env)):
                    raise HLPanic("contract violation: requires of '%s' "
                                  "(function precondition failed at runtime)"
                                  % self.fns[key]["name"], tls.line)
            # Single-statement `return` bodies — the getter/helper shape
            # that dominates the compiler's own call mix — evaluate
            # without entering exec_stmts and without raising/catching
            # ReturnSig: the value flows straight back to the caller.
            # Every side condition the general path enforces is either
            # checked before (requires, param_preds) or excludes this
            # fast path (ensures / ret_pred need the return value
            # wrapped, so they take the general path). The line slot is
            # written exactly as exec_stmt's return arm would, so a
            # panic inside the value reports the same line; the
            # #[tail_call] trampoline keeps its rebinding loop.
            if len(body) == 1 and ensures is None and ret_pred is None:
                s0 = body[0]
                if s0["k"] == "return":
                    v = s0["value"]
                    if v is None:
                        return None
                    tls.line = s0["line"]
                    rc = v.get("rc")
                    if (rc is not None and rc[0] == "user" and rc[1] == key
                            and key in self._tail_fns):
                        args = [self.eval_expr(a, env) for a in v.get("args", [])]
                        continue
                    return self.eval_expr(v, env)
            try:
                fn_stack.append(key)
                self.exec_stmts(body, env)
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
                if contracts and ensures is not None:
                    env0["result"] = [r.value, False, False]
                    if not self._truthy(self.eval_expr(ensures, env)):
                        raise HLPanic("contract violation: ensures of '%s' "
                                      "(function postcondition failed at "
                                      "runtime)" % self.fns[key]["name"],
                                      tls.line) from None
                # Stage 98: return-side refinement guard — the caller
                # may rely on the declared invariant of the refined
                # return type.
                if ret_pred is not None:
                    self.check_refine(
                        ret_pred, r.value,
                        "return value of '%s'" % key)
                return r.value
            finally:
                fn_stack.pop()
            # Implicit void return: still check ensures (result is None).
            if contracts and ensures is not None:
                env0["result"] = [None, False, False]
                if not self._truthy(self.eval_expr(ensures, env)):
                    raise HLPanic("contract violation: ensures of '%s' "
                                  "(function postcondition failed at runtime)"
                                  % self.fns[key]["name"], tls.line)
            return None

    @staticmethod
    def _truthy(v):
        return bool(v)

    # ---------- Stage 98 (v0.117.0-alpha): refinement guards ----------
    def check_refine(self, ref, val, what):
        """Evaluate a refinement predicate on a value; violation is a
        clean panic (exit 101), same discipline as contract checks. The
        predicate is evaluated in a synthetic frame whose ONLY binding
        is `self` (the guarded value) — predicates cannot reference
        anything else, so this cannot observe or clobber the caller's
        environment."""
        env = [{"self": [val, False, False]}]
        if not self._truthy(self.eval_expr(ref["ast"], env)):
            raise HLPanic(
                "refinement violation: %s must satisfy '%s'"
                % (what, ref["text"]), self.line)

