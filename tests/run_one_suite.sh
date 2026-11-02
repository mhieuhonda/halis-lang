#!/usr/bin/env bash
# run_one_suite.sh — run a single suite gate with the shared build law.
# Usage: bash tests/run_one_suite.sh suite_07_asm
#        bash tests/run_one_suite.sh tests/suites/suite_07_asm.sh  (legacy path)
#
# The suites are Halis gates now (tests/acc/suite_XX_*_acceptance.hls);
# this runner accepts the old suite name or path, maps it to the gate,
# builds it once, and runs it from the repo root.
set -u
cd "$(dirname "$0")/.."

arg="${1:-}"
if [ -z "$arg" ]; then
    echo "usage: bash tests/run_one_suite.sh <suite-gate|legacy-suite-path>" >&2
    exit 2
fi

gate=$(basename "$arg" .sh)

if [ ! -x "bin/acc-$gate" ]; then
    make -s "bin/acc-$gate" >/dev/null 2>&1
fi

if [ ! -x "bin/acc-$gate" ]; then
    echo "  [FAIL] $gate (gate build failed)"
    exit 1
fi

"bin/acc-$gate"
