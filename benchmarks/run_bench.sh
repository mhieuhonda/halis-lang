#!/usr/bin/env bash
# ============================================================================
# run_bench.sh — Run all benchmarks three ways:
#   1. Stage-0 interpreter (interpreted)
#   2. Native hlc-compiled binary (compiled via Stage-0 + gcc)
#   3. Native hlc-compiled binary (compiled via the native hlc itself)
#
# Runs each benchmark three ways and prints its output (the benchmarks
# self-time; for real timing use `time` externally). All three paths
# should produce identical output (the differential property of the
# bootstrap chain).
# ============================================================================
set -u
cd "$(dirname "$0")/.."
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM HUP

run_interp() {
    local f="$1"
    echo "  [interp]    $(python3 boot/boot.py "$f" 2>/dev/null)"
}

run_native_stage0() {
    local f="$1"
    local name=$(basename "$f" .hls)
    if python3 boot/boot.py src/hlc.hls "$f" "$TMP/$name.c" >/dev/null 2>&1; then
        if gcc -O2 -o "$TMP/$name.bin" "$TMP/$name.c" -lm -pthread 2>/dev/null; then
            echo "  [native s0] $("$TMP/$name.bin" 2>/dev/null)"
        else
            echo "  [native s0] gcc error on $name"
        fi
    else
        echo "  [native s0] hlc compile error on $name"
    fi
}

run_native_self() {
    local f="$1"
    local name=$(basename "$f" .hls)
    # Use the bootstrap chain to get a native hlc.
    if [ ! -x "$TMP/hlc_native" ]; then
        python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_s1.c" >/dev/null 2>&1
        gcc -O2 -o "$TMP/hlc_native" "$TMP/hlc_s1.c" -lm -pthread 2>/dev/null
    fi
    if "$TMP/hlc_native" "$f" "$TMP/$name.self.c" >/dev/null 2>&1; then
        if gcc -O2 -o "$TMP/$name.self.bin" "$TMP/$name.self.c" -lm -pthread 2>/dev/null; then
            echo "  [native self] $("$TMP/$name.self.bin" 2>/dev/null)"
        fi
    fi
}

# Timing benchmarks (wall-clock output, inherently non-deterministic)
# skip the interpreter path — the interpreted CPU-bound workload would
# take minutes and the three outputs are not comparable anyway.
TIMING_BENCHES="benchmarks/conc_bench.hls"

for f in benchmarks/*.hls; do
    name=$(basename "$f")
    echo "=== $name ==="
    if echo "$TIMING_BENCHES" | grep -q "$name"; then
        echo "  [interp]    (skipped: timing benchmark — interpreter is not representative)"
    else
        run_interp "$f"
    fi
    run_native_stage0 "$f"
    run_native_self "$f"
    echo ""
done
