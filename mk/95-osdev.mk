# Stage 84 (v0.103.0-alpha): linker-script integration (`#![link_script]`)
# + custom sections (`#[section("NAME")]` / `#[align(N)]`) — see SPEC
# section 38.
# ============================================================================

# Stage 84: linker-script integration and custom sections
LINKER_SCRIPT ?= link.ld
LINKER_TEST_SRC ?= tests/stage84_linker_sections.c
LINKER_TEST_OBJ ?= build/stage84-linker-test.o
LINKER_TEST_BIN ?= build/stage84-linker-test.elf
# The C compiler driver also locates `ld` and `nm`, so derive them from
# it rather than assuming bare `ld`/`nm` are on PATH (a bare `ld` is not
# installed on most distros — `ld` is a gcc-internal binary, reachable
# only as `$(CC) -print-prog-name=ld`). An empty NM used to make the
# verification pipeline `| grep -q ...` with no producer, which blocks
# forever on stdin.
LINKER_TEST_CC ?= $(CC)
LINKER_TEST_LD ?= $(shell $(CC) -print-prog-name=ld)
LINKER_TEST_NM ?= $(shell $(CC) -print-prog-name=nm)

.PHONY: test-linker
test-linker: $(LINKER_TEST_BIN)
	@$(LINKER_TEST_NM) $(LINKER_TEST_BIN) | grep -q '__halis_metadata_start' && \
	 $(LINKER_TEST_NM) $(LINKER_TEST_BIN) | grep -q '__halis_sections_start' && \
	 $(LINKER_TEST_NM) $(LINKER_TEST_BIN) | grep -q '__halis_sections_end'

$(LINKER_TEST_OBJ): $(LINKER_TEST_SRC) $(LINKER_SCRIPT)
	@mkdir -p $(dir $@)
	$(LINKER_TEST_CC) $(CPPFLAGS) $(CFLAGS) -ffreestanding -fno-pie -c $< -o $@

$(LINKER_TEST_BIN): $(LINKER_TEST_OBJ) $(LINKER_SCRIPT)
	$(LINKER_TEST_LD) -T $(LINKER_SCRIPT) -o $@ $<

# boot-acceptance: the Stage 85 acceptance gate. Runs the 7-section
# end-to-end suite for the boot protocols: the crate attribute and its
# guards (protocol name, entry-file-only, freestanding-only, once per
# crate, and a linker script that drops the header section), the
# core/boot.hls surface, the emitted Multiboot2 header (magic,
# architecture, 40-byte length, checksum, module + end tags) and Limine
# request list (both delimiters, the common magic and all five request
# IDs), the LINKED image inspected with objdump (the header really is in
# its section, really is 8-byte aligned, and really starts with the
# protocol's own bytes), nine behaviour probes plus the four
# checked-constructor panics, and the demos, the tools and `--audit`.
boot-acceptance:
	@echo "[Stage 85 acceptance] running tests/boot_acceptance.py..."
	@$(PYTHON) tests/boot_acceptance.py
	@echo "ACCEPTANCE OK: Stage 85 -- Multiboot2 + Limine boot protocol headers"

# link-acceptance: the Stage 84 acceptance gate. Runs the 7-section
# end-to-end suite for the linker script + custom sections: resolution +
# guards, the standalone parse of core/section.hls, Stage-0 enforcement
# (the no_std ok-test clean, the demo running, all 13 fail programs
# rejected with their own diagnostic by BOTH compilers), nine targeted
# behaviour probes (name validation, kinds, the script reader, comments,
# coverage, the placement solver, the overlap/align faults, and the
# freestanding arena budget), self-hosted emission + native parity (the
# C carries section/aligned, the ok-test links -nostdlib, a covered
# section compiles and runs under the shipped link.ld), hlfmt + hllint,
# and `--audit` / `--opt-stats` reporting.
link-acceptance:
	@echo "[Stage 84 acceptance] running tests/link_acceptance.py..."
	@$(PYTHON) tests/link_acceptance.py
	@echo "ACCEPTANCE OK: Stage 84 -- linker-script integration + custom sections"

