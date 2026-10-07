#!/usr/bin/env python3
"""hllint — Linter for Halis (HLS).

Stage 14: safety rules for HLS programs.

Rules:
  L001  unused-binding        A `let` binding is never referenced after
                              its declaration.
  L002  unused-function       A function is never called.
  L003  unused-struct-field   A struct field is never read.
  L004  ignored-result        A call to a function returning Result[T, E]
                              is used as a statement (no `?`, no
                              `let _ = ...`, no `match`).
  L005  explicit-unwrap       A call to `result_unwrap` or
                              `option_unwrap` without a prior
                              `result_is_ok` / `option_is_some` check.
  L006  unnecessary-effects   A function declares `uses IO` (or any
                              effect) but its body calls only pure
                              functions.
  L007  dead-code-after-return Statements after `return` are unreachable.
  L008  long-function         A function body exceeds 80 statements
                              (refactor candidate).
  L009  shadowing             A `let` binding shadows an outer binding
                              with the same name (info only — the checker
                              already rejects this as a compile error, so
                              this rule never fires for valid programs).
  L010  empty-impl            An `impl` block has no methods.
  L011  inline-always-large   `#[inline(always)]` is on a function with
                              >50 statements (likely a mistake — the
                              inliner will bloat the binary without
                              proportional speedup; consider #[hot] or
                              removing the annotation).
  L012  tail-call-large       `#[tail_call]` is on a function with >30
                              statements (the tail loop re-runs the
                              whole body every iteration — a large body
                              belongs in a helper the loop tail-calls,
                              not in the loop itself).

Stage 118 (v0.137.0-alpha) — autofix mode. Eight of the twelve rules
have a mechanical, semantics-preserving fix:

  L001  unused-binding        rename `let x` to `let _x` (the value's
                              side effects are kept, only the name
                              changes — the underscore prefix is the
                              same convention the rule already honours)
  L002  unused-function       rename `fn f` to `fn _f` (the definition
                              site only — an unused function has no
                              call sites; extern declarations and
                              handler/section fns are never touched)
  L004  ignored-result        wrap the statement: `f(x)` becomes
                              `let _ignored: T = f(x)` (the discard
                              binding, typed with the checker's own
                              result-type text — a bare `let _ =`
                              cannot parse, `_` is a match wildcard)
  L006  unnecessary-effects   delete the `uses ...` clause (the body
                              is provably pure — the clause is a lie)
  L007  dead-code-after-return delete the unreachable statements
  L010  empty-impl            delete the empty `impl` block
  L011  inline-always-large   delete the `#[inline(always)]` attribute
  L012  tail-call-large       delete the `#[tail_call]` attribute

The remaining four need a human: L003 (deleting a field breaks
constructors and layout), L005 (choosing how to handle the error is a
design decision), L008 (refactoring a long function is a design
decision), L009 (never fires — the checker rejects shadowing).

Usage:
  hllint FILE.hls              # print warnings to stdout, exit 0
  hllint --strict FILE.hls     # exit non-zero if any warnings
  hllint --rule L001 FILE.hls  # only run rule L001
  hllint --list                # list rules and exit
  hllint --fix FILE.hls        # apply the safe fixes, write the file
  hllint --diff FILE.hls       # show what --fix would do, write nothing

The fix protocol is pessimistic: every fix is planned as a byte-span
delete/insert/replace against the token stream, applied to a COPY of
the source, and kept only if the copy re-tokenises, re-parses, and
re-lints with every targeted warning gone and no new ones. A fix that
cannot be located exactly (or would be ambiguous — two `fn save`
definitions in different impls, say) is skipped with a reason, never
guessed. --fix loops to a fixed point (removing dead code can surface
a new unused binding), so one run leaves the file as clean as the
fixable rules can get it.

Status: alpha. The linter runs the Stage-0 checker to get the AST +
type/effect info, then walks the AST for each rule. Without --fix or
--diff the source is never modified — it only reports issues.
"""
import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from boot.lexer import tokenize, HLError  # noqa: E402
from boot.parser import Parser  # noqa: E402
from boot.checker import check  # noqa: E402


# ---------------------------------------------------------------------------
# Rule definitions.
# ---------------------------------------------------------------------------

RULES = {
    "L001": ("unused-binding",        "warning"),
    "L002": ("unused-function",       "warning"),
    "L003": ("unused-struct-field",   "warning"),
    "L004": ("ignored-result",       "warning"),
    "L005": ("explicit-unwrap",      "warning"),
    "L006": ("unnecessary-effects",   "warning"),
    "L007": ("dead-code-after-return", "warning"),
    "L008": ("long-function",         "info"),
    "L009": ("shadowing",             "info"),
    "L010": ("empty-impl",            "warning"),
    # Stage 29 (v0.46.0-alpha): inline-always-large — warn when
    # #[inline(always)] is on a function >50 statements. Inlining a
    # large function at every call site bloats the binary without
    # proportional speedup; the user almost certainly meant #[hot]
    # (let the optimiser decide based on the profile). 50 statements
    # is the same threshold gcc uses for its -Winline warning.
    "L011": ("inline-always-large",   "warning"),
    # Stage 31 (v0.48.0-alpha): tail-call-large — warn when
    # #[tail_call] is on a function >30 statements. The transform
    # turns the recursion into a loop that re-runs the ENTIRE body
    # every iteration; a large body (unrelated setup, long branches)
    # should live in a helper that the small tail loop calls, not in
    # the loop itself. 30 statements keeps the hot path reviewable.
    "L012": ("tail-call-large",       "warning"),
}

# Stage 118 (v0.137.0-alpha): the rules with a mechanical fix. Everything
# else needs a human (see the module docstring for the why).
FIXABLE_RULES = {
    "L001", "L002", "L004", "L006", "L007", "L010", "L011", "L012",
}


# ---------------------------------------------------------------------------
# AST walkers.
# ---------------------------------------------------------------------------

def walk_stmts(stmts, fn):
    """Walk all statements (recursively into if/while/for bodies)."""
    for s in stmts:
        fn(s)
        if s["k"] == "if":
            walk_stmts(s["then"], fn)
            if s.get("els"):
                walk_stmts(s["els"], fn)
        elif s["k"] == "while":
            walk_stmts(s["body"], fn)
        elif s["k"] == "for":
            walk_stmts(s["body"], fn)


def walk_expr(e, fn):
    """Walk an expression tree, calling fn(e) on each node."""
    if not isinstance(e, dict):
        return
    fn(e)
    k = e.get("k")
    if k == "bin":
        walk_expr(e["l"], fn)
        walk_expr(e["r"], fn)
    elif k == "un":
        walk_expr(e["e"], fn)
    elif k == "call":
        for a in e["args"]:
            walk_expr(a, fn)
    elif k in ("method", "fieldcall"):
        walk_expr(e["target"], fn)
        for a in e["args"]:
            walk_expr(a, fn)
    elif k == "field":
        walk_expr(e["target"], fn)
    elif k == "index":
        walk_expr(e["target"], fn)
        walk_expr(e["idx"], fn)
    elif k == "qmark":
        walk_expr(e["e"], fn)
    elif k == "match":
        walk_expr(e["scrut"], fn)
        for arm in e["arms"]:
            walk_expr(arm["body"], fn)
    elif k == "listlit":
        for item in e["items"]:
            walk_expr(item, fn)
    elif k == "structlit":
        for _, v in e["fields"]:
            walk_expr(v, fn)


def collect_idents(e, idents):
    """Collect all identifier REFERENCES in an expression.

    Deep-scan-7 fix: field-access names (`x.foo`) were added to the
    ident set — masking a real unused `let foo = ...` (false-negative
    on L001). Field access is now excluded: only the target of a
    `field` node is collected, not the field name itself. Same for
    method calls: the method name is the callee, not a reference to
    a let-binding. Same for struct-literal field names (they're field
    WRITES, not reads).
    """
    def visit(node):
        if not isinstance(node, dict):
            return
        k = node.get("k")
        if k == "ident":
            idents.add(node["name"])
        # field: only collect the target's idents (the field name is
        # NOT a reference to a let binding).
        # method / fieldcall: only the target + args have idents.
        # structlit: only the field VALUES, not the field names.
    walk_expr(e, visit)


def collect_calls(e, calls):
    """Collect all function/method call names in an expression."""
    def visit(node):
        if node.get("k") == "call":
            calls.add(node["name"])
        elif node.get("k") in ("method", "fieldcall"):
            calls.add(node["name"])
    walk_expr(e, visit)


def collect_field_reads(e, fields):
    """Collect all struct field reads (`x.field`) in an expression."""
    def visit(node):
        if node.get("k") == "field":
            fields.add(node["name"])
    walk_expr(e, visit)


