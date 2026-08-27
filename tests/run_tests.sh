#!/usr/bin/env bash
# ============================================================================
# run_tests.sh — Bo kiem thu tong cua Hieu Louis (HLS)
#  1. Stage-0: chay cac chuong trinh ok (so sanh snapshot neu co)
#  2. Stage-0: tu choi cac chuong trinh fail (ky vong thong bao loi)
#  3. Tu dich: hlc.hls (via Stage-0) bien dich moi chuong trinh ok -> C
#     -> gcc -> chay native -> SO SANH VOI KET QUA THONG DICH (vi sai)
#  4. Bootstrap: hlc tu dich chinh no 2 lan -> 2 ban C phai giong het nhau
# ============================================================================
set -u
cd "$(dirname "$0")/.."
PASS=0
FAIL=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

ok()   { PASS=$((PASS+1)); echo "  [PASS] $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  [FAIL] $1"; }

echo "=== 1. Stage-0: chuong trinh hop le ==="
for f in tests/ok/*.hls; do
    name=$(basename "$f" .hls)
    out=$(python3 boot/boot.py "$f" 2>/dev/null); code=$?
    if [ $code -eq 0 ] || [ $code -eq 101 ]; then
        snap="tests/snapshots/$name.txt"
        if [ -f "$snap" ] && [ "$out" != "$(cat "$snap")" ]; then
            bad "$name (khac snapshot)"
        else
            ok "$name"
        fi
    else
        bad "$name (exit=$code)"
    fi
done

echo "=== 2. Stage-0: chuong trinh KY VONG LOI ==="
for f in tests/fail/*.hls; do
    name=$(basename "$f" .hls)
    err=$(python3 boot/boot.py --check "$f" 2>&1 >/dev/null); code=$?
    if [ $code -eq 1 ]; then
        ok "$name -> $err"
    else
        bad "$name (phai bi tu choi nhung exit=$code)"
    fi
done

echo "=== 3. Tu dich + kiem thu vi sai (interpreter vs native) ==="
for f in tests/ok/*.hls; do
    name=$(basename "$f" .hls)
    interp_out=$(python3 boot/boot.py "$f" 2>/dev/null); interp_code=$?
    if ! python3 boot/boot.py src/hlc.hls "$f" "$TMP/$name.c" >/dev/null 2>&1; then
        bad "$name (hlc bien dich that bai)"
        continue
    fi
    if ! gcc -O2 -o "$TMP/$name.bin" "$TMP/$name.c" -lm 2>"$TMP/$name.gcc"; then
        bad "$name (gcc loi)"
        continue
    fi
    nat_out=$("$TMP/$name.bin" 2>/dev/null); nat_code=$?
    if [ "$interp_out" == "$nat_out" ] && [ "$interp_code" == "$nat_code" ]; then
        ok "$name (native giong het interpreter)"
    else
        bad "$name (interp=$interp_code nat=$nat_code)"
        diff <(echo "$interp_out") <(echo "$nat_out") | head -4
    fi
done

echo "=== 4. Vi sai cho examples (co du lieu) ==="
python3 boot/boot.py examples/wordcount.hls examples/data.txt > "$TMP/wc_interp.txt" 2>/dev/null
python3 boot/boot.py src/hlc.hls examples/wordcount.hls "$TMP/wc.c" >/dev/null 2>&1
gcc -O2 -o "$TMP/wc.bin" "$TMP/wc.c" -lm 2>/dev/null
"$TMP/wc.bin" examples/data.txt > "$TMP/wc_nat.txt" 2>/dev/null
if diff -q "$TMP/wc_interp.txt" "$TMP/wc_nat.txt" >/dev/null 2>&1; then
    ok "wordcount (native vs interpreter)"
else
    bad "wordcount"
fi

echo "=== 5. BOOTSTRAP: hlc tu dich chinh no (fixed-point) ==="
echo "  [5.1] Stage-0 chay hlc.hls de bien dich chinh hlc.hls..."
if python3 boot/boot.py src/hlc.hls src/hlc.hls "$TMP/hlc_s1.c" >/dev/null 2>&1; then
    ok "hlc.hls tu dich qua Stage-0"
else
    bad "hlc.hls tu dich qua Stage-0"
fi
echo "  [5.2] Bien dich ban C dau tien thanh hlc native..."
if gcc -O2 -o "$TMP/hlc1" "$TMP/hlc_s1.c" -lm 2>/dev/null; then
    ok "gcc bien dich hlc native"
else
    bad "gcc bien dich hlc native"
fi
echo "  [5.3] hlc native tu bien dich hlc.hls lan 2..."
"$TMP/hlc1" src/hlc.hls "$TMP/hlc_s2.c" 2>/dev/null
if [ $? -eq 0 ]; then
    ok "hlc native bien dich hlc.hls"
else
    bad "hlc native bien dich hlc.hls"
fi
if diff -q "$TMP/hlc_s1.c" "$TMP/hlc_s2.c" >/dev/null 2>&1; then
    ok "BOOTSTRAP XAC DINH: 2 lan sinh ma giong het nhau"
else
    bad "2 lan sinh ma khac nhau!"
fi
echo "  [5.4] hlc native bien dich chuong trinh thu..."
"$TMP/hlc1" examples/fibonacci.hls "$TMP/fib.c" >/dev/null 2>&1 \
    && gcc -O2 -o "$TMP/fib" "$TMP/fib.c" -lm 2>/dev/null \
    && nat=$("$TMP/fib" 2>/dev/null) \
    && interp=$(python3 boot/boot.py examples/fibonacci.hls 2>/dev/null)
if [ "$nat" == "$interp" ]; then
    ok "hlc native bien dich + chay fibonacci dung"
else
    bad "hlc native bien dich fibonacci"
fi

echo ""
echo "=========================================="
echo "KET QUA: $PASS PASS / $FAIL FAIL"
echo "=========================================="
[ $FAIL -eq 0 ]