.PHONY: freestanding freestanding-check freestanding-acceptance nostd nostd-acceptance alloc-acceptance mem-acceptance panic-acceptance stackguard-acceptance asmreg-acceptance link-acceptance boot-acceptance test-linker
# Phase VI — OS development foundation (Stages 77-78+)
# ============================================================================
# These stages give the LANGUAGE the capabilities OS developers need.
# The Halis project itself does not write an OS; these features let
# users do so (ROADMAP.md "STAGES 77-96").
#
# Stage 77 (v0.96.0-alpha): `#![freestanding]` mode (no libc, no OS
# calls, std disabled, entry `_start`).
# Stage 78 (v0.97.0-alpha): `#![no_std]` + the `core` module family
# (option, result, iter, clone, eq).
# Stage 79 (v0.98.0-alpha): `core.alloc` — the Alloc protocol
# (Layout + AllocError + BumpAlloc + PoolAlloc + NullAlloc +
# AllocStats).
# Stage 80 (v0.99.0-alpha): `core.mem` — the physical-page allocator
# (FrameAlloc) and the x86-64 4-level page-table model
# (AddressSpace, huge pages, translate/unmap/protect).
# Stage 81 (v0.100.0-alpha): `core.panic` + `#[panic_handler]` — the
# kernel panic strategy (PanicInfo / PanicAction / PanicLog plus the
# single overridable handler behind the reentrancy-guarded hook).
# Stage 82 (v0.101.0-alpha): the deterministic stack size + guard
# pages (`#![stack_size(N)]` budget verified against the worst call
# chain, `core.stack` region model, 1-MiB + guard task stacks in the
# C runtime).
# Stage 83 (v0.102.0-alpha): the inline-asm register constraints —
# explicit `clobber(...)` lists, named registers binding the full
# 64-bit register, SSE-only floats, compiler-owned sp/bp, operand-
# clobber overlap analysis, and `core.asm` (the register file as
# data) — see SPEC section 37.
# Stage 84 (v0.103.0-alpha): the linker script — `#![link_script]`
# names the script, `#[section("NAME")]` places a function in a named
# output section (validated, and checked against the script), and
# `#[align(N)]` pins its entry; `core.section` models the whole layout
# story in pure HLS — see SPEC section 38.
# Stage 85 (v0.104.0-alpha): the boot protocols — `#![boot_header]`
# makes the C backend emit the Multiboot2 or Limine header a firmware
# scans for, and the Stage 84 placement check proves the linker script
# KEEPs it; `core.boot` models the received side (both tag encodings,
# both memory-map encodings, the framebuffer) — see SPEC section 39.
# ============================================================================

# freestanding: compile an HLS program to freestanding C + link with
# -nostdlib into a runnable binary.
#   make freestanding F=examples/freestanding_demo.hls [OUT=/tmp/fs_demo]
# The link recipe: -ffreestanding -nostdlib with -ffunction-sections
# + --gc-sections (drops the dead hosted-only runtime sections before
# undefined-symbol resolution) and -fno-stack-protector (no
# __stack_chk_fail without libc). The binary enters via `_start` and
# exits via a raw syscall; its exit code is the program's answer.
freestanding:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@test -n "$(F)" || (echo "Usage: make freestanding F=examples/freestanding_demo.hls [OUT=/tmp/fs_demo]" && false)
	@mkdir -p $(BIN)
	@if [ -z "$(OUT)" ]; then OUT=$(BIN)/fs_demo; fi; \
	  $(BIN)/hlc $(F) $$OUT.c && \
	  $(CC) -O2 -ffreestanding -nostdlib -ffunction-sections \
	    -fno-stack-protector -Wl,--gc-sections \
	    -o $$OUT $$OUT.c && \
	  echo "freestanding binary: $$OUT (entry _start, no libc)"