def exprs_in_stmt(s):
    """Yield every expression contained in a statement (recursively)."""
    if s is None:
        return
    k = s.get("k")
    if k == "let":
        yield s["value"]
    elif k == "assign":
        yield s["value"]
        # BUG-DS4-21: field/index assignment targets READ their container
        # (`xs[i] = v` reads `xs`; `p.x = 5` reads `p`). The old code only
        # yielded `tgt["idx"]`, so the container identifier was never
        # collected and L001 reported "let binding 'xs' is never used"
        # (false positive) for every index/field assignment. A plain ident
        # target (`x = v`) is still NOT yielded — writing to a binding is
        # not a read, so write-only variables remain lintable.
        tgt = s["target"]
        if tgt["k"] in ("field", "index"):
            yield tgt
    elif k == "return":
        if s.get("value") is not None:
            yield s["value"]
    elif k == "expr":
        yield s["e"]
    elif k == "if":
        yield s["cond"]
    elif k == "while":
        yield s["cond"]
    elif k == "for":
        yield s["iter"]
    elif k == "asm":
        # Deep-scan-20 fix: asm! operand expressions (in(reg) y,
        # inout(reg) x, ...) reference their input bindings — without
        # this, L001 flagged `let y: int = port * 2` used only inside
        # asm!() as "never used" (false positive on the kernel/IRQ
        # code the rule most needs to lint).
        for op in s.get("operands") or []:
            if isinstance(op, dict) and op.get("expr") is not None:
                yield op["expr"]


def all_exprs_in_stmts(stmts):
    """Yield every expression in a statement list (recursively into
    nested if/while/for bodies)."""
    for s in stmts:
        for e in exprs_in_stmt(s):
            yield e
        if s["k"] == "if":
            for e in all_exprs_in_stmts(s["then"]):
                yield e
            if s.get("els"):
                for e in all_exprs_in_stmts(s["els"]):
                    yield e
        elif s["k"] == "while":
            for e in all_exprs_in_stmts(s["body"]):
                yield e
        elif s["k"] == "for":
            for e in all_exprs_in_stmts(s["body"]):
                yield e


# ---------------------------------------------------------------------------
# Linter.
# ---------------------------------------------------------------------------

