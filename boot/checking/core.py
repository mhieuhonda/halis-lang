"""Checker mixin (core) - verbatim segment of the original
boot/checker.py Checker class (lines 699..1045), split for
maintainability. The final Checker class assembles all mixins in
boot/checking/checker.py - behavior is unchanged."""
from ..lexer import HLError
from .. import proof as _proof
from .helpers import (
    BUILTIN_FNS, chan_inner, future_inner, is_chan, is_future, is_list, is_map, is_stream,
    is_taint, is_task, list_elem, map_val, stream_inner, taint_inner, task_inner, type_args,
    type_base,
)
from ..compat import zip_strict

class CheckerCore(object):
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
        type_map = dict(zip_strict(tp, args))
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
        type_map = dict(zip_strict(tp, args))
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
            # Deep-scan-20 fix (MED): extern (FFI) RETURN types were only
            # checked for existence — `extern fn f() -> list[int]` passed
            # the decl check, then the call-site FFI validation only
            # re-checked the PARAMS, so the interpreter's ctypes path
            # returned a raw int where the type system claimed a list,
            # and the first list op died with a raw Python TypeError
            # (outside the HLError discipline). Validate the ret type
            # at the declaration, same rule as params.
            if fn.get("extern", False):
                if fn["ret"] not in ("int", "float", "bool", "str", "void"):
                    self.err(
                        "extern fn '%s' returns %s — extern fn returns must "
                        "be int / float / bool / str / void (use a "
                        "string-encoded form for complex data)"
                        % (fn["name"], fn["ret"]), fn)
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
        # 5. Stage 77 (v0.96.0-alpha): freestanding / no_std mode
        # enforcement (crate-level `#![...]` attributes).
        self.check_crate_modes()

    # ---------- Stage 77 (v0.96.0-alpha): crate modes ----------
    # `#![freestanding]` / `#![no_std]` turn the whole program into a
    # freestanding (no libc, no OS calls, entry `_start`) or no_std
    # (no `std`, hosted libc kept) crate. Enforcement here is purely
    # static: the module boundary (`std` banned), the host boundary
    # (`extern` blocks banned), and the capability boundary (no
    # declared effects — every function must be pure). Effectful
    # builtins (print, read_file, ...) are additionally rejected by
    # the existing undeclared-effect errors from check_effects().
    def crate_modes(self):
        names = {a["name"] for a in self.p.get("crate_attrs", [])}
        freestanding = "freestanding" in names
        # `#![freestanding]` implies `#![no_std]` (a freestanding
        # crate is always std-free; Stage 78 documents the split).
        no_std = freestanding or "no_std" in names
        return {"freestanding": freestanding, "no_std": no_std}

    def is_freestanding(self):
        """True while checking any function of a `#![freestanding]`
        crate. Reads the program attributes directly (order-free —
        usable from body checking, which runs before
        check_crate_modes)."""
        return self.crate_modes()["freestanding"]

    def check_crate_modes(self):
        modes = self.crate_modes()
        # Published for --audit (boot.py reads checker.crate_modes).
        self.crate_mode_flags = modes
        if not modes["no_std"]:
            return
        label = ("#![freestanding]" if modes["freestanding"]
                 else "#![no_std]")
        for imp in self.p.get("imports", []):
            path = imp.get("path", "")
            if path.startswith("std."):
                self.err("cannot import '%s' in %s mode (the std module "
                         "is disabled; use 'core.*' or a relative import)"
                         % (path, label), imp)
        for ext in self.p.get("externs", []):
            self.err("extern \"%s\" blocks are not available in %s mode "
                     "(no host calls in freestanding code)"
                     % (ext.get("abi", "C"), label), ext)
        for key, fn in self.fns.items():
            declared = fn.get("effects", set())
            if declared:
                self.err("function '%s' declares `uses %s` — capabilities "
                         "are unavailable in %s mode (no OS calls; every "
                         "function must be pure)"
                         % (fn["name"], ", ".join(sorted(declared)), label),
                         fn)

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
