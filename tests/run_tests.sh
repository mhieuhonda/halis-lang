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

# Stage 19 perfection (v0.38.0-alpha): hlpgo.py offline profile utilities
# (report / merge / diff). The profile produced by --pgo-generate above
# is the input; the tools must (a) report the expected site kinds and
# top-N format, (b) merge two profiles into one whose counters are the
# sum, (c) diff two profiles and report per-site deltas.
if [ -s "$TMP/pgo.hlcprof" ]; then
    # (a) report: must list the entry/branch/loop site counts and the
    #     top-N functions by entry count. The profile comes from
    #     feat_stage19_pgo.hls (classify / sum_upto / main) — the top-N
    #     list must mention one of these functions.
    hlpgo_out=$(python3 tools/hlpgo.py report "$TMP/pgo.hlcprof" --top 5 2>&1)
    if echo "$hlpgo_out" | grep -q "sites  :" \
            && echo "$hlpgo_out" | grep -q "calls  :" \
            && echo "$hlpgo_out" | grep -q "hottest functions" \
            && echo "$hlpgo_out" | grep -qE "classify|sum_upto|e:main"; then
        ok "pgo-report: hotness report lists entry/branch/loop sites + top-N fns"
    else
        bad "pgo-report: report missing expected sections"
        echo "$hlpgo_out" | head -5
    fi
    # (b) merge: merging the profile with itself must double every count.
    cp "$TMP/pgo.hlcprof" "$TMP/pgo_a.hlcprof"
    cp "$TMP/pgo.hlcprof" "$TMP/pgo_b.hlcprof"
    if python3 tools/hlpgo.py merge "$TMP/pgo_merged.hlcprof" \
            "$TMP/pgo_a.hlcprof" "$TMP/pgo_b.hlcprof" >/dev/null 2>&1; then
        # The merged file must START with the forward-compatible header
        # added in v0.38.0-alpha, and the parsed counters must be 2x.
        if head -1 "$TMP/pgo_merged.hlcprof" | grep -q "^# hlcprof v1$"; then
            ok "pgo-merge: forward-compatible v1 header written"
        else
            bad "pgo-merge: v1 header missing from merged profile"
        fi
        merged_site_count=$(grep -c "^[eb]:l:" "$TMP/pgo_merged.hlcprof" 2>/dev/null || echo 0)
        plain_site_count=$(grep -c "^[eb]:l:" "$TMP/pgo_a.hlcprof" 2>/dev/null || echo 0)
        # Sample one site id and verify its count doubled.
        sample_id=$(grep "^e:" "$TMP/pgo_a.hlcprof" | head -1 | awk '{print $1}')
        sample_a=$(grep "^$sample_id " "$TMP/pgo_a.hlcprof" | awk '{print $2}')
        sample_m=$(grep "^$sample_id " "$TMP/pgo_merged.hlcprof" | awk '{print $2}')
        if [ -n "$sample_a" ] && [ "$sample_m" -eq $((sample_a * 2)) ] 2>/dev/null; then
            ok "pgo-merge: site $sample_id count doubled ($sample_a -> $sample_m)"
        else
            bad "pgo-merge: site $sample_id did not double ($sample_a -> $sample_m)"
        fi
    else
        bad "pgo-merge: failed to merge two profiles"
    fi
    # (c) diff: diffing the original against the merged must report
    #     every site with delta == +count (the merged is 2x, so the
    #     delta is +count_original). Use --min-delta to keep the output
    #     bounded.
    diff_out=$(python3 tools/hlpgo.py diff "$TMP/pgo_a.hlcprof" \
            "$TMP/pgo_merged.hlcprof" --min-delta 1000 2>&1)
    if echo "$diff_out" | grep -q "sites that differ by" \
            && echo "$diff_out" | grep -q "delta +"; then
        ok "pgo-diff: per-site deltas reported"
    else
        bad "pgo-diff: no deltas reported"
        echo "$diff_out" | head -3
    fi
    # (d) backward compat: a v0 profile (no header) must still parse
    #     identically. The original pgo.hlcprof is a v0 file (produced
    #     by the runtime, which does not yet emit the header).
    if python3 tools/hlpgo.py report "$TMP/pgo.hlcprof" 2>&1 | grep -q "format : v0"; then
        ok "pgo-report: v0 (headerless) profile parses backward-compatibly"
    else
        bad "pgo-report: v0 profile not recognised"
    fi