# freestanding-check: compile to freestanding C and verify the TU
# shape without linking (works even without a freestanding-capable
# linker): only the 3 freestanding headers, `_start` present, no
# `int main(`, panics trap, bump allocator present.
# Usage: make freestanding-check [F=examples/freestanding_demo.hls]
freestanding-check:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@if [ -z "$(F)" ]; then F=examples/freestanding_demo.hls; fi; \
	  $(BIN)/hlc $$F $(BIN)/fs_check.c && \
	  $(PYTHON) -c "import re,sys; \
	    src = open('$(BIN)/fs_check.c', encoding='utf-8', errors='replace').read(); \
	    inc = sorted(set(re.findall(r'#include\s*<([^>]+)>', src))); \
	    assert inc == ['stdbool.h', 'stddef.h', 'stdint.h'], inc; \
	    assert 'void _start(void)' in src; \
	    assert not re.search(r'int main\s*\(', src); \
	    assert '__builtin_trap' in src and 'exit(101)' not in src; \
	    assert 'hl_bump_alloc(size_t n)' in src; \
	    print('freestanding TU shape: OK (%d bytes, %s)' % (len(src), inc))"

# freestanding-acceptance: the Stage 77 acceptance gate. Runs the
# 8-section end-to-end suite: crate-attribute parsing, Stage-0
# enforcement, entry-only rule + implication, float-library
# denylist, --audit + hlfmt, self-hosted emission shape, the
# link-closure proof, and self-hosted parity on fail programs.
freestanding-acceptance:
	@echo "[Stage 77 acceptance] running tests/freestanding_acceptance.py..."
	@$(PYTHON) tests/freestanding_acceptance.py
	@echo "ACCEPTANCE OK: Stage 77 -- #![freestanding] mode (no libc, no OS calls)"

# nostd: compile a `#![no_std]` program to hosted C + link normally.
#   make nostd F=examples/nostd_demo.hls [OUT=/tmp/nostd_demo]
# A no_std crate keeps libc (entry `main`) but may only use `core.*`
# (plus the pure core language) — no `std`, no effects.
nostd:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@test -n "$(F)" || (echo "Usage: make nostd F=examples/nostd_demo.hls [OUT=/tmp/nostd_demo]" && false)
	@mkdir -p $(BIN)
	@if [ -z "$(OUT)" ]; then OUT=$(BIN)/nostd_demo; fi; \
	  $(BIN)/hlc $(F) $$OUT.c && \
	  $(CC) -O2 -o $$OUT $$OUT.c -lm -pthread && \
	  echo "no_std binary: $$OUT (entry main, core.* only)"

# nostd-acceptance: the Stage 78 acceptance gate. Runs the 8-section
# end-to-end suite: core. resolution, standalone parsing, Stage-0
# enforcement, core API behavior, the freestanding+core bridge,
# self-hosted emission + parity, hlfmt stability, mode distinction.
nostd-acceptance:
	@echo "[Stage 78 acceptance] running tests/nostd_acceptance.py..."
	@$(PYTHON) tests/nostd_acceptance.py
	@echo "ACCEPTANCE OK: Stage 78 -- #![no_std] + core modules"

# alloc-acceptance: the Stage 79 acceptance gate. Runs the 7-section
# end-to-end suite for `core.alloc`: resolution + guards, standalone
# parse + core-only imports, Stage-0 enforcement in both no_std and
# freestanding modes, targeted behavior probes (layout validation,
# bump+reset+OOM, alignment padding, pool LIFO recycle, double-free,
# null sentinel, stats accounting), self-hosted emission + native
# parity (incl. freestanding), hlfmt stability, and `--audit` purity.
alloc-acceptance:
	@echo "[Stage 79 acceptance] running tests/alloc_acceptance.py..."
	@$(PYTHON) tests/alloc_acceptance.py
	@echo "ACCEPTANCE OK: Stage 79 -- core.alloc (pluggable allocator protocol)"

