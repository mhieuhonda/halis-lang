#!/usr/bin/env bash
# Suite suite_06_advanced - verbatim section of the original tests/run_tests.sh
# (split for maintainability; sourced by tests/run_tests.sh in order).

echo "=== 14. Stage 28: stack-frame layout control (kernel code) ==="
# Stage 28 (v0.45.0-alpha): #[no_red_zone], #[irq_handler], #[stack_size(N)]
# attributes parse and emit the right C __attribute__ on the function
# signature. The kernel_irq_demo.hls example exercises all three.

# (a) the example file parses with all three attributes (via boot).
if python3 boot/boot.py examples/kernel_irq_demo.hls >/dev/null 2>&1; then
    ok "stack: examples/kernel_irq_demo.hls parses via boot"
else
    bad "stack: examples/kernel_irq_demo.hls failed to parse via boot"
fi

# (b) hlc compiles it to a C source that contains __attribute__((interrupt)).
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
if "$TMP/hlc1" examples/kernel_irq_demo.hls "$TMP/kernel_irq.c" >/dev/null 2>&1; then
    if grep -q "__attribute__((interrupt))" "$TMP/kernel_irq.c"; then
        ok "stack: C source has __attribute__((interrupt))"
    else
        bad "stack: __attribute__((interrupt)) missing from C source"
    fi
    # (c) the C source compiles under freestanding flags (no libc, no SSE).
    if gcc -O2 -Wno-attributes -ffreestanding -mgeneral-regs-only \
        -mno-red-zone -fno-stack-protector -fno-pic -c \
        -o "$TMP/kernel_irq.o" "$TMP/kernel_irq.c" 2>"$TMP/kernel_gcc.log"; then
        ok "stack: C source compiles under freestanding flags (-ffreestanding -mgeneral-regs-only -mno-red-zone)"
    else
        bad "stack: freestanding compile failed"
        cat "$TMP/kernel_gcc.log" | head -5
    fi
else
    bad "stack: hlc compile failed"
fi

# (d) #[stack_size(N)] checker fires on a too-small bound.
cat > "$TMP/stack_fail.hls" << 'SEOF'
#[stack_size(8)]
fn too_big(x: int) -> int {
    let a: int = x + 1
    let b: int = a + 2
    let c: int = b + 3
    return a + b + c
}

fn main() -> int {
    return too_big(10)
}
SEOF
err=$(python3 boot/boot.py src/hlc.hls "$TMP/stack_fail.hls" "$TMP/stack_fail.c" 2>&1)
if echo "$err" | grep -q "#\[stack_size(8)\] violated"; then
    ok "stack: #[stack_size(8)] checker fires on too-large frame"
else
    bad "stack: #[stack_size(N)] checker did NOT fire (got: $err)"
fi

# (e) #[irq_handler] checker rejects a non-void return.
cat > "$TMP/irq_bad.hls" << 'IEOF'
#[irq_handler]
fn bad_irq(frame: int) -> int {
    return 0
}

fn main() -> int {
    return 0
}
IEOF
err=$(python3 boot/boot.py src/hlc.hls "$TMP/irq_bad.hls" "$TMP/irq_bad.c" 2>&1)
if echo "$err" | grep -q "irq_handler.*void"; then
    ok "stack: #[irq_handler] rejects non-void return"
else
    bad "stack: #[irq_handler] did NOT reject non-void (got: $err)"
fi

# (f) make stack-acceptance runs end-to-end.
if make stack-acceptance >"$TMP/stack_acc.log" 2>&1; then
    if grep -q "ACCEPTANCE OK" "$TMP/stack_acc.log"; then
        ok "stack: make stack-acceptance runs end-to-end"
    else
        bad "stack: make stack-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/stack_acc.log"
    fi
else
    bad "stack: make stack-acceptance failed"
    tail -10 "$TMP/stack_acc.log"
fi

# (g) bootstrap still works after the src/hlc.hls Stage 28 changes.
if python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_s28.c" >/dev/null 2>&1 \
    && diff -q "$TMP/hlc_s28.c" "$TMP/hlc_nat.c" >/dev/null; then
    ok "stack: bootstrap deterministic with Stage 28 changes"
else
    bad "stack: bootstrap not deterministic with Stage 28 changes"
fi

echo "=== 15. Stage 29: inline / hot / cold attributes + --opt-stats ==="
# Stage 29 (v0.46.0-alpha): #[inline(always)], #[inline(never)], #[hot],
# #[cold] attributes parse and emit the right C __attribute__. --opt-stats
# prints the per-function decision table. LTO honours inline(always) and
# inline(never). hllint warns (L011) on inline(always) > 50 statements.