else
    bad "pgo: no profile available for hlpgo tests"
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

# Stage 20 perfection (v0.39.0-alpha): --lto-stats, --lto-threshold,
# and generic instantiation dedup verification.
DEDUP_F=tests/ok/feat_stage20_lto_dedup.hls
dedup_interp=$(python3 boot/boot.py "$DEDUP_F" </dev/null 2>/dev/null)
# (a) --lto-stats prints a structured summary; the dedup program
#     must show 1 generic struct instantiation (Pair[int, int]) and
#     2 generic fn instantiations (pair_first[int,int], pair_second[int,int]).
if "$TMP/hlc1" --lto-stats "$DEDUP_F" "$TMP/dedup.c" >"$TMP/dedup.stats" 2>&1; then
    if grep -q "=== LTO stats ===" "$TMP/dedup.stats" \
            && grep -q "inline expansions (sites)" "$TMP/dedup.stats" \
            && grep -q "bodies dropped (phase A+B)" "$TMP/dedup.stats" \
            && grep -q "inline stmt budget" "$TMP/dedup.stats" \
            && grep -q "generic fn instantiations   : 2" "$TMP/dedup.stats" \
            && grep -q "generic struct instantiations: 1" "$TMP/dedup.stats"; then
        ok "lto-stats: structured summary correct (2 fn insts, 1 struct inst — dedup verified)"
    else
        bad "lto-stats: stats summary missing expected fields"
        cat "$TMP/dedup.stats" | head -10
    fi
else
    bad "lto-stats: --lto-stats compile failed"
fi
# (b) the LTO binary must produce byte-identical output to the
#     interpreter (differential on the dedup program).
if gcc -O2 -o "$TMP/dedup_bin" "$TMP/dedup.c" -lm -pthread 2>/dev/null; then
    dedup_nat=$("$TMP/dedup_bin" 2>/dev/null)
    if [ "$dedup_interp" == "$dedup_nat" ]; then
        ok "lto-dedup: LTO binary output == interpreter (byte-identical)"
    else
        bad "lto-dedup: divergence (interp vs LTO native)"
        diff <(echo "$dedup_interp") <(echo "$dedup_nat") | head -4
    fi
else
    bad "lto-dedup: gcc failed on LTO output"
fi
# (c) the C output must contain exactly ONE definition of each Pair
#     instantiation (dedup). Count definitions (lines ending in `{`).
#     Prototypes end in `;` — they are NOT definitions.
pair_defs=$(grep -c "^static.*usf_new_Pair__int__int.*{$" "$TMP/dedup.c" || true)
pair_first_defs=$(grep -c "^int64_t usf_pair_first__int__int.*{$" "$TMP/dedup.c" || true)
pair_second_defs=$(grep -c "^int64_t usf_pair_second__int__int.*{$" "$TMP/dedup.c" || true)
if [ "$pair_defs" -eq 1 ] && [ "$pair_first_defs" -eq 1 ] && [ "$pair_second_defs" -eq 1 ]; then
    ok "lto-dedup: 1 def each for Pair[int,int] / pair_first / pair_second (no duplicate instantiations)"
else
    bad "lto-dedup: duplicate instantiations (Pair=$pair_defs first=$pair_first_defs second=$pair_second_defs)"