class Linter:
    def __init__(self, program, only_rules=None, path=None, source=None):
        self.program = program
        self.warnings = []
        self.only_rules = only_rules or set(RULES.keys())
        # L010 scans the raw source (impl blocks are not in the AST).
        # Stage 118: the fixer lints in-memory bytes, so `source` (the
        # exact bytes being linted) takes precedence over `path` (a
        # file to open) — a fix round must see ITS source, not the
        # possibly-stale file on disk.
        self.path = path
        self.source = source
        # Stage 118: rules that have a mechanical fix also emit a fix
        # HINT — a structured description of the edit (kind + the AST
        # nodes / byte spans involved). The fixer consumes hints, never
        # warning text, so the two stay decoupled: a rule's message can
        # change wording without breaking its fix.
        self.fix_hints = []

    def run(self):
        # BUG (deep-scan-5): iterating a SET made the rule order (and the
        # output line order) non-deterministic across runs. Sort for
        # reproducible output.
        for rule_id in sorted(self.only_rules):
            if rule_id not in RULES:
                continue
            method = getattr(self, "_rule_" + rule_id.lower().replace("-", "_"), None)
            if method:
                method()
        return self.warnings

    def _warn(self, rule_id, line, msg, fix=None):
        if rule_id in self.only_rules:
            self.warnings.append((rule_id, RULES[rule_id][1], line, msg))
            # Stage 118: a fix hint rides along with the warning. Hints
            # for non-fixable rules are simply never emitted by the
            # rules themselves.
            if fix is not None:
                self.fix_hints.append((rule_id, line, fix))

    # ---------- rules ----------
    def _rule_l001(self):
        """Unused-binding: a `let` binding is never referenced."""
        for fname, fn in self.program["fns"].items():
            # Collect all identifier references in the function body
            # by walking EVERY expression (including nested ones).
            refs = set()
            for e in all_exprs_in_stmts(fn["body"]):
                collect_idents(e, refs)
            # Now check each `let` binding.
            for s in walk_stmts_collected(fn["body"]):
                if s["k"] == "let":
                    name = s["name"]
                    # Deep-scan-12 fix (DSS-T-10): `let _ = expr` is the
                    # idiomatic way to discard a value (e.g. for a side
                    # effect or to silence a "must consume" lint). The
                    # `_` binding is intentionally unused; flagging it
                    # as L001 is a false positive. Same for `let _foo =`
                    # (the underscore-prefixed convention). Skip both.
                    if name == "_" or name.startswith("_"):
                        continue
                    if name not in refs:
                        self._warn("L001", s.get("line", 0),
                                   "let binding '%s' is never used" % name,
                                   fix={"kind": "rename_let", "name": name,
                                        "line": s.get("line", 0),
                                        "fname": fname})

    def _rule_l002(self):
        """Unused-function: a function is never called."""
        called = set()
        for fname, fn in self.program["fns"].items():
            for e in all_exprs_in_stmts(fn["body"]):
                collect_calls(e, called)
        # Deep-scan-20 fix: functions used only as a spawn target were
        # reported as "never called" — spawn(worker, 41) is a CALL node
        # whose callee name is `spawn`; the function-VALUE argument is an
        # ident the old collector never counted. Fired on every
        # idiomatic concurrency program (feat_conc_spawn/chan/actor...).
        # The checker rewrites the node after check (args= vargs), so
        # look at BOTH the raw first arg and the spawn_fn annotation.
        def _visit_spawn(node):
            if not isinstance(node, dict):
                return
            if node.get("k") == "call":
                # The fn-name-taking builtins (the spawn family and the
                # stream combinators) record their target in spawn_fn
                # once the checker has run — and the checker then
                # REWRITES args to drop the fn-name argument, so
                # spawn_fn is the only reliable source on a checked
                # node. The raw-argument shapes below cover the
                # unchecked fallback (the checker errored out early).
                if node.get("spawn_fn"):
                    called.add(node["spawn_fn"])
                nm = node.get("name", "")
                args = node.get("args") or []
                if nm in ("spawn", "async_spawn", "gen_spawn"):
                    if args and isinstance(args[0], dict) \
                            and args[0].get("k") == "ident":
                        called.add(args[0]["name"])
                # Stage 118: the stream combinators take a function NAME
                # as an argument, exactly like spawn does —
                # stream_map_int(s, double_it) never calls double_it
                # through a call node, so the call-graph collector saw
                # nothing and L002 flagged the target (the fixer then
                # renamed it and broke the stream).
                elif nm in ("stream_map_int", "stream_filter_int",
                            "stream_flat_map_int"):
                    if len(args) > 1 and isinstance(args[1], dict) \
                            and args[1].get("k") == "ident":
                        called.add(args[1]["name"])
                elif nm == "stream_fold_int":
                    if len(args) > 2 and isinstance(args[2], dict) \
                            and args[2].get("k") == "ident":
                        called.add(args[2]["name"])
        for fname, fn in self.program["fns"].items():
            for e in all_exprs_in_stmts(fn["body"]):
                walk_expr(e, _visit_spawn)
        # BUG (deep-scan-5): struct field DEFAULT expressions can call
        # functions (e.g. `x: int = five()`) — a function called only
        # from a default was falsely reported as unused.
        for sname, sdef in self.program["structs"].items():
            for _fname, _ftype, dflt in sdef["fields"]:
                if dflt is not None:
                    collect_calls(dflt, called)
        # Special-case: `main` is always considered used.
        called.add("main")
        # Stage 81 (v0.100.0-alpha): a `#[panic_handler]` fn is invoked
        # via the runtime hook (invisible to the call graph) — never
        # flag it unused.
        for _fname, _fn in self.program["fns"].items():
            if _fn.get("attrs", {}).get("panic_handler", False):
                called.add(_fname)
        # Methods registered as "Struct.method" — the short name is
        # what appears in fieldcall/method nodes.
        for fname in self.program["fns"]:
            if fname == "main":
                continue
            short = fname.split(".")[-1]
            # Stage 118: a `_`-prefixed name is the same "intentionally
            # unused" convention the rule already honours for bindings
            # (L001 skips `_foo`). Without this, the L002 autofix's own
            # output (`fn set` -> `fn _set`) would be flagged again and
            # renamed forever — `_set`, `__set`, ... — so the underscore
            # form is the accepted end state, not a new defect.
            if short.startswith("_"):
                continue
            if fname not in called and short not in called:
                fn = self.program["fns"][fname]
                self._warn("L002", 0, "function '%s' is never called" % fname,
                           fix={"kind": "rename_fn", "fname": fname,
                                "short": short, "line": fn.get("line", 0),
                                "extern": bool(fn.get("extern")),
                                "attrs": fn.get("attrs", {})})

    def _rule_l003(self):
        """Unused-struct-field: a struct field is never read."""
        # Collect all field-read names across all functions.
        read_fields = set()
        for fname, fn in self.program["fns"].items():
            for e in all_exprs_in_stmts(fn["body"]):
                collect_field_reads(e, read_fields)
        # Also collect field reads in struct literal field NAMES — wait,
        # those are field WRITES, not reads. Skip.
        # Check each struct's fields.
        for sname, sdef in self.program["structs"].items():
            for fname, ftype, _ in sdef["fields"]:
                if fname not in read_fields:
                    self._warn("L003", sdef.get("line", 0),
                               "struct field '%s.%s' is never read" % (sname, fname))

    def _rule_l004(self):
        """Ignored-result: a call returning Result is used as a statement.

        Stage 14 release: the original alpha was a crude substring match
        (flag any call whose name contained `parse` or started with
        `result_`). The release version walks the AST with the checker's
        annotations: if a `call` node has type `Result[...]` (or the
        callee's signature returns `Result[...]`) AND it appears as the
        direct expression of an `expr` statement (not in a `?`, not in a
        `let`, not in a `match` scrutinee), warn.
        """
        # First, build a callee -> return-type map.
        callee_rets = {}
        for fname, fn in self.program["fns"].items():
            callee_rets[fname] = fn.get("ret", "void")
        # Methods: short name -> return type.
        for fname, fn in self.program["fns"].items():
            if "." in fname:
                short = fname.split(".", 1)[1]
                callee_rets[short] = fn.get("ret", "void")
        # Builtins that return Result-like values.
        builtins_returning_result = {
            "read_line", "read_file_tainted",
        }
        for b in builtins_returning_result:
            callee_rets.setdefault(b, "Result[str, int]")

        def _is_result_type(t):
            if not t:
                return False
            return t.startswith("Result[") or t == "Result"

        for fname, fn in self.program["fns"].items():
            for s in walk_stmts_collected(fn["body"]):
                if s["k"] != "expr":
                    continue
                e = s["e"]
                # Top-level call.
                if e.get("k") == "call":
                    callee = e.get("name", "")
                    rt = callee_rets.get(callee, "")
                    # Also check the AST annotation if the checker ran.
                    if not _is_result_type(rt):
                        rt = e.get("t", "")
                    if _is_result_type(rt):
                        self._warn("L004", s.get("line", 0),
                                   "call to '%s' returns Result; result is ignored "
                                   "(use `?` to propagate, or bind a discard: "
                                   "`let _ignored: %s = ...`)" % (callee, rt),
                                   fix={"kind": "wrap_let",
                                        "line": s.get("line", 0),
                                        "head": callee,
                                        "ty": rt})
                # Top-level method call (`x.method()`).
                elif e.get("k") == "method":
                    m = e.get("name", "")
                    rt = callee_rets.get(m, "")
                    if not _is_result_type(rt):
                        rt = e.get("t", "")
                    if _is_result_type(rt):
                        self._warn("L004", s.get("line", 0),
                                   "method call '%s' returns Result; result is ignored "
                                   "(use `?` to propagate, or bind a discard: "
                                   "`let _ignored: %s = ...`)" % (m, rt),
                                   fix={"kind": "wrap_let",
                                        "line": s.get("line", 0),
                                        "head": m,
                                        "ty": rt})

    def _rule_l005(self):
        """Explicit-unwrap without prior is_some/is_ok check.

        Stage 14 release: control-flow-aware. Walks each function's
        statements in order; tracks per-binding whether there's been a
        recent (in the same block, after the binding's last assignment)
        `is_some` / `is_ok` check on the SAME value. A `result_unwrap(x)`
        or `option_unwrap(x)` is only flagged when no such check has
        occurred in the same block.

        Conservative: a false-negative (we miss a real unsafe unwrap
        across an if-branch) is acceptable; a false-positive (flag a
        safe unwrap) is not. So we ONLY warn when:
          - the unwrap is on an identifier `x`, AND
          - there was NO recent `is_some(x)` / `is_ok(x)` in the same
            block scope.
        If we can't tell (e.g. the unwrap is on a complex expression,
        or the prior check is inside a nested block), we DON'T warn.
        """
        UNWRAP_NAMES = {"result_unwrap", "option_unwrap"}
        # Deep-scan-20 fix (HIGH): the polarity was inverted for the
        # negative predicates. is_ok/is_some prove the value good in the
        # THEN branch; is_err/is_none prove it good in the ELSE branch
        # (and after the if when the then-branch terminates — the
        # early-exit idiom). The old code marked the THEN branch for
        # BOTH polarities: safe early-exit code was flagged while
        # `if result_is_err(r) { result_unwrap(r) }` (which panics)
        # sailed through.
        OK_PREDICATES = ("result_is_ok", "option_is_some")
        ERR_PREDICATES = ("result_is_err", "option_is_none")

        def _cond_var(cond):
            """(predicate-kind, checked-ident) for an is_*/is_* call cond."""
            if cond and cond.get("k") == "call" and cond.get("args"):
                arg = cond["args"][0]
                if arg.get("k") == "ident":
                    cn = cond.get("name", "")
                    if cn in OK_PREDICATES:
                        return ("ok", arg["name"])
                    if cn in ERR_PREDICATES:
                        return ("err", arg["name"])
            return (None, None)

        def _block_terminates(stmts):
            """Does this block provably exit (return / panic / exit)?"""
            for st in stmts or []:
                k2 = st.get("k")
                if k2 == "return":
                    return True
                if k2 == "expr":
                    e2 = st.get("e")
                    if isinstance(e2, dict) and e2.get("k") == "call" \
                            and e2.get("name") in ("panic", "exit"):
                        return True
            return False

        def _scan_unwrap_exprs(exprs, cvars):
            """Deep-scan-25 fix (L005 false negative): shared unwrap-scan
            helper. Flags `result_unwrap(x)` / `option_unwrap(x)` calls
            whose argument identifier is not in the checked-set `cvars`."""
            for e in exprs:
                if e is None:
                    continue

                def visit(node):
                    if node.get("k") == "call" and node.get("name") in UNWRAP_NAMES:
                        args = node.get("args", [])
                        if not args:
                            return
                        arg = args[0]
                        # Only flag if the argument is an identifier
                        # that hasn't been recently checked.
                        if arg.get("k") == "ident":
                            if arg["name"] not in cvars:
                                self._warn("L005", node.get("line", 0),
                                           "explicit unwrap of '%s' without prior "
                                           "is_ok/is_some check in this block"
                                           % arg["name"])
                walk_expr(e, visit)

        def _walk_block(stmts, checked_vars):
            """Walk a flat statement list, mutating `checked_vars` (a set
            of variable names whose Result/Option value was recently
            checked). Returns a list of warnings to emit."""
            warns = []
            for s in stmts:
                k = s.get("k")
                if k == "let":
                    # Deep-scan-20 fix: `let b = result_is_err(x)` merely
                    # computes a bool — it does NOT make a later
                    # unwrap(x) safe, and the old code marked x checked
                    # for BOTH polarities (the negative one directly
                    # contradicting the rule). Only the positive
                    # predicates can (weakly) vouch for the value.
                    val = s.get("value")
                    if val and val.get("k") == "call":
                        cn = val.get("name", "")
                        if cn in OK_PREDICATES and val.get("args"):
                            arg = val["args"][0]
                            if arg.get("k") == "ident":
                                checked_vars.add(arg["name"])
                elif k == "assign":
                    # An assignment to x clears x's checked status.
                    tgt = s.get("target")
                    if tgt and tgt.get("k") == "ident":
                        checked_vars.discard(tgt["name"])
                elif k == "if":
                    # Deep-scan-20 fix: branch polarity. is_ok/is_some
                    # marks the THEN branch; is_err/is_none marks the
                    # ELSE branch (and the post-if code when the
                    # then-branch terminates — the early-exit idiom
                    # `if result_is_err(x) { return 1 }` makes a later
                    # unwrap(x) safe).
                    # Deep-scan-25 fix (L005 false negative): the condition
                    # itself is evaluated BEFORE the branches — an unsafe
                    # `if result_unwrap(r) > 0 { ... }` used to slip
                    # through unvisited because the branch `continue`d
                    # before the generic unwrap scan. Scan the condition
                    # with the incoming checked-set.
                    _scan_unwrap_exprs([s["cond"]], checked_vars)
                    kind, cvar = _cond_var(s.get("cond"))
                    then_checked = set(checked_vars)
                    else_checked = set(checked_vars)
                    if kind == "ok":
                        then_checked.add(cvar)
                    elif kind == "err":
                        else_checked.add(cvar)
                    warns.extend(_walk_block(s.get("then", []) or [], then_checked))
                    if s.get("els"):
                        warns.extend(_walk_block(s["els"] or [], else_checked))
                    if kind == "err" and _block_terminates(s.get("then") or []):
                        checked_vars.add(cvar)
                    continue
                elif k == "while":
                    # Inside a while, the checked status from outside
                    # doesn't apply (loop body may execute zero times).
                    # Deep-scan-25 fix (L005 false negative): scan the
                    # loop condition too (first evaluation happens with
                    # the incoming checked-set, before any body run).
                    _scan_unwrap_exprs([s["cond"]], checked_vars)
                    warns.extend(_walk_block(s.get("body", []) or [], set()))
                    continue
                elif k == "for":
                    # Deep-scan-25 fix (L005 false negative): scan the
                    # iterable expression (evaluated once, before the
                    # body — same context as the incoming checked-set).
                    _scan_unwrap_exprs([s["iter"]], checked_vars)
                    warns.extend(_walk_block(s.get("body", []) or [], set()))
                    continue
                # Look for unwrap calls in this statement's expressions.
                for e in exprs_in_stmt(s):
                    _scan_unwrap_exprs([e], checked_vars)
            return warns
        for fname, fn in self.program["fns"].items():
            _walk_block(fn["body"], set())

    def _rule_l006(self):
        """Unnecessary-effects: function declares `uses` but body is pure."""
        # We need the checker's `computed_effects` for this. The checker
        # already errors on the reverse case (uses IO but body calls
        # impure). For this rule, we flag functions whose DECLARED
        # effects are a strict superset of their COMPUTED effects.
        try:
            checker = check(self.program)
            computed = getattr(checker, "computed_effects", {})
        except HLError:
            return
        for fname, fn in self.program["fns"].items():
            declared = set(fn["effects"])
            comp = computed.get(fname, set())
            if declared and not comp:
                self._warn("L006", 0,
                           "function '%s' declares effects {%s} but body is pure"
                           % (fname, ", ".join(sorted(declared))),
                           fix={"kind": "drop_uses", "fname": fname,
                                "short": fname.split(".")[-1],
                                "line": fn.get("line", 0),
                                "extern": bool(fn.get("extern"))})

    def _rule_l007(self):
        """Dead-code-after-return: statements after `return` are unreachable."""
        for fname, fn in self.program["fns"].items():
            self._check_dead_after_return(fn["body"], fname)

    def _check_dead_after_return(self, stmts, fname):
        seen_return = False
        hinted = False  # Stage 118: one delete hint per dead run/block.
        for s in stmts:
            if seen_return:
                if not hinted:
                    # The fixer deletes from the FIRST dead statement of
                    # the run to the enclosing block's closing brace, so
                    # one hint (the run's head) is all it needs.
                    hinted = True
                    self._warn("L007", s.get("line", 0),
                               "statement after `return` is unreachable",
                               fix={"kind": "delete_dead",
                                    "head": s,
                                    "line": s.get("line", 0)})
                else:
                    self._warn("L007", s.get("line", 0),
                               "statement after `return` is unreachable")
            if s["k"] == "return":
                seen_return = True
            # Recurse into nested scopes (the return inside an if-branch
            # only kills code AFTER the if, not inside it).
            if s["k"] == "if":
                self._check_dead_after_return(s["then"], fname)
                if s.get("els"):
                    self._check_dead_after_return(s["els"], fname)
                # Deep-scan-12 fix (DSS-T-11): if BOTH branches of an
                # `if` end in `return`, the code AFTER the if is also
                # unreachable. The previous check only flagged a
                # top-level `return` statement, missing the common
                # pattern of `if cond { return X } else { return Y }`
                # followed by more statements. Detect the
                # both-branches-return case here.
                if _stmts_end_in_return(s["then"]) and \
                        _stmts_end_in_return(s.get("els") or []):
                    seen_return = True
            elif s["k"] == "while":
                self._check_dead_after_return(s["body"], fname)
            elif s["k"] == "for":
                self._check_dead_after_return(s["body"], fname)

    def _rule_l008(self):
        """Long-function: function body exceeds 80 statements."""
        for fname, fn in self.program["fns"].items():
            count = [0]
            def count_stmts(s):
                count[0] += 1
            walk_stmts(fn["body"], count_stmts)
            if count[0] > 80:
                self._warn("L008", 0,
                           "function '%s' has %d statements (refactor candidate)"
                           % (fname, count[0]))

    def _rule_l009(self):
        """Shadowing: a `let` binding shadows an outer binding.

        NOTE: the Stage-0 checker already rejects shadowing as a compile
        error, so this rule never fires for valid programs. It's kept
        for documentation and as a placeholder for a future "warning
        before error" mode.
        """
        # No-op: the checker rejects shadowing before the linter runs.
        pass

    def _rule_l010(self):
        """Empty-impl: an `impl` block has no methods.

        BUG (deep-scan-5): this rule was a no-op justified by a false
        claim — the parser silently ACCEPTS `impl Foo {}`. Implement it
        against the raw source (the AST does not carry impl blocks; scan
        the token stream of the original file).
        """
        try:
            if self.source is not None:
                src = self.source
            else:
                with open(self.path, "rb") as f:
                    src = f.read()
        except (OSError, AttributeError, TypeError):
            return
        # Scan for `impl Ident ... { }` with an empty body.
        import re as _re
        for m in _re.finditer(rb"impl\s+[A-Za-z_][A-Za-z0-9_]*\s*(\[[^\]]*\])?\s*{\s*}", src):
            # Find the line of the match.
            line = src[:m.start()].count(b"\n") + 1
            name_m = _re.search(rb"impl\s+([A-Za-z_][A-Za-z0-9_]*)", m.group(0))
            nm = name_m.group(1).decode("utf-8", "replace") if name_m else "?"
            self._warn("L010", line, "impl block for '%s' is empty" % nm,
                       fix={"kind": "delete_span",
                            "start": m.start(), "end": m.end(),
                            "line": line})

    def _rule_l011(self):
        """Stage 29 (v0.46.0-alpha): inline-always-large — warn when
        #[inline(always)] is on a function whose body exceeds 50
        statements (likely a mistake — inlining a large function at
        every call site bloats the binary without proportional speedup;
        the user probably meant #[hot] or no annotation). 50 is the
        same threshold gcc uses for -Winline.

        The function's attrs are stored on the fn dict by the boot
        parser (Stage 28+29)."""
        for fname, fn in self.program["fns"].items():
            attrs = fn.get("attrs")
            if not attrs:
                continue
            if attrs.get("inline") != "always":
                continue
            # Count statements recursively (mirrors L008).
            count = [0]
            def count_stmts(s):
                count[0] += 1
            walk_stmts(fn["body"], count_stmts)
            if count[0] > 50:
                self._warn("L011", fn.get("line", 0),
                           "function '%s' has #[inline(always)] but %d "
                           "statements (>50 — likely a mistake; consider "
                           "removing the annotation or using #[hot])"
                           % (fname, count[0]),
                           fix={"kind": "drop_attr", "attr": "inline_always",
                                "fname": fname, "short": fname.split(".")[-1],
                                "line": fn.get("line", 0)})

    def _rule_l012(self):
        """Stage 31 (v0.48.0-alpha): tail-call-large — warn when
        #[tail_call] is on a function whose body exceeds 30
        statements. The verified transform rebinds the parameters and
        re-runs the ENTIRE body every loop iteration: a large body
        (setup work, long non-loop branches) pays full price on every
        recursion step. Move it into a helper the small tail loop
        calls, or drop the attribute. Mirrors L011's structure (attrs
        are stored on the fn dict by the boot parser)."""
        for fname, fn in self.program["fns"].items():
            attrs = fn.get("attrs")
            if not attrs:
                continue
            if not attrs.get("tail_call", False):
                continue
            # Count statements recursively (mirrors L008/L011).
            count = [0]
            def count_stmts(s):
                count[0] += 1
            walk_stmts(fn["body"], count_stmts)
            if count[0] > 30:
                self._warn("L012", fn.get("line", 0),
                           "function '%s' has #[tail_call] but %d "
                           "statements (>30 — the loop re-runs the whole "
                           "body every iteration; move the bulk into a "
                           "helper the tail loop calls)"
                           % (fname, count[0]),
                           fix={"kind": "drop_attr", "attr": "tail_call",
                                "fname": fname, "short": fname.split(".")[-1],
                                "line": fn.get("line", 0)})


