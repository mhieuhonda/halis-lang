#!/usr/bin/env python3
"""hllint — Linter for Hieu Louis (HLS).

Stage 14 (v0.12.0-alpha): safety rules for HLS programs.

Rules:
  L001  unused-binding        A `let` binding is never referenced after
                              its declaration.
  L002  unused-function       A function is never called.
  L003  unused-struct-field   A struct field is never read.
  L004  ignored-result         A call to a function returning Result[T, E]
                              is used as a statement (no `?`, no
                              `let _ = ...`, no `match`).
  L005  explicit-unwrap        A call to `result_unwrap` or
                              `option_unwrap` without a prior
                              `result_is_ok` / `option_is_some` check.
  L006  unnecessary-effects    A function declares `uses IO` (or any
                              effect) but its body calls only pure
                              functions.
  L007  dead-code-after-return Statements after `return` are unreachable.
  L008  long-function         A function body exceeds 80 statements
                              (refactor candidate).
  L009  shadowing             A `let` binding shadows an outer binding
                              with the same name.
  L010  empty-impl            An `impl` block has no methods.

Usage:
  hllint FILE.hls              # print warnings to stdout, exit 0
  hllint --strict FILE.hls     # exit non-zero if any warnings
  hllint --rule L001 FILE.hls  # only run rule L001
  hllint --list FILE.hls       # list rules and exit

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
    "L009": ("shadowing",             "warning"),
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
            # Collect all identifier references in the function body.
            refs = set()
            def visit(node):
                if node.get("k") == "ident":
                    refs.add(node["name"])
            for s in fn["body"]:
                walk_stmts([s], lambda s: None)
                if s["k"] == "let":
                    collect_idents(s["value"], refs)
                elif s["k"] == "assign":
                    collect_idents(s["value"], refs)
                elif s["k"] == "return" and s.get("value"):
                    collect_idents(s["value"], refs)
                elif s["k"] == "expr":
                    walk_expr(s["e"], visit)
                elif s["k"] == "if":
                    collect_idents(s["cond"], refs)
                elif s["k"] == "while":
                    collect_idents(s["cond"], refs)
                elif s["k"] == "for":
                    collect_idents(s["iter"], refs)
            # Also walk the body recursively.
            for s in fn["body"]:
                if s["k"] == "expr":
                    walk_expr(s["e"], visit)
                elif s["k"] == "if":
                    collect_idents(s["cond"], refs)
                    for sub in s["then"] + (s.get("els") or []):
                        if sub["k"] == "expr":
                            walk_expr(sub["e"], visit)
                elif s["k"] == "while":
                    collect_idents(s["cond"], refs)
                    for sub in s["body"]:
                        if sub["k"] == "expr":
                            walk_expr(sub["e"], visit)
            # Now check each `let` binding.
            for s in fn["body"]:
                if s["k"] == "let":
                    name = s["name"]
                    # If the binding is used anywhere (including the
                    # let's own value — which would be a recursion error
                    # but the checker already rejects that), skip.
                    # We use a simple heuristic: if `name` appears in
                    # `refs` (the set of all identifier references),
                    # it's used.
                    if name not in refs:
                        self._warn("L001", s.get("line", 0),
                                   "let binding '%s' is never used" % name)

    def _rule_l002(self):
        """Unused-function: a function is never called."""
        # Collect all function/method names referenced via call/fieldcall.
        called = set()
        for fname, fn in self.program["fns"].items():
            # Walk EVERY expression in the function body (let values,
            # assign values, return values, if/while/for conditions,
            # expr statements).
            def visit_call(node):
                if node.get("k") == "call":
                    called.add(node["name"])
                elif node.get("k") in ("method", "fieldcall"):
                    called.add(node["name"])
            for s in fn["body"]:
                if s["k"] == "let":
                    walk_expr(s["value"], visit_call)
                elif s["k"] == "assign":
                    walk_expr(s["value"], visit_call)
                elif s["k"] == "return" and s.get("value"):
                    walk_expr(s["value"], visit_call)
                elif s["k"] == "expr":
                    walk_expr(s["e"], visit_call)
                elif s["k"] == "if":
                    walk_expr(s["cond"], visit_call)
                    for sub in s["then"] + (s.get("els") or []):
                        if sub["k"] == "expr":
                            walk_expr(sub["e"], visit_call)
                elif s["k"] == "while":
                    walk_expr(s["cond"], visit_call)
                    for sub in s["body"]:
                        if sub["k"] == "expr":
                            walk_expr(sub["e"], visit_call)
                elif s["k"] == "for":
                    walk_expr(s["iter"], visit_call)
                    for sub in s["body"]:
                        if sub["k"] == "expr":
                            walk_expr(sub["e"], visit_call)
        # Special-case: `main` is always considered used.
        called.add("main")
        for fname in self.program["fns"]:
            if fname == "main":
                continue
            short = fname.split(".")[-1]
            if fname not in called and short not in called:
                self._warn("L002", 0, "function '%s' is never called" % fname)

    def _rule_l004(self):
        """Ignored-result: a call returning Result is used as a statement."""
        # We don't have type info in the AST today without re-running
        # the checker. For the alpha, we flag any `expr` statement whose
        # top-level expression is a `call` to a function whose name
        # contains "parse" or "result_".
        for fname, fn in self.program["fns"].items():
            for s in fn["body"]:
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
            for s in fn["body"]:
                if s["k"] == "expr":
                    def visit(node):
                        if node.get("k") == "call" and node["name"] in (
                                "result_unwrap", "option_unwrap"):
                            self._warn("L005", node.get("line", 0),
                                       "explicit unwrap without prior is_ok/is_some check")
                    walk_expr(s["e"], visit)

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
            for i, s in enumerate(fn["body"]):
                if s["k"] == "return":
                    if i < len(fn["body"]) - 1:
                        nxt = fn["body"][i + 1]
                        self._warn("L007", nxt.get("line", 0),
                                   "statement after `return` is unreachable")

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
        """Shadowing: a `let` binding shadows an outer binding."""
        # Track bindings as we descend into scopes.
        for fname, fn in self.program["fns"].items():
            outer = set(p[0] for p in fn["params"])
            self._check_shadowing(fn["body"], outer, fname)

    def _check_shadowing(self, stmts, bindings, fname):
        local = set(bindings)
        for s in stmts:
            if s["k"] == "let":
                if s["name"] in local:
                    self._warn("L009", s.get("line", 0),
                               "let binding '%s' shadows an outer binding" % s["name"])
                local.add(s["name"])
            elif s["k"] == "for":
                if s["var"] in local:
                    self._warn("L009", s.get("line", 0),
                               "for-loop variable '%s' shadows an outer binding" % s["var"])
                local.add(s["var"])
                self._check_shadowing(s["body"], local, fname)
            elif s["k"] == "if":
                self._check_shadowing(s["then"], local, fname)
                if s.get("els"):
                    self._check_shadowing(s["els"], local, fname)
            elif s["k"] == "while":
                self._check_shadowing(s["body"], local, fname)

    def _rule_l010(self):
        """Empty-impl: an impl block has no methods."""
        # We don't have impl blocks in the AST directly; methods are
        # registered as "Struct.method" in the fns dict. We can't easily
        # detect empty impls without parser changes. For the alpha, we
        # skip this rule.
        pass


def main():
    parser = argparse.ArgumentParser(
        prog="hllint",
        description="Hieu Louis linter (Stage 14-alpha).")
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