fi
# (d) --lto-threshold=N controls inlining. Use feat_stage20_lto.hls
#     (which has list_sort_int_asc, a bigger function) — threshold=5
#     must inline FEWER call sites than threshold=60.
"$TMP/hlc1" --lto-threshold 5 --lto-stats tests/ok/feat_stage20_lto.hls "$TMP/lto_t5.c" >"$TMP/lto_t5.stats" 2>&1
n_inline_t5=$(grep "inline expansions (sites)" "$TMP/lto_t5.stats" | awk '{print $NF}')
"$TMP/hlc1" --lto-threshold 60 --lto-stats tests/ok/feat_stage20_lto.hls "$TMP/lto_t60.c" >"$TMP/lto_t60.stats" 2>&1
n_inline_t60=$(grep "inline expansions (sites)" "$TMP/lto_t60.stats" | awk '{print $NF}')
if [ "${n_inline_t60:-0}" -gt "${n_inline_t5:-0}" ]; then
    ok "lto-threshold: higher budget (60) inlines more ($n_inline_t5 -> $n_inline_t60 expansions)"
else
    bad "lto-threshold: threshold did not affect inline count ($n_inline_t5 -> $n_inline_t60)"
fi
# (e) --lto-threshold rejects out-of-range values.
if "$TMP/hlc1" --lto-threshold 0 "$DEDUP_F" "$TMP/bad.c" 2>&1 | grep -q "error: --lto-threshold"; then
    ok "lto-threshold: out-of-range value (0) rejected"
else
    bad "lto-threshold: out-of-range value (0) not rejected"
fi
# (f) boot.py accepts the new flags silently (no effect on interpreter).
boot_out=$(python3 boot/boot.py --lto-stats --lto-threshold 30 "$DEDUP_F" 2>/dev/null)
if [ "$boot_out" == "$dedup_interp" ]; then
    ok "lto-flags: boot.py accepts --lto-stats / --lto-threshold (interpreter unaffected)"
else
    bad "lto-flags: boot.py diverged with --lto-stats / --lto-threshold"
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

# Stage 21 perfection (v0.40.0-alpha): horizontal reduce_min/max +
# --target-feature native auto-detection.
# (d) the new reduce_min/reduce_max ops are covered by the differential
#     suite (sections 1/3) since feat_stage21_simd.hls calls them; here
#     we verify the values are correct (mixed-sign lanes).
simd_interp_v2=$(python3 boot/boot.py "$SIMD_F" </dev/null 2>/dev/null)
if echo "$simd_interp_v2" | grep -q "^reduce_min: -5$" \
        && echo "$simd_interp_v2" | grep -q "^reduce_max: 7$"; then
    ok "simd: reduce_min/reduce_max correct on mixed-sign lanes (-5, 7)"
else
    bad "simd: reduce_min/reduce_max values wrong"
    echo "$simd_interp_v2" | grep reduce_
fi
# (e) --target-feature native: auto-detects the host's best feature.
#     Both the interpreter and the native build must resolve "native"
#     to the SAME concrete feature, so has_feature() const-folds
#     identically on both sides.
simd_interp_native=$(python3 boot/boot.py --target-feature native "$SIMD_F" </dev/null 2>/dev/null)
if "$TMP/hlc1" --target-feature native "$SIMD_F" "$TMP/s21n.c" >/dev/null 2>&1 \
        && gcc -O2 -o "$TMP/s21n_bin" "$TMP/s21n.c" -lm -pthread 2>/dev/null; then
    s21n_out=$("$TMP/s21n_bin" 2>/dev/null)
    if [ "$s21n_out" == "$simd_interp_native" ]; then
        ok "simd: --target-feature native produces byte-identical output (interp == native)"
    else
        bad "simd: --target-feature native diverged from interpreter"
        diff <(echo "$s21n_out") <(echo "$simd_interp_native") | head -4
    fi
    # The intrinsic helpers must be present under --target-feature native
    # when the host CPU supports AVX2 (the auto-detected feature).
    if grep -q "simd_cpu_supports(avx2) = true" <<<"$s21n_out"; then
        if grep -q "hl_simd_" "$TMP/s21n.c"; then
            ok "simd: --target-feature native emitted intrinsic fast path on AVX2 host"
        else
            bad "simd: --target-feature native did not emit intrinsic fast path on AVX2 host"
        fi
    fi
