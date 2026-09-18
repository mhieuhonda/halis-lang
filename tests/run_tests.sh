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

# Suites live in tests/suites/ and are sourced in the original
# section order; PASS/FAIL counters and helpers are shared.
source "$(dirname "$0")/suites/suite_01_boot.sh"
source "$(dirname "$0")/suites/suite_02_differential.sh"
source "$(dirname "$0")/suites/suite_03_selfhost.sh"
source "$(dirname "$0")/suites/suite_04_optimizers.sh"
source "$(dirname "$0")/suites/suite_05_backends.sh"
source "$(dirname "$0")/suites/suite_06_advanced.sh"
source "$(dirname "$0")/suites/suite_07_asm.sh"
source "$(dirname "$0")/suites/suite_08_osdev.sh"

echo "=========================================="
echo "RESULT: $PASS PASS / $FAIL FAIL"
echo "=========================================="
[ $FAIL -eq 0 ]