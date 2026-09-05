# ============================================================================
# Makefile — Halis (HLS)
# ============================================================================
CC      ?= gcc
CFLAGS  ?= -O2
PYTHON  ?= python3
HLC     = src/hlc.hls
BIN     = bin
PREFIX  ?= /usr/local

.PHONY: all stage0 bootstrap test examples clean run check bench install uninstall audit opt-stats emit-ir emit-llvm fmt lint lsp-check pkg-init pkg-add pkg-lock pkg-audit pkg-verify pkg-build pkg-publish pkg-log pkg-log-verify prove prove-full model prove-acceptance hltest fuzz cov fuzz-acceptance

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

# Compile and run an HLS program: make run F=examples/hello.hls
run:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@$(BIN)/hlc $(F) $(BIN)/hls_out.c && $(CC) $(CFLAGS) -o $(BIN)/hls_out $(BIN)/hls_out.c -lm -pthread && $(BIN)/hls_out

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


# ============================================================================
# Stage 18 (v0.34.0-alpha): testing ecosystem & fuzzing targets
# ============================================================================

# hltest: run every test_* function in the given .hls files (or dirs).
# Usage: make hltest [F=tests/ok] [GREP=map] [J=4] [JUNIT=out.xml]
hltest:
	@test -n "$(F)" || F=tests/ok; \
	  if [ -n "$(GREP)" ]; then G="--grep $(GREP)"; fi; \
	  if [ -n "$(JUNIT)" ]; then J="--junit $(JUNIT)"; fi; \
	  if [ -n "$(J)" ]; then P="-j $(J)"; fi; \
	  $(PYTHON) tools/hltest.py -r $$P $$G $$J $$F

# hls-fuzz: AST-level differential fuzzer. Default 60s smoke run;
# CI runs `make fuzz-acceptance` for the 1-hour acceptance run.
fuzz:
	@$(PYTHON) tools/hls-fuzz.py --time $(or $(TIME),60) --seed $(or $(SEED),)

# fuzz-acceptance: the Stage 18 acceptance criterion — fuzzer runs for
# 1 hour without finding any semantic discrepancy between the
# interpreter and the native compiler.
fuzz-acceptance:
	@echo "[Stage 18 acceptance] running hls-fuzz for 1 hour..."
	@$(PYTHON) tools/hls-fuzz.py --time 3600
	@echo "ACCEPTANCE OK: 1-hour fuzz run produced no divergences"

# hlcov: HLIR-level coverage report for a single file.
# Usage: make cov F=examples/hello.hls [LCOV=out.lcov]
cov:
	@test -n "$(F)" || (echo "Usage: make cov F=examples/hello.hls [LCOV=out.lcov]" && false)
	@if [ -n "$(LCOV)" ]; then L="--lcov $(LCOV)"; fi; \
	  $(PYTHON) tools/hlcov.py $$L $(F)

# ============================================================================
# Stage 19 (v0.35.0-alpha): profile-guided optimisation targets
# ============================================================================

# pgo: build a PGO-TRAINED native compiler (the canonical release binary):
#   1. the plain native hlc compiles hlc.hls with --pgo-generate
#      (counters at every fn entry / branch / loop back-edge),
#   2. the instrumented compiler runs the training workload (compiling
#      hlc.hls itself + a spread of example programs, merged into one
#      .hlcprof via HLS_PGO_MERGE=1),
#   3. hlc recompiles itself with --pgo-use -> __builtin_expect branch
#      hints, hot/cold attributes, static-inline hints and hoisted
#      string literals in hot functions,
#   4. the trained binary is verified byte-identical on output.
pgo: bootstrap
	@mkdir -p $(BIN)
	@echo "[1/4] hlc: generating PGO-instrumented compiler..."
	@$(BIN)/hlc --pgo-generate $(HLC) $(BIN)/hlc_gen.c
	@$(CC) $(CFLAGS) -o $(BIN)/hlc_gen $(BIN)/hlc_gen.c -lm -pthread
	@echo "[2/4] training workload (self-compile + examples, merged profile)..."
	@rm -f $(BIN)/hlc.hlcprof
	@for i in 1 2 3; do \
	  for f in $(HLC) examples/fibonacci.hls examples/optimize_demo.hls \
		   examples/hello.hls examples/primes.hls examples/enum_demo.hls; do \
	    HLS_PGO_FILE=$(BIN)/hlc.hlcprof HLS_PGO_MERGE=1 \
	      $(BIN)/hlc_gen $$f $(BIN)/hlc_train_tmp.c || exit 1; \
	  done; \
	done
	@rm -f $(BIN)/hlc_train_tmp.c
	@echo "     profile: $$(wc -l < $(BIN)/hlc.hlcprof) sites"
	@echo "[3/4] hlc: recompiling itself with --pgo-use (trained)..."
	@$(BIN)/hlc --pgo-use $(BIN)/hlc.hlcprof $(HLC) $(BIN)/hlc_pgo.c
	@$(CC) $(CFLAGS) -o $(BIN)/hlc_pgo $(BIN)/hlc_pgo.c -lm -pthread
	@echo "[4/4] verifying byte-identical output on sample programs..."
	@for f in examples/fibonacci.hls examples/primes.hls examples/optimize_demo.hls; do \
	  $(BIN)/hlc $$f $(BIN)/v_plain.c && $(BIN)/hlc_pgo $$f $(BIN)/v_trained.c; \
	  diff -q $(BIN)/v_plain.c $(BIN)/v_trained.c >/dev/null \
	    || (echo "PGO FAILED: $$f output differs" && exit 1); \
	done
	@rm -f $(BIN)/v_plain.c $(BIN)/v_trained.c
	@echo "PGO OK: trained compiler at $(BIN)/hlc_pgo (profile: $(BIN)/hlc.hlcprof)"