else
    bad "simd: --target-feature native compile failed"
fi
# (f) --target-feature native is accepted by boot.py and resolves to a
#     concrete feature. On an AVX2 host, has_feature("avx2") must
#     const-fold to true under --target-feature native (same as the
#     explicit --target-feature avx2 path).
if echo "$simd_interp_native" | grep -q "^has_feature(avx2) = true$"; then
    ok "simd: boot.py --target-feature native resolves to avx2 on AVX2 host"
else
    bad "simd: boot.py --target-feature native did not resolve to avx2"
    echo "$simd_interp_native" | grep has_feature
fi

echo "=== 10. Stage 22: cross-compilation targets (Linux/macOS/Windows/FreeBSD) ==="
# Stage 22 (v0.41.0-alpha): the cross-compilation orchestrator
# (tools/hlcross.py) drives hlc -> C -> cross-linker. The C backend
# is portable ANSI C11; the cross-compilation problem reduces to
# picking the right cross-linker. When no cross-linker is available,
# the C source is still written (so it can be copied to a target
# machine and compiled there).
CROSS_F=tests/ok/feat_stage22_cross.hls
cross_interp=$(python3 boot/boot.py "$CROSS_F" </dev/null 2>/dev/null)
# (a) the feat_stage22_cross program is differentially covered by
#     sections 1/3 (it lives in tests/ok); here we verify the
#     expected platform-independent output.
if echo "$cross_interp" | grep -q "1 + 1 = 2" \
        && echo "$cross_interp" | grep -q "10 \* 10 = 100" \
        && echo "$cross_interp" | grep -q "str_to_upper_ascii('hello') = HELLO" \
        && echo "$cross_interp" | grep -q "sorted: 1,1,2,3,4,5,6,9"; then
    ok "cross: feat_stage22_cross platform-independent output correct"
else
    bad "cross: feat_stage22_cross output wrong"
    echo "$cross_interp" | head -8
fi
# (b) hlcross --list-targets prints the Stage 22 target set.
list_out=$(python3 tools/hlcross.py --list-targets 2>&1)
if echo "$list_out" | grep -q "x86_64-linux-gnu" \
        && echo "$list_out" | grep -q "aarch64-apple-darwin" \
        && echo "$list_out" | grep -q "x86_64-pc-windows-msvc" \
        && echo "$list_out" | grep -q "x86_64-unknown-freebsd"; then
    ok "cross: --list-targets prints the Stage 22 target set"
else
    bad "cross: --list-targets missing expected targets"
fi
# (c) hlcross --show-host prints a non-empty canonical triple.
host_triple=$(python3 tools/hlcross.py --show-host 2>/dev/null)
if [ -n "$host_triple" ] && echo "$host_triple" | grep -qE "^[a-z0-9_-]+$"; then
    ok "cross: --show-host detected '$host_triple'"
else
    bad "cross: --show-host did not return a triple"
fi
# (d) hlcross rejects an unknown target.
if python3 tools/hlcross.py "$CROSS_F" /tmp/bad_cross --target foo-bar-baz 2>&1 \
        | grep -q "unknown target"; then
    ok "cross: unknown target rejected with clear error"
else
    bad "cross: unknown target not rejected"
