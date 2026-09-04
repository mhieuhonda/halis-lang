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
    if ! gcc -O2 -o "$TMP/$name.bin" "$TMP/$name.c" -lm -pthread 2>"$TMP/$name.gcc"; then
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
    && gcc -O2 -o "$TMP/stress" "$TMP/stress.c" -lm -pthread 2>/dev/null; then
    stress_out=$(bash -c "ulimit -v 262144; \"$TMP/stress\"" 2>/dev/null); stress_rc=$?
    if [ $stress_rc -eq 0 ]; then
        delta=$(echo "$stress_out" | grep "rss_delta_pages=" | cut -d= -f2)
        # Deep-scan-11 fix: if the stress binary produced no
        # `rss_delta_pages=` line (e.g. it crashed before printing
        # the result), `delta` is empty. The old test
        # `[ "" -le 1024 ]` errors with "integer expression expected"
        # (suppressed by 2>/dev/null) and falls through to the
        # `else` branch, printing the confusing "RSS grew by  pages"
        # message with a blank. Distinguish the two failure modes.
        if [ -z "$delta" ]; then
            bad "stress_leak: no rss_delta_pages= line in output (stress binary crashed before reporting?)"
        elif [ "$delta" -le 1024 ] 2>/dev/null; then
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
gcc -O2 -o "$TMP/wc.bin" "$TMP/wc.c" -lm -pthread 2>/dev/null
"$TMP/wc.bin" examples/data.txt > "$TMP/wc_nat.txt" 2>/dev/null
if diff -q "$TMP/wc_interp.txt" "$TMP/wc_nat.txt" >/dev/null 2>&1; then
    ok "wordcount (native vs interpreter)"
else
    bad "wordcount"
fi

echo "=== 4a. Stage 16: NATIVE hlc compiles every ok program ==="
# Deep-scan-9 test-gap fix: section 3 above compiles the ok/ programs
# with the INTERPRETED compiler (Stage-0). The NATIVE binary was only
# exercised on hlc.hls itself + fibonacci — which let a heap-use-after-
# free in the native codegen (double field-release for match/qmark .t)
# hide until feat_clone_deep crashed it. From now on, the native
# compiler must compile (and, where possible, run) EVERY ok program.
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
nat_ok=0
nat_bad=0
for f in tests/ok/*.hls; do
    name=$(basename "$f" .hls)
    if "$TMP/hlc1" "$f" "$TMP/$name.nat.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/$name.nat.bin" "$TMP/$name.nat.c" -lm -pthread 2>/dev/null; then
        nat_ok=$((nat_ok+1))
    else
        nat_bad=$((nat_bad+1))
        bad "native hlc compile: $name"
    fi
done
if [ $nat_bad -eq 0 ]; then
    ok "native hlc compiles + gcc-builds all $nat_ok ok programs"
fi

echo "=== 4b. Stage 17: contracts + proof (fast-mode differential) ==="
# The proof elision must be semantics-preserving: the -O fast build
# (checks elided where PROVEN) must produce byte-identical output to
# the interpreter for the contracted test programs.
# Deep-scan-10 (Stage-17 perfection): the soundness regressions are
# also compiled -O fast — their checks must NOT be elided, so both
# implementations must panic identically (101) byte for byte.
for f in tests/ok/feat_contract_*.hls tests/ok/feat_proof_elide.hls \
         tests/ok/feat_proof_sound_*.hls; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .hls)
    if [ ! -x "$TMP/hlc1" ]; then
        python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
        gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
    fi
    interp_out=$(timeout 60 python3 boot/boot.py "$f" </dev/null 2>/dev/null); interp_rc=$?
    if "$TMP/hlc1" --fast "$f" "$TMP/$name.fast.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/$name.fast.bin" "$TMP/$name.fast.c" -lm -pthread 2>/dev/null; then
        fast_out=$(timeout 60 "$TMP/$name.fast.bin" </dev/null 2>/dev/null); fast_rc=$?
        if [ "$interp_out" == "$fast_out" ] && [ "$interp_rc" == "$fast_rc" ]; then
            ok "$name (-O fast output identical to interpreter)"
        else
            bad "$name (-O fast diverges: interp=$interp_rc fast=$fast_rc)"
        fi
    else
        bad "$name (fast compile failed)"
    fi
done
# hlprove must run cleanly on the acceptance example and report elisions.
if python3 tools/hlprove.py examples/hmac_proven.hls >/dev/null 2>&1; then
    ok "hlprove runs on the HMAC acceptance example"
else
    bad "hlprove failed on the HMAC acceptance example"
fi
# hlmodel must exhaustively check the demo state machine.
if python3 tools/hlmodel.py examples/conn_machine.hls --fn step --invariant all_valid --init Closed >/dev/null 2>&1; then
    ok "hlmodel exhaustive check of the demo state machine"
else
    bad "hlmodel failed on the demo state machine"
fi
# Runtime contract checking (--contracts) must catch a violated requires.
cat > "$TMP/rt_contract.hls" <<'RTSEOF'
fn risky(n: int) -> int
    requires n > 0
{
    return n * 2
}
fn main() -> int uses Args {
    let n: int = args().len() - 1
    return risky(n)
}
RTSEOF
rt_out=$(python3 boot/boot.py --contracts "$TMP/rt_contract.hls" </dev/null 2>&1 >/dev/null); rt_rc=$?
if [ $rt_rc -eq 101 ] && echo "$rt_out" | grep -q "contract violation: requires"; then
    ok "--contracts runtime requires violation panics cleanly (101)"
else
    bad "--contracts did not catch the violated requires (rc=$rt_rc)"
fi
# Stage-17 perfection (v0.30.0-alpha): the NATIVE backend now checks
# ENSURES at every return too (previously requires-only). Both
# implementations must panic identically on a violated postcondition.
cat > "$TMP/rt_ens.hls" <<'ENSEOF'
fn bad(x: int) -> int
    requires x > 0
    ensures result > 100
{
    return x
}
fn main() -> int uses IO {
    let a: int = bad(5)
    println("a=" + a.to_str())
    return 0
}
ENSEOF
ens_interp=$(python3 boot/boot.py --contracts "$TMP/rt_ens.hls" </dev/null 2>&1); ens_irc=$?
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
if "$TMP/hlc1" --contracts "$TMP/rt_ens.hls" "$TMP/rt_ens.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/rt_ens.bin" "$TMP/rt_ens.c" -lm -pthread 2>/dev/null; then
    ens_nat=$("$TMP/rt_ens.bin" </dev/null 2>/dev/null); ens_nrc=$?
    if [ $ens_irc -eq 101 ] && [ $ens_nrc -eq 101 ] \
            && echo "$ens_interp" | grep -q "contract violation: ensures of 'bad'"; then
        ok "native --contracts ensures violation panics identically (101)"
    else
        bad "native ensures check diverged (interp=$ens_irc nat=$ens_nrc)"
    fi
else
    bad "native --contracts ensures compile failed"
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
if gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_s1.c" -lm -pthread 2>/dev/null; then
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
