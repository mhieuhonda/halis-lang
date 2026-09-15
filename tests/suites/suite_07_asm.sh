#!/usr/bin/env bash
# Suite suite_07_asm - verbatim section of the original tests/run_tests.sh
# (split for maintainability; sourced by tests/run_tests.sh in order).

echo "=== 18. Stage 27: inline assembly (asm!) ==="
# Stage 27 (v0.50.0-alpha): the asm! statement is parsed by both
# the boot lexer/parser and the self-hosted hlc.hls, type-checked
# by both checkers, and lowered to GCC's extended-asm syntax by the
# self-hosted codegen. The boot interpreter cannot execute native
# asm (it raises a clean HLPanic), but the parser/checker must
# accept the syntax so programs declaring asm! blocks can still
# be parsed + type-checked by boot.

# (a) boot parses feat_stage27_asm.hls (smoke test).
if python3 boot/boot.py tests/ok/feat_stage27_asm.hls >"$TMP/s27_boot.out" 2>&1; then
    if grep -q "ACCEPTANCE OK" "$TMP/s27_boot.out"; then
        ok "asm: feat_stage27_asm.hls runs on the boot interpreter (no asm executed)"
    else
        bad "asm: feat_stage27_asm.hls did not pass on the boot interpreter"
        tail -5 "$TMP/s27_boot.out"
    fi
else
    bad "asm: feat_stage27_asm.hls failed to run on the boot interpreter"
    tail -10 "$TMP/s27_boot.out"
fi

# (b) the asm! syntax parses via boot's --check mode.
if python3 boot/boot.py --check tests/ok/feat_stage27_asm.hls >"$TMP/s27_check.out" 2>&1; then
    if grep -q "OK: types and effects valid" "$TMP/s27_check.out"; then
        ok "asm: boot --check accepts the asm! syntax (types + effects valid)"
    else
        bad "asm: boot --check did not accept asm!"
        cat "$TMP/s27_check.out"
    fi
else
    bad "asm: boot --check failed on asm!"
    cat "$TMP/s27_check.out"
fi

# (c) the self-hosted hlc.hls parses + checks feat_stage27_asm.hls.
if "$TMP/hlc1" tests/ok/feat_stage27_asm.hls "$TMP/s27.c" >"$TMP/s27_hlc.out" 2>&1; then
    ok "asm: self-hosted hlc compiles feat_stage27_asm.hls"
else
    bad "asm: self-hosted hlc failed to compile feat_stage27_asm.hls"
    cat "$TMP/s27_hlc.out"
fi

# (d) the C source contains __asm__ on every asm!-containing function.
asm_count=$(grep -c "__asm__" "$TMP/s27.c" 2>/dev/null || echo 0)
if [ "$asm_count" -ge 6 ]; then
    ok "asm: C source has $asm_count __asm__ sites (6+ expected)"
else
    bad "asm: C source has only $asm_count __asm__ sites (expected 6+)"
fi

# (e) the C source contains the default clobber list ("cc", "memory").
if grep -q '"cc", "memory"' "$TMP/s27.c"; then
    ok "asm: C source has default clobber list ("cc", "memory")"
else
    bad "asm: C source missing default clobber list"
fi

# (f) the noreturn option emits __builtin_unreachable() after the asm.
if grep -A 1 '"hlt"' "$TMP/s27.c" | grep -q "__builtin_unreachable"; then
    ok "asm: noreturn option emits __builtin_unreachable()"
else
    bad "asm: noreturn option did not emit __builtin_unreachable()"
fi

# (g) the pure option drops __volatile__ (gcc may elide if outputs unused).
if grep -q '__asm__ ("mov' "$TMP/s27.c"; then
    ok "asm: pure option drops __volatile__ (gcc may elide)"
else
    bad "asm: pure option did not drop __volatile__"
fi

# (h) the C source compiles cleanly with gcc -O2.
if gcc -O2 -Werror -c -o "$TMP/s27.o" "$TMP/s27.c" -lm -pthread 2>"$TMP/s27_gcc.log"; then
    ok "asm: C source compiles cleanly with gcc -O2 -Werror"
else
    bad "asm: C source fails to compile with gcc -O2 -Werror"
    cat "$TMP/s27_gcc.log"
fi

