#!/usr/bin/env bash
# Suite suite_04_optimizers - verbatim section of the original tests/run_tests.sh
# (split for maintainability; sourced by tests/run_tests.sh in order).

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
    # Deep-scan-23 fix: this check grepped "$TMP/$name.c" but `name` is
    # never assigned in this phase (a set -u crash), and the old pattern
    # also matched `hl_simd_cpu_supports(hl_str` — the PORTABLE runtime
    # probe that legitimately exists in every build — so the check could
    # never pass. Compile the simd test WITHOUT the feature flag and
    # grep for the INTRINSIC machinery only (immintrin/NEON/RVV headers
    # and intrinsics, plus the fast-path kernel names).
    if "$TMP/hlc1" "$SIMD_F" "$TMP/s21_unflagged.c" >/dev/null 2>&1 \
            && ! grep -q "immintrin\|arm_neon\|riscv_vector\|_mm_\|vld1q_\|vst1q_\|__riscv_v\|hl_simd_add_i32x4" "$TMP/s21_unflagged.c" 2>/dev/null; then
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
            grep -E "time = |RATIO" "$TMP/s21b.out" | tail -3
        fi
    else
        echo "  [SKIP] host CPU has no AVX2 — timing gate skipped (checksum still verified)"
    fi
else
    bad "simd-bench: compile failed"
fi

# (c2) Stage 31 perfection (v0.48.1-alpha): the harmonised fused
#      kernels. feat_stage31_simd_fused.hls exercises the vector body,
#      the tails and the boundary shapes; the interpreter output must
#      be byte-identical to BOTH the unflagged native build (the C
#      scalar fallback) and the --target-feature avx2 build (the
#      cache-blocked 256-bit intrinsic kernel) AND the --target-feature
#      sse4.2 build (the 128-bit kernel). The two panic programs
#      (panic_simd_lane.hls / panic_simd_lane_ys.hls) verify the
#      upfront fail-fast int32 range check covers tail-region elements
#      in every implementation (sections 1/3 already run them
#      differentially: both sides must panic with exit 101).
FUSED_F=tests/ok/feat_stage31_simd_fused.hls
fused_interp=$(python3 boot/boot.py "$FUSED_F" </dev/null 2>/dev/null); fused_irc=$?
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi
fused_modes="avx2 sse4.2"
fused_fail=0
for mode in "" $fused_modes; do
    if [ -n "$mode" ]; then T="--target-feature $mode"; else T=""; fi
    if "$TMP/hlc1" $T "$FUSED_F" "$TMP/fused_$mode.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/fused_$mode.bin" "$TMP/fused_$mode.c" -lm -pthread 2>/dev/null; then
        fused_out=$("$TMP/fused_$mode.bin" 2>/dev/null); fused_rc=$?
        if [ "$fused_out" == "$fused_interp" ] && [ "$fused_rc" == "$fused_irc" ]; then
            if [ -n "$mode" ]; then
                ok "simd-fused: $mode kernel output == interpreter (byte-identical)"
            else
                ok "simd-fused: scalar-fallback output == interpreter (byte-identical)"
            fi
        else
            bad "simd-fused: $mode kernel diverges from interpreter"
            diff <(echo "$fused_interp") <(echo "$fused_out") | head -4
            fused_fail=1
        fi
    else
        bad "simd-fused: $mode compile failed"
        fused_fail=1
    fi
done
# (c3) Stage 31 perfection: multi-block native self-consistency. The
#      cache-blocked kernels tile the 1M-element pass in 4 KB blocks;
#      a 5,003-element list (n % 4 == 3, multi-block, odd tails) must
#      give byte-identical output through the fallback and BOTH
#      intrinsic paths (native-only: the interpreter runs the same
#      portable path, so the fused differential above already covers
#      it — this catches block-boundary bugs the small lists cannot
#      reach).
cat > ".fused_big_tmp.hls" <<'FBEOF'
import "std.simd"

fn build_list(n: int, seed: int) -> list[int] {
    let xs: list[int] = []
    let mut v: int = seed
    let mut i: int = 0
    while i < n {
        v = (v * 1103515245 + 12345) % 2147483647
        if v < 0 { v = 0 - v }
        xs.push(v % 1000000)
        i = i + 1
    }
    return xs
}