# (a) the example file parses with all four attributes (via boot).
if python3 boot/boot.py examples/inline_attrs_demo.hls >/dev/null 2>&1; then
    ok "inline: examples/inline_attrs_demo.hls parses via boot"
else
    bad "inline: examples/inline_attrs_demo.hls failed to parse via boot"
fi

# (b) hlc compiles it to a C source that contains the right C attributes.
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
if "$TMP/hlc1" examples/inline_attrs_demo.hls "$TMP/inline_attrs.c" >/dev/null 2>&1; then
    if grep -q "static inline __attribute__((always_inline)) int64_t usf_small_hot_helper" "$TMP/inline_attrs.c" \
        && grep -q "__attribute__((noinline)) int64_t usf_big_rare_path" "$TMP/inline_attrs.c" \
        && grep -q "__attribute__((hot)) int64_t usf_hot_loop" "$TMP/inline_attrs.c" \
        && grep -q "__attribute__((cold)) int64_t usf_cold_path" "$TMP/inline_attrs.c"; then
        ok "inline: C source has all four C attributes (always_inline / noinline / hot / cold)"
    else
        bad "inline: C source missing one or more C attributes"
        grep -E "always_inline|noinline|hot|cold" "$TMP/inline_attrs.c" | head -8
    fi
    # (c) the C source compiles cleanly with -Wno-attributes.
    if gcc -O2 -Wno-attributes -o "$TMP/inline_attrs_bin" "$TMP/inline_attrs.c" -lm -pthread 2>"$TMP/inline_gcc.log"; then
        ok "inline: C source compiles cleanly (-Wno-attributes)"
        # And runs without crashing.
        if "$TMP/inline_attrs_bin" >/dev/null 2>&1; then
            ok "inline: compiled binary runs cleanly"
        else
            bad "inline: compiled binary crashed at runtime"
        fi
    else
        bad "inline: C source fails to compile"
        cat "$TMP/inline_gcc.log" | head -5
    fi
else
    bad "inline: hlc compile failed"
fi

# (d) --opt-stats prints the per-function decision table.
if "$TMP/hlc1" --opt-stats examples/inline_attrs_demo.hls "$TMP/inline_optstats.c" \
    >"$TMP/inline_optstats.log" 2>&1; then
    if grep -q "ALWAYS" "$TMP/inline_optstats.log" \
        && grep -q "NEVER" "$TMP/inline_optstats.log" \
        && grep -q "HOT" "$TMP/inline_optstats.log" \
        && grep -q "COLD" "$TMP/inline_optstats.log"; then
        ok "inline: --opt-stats prints inline/hot/cold decisions"
    else
        bad "inline: --opt-stats missing one or more decisions"
        cat "$TMP/inline_optstats.log" | head -20
    fi
else
    bad "inline: --opt-stats invocation failed"
fi

# (e) --lto honours #[inline(always)] (small_hot_helper inlined) and
#     #[inline(never)] (big_rare_path NOT inlined).
if "$TMP/hlc1" --lto --lto-stats examples/inline_attrs_demo.hls "$TMP/inline_lto.c" \
    >"$TMP/inline_lto.log" 2>&1; then
    if grep -q "small_hot_helper" "$TMP/inline_lto.log" \
        && ! grep -q "usf_small_hot_helper(" "$TMP/inline_lto.c"; then
        ok "inline: --lto inlines small_hot_helper (#[inline(always)])"
    else
        bad "inline: --lto did not inline small_hot_helper properly"
    fi
    if grep -q "usf_big_rare_path(" "$TMP/inline_lto.c"; then
        ok "inline: --lto keeps big_rare_path out-of-line (#[inline(never)])"
    else
        bad "inline: --lto did NOT keep big_rare_path out-of-line"
    fi
else
    bad "inline: --lto invocation failed"
fi

# (f) hllint warns (L011) on #[inline(always)] > 50 statements.
printf '#[inline(always)]
fn big_inline(n: int) -> int {
  let mut s: int = 0
  let mut i: int = 0
' > "$TMP/l011_test.hls"
for n in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45 46 47 48 49 50 51; do
    printf '  s = s + i
  i = i + 1
' >> "$TMP/l011_test.hls"
done
printf '  return s
}

fn main() -> int {
  return big_inline(10)
}
' >> "$TMP/l011_test.hls"
if python3 tools/hllint.py --rule L011 "$TMP/l011_test.hls" 2>&1 | grep -q "L011"; then
    ok "inline: hllint L011 warns on #[inline(always)] > 50 statements"
else
    bad "inline: hllint L011 did NOT warn on large #[inline(always)]"
fi

# (g) hllint does NOT warn on #[inline(always)] <= 50 statements.
printf '#[inline(always)]
fn small_inline(x: int) -> int {
  return x + 1
}