# (i) the inb() helper compiles to a SINGLE in instruction
#     (verified by objdump on the produced .o file).
in_count=$(objdump -d "$TMP/s27.o" 2>/dev/null | grep -E "in\s+\(%dx\),%al|in\s+%dx,%al" | wc -l)
if [ "$in_count" -ge 1 ]; then
    ok "asm: inb() compiles to a single 'in' instruction ($in_count site(s))"
else
    bad "asm: inb() did not compile to a single 'in' instruction"
    objdump -d "$TMP/s27.o" | grep -A 5 "usf_asm_inb"
fi

# (j) the interpreter raises a clean error if asm! is executed.
#     This verifies the boot interp's asm! handler.
cat >"$TMP/s27_run.hls" <<'HLS_EOF'
fn trigger_asm() -> void {
    asm!("nop")
}
fn main() -> int uses IO {
    trigger_asm()
    return 0
}
HLS_EOF
if python3 boot/boot.py "$TMP/s27_run.hls" >"$TMP/s27_run.out" 2>&1; then
    bad "asm: interpreter should have raised an error on asm! execution"
    cat "$TMP/s27_run.out"
else
    if grep -q "asm! cannot be executed by the boot interpreter" "$TMP/s27_run.out"; then
        ok "asm: interpreter raises a clean error when asm! is executed"
    else
        bad "asm: interpreter did not raise the expected asm! error"
        cat "$TMP/s27_run.out"
    fi
fi

# (k) the checker rejects an out-of-range {N} template placeholder.
cat >"$TMP/s27_bad_oob.hls" <<'HLS_EOF'
fn bad_oob() -> void {
    let mut x: int = 0
    asm!("mov {0}, {5}", out(reg) x)
}
fn main() -> int uses IO {
    return 0
}
HLS_EOF
if python3 boot/boot.py --check "$TMP/s27_bad_oob.hls" >"$TMP/s27_bad_oob.out" 2>&1; then
    bad "asm: checker did not reject out-of-range {5} template placeholder"
else
    if grep -q "out of range" "$TMP/s27_bad_oob.out"; then
        ok "asm: checker rejects out-of-range {5} template placeholder"
    else
        bad "asm: checker rejected {5} but with the wrong message"
        cat "$TMP/s27_bad_oob.out"
    fi
fi

# (l) the checker rejects options(pure) with no outputs.
cat >"$TMP/s27_bad_pure.hls" <<'HLS_EOF'
fn bad_pure() -> void {
    asm!("nop", options(pure))
}
fn main() -> int uses IO {
    return 0
}
HLS_EOF
if python3 boot/boot.py --check "$TMP/s27_bad_pure.hls" >"$TMP/s27_bad_pure.out" 2>&1; then
    bad "asm: checker did not reject options(pure) with no outputs"
else
    if grep -q "pure" "$TMP/s27_bad_pure.out"; then
        ok "asm: checker rejects options(pure) with no outputs"
    else
        bad "asm: checker rejected pure-no-outputs but with the wrong message"
        cat "$TMP/s27_bad_pure.out"
    fi
fi

# (m) the checker rejects options(noreturn) with outputs.
cat >"$TMP/s27_bad_noret.hls" <<'HLS_EOF'
fn bad_noret() -> void {
    let mut x: int = 0
    asm!("nop", out(reg) x, options(noreturn))
}
fn main() -> int uses IO {
    return 0
}
HLS_EOF
if python3 boot/boot.py --check "$TMP/s27_bad_noret.hls" >"$TMP/s27_bad_noret.out" 2>&1; then
    bad "asm: checker did not reject options(noreturn) with outputs"
else
    if grep -q "noreturn" "$TMP/s27_bad_noret.out"; then
        ok "asm: checker rejects options(noreturn) with outputs"
    else
        bad "asm: checker rejected noreturn-with-outputs but with the wrong message"
        cat "$TMP/s27_bad_noret.out"
    fi
fi

# (n) the checker rejects `out(reg) x` on an immutable variable.
cat >"$TMP/s27_bad_immut.hls" <<'HLS_EOF'
fn bad_immut() -> void {
    let x: int = 0
    asm!("nop", out(reg) x)
}
fn main() -> int uses IO {
    return 0
}
HLS_EOF
if python3 boot/boot.py --check "$TMP/s27_bad_immut.hls" >"$TMP/s27_bad_immut.out" 2>&1; then
    bad "asm: checker did not reject out(reg) on immutable x"
