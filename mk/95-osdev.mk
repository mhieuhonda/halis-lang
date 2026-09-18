# ============================================================================
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

.PHONY: freestanding freestanding-check freestanding-acceptance nostd nostd-acceptance
