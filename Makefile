# ============================================================================
# Makefile — Hieu Louis (HLS)
# ============================================================================
CC      ?= gcc
CFLAGS  ?= -O2
PYTHON  ?= python3
HLC     = src/hlc.hls
BIN     = bin

.PHONY: all stage0 bootstrap test examples clean run check

# Main goal: use the full bootstrap chain to build the native compiler
all: bootstrap

# Run Stage-0 directly (interpret HLS)
stage0:
	$(PYTHON) boot/boot.py --help 2>/dev/null || true
	@echo "Example: $(PYTHON) boot/boot.py examples/hello.hls"

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
	@$(BIN)/hlc $(F) /tmp/hls_out.c && $(CC) $(CFLAGS) -o /tmp/hls_out /tmp/hls_out.c -lm && /tmp/hls_out

# Only check types + effects (no execution)
check:
	$(PYTHON) boot/boot.py --check $(F)

# Full test suite
test:
	bash tests/run_tests.sh

examples:
	@for f in examples/hello.hls examples/fibonacci.hls examples/primes.hls; do \
		echo "--- $$f"; $(PYTHON) boot/boot.py $$f; \
	done

clean:
	rm -rf $(BIN) /tmp/hls_out /tmp/hls_out.c