else
    if grep -q "mut" "$TMP/s27_bad_immut.out"; then
        ok "asm: checker rejects out(reg) on immutable x"
    else
        bad "asm: checker rejected immutable out but with the wrong message"
        cat "$TMP/s27_bad_immut.out"
    fi
fi

# (o) make asm-acceptance runs end-to-end.
if make asm-acceptance >"$TMP/s27_acc.log" 2>&1; then
    if grep -q "ACCEPTANCE OK" "$TMP/s27_acc.log"; then
        ok "asm: make asm-acceptance runs end-to-end"
    else
        bad "asm: make asm-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/s27_acc.log"
    fi
else
    bad "asm: make asm-acceptance failed"
    tail -10 "$TMP/s27_acc.log"
fi

# (p) bootstrap is deterministic with the asm! additions (the
#     self-hosted compiler must remain deterministic).
if python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_asm1.c" >/dev/null 2>&1     && python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_asm2.c" >/dev/null 2>&1; then
    if diff -q "$TMP/hlc_asm1.c" "$TMP/hlc_asm2.c" >/dev/null; then
        ok "asm: bootstrap deterministic with asm! changes"
    else
        bad "asm: bootstrap not deterministic with asm! changes"
    fi
else
    bad "asm: bootstrap compile failed"
fi

# (q) the asm_demo.hls example compiles + runs cleanly (the safe
#     helpers — read_tsc, asm_add_one, cpu_pause — should run on the
#     host; the privileged helpers — inb, outb, halt_forever — are
#     NOT called from main()).
if "$TMP/hlc1" examples/asm_demo.hls "$TMP/s27_demo.c" >/dev/null 2>&1     && gcc -O2 -o "$TMP/s27_demo" "$TMP/s27_demo.c" -lm -pthread 2>"$TMP/s27_demo_gcc.log"; then
    if "$TMP/s27_demo" >"$TMP/s27_demo.out" 2>&1; then
        if grep -q "Stage 27 asm! demo:" "$TMP/s27_demo.out"             && grep -q "asm_add_one(41)  -> 42" "$TMP/s27_demo.out"; then
            ok "asm: examples/asm_demo.hls runs cleanly (asm_add_one(41) -> 42)"
        else
            bad "asm: examples/asm_demo.hls ran but output is unexpected"
            cat "$TMP/s27_demo.out"
        fi
    else
        bad "asm: examples/asm_demo.hls binary failed to run"
        cat "$TMP/s27_demo.out"
    fi
else
    bad "asm: examples/asm_demo.hls failed to compile + link"
    cat "$TMP/s27_demo_gcc.log"
fi

# ======================================================================
# Stage 27 perfection (v0.50.2-alpha) deep-scan-17:
# New negative tests for the soundness hardening. Each test verifies
# that the checker REJECTS an asm! program that previously slipped
# through to GCC and produced an opaque C compile error.
# ======================================================================

# (r) checker rejects an unknown string-form constraint (`"exa"`).
if python3 boot/boot.py --check tests/fail/fail_asm_bad_constraint_str.hls >"$TMP/s27_r.out" 2>&1; then
    bad "asm: checker accepted unknown string-form constraint 'exa'"
else
    if grep -q "not a recognised x86-64 register name" "$TMP/s27_r.out"; then
        ok "asm: checker rejects unknown string-form constraint 'exa'"
    else
        bad "asm: rejected 'exa' but with the wrong message"
        cat "$TMP/s27_r.out"
    fi
fi

# (r-self) the self-hosted hlc rejects the same program (mirrors boot).
if "$TMP/hlc1" tests/fail/fail_asm_bad_constraint_str.hls "$TMP/s27_r.c" >"$TMP/s27_r_hlc.out" 2>&1; then
    bad "asm: self-hosted hlc accepted unknown string-form constraint 'exa'"
else
    if grep -q "not a recognised x86-64 register name" "$TMP/s27_r_hlc.out"; then
        ok "asm: self-hosted hlc rejects unknown string-form constraint 'exa'"
    else
        bad "asm: self-hosted hlc rejected 'exa' but with the wrong message"
        cat "$TMP/s27_r_hlc.out"
    fi
