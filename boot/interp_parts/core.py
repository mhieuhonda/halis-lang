"""Interp mixin (core) - verbatim segment of the original
boot/interp.py Interp class (lines 691..859), split for
maintainability. The final Interp class assembles all mixins in
boot/interp_parts/interp.py - behavior is unchanged."""
from .rt import (
    ConcRuntime, HLPanic, HalisRNG, ReturnSig, TailCallSig, sys, threading, to_display,
)
from ..compat import zip_strict

class InterpCore(object):
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
        # Stage 81 (v0.100.0-alpha): reentrancy guard for the panic
        # handler. While the handler itself runs, a nested panic must
        # NOT re-enter it (the allocator may be broken) — it falls
        # straight through to the default report in run().
        self._in_panic_hook = False

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
            for (pn, _, _), v in zip_strict(params, call_args):
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
                                      "runtime)" % fn["name"], self.line) from None
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

