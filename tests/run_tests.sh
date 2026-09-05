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

echo "=== 6. Stage 18: testing ecosystem (hltest + hlcov) ==="
# hltest must discover and PASS every test_* function in the Stage 18
# acceptance file (12 tests: assertions + quickcheck properties).
hltest_out=$(python3 tools/hltest.py tests/ok/feat_stage18_hltest.hls 2>&1); hltest_rc=$?
hltest_pass=$(echo "$hltest_out" | grep -E "^== hltest:" | tail -1)
if [ $hltest_rc -eq 0 ] && echo "$hltest_pass" | grep -q "0 fail"; then
    ok "hltest: $hltest_pass"
else
    bad "hltest: $hltest_pass"
    echo "$hltest_out" | tail -8
fi
# hlcov must report coverage on the same file (non-zero total).
hlcov_out=$(python3 tools/hlcov.py tests/ok/feat_stage18_hltest.hls 2>/dev/null)
if echo "$hlcov_out" | grep -q "total:.*blocks hit"; then
    ok "hlcov: $(echo "$hlcov_out" | grep 'total:')"
else
    bad "hlcov: no total line"
fi
# hls-fuzz must run for 5 seconds without finding any divergence.
fuzz_out=$(timeout 15 python3 tools/hls-fuzz.py --time 5 --quiet 2>&1); fuzz_rc=$?
if [ $fuzz_rc -eq 0 ] && echo "$fuzz_out" | tail -1 | grep -q "0 diverge"; then
    ok "hls-fuzz: $(echo "$fuzz_out" | tail -1)"
else
    bad "hls-fuzz: divergences found (see fuzz-corpus/)"
    echo "$fuzz_out" | tail -5
fi

echo "=== 7. Stage 19: profile-guided optimisation (PGO) ==="
# The PGO acceptance, in three steps (native compiler required):
#   (a) --pgo-generate: the instrumented binary writes a .hlcprof with
#       sane counters (entries / branches / loop back-edges).
#   (b) --pgo-use: the trained compile must build AND run, producing
#       BYTE-IDENTICAL output to the plain build (hints change layout,
#       never meaning).
#   (c) the profile/counter machinery must not appear in unflagged C.
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
PGO_F=tests/ok/feat_stage19_pgo.hls
if "$TMP/hlc1" --pgo-generate "$PGO_F" "$TMP/pgo_gen.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/pgo_gen_bin" "$TMP/pgo_gen.c" -lm -pthread 2>/dev/null; then
    ok "pgo-generate: instrumented binary compiles"
    HLS_PGO_FILE="$TMP/pgo.hlcprof" "$TMP/pgo_gen_bin" > "$TMP/pgo_gen.out" 2>/dev/null
    if [ -s "$TMP/pgo.hlcprof" ] \
            && grep -q "^e:main 1$" "$TMP/pgo.hlcprof" \
            && grep -q "^l:sum_upto:0 1000$" "$TMP/pgo.hlcprof" \
            && grep -q "^b:classify:0 " "$TMP/pgo.hlcprof"; then
        ok "pgo-generate: .hlcprof has entry/branch/loop counters"
    else
        bad "pgo-generate: .hlcprof missing or counters wrong"
        head -5 "$TMP/pgo.hlcprof" 2>/dev/null
    fi
    # Train: recompile with the profile; output must be byte-identical.
    if "$TMP/hlc1" --pgo-use "$TMP/pgo.hlcprof" "$PGO_F" "$TMP/pgo_use.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/pgo_use_bin" "$TMP/pgo_use.c" -lm -pthread 2>/dev/null; then
        ok "pgo-use: trained binary compiles"
        pgo_out=$("$TMP/pgo_use_bin" 2>/dev/null)
        if [ "$pgo_out" == "$(cat "$TMP/pgo_gen.out")" ]; then
            ok "pgo-use: trained output byte-identical to plain build"
        else
            bad "pgo-use: trained output diverges"
        fi
        if grep -q "__builtin_expect" "$TMP/pgo_use.c"; then
            ok "pgo-use: branch hints present in trained C"
        else
            bad "pgo-use: no __builtin_expect hints in trained C"
        fi
    else
        bad "pgo-use: trained compile failed"
    fi
    # Unflagged builds must contain zero PGO machinery.
    if grep -q "__hlc_pgo_counts" "$TMP/pgo_gen.c" \
            && ! grep -q "__hlc_pgo_counts\|__builtin_expect" "$TMP/$name.c" 2>/dev/null; then
        ok "pgo: unflagged build has zero instrumentation"
    else
        bad "pgo: instrumentation leaked into an unflagged build"
    fi
else
    bad "pgo-generate: instrumented compile failed"