def _stmts_end_in_return(stmts) -> bool:
    """True iff the last statement of `stmts` is a `return` (or an
    `if`/`else` whose both branches end in `return`). Used by L007 to
    detect unreachable code after a both-branches-return if-statement.
    Deep-scan-12 fix (DSS-T-11)."""
    if not stmts:
        return False
    last = stmts[-1]
    if last["k"] == "return":
        return True
    if last["k"] == "if":
        then_returns = _stmts_end_in_return(last["then"])
        els_returns = _stmts_end_in_return(last.get("els") or [])
        return then_returns and els_returns
    return False


def walk_stmts_collected(stmts):
    """Yield each statement in a flat sequence (recursively into nested
    scopes). Used by rules that need to inspect every statement."""
    for s in stmts:
        yield s
        if s["k"] == "if":
            for sub in walk_stmts_collected(s["then"]):
                yield sub
            if s.get("els"):
                for sub in walk_stmts_collected(s["els"]):
                    yield sub
        elif s["k"] == "while":
            for sub in walk_stmts_collected(s["body"]):
                yield sub
        elif s["k"] == "for":
            for sub in walk_stmts_collected(s["body"]):
                yield sub


# ---------------------------------------------------------------------------
# Stage 118 (v0.137.0-alpha): the fixer.
# ---------------------------------------------------------------------------

