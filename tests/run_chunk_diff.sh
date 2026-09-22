#!/usr/bin/env bash
# Chunked differential runner mirroring suite_01_boot.sh section 3 logic.
# Usage: bash tests/run_chunk_diff.sh <start_idx> <count>
# Processes tests/ok/*.hls from index start_idx (0-based), count files.
set -u
cd "$(dirname "$0")/.."
PASS=0
FAIL=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM HUP
START=$1
COUNT=$2

ok()   { PASS=$((PASS+1)); echo "  [PASS] $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  [FAIL] $1"; }

LIBCURL_CFLAGS="$(curl-config --cflags 2>/dev/null)"
LIBCURL_LIBS="$(curl-config --libs 2>/dev/null)"
if [ -z "$LIBCURL_LIBS" ]; then
    LIBCURL_CFLAGS="$(pkg-config --cflags libcurl 2>/dev/null)"
    LIBCURL_LIBS="$(pkg-config --libs libcurl 2>/dev/null)"
fi
if [ -n "$LIBCURL_LIBS" ]; then
    CURL_DEFS="-DHL_HAVE_LIBCURL"
else
    CURL_DEFS=""
    LIBCURL_LIBS=""
fi

FILES=($(ls tests/ok/*.hls | sed -n "$((START+1)),$((START+COUNT))p"))
echo "chunk: ${#FILES[@]} files ($START .. $((START+COUNT-1)))"
for f in "${FILES[@]}"; do
    name=$(basename "$f" .hls)
    interp_out=$(python3 boot/boot.py "$f" </dev/null 2>/dev/null); interp_code=$?
    if ! python3 boot/boot.py src/hlc.hls "$f" "$TMP/$name.c" >/dev/null 2>&1; then
        bad "$name (hlc compile failed)"
        continue
    fi
    if head -5 "$f" | grep -q '#!\[freestanding\]'; then
        if ! gcc -O2 -ffreestanding -nostdlib -ffunction-sections \
                -fno-stack-protector -Wl,--gc-sections \
                -o "$TMP/$name.bin" "$TMP/$name.c" 2>"$TMP/$name.gcc"; then
            bad "$name (freestanding gcc error)"
            continue
        fi
    elif ! gcc -O2 $CURL_DEFS $LIBCURL_CFLAGS -o "$TMP/$name.bin" "$TMP/$name.c" -lm -pthread $LIBCURL_LIBS 2>"$TMP/$name.gcc"; then
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
echo "CHUNK RESULT: $PASS PASS / $FAIL FAIL"
[ $FAIL -eq 0 ]