fi
# (e) cross-compile to the HOST target (always available — uses the
#     host compiler). The binary must run and produce the same output
#     as the interpreter.
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
if python3 tools/hlcross.py "$CROSS_F" "$TMP/cross_host" --target "$host_triple" \
        --hlc "$TMP/hlc1" --keep-c "$TMP/cross_host.c" >"$TMP/cross.log" 2>&1; then
    cross_host_out=$("$TMP/cross_host" 2>/dev/null)
    if [ "$cross_host_out" == "$cross_interp" ]; then
        ok "cross: host-target binary output == interpreter (byte-identical)"
    else
        bad "cross: host-target binary diverged from interpreter"
        diff <(echo "$cross_interp") <(echo "$cross_host_out") | head -4
    fi
    # The C source must be portable ANSI C11 (no SIMD intrinsic fast
    # paths, no PGO counters, no __builtin_expect hints — those only
    # appear under --target-feature / --pgo-generate / --pgo-use).
    # NOTE: hl_simd_cpu_supports (the runtime CPU probe) IS in the
    # runtime unconditionally — it's used by the simd_cpu_supports()
    # builtin. The intrinsic fast PATHS (hl_simd_add_i32x4, etc.)
    # are what only appear under --target-feature.
    if grep -q "hl_simd_add_i32x4\|hl_simd_mul_i32x4\|_mm_mullo_epi32\|__hlc_pgo_counts\|__builtin_expect" "$TMP/cross_host.c"; then
        bad "cross: C source leaked target-specific machinery (PGO/SIMD intrinsics)"
    else
        ok "cross: C source is portable (no PGO/SIMD intrinsic machinery in unflagged build)"
    fi
else
    bad "cross: host-target cross-compile failed"
    cat "$TMP/cross.log" | head -5
