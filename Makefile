# ============================================================================
# Makefile — Hieu Louis (HLS)
# ============================================================================
CC      ?= gcc
CFLAGS  ?= -O2
PYTHON  ?= python3
HLC     = src/hlc.hls
BIN     = bin
PREFIX  ?= /usr/local

.PHONY: all stage0 bootstrap test examples clean run check bench install uninstall audit opt-stats emit-ir emit-llvm fmt lint lsp-check

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
	@$(CC) $(CFLAGS) -o $(BIN)/hlc $(BIN)/hlc.c -lm
	@echo "[3/4] native hlc re-compiles itself..."
	@$(BIN)/hlc $(HLC) $(BIN)/hlc2.c
	@echo "[4/4] Comparing two passes of code generation..."
	@diff $(BIN)/hlc.c $(BIN)/hlc2.c && echo "BOOTSTRAP OK: self-compilation is deterministic"
	@rm -f $(BIN)/hlc2.c
	@echo "Native compiler: $(BIN)/hlc"

# Compile and run an HLS program: make run F=examples/hello.hls
run:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@$(BIN)/hlc $(F) $(BIN)/hls_out.c && $(CC) $(CFLAGS) -o $(BIN)/hls_out $(BIN)/hls_out.c -lm && $(BIN)/hls_out

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

# Run the example programs to verify they still work after a change
examples:
	@# Most examples exit 0; secure_demo.hls deliberately panics on
	@# integer overflow to demonstrate safe-panic semantics (exit 101).
	@# NOTE: taint_beta_demo.hls and wordcount.hls need a data-file
	@# argument and are run separately below.
	@for f in examples/hello.hls examples/fibonacci.hls examples/primes.hls \
		  examples/enum_demo.hls examples/option_demo.hls examples/result_demo.hls \
		  examples/ownership_demo.hls examples/effects_demo.hls \
		  examples/hex_demo.hls examples/base64_demo.hls examples/crypto_demo.hls \
		  examples/csv_demo.hls examples/list_demo.hls examples/time_demo.hls \
		  examples/uuid_demo.hls examples/web_demo.hls examples/stdlib_demo.hls \
		  examples/taint_demo.hls examples/ffi_demo.hls \
		  examples/optimize_demo.hls examples/llvm_demo.hls \
		  examples/tooling_demo.hls examples/pkg_demo.hls; do \
		echo "--- $$f"; $(PYTHON) boot/boot.py $$f || exit 1; \
	done
	@echo "--- examples/secure_demo.hls (deliberately panics on overflow)"
	@rc=0; $(PYTHON) boot/boot.py examples/secure_demo.hls || rc=$$?; \
	if [ "$$rc" != "0" ] && [ "$$rc" != "101" ]; then \
	    echo "secure_demo.hls failed with unexpected exit code $$rc"; exit 1; \
	fi
	@# wordcount needs a data-file argument
	@echo "--- examples/wordcount.hls"; $(PYTHON) boot/boot.py examples/wordcount.hls examples/data.txt
	@# taint_beta_demo.hls needs a data-file argument
	@echo "--- examples/taint_beta_demo.hls"; $(PYTHON) boot/boot.py examples/taint_beta_demo.hls examples/data.txt

clean:
	rm -rf $(BIN) bin/hls_out bin/hls_out.c

# Install the native compiler and stdlib to PREFIX (default /usr/local)
install: bootstrap
	@mkdir -p $(PREFIX)/bin $(PREFIX)/share/hls/std $(PREFIX)/share/hls/examples
	@install -m 755 $(BIN)/hlc $(PREFIX)/bin/hlc
	@cp -r std/*.hls $(PREFIX)/share/hls/std/
	@cp -r examples/*.hls $(PREFIX)/share/hls/examples/ 2>/dev/null || true
	@echo "Installed: $(PREFIX)/bin/hlc"
	@echo "Stdlib:   $(PREFIX)/share/hls/std/"

# Remove the installed files
uninstall:
	@rm -f $(PREFIX)/bin/hlc
	@rm -rf $(PREFIX)/share/hls
	@echo "Removed: $(PREFIX)/bin/hlc and $(PREFIX)/share/hls"
