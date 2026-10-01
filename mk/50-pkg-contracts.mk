# ============================================================================
# Stage 13 (v0.23.0-alpha): hls-pkg package manager targets
# ============================================================================
PKG = $(PYTHON) tools/hls-pkg.py

# Create a new package skeleton: make pkg-init NAME=mylib
pkg-init:
	@test "x$(NAME)" != "x" || (echo "Usage: make pkg-init NAME=mylib" && false)
	@$(PKG) init $(NAME)

# Add a dependency: make pkg-add NAME=std.str GIT=... MODPATH=std/str.hls TAG=v0.23.0-alpha
# MODPATH (not PATH) — PATH collides with the environment and would pass the
# whole shell PATH string to hls-pkg when the argument is forgotten.
pkg-add:
	@test "x$(NAME)" != "x" || (echo "Usage: make pkg-add NAME=.. GIT=.. MODPATH=.. [TAG=..] [BRANCH=..]" && false)
	@test "x$(MODPATH)" != "x" || (echo "Usage: make pkg-add NAME=.. GIT=.. MODPATH=.. [TAG=..] [BRANCH=..]" && false)
	@if [ -n "$(TAG)" ] && [ -n "$(BRANCH)" ]; then \
	  echo "error: --tag and --branch are mutually exclusive"; exit 1; \
	fi
	@if [ -n "$(TAG)" ]; then \
	  $(PKG) add $(NAME) $(GIT) $(MODPATH) --tag $(TAG); \
	elif [ -n "$(BRANCH)" ]; then \
	  $(PKG) add $(NAME) $(GIT) $(MODPATH) --branch $(BRANCH); \
	else \
	  $(PKG) add $(NAME) $(GIT) $(MODPATH); \
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

# Stage 97 (v0.116.0-alpha): SMT-based loop-invariant inference.
# Summarises every loop (entry facts + transition relation), generates
# template candidates and lets z3 decide initiation + preservation.
infer:
	@python3 tools/hlprove.py $(F) --infer-invariants

# The same, plus one .smt2 obligations file per verified loop (named
# <fn>__L<line>.smt2, next to the source like the contract bridge).
infer-smt:
	@python3 tools/hlprove.py $(F) --infer-invariants --smt

# Stage 99 (v0.118.0-alpha): the CVC5 SMT backend. The same bridge and
# the same obligations, decided by cvc5 instead of z3 (the binary
# first, then the cvc5 python module). --cvc5 implies --smt; --z3 and
# --cvc5 are mutually exclusive.
cvc5:
	@python3 tools/hlprove.py $(F) --cvc5

# Stage 100 (v0.119.0-alpha): the separation-logic fragment — heap
# shapes. Footprints over alias classes, separation verdicts
# (own(a) * own(b), proven from freshness evidence), and cursor-loop
# shape triples (lseg * cell * lseg) decided by the SMT backend.
shapes:
	@python3 tools/hlprove.py $(F) --shapes

# The same, plus one .shape.smt2 obligations file per verified loop
# (named <fn>__L<line>.shape.smt2, next to the source like every
# bridge dump).
shapes-smt:
	@python3 tools/hlprove.py $(F) --shapes --smt

# Stage 101 (v0.120.0-alpha): the cryptographic side-channel analysis.
# The #[secrets(...)] parameters are the taint roots; the report lists
# every branch / index / loop-bound / division sink a secret reaches,
# the incoming propagation chain, and the len() policy notes.
sidechannel:
	@python3 tools/hlprove.py $(F) --sidechannel

# Exhaustive finite-state model checking of a transition fn
model:
	@if [ -z "$(F)" ] || [ -z "$(FN)" ]; then echo "Usage: make model F=file.hls FN=transition_fn [INV=invariant_fn] [INIT=Enum.Variant]" && false; fi; \
	if [ -n "$(INV)" ]; then I="--invariant $(INV)"; else I=""; fi; \
	if [ -n "$(INIT)" ]; then S="--init $(INIT)"; else S=""; fi; \
	python3 tools/hlmodel.py $(F) --fn $(FN) $$I $$S

# The Stage 17 acceptance example (HMAC envelope, fully proven hot path)
prove-acceptance:
	@python3 tools/hlprove.py examples/hmac_proven.hls
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@$(BIN)/hlc --fast examples/hmac_proven.hls $(BIN)/hmac_fast.c
	@$(CC) $(CFLAGS) -o $(BIN)/hmac_fast $(BIN)/hmac_fast.c -lm -pthread
	@echo "--- running the -O fast (proof-elided) binary:"
	@$(BIN)/hmac_fast

# Stage 97 acceptance gate: the SMT-based loop-invariant inference.
# Requires a decider — a z3 binary or the z3-solver python module (the
# gate says which to install when neither exists).
invariant-acceptance: $(BIN)/hlc
	@echo "[Stage 97 acceptance] running tests/invariant_acceptance.py..."
	@$(PYTHON) tests/invariant_acceptance.py

# Stage 99 acceptance gate: the CVC5 SMT backend. Requires the cvc5
# backend (a binary or pip install cvc5) AND the z3 backend — the whole
# point of the stage is that the two agree on every verdict.
cvc5-acceptance:
	@echo "[Stage 99 acceptance] running tests/cvc5_acceptance.py..."
	@$(PYTHON) tests/cvc5_acceptance.py

# Stage 100 acceptance gate: the separation-logic fragment. Requires a
# decider — a z3 binary or the z3-solver python module (the gate says
# which to install when neither exists).
shapes-acceptance: $(BIN)/hlc
	@echo "[Stage 100 acceptance] running tests/shapes_acceptance.py..."
	@$(PYTHON) tests/shapes_acceptance.py

# Stage 101 acceptance gate: the side-channel analysis. Analysis-only
# (no solver needed): the engine battery, the soundness flips, the
# interprocedural chain, the CLI report, the demo parity, the audit
# parity between the front-ends, the fail tests and the tools.
sidechannel-acceptance: $(BIN)/hlc
	@echo "[Stage 101 acceptance] running tests/sidechannel_acceptance.py..."
	@$(PYTHON) tests/sidechannel_acceptance.py

refine-acceptance: $(BIN)/hlc
	@echo "[Stage 98 acceptance] running tests/refine_acceptance.py..."
	@$(PYTHON) tests/refine_acceptance.py

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
		   examples/proof_demo.hls examples/refine_demo.hls \
		   examples/heap_demo.hls examples/conn_machine.hls \
		   examples/bits_demo.hls \
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