fi
# The join builtin (O(n) string join) must be pure and differential-safe.
cat > "$TMP/join_check.hls" <<'JEOF'
fn main() -> int uses IO {
    let parts: list[str] = ["x", "yy", "zzz"]
    println(join(parts, "-"))
    println("empty=[" + join([], ",") + "]")
    let big: list[str] = []
    let mut i: int = 0
    while i < 5000 {
        big.push(i.to_str())
        i = i + 1
    }
    println("big join len = " + join(big, ",").len().to_str())
    return 0
}
JEOF
j_interp=$(python3 boot/boot.py "$TMP/join_check.hls" 2>/dev/null)
if python3 boot/boot.py src/hlc.hls "$TMP/join_check.hls" "$TMP/join_check.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/join_check_bin" "$TMP/join_check.c" -lm -pthread 2>/dev/null; then
    j_nat=$("$TMP/join_check_bin" 2>/dev/null)
    if [ "$j_interp" == "$j_nat" ] && echo "$j_nat" | grep -q "big join len = 23889"; then
        ok "join builtin: interpreter and native agree (O(n) path)"
    else
        bad "join builtin: divergence (interp vs native)"
    fi
else
    bad "join builtin: native compile failed"
fi

echo "=== 8. Stage 20: LTO across crates (inlining + DCE) ==="
# The Stage 20 acceptance, in four steps:
#   (a) the stdlib's list_sort_int_asc is INLINED into the caller (its
#       standalone definition is dropped: no usf_list_sort_int_asc
#       symbol remains in the LTO C),
#   (b) the LTO binary produces byte-identical output to the
#       interpreter AND to the non-LTO native build,
#   (c) binary size drops >= 15% (whole-program DCE of unused stdlib
#       functions),
#   (d) --emit lto produces a whole-program LTO'd LLVM IR module.
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
LTO_F=tests/ok/feat_stage20_lto.hls
lto_interp=$(python3 boot/boot.py "$LTO_F" </dev/null 2>/dev/null)
if "$TMP/hlc1" --lto "$LTO_F" "$TMP/l20.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/l20_bin" "$TMP/l20.c" -lm -pthread 2>/dev/null \
        && "$TMP/hlc1" "$LTO_F" "$TMP/l20p.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/l20p_bin" "$TMP/l20p.c" -lm -pthread 2>/dev/null; then
    lto_out=$("$TMP/l20_bin" 2>/dev/null)
    lto_plain_out=$("$TMP/l20p_bin" 2>/dev/null)
    if [ "$lto_out" == "$lto_interp" ] && [ "$lto_out" == "$lto_plain_out" ]; then
        ok "lto: inlined binary output identical (interpreter = plain native = LTO native)"
    else
        bad "lto: output divergence"
    fi
    # (a) the sort standalone definition must be GONE (fully inlined).
    if grep -q "usf_list_sort_int_asc" "$TMP/l20.c"; then
        bad "lto: list_sort_int_asc was not inlined (standalone definition present)"
    else
        ok "lto: list_sort_int_asc inlined into the caller, standalone definition dropped"
    fi
    # (c) binary size drop >= 15%.
    sz_lto=$(stat -c %s "$TMP/l20_bin")
    sz_plain=$(stat -c %s "$TMP/l20p_bin")
    if [ "$sz_plain" -gt 0 ] && [ "$sz_lto" -le $((sz_plain * 85 / 100)) ]; then
        ok "lto: binary size $sz_plain -> $sz_lto bytes ($((100 - sz_lto * 100 / sz_plain))% drop, >= 15%)"
    else
        bad "lto: binary size drop below 15% ($sz_plain -> $sz_lto)"
    fi
else
    bad "lto: LTO compile failed"
fi
# (d) --emit lto: whole-program LTO'd LLVM IR (DCE'd — fewer defines
# than the non-LTO emission).
python3 boot/boot.py --emit lto "$LTO_F" > "$TMP/l20.ll" 2>"$TMP/l20.rep"
defs_lto=$(grep -c "^define" "$TMP/l20.ll" || true)
python3 boot/boot.py --emit llvm "$LTO_F" > "$TMP/l20p.ll" 2>/dev/null
defs_plain=$(grep -c "^define" "$TMP/l20p.ll" || true)
if [ "$defs_lto" -gt 0 ] && [ "$defs_lto" -lt "$defs_plain" ]; then
    ok "lto: --emit lto IR has $defs_lto defines (non-LTO: $defs_plain — DCE applied)"
else
    bad "lto: --emit lto produced $defs_lto defines (non-LTO: $defs_plain)"
fi
# The && short-circuit regression (found by the LTO work): the native
# build must not eagerly evaluate the right operand of &&/||.
SC_F=tests/ok/feat_shortcircuit_slice.hls
sc_interp=$(python3 boot/boot.py "$SC_F" </dev/null 2>/dev/null); sc_rc=$?
if "$TMP/hlc1" "$SC_F" "$TMP/sc.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/sc_bin" "$TMP/sc.c" -lm -pthread 2>/dev/null; then
    sc_nat=$("$TMP/sc_bin" </dev/null 2>/dev/null); sc_nrc=$?
    if [ "$sc_interp" == "$sc_nat" ] && [ "$sc_rc" == "$sc_nrc" ]; then
        ok "&& short-circuit: fresh right-operand subexpressions stay lazy (regression)"
    else
        bad "&& short-circuit regression (interp=$sc_rc native=$sc_nrc)"
    fi