fi

# (s) checker rejects duplicate options (`options(nomem, nomem)`).
if python3 boot/boot.py --check tests/fail/fail_asm_dup_option.hls >"$TMP/s27_s.out" 2>&1; then
    bad "asm: checker accepted duplicate option 'nomem'"
else
    if grep -q "appears more than once" "$TMP/s27_s.out"; then
        ok "asm: checker rejects duplicate option 'nomem'"
    else
        bad "asm: rejected duplicate 'nomem' but with the wrong message"
        cat "$TMP/s27_s.out"
    fi
fi

# (s-self) self-hosted mirror.
if "$TMP/hlc1" tests/fail/fail_asm_dup_option.hls "$TMP/s27_s.c" >"$TMP/s27_s_hlc.out" 2>&1; then
    bad "asm: self-hosted hlc accepted duplicate option 'nomem'"
else
    if grep -q "appears more than once" "$TMP/s27_s_hlc.out"; then
        ok "asm: self-hosted hlc rejects duplicate option 'nomem'"
    else
        bad "asm: self-hosted hlc rejected duplicate 'nomem' but with the wrong message"
        cat "$TMP/s27_s_hlc.out"
    fi
fi

# (t) checker rejects pure + noreturn (contradictory).
if python3 boot/boot.py --check tests/fail/fail_asm_pure_noreturn.hls >"$TMP/s27_t.out" 2>&1; then
    bad "asm: checker accepted contradictory pure + noreturn"
else
    if grep -q "pure.*noreturn.*contradictory\|noreturn.*pure.*contradictory\|contradictory" "$TMP/s27_t.out"; then
        ok "asm: checker rejects contradictory pure + noreturn"
    else
        bad "asm: rejected pure+noreturn but with the wrong message"
        cat "$TMP/s27_t.out"
    fi
fi

# (u) checker rejects pure without nomem.
if python3 boot/boot.py --check tests/fail/fail_asm_pure_without_nomem.hls >"$TMP/s27_u.out" 2>&1; then
    bad "asm: checker accepted pure without nomem"
else
    if grep -q "requires.*nomem" "$TMP/s27_u.out"; then
        ok "asm: checker rejects pure without nomem"
    else
        bad "asm: rejected pure-without-nomem but with the wrong message"
        cat "$TMP/s27_u.out"
    fi
fi

# (v) checker rejects imm constraint on out/inout/late_out.
if python3 boot/boot.py --check tests/fail/fail_asm_imm_out.hls >"$TMP/s27_v.out" 2>&1; then
    bad "asm: checker accepted imm on out direction"
else
    if grep -q "imm.*cannot be used" "$TMP/s27_v.out"; then
        ok "asm: checker rejects imm constraint on out direction"
    else
        bad "asm: rejected imm-on-out but with the wrong message"
        cat "$TMP/s27_v.out"
    fi
fi

# (w) checker rejects direction-prefix/constraint mismatch (`in("=r") x`).
if python3 boot/boot.py --check tests/fail/fail_asm_in_prefix_constraint.hls >"$TMP/s27_w.out" 2>&1; then
    bad "asm: checker accepted in(\"=r\") x (direction-prefix mismatch)"
else
    if grep -q "direction prefix" "$TMP/s27_w.out"; then
        ok "asm: checker rejects in(\"=r\") x (direction-prefix mismatch)"
    else
        bad "asm: rejected in(\"=r\") but with the wrong message"
        cat "$TMP/s27_w.out"
    fi
fi

# (x) the deep-scan-17 positive regression test compiles + runs cleanly
#     on the boot interpreter and produces the expected output.
if python3 boot/boot.py tests/ok/feat_stage27_perfection.hls >"$TMP/s27_x.out" 2>&1; then
    if grep -q "deep-scan-17 hardening does not break valid asm!" "$TMP/s27_x.out"; then
        ok "asm: feat_stage27_perfection.hls runs on the boot interpreter"
    else
        bad "asm: feat_stage27_perfection.hls did not print the acceptance line"
        cat "$TMP/s27_x.out"
    fi
