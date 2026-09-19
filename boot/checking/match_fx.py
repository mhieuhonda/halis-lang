"""Checker mixin (match_fx) - verbatim segment of the original
boot/checker.py Checker class (lines 4432..4792), split for
maintainability. The final Checker class assembles all mixins in
boot/checking/checker.py - behavior is unchanged."""
from .helpers import (
    BUILTIN_EFFECTS, instantiate_type, type_args, type_base,
)
from ..compat import zip_strict

class CheckerMatch_fx(object):
    def check_match(self, e, env, expected):
        scrut_t = self.check_expr(e["scrut"], env, None)
        if scrut_t == "never":
            self.err("never value cannot be used in match", e)
        if "[" in scrut_t:
            ename = type_base(scrut_t)
        else:
            ename = scrut_t
        if ename not in self.enums:
            self.err("match scrutinee must be an enum, got %s" % scrut_t, e)
        edef = self.enums[ename]
        # For generic enums, instantiate variant payloads from scrut_t.
        typeparams = edef["typeparams"]
        type_map = {}
        if typeparams:
            sargs = type_args(scrut_t)
            if len(sargs) != len(typeparams):
                self.err("invalid enum instantiation: %s" % scrut_t, e)
            for tp, sa in zip_strict(typeparams, sargs):
                type_map[tp] = sa
        # Check exhaustiveness and arm types.
        covered = set()
        has_wildcard = False
        arm_types = []
        # Deep-scan-20: post-match move-state = union of every
        # fall-through arm's moves (collected while checking, applied
        # AFTER the loop so later arms are checked against the clean
        # pre-match state, exactly like if-arms).
        arm_posts = []
        for arm in e["arms"]:
            pat = arm["pattern"]
            if pat["k"] == "wildcard":
                has_wildcard = True
            else:
                pen = pat["enum"]
                pv = pat["variant"]
                # Deep-scan-10: a BARE pattern (no `Enum.` prefix —
                # accepted per the SPEC §5 grammar) resolves against the
                # scrutinee's enum here.
                if pen == "":
                    pen = ename
                    pat["enum"] = ename
                if pen != ename:
                    self.err("match arm pattern belongs to enum %s, not %s"
                             % (pen, ename), arm)
                # find the variant
                variant = None
                for v, payloads in edef["variants"]:
                    if v == pv:
                        variant = (v, payloads)
                        break
                if variant is None:
                    self.err("enum %s has no variant %s" % (ename, pv), arm)
                v, payloads = variant
                covered.add(v)
                # Deep-scan-20 fix (HIGH, llvm backend): annotate the arm
                # for the LLVM emitter's match lowering — variant_idx
                # (the DECLARATION index), and binds (name, instantiated
                # type) per payload. Without these the emitter emitted no
                # switch at all (first arm executed unconditionally) and
                # never bound the payload slots, returning garbage for
                # every match expression reaching --emit llvm.
                for vi, (vname3, _) in enumerate(edef["variants"]):
                    if vname3 == v:
                        arm["variant_idx"] = vi
                        break
                if pat["has_paren"] and len(pat["bindings"]) == len(payloads):
                    inst_pl = [instantiate_type(p, type_map) if typeparams
                               else p for p in payloads]
                    arm["binds"] = [(bn, bt) for bn, bt in
                                     zip(pat["bindings"], inst_pl)
                                     if bn != "_"]
                # Check binding count.
                if pat["has_paren"] and len(pat["bindings"]) != len(payloads):
                    self.err("variant %s has %d payloads, but pattern binds %d"
                             % (pv, len(payloads), len(pat["bindings"])), arm)
                if not pat["has_paren"] and len(payloads) != 0:
                    self.err("variant %s requires %d payload bindings"
                             % (pv, len(payloads)), arm)
            # Check arm body in a new scope with the bindings.
            # Deep-scan-20 fix (MED, parity with if-arms): each match arm
            # must isolate move-state. A take/drop in an earlier arm
            # permanently marked the binding moved, so a later, mutually
            # EXCLUSIVE arm got a spurious "use of moved value" — the
            # identical program with the arms swapped was accepted.
            # Snapshot -> check -> restore -> union the arm's moves into
            # the post-match state (same protocol as if-arms above).
            arm_pre = self.snapshot_moved(env)
            self.child(env)
            if pat["k"] != "wildcard":
                # Bind payload values.
                if pat["has_paren"]:
                    # find variant payloads
                    for v, payloads in edef["variants"]:
                        if v == pat["variant"]:
                            inst_payloads = [instantiate_type(p, type_map) if typeparams
                                             else p for p in payloads]
                            for bname, btype in zip(pat["bindings"], inst_payloads):
                                if bname == "_":
                                    continue
                                # BUG-SC-2 fix: SPEC.md section 5 states that
                                # match-arm bindings INTRODUCE a new scope and
                                # may shadow outer bindings for the duration of
                                # the arm (the one and only shadowing exception).
                                # Previously `self.lookup(env, bname)` searched
                                # ALL scopes and rejected any name already
                                # bound anywhere — contradicting the SPEC. We
                                # now only reject duplicates WITHIN the same
                                # arm (e.g. `E.Foo(a, a)`), which is the only
                                # real error.
                                if bname in env[-1]:
                                    self.err("duplicate binding name in match arm: %s" % bname, arm)
                                env[-1][bname] = [btype, False, False]
                            break
            body_t = self.check_expr(arm["body"], env, expected)
            arm["body_t"] = body_t
            env.pop()
            # Deep-scan-20: restore the pre-arm move-state (a later,
            # mutually exclusive arm must be checked against the clean
            # pre-match state); a `never` body diverges and cannot
            # contribute moves to the post-match state.
            arm_post = self.snapshot_moved(env)
            self.restore_moved(env, arm_pre)
            if body_t != "never":
                arm_posts.append(arm_post)
            if body_t == "never":
                arm_types.append(None)
            else:
                arm_types.append(body_t)
        # Exhaustiveness check.
        all_variants = {v for v, _ in edef["variants"]}
        if not has_wildcard:
            missing = all_variants - covered
            if missing:
                self.err("match is not exhaustive; missing: %s"
                         % ", ".join(sorted(missing)), e)
        # Deep-scan-20: apply the union of the fall-through arms' moves
        # to the post-match state (sound direction: a binding moved in
        # ANY arm is moved after the match).
        for arm_post in arm_posts:
            self.union_moved(env, arm_post)
        # All arm types must agree.
        non_never = [t for t in arm_types if t is not None]
        if not non_never:
            # All arms are `never` — match type is never.
            e["t"] = "never"
            return "never"
        first = non_never[0]
        for t in non_never:
            if t != first:
                self.err("match arms have different types: %s and %s" % (first, t), e)
        e["t"] = first
        return first

    # ---------- `?` operator ----------
    def check_qmark(self, e, env, expected):
        inner_t = self.check_expr(e["e"], env, None)
        if inner_t == "never":
            self.err("never value cannot be used in expression", e)
        ename = type_base(inner_t) if "[" in inner_t else inner_t
        if ename not in self.enums:
            self.err("? operator requires an enum type, got %s" % inner_t, e)
        edef = self.enums[ename]
        # Find the "error" variant: Err (with one payload) or None (no payload).
        err_variant = None
        ok_variant = None
        n_err_candidates = 0
        n_ok_candidates = 0
        for v, payloads in edef["variants"]:
            if v == "Err" and len(payloads) == 1:
                err_variant = (v, payloads)
                n_err_candidates += 1
            elif v == "None" and len(payloads) == 0:
                err_variant = (v, payloads)
                n_err_candidates += 1
            elif v == "Ok" and len(payloads) == 1:
                ok_variant = (v, payloads)
                n_ok_candidates += 1
            elif v == "Some" and len(payloads) == 1:
                ok_variant = (v, payloads)
                n_ok_candidates += 1
            else:
                # BUG (deep-scan-5): a third variant beyond the ok/err pair
                # can match NEITHER arm at runtime — the interpreter would
                # panic ("matched neither ok nor err variant") on a
                # checker-clean program. Reject at check time.
                self.err("? operator requires enum %s to have exactly the "
                         "ok variant (Ok/Some) and error variant (Err/None); "
                         "found extra variant '%s'" % (ename, v), e)
        # BUG (deep-scan-5): if the enum declares BOTH 'Err' and 'None',
        # the loop above silently keeps only the LAST one — `?` on the
        # other one panics at runtime. Reject the ambiguity.
        if n_err_candidates > 1:
            self.err("? operator requires enum %s to declare EITHER 'Err' "
                     "OR 'None' as its error variant, not both" % ename, e)
        # BUG (deep-scan-24): the same hazard existed for the ok variant —
        # an enum declaring BOTH 'Ok(P)' and 'Some(P)' passed the checker
        # (ok_variant silently kept the last one), and `?` on the other
        # variant panicked at runtime ("matched neither ok nor err
        # variant"). Reject the ambiguity at check time, mirroring the
        # n_err_candidates guard.
        if n_ok_candidates > 1:
            self.err("? operator requires enum %s to declare EITHER 'Ok' "
                     "OR 'Some' as its ok variant, not both" % ename, e)
        if err_variant is None:
            self.err("? operator requires enum %s to have an 'Err' (1 payload) or 'None' variant"
                     % ename, e)
        if ok_variant is None:
            self.err("? operator requires enum %s to have an 'Ok' or 'Some' variant (1 payload)"
                     % ename, e)
        # The success value type is the (instantiated) payload type of the
        # success variant.
        typeparams = edef["typeparams"]
        if typeparams:
            type_map = {}
            iargs = type_args(inner_t)
            if len(iargs) != len(typeparams):
                self.err("invalid enum instantiation: %s" % inner_t, e)
            for tp, ia in zip_strict(typeparams, iargs):
                type_map[tp] = ia
            ok_payload = ok_variant[1][0]
            success_t = instantiate_type(ok_payload, type_map)
            # BUG-3 fix: verify the error payload type matches the enclosing
            # function's error type argument. Without this check, `?` on a
            # `Result[int, int]` inside a function returning `Result[int, str]`
            # would silently propagate an `Err(int)` as if it were `Err(str)`.
            if err_variant[1]:  # has a payload (Err, not None)
                err_payload_t = instantiate_type(err_variant[1][0], type_map)
                cur_ret_args = type_args(self.cur_fn_ret) if "[" in self.cur_fn_ret else []
                # The error type argument is the LAST type arg of the return.
                if cur_ret_args and err_payload_t != cur_ret_args[-1]:
                    self.err("? operator: error payload type %s does not match "
                             "enclosing function's error type %s (in return type %s)"
                             % (err_payload_t, cur_ret_args[-1], self.cur_fn_ret), e)
        else:
            success_t = ok_variant[1][0]
        # Check that the enclosing function's return type is compatible.
        cur_ret = self.cur_fn_ret
        if cur_ret == "void":
            self.err("? operator cannot be used in a void function", e)
        cur_ret_base = type_base(cur_ret) if "[" in cur_ret else cur_ret
        if cur_ret_base != ename:
            self.err("? operator: enclosing function returns %s, cannot propagate %s"
                     % (cur_ret, ename), e)
        e["t"] = success_t
        e["err_variant"] = err_variant[0]
        e["ok_variant"] = ok_variant[0]
        # Deep-scan-20 fix (HIGH, llvm backend): annotate the Ok
        # variant's DECLARATION index and the success payload type so
        # the LLVM emitter switches on the real tag. Its defaults
        # (qmark_ok_idx=0, qmark_ok_type="int") took the Ok path on an
        # Err value whenever Ok wasn't the first declared variant, and
        # read the payload through the wrong accessor.
        for vi, (vname2, _) in enumerate(edef["variants"]):
            if vname2 == ok_variant[0]:
                e["qmark_ok_idx"] = vi
                break
        e["qmark_ok_type"] = success_t
        return success_t

    # ---------- effects (Stage 9-alpha: fine-grained, set-based) ----------
    def check_effects(self):
        """Fixpoint on the static call graph that computes, per function, the
        SET of effects its body transitively requires. A function passes iff
        its declared effect set is a superset of the computed set.

        Stage 9-beta: after the fixpoint converges, the per-function computed
        effect set is stored in self.computed_effects so an external auditor
        (e.g. boot.py --audit) can print the full capability tree.
        """
        # eff[key] = computed set of effects required by `key`'s body.
        # BUG-DS4-2: initialise from ALL call-graph nodes — self.fns keys PLUS
        # the synthetic "@default.<Struct>" nodes added while checking
        # struct field defaults (their effects must reach constructors).
        eff = {key: set() for key in self.edges}

        # Monotone fixpoint: union in each callee's computed effect set.
        changed = True
        while changed:
            changed = False
            for key, callees in self.edges.items():
                for c in callees:
                    if c.startswith("b:"):
                        new_eff = BUILTIN_EFFECTS.get(c[2:], set())
                    else:
                        # BUG-SC-1 fix (SOUNDNESS): extern fns have no body
                        # (so no outgoing edges), but their DECLARED effects
                        # are part of the capability surface. A caller of an
                        # extern fn must declare a superset of the extern's
                        # declared `uses` set. Previously the fixpoint used
                        # `eff[c]` (always empty for externs), so callers
                        # silently bypassed the capability check — a function
                        # calling `puts` (declared `uses IO`) without declaring
                        # `uses IO` itself would compile cleanly.
                        callee_fn = self.fns.get(c)
                        if callee_fn is not None and callee_fn.get("extern", False):
                            new_eff = set(callee_fn["effects"])
                        else:
                            new_eff = eff.get(c, set())
                    before = len(eff[key])
                    eff[key] |= new_eff
                    if len(eff[key]) != before:
                        changed = True

        # Publish the computed effect sets for downstream audit.
        self.computed_effects = eff

        # Capability check: declared ⊇ computed.
        # Also enforce the `pure` keyword (Stage 9-beta): a function declared
        # `pure` must have BOTH an empty declared set AND an empty computed
        # set (it transitively calls nothing effectful).
        for key, fn in self.fns.items():
            declared = fn["effects"]
            computed = eff[key]
            missing = computed - declared
            if not missing:
                # If the function is marked `pure`, verify it actually is.
                if fn.get("pure", False):
                    if computed:
                        self.err(
                            "function '%s' is declared 'pure' but transitively "
                            "uses effects %s (declared pure but callee chain "
                            "is not pure)" % (fn["name"], ", ".join(sorted(computed))),
                            fn)
                continue
            # Find a witness edge for one of the missing effects and report it.
            # Deep-scan-10 fix: iterate the edges in SORTED order — the
            # old code iterated a Python set, so with two violating
            # callees the REPORTED one varied with PYTHONHASHSEED
            # (SPEC §17.6 promises deterministic Stage-0 behaviour).
            for c in sorted(self.edges.get(key, ())):
                if c.startswith("b:"):
                    c_eff = BUILTIN_EFFECTS.get(c[2:], set())
                    callee_disp = c[2:]
                else:
                    # BUG (deep-scan-5): extern fns have no body, so their
                    # `eff` entry is empty — the witness check silently
                    # passed even though the fixpoint had already unioned
                    # the extern's DECLARED effects into the caller's
                    # computed set (the --audit output showed VIOLATION
                    # while --check said OK). Mirror the fixpoint here:
                    # use the extern's declared effects as the requirement.
                    callee_fn = self.fns.get(c)
                    if callee_fn is not None and callee_fn.get("extern", False):
                        c_eff = set(callee_fn["effects"])
                    else:
                        c_eff = eff.get(c, set())
                    callee_disp = c
                violated = c_eff - declared
                if not violated:
                    continue
                miss = sorted(violated)[0]
                self.err(
                    "function '%s' calls '%s' which requires effect '%s' "
                    "not declared (declared: %s; missing: %s)"
                    % (fn["name"], callee_disp, miss,
                       ", ".join(sorted(declared)) or "(none - pure)",
                       ", ".join(sorted(violated))),
                    fn)