fn main() -> int {
  return small_inline(10)
}
' > "$TMP/l011_small.hls"
if ! python3 tools/hllint.py --rule L011 "$TMP/l011_small.hls" 2>&1 | grep -q "L011"; then
    ok "inline: hllint L011 does NOT warn on small #[inline(always)]"
else
    bad "inline: hllint L011 FALSE POSITIVE on small #[inline(always)]"
fi

# (h) make inline-acceptance runs end-to-end.
if make inline-acceptance >"$TMP/inline_acc.log" 2>&1; then
    if grep -q "ACCEPTANCE OK" "$TMP/inline_acc.log"; then
        ok "inline: make inline-acceptance runs end-to-end"
    else
        bad "inline: make inline-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/inline_acc.log"
    fi
else
    bad "inline: make inline-acceptance failed"
    tail -10 "$TMP/inline_acc.log"
fi

# (i) bootstrap still works after the src/hlc.hls Stage 29 changes
#     (the self-hosted compiler must remain deterministic).
if python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_s29.c" >/dev/null 2>&1 \
    && diff -q "$TMP/hlc_s29.c" "$TMP/hlc_nat.c" >/dev/null; then
    ok "inline: bootstrap deterministic with Stage 29 changes"
else
    bad "inline: bootstrap not deterministic with Stage 29 changes"
fi

echo "=== 16. Stage 30: boxed-vs-stack layout analysis (escape analysis) ==="
# Stage 30 (v0.47.0-alpha): #[stack]/#[boxed] let-binding attributes,
# automatic escape analysis (non-escaping list[primitive] bindings are
# stack-allocated), checker-enforced soundness (a stack value can NEVER
# outlive its creating frame), the --opt-stats layout report, and the
# fibonacci zero-heap-objects acceptance.

# (a) the demo example runs via the boot interpreter.
if python3 boot/boot.py examples/stack_layout_demo.hls >"$TMP/s30_demo.out" 2>&1; then
    ok "stage30: examples/stack_layout_demo.hls runs via boot"
else
    bad "stage30: examples/stack_layout_demo.hls failed via boot"
    cat "$TMP/s30_demo.out" | head -5
fi

# (b) the native hlc compiles it; the C source contains the typed frame
#     arrays (the #[stack] + auto layouts) and the accessors.
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
if "$TMP/hlc1" examples/stack_layout_demo.hls "$TMP/s30_demo.c" >/dev/null 2>&1; then
    if grep -q "int64_t u_window\[2\];" "$TMP/s30_demo.c" \
        && grep -q "int64_t u_coeffs\[3\];" "$TMP/s30_demo.c" \
        && grep -q "static inline int64_t hlc_sg_i64" "$TMP/s30_demo.c"; then
        ok "stage30: C source has typed frame arrays (#[stack] + auto) + accessors"
    else
        bad "stage30: C source missing stack arrays / accessors"
        grep -n "u_window\|u_coeffs\|hlc_sg_i64" "$TMP/s30_demo.c" | head -5
    fi
    # (c) the C source compiles and runs; output matches the interpreter.
    if gcc -O2 -o "$TMP/s30_demo_bin" "$TMP/s30_demo.c" -lm -pthread 2>"$TMP/s30_gcc.log"; then
        "$TMP/s30_demo_bin" >"$TMP/s30_demo_native.out" 2>&1
        if diff -q "$TMP/s30_demo.out" "$TMP/s30_demo_native.out" >/dev/null; then
            ok "stage30: differential (interpreter == native) for the demo"
        else
            bad "stage30: differential mismatch on the demo"
            diff "$TMP/s30_demo.out" "$TMP/s30_demo_native.out" | head -5
        fi
    else
        bad "stage30: C source fails to compile"
        cat "$TMP/s30_gcc.log" | head -5
    fi
else
    bad "stage30: hlc compile of the demo failed"
fi

# (d) --opt-stats prints the layout decision table.
if "$TMP/hlc1" --opt-stats examples/stack_layout_demo.hls "$TMP/s30_stats.c" \
    >"$TMP/s30_stats.log" 2>&1; then
    if grep -q "list layout decisions" "$TMP/s30_stats.log" \
        && grep -q "stack-allocated list bindings" "$TMP/s30_stats.log" \
        && grep -q "fib_stack::window" "$TMP/s30_stats.log" \
        && grep -q "#\[stack\] forced (escape-free)" "$TMP/s30_stats.log" \
        && grep -q "escape-free (auto)" "$TMP/s30_stats.log" \
        && grep -q "escapes: return value" "$TMP/s30_stats.log" \
        && grep -q "#\[boxed\] forced heap layout" "$TMP/s30_stats.log"; then
        ok "stage30: --opt-stats prints the per-binding layout decisions"
    else
        bad "stage30: --opt-stats layout section incomplete"
        grep -A8 "list layout" "$TMP/s30_stats.log" | head -12
    fi