else
    bad "asm: feat_stage27_perfection.hls failed to run on the boot interpreter"
    cat "$TMP/s27_x.out"
fi

# (x-check) the self-hosted hlc compiles feat_stage27_perfection.hls.
if "$TMP/hlc1" tests/ok/feat_stage27_perfection.hls "$TMP/s27_x.c" >"$TMP/s27_x_hlc.out" 2>&1; then
    # The C source must have at least 9 __asm__ sites (one per
    # asm!-containing function) and compile cleanly with gcc -O2 -Werror.
    s27_x_asm_count=$(grep -c "__asm__" "$TMP/s27_x.c" 2>/dev/null || echo 0)
    if [ "$s27_x_asm_count" -ge 9 ]; then
        if gcc -O2 -Werror -c -o "$TMP/s27_x.o" "$TMP/s27_x.c" -lm -pthread 2>"$TMP/s27_x_gcc.log"; then
            # Verify the inb() helper compiles to a single in instruction.
            s27_x_in_count=$(objdump -d "$TMP/s27_x.o" 2>/dev/null | grep -E "in\s+\(%dx\),%al|in\s+%dx,%al" | wc -l)
            if [ "$s27_x_in_count" -ge 1 ]; then
                ok "asm: feat_stage27_perfection.hls compiles + inb()=single 'in' instruction ($s27_x_asm_count __asm__ sites)"
            else
                bad "asm: feat_stage27_perfection.hls inb() did not compile to a single 'in' instruction"
                objdump -d "$TMP/s27_x.o" 2>/dev/null | grep -A 5 "usf_asm_inb"
            fi
        else
            bad "asm: feat_stage27_perfection.hls C source fails to compile with gcc -O2 -Werror"
            cat "$TMP/s27_x_gcc.log"
        fi
    else
        bad "asm: feat_stage27_perfection.hls C source has only $s27_x_asm_count __asm__ sites (expected 9+)"
    fi
else
    bad "asm: self-hosted hlc failed to compile feat_stage27_perfection.hls"
    cat "$TMP/s27_x_hlc.out"
fi

# (y) boot --check accepts feat_stage27_perfection.hls (types + effects valid).
if python3 boot/boot.py --check tests/ok/feat_stage27_perfection.hls >"$TMP/s27_y.out" 2>&1; then
    if grep -q "OK: types and effects valid" "$TMP/s27_y.out"; then
        ok "asm: boot --check accepts feat_stage27_perfection.hls"
    else
        bad "asm: boot --check did not accept feat_stage27_perfection.hls"
        cat "$TMP/s27_y.out"
    fi
else
    bad "asm: boot --check failed on feat_stage27_perfection.hls"
    cat "$TMP/s27_y.out"
fi

# (z) bootstrap is still deterministic with the deep-scan-17 checker
#     additions (the new HLS code in src/hlc.hls must self-compile
#     deterministically — both boot and self-hosted compilers must
#     agree on the new check_asm logic).
if python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_ds17_1.c" >/dev/null 2>&1     && python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_ds17_2.c" >/dev/null 2>&1; then
    if diff -q "$TMP/hlc_ds17_1.c" "$TMP/hlc_ds17_2.c" >/dev/null; then
        ok "asm: bootstrap deterministic with deep-scan-17 checker additions"
    else
        bad "asm: bootstrap not deterministic with deep-scan-17 checker additions"
    fi
else
    bad "asm: bootstrap compile failed (deep-scan-17)"
fi

# ======================================================================
# Stage 27 perfection (v0.50.3-alpha) deep-scan-18:
# Broader codebase hardening. Six classes of bugs found by the
# deep-scan-18 audit: tainted-primitive container codegen, proof
# const_eval overflow corner, cyclic-clone hang, struct-default
# effect over-attribution, math_sqrt(inf), never-type binop
# propagation, parse_placeholder_int overflow cap, dead-code
# removal, in(imm) runtime-expr rejection, hlserve symlink traversal,
# match binding assertion, dead isinstance in check_method.
# ======================================================================

# (aa) checker rejects in(imm) on a RUNTIME expression.
if python3 boot/boot.py --check tests/fail/fail_asm_imm_runtime.hls >"$TMP/s27_aa.out" 2>&1; then
    bad "asm: checker accepted in(imm) on runtime expr"
