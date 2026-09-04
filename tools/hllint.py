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

Usage:
  hllint FILE.hls              # print warnings to stdout, exit 0
  hllint --strict FILE.hls     # exit non-zero if any warnings
  hllint --rule L001 FILE.hls  # only run rule L001
  hllint --list                # list rules and exit

Status: alpha. The linter runs the Stage-0 checker to get the AST +
type/effect info, then walks the AST for each rule. The linter does
NOT modify the source — it only reports issues.
"""
import argparse
import os
import sys

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
    def __init__(self, program, only_rules=None, path=None):
        self.program = program
        self.warnings = []
        self.only_rules = only_rules or set(RULES.keys())
        # L010 scans the raw source (impl blocks are not in the AST).
        self.path = path

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

    def _warn(self, rule_id, line, msg):
        if rule_id in self.only_rules:
            self.warnings.append((rule_id, RULES[rule_id][1], line, msg))

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
                                   "let binding '%s' is never used" % name)

    def _rule_l002(self):
        """Unused-function: a function is never called."""
        called = set()
        for fname, fn in self.program["fns"].items():
            for e in all_exprs_in_stmts(fn["body"]):
                collect_calls(e, called)
        # BUG (deep-scan-5): struct field DEFAULT expressions can call
        # functions (e.g. `x: int = five()`) — a function called only
        # from a default was falsely reported as unused.
        for sname, sdef in self.program["structs"].items():
            for _fname, _ftype, dflt in sdef["fields"]:
                if dflt is not None:
                    collect_calls(dflt, called)
        # Special-case: `main` is always considered used.
        called.add("main")
        # Methods registered as "Struct.method" — the short name is
        # what appears in fieldcall/method nodes.
        for fname in self.program["fns"]:
            if fname == "main":
                continue
            short = fname.split(".")[-1]
            if fname not in called and short not in called:
                self._warn("L002", 0, "function '%s' is never called" % fname)

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
                                   "(use `?` to propagate, or `let _ = ...` to discard)" % callee)
                # Top-level method call (`x.method()`).
                elif e.get("k") == "method":
                    m = e.get("name", "")
                    rt = callee_rets.get(m, "")
                    if not _is_result_type(rt):
                        rt = e.get("t", "")
                    if _is_result_type(rt):
                        self._warn("L004", s.get("line", 0),
                                   "method call '%s' returns Result; result is ignored "
                                   "(use `?` to propagate)" % m)

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
        CHECK_PREFIXES = ("result_is_", "option_is_")

        def _walk_block(stmts, checked_vars):
            """Walk a flat statement list, mutating `checked_vars` (a set
            of variable names whose Result/Option value was recently
            checked). Returns a list of warnings to emit."""
            warns = []
            for s in stmts:
                k = s.get("k")
                if k == "let":
                    # If the let value is `if cond { ... } else { ... }` and
                    # the cond is `is_some(x)` / `is_ok(x)`, the let-bound
                    # variable is implicitly safe to unwrap.
                    val = s.get("value")
                    if val and val.get("k") == "call":
                        cn = val.get("name", "")
                        if cn in ("result_is_ok", "option_is_some"):
                            # The check argument might be `x` (ident).
                            if val.get("args"):
                                arg = val["args"][0]
                                if arg.get("k") == "ident":
                                    checked_vars.add(arg["name"])
                    # New let invalidates the binding-name check status.
                    # We add the let-bound name to the unchecked set
                    # implicitly (we don't pre-populate anything).
                elif k == "assign":
                    # An assignment to x clears x's checked status.
                    tgt = s.get("target")
                    if tgt and tgt.get("k") == "ident":
                        checked_vars.discard(tgt["name"])
                elif k == "if":
                    # If the condition is `is_some(x)` or `is_ok(x)`,
                    # mark x as checked WITHIN the then-branch.
                    cond = s.get("cond")
                    then_checked = set(checked_vars)
                    if cond and cond.get("k") == "call":
                        cn = cond.get("name", "")
                        if cn in ("result_is_ok", "option_is_some") and cond.get("args"):
                            arg = cond["args"][0]
                            if arg.get("k") == "ident":
                                then_checked.add(arg["name"])
                    warns.extend(_walk_block(s.get("then", []) or [], then_checked))
                    if s.get("els"):
                        # else-branch keeps the parent checked_vars.
                        warns.extend(_walk_block(s["els"] or [], set(checked_vars)))
                    continue
                elif k == "while":
                    # Inside a while, the checked status from outside
                    # doesn't apply (loop body may execute zero times).
                    warns.extend(_walk_block(s.get("body", []) or [], set()))
                    continue
                elif k == "for":
                    warns.extend(_walk_block(s.get("body", []) or [], set()))
                    continue
                # Look for unwrap calls in this statement's expressions.
                for e in exprs_in_stmt(s):
                    def visit(node):
                        if node.get("k") == "call" and node.get("name") in UNWRAP_NAMES:
                            args = node.get("args", [])
                            if not args:
                                return
                            arg = args[0]
                            # Only flag if the argument is an identifier
                            # that hasn't been recently checked.
                            if arg.get("k") == "ident":
                                if arg["name"] not in checked_vars:
                                    self._warn("L005", node.get("line", 0),
                                               "explicit unwrap of '%s' without prior "
                                               "is_ok/is_some check in this block"
                                               % arg["name"])
                    walk_expr(e, visit)
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
                           % (fname, ", ".join(sorted(declared))))

    def _rule_l007(self):
        """Dead-code-after-return: statements after `return` are unreachable."""
        for fname, fn in self.program["fns"].items():
            self._check_dead_after_return(fn["body"], fname)

    def _check_dead_after_return(self, stmts, fname):
        seen_return = False
        for s in stmts:
            if seen_return:
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
            with open(self.path, "rb") as f:
                src = f.read()
        except (OSError, AttributeError):
            return
        # Scan for `impl Ident ... { }` with an empty body.
        import re as _re
        for m in _re.finditer(rb"impl\s+[A-Za-z_][A-Za-z0-9_]*\s*(\[[^\]]*\])?\s*{\s*}", src):
            # Find the line of the match.
            line = src[:m.start()].count(b"\n") + 1
            name_m = _re.search(rb"impl\s+([A-Za-z_][A-Za-z0-9_]*)", m.group(0))
            nm = name_m.group(1).decode("utf-8", "replace") if name_m else "?"
            self._warn("L010", line, "impl block for '%s' is empty" % nm)


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
    args = parser.parse_args()
    if args.list:
        print("Rules:")
        for rid, (name, severity) in RULES.items():
            print("  %s  %-25s  %s" % (rid, name, severity))
        return 0
    if not args.file:
        parser.error("file is required (unless --list)")
    if not os.path.isfile(args.file):
        sys.stderr.write("error: file not found: %s\n" % args.file)
        return 1
    with open(args.file, "rb") as f:
        src = f.read()
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
    only = set(args.rules) if args.rules else None
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
