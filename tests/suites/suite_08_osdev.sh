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

echo "=== 16. Stage 79: core.alloc — pluggable allocator protocol ==="
# Stage 79 (v0.98.0-alpha): the Alloc protocol over `Layout` +
# `AllocError` + three reference allocators (BumpAlloc, PoolAlloc,
# NullAlloc) plus an AllocStats accounting helper. Pure HLS, all
# `no_std`-clean; the same module is importable from `#![freestanding]`.
ALLOC_F=tests/ok/feat_stage79_alloc.hls
# (a) the alloc ok-test checks + runs on the interpreter (exit 0).
alloc_interp_out=$(python3 boot/boot.py "$ALLOC_F" </dev/null 2>/dev/null); alloc_interp_code=$?
if [ "$alloc_interp_code" -eq 0 ]; then
    ok "alloc: feat_stage79_alloc runs on the interpreter (exit 0)"
else
    bad "alloc: feat_stage79_alloc interpreter exit=$alloc_interp_code"
fi
# (b) the alloc module resolves through the `core.` prefix.
if [ -f "core/alloc.hls" ]; then
    ok "alloc: core/alloc.hls present"
else
    bad "alloc: core/alloc.hls missing"
fi
# (c) the demo runs end-to-end on the interpreter (exit 0).
alloc_demo_code=$(python3 boot/boot.py examples/alloc_demo.hls </dev/null 2>/dev/null; echo $?)
if [ "$alloc_demo_code" -eq 0 ]; then
    ok "alloc: examples/alloc_demo.hls runs on the interpreter (exit 0)"
else
    bad "alloc: examples/alloc_demo.hls interpreter exit=$alloc_demo_code"
fi
# (d) the same ok-test with #![freestanding] instead of #![no_std]
#     links -nostdlib and exits with the same code (0).
alloc_fs_src=$(mktemp --suffix=.hls)
python3 - "$ALLOC_F" "$alloc_fs_src" <<'PY'
import sys
src = open(sys.argv[1], encoding='utf-8').read()
src = src.replace('\n#![no_std]\n', '\n#![freestanding]\n', 1)
open(sys.argv[2], 'w', encoding='utf-8', newline='\n').write(src)
PY
if python3 boot/boot.py src/hlc.hls "$alloc_fs_src" "$TMP/alloc_fs.c" >/dev/null 2>&1 \
        && gcc -O2 -ffreestanding -nostdlib -ffunction-sections \
               -fno-stack-protector -Wl,--gc-sections \
               -o "$TMP/alloc_fs.bin" "$TMP/alloc_fs.c" 2>"$TMP/alloc_fs.gcc"; then
    alloc_fs_nat=$("$TMP/alloc_fs.bin" </dev/null 2>/dev/null; echo $?)
    if [ "$alloc_fs_nat" == "$alloc_interp_code" ]; then
        ok "alloc: freestanding -nostdlib binary exit ($alloc_fs_nat) == interpreter"
    else
        bad "alloc: freestanding binary exit=$alloc_fs_nat, interp=$alloc_interp_code"
    fi
else
    bad "alloc: freestanding -nostdlib link failed"
    head -5 "$TMP/alloc_fs.gcc"
fi
rm -f "$alloc_fs_src"
# (e) make alloc-acceptance runs end-to-end (7 sections).
if make alloc-acceptance >"$TMP/alloc_acc79.log" 2>&1; then
    if grep -q "ACCEPTANCE OK: Stage 79" "$TMP/alloc_acc79.log"; then
        ok "alloc: make alloc-acceptance runs end-to-end"
    else
        bad "alloc: make alloc-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/alloc_acc79.log"
    fi
else
    bad "alloc: make alloc-acceptance failed"
    tail -15 "$TMP/alloc_acc79.log"
fi

echo "=== 17. Stage 80: core.mem — physical-page allocator + page tables ==="
# Stage 80 (v0.99.0-alpha): the FrameAlloc bitmap allocator over a
# physical region (first-fit, contiguous runs, 2-MiB-aligned huge
# frames, double-free detection, reservations) plus the x86-64
# 4-level page-table model (AddressSpace with on-demand table
# creation billed to the frame allocator, atomic two-pass mapping,
# translate, unmap, huge maps, protect). Pure HLS, `no_std`-clean;
# the same module is importable from `#![freestanding]`.
MEM_F=tests/ok/feat_stage80_mem.hls
# (a) the mem ok-test checks + runs on the interpreter (exit 0).
mem_interp_out=$(python3 boot/boot.py "$MEM_F" </dev/null 2>/dev/null); mem_interp_code=$?
if [ "$mem_interp_code" -eq 0 ]; then
    ok "mem: feat_stage80_mem runs on the interpreter (exit 0)"
else
    bad "mem: feat_stage80_mem interpreter exit=$mem_interp_code"
fi
# (b) the mem module resolves through the `core.` prefix.
if [ -f "core/mem.hls" ]; then
    ok "mem: core/mem.hls present"