fn scalar_correlate8(xs: list[int], w0: int, w1: int, w2: int, w3: int,
                      w4: int, w5: int, w6: int, w7: int) -> int {
    let mut total: int = 0
    let n: int = xs.len()
    let mut i: int = 0
    while i + 8 <= n {
        total = total + xs.get(i) * w0 + xs.get(i + 1) * w1 + xs.get(i + 2) * w2
            + xs.get(i + 3) * w3 + xs.get(i + 4) * w4 + xs.get(i + 5) * w5
            + xs.get(i + 6) * w6 + xs.get(i + 7) * w7
        i = i + 1
    }
    return total
}

fn main() -> int uses IO {
    let xs: list[int] = build_list(5003, 7)
    let ys: list[int] = build_list(5003, 11)
    let c: int = scalar_correlate8(xs, 3, 1, 4, 1, 5, 9, 2, 6)
    let v: int = simd_correlate8_sum_i32x4(xs, 3, 1, 4, 1, 5, 9, 2, 6)
    if c != v {
        println("correlate8 MISMATCH scalar=" + c.to_str() + " vector=" + v.to_str())
        return 1
    }
    println("correlate8(5003) = " + v.to_str())
    println("transform(5003) = " + simd_transform_sum_i32x4(xs, ys, 3, 1).to_str())
    return 0
}
FBEOF
# NOTE: the temp program lives in the repo root (dot-file, excluded
# from the tests/ok glob) so that `import "std.simd"` resolves — the
# import walker searches from the IMPORTING FILE's directory upward.
big_ref=""
for mode in "" $fused_modes; do
    if [ -n "$mode" ]; then T="--target-feature $mode"; else T=""; fi
    if "$TMP/hlc1" $T ".fused_big_tmp.hls" "$TMP/fused_big_$mode.c" >/dev/null 2>&1 \
            && gcc -O2 -o "$TMP/fused_big_$mode.bin" "$TMP/fused_big_$mode.c" -lm -pthread 2>/dev/null; then
        big_out=$("$TMP/fused_big_$mode.bin" 2>/dev/null); big_rc=$?
        if [ -z "$big_ref" ]; then
            big_ref="$big_out|$big_rc"
        elif [ "$big_out|$big_rc" != "$big_ref" ]; then
            bad "simd-fused-big: $mode output differs from the unflagged build (block boundary?)"
            diff <(echo "$big_ref" | tr '|' '\n') <(echo "$big_out|$big_rc" | tr '|' '\n') | head -4
            fused_fail=1
        fi
    else
        bad "simd-fused-big: $mode compile failed"
        fused_fail=1
    fi
done
if [ $fused_fail -eq 0 ] && [ -n "$big_ref" ]; then
    ok "simd-fused-big: 5003-element multi-block output identical across fallback + avx2 + sse4.2"
fi
rm -f ".fused_big_tmp.hls"
# (c4) Stage 31 perfection: the NEON link regression. Under
#      --target-feature neon the fused kernels must be defined in BOTH
#      the #if __aarch64__ branch (the NEON kernels) and the #else
#      branch (the portable fallback). The old emitter pushed them
#      only into the x86 main-helper list, which the aarch64 branch
#      never includes — a latent link failure on real NEON hardware.
if "$TMP/hlc1" --target-feature neon "$FUSED_F" "$TMP/fused_neon.c" >/dev/null 2>&1; then
    n_corr=$(grep -c "static int64_t hl_simd_correlate8_sum_i32x4" "$TMP/fused_neon.c")
    n_tran=$(grep -c "static int64_t hl_simd_transform_sum_i32x4" "$TMP/fused_neon.c")
    if [ "$n_corr" -eq 2 ] && [ "$n_tran" -eq 2 ] && grep -q "vld1q_s32" "$TMP/fused_neon.c"; then
        ok "simd-neon: fused kernels defined in BOTH branches (NEON + fallback) — no aarch64 link hole"
        if gcc -O2 -o "$TMP/fused_neon.bin" "$TMP/fused_neon.c" -lm -pthread 2>/dev/null; then
            neon_out=$("$TMP/fused_neon.bin" 2>/dev/null)
            if [ "$neon_out" == "$fused_interp" ]; then
                ok "simd-neon: x86 fallback of the neon build == interpreter (byte-identical)"
            else
                bad "simd-neon: fallback diverges from interpreter"
            fi
        fi
    else
        bad "simd-neon: fused kernels missing from a branch (correlate=$n_corr transform=$n_tran)"
    fi
else
    bad "simd-neon: --target-feature neon compile failed"
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