class _LocateError(Exception):
    """A fix hint could not be located exactly in the source.

    Raised by every locator; caught by the planner, which turns it into
    a per-hint skip record. An autofix that cannot find its target never
    guesses — the warning survives, the file is untouched.
    """


def _line_starts(src):
    """Byte offset of the start of every line, in lexer line order.

    Replicates the Stage-0 lexer's line counting exactly: a line breaks
    after `\\n`, after a lone `\\r`, and ONCE for a `\\r\\n` pair (the
    lexer's `\\r` branch defers to the following `\\n`). Getting this
    wrong by one byte shifts every fix; token round-trip checks in
    _TokIndex catch a drift instead of writing a corrupt file.
    """
    starts = [0]
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == 10:  # \n
            i += 1
            starts.append(i)
        elif c == 13:  # \r
            if i + 1 < n and src[i + 1] == 10:
                i += 2
            else:
                i += 1
            starts.append(i)
        else:
            i += 1
    return starts


def _find_block_close(src, start):
    """Byte offset of the `}` that closes the block containing `start`,
    or -1. String- and comment-aware: `"` literals (with `\\` escapes)
    and `#` line comments are skipped so braces inside them never
    confuse the depth count. `start` must sit inside the block, after
    its opening `{` (any statement start qualifies — the dead-code run
    heads the L007 hints point at are exactly that)."""
    i, n, depth = start, len(src), 0
    while i < n:
        c = src[i]
        if c == 0x22:  # `"` — skip the string literal
            i += 1
            while i < n and src[i] != 0x22:
                if src[i] == 0x5C:  # backslash escape
                    i += 1
                i += 1
            i += 1
            continue
        if c == 0x23:  # `#` — comment to end of line
            while i < n and src[i] not in (10, 13):
                i += 1
            continue
        if c == 0x7B:  # `{`
            depth += 1
        elif c == 0x7D:  # `}`
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return -1


def _absorb_line(src, start, end):
    """Grow a deletion span so a deletable LINE leaves no blank behind.

    If everything before `start` on its line and everything after `end`
    on its (same) line is whitespace, the span grows to swallow the
    whole line including its newline — deleting `#[tail_call]` on its
    own line removes the line, not just the attribute. CRLF-safe (the
    trailing `\\r` is whitespace to strip() and lands inside the
    swallowed range)."""
    ls = src.rfind(b"\n", 0, start) + 1
    le = src.find(b"\n", end)
    if le == -1:
        le = len(src)
    before = src[ls:start]
    after = src[end:le]
    if before.strip() == b"" and after.strip() == b"":
        return ls, (le + 1 if le < len(src) else le)
    return start, end


def _first_nonws(src, line_starts, line):
    """Byte offset of the first non-whitespace character of a (1-based)
    line, or None if the line does not exist / is blank."""
    if line < 1 or line > len(line_starts):
        return None
    ls = line_starts[line - 1]
    le = line_starts[line] if line < len(line_starts) else len(src)
    seg = src[ls:le]
    for j, c in enumerate(seg):
        if c not in (32, 9, 13):
            return ls + j
    return None


class _TokIndex:
    """Token stream + byte-offset bridge.

    Tokens carry (line, col); the source carries bytes; fixes need
    byte spans. The index converts and CHECKS: every kw/ident/sym
    offset is verified to round-trip against the source bytes, so a
    line-table drift raises _LocateError instead of producing an edit
    that mangles the file."""

    def __init__(self, toks, src, line_starts):
        self.toks = toks
        self.src = src
        self.ls = line_starts

    def off(self, i):
        t = self.toks[i]
        if t["k"] == "eof":
            raise _LocateError("unexpected end of file")
        o = self.ls[t["line"] - 1] + t["col"] - 1
        if t["k"] in ("kw", "ident", "sym"):
            b = t["v"].encode("utf-8")
            if self.src[o:o + len(b)] != b:
                raise _LocateError("token/offset mismatch at line %d"
                                   % t["line"])
        return o

    def end(self, i):
        t = self.toks[i]
        return self.off(i) + len(t["v"].encode("utf-8"))

    def fn_defs(self, short):
        """Indices of every `fn <short>` definition site (the `fn`
        keyword token's index; the name is the very next token)."""
        out = []
        for i, t in enumerate(self.toks):
            if t["k"] == "kw" and t["v"] == "fn" \
                    and i + 1 < len(self.toks) \
                    and self.toks[i + 1]["k"] == "ident" \
                    and self.toks[i + 1]["v"] == short:
                out.append(i)
        return out

    def name_index(self, i):
        """Index of the identifier token right after the fn at `i`."""
        return i + 1


