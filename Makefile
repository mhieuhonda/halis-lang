# ============================================================================
# Makefile — Halis (HLS)
# ============================================================================
CC      ?= gcc
CFLAGS  ?= -O2
PYTHON  ?= python3
HLC     = src/hlc.hls
BIN     = bin
PREFIX  ?= /usr/local

# ---- Stage 37 (v0.56.0-alpha): libcurl probe for the TLS builtin ----
# The TLS builtin (net_tls_get) is conditionally compiled: the C
# runtime emits a `#ifdef HL_HAVE_LIBCURL` guard around the libcurl
# code path. When libcurl is installed (curl-config exists), we add
# -DHL_HAVE_LIBCURL to CFLAGS and -lcurl to LIBS — net_tls_get works.
# When libcurl is NOT installed, the guard compiles a clean panic
# stub — net_tls_get panics with a clear "built without libcurl"
# message, but everything else (TCP/UDP/DNS) works.
#
# Auto-probe via curl-config (universally packaged with libcurl-dev).
# Fallback to pkg-config if curl-config is missing.
LIBCURL_CFLAGS := $(shell curl-config --cflags 2>/dev/null)
LIBCURL_LIBS   := $(shell curl-config --libs 2>/dev/null)
ifeq ($(strip $(LIBCURL_LIBS)),)
  LIBCURL_CFLAGS := $(shell pkg-config --cflags libcurl 2>/dev/null)
  LIBCURL_LIBS   := $(shell pkg-config --libs libcurl 2>/dev/null)
endif
ifneq ($(strip $(LIBCURL_LIBS)),)
  HL_CURL_DEFS := -DHL_HAVE_LIBCURL
else
  HL_CURL_DEFS :=
endif

.PHONY: all stage0 bootstrap bootstrap-nolibc xbootstrap-acceptance test examples clean run check bench install uninstall audit opt-stats emit-ir emit-llvm fmt lint lsp-check pkg-init pkg-add pkg-lock pkg-audit pkg-verify pkg-build pkg-publish pkg-log pkg-log-verify prove prove-full infer infer-smt model prove-acceptance invariant-acceptance hltest fuzz cov fuzz-acceptance wasm-opt webapp webapp-acceptance serve serve-acceptance wasm-pack wasm-pack-check wasm-pack-pack wasm-pack-acceptance aarch64-bench aarch64-acceptance aarch64-list-targets stack-acceptance inline-acceptance opt-stats-report kernel-attrs escape-acceptance layout-report tail-acceptance tail-report asm-acceptance asm-attrs bench-stdlib spec-check stage32-acceptance async-acceptance stream-acceptance io-acceptance fs-acceptance net-acceptance http-acceptance http2-acceptance json-stream-acceptance regex-acceptance fmt-acceptance hash-acceptance collections-acceptance sync-acceptance thread-acceptance time-acceptance math-acceptance process-acceptance env-acceptance archive-acceptance uuid-ulid-acceptance cli-acceptance tui-acceptance color-acceptance progress-acceptance log-acceptance http-server-acceptance websocket-acceptance cookie-acceptance session-acceptance csrf-acceptance template-acceptance sse-acceptance graphql-acceptance openapi-acceptance jsffi-acceptance dom-acceptance freestanding freestanding-check freestanding-acceptance nostd nostd-acceptance alloc-acceptance mem-acceptance panic-acceptance stackguard-acceptance asmreg-acceptance link-acceptance boot-acceptance idt-acceptance mmio-acceptance port-acceptance dma-acceptance atomics-acceptance irqsafe-acceptance triple triple-x86_64-acceptance triple-aarch64-acceptance triple-riscv64-acceptance debuginfo-acceptance test-linker

# Main goal: use the full bootstrap chain to build the native compiler
all: bootstrap

# Run Stage-0 directly (interpret an HLS program via the bootstrap seed)
stage0:
	@test "x$(F)" != "x" || (echo "Usage: make stage0 F=examples/hello.hls" && false)
	@$(PYTHON) boot/boot.py $(F)

# FULL BOOTSTRAP:
#   1. Stage-0 runs hlc.hls to compile itself -> hlc.c
#   2. gcc compiles to native hlc
#   3. native hlc re-compiles itself -> check determinism
bootstrap:
	@mkdir -p $(BIN)
	@echo "[1/4] Stage-0: hlc.hls self-compiles..."
	@$(PYTHON) boot/boot.py $(HLC) $(HLC) $(BIN)/hlc.c
	@echo "[2/4] gcc: compiling native hlc..."
	@$(CC) $(CFLAGS) -o $(BIN)/hlc $(BIN)/hlc.c -lm -pthread
	@echo "[3/4] native hlc re-compiles itself..."
	@$(BIN)/hlc $(HLC) $(BIN)/hlc2.c
	@echo "[4/4] Comparing two passes of code generation..."
	@diff $(BIN)/hlc.c $(BIN)/hlc2.c && echo "BOOTSTRAP OK: self-compilation is deterministic"
	@rm -f $(BIN)/hlc2.c
	@echo "Native compiler: $(BIN)/hlc"

