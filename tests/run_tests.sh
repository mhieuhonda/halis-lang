#!/usr/bin/env bash
# ============================================================================
# run_tests.sh — the full suite driver.
# ----------------------------------------------------------------------------
# The eight differential suites are Halis gates now (tests/acc/
# suite_XX_*_acceptance.hls, compiled by bin/hlc): this driver builds
# each gate once and runs it, letting the gates print their own
# per-leg [OK]/[FAIL] grammar and keeping the suite exit law — the
# run is green only when every gate is.
# ============================================================================
set -u
cd "$(dirname "$0")/.."
PASS=0
FAIL=0

GATES=(
    suite_01_boot
    suite_02_differential
    suite_03_selfhost
    suite_04_optimizers
    suite_05_backends
    suite_06_advanced
    suite_07_asm
    suite_08_osdev
)

for gate in "${GATES[@]}"; do
    if [ ! -x "bin/acc-$gate" ]; then
        if ! make -s "bin/acc-$gate" >/dev/null 2>&1; then
            FAIL=$((FAIL+1))
            echo "  [FAIL] $gate (gate build failed)"
            continue
        fi
    fi
    if "bin/acc-$gate"; then
        PASS=$((PASS+1))
    else
        FAIL=$((FAIL+1))
        echo "  [FAIL] $gate"
    fi
done

echo "=========================================="
echo "RESULT: $PASS PASS / $FAIL FAIL (suite gates)"
echo "=========================================="
[ $FAIL -eq 0 ]
