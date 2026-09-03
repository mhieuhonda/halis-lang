#!/usr/bin/env python3
"""hllint — Linter for Hieu Louis (HLS).

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
    """Collect all identifier references in an expression."""
    def visit(node):
        if node.get("k") == "ident":
            idents.add(node["name"])
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
    def __init__(self, program, only_rules=None):
        self.program = program
        self.warnings = []
        self.only_rules = only_rules or set(RULES.keys())

    def run(self):
        for rule_id in self.only_rules:
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
                    if name not in refs:
                        self._warn("L001", s.get("line", 0),
                                   "let binding '%s' is never used" % name)

    def _rule_l002(self):
        """Unused-function: a function is never called."""
        called = set()
        for fname, fn in self.program["fns"].items():
            for e in all_exprs_in_stmts(fn["body"]):
                collect_calls(e, called)
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
        """Ignored-result: a call returning Result is used as a statement."""
        # We don't have type info in the AST today without re-running
        # the checker. For the alpha, we flag any `expr` statement whose
        # top-level expression is a `call` to a function whose name
        # contains "parse" or "result_".
        for fname, fn in self.program["fns"].items():
            for s in walk_stmts_collected(fn["body"]):
                if s["k"] == "expr" and s["e"]["k"] == "call":
                    callee = s["e"]["name"]
                    if "parse" in callee.lower() or callee.startswith("result_"):
                        self._warn("L004", s.get("line", 0),
                                   "call to '%s' returns Result; result is ignored "
                                   "(use `?` to propagate, or `let _ = ...` to discard)" % callee)

    def _rule_l005(self):
        """Explicit-unwrap without prior is_some/is_ok check."""
        # For the alpha, we flag ANY call to `result_unwrap` / `option_unwrap`.
        # A more sophisticated linter would track control flow.
        for fname, fn in self.program["fns"].items():
            for e in all_exprs_in_stmts(fn["body"]):
                def visit(node):
                    if node.get("k") == "call" and node["name"] in (
                            "result_unwrap", "option_unwrap"):
                        self._warn("L005", node.get("line", 0),
                                   "explicit unwrap without prior is_ok/is_some check")
                walk_expr(e, visit)

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

        NOTE: the parser already rejects empty impl blocks (they require
        at least one `fn`), so this rule never fires for valid programs.
        Kept for documentation.
        """
        # No-op: parser rejects empty impls.
        pass


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
        description="Hieu Louis linter (Stage 14).")
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
    linter = Linter(program, only_rules=only)
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