else
    bad "mem: core/mem.hls missing"
fi
# (c) the demo runs end-to-end on the interpreter (exit 0).
mem_demo_code=$(python3 boot/boot.py examples/mem_demo.hls </dev/null 2>/dev/null; echo $?)
if [ "$mem_demo_code" -eq 0 ]; then
    ok "mem: examples/mem_demo.hls runs on the interpreter (exit 0)"
else
    bad "mem: examples/mem_demo.hls interpreter exit=$mem_demo_code"
fi
# (d) the same ok-test with #![freestanding] instead of #![no_std]
#     links -nostdlib and exits with the same code (0).
mem_fs_src=$(mktemp --suffix=.hls)
python3 - "$MEM_F" "$mem_fs_src" <<'PY'
import sys
src = open(sys.argv[1], encoding='utf-8').read()
src = src.replace('\n#![no_std]\n', '\n#![freestanding]\n', 1)
open(sys.argv[2], 'w', encoding='utf-8', newline='\n').write(src)
PY
if python3 boot/boot.py src/hlc.hls "$mem_fs_src" "$TMP/mem_fs.c" >/dev/null 2>&1 \
        && gcc -O2 -ffreestanding -nostdlib -ffunction-sections \
               -fno-stack-protector -Wl,--gc-sections \
               -o "$TMP/mem_fs.bin" "$TMP/mem_fs.c" 2>"$TMP/mem_fs.gcc"; then
    mem_fs_nat=$("$TMP/mem_fs.bin" </dev/null 2>/dev/null; echo $?)
    if [ "$mem_fs_nat" == "$mem_interp_code" ]; then
        ok "mem: freestanding -nostdlib binary exit ($mem_fs_nat) == interpreter"
    else
        bad "mem: freestanding binary exit=$mem_fs_nat, interp=$mem_interp_code"
    fi
else
    bad "mem: freestanding -nostdlib link failed"
    head -5 "$TMP/mem_fs.gcc"
fi
rm -f "$mem_fs_src"
# (e) make mem-acceptance runs end-to-end (8 sections).
if make mem-acceptance >"$TMP/mem_acc80.log" 2>&1; then
    if grep -q "ACCEPTANCE OK: Stage 80" "$TMP/mem_acc80.log"; then
        ok "mem: make mem-acceptance runs end-to-end"
    else
        bad "mem: make mem-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/mem_acc80.log"
    fi
else
    bad "mem: make mem-acceptance failed"
    tail -15 "$TMP/mem_acc80.log"
fi

echo "=== 18. Stage 81: core.panic + #[panic_handler] ==="
# Stage 81 (v0.100.0-alpha): the kernel panic strategy — PanicInfo /
# PanicAction / PanicLog in `core.panic` plus the single overridable
# `#[panic_handler] fn panic_handler(msg: str) -> void`, invoked once
# per panic (reentrancy-guarded) before the default action. Pure HLS,
# `no_std`-clean; importable from `#![freestanding]` identically.
PANIC_F=tests/ok/feat_stage81_panic.hls
# (a) the panic ok-test checks + runs on the interpreter (exit 0 —
#     the effect-free handler is wired but never fires here).
panic_interp_out=$(python3 boot/boot.py "$PANIC_F" </dev/null 2>/dev/null); panic_interp_code=$?
if [ "$panic_interp_code" -eq 0 ]; then
    ok "panic: feat_stage81_panic runs on the interpreter (exit 0)"
else
    bad "panic: feat_stage81_panic interpreter exit=$panic_interp_code"
fi
# (b) the panic module resolves through the `core.` prefix.
if [ -f "core/panic.hls" ]; then
    ok "panic: core/panic.hls present"
else
    bad "panic: core/panic.hls missing"
fi
# (c) the hosted demo panics with the handler marker first (exit 101).
panic_demo_out=$(python3 boot/boot.py examples/panic_demo.hls </dev/null 2>/dev/null); panic_demo_code=$?
panic_demo_err=$(python3 boot/boot.py examples/panic_demo.hls </dev/null 2>&1 >/dev/null)
if [ "$panic_demo_code" -eq 101 ] && echo "$panic_demo_out" | grep -q "kernel panic: demo fault"; then
    ok "panic: examples/panic_demo.hls handler fires, exit 101"
else
    bad "panic: examples/panic_demo.hls exit=$panic_demo_code (want 101 + marker)"
fi
if echo "$panic_demo_err" | grep -q "panic: demo fault"; then
    ok "panic: default report still runs after the handler"
else
    bad "panic: default report missing after the handler"
