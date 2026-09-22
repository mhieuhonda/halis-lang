"""Checker mixin (stmt) - verbatim segment of the original
boot/checker.py Checker class (lines 1740..2230), split for
maintainability. The final Checker class assembles all mixins in
boot/checking/checker.py - behavior is unchanged."""
from .helpers import (
    instantiate_type, is_list, list_elem, type_base,
)

class CheckerStmt(object):
    def check_stmts(self, stmts, env, fn, in_loop):
        for s in stmts:
            self.check_stmt(s, env, fn, in_loop)

    def child(self, env):
        env.append({})
        return env

    # ---------- statements ----------
    def check_stmt(self, s, env, fn, in_loop):
        k = s["k"]
        if k == "let":
            self.require_type(s["t"], s, "variable type")
            if self.lookup(env, s["name"]) is not None:
                self.err("shadowing not allowed: %s" % s["name"], s)
            vt = self.check_expr(s["value"], env, s["t"])
            if vt == "never":
                env[-1][s["name"]] = [s["t"], s["mut"], False]
                return
            if vt != s["t"]:
                self.err("type mismatch: declared %s but got %s"
                         % (s["t"], vt), s)
            env[-1][s["name"]] = [s["t"], s["mut"], False]
        elif k == "assign":
            self.check_assign(s, env)
        elif k == "if":
            ct = self.check_expr(s["cond"], env, None)
            # Deep-scan-20 fix (LOW): `never` is the bottom type — a
            # condition that provably diverges (panic("dead")) is dead
            # code, not a type error. bin/un/all_return already treat
            # never as bottom; the condition checks were the only strict
            # equality sites (while false positive: `while panic(...) {}`
            # was rejected as "got never").
            if ct not in ("bool", "never"):
                self.err("if condition must be bool, got %s" % ct, s)
            # Deep-scan-16 HIGH-severity soundness fix: a binding moved
            # inside EITHER arm may be moved at runtime (the arm could
            # have executed). The previous snapshot+full-restore treated
            # post-if state as pre-if state — a binding moved in an if-
            # arm compiled cleanly through subsequent uses, but at
            # runtime the native codegen had already nulled the local
            # (`u_x = NULL` after drop/take). Sound fix: UNION the moved-
            # status across both arms (a binding is moved if it was moved
            # along any path).
            #
            # Reachability refinement: an arm that always exits (return /
            # break / continue / never-typed expr) does NOT contribute its
            # moved-state to the post-if state — its moves cannot reach
            # the code after the `if`. Without this refinement, the
            # idiomatic
            #     `if cond { ch.send(take(x)); return; } use(x);`
            # was rejected with a spurious "use of moved value" on `use(x)`,
            # even though the only path that reaches `use(x)` is the one
            # where `cond` was false (so `x` was never moved).
            snap = self.snapshot_moved(env)
            self.child(env)
            self.check_stmts(s["then"], env, fn, in_loop)
            env.pop()
            then_snap = self.snapshot_moved(env)
            then_terminates = self.path_terminates(s["then"], in_loop)
            self.restore_moved(env, snap)
            if s["els"] is not None:
                self.child(env)
                self.check_stmts(s["els"], env, fn, in_loop)
                env.pop()
                else_snap = self.snapshot_moved(env)
                else_terminates = self.path_terminates(s["els"], in_loop)
                self.restore_moved(env, snap)
                # Union each arm's moves ONLY if that arm can fall through
                # to the post-if code. An arm that always exits cannot
                # contribute moves to the post-if state.
                if not then_terminates:
                    self.union_moved(env, then_snap)
                if not else_terminates:
                    self.union_moved(env, else_snap)
            else:
                # No else arm — equivalent to an empty else (no moves).
                # Union just the then-arm state (if it can fall through).
                if not then_terminates:
                    self.union_moved(env, then_snap)
        elif k == "while":
            self.loop_header += 1
            ct = self.check_expr(s["cond"], env, None)
            self.loop_header -= 1
            # Deep-scan-20 fix (LOW): same bottom-type rule as `if` —
            # a `never` condition is dead code, not a type error.
            if ct not in ("bool", "never"):
                self.err("while condition must be bool, got %s" % ct, s)
            # Deep-scan-16 HIGH-severity soundness fix: the loop body may
            # execute >= 1 time. A binding moved inside the body (drop/take)
            # is nulled at runtime after the first iteration. Post-loop
            # uses of the binding read NULL and segfault. The previous
            # snapshot+restore cleared moves done in the body, so
            #   `while true { take(xs); break } let y = xs[0]`
            # compiled cleanly but crashed at runtime.
            # Sound fix: do NOT restore moves done in the body. The
            # idiomatic "drop then revive via mut reassignment" pattern
            # still works because check_assign's revive step clears the
            # moved flag.
            #
            # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-09
            # fix. The previous `snap = self.snapshot_moved(env)` line
            # was dead code — the deep-scan-16 fix removed the matching
            # `restore_moved` call (soundness), but left the snapshot
            # allocation as a per-iteration dict that was immediately
            # discarded. Removed it for clarity (a reader thinks the
            # snapshot is used; it isn't).
            #
            # Deep-scan-28 (break/continue edges + loop-top condition):
            # the body itself is still analysed ONCE against the entry
            # state (the language's pinned move model — feat_moved_scope
            # documents that a body move must not reject a post-loop
            # revive, and bodies are checked linearly). Three additive
            # unions close the remaining soundness gaps:
            #   (a) `break` edges: a revive AFTER a break must not erase
            #       the move for the break path — break-edge snapshots
            #       are unioned into the post-loop state;
            #   (b) `continue` edges: a continue guarantees the body
            #       re-enters, so when any continue edge moved a
            #       binding, the body is re-analysed against the
            #       merged loop-top state until no new moves appear
            #       (monotone, terminates); loops without continue
            #       keep the single-pass analysis;
            #   (c) the condition re-evaluates every iteration but was
            #       only checked against the ENTRY state — after the
            #       body pass it is re-checked against the merged
            #       loop-top state (entry ∪ body-end ∪ continue
            #       edges), so `while len(xs) > 0 { take(xs) }` is
            #       rejected instead of hanging/segfaulting at runtime.
            self.loop_frames.append({"break": [], "continue": []})
            self.child(env)
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
            # Loop-top state for a potential next iteration = body-end ∪
            # continue edges (body-end is already applied to the shared
            # binding cells by the linear pass).
            for snap in self.loop_frames[-1]["continue"]:
                self.union_moved(env, snap)
            # (b) continue guarantees re-entry — re-analyse the body
            # against the merged loop-top state until stable.
            if self.loop_frames[-1]["continue"]:
                passes = 1
                while passes < 64:
                    passes += 1
                    before = self.snapshot_moved(env)
                    self.child(env)
                    self.check_stmts(s["body"], env, fn, True)
                    env.pop()
                    for snap in self.loop_frames[-1]["continue"]:
                        self.union_moved(env, snap)
                    after = self.snapshot_moved(env)
                    if self.snapshot_equal(before, after):
                        break
            # (c) the condition runs on every iteration >= 2 against the
            # merged loop-top state (NOT the break edges — a break exits
            # before the condition is re-evaluated).
            self.loop_header += 1
            ct = self.check_expr(s["cond"], env, None)
            self.loop_header -= 1
            if ct not in ("bool", "never"):
                self.err("while condition must be bool, got %s" % ct, s)
            # (a) post-loop state additionally unions the break edges.
            for snap in self.loop_frames[-1]["break"]:
                self.union_moved(env, snap)
            self.loop_frames.pop()
            # No restore_moved — moves done in the body propagate to
            # post-loop state (soundness: assume the body executed).
        elif k == "for":
            self.loop_header += 1
            it = self.check_expr(s["iter"], env, None)
            self.loop_header -= 1
            if not is_list(it):
                self.err("for-in expression must be a list, got %s" % it, s)
            elem = list_elem(it)
            if s["vtype"] != elem:
                self.err("loop variable type %s does not match element %s"
                         % (s["vtype"], elem), s)
            # Deep-scan-16: same soundness fix as `while` — a binding moved
            # inside the for-body may be nulled at runtime after the first
            # iteration. Post-loop uses must be rejected.
            #
            # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-09
            # fix. Removed the dead `snap = self.snapshot_moved(env)`
            # line (no matching restore_moved; deep-scan-16 fix left it
            # as dead code).
            #
            # Deep-scan-28: break/continue edge handling, mirroring the
            # `while` branch (the iterable is evaluated ONCE so there is
            # no condition to re-check): a continue edge guarantees body
            # re-entry (re-analyse until stable); break edges union into
            # the post-loop state. Loops without continue stay single-pass
            # (feat_moved_scope's pinned semantics).
            self.loop_frames.append({"break": [], "continue": []})
            self.child(env)
            # BUG (deep-scan-5): the `let` branch rejects shadowing but the
            # `for` branch never checked — a loop variable could silently
            # shadow an outer binding (SPEC §4: no shadowing).
            if self.lookup(env, s["var"]) is not None:
                self.err("cannot shadow outer variable with the loop "
                         "variable: %s" % s["var"], s)
            env[-1][s["var"]] = [elem, False, False]
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
            for snap in self.loop_frames[-1]["continue"]:
                self.union_moved(env, snap)
            if self.loop_frames[-1]["continue"]:
                passes = 1
                while passes < 64:
                    passes += 1
                    before = self.snapshot_moved(env)
                    self.child(env)
                    env[-1][s["var"]] = [elem, False, False]
                    self.check_stmts(s["body"], env, fn, True)
                    env.pop()
                    for snap in self.loop_frames[-1]["continue"]:
                        self.union_moved(env, snap)
                    after = self.snapshot_moved(env)
                    if self.snapshot_equal(before, after):
                        break
            for snap in self.loop_frames[-1]["break"]:
                self.union_moved(env, snap)
            self.loop_frames.pop()
            # No restore_moved — moves done in the body propagate.
        elif k == "return":
            if fn["ret"] == "void":
                if s["value"] is not None:
                    self.err("void function cannot return a value", s)
            else:
                if s["value"] is None:
                    self.err("function returning %s must return a value" % fn["ret"], s)
                vt = self.check_expr(s["value"], env, fn["ret"])
                if vt != fn["ret"] and vt != "never":
                    self.err("return type mismatch: expected %s, got %s"
                             % (fn["ret"], vt), s)
        elif k == "break":
            if not in_loop:
                self.err("break only allowed inside a loop", s)
            # Deep-scan-28: record the moved-state at the break edge so
            # the loop driver can union it into the post-loop state (a
            # later revive in the body must not erase it).
            if self.loop_frames:
                self.loop_frames[-1]["break"].append(self.snapshot_moved(env))
        elif k == "continue":
            if not in_loop:
                self.err("continue only allowed inside a loop", s)
            # Deep-scan-28: record the moved-state at the continue edge —
            # it is the entry state of the NEXT iteration.
            if self.loop_frames:
                self.loop_frames[-1]["continue"].append(self.snapshot_moved(env))
        elif k == "expr":
            self.check_expr(s["e"], env, None)
        elif k == "asm":
            # Stage 27 (v0.50.0-alpha): inline-assembly statement.
            # The boot checker accepts the AST (the boot interpreter
            # cannot execute asm, but the program is still parseable
            # and the self-hosted compiler hlc.hls is the canonical
            # path that lowers asm! to GCC extended asm). Validation:
            #   * `in` operands must be primitive (int/float/bool).
            #   * `out`/`inout`/`late_out` operands must be a mutable
            #     lvalue of a primitive type.
            #   * `{N}` template placeholders must reference an
            #     existing operand (N in [0, len(operands))).
            #   * `pure` requires at least one output operand (a
            #     pure asm with no output is dead code).
            #   * `noreturn` requires no `out`/`inout`/`late_out`
            #     (no point writing outputs that are never observed).
            self.check_asm(s, env)
        else:
            self.err("unknown statement: %s" % k, s)

    # Stage 27 (v0.50.0-alpha): asm! checker. See the boot parser's
    # parse_asm_stmt for the AST node shape.
    def check_asm(self, s, env):
        template = s["template"]
        operands = s["operands"]
        options = s["options"]
        # Validate the template's {N} placeholders.
        n_ops = len(operands)
        i = 0
        while i < len(template):
            ch = ord(template[i])
            # `{` starts a placeholder; `{{` is a literal `{`.
            if ch == 123:  # '{'
                if i + 1 < len(template) and ord(template[i + 1]) == 123:
                    i = i + 2
                    continue
                # Find the matching `}`.
                j = i + 1
                while j < len(template) and ord(template[j]) != 125:
                    j = j + 1
                if j >= len(template):
                    self.err("asm! template has unterminated '{' (no "
                             "matching '}'): %r" % template, s)
                body = template[i + 1:j]
                # body should be a non-negative integer.
                try:
                    n = int(body)
                except ValueError:
                    self.err("asm! template placeholder {%s} is not a "
                             "number (expected {0}, {1}, ...)" % body, s)
                    return  # unreachable — err raises
                if n < 0 or n >= n_ops:
                    self.err("asm! template placeholder {%d} is out of "
                             "range (have %d operand(s))" % (n, n_ops), s)
                i = j + 1
            elif ch == 125:  # '}'
                if i + 1 < len(template) and ord(template[i + 1]) == 125:
                    i = i + 2
                    continue
                self.err("asm! template has lone '}' (use '}}' for a "
                         "literal '}'): %r" % template, s)
            else:
                i = i + 1
        # Validate each operand.
        has_output = False
        for op in operands:
            d = op["dir"]
            if d not in ("in", "out", "inout", "late_out"):
                self.err("asm! operand direction '%s' is unknown" % d, s)
            if d != "in":
                has_output = True
            # Constraint validation (lightweight — the codegen re-validates).
            c = op["constraint"]
            if not op["is_constraint_str"]:
                if c not in ("reg", "mem", "imm"):
                    self.err("asm! constraint '%s' is unknown (bare "
                             "constraints: reg, mem, imm; for a "
                             "specific register use a string like "
                             "\"eax\")" % c, s)
                # Stage 27 perfection (v0.50.2-alpha) deep-scan-17:
                # `imm` is an immediate-constant constraint — it cannot
                # be used as an output (you can't write to a constant).
                # `in(imm) expr` is technically valid in GCC but requires
                # `expr` to be a compile-time constant; the HLS compiler
                # does not constant-fold in general, so reject `imm` on
                # outputs unconditionally.
                #
                # Stage 27 perfection (v0.50.3-alpha) deep-scan-18:
                # BUG-10 fix. Also reject `in(imm)` on a NON-literal
                # expression — GCC's `i` constraint requires a compile-
                # time constant. Without this check, `in(imm) some_var`
                # was accepted by the HLS checker and reached GCC as
                # `"i"(some_var)`, producing an opaque "impossible
                # constraint" error with no source-line attribution.
                # Now the user gets a clear HLS error pointing at the
                # asm! line, suggesting `reg` instead.
                if c == "imm" and d != "in":
                    self.err("asm! constraint 'imm' cannot be used with "
                             "direction '%s' — an immediate is a compile-"
                             "time constant and cannot be written to" % d,
                             s)
                if c == "imm" and d == "in":
                    in_expr = op["expr"]
                    in_kind = in_expr["k"] if isinstance(in_expr, dict) else None
                    # Accept integer literals, bool literals, and
                    # unary-minus of integer literals (the common
                    # constant-expr forms). Reject everything else —
                    # the user should use `reg` for runtime values.
                    if in_kind == "un" and in_expr.get("op") == "-":
                        inner = in_expr.get("e")
                        if isinstance(inner, dict) and inner.get("k") == "int":
                            in_kind = "int"  # treat -5 as a constant
                    if in_kind not in ("int", "bool"):
                        self.err("asm! `in(imm)` requires a compile-time "
                                 "constant (int/bool literal or unary-"
                                 "minus-of-int-literal), got a runtime "
                                 "expression — use `reg` instead for "
                                 "runtime values", s)
            else:
                # Stage 27 perfection (v0.50.2-alpha) deep-scan-17:
                # String-form constraint (a register name like "eax" or
                # a GCC constraint letter like "a"). Validate against an
                # allowlist of known x86-64 register names + GCC single-
                # letter constraints; reject anything else with a clear
                # HLS error (otherwise the malformed constraint silently
                # reaches GCC, which produces an opaque C compile error
                # with no source-line attribution).
                #
                # Deep-scan-30 fix: validate case-SENSITIVELY and with the
                # same allowlist as the self-hosted checker
                # (src/hlc/check_expr.hls is_known_asm_constraint). The
                # boot checker used to lowercase the constraint first,
                # which (a) rejected valid uppercase constraints like
                # "S"/"D" that its own error message advertised, and
                # (b) diverged from the native checker — a program
                # accepted by `hlc` was rejected by the boot interpreter
                # (and vice versa was impossible, but parity broke
                # differential testing). GCC constraint letters ARE case
                # sensitive: "S" (memory operand) and "s" (immediate)
                # are different constraints.
                # GCC single-letter constraint letters we accept (the
                # full self-hosted set).
                known_single = ("a", "b", "c", "d", "D", "S", "q", "r",
                                 "f", "t", "u", "y", "x", "A", "Q", "R",
                                 "m", "i", "n", "F", "I", "J", "K",
                                 "L", "M", "N", "O", "P",
                                 "g", "X", "p", "v", "z")
                # x86-64 register names we accept (the codegen translates
                # these to GCC constraint letters; see translate_asm_constraint).
                known_reg = ("eax", "rax", "ebx", "rbx", "ecx", "rcx",
                             "edx", "rdx", "esi", "rsi", "edi", "rdi",
                             "al", "bl", "cl", "dl", "ah", "bh", "ch", "dh",
                             "ax", "bx", "cx", "dx", "si", "di", "bp", "sp",
                             "rbp", "rsp", "r8", "r9", "r10", "r11",
                             "r12", "r13", "r14", "r15")
                # If the user already prefixed the constraint with `=` or
                # `+` (e.g. `out("=r") x`), validate the BODY (after the
                # prefix). The codegen has special-case logic to skip
                # re-adding the prefix when one is already present.
                body = c if isinstance(c, str) else ""
                prefix = ""
                if isinstance(c, str) and len(c) >= 1 and c[0] in ("=", "+"):
                    prefix = c[0]
                    body = c[1:]
                    # `&` is the late-clobber marker; strip it for the
                    # body check.
                    if len(body) >= 1 and body[0] == "&":
                        body = body[1:]
                if body not in known_single and body not in known_reg:
                    self.err("asm! constraint '%s' is not a recognised "
                             "x86-64 register name or GCC constraint "
                             "letter (known: eax/rax/ebx/rbx/ecx/rcx/"
                             "edx/rdx/esi/rsi/edi/rdi, or single-letter "
                             "GCC constraints a/b/c/d/D/S/q/r/A)" % c, s)
                # Direction-vs-prefix consistency: `in("=r") x` is wrong
                # (`=` means write-only output). `out("r") x` is OK (the
                # codegen adds `=`). `in("+r") x` is also wrong.
                if d == "in" and prefix in ("=", "+"):
                    self.err("asm! constraint '%s' has a direction prefix "
                             "'%s' but is used with `in` (input operands "
                             "have no prefix — drop the '%s')" %
                             (c, prefix, prefix), s)
                if d == "out" and prefix == "+":
                    self.err("asm! constraint '%s' has prefix '+' (read-"
                             "write) but is used with `out` (write-only) "
                             "— use `inout` if you want read-write" % c, s)
            # Validate the operand's value/lvalue type.
            if d == "in":
                vt = self.check_expr(op["expr"], env, None)
                if vt not in ("int", "float", "bool"):
                    self.err("asm! `in` operand must be a primitive "
                             "(int/float/bool), got %s" % vt, s)
            else:
                # out/inout/late_out — must be a mutable lvalue.
                if op["var_kind"] != "ident" and op["var_kind"] != "field" \
                        and op["var_kind"] != "index":
                    self.err("asm! %s operand must be an lvalue" % d, s)
                # Use check_lvalue to annotate + verify the binding.
                tgt = op["expr"]
                # the parser stored the lvalue as `expr`; treat it as
                # an assignment-target to enforce mutability.
                root = tgt
                while root["k"] in ("field", "index"):
                    root = root["target"]
                binding = self.lookup(env, root["name"])
                if binding is None:
                    self.err("variable does not exist: %s" % root["name"], s)
                # `out(reg) x` writes to x — x must be mut OR be a
                # field/index target (mutation through a reference).
                # We allow `out` on a non-mut lvalue ONLY when the
                # lvalue is a field/index (writing through a borrowed
                # reference). For an ident, `mut` is required.
                if tgt["k"] == "ident" and not binding[1]:
                    self.err("asm! %s operand must be a `mut` binding "
                             "(cannot write to immutable variable %s)"
                             % (d, root["name"]), s)
                if d == "inout":
                    if len(binding) >= 3 and binding[2]:
                        self.err("asm! inout operand uses a moved value: "
                                 "%s" % root["name"], s)
                tt = self.check_lvalue(tgt, env)
                if tt not in ("int", "float", "bool"):
                    self.err("asm! %s operand must be a primitive "
                             "(int/float/bool), got %s" % (d, tt), s)
                # Annotate the operand with the resolved type for
                # the codegen (matches the self-hosted checker).
                op["t"] = tt
        # Option validation.
        # Stage 27 perfection (v0.50.2-alpha) deep-scan-17: reject
        # duplicate option names (e.g. `options(pure, pure)` is a
        # user mistake — the asm accepts the duplicate silently, which
        # is a foot-gun: a user who fat-fingers `options(nomem, nomem)`
        # gets no signal that they probably meant `options(nomem,
        # preserves_flags)`).
        seen_opts = set()
        for opt in options:
            if opt in seen_opts:
                self.err("asm! option '%s' appears more than once in "
                         "the options list (duplicates are a no-op but "
                         "usually indicate a typo)" % opt, s)
            seen_opts.add(opt)
        # Stage 27 perfection (v0.50.2-alpha) deep-scan-17:
        # `pure` + `noreturn` are contradictory — `pure` says the asm
        # may be elided if outputs are unused, but `noreturn` says it
        # doesn't fall through (eliding a noreturn silently turns it
        # into a return). Check this BEFORE the pure-requires-output
        # and noreturn-forbids-outputs checks so the user gets the
        # clearest error message for this contradiction (otherwise
        # they'd get a side-effect error like "pure requires outputs"
        # which doesn't explain the underlying contradiction).
        if "pure" in options and "noreturn" in options:
            self.err("asm! options `pure` and `noreturn` are "
                     "contradictory — `pure` says the asm may be "
                     "elided if its outputs are unused, but `noreturn` "
                     "says it doesn't fall through (eliding it would "
                     "silently turn a noreturn into a return)", s)
        # Stage 27 perfection (v0.50.2-alpha) deep-scan-17:
        # `pure` without `nomem` is contradictory — `pure` says the
        # asm has no side effects, but the default `"memory"` clobber
        # says it may read/write memory (a memory side effect). Rust's
        # `asm!` requires `pure` to be paired with `nomem`; we follow
        # the same rule for soundness.
        if "pure" in options and "nomem" not in options:
            self.err("asm! with `options(pure)` requires `nomem` too — "
                     "a pure asm must not have memory side effects (the "
                     "default `\"memory\"` clobber would contradict "
                     "`pure`)", s)
        if "pure" in options and not has_output:
            self.err("asm! with `options(pure)` requires at least one "
                     "output operand (a pure asm with no outputs is dead "
                     "code — gcc would legitimately delete it)", s)
        if "noreturn" in options and has_output:
            self.err("asm! with `options(noreturn)` cannot have output "
                     "operands (the asm does not fall through, so the "
                     "outputs would never be observed)", s)

    def check_assign(self, s, env):
        tgt = s["target"]
        # find root binding
        root = tgt
        while root["k"] in ("field", "index"):
            root = root["target"]
        binding = self.lookup(env, root["name"])
        if binding is None:
            self.err("variable does not exist: %s" % root["name"], s)
        # Stage 8-alpha: use-after-move check on the LHS root.
        # Reading a moved binding (even to reassign) requires `mut` first.
        # For `mut x = ...`, the binding is "revived" — its moved flag is cleared.
        # For `x.field = ...` / `xs[i] = ...`, the binding itself is being read
        # (to obtain the reference), so moved bindings cannot be used.
        if len(binding) >= 3 and binding[2]:
            if tgt["k"] == "ident":
                # Whole-binding assignment: this is allowed on `let mut` only.
                # The binding will be revived below after the RHS is checked.
                if not binding[1]:
                    self.err("cannot reassign immutable variable: %s" % root["name"], s)
            else:
                # Field/index assignment — uses the binding's value (reference).
                self.err("use of moved value: %s" % root["name"], s)
        else:
            # 'mut' only governs REASSIGNMENT of the binding (name = v).
            # Field/index assignment mutates CONTENTS through a reference — no mut needed.
            if tgt["k"] == "ident" and not binding[1]:
                self.err("cannot reassign immutable variable: %s" % root["name"], s)
        tt = self.check_lvalue(tgt, env)
        vt = self.check_expr(s["value"], env, tt)
        if vt == "never":
            return
        if vt != tt:
            self.err("type mismatch on assignment: expected %s, got %s" % (tt, vt), s)
        # Stage 8-alpha: revive the binding (clear moved) on whole-binding
        # assignment. The binding now owns a fresh value.
        if tgt["k"] == "ident":
            self.revive_binding(env, root["name"])

    def check_lvalue(self, e, env):
        # Stage 8-beta: every branch annotates e["t"] so downstream
        # consumers (codegen, linters) can read the lvalue's static type.
        if e["k"] == "ident":
            b = self.lookup(env, e["name"])
            e["t"] = b[0]
            return b[0]
        if e["k"] == "field":
            bt = self.check_expr(e["target"], env, None)
            info = self.resolve_struct(bt)
            if info is None:
                self.err("cannot access field on type %s" % bt, e)
            st, type_map = info
            for fname, ftype, _ in st["fields"]:
                if fname == e["name"]:
                    ft = instantiate_type(ftype, type_map) if type_map else ftype
                    e["t"] = ft
                    return ft
            self.err("struct %s has no field %s" % (type_base(bt), e["name"]), e)
        if e["k"] == "index":
            tt = self.check_expr(e["target"], env, None)
            if not is_list(tt):
                self.err("cannot use index on type %s" % tt, e)
            it = self.check_expr(e["idx"], env, None)
            if it != "int":
                self.err("index must be int, got %s" % it, e)
            et = list_elem(tt)
            e["t"] = et
            return et
        self.err("invalid lvalue", e)

    # ---------- expressions ----------