# pgo-acceptance: the Stage 19 acceptance criterion — the PGO-trained hlc
# compiles hlc.hls in <= 80% of the non-PGO build's wall time (median of
# 9 runs each), with byte-identical output.
pgo-acceptance: pgo
	@python3 scripts/pgo_ratio.py --plain $(BIN)/hlc --trained $(BIN)/hlc_pgo \
	  --input $(HLC) --runs 9 --max-ratio 0.80

# pgo-report: same measurement, informational only (no gate).
pgo-report: pgo
	@python3 scripts/pgo_ratio.py --plain $(BIN)/hlc --trained $(BIN)/hlc_pgo \
	  --input $(HLC) --runs 9

# ============================================================================
# Stage 20 (v0.36.0-alpha): link-time optimisation across crates
# ============================================================================

# lto: compile a program with the whole-program LTO pipeline
# (cross-crate inlining + DCE) and run it. Usage: make lto F=prog.hls
lto:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@test -n "$(F)" || (echo "Usage: make lto F=examples/foo.hls" && false)
	@mkdir -p $(BIN)
	@$(BIN)/hlc --lto $(F) $(BIN)/hls_lto.c
	@$(CC) $(CFLAGS) -o $(BIN)/hls_lto $(BIN)/hls_lto.c -lm -pthread
	@$(BIN)/hls_lto

# emit-lto-ir: whole-program LTO'd LLVM IR (single .ll with every
# transitive dependency; .bc too when llvm-as is available).
# Usage: make emit-lto-ir F=examples/foo.hls [OUT=/tmp/foo]
emit-lto-ir:
	@test -n "$(F)" || (echo "Usage: make emit-lto-ir F=examples/foo.hls [OUT=/tmp/foo]" && false)
	@if [ -z "$(OUT)" ]; then OUT=$(F:.hls=.lto.ll); fi; \
	  $(PYTHON) boot/boot.py --emit lto $(F) > $$OUT; \
	  echo "wrote $$OUT"; \
	  if command -v llvm-as >/dev/null 2>&1; then \
	    llvm-as $$OUT -o $$(dirname $$OUT)/$$(basename $$OUT .ll).bc; \
	    echo "wrote $$(dirname $$OUT)/$$(basename $$OUT .ll).bc (bitcode)"; \
	  else \
	    echo "llvm-as not available: skipped bitcode (.bc) emission"; \
	  fi

# ============================================================================
# Stage 21 (v0.37.0-alpha): SIMD vectorisation targets
# ============================================================================

# simd-bench: run the acceptance benchmark (1M-element 8-tap FIR
# correlation, scalar vs std.simd vector path). With FEATURE set the
# vector path uses native intrinsics; without it, the portable
# (reference) implementation runs (very slow — it is the semantic
# reference, not the fast path).
# Usage: make simd-bench [FEATURE=avx2]
simd-bench:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@if [ -n "$(FEATURE)" ]; then T="--target-feature $(FEATURE)"; fi; \
	  $(BIN)/hlc $$T benchmarks/simd_bench.hls $(BIN)/simd_bench.c; \
	  $(CC) $(CFLAGS) -o $(BIN)/simd_bench $(BIN)/simd_bench.c -lm -pthread; \
	  $(BIN)/simd_bench