else
    bad "stage30: --opt-stats invocation failed"
fi

# (e) the auto mode fires without any attribute: feat_stage30_auto.hls's
#     escape-free bindings are stack arrays, the escaping one is a heap list.
if "$TMP/hlc1" tests/ok/feat_stage30_auto.hls "$TMP/s30_auto.c" >/dev/null 2>&1; then
    if grep -q "int64_t u_coeffs\[3\];" "$TMP/s30_auto.c" \
        && grep -q "hl_list\* u_xs" "$TMP/s30_auto.c"; then
        ok "stage30: auto analysis (no attributes) picks stack + heap layouts"
    else
        bad "stage30: auto analysis layouts missing in C source"
        grep -n "u_coeffs\|u_xs" "$TMP/s30_auto.c" | head -5
    fi
else
    bad "stage30: hlc compile of feat_stage30_auto.hls failed"
fi

# (f) the ok-program with #[stack] is differential (section 1/3/4a also
#     cover it; here we assert the stack arrays made it into the C).
if "$TMP/hlc1" tests/ok/feat_stage30_stack.hls "$TMP/s30_ok.c" >/dev/null 2>&1; then
    if grep -q "int64_t u_window\[2\];" "$TMP/s30_ok.c" \
        && grep -q "bool u_bs\[2\];" "$TMP/s30_ok.c" \
        && grep -q "double u_fs\[2\];" "$TMP/s30_ok.c"; then
        ok "stage30: feat_stage30_stack.hls lowers int/float/bool stack arrays"
    else
        bad "stage30: feat_stage30_stack.hls missing one of the typed arrays"
        grep -n "u_window\|u_bs\|u_fs" "$TMP/s30_ok.c" | head -5
    fi
else
    bad "stage30: hlc compile of feat_stage30_stack.hls failed"
fi

# (g) the fail-programs: boot --check AND the native compiler must both
#     reject every escape class.
LEAKS=0
for f in tests/fail/fail_stack_*.hls; do
    if python3 boot/boot.py --check "$f" >/dev/null 2>&1; then
        echo "  boot --check LEAK: $f"
        LEAKS=$((LEAKS + 1))
    fi
done
if [ $LEAKS -eq 0 ]; then
    ok "stage30: boot --check rejects all 13 fail_stack_* programs"
else
    bad "stage30: boot --check leaked $LEAKS fail_stack_* programs"
fi
LEAKS=0
for f in tests/fail/fail_stack_*.hls; do
    if "$TMP/hlc1" "$f" "$TMP/s30_leak.c" >/dev/null 2>&1; then
        echo "  native hlc LEAK: $f"
        LEAKS=$((LEAKS + 1))
    fi
done
if [ $LEAKS -eq 0 ]; then
    ok "stage30: native hlc rejects all 13 fail_stack_* programs"
else
    bad "stage30: native hlc leaked $LEAKS fail_stack_* programs"
fi

# (h) the escape error message names the binding, the use class and line.
if "$TMP/hlc1" tests/fail/fail_stack_escape_return.hls "$TMP/s30_err.c" \
    >"$TMP/s30_err.log" 2>&1; then
    bad "stage30: escape-via-return should not compile"
else
    if grep -q "escapes its creating frame" "$TMP/s30_err.log" \
        && grep -q "return value" "$TMP/s30_err.log" \
        && grep -q "NEVER outlive" "$TMP/s30_err.log"; then
        ok "stage30: escape error names binding + use class + the guarantee"
    else
        bad "stage30: escape error message incomplete"
        cat "$TMP/s30_err.log" | head -3
    fi
fi

# (i) the fibonacci acceptance: zero heap objects in the inner loop.
#     (The full gate also runs via `make escape-acceptance`.)
if "$TMP/hlc1" examples/fibonacci.hls "$TMP/s30_fib.c" >/dev/null 2>&1; then
    if grep -q "int64_t u_window\[2\];" "$TMP/s30_fib.c"; then
        ok "stage30: fibonacci fib_loop carries the #[stack] frame array"
    else
        bad "stage30: fibonacci missing the #[stack] frame array"
    fi
    FIB_N=$(awk '/int64_t usf_fib_loop/,/^}/' "$TMP/s30_fib.c" | grep -c "hl_list_new")
    SPIN_N=$(awk '/int64_t usf_spin_fib/,/^}/' "$TMP/s30_fib.c" | grep -c "hl_list_new")
    if [ "$FIB_N" = "0" ] && [ "$SPIN_N" = "0" ]; then
        ok "stage30: usf_fib_loop / usf_spin_fib contain zero hl_list_new calls"
    else
        bad "stage30: heap list construction leaked into the acceptance fns"
    fi
