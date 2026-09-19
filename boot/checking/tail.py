"""Checker mixin (tail) - verbatim segment of the original
boot/checker.py Checker class (lines 1358..1560), split for
maintainability. The final Checker class assembles all mixins in
boot/checking/checker.py - behavior is unchanged."""

class CheckerTail(object):
    # verified tail `return f(...)` raises TailCallSig, and call_fn
    # rebinds the parameters and re-runs the body in the SAME Python
    # frame — fib_tail(1_000_000) needs no Python recursion either.

    TAIL_PRIM_TYPES = ("int", "float", "bool")

    def _tail_is_self_call(self, e, key):
        """Is expression node e a direct call to fn `key`?"""
        if not isinstance(e, dict) or e.get("k") != "call":
            return False
        rc = e.get("rc")
        return rc is not None and rc[0] == "user" and rc[1] == key

    def _tail_check_expr(self, e, key):
        """Verify one expression subtree of a #[tail_call] fn."""
        if e is None or not isinstance(e, dict):
            return
        k = e.get("k")
        if k in ("int", "float", "bool", "ident"):
            return
        if k == "str":
            self.err("#[tail_call] violated: '%s' cannot use string values "
                     "(line %d) — only int / float / bool may flow through "
                     "a tail-call function (a string would need release at "
                     "the loop jump; string LITERALS are only allowed as "
                     "the direct argument of panic / print / println)"
                     % (key, e.get("line", 0)), e)
            return
        if k == "call":
            # A self-call outside a return-value position is never tail.
            rc = e.get("rc")
            if rc is not None and rc[0] == "user" and rc[1] == key:
                self.err("#[tail_call] violated: the recursive call to "
                         "'%s' at line %d is not in tail position — every "
                         "recursive call must be the entire return "
                         "expression (`return %s(...)`)"
                         % (key, e.get("line", 0), key), e)
                return
            # Consume-builtin exception: panic / print / println /
            # eprint / eprintln with ONE string literal argument
            # (Stage 56, v0.75.0-alpha added the stderr pair — they
            # consume their argument exactly like print / println).
            if rc is not None and rc[0] == "builtin" and \
                    rc[1] in ("panic", "print", "println", "eprint", "eprintln"):
                args = e.get("args", [])
                if len(args) != 1 or args[0].get("k") != "str":
                    self.err("#[tail_call] violated: the %s call at line %d "
                             "must take exactly one string LITERAL — a "
                             "computed string would create a refcounted "
                             "temporary needing cleanup between the tail "
                             "call and the jump"
                             % (rc[1], e.get("line", 0)), e)
                return
            # Any other call: the RESULT must be primitive (void/never
            # allowed for statement position), then walk the arguments.
            t = e.get("t")
            if t not in ("void", "never") and t not in self.TAIL_PRIM_TYPES:
                self.err("#[tail_call] violated: '%s' contains a non-"
                         "primitive expression of type '%s' (line %d) — "
                         "only int / float / bool values may flow through "
                         "a tail-call function (refcounted values would "
                         "need cleanup between the tail call and the jump)"
                         % (key, t, e.get("line", 0)), e)
                return
            for a in e.get("args", []):
                self._tail_check_expr(a, key)
            return
        if k == "method":
            t = e.get("t")
            if t not in ("void", "never") and t not in self.TAIL_PRIM_TYPES:
                self.err("#[tail_call] violated: '%s' contains a non-"
                         "primitive expression of type '%s' (line %d) — "
                         "only int / float / bool values may flow through "
                         "a tail-call function" % (key, t, e.get("line", 0)), e)
                return
            self._tail_check_expr(e.get("target"), key)
            for a in e.get("args", []):
                self._tail_check_expr(a, key)
            return
        # Every other node kind: the node's own type must be primitive.
        t = e.get("t")
        if t not in ("void", "never") and t not in self.TAIL_PRIM_TYPES:
            self.err("#[tail_call] violated: '%s' contains a non-primitive "
                     "expression of type '%s' (line %d) — only int / "
                     "float / bool values may flow through a tail-call "
                     "function (refcounted values would need cleanup "
                     "between the tail call and the jump)"
                     % (key, t, e.get("line", 0)), e)
            return
        # Walk children (bin: l/r, un/qmark: e, field/fieldcall/index:
        # target/idx, match: scrut + arm bodies, listlit: items,
        # structlit: fields, enumlit: args).
        for ck in ("e", "l", "r", "scrut", "target", "idx"):
            child = e.get(ck)
            if isinstance(child, dict) and "k" in child:
                self._tail_check_expr(child, key)
        if k == "match":
            for arm in e.get("arms", []):
                self._tail_check_expr(arm.get("body"), key)
        elif k == "listlit":
            for it in e.get("items", []):
                self._tail_check_expr(it, key)
        elif k == "structlit":
            for fname, fexpr in e.get("fields", []):
                self._tail_check_expr(fexpr, key)
        for a in e.get("args", []):
            if isinstance(a, dict) and "k" in a:
                self._tail_check_expr(a, key)

    def _tail_check_stmts(self, stmts, key):
        """Walk the body. Returns the number of verified tail sites."""
        sites = 0
        for s in stmts:
            k = s["k"]
            if k == "return":
                v = s.get("value")
                if v is not None:
                    if self._tail_is_self_call(v, key):
                        # The verified tail site. Its ARGUMENTS still go
                        # through the primitive-dataflow check.
                        for a in v.get("args", []):
                            self._tail_check_expr(a, key)
                        sites += 1
                    else:
                        self._tail_check_expr(v, key)
            elif k == "let":
                if s["t"] not in self.TAIL_PRIM_TYPES:
                    self.err("#[tail_call] violated: '%s' cannot bind non-"
                             "primitive values — `let %s: %s` at line %d. "
                             "A refcounted binding would need release at "
                             "the loop jump, which the verified transform "
                             "forbids (keep the body to int / float / bool)"
                             % (key, s["name"], s["t"], s.get("line", 0)), s)
                self._tail_check_expr(s.get("value"), key)
            elif k == "assign":
                self._tail_check_expr(s.get("target"), key)
                self._tail_check_expr(s.get("value"), key)
            elif k == "if":
                self._tail_check_expr(s.get("cond"), key)
                sites += self._tail_check_stmts(s.get("then") or [], key)
                if s.get("els"):
                    sites += self._tail_check_stmts(s["els"], key)
            elif k == "while":
                self._tail_check_expr(s.get("cond"), key)
                sites += self._tail_check_stmts(s.get("body") or [], key)
            elif k == "for":
                # for-in iterates a list — never primitive. The iter
                # expression check reports it.
                self._tail_check_expr(s.get("iter"), key)
                sites += self._tail_check_stmts(s.get("body") or [], key)
            elif k == "expr":
                self._tail_check_expr(s.get("e"), key)
            elif k == "asm":
                # Deep-scan-28 fix: asm! statements (and their operand
                # expressions) escaped the #[tail_call] verifier entirely
                # — a recursive self-call hidden inside an asm operand
                # compiled to a genuine native recursive call, silently
                # breaking the attribute's stack-safety contract (a
                # `let x = loopn(1)` in the same position IS rejected).
                # Walk every operand expression: non-primitive values are
                # rejected by _tail_check_expr, and a self-call in an
                # operand is rejected as "not in tail position".
                for op in s.get("operands") or []:
                    self._tail_check_expr(op.get("expr"), key)
            # break / continue: no expressions.
        return sites

    def tail_call_check_fn(self, key, fn):
        """The Stage 31 entry point — verify one #[tail_call] fn."""
        attrs = fn.get("attrs", {})
        if not attrs.get("tail_call", False):
            return
        if fn.get("extern", False):
            self.err("#[tail_call] cannot be applied to an extern function", fn)
            return
        if fn["struct"] is not None:
            self.err("#[tail_call] is not supported on methods yet ('%s' is "
                     "a method of '%s')" % (fn["name"], fn["struct"]), fn)
            return
        if fn.get("typeparams"):
            self.err("#[tail_call] is not supported on generic functions yet "
                     "('%s' has type parameters) — instantiate a concrete "
                     "wrapper instead" % fn["name"], fn)
            return
        if fn.get("requires") is not None or fn.get("ensures") is not None:
            self.err("#[tail_call] and contracts (requires / ensures) are "
                     "mutually exclusive on '%s' — the postcondition check "
                     "would interpose cleanup between the tail call and "
                     "the jump" % fn["name"], fn)
            return
        # Parameters: primitive only. The transform rebinds them in
        # place; a refcounted parameter would need release at the jump.
        for pn, pt, _ in fn["params"]:
            if pt not in self.TAIL_PRIM_TYPES:
                self.err("#[tail_call] on '%s' requires all parameters to "
                         "be int / float / bool; parameter '%s: %s' is not "
                         "— refcounted values would need cleanup between "
                         "the tail call and the jump, which the verified "
                         "transform forbids" % (fn["name"], pn, pt), fn)
        # Return type: the loop feeds a value back; void has nothing.
        if fn["ret"] not in self.TAIL_PRIM_TYPES:
            self.err("#[tail_call] on '%s' requires the return type to be "
                     "int / float / bool, not '%s' (a tail call must feed "
                     "a value back through the loop)" % (fn["name"], fn["ret"]), fn)
        # Body: position + dataflow verification.
        sites = self._tail_check_stmts(fn["body"], key)
        if sites == 0:
            self.err("#[tail_call] on '%s' found no recursive call to "
                     "itself — remove the attribute (the tail-call "
                     "transform needs a self-call to turn into a loop)"
                     % fn["name"], fn)
            return
        self.tail_sites[key] = sites

    # ---------- Stage 17: contracts ----------
