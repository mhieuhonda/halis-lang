#!/usr/bin/env bash
# ============================================================================
# run_tests.sh — Full test suite for Halis (HLS)
#  1. Stage-0: run the ok programs (compare to snapshot if present)
#  2. Stage-0: reject the fail programs (expect compile errors)
#  3. Self-compile: hlc.hls (via Stage-0) compiles each ok program -> C
#     -> gcc -> run native -> COMPARE TO INTERPRETER OUTPUT (differential)
#  3b. Stage 8-beta: native memory-stress — RSS must stay flat under a
#      256 MB address-space limit (end-of-arena refcounted runtime)
#  4. Bootstrap: hlc self-compiles twice -> the two C outputs must be identical
# ============================================================================
set -u
cd "$(dirname "$0")/.."
PASS=0
FAIL=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM HUP

ok()   { PASS=$((PASS+1)); echo "  [PASS] $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  [FAIL] $1"; }

echo "=== 1. Stage-0: valid programs ==="
for f in tests/ok/*.hls; do
    name=$(basename "$f" .hls)
    out=$(python3 boot/boot.py "$f" </dev/null 2>/dev/null); code=$?
    if [ $code -eq 0 ] || [ $code -eq 101 ]; then
        snap="tests/snapshots/$name.txt"
        if [ -f "$snap" ] && [ "$out" != "$(cat "$snap")" ]; then
            bad "$name (differs from snapshot)"
        else
            ok "$name"
        fi
    else
        bad "$name (exit=$code)"
    fi
done

echo "=== 2. Stage-0: programs EXPECTED TO FAIL ==="
for f in tests/fail/*.hls; do
    name=$(basename "$f" .hls)
    err=$(python3 boot/boot.py --check "$f" 2>&1 >/dev/null); code=$?
    if [ $code -eq 1 ]; then
        ok "$name -> $err"
    else
        bad "$name (should be rejected but exit=$code)"
    fi
done

echo "=== 3. Self-compile + differential testing (interpreter vs native) ==="
for f in tests/ok/*.hls; do
    name=$(basename "$f" .hls)
    # Stage 10 release: redirect stdin from /dev/null so tests that use
    # read_line() don't hang waiting for input. The interpreter reads
    # EOF (returns empty tainted[str]); the native binary does the same
    # — the differential test still compares apples to apples.
    interp_out=$(python3 boot/boot.py "$f" </dev/null 2>/dev/null); interp_code=$?
    if ! python3 boot/boot.py src/hlc.hls "$f" "$TMP/$name.c" >/dev/null 2>&1; then
        bad "$name (hlc compile failed)"
        continue
    fi
    if ! gcc -O2 -o "$TMP/$name.bin" "$TMP/$name.c" -lm 2>"$TMP/$name.gcc"; then
        bad "$name (gcc error)"
        continue
    fi
    nat_out=$("$TMP/$name.bin" </dev/null 2>/dev/null); nat_code=$?
    if [ "$interp_out" == "$nat_out" ] && [ "$interp_code" == "$nat_code" ]; then
        ok "$name (native matches interpreter)"
    else
        bad "$name (interp=$interp_code nat=$nat_code)"
        diff <(echo "$interp_out") <(echo "$nat_out") | head -4
    fi
done

echo "=== 3b. Stage 8-beta memory-stress (native RSS must stay flat) ==="
# The Stage 8 acceptance criterion: a memory-stress program does not
# increase RSS. The stress binary churns ~500k allocations of every heap
# shape; under the old arena model it would exhaust a 256 MB address
# space, with refcounting it completes with delta == 0 pages.
if python3 boot/boot.py src/hlc.hls tests/memcheck/stress_leak.hls "$TMP/stress.c" >/dev/null 2>&1 \
    && gcc -O2 -o "$TMP/stress" "$TMP/stress.c" -lm 2>/dev/null; then
    stress_out=$(bash -c "ulimit -v 262144; \"$TMP/stress\"" 2>/dev/null); stress_rc=$?
    if [ $stress_rc -eq 0 ]; then
        delta=$(echo "$stress_out" | grep "rss_delta_pages=" | cut -d= -f2)
        if [ "$delta" -le 1024 ] 2>/dev/null; then
            ok "stress_leak under 256MB ulimit (rss delta=${delta} pages)"
        else
            bad "stress_leak RSS grew by ${delta} pages"
        fi
    else
        bad "stress_leak failed under 256MB ulimit (exit=$stress_rc)"
    fi
else
    bad "stress_leak (compile failed)"
fi

echo "=== 4. Differential test for examples (with data file) ==="
python3 boot/boot.py examples/wordcount.hls examples/data.txt > "$TMP/wc_interp.txt" 2>/dev/null
python3 boot/boot.py src/hlc.hls examples/wordcount.hls "$TMP/wc.c" >/dev/null 2>&1
gcc -O2 -o "$TMP/wc.bin" "$TMP/wc.c" -lm 2>/dev/null
"$TMP/wc.bin" examples/data.txt > "$TMP/wc_nat.txt" 2>/dev/null
if diff -q "$TMP/wc_interp.txt" "$TMP/wc_nat.txt" >/dev/null 2>&1; then
    ok "wordcount (native vs interpreter)"
else
    bad "wordcount"
fi

echo "=== 4b. Stage 14 release: tooling (hlfmt idempotent + hllint cfaware) ==="
# hlfmt must be idempotent: running twice = running once. We test by
# formatting once into a temp file, then formatting again into a second
# temp file, and diffing them. (The original source files may predate
# hlfmt's canonical form, so checking `hlfmt -c` against the original
# is too strict.)
fmt_fail=0
for f in examples/*.hls tests/ok/*.hls tests/fail/*.hls; do
    name=$(basename "$f" .hls)
    cp "$f" "$TMP/p1.hls"
    python3 tools/hlfmt.py -w "$TMP/p1.hls" >/dev/null 2>&1
    cp "$TMP/p1.hls" "$TMP/p2.hls"
    python3 tools/hlfmt.py -w "$TMP/p2.hls" >/dev/null 2>&1
    if diff -q "$TMP/p1.hls" "$TMP/p2.hls" >/dev/null 2>&1; then
        ok "hlfmt idempotent: $name"
    else
        bad "hlfmt non-idempotent: $name"
        fmt_fail=1
    fi
done
# hllint L005 control-flow-aware: 3 warnings expected on the cfaware test.
l005_out=$(python3 tools/hllint.py --rule L005 tests/ok/feat_lint_cfaware.hls 2>&1)
l005_count=$(echo "$l005_out" | grep -c "L005" || true)
if [ "$l005_count" -eq 3 ]; then
    ok "hllint L005 cfaware: 3 warnings (unsafe_unwrap, unsafe_in_loop, unwrap_literal)"
else
    bad "hllint L005 cfaware: expected 3 warnings, got $l005_count"
    echo "$l005_out"
fi
# hllint must NOT warn on safe unwraps (cases 2 and 3 in the test file).
if echo "$l005_out" | grep -q "safe_unwrap\|safe_unwrap_option"; then
    bad "hllint L005 cfaware: false positive on a safe unwrap"
else
    ok "hllint L005 cfaware: no false positives on safe unwraps"
fi

echo "=== 5. BOOTSTRAP: hlc self-compiles (fixed-point) ==="
echo "  [5.1] Stage-0 runs hlc.hls to compile hlc.hls itself..."
if python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_s1.c" >/dev/null 2>&1; then
    ok "hlc.hls self-compiles via Stage-0"
else
    bad "hlc.hls self-compiles via Stage-0"
fi
echo "  [5.2] Compile the first C pass into native hlc..."
if gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_s1.c" -lm 2>/dev/null; then
    ok "gcc compiles native hlc"
else
    bad "gcc compiles native hlc"
fi
echo "  [5.3] Native hlc re-compiles hlc.hls (pass 2)..."
"$TMP/hlc1" src/hlc.hls "$TMP/hlc_s2.c" 2>/dev/null
if [ $? -eq 0 ]; then
    ok "native hlc compiles hlc.hls"
else
    bad "native hlc compiles hlc.hls"
fi
if diff -q "$TMP/hlc_s1.c" "$TMP/hlc_s2.c" >/dev/null 2>&1; then
    ok "BOOTSTRAP DETERMINISTIC: two passes produce identical output"
else
    bad "two passes produce different output!"
fi
echo "  [5.4] Native hlc compiles a sample program..."
nat=""
interp=""
"$TMP/hlc1" examples/fibonacci.hls "$TMP/fib.c" >/dev/null 2>&1 \
    && gcc -O2 -o "$TMP/fib" "$TMP/fib.c" -lm 2>/dev/null \
    && nat=$("$TMP/fib" 2>/dev/null) \
    && interp=$(python3 boot/boot.py examples/fibonacci.hls 2>/dev/null)
if [ -n "$nat" ] && [ -n "$interp" ] && [ "$nat" == "$interp" ]; then
    ok "native hlc compiles + runs fibonacci correctly"
else
    bad "native hlc compiles fibonacci"
fi

echo ""
echo "=========================================="
echo "RESULT: $PASS PASS / $FAIL FAIL"
echo "=========================================="
[ $FAIL -eq 0 ]
