#!/usr/bin/env bash
# ============================================================================
# run_llvm_tests.sh — LLVM IR backend tests (Stage 12)
#
#   1. Emit LLVM IR for every tests/llvm/*.hls program (supported subset).
#   2. Validate the emitted IR STRUCTURALLY with tools/ll_validate.py
#      (terminators, declared symbols, store/load type agreement, phi
#      predecessors, `ptr <int>` literals, duplicate labels...).
#      If `llvm-as` is available on this machine, additionally assemble
#      the IR for a real parser check.
#   3. Compare the INTERPRETER's output of each program against the
#      expected snapshot embedded below (the differential native-vs-
#      interpreter loop of run_tests.sh covers the C backend; this file
#      covers the LLVM text path).
#
# This suite exists because the old CI only piped `--emit llvm` output to
# /dev/null — the emitter could ship structurally invalid IR while every
# scan reported green (see the v0.16.0-alpha changelog).
# ============================================================================
set -u
cd "$(dirname "$0")/.."
PASS=0
FAIL=0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM HUP

ok()   { PASS=$((PASS+1)); echo "  [PASS] $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  [FAIL] $1"; }

echo "=== LLVM IR backend: emit + structural validation ==="
for f in tests/llvm/*.hls; do
    name=$(basename "$f" .hls)
    if ! python3 boot/boot.py --emit llvm "$f" > "$TMP/$name.ll" 2> "$TMP/$name.err"; then
        bad "$name (emit failed: $(head -1 "$TMP/$name.err"))"
        continue
    fi
    if ! python3 tools/ll_validate.py "$TMP/$name.ll" > /dev/null 2> "$TMP/$name.val"; then
        bad "$name (invalid IR: $(rg -o 'V\d .{0,60}' "$TMP/$name.val" | head -2))"
        continue
    fi
    if command -v llvm-as >/dev/null 2>&1; then
        if ! llvm-as "$TMP/$name.ll" -o "$TMP/$name.bc" 2> "$TMP/$name.as"; then
            bad "$name (llvm-as rejected: $(head -1 "$TMP/$name.as"))"
            continue
        fi
    fi
    ok "$name (IR valid$(command -v llvm-as >/dev/null 2>&1 && echo ' + assembled'))"
done

echo "=== LLVM IR backend: interpreter snapshots (behaviour of the subset) ==="
# Expected outputs are checked against the interpreter, which is the
# reference semantics for the LLVM path's supported subset.
run_and_check() {
    local name="$1"; shift
    local expected="$1"; shift
    local actual
    actual=$(python3 boot/boot.py "tests/llvm/$name.hls" 2>/dev/null)
    if [ "$actual" == "$expected" ]; then
        ok "$name (interpreter snapshot)"
    else
        bad "$name (interpreter output mismatch)"
        diff <(echo "$expected") <(echo "$actual") | head -5
    fi
}
run_and_check bools $'bool ops ok\neq=true ne=true'
run_and_check lists $'total=100\nlen=2 first=a\npopped=40'
run_and_check shortcircuit $'short-circuit ok'
run_and_check divmod $'q=3 r=1 neg=-3\ndivmod ok'
run_and_check strings $'string ops ok\nlen=3'
run_and_check loops $'total=25\ni=3'

echo "=== LLVM IR backend: unsupported constructs fail CLEANLY ==="
# struct/enum/match/?/user-methods must raise a clear HLError, not emit
# silently-broken IR.
printf 'struct S { x: int }\nfn main() -> int { let s: S = S { x: 1 }\n return s.x }\n' > "$TMP/unsup.hls"
python3 boot/boot.py --emit llvm "$TMP/unsup.hls" > /dev/null 2> "$TMP/unsup.err"
rc=$?
if [ $rc -ne 0 ] && rg -q "not yet supported by --emit llvm" "$TMP/unsup.err" && ! rg -q "Traceback" "$TMP/unsup.err"; then
    ok "unsupported construct -> clean error"
else
    bad "unsupported construct (no clean error; rc=$rc; got: $(head -1 "$TMP/unsup.err"))"
fi

echo ""
echo "=========================================="
echo "RESULT: $PASS PASS / $FAIL FAIL"
echo "=========================================="
[ $FAIL -eq 0 ]
