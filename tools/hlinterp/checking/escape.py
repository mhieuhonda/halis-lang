"""Checker mixin (escape) - verbatim segment of the original
boot/checker.py Checker class (lines 1046..1357), split for
maintainability. The final Checker class assembles all mixins in
boot/checking/checker.py - behavior is unchanged."""
from .helpers import (
    is_list, list_elem,
)

class CheckerEscape(object):

    # Borrow-safe builtin list methods (receiver only).
    ESC_SAFE_LIST_METHODS = ("list.get", "list.set", "list.len")
    ESC_PRIM_ELEMS = ("int", "float", "bool")

    def escape_check_fn(self, key, fn):
        """Run the Stage 30 escape analysis over one function body."""
        if fn.get("extern", False):
            return
        # Generic functions (and methods of generic structs) are
        # instantiated per type-argument set; their codegen runs under
        # mangled keys, so layout decisions taken on the source key would
        # not apply. Skip the automatic analysis; #[stack] there is a
        # clear, documented error rather than a silent no-op.
        generic = bool(fn.get("typeparams"))
        if fn["struct"] is not None and fn["struct"] in self.structs:
            if self.structs[fn["struct"]].get("typeparams"):
                generic = True
        if generic:
            self._escape_check_generic_attrs(fn)
            return
        # Phase 1 — collect candidate bindings + validate forced attrs.
        cand = {}  # name -> decision record
        self._esc_collect_stmts(fn["body"], cand)
        # Phase 2 — classify every use of every candidate.
        self._esc_walk_stmts(fn["body"], cand)
        # Phase 3 — decide layouts + report #[stack] violations.
        for name in cand:  # dict preserves discovery order (deterministic)
            c = cand[name]
            if c["forced_stack"]:
                if c["esc"]:
                    self.err("#[stack] violated: '%s' escapes its creating "
                             "frame — %s (line %d). A stack-allocated value "
                             "can NEVER outlive the function that created "
                             "it: remove the escaping use or drop the "
                             "attribute." % (name, c["esc"], c["esc_line"]),
                             {"line": c["line"]})
                if c["push"]:
                    self.err("#[stack] violated: '%s' uses push() (line %d) — "
                             "a stack-allocated list is fixed-capacity "
                             "(capacity %d, taken from its literal). Use "
                             ".get/.set within the capacity or drop the "
                             "attribute." % (name, c["push_line"], c["cap"]),
                             {"line": c["line"]})
                if c["pop"]:
                    self.err("#[stack] violated: '%s' uses pop() (line %d) — "
                             "a stack-allocated list is fixed-capacity; its "
                             "length is part of the layout and cannot "
                             "shrink. Drop the attribute to use pop()."
                             % (name, c["pop_line"]), {"line": c["line"]})
            # Final layout (consumed by the codegen side of the mirror
            # contract; the interpreter itself is layout-agnostic).
            if c["boxed"]:
                c["layout"] = "boxed"
            elif (not c["esc"]) and (not c["push"]) and (not c["pop"]):
                c["layout"] = "stack"
            else:
                c["layout"] = "heap"

    def _escape_check_generic_attrs(self, fn):
        """Reject #[stack]/#[boxed] inside generic functions / methods
        with a precise message (the automatic analysis is simply skipped
        there — layout is instantiation-dependent)."""
        def visit(stmts):
            for s in stmts:
                if s["k"] == "let" and (s.get("stack") or s.get("boxed")):
                    attr = "stack" if s.get("stack") else "boxed"
                    self.err("#[%s] is not supported inside generic "
                             "functions yet (the layout would depend on "
                             "the type instantiation); drop the attribute "
                             "here" % attr, s)
                # Deep-scan-20 fix (HIGH, crash): `if` statements were
                # visited with s["body"] — the parser emits "then"/"els"
                # for ifs, so ANY generic function containing an `if`
                # crashed the checker with KeyError('body'). Split the
                # cases and also walk match arms (arms carry bodies).
                elif s["k"] == "if":
                    visit(s["then"])
                    if s.get("els"):
                        visit(s["els"])
                elif s["k"] == "while":
                    visit(s["body"])
                elif s["k"] == "for":
                    visit(s["body"])
                elif s["k"] == "match":
                    for arm in (s.get("arms") or []):
                        visit(arm.get("body") or [])
        visit(fn["body"])

    def _esc_candidate(self, s):
        """Is this let-stmt a stack-layout candidate? Requires a
        list[primitive] binding initialised from a NON-EMPTY list
        literal (the capacity is fixed at compile time)."""
        t = s["t"]
        if not is_list(t):
            return False
        if list_elem(t) not in self.ESC_PRIM_ELEMS:
            return False
        v = s["value"]
        if v.get("k") != "listlit":
            return False
        return len(v["items"]) > 0

    def _esc_collect_stmts(self, stmts, cand):
        for s in stmts:
            k = s["k"]
            if k == "let":
                forced_stack = bool(s.get("stack"))
                boxed = bool(s.get("boxed"))
                if forced_stack or boxed:
                    attr = "stack" if forced_stack else "boxed"
                    t = s["t"]
                    if not is_list(t):
                        self.err("#[%s] applies to a list[T] binding; '%s' "
                                 "has type '%s'" % (attr, s["name"], t), s)
                    if forced_stack:
                        if list_elem(t) not in self.ESC_PRIM_ELEMS:
                            self.err("#[stack] requires a primitive element "
                                     "type (int / float / bool); '%s' is "
                                     "'%s' whose elements are reference-"
                                     "counted (boxed) values — a documented "
                                     "Stage 30 limitation" % (s["name"], t), s)
                        v = s["value"]
                        if v.get("k") != "listlit":
                            self.err("#[stack] requires a list-literal "
                                     "initializer (the capacity is fixed at "
                                     "compile time); '%s' is initialised "
                                     "from a call/expression" % s["name"], s)
                        elif len(v["items"]) == 0:
                            self.err("#[stack] on an empty list literal has "
                                     "no capacity — every .get() would "
                                     "panic; use a non-empty literal or "
                                     "drop the attribute", s)
                if self._esc_candidate(s) and not boxed:
                    v = s["value"]
                    cand[s["name"]] = {
                        "line": s["line"], "cap": len(v["items"]),
                        "forced_stack": forced_stack, "boxed": boxed,
                        "esc": None, "esc_line": 0,
                        "push": False, "push_line": 0,
                        "pop": False, "pop_line": 0,
                        "layout": "heap",
                    }
            elif k == "if":
                self._esc_collect_stmts(s["then"], cand)
                if s.get("els"):
                    self._esc_collect_stmts(s["els"], cand)
            elif k == "while":
                self._esc_collect_stmts(s["body"], cand)
            elif k == "for":
                self._esc_collect_stmts(s["body"], cand)

    def _esc_walk_stmts(self, stmts, cand):
        for s in stmts:
            k = s["k"]
            if k == "let":
                v = s["value"]
                if v.get("k") == "listlit":
                    # Element expressions of ANY list literal (including a
                    # candidate's own literal — its elements are primitive
                    # expressions and cannot alias a tracked binding, but
                    # walk them anyway for uniformity).
                    for it in v["items"]:
                        self._esc_expr(it, cand, False,
                                       "element of a list literal")
                else:
                    self._esc_expr(v, cand, False,
                                   "initializer of '%s'" % s["name"])
            elif k == "assign":
                tgt = s["target"]
                if tgt["k"] == "ident":
                    if tgt["name"] in cand:
                        c = cand[tgt["name"]]
                        if not c["esc"]:
                            c["esc"] = "reassigned (assignment target)"
                            c["esc_line"] = tgt.get("line", 0)
                elif tgt["k"] == "index" and tgt["target"].get("k") == "ident":
                    # xs[i] = v — the base binding is the index base (a
                    # borrow-safe position); only the index expr escapes.
                    self._esc_expr(tgt["idx"], cand, False,
                                   "index expression")
                else:
                    self._esc_expr(tgt, cand, False, "assignment target")
                self._esc_expr(s["value"], cand, False, "assigned value")
            elif k == "if":
                self._esc_expr(s["cond"], cand, False, "if condition")
                self._esc_walk_stmts(s["then"], cand)
                if s.get("els"):
                    self._esc_walk_stmts(s["els"], cand)
            elif k == "while":
                self._esc_expr(s["cond"], cand, False, "while condition")
                self._esc_walk_stmts(s["body"], cand)
            elif k == "for":
                it = s["iter"]
                if it.get("k") == "ident" and it["name"] in cand:
                    pass  # for-in iterable: borrow-safe position
                else:
                    self._esc_expr(it, cand, False, "for-in iterable")
                self._esc_walk_stmts(s["body"], cand)
            elif k == "return":
                if s["value"] is not None:
                    self._esc_expr(s["value"], cand, False, "return value")
            elif k == "expr":
                self._esc_expr(s["e"], cand, False,
                               "discarded expression value")

    def _esc_expr(self, e, cand, safe, why):
        """Walk an expression, classifying every occurrence of a tracked
        binding. `safe=True` means the CURRENT position is borrow-safe
        (receiver of .get/.set/.len, index base); method/index nodes
        re-derive the flag for their own children, so the ambient flag
        only matters when an ident node is reached directly."""
        if e is None or not isinstance(e, dict):
            return
        k = e.get("k")
        if k == "ident":
            name = e["name"]
            if name in cand:
                c = cand[name]
                if not safe and not c["esc"]:
                    c["esc"] = why
                    c["esc_line"] = e.get("line", 0)
            return
        if k == "method":
            rm = e.get("rm")
            mname = e.get("name", "?")
            tgt = e["target"]
            if rm and rm[0] == "builtin" and rm[1] in self.ESC_SAFE_LIST_METHODS:
                # .get/.set/.len receiver is borrow-safe.
                self._esc_expr(tgt, cand, True, "receiver")
            elif rm and rm[1] in ("list.push", "list.pop") and \
                    tgt.get("k") == "ident" and tgt["name"] in cand:
                # push/pop on a tracked binding: record the specific
                # growth/shrink use (a dedicated error class — fixed
                # capacity).
                c = cand[tgt["name"]]
                if rm[1] == "list.push" and not c["push"]:
                    c["push"] = True
                    c["push_line"] = tgt.get("line", 0)
                elif rm[1] == "list.pop" and not c["pop"]:
                    c["pop"] = True
                    c["pop_line"] = tgt.get("line", 0)
            else:
                self._esc_expr(tgt, cand, False,
                               "receiver of '%s()'" % mname)
            for a in e.get("args", []):
                self._esc_expr(a, cand, False,
                               "argument of '%s()'" % mname)
            return
        if k == "index":
            self._esc_expr(e["target"], cand, True, "index base")
            self._esc_expr(e["idx"], cand, False, "index expression")
            return
        if k == "call":
            # Deep-scan-20 fix (LOW, consistency): `len(xs)` is the call
            # spelling of the borrow-safe `xs.len()` method — the method
            # branch whitelists the receiver, but the call branch walked
            # all args with safe=False, so the identical operation forced
            # a #[stack] / auto candidate to the heap layout purely by
            # spelling. Treat the builtin len() receiver as borrow-safe.
            if e.get("rc") == ("builtin", "len") and (e.get("args") or []):
                self._esc_expr(e["args"][0], cand, True, "len() argument")
                for a in e["args"][1:]:
                    self._esc_expr(a, cand, False,
                                   "argument of call 'len()'")
                return
            for a in e.get("args", []):
                self._esc_expr(a, cand, False,
                               "argument of call '%s()'" % e.get("name", "?"))
            return
        # Generic children (bin: l/r, un/qmark: e, field/fieldcall:
        # target, match: scrut + arm bodies, structlit: fields).
        for ck in ("e", "l", "r", "scrut", "target"):
            child = e.get(ck)
            if isinstance(child, dict) and "k" in child:
                self._esc_expr(child, cand, False, why)
        if k == "match":
            for arm in e.get("arms", []):
                self._esc_expr(arm["body"], cand, False, "match arm body")
        elif k == "listlit":
            for it in e.get("items", []):
                self._esc_expr(it, cand, False, "element of a list literal")
        elif k == "structlit":
            for fname, fexpr in e.get("fields", []):
                self._esc_expr(fexpr, cand, False,
                               "field '%s' of a struct literal" % fname)
        for a in e.get("args", []):
            if isinstance(a, dict) and "k" in a:
                self._esc_expr(a, cand, False, why)

    # ---------- Stage 31 (v0.48.0-alpha): verified tail calls ----------
    # #[tail_call] on a function asserts that every recursive call to it
    # is in VERIFIED tail position. Mirrors the hlc.hls checker exactly
    # (same rules, same error messages) so `boot.py --check` rejects
    # exactly the same programs the self-hosted compiler rejects:
    #
    #   1. Position — every self-call must be the ENTIRE return
    #      expression (`return f(...)`), never nested inside an
    #      operator / argument / let binding / discarded statement.
    #
    #   2. No cleanup — the interpreter trampoline (and the native
    #      goto transform) rebind parameters in place, so nothing
    #      refcounted may flow through the function: parameters, return
    #      type, every let binding and every expression must be
    #      int / float / bool. The ONLY exception is a string LITERAL
    #      passed directly to panic / print / println (consume-builtins
    #      in both backends — the argument is transferred, never bound).
    #
    #   3. Scope — plain fns only (not generic, not a method, no
    #      contracts, not irq_handler / inline(always), primitive ret).
    #
    # The interpreter mirrors the native transform as a trampoline: a
