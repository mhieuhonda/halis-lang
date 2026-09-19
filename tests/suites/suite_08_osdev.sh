#!/usr/bin/env bash
# Suite suite_08_osdev - Phase VI: OS development foundation.
# (Sourced by tests/run_tests.sh after suite_07_asm.sh.)

echo "=== 14. Stage 77: #![freestanding] mode (no libc, no OS calls) ==="
# Stage 77 (v0.96.0-alpha): the crate-level attribute disables libc
# linking, disables `std`, and exposes only `core` (+ relative
# imports). The C backend emits a freestanding TU (3 headers, bump
# arena, trap panics, `_start` entry with raw-syscall exit).
FS_F=tests/ok/feat_freestanding_basic.hls
# (a) the freestanding ok-test checks + runs on the interpreter (exit 0).
fs_interp_out=$(python3 boot/boot.py "$FS_F" </dev/null 2>/dev/null); fs_interp_code=$?
if [ "$fs_interp_code" -eq 0 ]; then
    ok "freestanding: feat_freestanding_basic runs on the interpreter (exit 0)"
else
    bad "freestanding: feat_freestanding_basic interpreter exit=$fs_interp_code"
fi
# (b) every Stage 77 fail program is rejected by Stage-0 with a
#     mode-specific message.
for ff in tests/fail/fail_freestanding_*.hls; do
    fs_name=$(basename "$ff" .hls)
    fs_err=$(python3 boot/boot.py --check "$ff" 2>&1 >/dev/null); fs_rc=$?
    # Deep-scan-28 fix: fail_freestanding_late_attr exercises the
    # crate-attribute POSITION rule — its message ("crate-level
    # attribute must appear before any item") does not mention
    # "freestanding", so this loop rejected the rejected-program
    # rejection. Align the needle with tests/freestanding_acceptance.py,
    # which already EXPECTs "before any item" for this file.
    fs_needle="freestanding"
    if [ "$fs_name" = "fail_freestanding_late_attr" ]; then
        fs_needle="before any item"
    fi
    if [ "$fs_rc" -eq 1 ] && echo "$fs_err" | grep -q "$fs_needle"; then
        ok "freestanding: $fs_name rejected ($fs_err)"
    else
        bad "freestanding: $fs_name not rejected with a freestanding message (rc=$fs_rc)"
    fi
done
# (c) the self-hosted compiler emits the freestanding TU shape.
if python3 boot/boot.py src/hlc.hls "$FS_F" "$TMP/fs_basic.c" >/dev/null 2>&1; then
    if grep -q "void _start(void)" "$TMP/fs_basic.c" \
        && ! grep -qE "int main\s*\(" "$TMP/fs_basic.c" \
        && grep -q "__builtin_trap" "$TMP/fs_basic.c" \
        && ! grep -q "#include <stdio.h>" "$TMP/fs_basic.c"; then
        ok "freestanding: hlc emits _start + traps with no hosted headers"
    else
        bad "freestanding: hlc emission has the wrong TU shape"
    fi
else
    bad "freestanding: hlc emission failed"
fi
# (d) the freestanding binary links -nostdlib and runs with the
#     interpreter's exit code (skipped gracefully without gcc).
if command -v gcc >/dev/null 2>&1; then
    if gcc -O2 -ffreestanding -nostdlib -ffunction-sections \
            -fno-stack-protector -Wl,--gc-sections \
            -o "$TMP/fs_basic" "$TMP/fs_basic.c" 2>"$TMP/fs_basic.gcc"; then
        "$TMP/fs_basic" </dev/null >/dev/null 2>&1; fs_nat_code=$?
        if [ "$fs_nat_code" == "$fs_interp_code" ]; then
            ok "freestanding: -nostdlib binary exit ($fs_nat_code) == interpreter"
        else
            bad "freestanding: -nostdlib binary exit=$fs_nat_code, interp=$fs_interp_code"
        fi
    else
        bad "freestanding: -nostdlib link failed"
        head -5 "$TMP/fs_basic.gcc"
    fi
else
    echo "  [SKIP] freestanding: gcc not installed (-nostdlib link skipped)"
    PASS=$((PASS+1))
fi
# (e) make freestanding-acceptance runs end-to-end (8 sections).
if make freestanding-acceptance >"$TMP/fs_acc77.log" 2>&1; then
    if grep -q "ACCEPTANCE OK: Stage 77" "$TMP/fs_acc77.log"; then
        ok "freestanding: make freestanding-acceptance runs end-to-end"
    else
        bad "freestanding: make freestanding-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/fs_acc77.log"
    fi
else
    bad "freestanding: make freestanding-acceptance failed"
    tail -15 "$TMP/fs_acc77.log"
fi

echo "=== 15. Stage 78: #![no_std] + core modules ==="
# Stage 78 (v0.97.0-alpha): the `core` family (option, result, iter,
# clone, eq) is importable from no_std / freestanding crates; hosted
# libc is kept (entry `main`).
NS_F=tests/ok/feat_nostd_core.hls
# (a) the no_std ok-test checks + runs on the interpreter (exit 0).
ns_interp_out=$(python3 boot/boot.py "$NS_F" </dev/null 2>/dev/null); ns_interp_code=$?
if [ "$ns_interp_code" -eq 0 ]; then
    ok "nostd: feat_nostd_core runs on the interpreter (exit 0)"
else
    bad "nostd: feat_nostd_core interpreter exit=$ns_interp_code"
fi
# (b) every core module resolves through the `core.` prefix.
for mod in option result iter clone eq; do
    if [ -f "core/$mod.hls" ]; then
        ok "nostd: core/$mod.hls present"
    else
        bad "nostd: core/$mod.hls missing"
    fi
done
# (c) every Stage 78 fail program is rejected with a mode message.
for ff in tests/fail/fail_nostd_*.hls; do
    ns_name=$(basename "$ff" .hls)
    ns_err=$(python3 boot/boot.py --check "$ff" 2>&1 >/dev/null); ns_rc=$?
    if [ "$ns_rc" -eq 1 ] && echo "$ns_err" | grep -q "no_std"; then
        ok "nostd: $ns_name rejected ($ns_err)"
    else
        bad "nostd: $ns_name not rejected with a no_std message (rc=$ns_rc)"
    fi
done
# (d) make nostd-acceptance runs end-to-end (8 sections).
if make nostd-acceptance >"$TMP/ns_acc78.log" 2>&1; then
    if grep -q "ACCEPTANCE OK: Stage 78" "$TMP/ns_acc78.log"; then
        ok "nostd: make nostd-acceptance runs end-to-end"
    else
        bad "nostd: make nostd-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/ns_acc78.log"
    fi
else
    bad "nostd: make nostd-acceptance failed"
    tail -15 "$TMP/ns_acc78.log"
fi