else
    bad "stage30: hlc compile of fibonacci.hls failed"
fi
if [ -x "$TMP/hlc1" ]; then
    if gcc -O2 -o "$TMP/s30_fib_wrap" "$TMP/s30_fib.c" tests/memcheck/malloc_count_wrap.c \
        -Wl,--wrap=malloc -Wl,--wrap=realloc -lm -pthread 2>/dev/null; then
        "$TMP/s30_fib_wrap" >/dev/null 2>"$TMP/s30_fib_wrap.err" || true
        MC=$(grep -o 'HL_MALLOC_COUNT=[0-9]*' "$TMP/s30_fib_wrap.err" | cut -d= -f2)
        if [ -n "$MC" ] && [ "$MC" -le 128 ]; then
            ok "stage30: malloc interposer: $MC allocations across 20k inner-loop rounds (constant)"
        else
            bad "stage30: malloc interposer count too high: $MC"
        fi
    else
        bad "stage30: interposer build failed"
    fi
fi

# (j) make escape-acceptance runs end-to-end.
if make escape-acceptance >"$TMP/s30_acc.log" 2>&1; then
    if grep -q "ACCEPTANCE OK: Stage 30" "$TMP/s30_acc.log"; then
        ok "stage30: make escape-acceptance runs end-to-end"
    else
        bad "stage30: make escape-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/s30_acc.log"
    fi
else
    bad "stage30: make escape-acceptance failed"
    tail -10 "$TMP/s30_acc.log"
fi

# (k) bootstrap still deterministic with the Stage 30 changes (the
#     compiler's own print_opt_stats now uses #[stack] + auto layouts).
if python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_s30.c" >/dev/null 2>&1 \
    && diff -q "$TMP/hlc_s30.c" "$TMP/hlc_nat.c" >/dev/null; then
    ok "stage30: bootstrap deterministic with Stage 30 changes"
else
    bad "stage30: bootstrap not deterministic with Stage 30 changes"
fi

echo ""
echo "=== 17. Stage 31: verified tail calls (#[tail_call]) ==="

# (a) the feature matrix runs via the interpreter (fast depths).
if s31out=$(python3 boot/boot.py tests/ok/feat_stage31_tail.hls 2>&1); then
    if echo "$s31out" | grep -q "fib_tail(10000) = 271496360" \
        && echo "$s31out" | grep -q "collatz_steps(27) = 111" \
        && echo "$s31out" | grep -q "sum_tail(50000, 0) = 1250175003"; then
        ok "stage31: feature matrix (fib / collatz / float / bool / while / panic-literal) via boot"
    else
        bad "stage31: feature matrix produced wrong values"
        echo "$s31out" | head -5
    fi
else
    bad "stage31: feature matrix failed to run"
fi

# (b) the C source has the loop label + the rebind goto and ZERO
#     recursive calls in usf_fib_tail (a jmp, not a call).
if "$TMP/hlc1" tests/ok/feat_stage31_tail.hls "$TMP/s31.c" >/dev/null 2>&1; then
    if grep -q "^hl_tail_restart:;" "$TMP/s31.c" \
        && grep -q "goto hl_tail_restart;" "$TMP/s31.c" \
        && [ "$(awk '/^int64_t usf_fib_tail\(int64_t u_n_p/,/^}$/' "$TMP/s31.c" \
            | grep -c 'usf_fib_tail(')" -le 1 ]; then
        ok "stage31: C source lowers the tail call to a parameter-rebinding goto"
    else
        bad "stage31: the tail-call transform is missing from the C source"
    fi
else
    bad "stage31: native hlc failed to compile the feature matrix"
fi

# (c) native + differential: the tail test's outputs match the
#     interpreter's byte for byte (already covered by section 3's
#     differential loop; this re-states it for the section report).
nat31=$("$TMP/hlc1" tests/ok/feat_stage31_tail.hls "$TMP/s31b.c" >/dev/null 2>&1 \
    && gcc -O2 -o "$TMP/s31.bin" "$TMP/s31b.c" -lm -pthread 2>/dev/null \
    && "$TMP/s31.bin" 2>/dev/null)
if [ "$nat31" == "$s31out" ]; then
    ok "stage31: differential (native tail loop == interpreter trampoline)"
else
    bad "stage31: differential mismatch"
    diff <(echo "$s31out") <(echo "$nat31") | head -4
fi

# (d) constant stack: the native binary runs 1M-deep under a 1 MB
#     stack (a real 1M call chain needs ~48 MB).
if bash -c "ulimit -s 1024; '$TMP/s31.bin' >/dev/null 2>&1"; then
    ok "stage31: native tail recursion under ulimit -s 1024 (1 MB stack)"