fi
# (d) the same ok-test with #![freestanding] instead of #![no_std]
#     links -nostdlib and exits with the same code (0).
panic_fs_src=$(mktemp --suffix=.hls)
python3 - "$PANIC_F" "$panic_fs_src" <<'PY'
import sys
src = open(sys.argv[1], encoding='utf-8').read()
src = src.replace('\n#![no_std]\n', '\n#![freestanding]\n', 1)
open(sys.argv[2], 'w', encoding='utf-8', newline='\n').write(src)
PY
if python3 boot/boot.py src/hlc.hls "$panic_fs_src" "$TMP/panic_fs.c" >/dev/null 2>&1 \
        && gcc -O2 -ffreestanding -nostdlib -ffunction-sections \
               -fno-stack-protector -Wl,--gc-sections \
               -o "$TMP/panic_fs.bin" "$TMP/panic_fs.c" 2>"$TMP/panic_fs.gcc"; then
    panic_fs_nat=$("$TMP/panic_fs.bin" </dev/null 2>/dev/null; echo $?)
    if [ "$panic_fs_nat" == "$panic_interp_code" ]; then
        ok "panic: freestanding -nostdlib binary exit ($panic_fs_nat) == interpreter"
    else
        bad "panic: freestanding binary exit=$panic_fs_nat, interp=$panic_interp_code"
    fi
else
    bad "panic: freestanding -nostdlib link failed"
    head -5 "$TMP/panic_fs.gcc"
fi
rm -f "$panic_fs_src"
# (e) make panic-acceptance runs end-to-end (7 sections).
if make panic-acceptance >"$TMP/panic_acc81.log" 2>&1; then
    if grep -q "ACCEPTANCE OK: Stage 81" "$TMP/panic_acc81.log"; then
        ok "panic: make panic-acceptance runs end-to-end"
    else
        bad "panic: make panic-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/panic_acc81.log"
    fi
else
    bad "panic: make panic-acceptance failed"
    tail -15 "$TMP/panic_acc81.log"
fi

echo "=== 19. Stage 82: deterministic stack size + guard pages ==="
# Stage 82 (v0.101.0-alpha): `#![stack_size(N)]` — the checker computes
# every fn's worst-case frame, walks the call graph, rejects recursion
# cycles, and proves the worst chain fits N bytes. `core.stack` models
# the region (StackConfig / StackFault / probes / canary / planning)
# and the runtime pins task stacks (1 MiB + 4-KiB guard page).
STK_F=tests/ok/feat_stage82_stack.hls
# (a) the stack ok-test checks + runs on the interpreter (exit 0).
stk_interp_out=$(python3 boot/boot.py "$STK_F" </dev/null 2>/dev/null); stk_interp_code=$?
if [ "$stk_interp_code" -eq 0 ]; then
    ok "stack: feat_stage82_stack runs on the interpreter (exit 0)"
else
    bad "stack: feat_stage82_stack interpreter exit=$stk_interp_code"
fi
# (b) the stack module resolves through the `core.` prefix.
if [ -f "core/stack.hls" ]; then
    ok "stack: core/stack.hls present"
else
    bad "stack: core/stack.hls missing"
fi
# (c) every Stage 82 fail program is rejected by Stage-0 with the
#     Stage 82 message, and the self-hosted checker agrees.
for ff in tests/fail/fail_stack_budget_exceeded.hls \
          tests/fail/fail_stack_budget_recursion.hls \
          tests/fail/fail_stack_size_fn.hls; do
    stk_name=$(basename "$ff" .hls)
    stk_err=$(python3 boot/boot.py --check "$ff" 2>&1 >/dev/null); stk_rc=$?
    if [ "$stk_rc" -eq 1 ] && echo "$stk_err" | grep -qE "stack_size|bounded recursion"; then
        ok "stack: $stk_name rejected by boot ($stk_err)"
    else
        bad "stack: $stk_name not rejected with a stack message (rc=$stk_rc)"
    fi
    if python3 boot/boot.py src/hlc.hls "$ff" "$TMP/stk_fail.c" >/dev/null 2>&1; then
        bad "stack: $stk_name accepted by the self-hosted checker"
    else
        ok "stack: $stk_name rejected by hlc too (parity)"
    fi
done
# (d) the hosted demo hits the guard (exit 101, handler marker first).
stk_demo_out=$(python3 boot/boot.py examples/stack_guard_demo.hls </dev/null 2>/dev/null); stk_demo_code=$?
if [ "$stk_demo_code" -eq 101 ] && echo "$stk_demo_out" | grep -q "kernel panic: stack overflow"; then
    ok "stack: examples/stack_guard_demo.hls hits the guard, exit 101"
else
    bad "stack: examples/stack_guard_demo.hls exit=$stk_demo_code (want 101 + marker)"
fi
# (e) make stackguard-acceptance runs end-to-end (7 sections).
if make stackguard-acceptance >"$TMP/stk_acc82.log" 2>&1; then
    if grep -q "ACCEPTANCE OK: Stage 82" "$TMP/stk_acc82.log"; then
        ok "stack: make stackguard-acceptance runs end-to-end"
    else
        bad "stack: make stackguard-acceptance did not print ACCEPTANCE OK"
        tail -5 "$TMP/stk_acc82.log"
    fi
else
    bad "stack: make stackguard-acceptance failed"
    tail -15 "$TMP/stk_acc82.log"
fi
