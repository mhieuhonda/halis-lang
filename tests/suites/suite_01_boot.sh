#!/usr/bin/env bash
# Suite suite_01_boot - verbatim section of the original tests/run_tests.sh
# (split for maintainability; sourced by tests/run_tests.sh in order).

echo "=== 1. Stage-0: valid programs ==="
for f in tests/ok/*.hls; do
    name=$(basename "$f" .hls)
    out=$(python3 boot/boot.py "$f" </dev/null 2>/dev/null); code=$?
    if [ $code -eq 0 ] || [ $code -eq 101 ]; then
        snap="tests/snapshots/$name.txt"
        if [ -f "$snap" ] && [ "$out" != "$(cat "$snap")" ]; then
            bad "$name (differs from snapshot)"
        else
            ok "$name"
        fi
    else
        bad "$name (exit=$code)"
    fi
done

echo "=== 2. Stage-0: programs EXPECTED TO FAIL ==="
for f in tests/fail/*.hls; do
    name=$(basename "$f" .hls)
    err=$(python3 boot/boot.py --check "$f" 2>&1 >/dev/null); code=$?
    if [ $code -eq 1 ]; then
        ok "$name -> $err"
    else
        bad "$name (should be rejected but exit=$code)"
    fi
done

echo "=== 3. Self-compile + differential testing (interpreter vs native) ==="
# Stage 37 (v0.56.0-alpha): probe libcurl so the net_tls_get builtin
# links cleanly when libcurl is installed (otherwise the runtime's
# #ifdef HL_HAVE_LIBCURL guard emits a clean panic stub).
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
for f in tests/ok/*.hls; do
    name=$(basename "$f" .hls)
    # Stage 10 release: redirect stdin from /dev/null so tests that use
    # read_line() don't hang waiting for input. The interpreter reads
    # EOF (returns empty tainted[str]); the native binary does the same
    # — the differential test still compares apples to apples.
    interp_out=$(python3 boot/boot.py "$f" </dev/null 2>/dev/null); interp_code=$?
    if ! python3 boot/boot.py src/hlc.hls "$f" "$TMP/$name.c" >/dev/null 2>&1; then
        bad "$name (hlc compile failed)"
        continue
    fi
    # Stage 77 (v0.96.0-alpha): `#![freestanding]` crates enter via
    # `_start` with no libc — the hosted link recipe (which expects
    # `main` + libc) cannot link them. Detect the crate attribute and
    # use the freestanding recipe instead (the same flags documented
    # in examples/freestanding_demo.hls): -ffreestanding -nostdlib
    # with -ffunction-sections + --gc-sections so the dead hosted-only
    # runtime sections vanish before undefined-symbol resolution.
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

echo "=== 3b. Stage 8-beta memory-stress (native RSS must stay flat) ==="
# The Stage 8 acceptance criterion: a memory-stress program does not
# increase RSS. The stress binary churns ~500k allocations of every heap
# shape; under the old arena model it would exhaust a 256 MB address
# space, with refcounting it completes with delta == 0 pages.
if python3 boot/boot.py src/hlc.hls tests/memcheck/stress_leak.hls "$TMP/stress.c" >/dev/null 2>&1 \
    && gcc -O2 -o "$TMP/stress" "$TMP/stress.c" -lm -pthread 2>/dev/null; then
    stress_out=$(bash -c "ulimit -v 262144; \"$TMP/stress\"" 2>/dev/null); stress_rc=$?
    if [ $stress_rc -eq 0 ]; then
        delta=$(echo "$stress_out" | grep "rss_delta_pages=" | cut -d= -f2)
        # Deep-scan-11 fix: if the stress binary produced no
        # `rss_delta_pages=` line (e.g. it crashed before printing
        # the result), `delta` is empty. The old test
        # `[ "" -le 1024 ]` errors with "integer expression expected"
        # (suppressed by 2>/dev/null) and falls through to the
        # `else` branch, printing the confusing "RSS grew by  pages"
        # message with a blank. Distinguish the two failure modes.
        if [ -z "$delta" ]; then
            bad "stress_leak: no rss_delta_pages= line in output (stress binary crashed before reporting?)"
        elif [ "$delta" -le 1024 ] 2>/dev/null; then
            ok "stress_leak under 256MB ulimit (rss delta=${delta} pages)"
        else
            bad "stress_leak RSS grew by ${delta} pages"
        fi
    else
        bad "stress_leak failed under 256MB ulimit (exit=$stress_rc)"
    fi
else
    bad "stress_leak (compile failed)"
fi

