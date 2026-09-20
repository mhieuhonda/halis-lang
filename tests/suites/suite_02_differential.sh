#!/usr/bin/env bash
# Suite suite_02_differential - verbatim section of the original tests/run_tests.sh
# (split for maintainability; sourced by tests/run_tests.sh in order).

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
    # Deep-scan-28 fix: `#![freestanding]` crates emit `_start` and no
    # `main`, so the hosted `gcc -O2` recipe double-defines `_start`
    # (Scrt1.o) and the whole section reported a false failure since
    # Stage 77 added tests/ok/feat_freestanding_basic.hls. Use the same
    # freestanding link recipe suite_01_boot.sh uses for those files.
    if head -5 "$f" | grep -q '#!\[freestanding\]'; then
        if "$TMP/hlc1" "$f" "$TMP/$name.nat.c" >/dev/null 2>&1 \
                && gcc -O2 -ffreestanding -nostdlib -ffunction-sections \
                       -fno-stack-protector -Wl,--gc-sections \
                       -o "$TMP/$name.nat.bin" "$TMP/$name.nat.c" 2>/dev/null; then
            nat_ok=$((nat_ok+1))
        else
            nat_bad=$((nat_bad+1))
            bad "native hlc compile: $name"
        fi
    elif "$TMP/hlc1" "$f" "$TMP/$name.nat.c" >/dev/null 2>&1 \
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


echo "=== 4c. deep-scan-29: Stage 32 bench driver sanity ==="
# The bench-stdlib gate was fully red before deep-scan-29: 53 functions
# failed the driver's `uses IO`-only type-check (any callee requiring
# Net/Rand/Proc/Conc) and 54 panicked on generic default inputs. Lock
# in the three fix classes with one representative function each:
#   mutex_new  — Conc effect      (was: hlc type error)
#   env_var    — Proc effect      (was: hlc type error)
#   base64_decode — curated valid input (was: run panic on "hello world")
# All three must be MEASURED (not skipped, not failed) at low iters.
bench_out=$(python3 tools/hls-bench.py --iters 3000 \
    --only mutex_new,env_var,base64_decode 2>&1); bench_rc=$?
bench_measured=$(echo "$bench_out" | grep -E "^[0-9]+ measured" | grep -oE "^[0-9]+")
if [ $bench_rc -eq 0 ] && [ "$bench_measured" -eq 3 ]; then
    ok "hls-bench driver: effects + curated inputs (3/3 measured, 0 failed)"
else
    bad "hls-bench driver: rc=$bench_rc measured=$bench_measured (want 3)"
    echo "$bench_out" | tail -6
fi
# Skipped-only selections must report cleanly, not crash.
if python3 tools/hls-bench.py --iters 3000 --only csrf_generate_token >/dev/null 2>&1; then
    bad "hls-bench: skip-only selection should exit non-zero"
else
    ok "hls-bench: skip-only selection reports 'no functions to benchmark'"
fi
# ll_validate --help must print usage (was: 'cannot read: --help' error).
if python3 tools/ll_validate.py --help >/dev/null 2>&1; then
    ok "ll_validate: --help prints usage"
else
    bad "ll_validate: --help broken"
fi