else
    bad "stage31: native binary blew the 1 MB stack limit"
fi

# (e) the interpreter trampoline runs deep with zero Python recursion.
cat > "$TMP/s31_trampoline.py" <<'PYEOF31'
import sys
sys.path.insert(0, ".")
sys.setrecursionlimit(200)  # a 100k tail depth must NOT need Python frames
from boot.boot import load_program
from boot.checker import check
from boot.interp import Interp
import io
prog = load_program("tests/ok/feat_stage31_tail.hls")
check(prog)
interp = Interp(prog, [b"tests/ok/feat_stage31_tail.hls"], io.BytesIO())
r = interp.call_fn("fib_tail", [100000, 0, 1])
assert r == 911435502, r
print("trampoline ok at depth 100000 with recursionlimit 200")
PYEOF31
if python3 "$TMP/s31_trampoline.py" >/dev/null 2>&1; then
    ok "stage31: interpreter trampoline, 100k deep under recursionlimit 200"
else
    bad "stage31: interpreter trampoline failed (RecursionError?)"
fi

# (f) the verifier rejects every fail_tail_call_* program (boot + native).
s31fail=0
for f in tests/fail/fail_tail_call_*.hls; do
    if ! python3 boot/boot.py --check "$f" >/dev/null 2>&1; then
        s31fail=$((s31fail+1))
    else
        bad "stage31: $(basename $f) was NOT rejected by the boot checker"
    fi
done
if [ $s31fail -eq 13 ] && [ -x "$TMP/hlc1" ]; then
    n31fail=0
    for f in tests/fail/fail_tail_call_*.hls; do
        if ! "$TMP/hlc1" "$f" "$TMP/s31f.c" >/dev/null 2>&1; then
            n31fail=$((n31fail+1))
        else
            bad "stage31: $(basename $f) was NOT rejected by the native compiler"
        fi
    done
    if [ $n31fail -eq 13 ]; then
        ok "stage31: all 13 fail_tail_call_* programs rejected (boot + native)"
    fi
elif [ $s31fail -eq 13 ]; then
    ok "stage31: all 13 fail_tail_call_* programs rejected by the boot checker"
fi

# (g) --opt-stats prints the verified tail-call decisions.
if "$TMP/hlc1" --opt-stats examples/fibonacci.hls "$TMP/s31os.c" >/dev/null 2>&1 \
    && "$TMP/hlc1" --opt-stats examples/fibonacci.hls "$TMP/s31os.c" 2>&1 \
        | grep -q "verified tail calls (Stage 31)"; then
    ok "stage31: --opt-stats prints the verified tail-call table"
else
    bad "stage31: --opt-stats missing the tail-call table"
fi

# (h) hllint L012 fires on a large #[tail_call] fn and not on small ones.
if [ "$(python3 tools/hllint.py tests/ok/feat_stage31_tail.hls 2>/dev/null | grep -c 'L012')" -eq 0 ]; then
    ok "stage31: hllint L012 silent on small #[tail_call] functions"
else
    bad "stage31: hllint L012 false positive on small functions"
fi

# (i) make tail-acceptance runs end-to-end.
if make tail-acceptance >"$TMP/s31_acc.log" 2>&1; then
    if grep -q "ACCEPTANCE OK" "$TMP/s31_acc.log"; then
        ok "stage31: make tail-acceptance runs end-to-end"
    else
        bad "stage31: make tail-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/s31_acc.log"
    fi
else
    bad "stage31: make tail-acceptance failed"
    tail -10 "$TMP/s31_acc.log"
fi

echo ""
echo "=== 16. Stage 26: RISC-V 64 backend (RVV + bare-metal) ==="
# Stage 26 (v0.49.0-alpha): the RVV intrinsic emission in src/hlc.hls
# generates <riscv_vector.h> intrinsics (__riscv_vadd_vv_i32m1,
# __riscv_vsub_vv_i32m1, __riscv_vmul_vv_i32m1, __riscv_vmin_vv_i32m1,
# __riscv_vmax_vv_i32m1, __riscv_vadd_vv_f64m1, __riscv_vsub_vv_f64m1,
# __riscv_vmul_vv_f64m1) when --target-feature rvv is passed. The
# hlcross.py orchestrator accepts riscv64gc-unknown-linux-gnu and
# riscv64-unknown-none targets and applies -march/-mabi/-ffreestanding
# flags.