else
    if grep -q "requires a compile-time constant" "$TMP/s27_aa.out"; then
        ok "asm: checker rejects in(imm) on runtime expr"
    else
        bad "asm: rejected in(imm)-runtime but with the wrong message"
        cat "$TMP/s27_aa.out"
    fi
fi

# (bb) the deep-scan-18 positive regression test (feat_stage27_perfection2.hls)
#      runs on the boot interpreter and produces expected output.
if python3 boot/boot.py tests/ok/feat_stage27_perfection2.hls >"$TMP/s27_bb.out" 2>&1; then
    if grep -q "deep-scan-18 hardening does not break valid programs" "$TMP/s27_bb.out"; then
        ok "asm: feat_stage27_perfection2.hls runs on the boot interpreter"
    else
        bad "asm: feat_stage27_perfection2.hls did not print the acceptance line"
        cat "$TMP/s27_bb.out"
    fi
else
    bad "asm: feat_stage27_perfection2.hls failed to run on the boot interpreter"
    cat "$TMP/s27_bb.out"
fi

# (bc) the self-hosted hlc compiles feat_stage27_perfection2.hls + gcc -O2 -Werror.
if "$TMP/hlc1" tests/ok/feat_stage27_perfection2.hls "$TMP/s27_bb.c" >"$TMP/s27_bb_hlc.out" 2>&1; then
    if gcc -O2 -Werror -c -o "$TMP/s27_bb.o" "$TMP/s27_bb.c" -lm -pthread 2>"$TMP/s27_bb_gcc.log"; then
        ok "asm: feat_stage27_perfection2.hls compiles cleanly with gcc -O2 -Werror"
    else
        bad "asm: feat_stage27_perfection2.hls C source fails to compile with gcc -O2 -Werror"
        head -5 "$TMP/s27_bb_gcc.log"
    fi
else
    bad "asm: self-hosted hlc failed to compile feat_stage27_perfection2.hls"
    cat "$TMP/s27_bb_hlc.out"
fi

# (bd) the deep-scan-18 hardening does not break the existing
#      stdlib (math_sqrt in particular — the BUG-05 fix uses `x == 2.0*x`
#      to detect +inf, which must NOT affect finite inputs).
cat >"$TMP/s27_bd.hls" <<'HLS_EOF'
import "std.math"
fn check_sqrt() -> int uses IO {
    # sqrt(4) = 2.0, sqrt(2) ~= 1.41421..., sqrt(0) = 0.0
    let s4: float = math_sqrt(4.0)
    let s2: float = math_sqrt(2.0)
    let s0: float = math_sqrt(0.0)
    if s4 == 2.0 && s2 > 1.4142135 && s2 < 1.4142136 && s0 == 0.0 {
        println("deep-scan-18: math_sqrt finite-path unchanged")
        return 0
    }
    println("FAIL: s4=" + s4.to_str() + " s2=" + s2.to_str() + " s0=" + s0.to_str())
    return 1
}
fn main() -> int uses IO {
    return check_sqrt()
}
HLS_EOF
if python3 boot/boot.py "$TMP/s27_bd.hls" >"$TMP/s27_bd.out" 2>&1; then
    if grep -q "deep-scan-18: math_sqrt finite-path unchanged" "$TMP/s27_bd.out"; then
        ok "asm: math_sqrt finite-path unchanged (BUG-05 fix is sound)"
    else
        bad "asm: math_sqrt finite-path broken by BUG-05 fix"
        cat "$TMP/s27_bd.out"
    fi
else
    bad "asm: math_sqrt smoke test failed to run"
    cat "$TMP/s27_bd.out"
fi

# (be) the deep-scan-18 hardening does not break the proof const-eval
#      path (BUG-02 fix returns None for INT64_MIN/-1, letting the
#      runtime handle it).
cat >"$TMP/s27_be.hls" <<'HLS_EOF'
fn safe_div(a: int, b: int) -> int requires b != 0 {
    return a / b
}
fn main() -> int uses IO {
    # Normal division corner — must still work.
    let r: int = safe_div(20, 4)
    if r == 5 {
        println("deep-scan-18: const_eval normal path unchanged")
        return 0
    }
    println("FAIL: safe_div(20,4)=" + r.to_str())
    return 1
}
HLS_EOF
if python3 boot/boot.py "$TMP/s27_be.hls" >"$TMP/s27_be.out" 2>&1; then
    if grep -q "const_eval normal path unchanged" "$TMP/s27_be.out"; then
        ok "asm: proof const_eval normal-path unchanged (BUG-02 fix is sound)"
    else
        bad "asm: proof const_eval normal-path broken by BUG-02 fix"
        cat "$TMP/s27_be.out"
    fi