# mem-acceptance: the Stage 80 acceptance gate. Runs the 8-section
# end-to-end suite for `core.mem`: resolution + guards, standalone
# parse + core-only imports, Stage-0 enforcement in both no_std and
# freestanding modes, targeted behavior probes (page geometry +
# canonical math, frame alloc/free/reuse/OOM, runs + 2-MiB huge
# alignment, PTE format + NX round trip, map with on-demand tables,
# atomicity under frame exhaustion, unmap + protect, huge map +
# unmap), self-hosted emission + native parity (incl. freestanding),
# hlfmt stability, and `--audit` purity.
mem-acceptance:
	@echo "[Stage 80 acceptance] running tests/mem_acceptance.py..."
	@$(PYTHON) tests/mem_acceptance.py
	@echo "ACCEPTANCE OK: Stage 80 -- core.mem (physical-page allocator + page tables)"

# panic-acceptance: the Stage 81 acceptance gate. Runs the 7-section
# end-to-end suite for `core.panic` + `#[panic_handler]`: resolution +
# guards, standalone parse + core-only imports, Stage-0 enforcement
# (hosted demo panics with the handler marker, no_std ok-test clean in
# no_std / freestanding / bare-hosted modes, three fail programs
# rejected), targeted behavior probes (info geometry, log eviction,
# guard panics, handler-fires, exit-wins, nested-panic guard),
# self-hosted emission + native parity (hosted marker-before-default
# at 101, exit-wins at 42, ok-test at 0, freestanding -nostdlib at 0,
# boot/hlc parity on fail programs), hlfmt stability, and `--audit`
# purity.
panic-acceptance:
	@echo "[Stage 81 acceptance] running tests/panic_acceptance.py..."
	@$(PYTHON) tests/panic_acceptance.py
	@echo "ACCEPTANCE OK: Stage 81 -- core.panic + #[panic_handler]"

# stackguard-acceptance: the Stage 82 acceptance gate. Runs the
# 7-section end-to-end suite for the deterministic stack size + guard
# pages: resolution + guards, standalone parse + core-only imports,
# Stage-0 enforcement (hosted demo hits the guard at 101 with the
# handler marker, no_std ok-test clean in no_std / freestanding /
# bare-hosted modes, three fail programs rejected), budget probes
# (fitting program accepted with the audited worst chain, over-budget
# + recursion-cycle rejected, six guard-panic probes), self-hosted
# emission + native parity (hosted demo at 101, ok-test at 0,
# freestanding -nostdlib at 0, task runtime carries hl_thread_start /
# HL_TASK_STACK_BYTES / pthread_attr_setguardsize, boot/hlc parity on
# fail programs), hlfmt stability, and `--audit` purity.
stackguard-acceptance:
	@echo "[Stage 82 acceptance] running tests/stack_acceptance.py..."
	@$(PYTHON) tests/stack_acceptance.py
	@echo "ACCEPTANCE OK: Stage 82 -- deterministic stack size + guard pages"

# asmreg-acceptance: the Stage 83 acceptance gate. Runs the 7-section
# end-to-end suite for the inline-asm register constraints: resolution
# + guards, standalone parse (enum + fns, zero imports), Stage-0
# enforcement (the no_std ok-test clean, the demo refused by the
# interpreter because it executes asm!, the ten fail programs
# rejected), targeted probes (valid syscall-shaped/xmm/bool programs
# accepted; every diagnostic class — width, owner, overlap, duplicate,
# reserved, SSE — rejected with its exact message), self-hosted
# emission + native parity (local register variables for r10/r11/r14/
# xmm, explicit clobber lists, -Werror link, DEMO OK, freestanding
# -nostdlib at 0, boot/hlc parity on fail programs), hlfmt stability,
# and `--audit` purity.
asmreg-acceptance:
	@echo "[Stage 83 acceptance] running tests/asmreg_acceptance.py..."
	@$(PYTHON) tests/asmreg_acceptance.py
	@echo "ACCEPTANCE OK: Stage 83 -- inline-asm register constraints (clobber, input, output)"