# (a) --target-feature rvv emits RVV intrinsics in the C source.
if python3 boot/boot.py src/hlc.hls examples/riscv_demo.hls "$TMP/riscv_rvv.c" --target-feature rvv >/dev/null 2>&1; then
    rvv_count=$(grep -c "__riscv_vadd_vv_i32m1\|__riscv_vsub_vv_i32m1\|__riscv_vmul_vv_i32m1\|__riscv_vmin_vv_i32m1\|__riscv_vmax_vv_i32m1" "$TMP/riscv_rvv.c" 2>/dev/null || echo 0)
    if [ "$rvv_count" -gt 0 ]; then
        ok "riscv: --target-feature rvv emits RVV intrinsics ($rvv_count sites)"
    else
        bad "riscv: --target-feature rvv did not emit any RVV intrinsics"
    fi
    # (b) the C source includes <riscv_vector.h>.
    if grep -q "<riscv_vector.h>" "$TMP/riscv_rvv.c"; then
        ok "riscv: --target-feature rvv includes <riscv_vector.h>"
    else
        bad "riscv: <riscv_vector.h> not included in RVV C source"
    fi
    # (c) the C source has the #if __riscv && __riscv_v guard.
    if grep -q "defined(__riscv) && defined(__riscv_v)\|defined(__riscv_v)" "$TMP/riscv_rvv.c"; then
        ok "riscv: C source has __riscv && __riscv_v guard"
    else
        bad "riscv: missing __riscv/__riscv_v guard"
    fi
    # (d) the RVV C source still compiles on the x86_64 host (uses
    #     the scalar fallback in the #else branch).
    if gcc -O2 -o "$TMP/riscv_rvv_x86" "$TMP/riscv_rvv.c" -lm -pthread 2>/dev/null; then
        if "$TMP/riscv_rvv_x86" >/dev/null 2>&1; then
            ok "riscv: RVV C source compiles + runs on x86_64 (scalar fallback)"
        else
            bad "riscv: RVV C source compiles on x86_64 but doesn't run cleanly"
        fi
    else
        bad "riscv: RVV C source fails to compile on x86_64"
    fi
else
    bad "riscv: --target-feature rvv compile failed"
fi

# (e) hlcross --list-targets prints the RISC-V target set.
list_out=$(python3 tools/hlcross.py --list-targets 2>&1)
if echo "$list_out" | grep -q "riscv64gc-unknown-linux-gnu" \
    && echo "$list_out" | grep -q "riscv64-unknown-none"; then
    ok "riscv: hlcross --list-targets includes RISC-V targets"
else
    bad "riscv: hlcross --list-targets missing RISC-V targets"
fi

# (f) hlcross accepts the riscv64 alias.
if python3 tools/hlcross.py examples/hello.hls "$TMP/riscv_alias" --target riscv64 --keep-c "$TMP/riscv_alias.c" 2>&1 | grep -q "no cross-linker found\|ELF riscv64"; then
    ok "riscv: hlcross accepts the 'riscv64' alias"
else
    bad "riscv: hlcross did not accept the 'riscv64' alias"
fi

# (g) the hlriscv.py helper exists and has a main().
if python3 -c "import sys; sys.path.insert(0, 'tools'); import hlriscv; assert hasattr(hlriscv, 'main'); print('OK')" 2>&1 | grep -q OK; then
    ok "riscv: tools/hlriscv.py imports and has main()"
else
    bad "riscv: tools/hlriscv.py import failed"
fi

# (h) hlriscv.py --list-targets prints the RVV target + the bare-metal info.
if python3 tools/hlriscv.py --list-targets 2>&1 | grep -q "rvv" \
    && python3 tools/hlriscv.py --list-targets 2>&1 | grep -q "riscv64-unknown-none"; then
    ok "riscv: hlriscv --list-targets prints RVV feature + bare-metal target"
else
    bad "riscv: hlriscv --list-targets missing RVV or bare-metal"
fi

# (i) hlriscv.py compiles simd_bench.hls with RVV; the C source
#     contains RVV intrinsics and includes <riscv_vector.h>.
if python3 tools/hlriscv.py benchmarks/simd_bench.hls "$TMP/riscv_bench" \
    --target riscv64gc-unknown-linux-gnu --target-feature rvv \
    --keep-c "$TMP/riscv_bench.c" >"$TMP/riscv_bench.log" 2>&1 \
    || [ $? -eq 3 ]; then
    if grep -q "__riscv_vadd_vv_i32m1\|__riscv_vsub_vv_i32m1\|__riscv_vmul_vv_i32m1" "$TMP/riscv_bench.c" 2>/dev/null \
        && grep -q "<riscv_vector.h>" "$TMP/riscv_bench.c" 2>/dev/null; then
        ok "riscv: hlriscv produces C source with RVV intrinsics + riscv_vector.h"
    else
        bad "riscv: hlriscv C source missing RVV intrinsics"
    fi
else
    bad "riscv: hlriscv compile failed unexpectedly"
    cat "$TMP/riscv_bench.log" | head -5
fi

