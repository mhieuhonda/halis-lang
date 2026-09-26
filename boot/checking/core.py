"""Checker mixin (core) - verbatim segment of the original
boot/checker.py Checker class (lines 699..1045), split for
maintainability. The final Checker class assembles all mixins in
boot/checking/checker.py - behavior is unchanged."""
from ..lexer import HLError
from .. import linkerscript
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
        # Deep-scan-28: per-loop break/continue edge frames. Each while/for
        # pushes a frame; `break` records the moved-state snapshot at the
        # break point into frame["break"], `continue` into frame["continue"].
        # The loop driver unions break edges into the post-loop state and
        # continue edges into the next-iteration (loop-top) state — without
        # this, a revive AFTER a break/continue edge erases the move for
        # that edge and a moved binding is read back (NULL in native).
        self.loop_frames = []
        # Stage 17: seeded interval facts per fn (for --check --fast and
        # hlprove reporting).
        self.proof_facts = {}
        # Stage 31 (v0.48.0-alpha): fn key -> number of VERIFIED tail
        # self-call sites (filled by tail_call_check_fn).
        self.tail_sites = {}
        # Stage 82 (v0.101.0-alpha): the crate's deterministic stack
        # budget, filled by check_stack_budget for --audit:
        # {"budget", "worst", "path"} or None.
        self.stack_budget_info = None

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
        # 2.5 Stage 77 (v0.96.0-alpha): freestanding / no_std mode
        # enforcement runs BEFORE the bodies are checked so violations
        # report the mode-specific message (not the generic effect
        # error): the module boundary, the host boundary, and the
        # declared-capability boundary need signatures only.
        self.check_crate_modes()
        # 2.6 Stage 81 (v0.100.0-alpha): panic-handler validation runs
        # on signatures only (uniqueness + shape), so it also reports
        # before any body-level error in the handler itself.
        self.check_panic_handler()
        # 2.7 Stage 82 (v0.101.0-alpha): per-fn #[stack_size(N)] frame
        # bounds validate on the AST only (no types needed) — the same
        # pass the self-hosted checker runs on signatures, closing the
        # boot↔hlc parity gap (boot used to parse the attribute and
        # silently ignore the bound).
        self.check_stack_size_attrs()
        # 2.8 Stage 84 (v0.103.0-alpha): linker-script integration. The
        # `#[section("X")]` attributes are validated on the AST only,
        # and the `#![link_script("...")]` attribute (if present) is
        # read from disk and every section the program names must be
        # placed by the script's SECTIONS block.
        self.check_link_script()
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
        # 5. Stage 82 (v0.101.0-alpha): the crate-level deterministic
        # stack budget — runs LAST (needs the complete call graph,
        # including the edges the body checks just added).
        self.check_stack_budget()

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

    # ---------- Stage 81 (v0.100.0-alpha): panic-handler validation ----------
    # `#[panic_handler]` marks the program's single panic handler: a
    # top-level, non-generic, non-extern fn with exactly one `str`
    # parameter returning `void`. The runtime invokes it once per
    # panic (behind a reentrancy guard) before the default action, so
    # the shape is load-bearing: the C hook calls it as
    # `usf_panic_handler(hl_str*)` and the interpreter calls it with
    # one str argument. Anything else is rejected here, on signatures
    # only (no body checking needed — a handler body is checked like
    # any other fn body afterwards).
    def check_panic_handler(self):
        found = None
        for key, fn in self.fns.items():
            if not fn.get("attrs", {}).get("panic_handler", False):
                continue
            if found is not None:
                self.err("only one #[panic_handler] fn per program "
                         "(already have '%s'; '%s' is a duplicate)"
                         % (found, key), fn)
            found = key
            if fn.get("struct") is not None:
                self.err("#[panic_handler] must be a top-level function, "
                         "not a method ('%s')" % key, fn)
            if fn.get("extern", False):
                self.err("#[panic_handler] cannot be an extern fn "
                         "('%s')" % key, fn)
            if fn.get("typeparams"):
                self.err("#[panic_handler] cannot be generic ('%s' has "
                         "type parameters)" % key, fn)
            params = fn.get("params", [])
            if len(params) != 1 or params[0][1] != "str":
                self.err("#[panic_handler] requires exactly one parameter "
                         "of type 'str' (the panic message); '%s' does not "
                         "match" % key, fn)
            if fn.get("ret") != "void":
                self.err("#[panic_handler] must return 'void'; '%s' "
                         "returns '%s'" % (key, fn.get("ret")), fn)

    # ---------- Stage 82 (v0.101.0-alpha): deterministic stack sizing ----------
    # Two halves, mirroring the self-hosted checker exactly:
    #
    # 1. Per-fn `#[stack_size(N)]` — the Stage 28 frame bound, now
    #    validated in boot too (boot used to parse the attribute and
    #    silently ignore it: a program boot accepted, hlc rejected).
    #    The estimator is the same conservative upper bound hlc uses:
    #    32 bytes base + 8 per local slot/param + 16 per call site,
    #    walking the statement tree recursively (if/while/for bodies)
    #    and every call site nested in expressions (including match
    #    arms, struct-literal field values, list items — the nodes the
    #    deep scans kept adding). asm! operands are not modeled (they
    #    are register-resident per the ABI; documented in SPEC 36).
    #
    # 2. The crate budget `#![stack_size(N)]` — the whole-program
    #    deterministic stack size. Frame(fn) as above; the call graph
    #    (edges filled by the body checks, plus the synthetic
    #    "@default.<Struct>" nodes whose cost lives in the constructing
    #    frame) is walked for the WORST chain. Recursion cycles are
    #    rejected outright — a deterministic stack size is impossible
    #    when a cycle's depth is input-dependent. The worst chain must
    #    fit the budget.
    def estimate_stack_size(self, fn):
        b = 32  # base overhead (saved RBP / RBX / alignment)
        b += 8 * len(fn.get("params", []))
        return self._est_stmts(fn.get("body", []), b)

    def _est_stmts(self, stmts, b):
        for s in stmts:
            b = self._est_stmt(s, b)
        return b

    def _est_stmt(self, s, b):
        k = s.get("k")
        if k == "let":
            # Every HLS type lowers to an 8-byte C scalar; void
            # bindings are unreachable and need no slot.
            if s.get("t") != "void":
                b += 8
            b += self._est_calls(s.get("value"))
        elif k == "for":
            # The loop's iteration variable + index temp + iterator
            # handle: 16 bytes, like hlc.
            b += 16
            b = self._est_stmts(s.get("body", []), b)
            b += self._est_calls(s.get("iter"))
        elif k == "while":
            b = self._est_stmts(s.get("body", []), b)
            b += self._est_calls(s.get("cond"))
        elif k == "if":
            b = self._est_stmts(s.get("then", []), b)
            els = s.get("els") or []
            if els:
                b = self._est_stmts(els, b)
            b += self._est_calls(s.get("cond"))
        elif k == "return":
            b += self._est_calls(s.get("value"))
        elif k == "assign":
            b += self._est_calls(s.get("target"))
            b += self._est_calls(s.get("value"))
        elif k == "expr":
            b += self._est_calls(s.get("e"))
        # asm!/break/continue: no modeled frame cost (parity with hlc).
        return b

    def _est_calls(self, e):
        """Counts the call sites nested in an expression tree — 16
        bytes each (caller-saved spills + return address). Mirrors
        hlc's estimate_stack_calls: only plain `call` nodes carry the
        +16 (method/field calls keep their parity with the Stage 28
        estimator), but EVERY child is walked: bin arms, call args,
        field/index targets, `?` chains, match scrutinee + arm bodies,
        list items, struct-literal field values."""
        if not isinstance(e, dict):
            return 0
        k = e.get("k")
        total = 16 if k == "call" else 0
        if k == "bin":
            total += self._est_calls(e.get("l"))
            total += self._est_calls(e.get("r"))
        elif k == "un":
            total += self._est_calls(e.get("e"))
        elif k == "call":
            for a in e.get("args", []):
                total += self._est_calls(a)
        elif k == "fieldcall":
            total += self._est_calls(e.get("target"))
            for a in e.get("args", []):
                total += self._est_calls(a)
        elif k == "field":
            total += self._est_calls(e.get("target"))
        elif k == "index":
            total += self._est_calls(e.get("target"))
            total += self._est_calls(e.get("idx"))
        elif k == "qmark":
            total += self._est_calls(e.get("e"))
        elif k == "match":
            total += self._est_calls(e.get("scrut"))
            for arm in e.get("arms", []):
                total += self._est_calls(arm.get("body"))
        elif k == "listlit":
            for it in e.get("items", []):
                total += self._est_calls(it)
        elif k == "structlit":
            for _fname, fval in e.get("fields", []):
                total += self._est_calls(fval)
        return total

    def check_stack_size_attrs(self):
        for key, fn in self.fns.items():
            sa = fn.get("attrs", {}).get("stack_size", -1)
            if sa is None or sa < 0:
                continue
            if fn.get("extern", False):
                continue
            est = self.estimate_stack_size(fn)
            if est > sa:
                self.err(
                    "#[stack_size(%d)] violated by function '%s': "
                    "estimated frame size is %d bytes (the body declares "
                    "too many locals or nests too many call sites for the "
                    "bound). Reduce locals or raise the bound."
                    % (sa, fn["name"], est), fn)

    # ---------- Stage 84 (v0.103.0-alpha): linker-script integration ------
    # A `#[section("X")]` attribute names an output section the LINKER
    # must place. Two failure modes are silent without a check:
    #   1. the named section is never placed by the script — the linker
    #      discards the function (or parks it in an orphan section at an
    #      address the author never asked for), and
    #   2. `#![link_script("...")]` names a file that is not there, so
    #      the whole `-T script` link silently does not happen.
    # This pass turns both into compile errors. It runs on the AST only
    # (attributes + the crate attribute), so it costs nothing and needs
    # no types.
    def check_link_script(self):
        import os as _os
        sections = []
        for key in sorted(self.fns):
            fn = self.fns[key]
            sec = fn.get("attrs", {}).get("section", "")
            if sec:
                sections.append((sec, fn))
        script_attr = None
        for a in self.p.get("crate_attrs", []):
            if a.get("name") == "link_script":
                script_attr = a
        # Published for --audit: {"path", "placed", "named"}.
        self.link_script_info = None
        if script_attr is not None:
            raw = script_attr.get("value") or ""
            base = self.p.get("entry_dir", "")
            path = raw if _os.path.isabs(raw) else _os.path.join(base, raw)
            try:
                with open(path, "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
            except OSError:
                self.err("crate #![link_script(\"%s\")]: cannot read the "
                         "linker script (resolved to %s)"
                         % (raw, path),
                         {"line": script_attr.get("line", 0)})
            # Comment-aware: a "SECTIONS" mentioned inside a comment is
            # not a SECTIONS block (the Stage-0 twin in boot/linkerscript.py
            # and the self-hosted ls_sections_have_block agree).
            if linkerscript.sections_block(text) is None:
                self.err("crate #![link_script(\"%s\")]: the script has no "
                         "SECTIONS block — a Halis image must describe its "
                         "output section layout explicitly" % raw,
                         # No column: a crate attribute is a whole-item
                         # diagnostic, and the self-hosted compiler has
                         # no column for one either.
                         {"line": script_attr.get("line", 0)})
            placed = linkerscript.placed_patterns(text)
            for sec, fn in sections:
                if not linkerscript.covers(placed, sec):
                    self.err(
                        "#[section(\"%s\")] on function '%s' is not placed by "
                        "the linker script %s — the linker would discard the "
                        "function. Add an output section that matches it "
                        "(e.g. `%s : { KEEP(*(%s)) }`)."
                        % (sec, fn["name"], raw, sec, sec), fn)
            self.link_script_info = {
                "path": path, "named": len(sections),
                "placed": len(placed),
            }
        elif sections:
            # Without a script the names are still emitted (gcc accepts
            # `__attribute__((section))` with its default linker script),
            # so this is not an error — but the author should know the
            # placement is then gcc's, not theirs.
            self.link_script_info = {"path": "", "named": len(sections),
                                     "placed": 0}

    def check_stack_budget(self):
        for a in self.p.get("crate_attrs", []):
            if a.get("name") != "stack_size":
                continue
            n = a.get("value")
            if not isinstance(n, int) or n <= 0:
                self.err("crate attribute 'stack_size' requires a "
                         "positive byte count: #![stack_size(1048576)]", a)
            # Frame cost per call-graph node. Extern fns have no Halis
            # body (the C callee's stack is the C world's business);
            # "@default.<Struct>" nodes cost 0 — their default
            # expressions evaluate inside the constructing frame.
            frames = {}
            for key, fn in self.fns.items():
                frames[key] = 0 if fn.get("extern", False) \
                    else self.estimate_stack_size(fn)
            for key in self.edges:
                if key.startswith("@default.") and key not in frames:
                    frames[key] = 0
            # Longest chain with cycle detection. Sorted iteration
            # keeps the reported chain deterministic regardless of
            # declaration order (the bootstrap re-compile must stay
            # byte-identical).
            memo = {}
            WHITE, GRAY, BLACK = 0, 1, 2
            color = {}
            stack = []

            def user_callees(node):
                return sorted(c for c in self.edges.get(node, ())
                              if c in frames)

            def visit(node):
                """Returns (cost, path) for the worst chain STARTING
                at node. Raises via err() on a recursion cycle."""
                color[node] = GRAY
                stack.append(node)
                best_cost, best_path = 0, []
                for c in user_callees(node):
                    if color.get(c, WHITE) == GRAY:
                        i = stack.index(c)
                        cycle = stack[i:] + [c]
                        self.err(
                            "crate #![stack_size(%d)] requires bounded "
                            "recursion: call cycle %s has no static depth "
                            "bound" % (n, " -> ".join(cycle)), a)
                    if color.get(c, WHITE) == WHITE:
                        ccost, cpath = visit(c)
                    else:
                        ccost, cpath = memo[c]
                    # Strict '>' keeps the FIRST maximum in sorted-callee
                    # order — deterministic in both compilers without a
                    # lexicographic tie-break.
                    if ccost > best_cost:
                        best_cost, best_path = ccost, cpath
                stack.pop()
                color[node] = BLACK
                memo[node] = (frames[node] + best_cost, [node] + best_path)
                return memo[node]

            worst, worst_path = 0, []
            for node in sorted(frames):
                if color.get(node, WHITE) == WHITE:
                    c, p = visit(node)
                    if c > worst:
                        worst, worst_path = c, p
            if worst > n:
                self.err(
                    "crate #![stack_size(%d)]: worst-case stack chain is "
                    "%d bytes (%s), exceeding the budget by %d bytes. "
                    "Reduce frames or depth, or raise the bound."
                    % (n, worst, " -> ".join(worst_path), worst - n), a)
            self.stack_budget_info = {
                "budget": n, "worst": worst, "path": worst_path}
            return

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

    def snapshot_equal(self, a, b):
        """Deep-scan-28: are two moved-state snapshots identical? Used by
        the loop fixpoint driver to detect convergence (the loop-top state
        grows monotonically, so once a pass changes nothing, we are done)."""
        return a == b

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