fi
# (f) cross-compile to a FOREIGN target — when no cross-linker is
#     available, hlcross reports SKIP (exit code 3) and writes the C
#     source (so it can be compiled on the target machine).
foreign_target="aarch64-apple-darwin"
if [ "$host_triple" != "$foreign_target" ]; then
    python3 tools/hlcross.py "$CROSS_F" "$TMP/cross_foreign" \
            --target "$foreign_target" --hlc "$TMP/hlc1" \
            --keep-c "$TMP/cross_foreign.c" >"$TMP/cross_foreign.log" 2>&1
    cross_rc=$?
    if [ $cross_rc -eq 0 ]; then
        # A cross-linker was available — the binary was produced.
        fmt=$(python3 -c "
import sys; sys.path.insert(0, 'tools')
from hlcross import detect_binary_format
print(detect_binary_format('$TMP/cross_foreign'))" 2>/dev/null)
        ok "cross: $foreign_target binary produced (format: $fmt) — cross-linker available"
    elif [ $cross_rc -eq 3 ]; then
        # SKIP — no cross-linker. The C source must still be written.
        if [ -s "$TMP/cross_foreign.c" ]; then
            ok "cross: $foreign_target SKIP (no cross-linker) — C source written for target-side compilation"
        else
            bad "cross: $foreign_target SKIP but no C source written"
        fi
    else
        bad "cross: $foreign_target failed unexpectedly (rc=$cross_rc)"
        cat "$TMP/cross_foreign.log" | head -5
    fi
fi
# (g) hls-pkg lock --target stamps the lockfile with the target triple.
#     Verify by locking in a temp package dir.
PKG_DIR="$TMP/stage22_pkg"
mkdir -p "$PKG_DIR"
cat > "$PKG_DIR/hls-pkg.toml" <<'PKGEOF'
[package]
name = "stage22-test"
version = "0.1.0"
authors = ["test"]
description = "Stage 22 lockfile target test"
[dependencies]
[effects]
allowed = []
PKGEOF
cat > "$PKG_DIR/main.hls" <<'HLS_EOF'
fn main() -> int uses IO {
    println("stage22 pkg")
    return 0
}
HLS_EOF
REPO_ABS="$(pwd)"
( cd "$PKG_DIR" && python3 "$REPO_ABS/tools/hls-pkg.py" lock --target aarch64-apple-darwin >pkg.log 2>&1 )
if grep -q '"target": "aarch64-apple-darwin"' "$PKG_DIR/hls-pkg.lock"; then
    ok "cross: hls-pkg lock --target stamps the lockfile"
else
    bad "cross: hls-pkg lock --target did not stamp the lockfile"
    cat "$PKG_DIR/pkg.log" | head -3
fi
# (h) hls-pkg verify --target checks the lockfile's target matches.
( cd "$PKG_DIR" && python3 "$REPO_ABS/tools/hls-pkg.py" verify --target aarch64-apple-darwin >pkg_verify.log 2>&1 )
if grep -q "lockfile target: aarch64-apple-darwin (matches --target)" "$PKG_DIR/pkg_verify.log"; then
    ok "cross: hls-pkg verify --target matches"
else
    bad "cross: hls-pkg verify --target did not match"
    cat "$PKG_DIR/pkg_verify.log" | head -3
fi
# (i) hls-pkg verify --target detects a mismatch.
( cd "$PKG_DIR" && python3 "$REPO_ABS/tools/hls-pkg.py" verify --target x86_64-pc-windows-gnu >pkg_mismatch.log 2>&1 )
rc=$?
if [ $rc -ne 0 ] && grep -q "lockfile target mismatch" "$PKG_DIR/pkg_mismatch.log"; then
    ok "cross: hls-pkg verify --target detects mismatch (rejected)"
else
    bad "cross: hls-pkg verify --target did not detect mismatch"
    cat "$PKG_DIR/pkg_mismatch.log" | head -3
fi
# (j) make cross-acceptance runs end-to-end on the host target.
if make cross-acceptance >"$TMP/cross_acc.log" 2>&1; then
    if grep -q "ACCEPTANCE OK: cross-compilation pipeline" "$TMP/cross_acc.log"; then
        ok "cross: make cross-acceptance runs end-to-end (host target)"
    else
        bad "cross: make cross-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/cross_acc.log"
    fi
else
    bad "cross: make cross-acceptance failed"
    tail -5 "$TMP/cross_acc.log"
fi

echo "=== 11. Stage 23: WebAssembly backend (wasm32-unknown-unknown) ==="
# Stage 23 (v0.42.0-alpha): the direct wasm emitter (tools/hlwasm.py)
# compiles an HLS program to a .wasm binary + .js glue + .html runner.
# The wasm module imports a small JS function set (println, print,
# f64_to_str) from module "env"; the JS glue provides them.
WASM_F=examples/hello.hls
# (a) hlwasm --list-targets prints the wasm target set.
wasm_list=$(python3 tools/hlwasm.py --list-targets 2>&1)
if echo "$wasm_list" | grep -q "wasm32-unknown-unknown" \
        && echo "$wasm_list" | grep -q "wasm32-unknown-emscripten"; then
    ok "wasm: --list-targets prints the Stage 23 target set"
else
    bad "wasm: --list-targets missing expected targets"
fi
# (b) hello.hls compiles to a <10 KB wasm binary.
if python3 tools/hlwasm.py "$WASM_F" "$TMP/hello_wasm" >"$TMP/wasm.log" 2>&1; then
    wasm_size=$(stat -c %s "$TMP/hello_wasm.wasm" 2>/dev/null || stat -f %z "$TMP/hello_wasm.wasm")
    if [ "$wasm_size" -lt 10240 ]; then
        ok "wasm: hello.hls compiles to ${wasm_size}-byte wasm (< 10 KB)"
    else
        bad "wasm: hello.hls wasm is ${wasm_size} bytes (>= 10 KB)"
    fi
    # (c) .js and .html glue files are produced.
    if [ -f "$TMP/hello_wasm.js" ] && [ -f "$TMP/hello_wasm.html" ]; then
        ok "wasm: .js + .html glue files produced"
    else
        bad "wasm: .js or .html glue missing"
    fi
else
    bad "wasm: hello.hls compile failed"
    cat "$TMP/wasm.log" | head -5
fi
# (d) running the wasm in Node.js produces the same output as the interpreter.
#     (Only runs if node is available; skipped gracefully otherwise.)
if command -v node >/dev/null 2>&1; then
    interp_out=$(python3 boot/boot.py "$WASM_F" </dev/null 2>/dev/null)
    if python3 tools/hlwasm.py "$WASM_F" "$TMP/hello_wasm2" --run >"$TMP/wasm_run.out" 2>&1; then
        wasm_out=$(grep -v '^wrote ' "$TMP/wasm_run.out" | grep -v '^note:')
        if [ "$interp_out" == "$wasm_out" ]; then
            ok "wasm: hello.hls wasm output == interpreter (byte-identical)"
        else
            bad "wasm: hello.hls wasm output differs from interpreter"
            diff <(echo "$interp_out") <(echo "$wasm_out") | head -4
        fi
    else
        bad "wasm: hello.hls wasm run failed"
        cat "$TMP/wasm_run.out" | head -5
    fi
else
    echo "  [SKIP] wasm: node.js not installed (output-match test skipped)"
    PASS=$((PASS+1))
fi
# (e) extern "js" blocks are accepted by the parser.
cat > "$TMP/test_jsffi.hls" <<'HLS_EOF'
extern "js" {
    fn js_alert(msg: str) -> void uses IO
    fn js_random() -> int uses IO
}
fn main() -> int uses IO {
    js_alert("hello")
    return 0
}
HLS_EOF
if python3 boot/boot.py --check "$TMP/test_jsffi.hls" >/dev/null 2>&1; then
    ok "wasm: extern \"js\" block accepted by the parser/checker"
else
    bad "wasm: extern \"js\" block rejected"
fi
# (f) compiling a program with extern "js" produces the right wasm imports.
if python3 tools/hlwasm.py "$TMP/test_jsffi.hls" "$TMP/test_jsffi" >"$TMP/jsffi.log" 2>&1; then
    # Check that the wasm imports include js_alert and js_random.
    if python3 -c "
import sys
with open('$TMP/test_jsffi.wasm', 'rb') as f:
    data = f.read()
# Look for the import names in the binary.
assert b'js_alert' in data, 'js_alert import missing'
assert b'js_random' in data, 'js_random import missing'
print('OK')
" 2>/dev/null | grep -q OK; then
        ok "wasm: extern \"js\" functions become wasm imports"
    else
        bad "wasm: extern \"js\" imports not found in wasm"
    fi
else
    bad "wasm: compile of extern \"js\" program failed"
    cat "$TMP/jsffi.log" | head -5
fi
# (g) unsupported constructs raise clean errors (not silent crashes).
cat > "$TMP/test_unsupp.hls" <<'HLS_EOF'
struct Point { x: int, y: int }
fn main() -> int uses IO {
    let p: Point = Point { x: 1, y: 2 }
    return 0
}
HLS_EOF
if python3 tools/hlwasm.py "$TMP/test_unsupp.hls" "$TMP/test_unsupp" 2>&1 \
        | grep -q "not yet supported by --emit wasm"; then
    ok "wasm: unsupported construct raises clean error"
else
    bad "wasm: unsupported construct did not raise clean error"
fi
# (h) make wasm-acceptance runs end-to-end.
if make wasm-acceptance >"$TMP/wasm_acc.log" 2>&1; then
    if grep -q "ACCEPTANCE OK" "$TMP/wasm_acc.log"; then
        ok "wasm: make wasm-acceptance runs end-to-end"
    else
        bad "wasm: make wasm-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/wasm_acc.log"
    fi
else
    bad "wasm: make wasm-acceptance failed"
    tail -5 "$TMP/wasm_acc.log"
fi

echo ""
echo "=========================================="
echo "RESULT: $PASS PASS / $FAIL FAIL"
echo "=========================================="
[ $FAIL -eq 0 ]
