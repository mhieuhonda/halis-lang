#!/usr/bin/env bash
# run_one_suite.sh — run a single suite file with the shared harness
# Usage: bash tests/run_one_suite.sh tests/suites/suite_07_asm.sh
set -u
cd "$(dirname "$0")/.."
PASS=0
FAIL=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM HUP

ok()   { PASS=$((PASS+1)); echo "  [PASS] $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  [FAIL] $1"; }

# Suites 07/08 consume the native compiler binary ($TMP/hlc1) that the
# full-suite order builds in suite_03. When a suite runs STANDALONE
# (tests/run_one_suite.sh), build it here — same recipe as suite_02's
# guard — so the dependent checks don't spuriously fail with
# "command not found".
if [ ! -x "$TMP/hlc1" ]; then
    python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_nat.c" >/dev/null 2>&1
    gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_nat.c" -lm -pthread 2>/dev/null
fi

source "$1"

echo "=========================================="
echo "SUITE RESULT: $PASS PASS / $FAIL FAIL"
echo "=========================================="
[ $FAIL -eq 0 ]