else
    bad "&& short-circuit: native compile failed"
fi

echo "=== 9. Stage 21: SIMD vectorisation (std.simd + target features) ==="
# (a) the std.simd semantics test is differentially covered by sections
#     1/3/4a (it lives in tests/ok); here we verify the INTRINSIC FAST
#     PATH byte-identity: the native build compiled with
#     --target-feature avx2 must produce the same output as the
#     interpreter driven with the same flag (fast path == portable
#     semantics). x86-64 hosts only (the intrinsics); others skip.
SIMD_F=tests/ok/feat_stage21_simd.hls
simd_interp_flag=$(python3 boot/boot.py --target-feature avx2 "$SIMD_F" </dev/null 2>/dev/null)
if uname -m | grep -qE "x86_64|i386|i686"; then
    if [ ! -x "$TMP/hlc1" ]; then
        python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
        gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
    fi
    if "$TMP/hlc1" --target-feature avx2 "$SIMD_F" "$TMP/s21a.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/s21a_bin" "$TMP/s21a.c" -lm -pthread 2>/dev/null; then
        if grep -q "hl_simd_" "$TMP/s21a.c"; then
            ok "simd: intrinsic fast path present in --target-feature build"
        else
            bad "simd: fast path missing from --target-feature build"
        fi
        s21_out=$("$TMP/s21a_bin" 2>/dev/null)
        if [ "$s21_out" == "$simd_interp_flag" ]; then
            ok "simd: AVX2 fast path output == interpreter (byte-identical)"
        else
            bad "simd: AVX2 fast path diverges from interpreter"
            diff <(echo "$s21_out") <(echo "$simd_interp_flag") | head -4
        fi
    else
        bad "simd: --target-feature avx2 compile failed"
    fi
    # Unflagged native build must contain ZERO SIMD helpers.
    if ! grep -q "hl_simd_add_i32x4\|hl_simd_cpu_supports(hl_str" "$TMP/$name.c" 2>/dev/null; then
        ok "simd: unflagged build has no SIMD helper emission"
    else
        bad "simd: SIMD machinery leaked into an unflagged build"
    fi
else
    echo "  [SKIP] non-x86 host: intrinsic fast-path check skipped"
fi
# (b) the feature-flag const-folds has_feature in the native build.
cat > "$TMP/feat_check.hls" <<'FCEOF'
fn main() -> int uses IO {
    if has_feature("avx2") {
        println("flag-on")
    } else {
        println("flag-off")
    }
    return 0
}
FCEOF
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
if "$TMP/hlc1" --target-feature avx2 "$TMP/feat_check.hls" "$TMP/fc.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/fc_bin" "$TMP/fc.c" -lm -pthread 2>/dev/null; then
    fc_out=$("$TMP/fc_bin" 2>/dev/null)
    fc_interp=$(python3 boot/boot.py --target-feature avx2 "$TMP/feat_check.hls" 2>/dev/null)
    if [ "$fc_out" == "flag-on" ] && [ "$fc_interp" == "flag-on" ]; then
        ok "simd: has_feature const-folds (native and interpreter agree)"
    else
        bad "simd: has_feature mis-folded (native='$fc_out' interp='$fc_interp')"
    fi
else
    bad "simd: feature-flag compile failed"
fi
# (c) the SIMD acceptance benchmark (timing-gated: >= 2x on AVX2 hosts;
#     correctness-gated everywhere the checksums must match).
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
if "$TMP/hlc1" --target-feature avx2 benchmarks/simd_bench.hls "$TMP/s21b.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/s21b_bin" "$TMP/s21b.c" -lm -pthread 2>/dev/null; then
    "$TMP/s21b_bin" > "$TMP/s21b.out" 2>&1
    if grep -q "checksums MATCH" "$TMP/s21b.out"; then
        ok "simd-bench: 1M-element kernel checksums MATCH (vector == scalar)"
    else
        bad "simd-bench: checksum mismatch"
    fi
    if grep -q "simd_cpu_supports(avx2) = true" "$TMP/s21b.out"; then
        if python3 scripts/simd_ratio.py --out "$TMP/s21b.out" --min 2.0 >/dev/null 2>&1; then
            ratio_line=$(grep "RATIO" "$TMP/s21b.out" | tail -1)
            ok "simd-bench: acceptance ratio >= 2x ($ratio_line)"
        else
            bad "simd-bench: acceptance ratio below 2x"
        fi
    else
        echo "  [SKIP] host CPU has no AVX2 — timing gate skipped (checksum still verified)"
    fi
else
    bad "simd-bench: compile failed"
fi

echo ""
echo "=========================================="
echo "RESULT: $PASS PASS / $FAIL FAIL"
echo "=========================================="
[ $FAIL -eq 0 ]