# File target for the native compiler: acceptance targets declared with
# `bin/hlc` as a prerequisite stay buildable after `make clean` or on a
# fresh checkout, where the phony `bootstrap` goal alone is not enough.
$(BIN)/hlc:
	@$(MAKE) bootstrap

# Stage 92 (v0.111.0-alpha): the cross-bootstrap ladder. The hosted
# compiler emits its own source against the no-libc runtime, gcc links
# it -ffreestanding -nostdlib, and the resulting bin/hlc-fs (a binary
# with NO libc) must recompile the compiler byte-identically and emit
# code indistinguishable from the hosted compiler's. This is the
# roadmap's "Stage-0 -> freestanding hlc" arrow: every stage of the
# chain is the same source, however it was built.
bootstrap-nolibc:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@echo "[1/4] hosted hlc emits its own source with --no-libc..."
	@$(BIN)/hlc --no-libc $(HLC) $(BIN)/hlc_nolibc.c
	@echo "[2/4] gcc: linking the freestanding compiler..."
	@$(CC) $(CFLAGS) -ffreestanding -nostdlib -ffunction-sections -fno-stack-protector -Wl,--gc-sections -o $(BIN)/hlc-fs $(BIN)/hlc_nolibc.c
	@echo "[3/4] freestanding hlc recompiles the compiler..."
	@$(BIN)/hlc-fs --no-libc $(HLC) $(BIN)/hlc_nolibc2.c
	@diff $(BIN)/hlc_nolibc.c $(BIN)/hlc_nolibc2.c
	@echo "[4/4] freestanding hlc must agree with the hosted compiler..."
	@$(BIN)/hlc-fs $(HLC) $(BIN)/hlc_via_fs.c
	@$(BIN)/hlc $(HLC) $(BIN)/hlc_via_hosted.c
	@cmp $(BIN)/hlc_via_fs.c $(BIN)/hlc_via_hosted.c
	@rm -f $(BIN)/hlc_via_fs.c $(BIN)/hlc_via_hosted.c
	@echo "CROSS-BOOTSTRAP OK: Stage-0 -> hosted hlc -> freestanding hlc (no libc at the last stage)"

# Stage 92 acceptance: the flag + runtime shape, the full ladder, the
# binary's honesty (no libc symbols), the parity with the hosted
# compiler, a real no-libc program run, and the pinned gaps.
xbootstrap-acceptance:
	@echo "[Stage 92 acceptance] running tests/xbootstrap_acceptance.py..."
	@$(PYTHON) tests/xbootstrap_acceptance.py
	@echo "ACCEPTANCE OK: Stage 92 -- cross-bootstrappable build (Stage-0 -> freestanding hlc)"

# Compile and run an HLS program: make run F=examples/hello.hls
# Stage 37: link libcurl when available so net_tls_get works.
run:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@$(BIN)/hlc $(F) $(BIN)/hls_out.c && $(CC) $(CFLAGS) $(HL_CURL_DEFS) -o $(BIN)/hls_out $(BIN)/hls_out.c -lm -pthread $(LIBCURL_LIBS) && $(BIN)/hls_out

# Only check types + effects (no execution)
check:
	$(PYTHON) boot/boot.py --check $(F)

# Full test suite
test:
	bash tests/run_tests.sh

# Run the benchmarks folder (interpreter + native paths)
bench:
	@bash benchmarks/run_bench.sh

# Print the capability / effect tree of every function in a program
audit:
	@$(PYTHON) boot/boot.py --audit $(F)

# Stage 11 (v0.9.0-alpha): print the HLIR of a program
emit-ir:
	@$(PYTHON) boot/boot.py --emit ir $(F)

# Stage 12 (v0.10.0-alpha): print the LLVM IR of a program
emit-llvm:
	@$(PYTHON) boot/boot.py --emit llvm $(F)

# Stage 11: run the optimiser and print per-pass statistics
opt-stats:
	@$(PYTHON) boot/boot.py --opt-stats $(F)

# Stage 14 (v0.12.0-alpha): opinionated formatter
fmt:
	@$(PYTHON) tools/hlfmt.py $(F)

# Stage 14: linter
lint:
	@$(PYTHON) tools/hllint.py $(F)

# Stage 14: one-shot LSP diagnostics (for non-LSP editors)
lsp-check:
	@$(PYTHON) tools/hls-lsp.py --check $(F)


# ---------------------------------------------------------------------------
# Section targets live in mk/ (included in original section order,
# so this Makefile parses exactly like the former single-file version).
include mk/20-testing.mk mk/30-lto.mk mk/40-backends.mk mk/50-pkg-contracts.mk mk/60-advanced.mk mk/70-stdlib-core.mk mk/80-stdlib.mk mk/90-stdlib-late.mk mk/95-osdev.mk