# simd-acceptance: the Stage 21 acceptance gate — the vector kernel is
# >= 2x faster than the scalar kernel on a 1M-element list, with
# identical output (checksum equality). Requires an AVX2-capable host
# (the intrinsics are x86-64; on other hosts the run reports SKIP).
simd-acceptance:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@$(BIN)/hlc --target-feature avx2 benchmarks/simd_bench.hls $(BIN)/simd_acc.c
	@$(CC) $(CFLAGS) -o $(BIN)/simd_acc $(BIN)/simd_acc.c -lm -pthread
	@$(BIN)/simd_acc > $(BIN)/simd_acc.out 2>&1 || exit 1
	@grep -q "checksums MATCH" $(BIN)/simd_acc.out \
	  || (echo "FAIL: checksum mismatch (vector != scalar)"; exit 1)
	@if ! grep -q "simd_cpu_supports(avx2) = true" $(BIN)/simd_acc.out; then \
	  echo "SKIP: host CPU has no AVX2 (intrinsic path inactive)"; exit 0; fi
	@python3 scripts/simd_ratio.py --out $(BIN)/simd_acc.out --min 2.0

.PHONY: pgo pgo-acceptance pgo-report lto emit-lto-ir simd-bench simd-acceptance

# ============================================================================
# Stage 13 (v0.23.0-alpha): hls-pkg package manager targets
# ============================================================================
PKG = $(PYTHON) tools/hls-pkg.py

# Create a new package skeleton: make pkg-init NAME=mylib
pkg-init:
	@test "x$(NAME)" != "x" || (echo "Usage: make pkg-init NAME=mylib" && false)
	@$(PKG) init $(NAME)

# Add a dependency: make pkg-add NAME=std.str GIT=... PATH=std/str.hls TAG=v0.23.0-alpha
pkg-add:
	@test "x$(NAME)" != "x" || (echo "Usage: make pkg-add NAME=.. GIT=.. PATH=.. [TAG=..] [BRANCH=..]" && false)
	@if [ -n "$(TAG)" ] && [ -n "$(BRANCH)" ]; then \
	  echo "error: --tag and --branch are mutually exclusive"; exit 1; \
	fi
	@if [ -n "$(TAG)" ]; then \
	  $(PKG) add $(NAME) $(GIT) $(PATH) --tag $(TAG); \
	elif [ -n "$(BRANCH)" ]; then \
	  $(PKG) add $(NAME) $(GIT) $(PATH) --branch $(BRANCH); \
	else \
	  $(PKG) add $(NAME) $(GIT) $(PATH); \
	fi

# Resolve dependencies + write lockfile + append to transparency log
pkg-lock:
	@$(PKG) lock

# Print the total effect report of the dependency tree
pkg-audit:
	@$(PKG) audit

# Verify lockfile hashes + commits + transparency-log entries
pkg-verify:
	@$(PKG) verify

# Compile the package's entry point with resolved dependencies
pkg-build:
	@$(PKG) build --entry $(or $(ENTRY),main.hls)

# Stage 13 release: append the current package to the transparency log
pkg-publish:
	@$(PKG) publish

# Print the transparency log
pkg-log:
	@$(PKG) log

# Verify the transparency log's chain hashes
pkg-log-verify:
	@$(PKG) log --verify

# ============================================================================
# Stage 17 (v0.28.0-alpha): contracts / proof tools
# ============================================================================

# Proof report: which panic checks the interval prover proved dead
prove:
	@python3 tools/hlprove.py $(F)

# Proof report + SMT-LIB2 files (z3-ready) + loop invariant suggestions
prove-full:
	@python3 tools/hlprove.py $(F) --smt --suggest-invariants

# Exhaustive finite-state model checking of a transition fn
model:
	@python3 tools/hlmodel.py $(F) --fn $(FN) [--invariant $(INV)] [--init $(INIT)]

# The Stage 17 acceptance example (HMAC envelope, fully proven hot path)
prove-acceptance:
	@python3 tools/hlprove.py examples/hmac_proven.hls
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@$(BIN)/hlc --fast examples/hmac_proven.hls $(BIN)/hmac_fast.c
	@$(CC) $(CFLAGS) -o $(BIN)/hmac_fast $(BIN)/hmac_fast.c -lm -pthread
	@echo "--- running the -O fast (proof-elided) binary:"
	@$(BIN)/hmac_fast

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
		   examples/tooling_demo.hls examples/pkg_demo.hls \
		   examples/libcurl_demo.hls \
		   examples/conc_demo.hls examples/actor_demo.hls \
		   examples/bounded_chan_demo.hls examples/conc_pipeline.hls \
		   examples/par_scan.hls examples/hmac_proven.hls \
		   examples/proof_demo.hls \
		   examples/conn_machine.hls examples/bits_demo.hls \
		   examples/set_demo.hls; do \
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
	rm -rf $(BIN)

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