class Fixer:
    """Turns fix hints into byte-span edits — pessimistically.

    Protocol: for every round, lint the current source, plan the edits
    the hints describe, apply them to a COPY, and keep the copy only if
    it re-parses, re-lints with at least one warning resolved, no new
    unfixable warnings, and no growth overall — and, when the file is a
    real crate entry, the WHOLE CRATE (boot.boot.load_program + the
    checker over the merged program) still loads and checks no worse
    than before. That last net is what catches what a single-file
    check cannot: a rename colliding with an imported module, a stream
    target that no longer resolves, a binding an assign still names.
    Rounds repeat to a fixed point (deleting dead code can strand a
    function, whose rename is the next round's work) — at most
    MAX_ROUNDS of them.

    The fixer never touches the filesystem's target file; main() decides
    what to write. Verification writes the candidate bytes to a
    throwaway file in the TARGET'S directory (so relative imports
    resolve identically) and deletes it. Every skip is reported with a
    reason, never silent."""

    MAX_ROUNDS = 8

    def __init__(self, only_rules=None, path=None):
        self.only_rules = only_rules
        self.path = path

    # -- public entry ---------------------------------------------------

    def fix_source(self, src):
        """Run the fixed-point loop. Returns (new_src, report)."""
        from collections import Counter
        fixed_counts = {}
        skipped = []
        rounds = 0
        orig = src
        try:
            warnings, hints, status = self._lint(src)
        except HLError as ex:
            # A file that does not parse is never fixed, never written.
            return src, {"error": str(ex), "fixed": {}, "skipped": [],
                         "rounds": 0, "changed": False}
        for _ in range(self.MAX_ROUNDS):
            fixable = [(r, l, h) for (r, l, h) in hints
                       if r in FIXABLE_RULES]
            if not fixable:
                break
            rounds += 1
            try:
                edits, skip = self._plan(src, fixable)
            except HLError as ex:
                skipped.extend((r, l, "planning failed: %s" % ex)
                               for (r, l, _h) in fixable)
                break
            skipped.extend(skip)
            if not edits:
                break
            new_src = self._apply(src, edits)
            if new_src == src:
                break
            ok, why = self._verify(new_src, warnings, status)
            if not ok:
                skipped.extend((r, l, "rolled back: %s" % why)
                               for (r, l, _h) in fixable)
                break
            new_warnings, new_hints, new_status = self._lint(new_src)
            old_ms = Counter((r, m) for r, _s, _l, m in warnings)
            new_ms = Counter((r, m) for r, _s, _l, m in new_warnings)
            for (r, _m), n in (old_ms - new_ms).items():
                fixed_counts[r] = fixed_counts.get(r, 0) + n
            src = new_src
            warnings, hints, status = new_warnings, new_hints, new_status
            if not warnings:
                break
        report = {
            "fixed": fixed_counts,
            "skipped": skipped,
            "rounds": rounds,
            "changed": src != orig,
        }
        return src, report

    # -- lint helper ----------------------------------------------------

    def _crate_status(self, src):
        """Load the WHOLE crate (the file plus every transitive import)
        and check the merged program. Returns None when the crate loads
        and checks clean, else the first error's text.

        This is the semantic net a single-file check cannot be: it
        resolves imports (so functions the entry only names — stream
        targets, std helpers — exist), sees cross-module duplicates (a
        rename that collides with an imported module), and annotates
        the AST nodes the rules read (spawn_fn for the fn-name-taking
        builtins, types for the L004 discard). The candidate bytes are
        written to a throwaway file next to the target so relative
        imports resolve identically; it is always deleted."""
        if not self.path:
            return None  # library use: no crate to load
        from boot.boot import load_program
        tmp_path = None
        try:
            d = os.path.dirname(os.path.abspath(self.path)) or "."
            fd, tmp_path = tempfile.mkstemp(
                prefix=".hllint-fix-", suffix=".hls", dir=d)
            with os.fdopen(fd, "wb") as f:
                f.write(src)
            merged = load_program(tmp_path)
            try:
                check(merged)
            except HLError as ex:
                return str(ex)
            return None
        except HLError as ex:
            return str(ex)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _entry_keys(self, src):
        """The declaration keys the ENTRY file itself defines, so the
        lint view can exclude the imported modules' fns and structs.
        Returns None when the entry alone cannot be parsed."""
        try:
            prog = Parser(tokenize(src)).parse_program()
            return set(prog["fns"]), set(prog["structs"])
        except HLError:
            return None

    def _lint(self, src):
        """Parse + check + lint a byte string.

        Returns (warnings, fix_hints, status) where status is the
        crate-status error text (None when clean — see _crate_status).
        Parse errors of the entry file propagate (the caller refuses to
        fix unparseable files); checker errors do not (the linter works
        on programs the checker rejects — that is the L007 rescue case).

        When the fixer knows the file's path, the lint view is the
        ENTRY FILE's declarations only, but every node comes from the
        crate-merged program's parse — annotated by a check that saw
        the imports. When the crate cannot be loaded (a missing module,
        say), the view falls back to the single-file parse with a
        plain single-file check."""
        from boot.boot import load_program  # noqa: F401 (used via views)
        entry = self._entry_keys(src)
        status = self._crate_status(src)
        program = None
        if entry is not None and self.path:
            # The entry's bytes are the in-memory `src` (which may be
            # mid-fix), so the candidate is spliced in via a temp file
            # and the merged view is collected from there.
            merged = self._merged_view(src)
            if merged is not None:
                program = merged
        if program is None:
            # No crate to load (library use, or the crate cannot load —
            # a missing module, a cross-module duplicate). Fall back to
            # the single-file parse with a plain single-file check.
            toks = tokenize(src)
            program = Parser(toks).parse_program()
            try:
                check(program)
            except HLError:
                pass
        if entry is not None:
            fns, structs = entry
            program = {"fns": {k: v for k, v in program["fns"].items()
                               if k in fns},
                       "structs": {k: v for k, v in program["structs"].items()
                                   if k in structs},
                       "enums": program.get("enums", {}),
                       "imports": program.get("imports", []),
                       "externs": program.get("externs", []),
                       "crate_attrs": program.get("crate_attrs", [])}
        linter = Linter(program, only_rules=self.only_rules, source=src)
        warnings = linter.run()
        return warnings, linter.fix_hints, status

    def _merged_view(self, src):
        """The crate-merged program whose ENTRY-file nodes carry the
        checker's annotations. Returns None when the crate cannot be
        loaded (the caller falls back to the single-file view)."""
        if not self.path:
            return None
        from boot.boot import load_program
        tmp_path = None
        try:
            d = os.path.dirname(os.path.abspath(self.path)) or "."
            fd, tmp_path = tempfile.mkstemp(
                prefix=".hllint-fix-", suffix=".hls", dir=d)
            with os.fdopen(fd, "wb") as f:
                f.write(src)
            merged = load_program(tmp_path)
            check(merged)
            return merged
        except (HLError, OSError):
            return None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # -- planning ---------------------------------------------------------

    def _plan(self, src, fixable):
        """Hints -> (edits, skips). Edits are dicts with byte spans on
        `src`; skips are (rule, line, reason) records.

        L004 discard bindings get per-round unique names (`_ignored`,
        `_ignored2`, ...) — two `let _ignored` in one function is
        shadowing, which the checker rejects and the round would roll
        back over."""
        toks = tokenize(src)
        ti = _TokIndex(toks, src, _line_starts(src))
        edits, skips = [], []
        wraps = 0
        for rule, line, hint in fixable:
            try:
                if rule == "L004":
                    wraps += 1
                    edits.extend(self._plan_one(
                        ti, src, rule, hint,
                        discard=("_ignored" if wraps == 1
                                 else "_ignored%d" % wraps)))
                else:
                    edits.extend(self._plan_one(ti, src, rule, hint))
            except _LocateError as ex:
                skips.append((rule, line, str(ex)))
        return self._resolve_overlaps(edits, skips), skips

    def _plan_one(self, ti, src, rule, hint, discard="_ignored"):
        kind = hint["kind"]
        if kind == "rename_let":
            return self._fix_rename_let(ti, hint)
        if kind == "rename_fn":
            return [self._fix_rename_fn(ti, hint)]
        if kind == "wrap_let":
            return [self._fix_wrap_let(ti, src, hint, discard)]
        if kind == "drop_uses":
            return [self._fix_drop_uses(ti, src, hint)]
        if kind == "delete_dead":
            return [self._fix_delete_dead(ti, src, hint)]
        if kind == "delete_span":
            return [self._fix_delete_span(ti, src, hint)]
        if kind == "drop_attr":
            return self._fix_drop_attr(ti, hint)
        # Unknown hint kind (a rule grew ahead of the fixer): skip.
        raise _LocateError("no fix strategy for hint kind '%s'" % kind)

    # -- one strategy per fixable rule -----------------------------------

    def _fix_rename_let(self, ti, hint):
        """L001: rename the binding to the underscore convention.

        A rename refactor, not a line edit: the binding's declaration
        AND every consistent occurrence inside the enclosing function
        (reads, assign targets like `y = exit(1)`, pattern payload
        bindings) get the new name, so the program stays coherent even
        when the rule's reference collector had a blind spot — a missed
        reference is renamed too and still names the same binding.
        Occurrences that are NOT the binding (field access `x.name`,
        struct-literal field names `{ name: v }`) are left alone.

        The enclosing function must be uniquely locatable (`fn <name>`
        appears exactly once in the file); otherwise the fix is skipped
        rather than guessed."""
        name, line = hint["name"], hint["line"]
        fname = hint.get("fname") or ""
        short = fname.split(".")[-1] if fname else ""
        defs = ti.fn_defs(short) if short else []
        if len(defs) != 1:
            raise _LocateError(
                "cannot uniquely locate the enclosing function '%s' "
                "(%d definition sites)" % (short or "?", len(defs)))
        # The body range: from the fn's opening `{` to its matching `}`.
        b = defs[0]
        n = len(ti.toks)
        j = b
        while j < n and not (ti.toks[j]["k"] == "sym"
                             and ti.toks[j]["v"] == "{"):
            j += 1
        if j >= n:
            raise _LocateError("cannot find the function body")
        close = _find_block_close(ti.src, ti.off(j) + 1)
        if close < 0:
            raise _LocateError("cannot find the function's closing brace")
        brace_end = ti.off(j) + 1
        edits = []
        for i in range(defs[0] + 1, n):
            t = ti.toks[i]
            if t["k"] == "eof" or ti.off(i) >= close:
                break
            if ti.off(i) < brace_end:
                continue  # the signature: params/typeparams stay as-is
            if t["k"] != "ident" or t["v"] != name:
                continue
            prev = ti.toks[i - 1] if i > 0 else None
            nxt = ti.toks[i + 1] if i + 1 < n else None
            if prev and prev["k"] == "sym" and prev["v"] == ".":
                continue  # field / method name access
            if nxt and nxt["k"] == "sym" and nxt["v"] == ":" \
                    and prev and prev["k"] == "sym" \
                    and prev["v"] in ("{", ","):
                continue  # struct-literal field name
            edits.append({"start": ti.off(i), "end": ti.end(i),
                          "text": "_" + name, "rule": "L001",
                          "line": line,
                          "desc": "rename binding '%s'" % name})
        if not edits:
            raise _LocateError("no occurrences of '%s' found in the "
                               "function body" % name)
        return edits

    def _fix_rename_fn(self, ti, hint):
        """L002: `fn f` -> `fn _f` at the definition site.

        An unused function has no call sites, so renaming the one
        definition is the whole edit. Extern declarations (the name is
        the C symbol) and handler/section functions (invoked from
        outside the call graph) are never renamed. An ambiguous short
        name (two `fn save` in different impls) is skipped, not
        guessed."""
        short = hint["short"]
        if hint.get("extern"):
            raise _LocateError("'%s' is an extern declaration "
                               "(the name is the C symbol)" % short)
        attrs = hint.get("attrs") or {}
        if attrs.get("panic_handler") or attrs.get("irq_handler") \
                or attrs.get("section"):
            raise _LocateError("'%s' is a handler/section function "
                               "(invoked outside the call graph)" % short)
        defs = ti.fn_defs(short)
        if len(defs) != 1:
            raise _LocateError("'%s' has %d definition sites — ambiguous"
                               % (short, len(defs)))
        j = ti.name_index(defs[0])
        return {"start": ti.off(j), "end": ti.end(j),
                "text": "_" + short, "rule": "L002", "line": hint["line"],
                "desc": "rename `fn %s`" % short}

    def _fix_wrap_let(self, ti, src, hint, discard="_ignored"):
        """L004: `f(x)` -> `let _ignored: T = f(x)`.

        A pure prefix insertion at the statement's first token: the
        value expression ends exactly where the expression statement
        ended, so the wrap cannot change what the expression reads.
        HLS requires an annotation on every `let` (a bare `let _ =`
        does not parse — `_` is a match-pattern wildcard only), so the
        fix binds the checker's own result-type text to an
        underscore-prefixed discard name, the same convention std/ uses
        (`let _unused: bool = ...`). The verify step re-parses the
        result, so a wrong type text rolls the edit back."""
        line = hint["line"]
        at = _first_nonws(src, _line_starts(src), line)
        if at is None:
            raise _LocateError("cannot locate statement on line %d" % line)
        ty = hint.get("ty") or ""
        if not ty or "\n" in ty or "\r" in ty:
            raise _LocateError("no usable result type for the discard "
                               "binding")
        return {"start": at, "end": at,
                "text": "let %s: %s = " % (discard, ty),
                "rule": "L004", "line": line,
                "desc": "bind a discard (`let %s: %s = ...`)"
                        % (discard, ty)}

    def _fix_drop_uses(self, ti, src, hint):
        """L006: delete the `uses A, B` clause from the signature.

        The rule only fires when the computed effect set is EMPTY, so
        the whole clause is dead weight — deleting it cannot change the
        body's behaviour, only the signature's honesty."""
        short = hint["short"]
        if hint.get("extern"):
            # An extern's `uses` clause is part of the FFI contract the
            # parser demands (`extern fn 'puts' must declare uses IO`) —
            # deleting it breaks the parse, which the verifier would
            # catch; skipping here is the cheaper, clearer path.
            raise _LocateError("'%s' is an extern declaration "
                               "(the clause is part of the FFI contract)"
                               % short)
        defs = ti.fn_defs(short)
        if len(defs) != 1:
            raise _LocateError("'%s' has %d definition sites — ambiguous"
                               % (short, len(defs)))
        i = defs[0]
        n = len(ti.toks)
        j = i + 1
        while j < n:
            t = ti.toks[j]
            if t["k"] == "kw" and t["v"] == "uses":
                break
            if t["k"] == "sym" and t["v"] == "{":
                raise _LocateError("no `uses` clause found after `fn %s`"
                                   % short)
            j += 1
        else:
            raise _LocateError("no `uses` clause found after `fn %s`"
                               % short)
        # The clause is `uses Ident (, Ident)*` — consume the idents.
        start = ti.off(j)
        k = j + 1
        last = k
        while k < n and ti.toks[k]["k"] == "ident":
            last = k
            k += 1
            if k < n and ti.toks[k]["k"] == "sym" and ti.toks[k]["v"] == ",":
                k += 1
        end = ti.end(last)
        if start == end:
            raise _LocateError("`uses` clause has no effect list")
        if start > 0 and src[start - 1:start] == b" ":
            start -= 1
        elif end < len(src) and src[end:end + 1] == b" ":
            end += 1
        return {"start": start, "end": end, "text": b"",
                "rule": "L006", "line": hint["line"],
                "desc": "delete `uses` clause"}

    def _fix_delete_dead(self, ti, src, hint):
        """L007: delete the unreachable run inside its block.

        The span runs from the run head's first token to the enclosing
        block's closing `}` (brace-scan, string/comment aware), so the
        whole unreachable tail goes at once — nested braces, comments
        and all. The `}` itself survives. When the head statement owns
        its line (only indentation before it), the span also swallows
        that indentation — deleting the line must not leave the
        block's `}` glued to a stale indent."""
        head = hint["head"]
        line = hint["line"]
        start = self._stmt_start(ti, src, head, line)
        ls = src.rfind(b"\n", 0, start) + 1
        if src[ls:start].strip() == b"":
            start = ls
        close = _find_block_close(src, start)
        if close < 0:
            raise _LocateError("cannot find the block's closing brace")
        return {"start": start, "end": close, "text": b"",
                "rule": "L007", "line": line,
                "desc": "delete unreachable statements"}

    def _fix_delete_span(self, ti, src, hint):
        """L010: delete the empty `impl` block (the rule's regex match,
        carried verbatim in the hint). Sanity-checked against the
        source before use."""
        start, end = hint["start"], hint["end"]
        if not (0 <= start < end <= len(src)):
            raise _LocateError("impl span out of bounds")
        if not src[start:end].lstrip().startswith(b"impl") \
                or not src[start:end].rstrip().endswith(b"}"):
            raise _LocateError("impl span does not look like an impl block")
        start, end = _absorb_line(src, start, end)
        return {"start": start, "end": end, "text": b"",
                "rule": "L010", "line": hint["line"],
                "desc": "delete empty impl block"}

    def _fix_drop_attr(self, ti, hint):
        """L011 / L012: delete the `#[inline(always)]` / `#[tail_call]`
        attribute list. An attribute belongs to the NEXT `fn` after it
        (that is how parse_attributes consumes them), so a match is
        kept only when that next fn is the flagged one. Deleting the
        annotation is always safe — the attribute only influences
        codegen policy, never semantics."""
        if hint["attr"] == "inline_always":
            pat = [("sym", "#"), ("sym", "["), ("ident", "inline"),
                   ("sym", "("), ("ident", "always"), ("sym", ")"),
                   ("sym", "]")]
        else:  # tail_call
            pat = [("sym", "#"), ("sym", "["), ("ident", "tail_call"),
                   ("sym", "]")]
        toks = ti.toks
        n = len(toks)
        matches = []
        for i in range(n - len(pat) + 1):
            ok = True
            for d, (k, v) in enumerate(pat):
                t = toks[i + d]
                if t["k"] != k or t["v"] != v:
                    ok = False
                    break
            if ok:
                matches.append((i, i + len(pat) - 1))
        if not matches:
            raise _LocateError("no #%s attribute found"
                               % ("[inline(always)]"
                                  if hint["attr"] == "inline_always"
                                  else "[tail_call]"))
        # Keep only matches attached to the flagged fn (next `fn` after
        # the match must define `short`).
        kept = []
        for (a, b) in matches:
            j = b + 1
            while j < n and not (toks[j]["k"] == "kw" and toks[j]["v"] == "fn"):
                j += 1
            if j + 1 < n and toks[j + 1]["k"] == "ident" \
                    and toks[j + 1]["v"] == hint["short"]:
                kept.append((a, b))
        if not kept:
            raise _LocateError("attribute does not precede `fn %s`"
                               % hint["short"])
        src = ti.src
        edits = []
        for (a, b) in kept:
            start = ti.off(a)
            end = ti.end(b)
            start, end = _absorb_line(src, start, end)
            edits.append({"start": start, "end": end, "text": b"",
                          "rule": "L011" if hint["attr"] == "inline_always"
                          else "L012",
                          "line": hint["line"],
                          "desc": "delete attribute"})
        return edits

    def _stmt_start(self, ti, src, head, line):
        """Byte offset of a statement's first token — precisely when the
        statement kind has a recognisable head token, otherwise the
        line's first non-whitespace byte (the verify step catches any
        wrong guess)."""
        k = head.get("k")
        toks = ti.toks
        cands = []
        if k == "let":
            name = head.get("name")
            for i, t in enumerate(toks):
                if t["k"] == "kw" and t["v"] == "let" and t["line"] == line:
                    j = i + 1
                    if j < len(toks) and toks[j]["k"] == "kw" \
                            and toks[j]["v"] == "mut":
                        j += 1
                    if j < len(toks) and toks[j]["k"] == "ident" \
                            and toks[j]["v"] == name:
                        cands.append(i)
        elif k == "return":
            cands = [i for i, t in enumerate(toks)
                     if t["k"] == "kw" and t["v"] == "return"
                     and t["line"] == line]
        elif k in ("if", "while", "for"):
            cands = [i for i, t in enumerate(toks)
                     if t["k"] == "kw" and t["v"] == k
                     and t["line"] == line]
        elif k == "expr":
            e = head.get("e") or {}
            ek = e.get("k")
            if ek == "call":
                nm = e.get("name", "")
                cands = [i for i, t in enumerate(toks)
                         if t["k"] == "ident" and t["v"] == nm
                         and t["line"] == line
                         and i + 1 < len(toks)
                         and toks[i + 1]["k"] == "sym"
                         and toks[i + 1]["v"] == "("]
            elif ek == "method":
                nm = e.get("name", "")
                cands = [i for i, t in enumerate(toks)
                         if t["k"] == "ident" and t["v"] == nm
                         and t["line"] == line
                         and i > 0 and toks[i - 1]["k"] == "sym"
                         and toks[i - 1]["v"] == "."]
        if len(cands) == 1:
            return ti.off(cands[0])
        # Zero candidates (odd layout) or several (same-line repeats):
        # fall back to the line start and let the verifier judge.
        at = _first_nonws(src, _line_starts(src), line)
        if at is None:
            raise _LocateError("cannot locate statement on line %d" % line)
        return at

    # -- edits -> bytes ---------------------------------------------------

    def _resolve_overlaps(self, edits, skips):
        """Drop edits that overlap a kept one, longest span first.

        An L007 deletion subsumes any smaller rename/wrap inside the
        dead run (the dead code's warnings die with the code). Insert
        edits are zero-width: they only 'overlap' when strictly inside
        another span, never at a boundary."""
        out = []
        for e in sorted(edits, key=lambda e: (e["start"],
                                              -(e["end"] - e["start"]))):
            if any(e["start"] < k["end"] and k["start"] < e["end"]
                   for k in out):
                skips.append((e["rule"], e["line"],
                              "overlaps another fix (subsumed)"))
                continue
            out.append(e)
        return out

    def _apply(self, src, edits):
        """Splice the edits into the bytes, right to left."""
        out = src
        for e in sorted(edits, key=lambda e: e["start"], reverse=True):
            text = e["text"]
            if isinstance(text, str):
                text = text.encode("utf-8")
            out = out[:e["start"]] + text + out[e["end"]:]
        return out

    # -- verification -----------------------------------------------------

    def _verify(self, new_src, old_warnings, orig_status):
        """Would `new_src` be a strictly better file?

        Parse must hold; the crate must load and check no worse than
        the original (None-or-same-message policy — a DIFFERENT error
        is a new break, the same error is the pre-existing one, a
        clean fix of a formerly-broken file is the L007 rescue); at
        least one warning must be resolved; no unfixable rule may gain
        warnings; the total may not grow."""
        from collections import Counter
        try:
            new_warnings, _hints, new_status = self._lint(new_src)
        except HLError as ex:
            return False, "fixed source does not parse (%s)" % ex
        if new_status is not None:
            if orig_status is None:
                return False, "fix broke the crate check: %s" % new_status
            if new_status != orig_status:
                return False, "fix changed the crate error: %s" % new_status
        old_ms = Counter((r, m) for r, _s, _l, m in old_warnings)
        new_ms = Counter((r, m) for r, _s, _l, m in new_warnings)
        if not (old_ms - new_ms):
            return False, "no warning was resolved"
        for (r, m), n in new_ms.items():
            if r not in FIXABLE_RULES and n > old_ms.get((r, m), 0):
                return False, "fix would add new %s warnings" % r
        if len(new_warnings) > len(old_warnings):
            return False, "fix would add new warnings"
        return True, ""