# (j) make riscv-acceptance runs end-to-end (RVV codegen gate).
if make riscv-acceptance >"$TMP/riscv_acc.log" 2>&1; then
    if grep -q "ACCEPTANCE OK" "$TMP/riscv_acc.log"; then
        ok "riscv: make riscv-acceptance runs end-to-end"
    else
        bad "riscv: make riscv-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/riscv_acc.log"
    fi
else
    bad "riscv: make riscv-acceptance failed"
    tail -10 "$TMP/riscv_acc.log"
fi

# (k) make riscv-bare-acceptance runs end-to-end (zero-libc gate).
if make riscv-bare-acceptance >"$TMP/riscv_bare_acc.log" 2>&1; then
    if grep -q "ACCEPTANCE OK" "$TMP/riscv_bare_acc.log"; then
        ok "riscv: make riscv-bare-acceptance runs end-to-end"
    else
        bad "riscv: make riscv-bare-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/riscv_bare_acc.log"
    fi
else
    bad "riscv: make riscv-bare-acceptance failed"
    tail -10 "$TMP/riscv_bare_acc.log"
fi

# (l) the feat_stage26_riscv.hls test runs cleanly via the boot interpreter
#     (portable path) AND with --target-feature rvv (which sets
#     has_feature("rvv") -> true).
if python3 boot/boot.py tests/ok/feat_stage26_riscv.hls >"$TMP/s26_portable.out" 2>&1; then
    if grep -q "ACCEPTANCE OK" "$TMP/s26_portable.out"; then
        ok "riscv: feat_stage26_riscv.hls runs on portable path"
    else
        bad "riscv: feat_stage26_riscv.hls did not pass on portable path"
        tail -5 "$TMP/s26_portable.out"
    fi
else
    bad "riscv: feat_stage26_riscv.hls failed to run on portable path"
    tail -10 "$TMP/s26_portable.out"
fi
if python3 boot/boot.py --target-feature rvv tests/ok/feat_stage26_riscv.hls >"$TMP/s26_rvv.out" 2>&1; then
    if grep -q 'has_feature("rvv"): true' "$TMP/s26_rvv.out" \
        && grep -q "ACCEPTANCE OK" "$TMP/s26_rvv.out"; then
        ok "riscv: feat_stage26_riscv.hls runs with --target-feature rvv (has_feature(rvv)=true)"
    else
        bad "riscv: feat_stage26_riscv.hls did not pass with --target-feature rvv"
        tail -5 "$TMP/s26_rvv.out"
    fi
else
    bad "riscv: feat_stage26_riscv.hls failed with --target-feature rvv"
    tail -10 "$TMP/s26_rvv.out"
fi

# (m) bootstrap still works after the src/hlc.hls RVV changes (the
#     self-hosted compiler must remain deterministic).
if python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_rvv.c" >/dev/null 2>&1 \
    && python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_rvv2.c" --target-feature rvv >/dev/null 2>&1; then
    # The two outputs must be byte-identical when --target-feature is
    # not used by hlc.hls itself (which it isn't — hlc.hls doesn't use
    # std.simd).
    if diff -q "$TMP/hlc_rvv.c" "$TMP/hlc_rvv2.c" >/dev/null; then
        ok "riscv: bootstrap deterministic with --target-feature rvv"
    else
        bad "riscv: bootstrap not deterministic with --target-feature rvv"
    fi
else
    bad "riscv: bootstrap compile failed"
fi

# (n) has_feature("rvv") const-folds correctly under the native compiler.
if "$TMP/hlc1" --target-feature rvv tests/ok/feat_stage26_riscv.hls "$TMP/s26_native.c" >/dev/null 2>&1; then
    if grep -q "has_feature(\"rvv\"): true" "$TMP/s26_native.c" 2>/dev/null \
        || grep -q 'has_feature(.rvv.): true' "$TMP/s26_native.c" 2>/dev/null; then
        ok "riscv: native has_feature(\"rvv\") const-folds to true under --target-feature rvv"
    else
        # The const-fold replaces has_feature("rvv") with `true` or `1`
        # — verify the runtime print path emits "true" by compiling +
        # running the native binary.
        if gcc -O2 -o "$TMP/s26_native_bin" "$TMP/s26_native.c" -lm -pthread 2>/dev/null; then
            if "$TMP/s26_native_bin" 2>&1 | grep -q 'has_feature("rvv"): true'; then
                ok "riscv: native has_feature(\"rvv\") const-folds to true under --target-feature rvv"
            else
                bad "riscv: native has_feature(\"rvv\") did not const-fold to true"
            fi
        else
            bad "riscv: native C source fails to compile on x86_64"
        fi
    fi
else
    bad "riscv: native --target-feature rvv compile failed"
fi

echo ""
