#!/usr/bin/env bash
# Suite suite_03_selfhost - verbatim section of the original tests/run_tests.sh
# (split for maintainability; sourced by tests/run_tests.sh in order).

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
    # Deep-scan-23 fix: the second grep read "$TMP/$name.c" but `name`
    # is never assigned in this phase (a set -u crash), and no plain
    # build existed to inspect. Compile PGO_F WITHOUT any --pgo flag
    # and verify the output carries no counters or hints.
    if grep -q "__hlc_pgo_counts" "$TMP/pgo_gen.c" \
            && "$TMP/hlc1" "$PGO_F" "$TMP/pgo_plain.c" >/dev/null 2>&1 \
            && ! grep -q "__hlc_pgo_counts\|__builtin_expect" "$TMP/pgo_plain.c" 2>/dev/null; then
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

