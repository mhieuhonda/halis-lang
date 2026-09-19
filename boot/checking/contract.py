"""Checker mixin (contract) - verbatim segment of the original
boot/checker.py Checker class (lines 1561..1739), split for
maintainability. The final Checker class assembles all mixins in
boot/checking/checker.py - behavior is unchanged."""
from .. import proof as _proof

class CheckerContract(object):
    def check_contracts(self, key, fn, env):
        """Validate `requires` / `ensures` expressions and, for functions
        with a requires clause, run the interval proof pass over the
        body (annotating provably-safe operations for -O fast)."""
        params = fn["params"]
        req = fn.get("requires")
        ens = fn.get("ensures")
        # Contract environment: ONLY the parameters (+ `result` for
        # ensures). Nothing else is in scope.
        cenv = [{}]
        for pn, pt, _ in params:
            cenv[0][pn] = [pt, False, False]
        if req is not None:
            t = self.check_expr(req, cenv, None)
            if t not in ("bool", "never"):
                self.err("requires clause of '%s' must be bool, got %s"
                         % (fn["name"], t), req)
            self.check_contract_purity(req, fn, "requires")
        if ens is not None:
            if fn["ret"] == "void":
                self.err("ensures clause on void function '%s' (there is "
                         "no result)" % fn["name"], ens)
            cenv[0]["result"] = [fn["ret"], False, False]
            t = self.check_expr(ens, cenv, None)
            if t not in ("bool", "never"):
                self.err("ensures clause of '%s' must be bool, got %s"
                         % (fn["name"], t), ens)
            self.check_contract_purity(ens, fn, "ensures")
    def run_proof_pass(self, key, fn):
        """Stage 17: seed interval facts from the (validated) requires
        clause and annotate the body's provably-safe operations. Runs
        AFTER the body was type-checked — the annotations read e['t']."""
        req = fn.get("requires")
        if req is None or fn.get("extern", False):
            return
        facts = _proof.seed_from_requires(req, fn["params"], 0)
        _proof.propagate_stmts(fn["body"], facts)
        for var, iv in facts.items():
            # Deep-scan-10: skip the engine's NUL-prefixed internal keys
            # (non-zero set, minimum-length map) — they are not variables.
            if not isinstance(var, str) or var.startswith("\x00"):
                continue
            if not isinstance(iv, _proof.Interval):
                continue
            self.proof_facts.setdefault(key, {})[var] = str(iv)

    def check_contract_purity(self, e, fn, which):
        """Contract expressions must be pure and side-effect-free: no
        calls to user functions or effectful builtins; only literals,
        params, arithmetic, comparisons, len() / .len() and field reads
        of params are allowed.

        Deep-scan-10 fix: the walk now descends into EVERY child shape
        (index targets/indices, field targets, match scrutinees and arm
        bodies, list literal items, struct/enum literal payloads, and
        the `?` operand). The old walk only recursed into l/r/e/args,
        so an impure call hidden under an index (`requires double(x)[0]
        > 0`) or inside a match arm compiled cleanly — a hole in SPEC
        §26.1's purity rule."""
        if not isinstance(e, dict):
            return
        k = e["k"]
        if k == "call":
            if e.get("rc") == ("builtin", "len"):
                for a in (e.get("args") or []):
                    self.check_contract_purity(a, fn, which)
                return
            name = e.get("name", "")
            self.err("contract expression calls '%s' — contracts must be "
                     "pure (only literals, parameters, arithmetic, "
                     "comparisons and len() are allowed)" % name, e)
        if k == "method":
            name = e.get("name", "")
            # Deep-scan-24 fix: only the ARG-LESS builtin `.len()` is a
            # pure contract construct. The old shortcut whitelisted ANY
            # method node named 'len' and skipped its args entirely, so a
            # user-defined `impl P { fn len(self: P, n: int) -> int }`
            # allowed `requires p.len(double(2)) > 0` to compile — the
            # call to `double` hidden in the args was never walked (the
            # effect checker cannot catch a pure user fn there, so the
            # contract silently called a user function, violating SPEC
            # §26.1). When args are present, fall through to the error:
            # the walk below still descends into the target so the user
            # gets the precise "calls method" diagnostic.
            if name == "len" and not (e.get("args") or []):
                self.check_contract_purity(e.get("target"), fn, which)
                return
            self.err("contract expression calls method '%s' — contracts "
                     "must be pure" % name, e)
        for sub in (e.get("l"), e.get("r"), e.get("e")):
            self.check_contract_purity(sub, fn, which)
        for a in (e.get("args") or []):
            self.check_contract_purity(a, fn, which)
        if k == "index":
            self.check_contract_purity(e.get("target"), fn, which)
            self.check_contract_purity(e.get("idx"), fn, which)
        if k == "field":
            self.check_contract_purity(e.get("target"), fn, which)
        if k == "match":
            self.check_contract_purity(e.get("scrut"), fn, which)
            for arm in (e.get("arms") or []):
                self.check_contract_purity(arm.get("body"), fn, which)
        if k == "qmark":
            self.check_contract_purity(e.get("e"), fn, which)
        if k in ("listlit", "structlit", "enumlit"):
            for a in (e.get("items") or e.get("args") or []):
                self.check_contract_purity(a, fn, which)
            for _, fe in (e.get("fields") or []):
                self.check_contract_purity(fe, fn, which)

    def all_return(self, stmts):
        if not stmts:
            return False
        last = stmts[-1]
        if last["k"] == "return":
            return True
        if last["k"] == "expr" and last["e"].get("t") == "never":
            return True
        # BUG-SC-5 fix: a `let` or `assign` whose RHS is `never`-typed
        # (e.g. `let x: int = panic("...")` or `x = exit(0)`) always
        # diverges, so the function returns on all paths. Previously
        # this was rejected with a spurious "does not return on all
        # paths" error.
        if last["k"] == "let" and last["value"].get("t") == "never":
            return True
        if last["k"] == "assign" and last["value"].get("t") == "never":
            return True
        if last["k"] == "if" and last["els"] is not None:
            return self.all_return(last["then"]) and self.all_return(last["els"])
        if last["k"] == "expr" and last["e"]["k"] == "match" and \
           self.match_all_return(last["e"]):
            return True
        return False

    def path_terminates(self, stmts, in_loop):
        """Return True if executing `stmts` always exits the current path
        via `return` (in any context) or `break`/`continue` (in a loop
        context) or a `never`-typed expression.

        Used by the `if` move-state union logic to skip moved-state
        contributions from arms that always exit (their moves cannot
        reach the post-if code). This is the move-state analogue of
        `all_return`, extended to also cover `break`/`continue` (which
        terminate the current path the same way `return` does for the
        purpose of post-if reachability).
        """
        if not stmts:
            return False
        last = stmts[-1]
        if last["k"] == "return":
            return True
        if in_loop and last["k"] in ("break", "continue"):
            return True
        if last["k"] == "expr" and last["e"].get("t") == "never":
            return True
        if last["k"] == "let" and last["value"].get("t") == "never":
            return True
        if last["k"] == "assign" and last["value"].get("t") == "never":
            return True
        if last["k"] == "if" and last["els"] is not None:
            return (self.path_terminates(last["then"], in_loop) and
                    self.path_terminates(last["els"], in_loop))
        if last["k"] == "expr" and last["e"]["k"] == "match" and \
           self.match_all_return(last["e"]):
            return True
        return False

    def match_all_return(self, e):
        # BUG-008 fix: arm["body"] is the result of parse_expr (parser.py:540)
        # — it's always an EXPRESSION node (call, match, bin, enumlit, …),
        # never a statement node ("return" or "expr"). The original check
        # therefore always returned False. The correct check is: every arm
        # must be `never`-typed (i.e. its body is something that doesn't
        # fall through, like panic() or exit()).
        for arm in e["arms"]:
            if arm["body"].get("t") != "never":
                return False
        return True