def main():
    parser = argparse.ArgumentParser(
        prog="hllint",
        description="Halis linter (Stage 14).")
    parser.add_argument("file", nargs="?", help="HLS source file to lint.")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if any warnings are emitted.")
    parser.add_argument("--rule", action="append", dest="rules",
                        help="Only run the specified rule (e.g. --rule L001).")
    parser.add_argument("--list", action="store_true",
                        help="List all rules and exit.")
    parser.add_argument("--fix", action="store_true",
                        help="Apply the safe fixes and write the file "
                             "(Stage 118).")
    parser.add_argument("--diff", action="store_true",
                        help="Print the fixes --fix would make as a unified "
                             "diff; write nothing (Stage 118).")
    args = parser.parse_args()
    if args.list:
        print("Rules:")
        for rid, (name, severity) in RULES.items():
            mark = "  [fixable]" if rid in FIXABLE_RULES else ""
            print("  %s  %-25s  %s%s" % (rid, name, severity, mark))
        return 0
    if args.fix and args.diff:
        parser.error("--fix and --diff are mutually exclusive")
    if not args.file:
        parser.error("file is required (unless --list)")
    if not os.path.isfile(args.file):
        sys.stderr.write("error: file not found: %s\n" % args.file)
        return 1
    with open(args.file, "rb") as f:
        src = f.read()
    only = set(args.rules) if args.rules else None

    # ---- Stage 118: the autofix paths ----
    if args.fix or args.diff:
        fixer = Fixer(only_rules=only, path=args.file)
        try:
            new_src, report = fixer.fix_source(src)
        except HLError as ex:
            sys.stderr.write("error: %s\n" % ex)
            return 1
        if "error" in report:
            # Unparseable input: report it exactly like a plain lint run
            # would, and never write.
            sys.stderr.write("error: %s\n" % report["error"])
            return 1
        if args.diff and report["changed"]:
            import difflib
            a = src.decode("utf-8", "replace").splitlines(keepends=True)
            b = new_src.decode("utf-8", "replace").splitlines(keepends=True)
            for dline in difflib.unified_diff(
                    a, b, fromfile=args.file,
                    tofile=args.file + " (fixed)"):
                sys.stdout.write(dline if dline.endswith("\n")
                                 else dline + "\n")
            total = sum(report["fixed"].values())
            print("%s: %d warnings auto-fixable; run hllint --fix to apply"
                  % (args.file, total))
            return 1  # pending changes, git-diff --exit-code convention
        if args.fix and report["changed"]:
            with open(args.file, "wb") as f:
                f.write(new_src)
            total = sum(report["fixed"].values())
            parts = ", ".join("%s x%d" % (r, n)
                              for r, n in sorted(report["fixed"].items()))
            print("%s: fixed %d warnings in %d round(s) (%s)"
                  % (args.file, total, report["rounds"], parts))
        for rule, line, reason in report["skipped"]:
            print("%s: not auto-fixed [%s] line %s: %s"
                  % (args.file, rule, line, reason))
        # The final state — what a plain lint run would say now. The
        # plain (single-file) lint is the user-facing authority: the
        # fixer's merged view carries post-check node rewrites that
        # would make this summary diverge from `hllint FILE`.
        try:
            ftoks = tokenize(new_src)
            fprog = Parser(ftoks).parse_program()
            try:
                check(fprog)
            except HLError:
                pass
            flint = Linter(fprog, only_rules=only, path=args.file)
            final_warnings = flint.run()
        except HLError as ex:
            sys.stderr.write("error: %s\n" % ex)
            return 1
        if not final_warnings and not report["changed"] \
                and not report["skipped"]:
            print("%s: no warnings" % args.file)
            return 0
        for rid, severity, line, msg in final_warnings:
            print("%s:%d: %s [%s] %s" % (args.file, line, severity, rid, msg))
        if args.strict and final_warnings:
            return 1
        return 0

    # ---- the report-only path (unchanged since Stage 14) ----
    try:
        toks = tokenize(src)
        program = Parser(toks).parse_program()
        # Run the checker to populate type info (don't fail on errors).
        try:
            check(program)
        except HLError:
            pass  # Lint even if the program has type errors.
    except HLError as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 1
    linter = Linter(program, only_rules=only, path=args.file)
    warnings = linter.run()
    if not warnings:
        print("%s: no warnings" % args.file)
        return 0
    for rid, severity, line, msg in warnings:
        print("%s:%d: %s [%s] %s" % (args.file, line, severity, rid, msg))
    if args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