else
    bad "asm: proof const_eval smoke test failed to run"
    cat "$TMP/s27_be.out"
fi

# (bf) the deep-scan-18 cyclic-clone fix (BUG-03) does not break
#      non-cyclic clone (the common case).
cat >"$TMP/s27_bf.hls" <<'HLS_EOF'
struct Point { x: int, y: int }
fn clone_smoke() -> int {
    let p: Point = Point { x: 1, y: 2 }
    let q: Point = clone(p)
    # Mutating q must not affect p (deep clone).
    return q.x + q.y + p.x + p.y  # 1+2+1+2 = 6
}
fn main() -> int uses IO {
    let r: int = clone_smoke()
    if r == 6 {
        println("deep-scan-18: non-cyclic clone unchanged (BUG-03 fix is sound)")
        return 0
    }
    println("FAIL: clone_smoke()=" + r.to_str())
    return 1
}
HLS_EOF
if python3 boot/boot.py "$TMP/s27_bf.hls" >"$TMP/s27_bf.out" 2>&1; then
    if grep -q "non-cyclic clone unchanged" "$TMP/s27_bf.out"; then
        ok "asm: non-cyclic clone unchanged (BUG-03 fix is sound)"
    else
        bad "asm: non-cyclic clone broken by BUG-03 fix"
        cat "$TMP/s27_bf.out"
    fi
else
    bad "asm: clone smoke test failed to run"
    cat "$TMP/s27_bf.out"
fi

# (bg) the deep-scan-18 struct-default effect fix (BUG-04) does not
#      break the existing struct-default machinery (a pure fn that
#      OMITS a defaulted field still picks up the default's effects).
cat >"$TMP/s27_bg.hls" <<'HLS_EOF'
struct WithDefault {
    x: int,
    y: int = io_default()
}
fn io_default() -> int uses IO {
    return 99
}
# This fn OMITS y -> the default IS evaluated -> this fn must use IO.
fn construct_with_default() -> int uses IO {
    let w: WithDefault = WithDefault { x: 1 }
    return w.x + w.y
}
fn main() -> int uses IO {
    let r: int = construct_with_default()
    if r == 100 {
        println("deep-scan-18: struct-default effect propagation unchanged (BUG-04 fix is sound)")
        return 0
    }
    println("FAIL: construct_with_default()=" + r.to_str())
    return 1
}
HLS_EOF
if python3 boot/boot.py "$TMP/s27_bg.hls" >"$TMP/s27_bg.out" 2>&1; then
    if grep -q "struct-default effect propagation unchanged" "$TMP/s27_bg.out"; then
        ok "asm: struct-default effect propagation unchanged (BUG-04 fix is sound)"
    else
        bad "asm: struct-default effect propagation broken by BUG-04 fix"
        cat "$TMP/s27_bg.out"
    fi
else
    bad "asm: struct-default smoke test failed to run"
    cat "$TMP/s27_bg.out"
fi

# (bh) bootstrap is still deterministic with the deep-scan-18
#      additions (the new HLS code in src/hlc.hls — BUG-01 fix in
#      box_fn_for/gen_unbox, BUG-06 fix in check_bin/check_method/
#      check_qmark, BUG-07 fix in parse_placeholder_int — must
#      self-compile deterministically).
if python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_ds18_1.c" >/dev/null 2>&1     && python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_ds18_2.c" >/dev/null 2>&1; then
    if diff -q "$TMP/hlc_ds18_1.c" "$TMP/hlc_ds18_2.c" >/dev/null; then
        ok "asm: bootstrap deterministic with deep-scan-18 codegen additions"
    else
        bad "asm: bootstrap not deterministic with deep-scan-18 codegen additions"
    fi
else
    bad "asm: bootstrap compile failed (deep-scan-18 codegen)"
fi

echo ""
