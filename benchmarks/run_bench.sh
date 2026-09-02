#!/usr/bin/env bash
# ============================================================================
# run_bench.sh — Run all benchmarks three ways:
#   1. Stage-0 interpreter (interpreted)
#   2. Native hlc-compiled binary (compiled via Stage-0 + gcc)
#   3. Native hlc-compiled binary (compiled via the native hlc itself)
#
# Reports timing for each. All three paths should produce identical output
# (the differential property of the bootstrap chain).
# ============================================================================
set -u
cd "$(dirname "$0")/.."
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

run_interp() {
    local f="$1"
    echo "  [interp]    $(python3 boot/boot.py "$f" 2>/dev/null)"
}

run_native_stage0() {
    local f="$1"
    local name=$(basename "$f" .hls)
    if python3 boot/boot.py src/hlc.hls "$f" "$TMP/$name.c" >/dev/null 2>&1; then
        if gcc -O2 -o "$TMP/$name.bin" "$TMP/$name.c" -lm 2>/dev/null; then
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
        gcc -O2 -o "$TMP/hlc_native" "$TMP/hlc_s1.c" -lm 2>/dev/null
    fi
    if "$TMP/hlc_native" "$f" "$TMP/$name.self.c" >/dev/null 2>&1; then
        if gcc -O2 -o "$TMP/$name.self.bin" "$TMP/$name.self.c" -lm 2>/dev/null; then
            echo "  [native self] $("$TMP/$name.self.bin" 2>/dev/null)"
        fi
    fi
}

for f in benchmarks/*.hls; do
    name=$(basename "$f")
    echo "=== $name ==="
    run_interp "$f"
    run_native_stage0 "$f"
    run_native_self "$f"
    echo ""
done
