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

# Stage 102 (v0.121.0-alpha): the constant-time verifier. Every #[ct]
# claim is proven in its own taint universe (only the claim's
# #[secrets(...)] parameters are roots); VERIFIED per claim, VIOLATED
# with the sink lines and the incoming chains - and a violated claim
# exits 1, so the run gates a CI pipeline.
ct:
	@python3 tools/hlprove.py $(F) --consttime

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

# Stage 102 acceptance gate: the constant-time verifier. No solver
# needed: the verdict battery, the taint universes (an unrelated leak
# cannot violate a claim), the chains, the CLI exits (0 verified,
# 1 violated), the demo parity, the audit parity, the fail tests and
# the tools.
ct-acceptance: $(BIN)/hlc
	@echo "[Stage 102 acceptance] running tests/consttime_acceptance.py..."
	@$(PYTHON) tests/consttime_acceptance.py

# Stage 103 (v0.122.0-alpha): hls-audit, the transitive supply-chain
# effect report. Source mode walks the import graph (toolchain /
# dependency / workspace classification, intrinsic vs reachable
# effects, attribution chains, the extern surface); package mode
# walks the manifest tree hls-pkg audit never opens, checks the
# lockfile for drift, and gates the chain against the root's
# [effects].allowed. The root is your code - reported, never gated.
supply:
	@python3 tools/hls-audit.py $(F)

supply-pkg:
	@if [ -z "$(D)" ]; then echo "Usage: make supply-pkg D=examples/pkg_audit_demo [ALLOW=IO,Fs]" && false; fi; \
	if [ -n "$(ALLOW)" ]; then A="--allow $(ALLOW)"; else A=""; fi; \
	python3 tools/hls-audit.py --pkg $(D) $$A

# Stage 103 acceptance gate: the eight-section battery (engine,
# policy, fail-closed, the package tree, drift, the mode parity, the
# demos, the tools). Tool-only - no solver, no compiler needed; the
# native half of the demo parity runs when bin/hlc exists.
supply-acceptance:
	@echo "[Stage 103 acceptance] running tests/audit_acceptance.py..."
	@$(PYTHON) tests/audit_acceptance.py

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


# Stage 104 (v0.123.0-alpha): hls-sbom, the CycloneDX + SPDX bill of
# materials. Source mode lists the import tree (modules as hashed
# components, the audit's effect split as properties); package mode
# lists the manifest tree with the lockfile's own hashes. --release
# is the shipping gate: lockfile required, drift refused, versioned
# documents written, the SBOM chained into the transparency log.
sbom:
	@python3 tools/hls-sbom.py $(F)

sbom-pkg:
	@if [ -z "$(D)" ]; then echo "Usage: make sbom-pkg D=<pkg dir> [FORMAT=cdx|spdx|both]" && false; fi; \
	if [ -n "$(FORMAT)" ]; then FMT="--format $(FORMAT)"; else FMT=""; fi; \
	python3 tools/hls-sbom.py --pkg $(D) $$FMT

sbom-release:
	@if [ -z "$(D)" ]; then echo "Usage: make sbom-release D=<pkg dir>" && false; fi; \
	python3 tools/hls-sbom.py --pkg $(D) --release

# Stage 104 acceptance gate: the eight-section battery (the CycloneDX
# document, the SPDX twin, determinism, the package tree, the release
# gate with the transparency-log chaining, the audit parity, the
# demos, the tools). Tool-only - no solver, no compiler needed; the
# native half of the demo parity runs when bin/hlc exists.
sbom-acceptance:
	@echo "[Stage 104 acceptance] running tests/sbom_acceptance.py..."
	@$(PYTHON) tests/sbom_acceptance.py

# ============================================================================
# Stage 105 (v0.124.0-alpha): hls-repro — reproducible-build
# verification across distros
# ============================================================================

# Build the same tree N times in N isolated roots under N distro
# profiles (locale, TZ, umask, hash seed, env breadth, root-path
# shape) and require byte-identical C + binary. Writes the
# buildinfo next to the entry.
repro:
	@if [ -z "$(F)" ]; then echo "Usage: make repro F=<entry.hls> [BUILDS=2] [ENGINE=auto|boot|native]" && false; fi; \
	if [ -n "$(BUILDS)" ]; then B="--builds $(BUILDS)"; else B=""; fi; \
	if [ -n "$(ENGINE)" ]; then E="--engine $(ENGINE)"; else E=""; fi; \
	python3 tools/hls-repro.py $(F) $$B $$E

# The same, package mode: the manifest tree, the lockfile's drift
# check, deps staged the hls-pkg way.
repro-pkg:
	@if [ -z "$(D)" ]; then echo "Usage: make repro-pkg D=<pkg dir> [BUILDS=2]" && false; fi; \
	if [ -n "$(BUILDS)" ]; then B="--builds $(BUILDS)"; else B=""; fi; \
	python3 tools/hls-repro.py --pkg $(D) $$B

# The shipping gate: lockfile required, versioned buildinfo
# hls-repro-<name>-<version>.buildinfo.json written, ONE "repro"
# record chained into the transparency log.
repro-release:
	@if [ -z "$(D)" ]; then echo "Usage: make repro-release D=<pkg dir>" && false; fi; \
	python3 tools/hls-repro.py --pkg $(D) --release

# The cross-environment answer: rebuild the CURRENT tree under a
# shifted profile slice and require the buildinfo's bytes back.
repro-verify:
	@if [ -z "$(BINFO)" ]; then echo "Usage: make repro-verify BINFO=<file.buildinfo.json> [IN=<tree root>]" && false; fi; \
	if [ -n "$(IN)" ]; then I="--in $(IN)"; else I=""; fi; \
	python3 tools/hls-repro.py --verify $(BINFO) $$I

# Stage 105 acceptance gate: the recipe (two profiles, identical
# bytes), the STT_FILE diagnoser, the epoch policy + the SBOM
# bridge, the matrix, the input-drift refusal, verify (fresh +
# tampered + drifted), the release gate with the transparency
# chaining, the demos, the tools. Tool-only — no solver needed; the
# engine half runs native when bin/hlc exists, boot otherwise.
repro-acceptance:
	@echo "[Stage 105 acceptance] running tests/repro_acceptance.py..."
	@$(PYTHON) tests/repro_acceptance.py

# ============================================================================
# Stage 106 (v0.125.0-alpha): hls-sign — signed packages (minisign,
# ed25519)
# ============================================================================

# Generate a keypair: make sign-keygen [COMMENT="..."] — the passphrase
# comes from HLS_SIGN_PASSWORD (or --password-env via the tool).
sign-keygen:
	@if [ -n "$(COMMENT)" ]; then C="-c $(COMMENT)"; else C=""; fi; \
	python3 tools/hls-sign.py keygen $$C

# Sign any file: make sign F=file.hls — writes file.hls.minisig
sign:
	@test "x$(F)" != "x" || (echo "Usage: make sign F=<file>" && false)
	@python3 tools/hls-sign.py sign $(F)

# Verify: make sign-verify F=file [SIG=file.minisig] [PUB=key.pub]
sign-verify:
	@test "x$(F)" != "x" || (echo "Usage: make sign-verify F=<file> [SIG=<sig>] [PUB=<pub>]" && false); \
	if [ -n "$(SIG)" ]; then S="$(SIG)"; else S=""; fi; \
	if [ -n "$(PUB)" ]; then A="-p $(PUB)"; else A=""; fi; \
	python3 tools/hls-sign.py verify $(F) $$S $$A

# The shipping gate: audit + lockfile required, the versioned statement
# hls-sign-<name>-<version>.release.json + its minisign signature
# written, ONE "sign" record chained into the transparency log.
sign-release:
	@test "x$(D)" != "x" || (echo "Usage: make sign-release D=<pkg dir> [OUT=<dir>]" && false); \
	if [ -n "$(OUT)" ]; then O="--out $(OUT)"; else O=""; fi; \
	python3 tools/hls-sign.py release $(D) $$O

# The counterparty's side: signature, lockfile bytes, audit drift,
# content digest — against the CURRENT tree.
sign-verify-release:
	@test "x$(D)" != "x" || (echo "Usage: make sign-verify-release D=<pkg dir> [PUB=<pub>]" && false); \
	if [ -n "$(PUB)" ]; then A="-p $(PUB)"; else A=""; fi; \
	python3 tools/hls-sign.py verify-release $(D) $$A

# The RFC 8032 / RFC 8439 vectors, through the tool's front door.
sign-selftest:
	@python3 tools/hls-sign.py selftest

# Stage 106 acceptance gate: the nine-section battery (the primitives
# against the RFC vectors, the keys, the minisign signature contract,
# the layer parity, the statement, the release gate with the
# transparency chaining, the ledger, the demos, the tools). Tool-only
# — no solver, no compiler needed; the native half of the demo parity
# runs when bin/hlc exists.
sign-acceptance:
	@echo "[Stage 106 acceptance] running tests/signed_acceptance.py..."
	@$(PYTHON) tests/signed_acceptance.py

# ============================================================================
# Stage 107 (v0.126.0-alpha): hls-tlog — transparency-log gossip
# (multi-source verify)
# ============================================================================

# Replay any log's hash chain: make tlog-verify [LOG=...] (default the
# repo ledger).
tlog-verify:
	@if [ -n "$(LOG)" ]; then L="$(LOG)"; else L=""; fi; \
	python3 tools/hls-tlog.py verify $$L

# The verified view: make tlog [LOG=...]
tlog:
	@if [ -n "$(LOG)" ]; then L="$(LOG)"; else L=""; fi; \
	python3 tools/hls-tlog.py summary $$L

# Compare views across sources (files, dirs, http URLs):
# make tlog-gossip SOURCES="log1 log2 https://mirror/log"
tlog-gossip:
	@test "x$(SOURCES)" != "x" || (echo "Usage: make tlog-gossip SOURCES=<src> <src> ..." && false)
	@python3 tools/hls-tlog.py gossip $(SOURCES)

# Sign a log's view with the Stage 106 keys:
# make tlog-witness LOG=... KEY=... [OUT=dir] — passphrase from
# HLS_SIGN_PASSWORD.
tlog-witness:
	@test "x$(KEY)" != "x" || (echo "Usage: make tlog-witness [LOG=<log>] KEY=<secret> [OUT=<dir>]" && false)
	@if [ -n "$(LOG)" ]; then L="$(LOG)"; else L=""; fi; \
	if [ -n "$(OUT)" ]; then O="--out $(OUT)"; else O=""; fi; \
	python3 tools/hls-tlog.py witness $$L --sign $(KEY) $$O

# Verify a signed view; --against checks it against the current log:
# make tlog-witness-verify WITNESS=... [PUB=...] [AGAINST=log]
tlog-witness-verify:
	@test "x$(WITNESS)" != "x" || (echo "Usage: make tlog-witness-verify WITNESS=<file> [PUB=<pub>] [AGAINST=<log>]" && false)
	@if [ -n "$(PUB)" ]; then A="-p $(PUB)"; else A=""; fi; \
	if [ -n "$(AGAINST)" ]; then G="--against $(AGAINST)"; else G=""; fi; \
	python3 tools/hls-tlog.py witness-verify $(WITNESS) $$A $$G

# An inclusion proof for one record:
# make tlog-prove NAME=... [VERSION=...] [LOG=...] [OUT=dir]
tlog-prove:
	@test "x$(NAME)" != "x" || (echo "Usage: make tlog-prove NAME=<name> [VERSION=<v>] [LOG=<log>] [OUT=<dir>]" && false)
	@if [ -n "$(VERSION)" ]; then V="$(VERSION)"; else V=""; fi; \
	if [ -n "$(LOG)" ]; then L="--log $(LOG)"; else L=""; fi; \
	if [ -n "$(OUT)" ]; then O="--out $(OUT)"; else O=""; fi; \
	python3 tools/hls-tlog.py prove $(NAME) $$V $$L $$O

# Verify a proof (offline, or against the live history):
# make tlog-prove-verify PROOF=... [LOG=...]
tlog-prove-verify:
	@test "x$(PROOF)" != "x" || (echo "Usage: make tlog-prove-verify PROOF=<file> [LOG=<log>]" && false)
	@if [ -n "$(LOG)" ]; then L="--log $(LOG)"; else L=""; fi; \
	python3 tools/hls-tlog.py prove-verify $(PROOF) $$L

# The stage's pinned numbers, through the tool's front door.
tlog-selftest:
	python3 tools/hls-tlog.py selftest

# Stage 107 acceptance gate: the nine-section battery (the chain
# arithmetic against hpkg_log's own writer, the views, the sources —
# file, directory, a live local HTTP server, the gossip verdicts, the
# witnesses (rollback, rewritten, held), the proofs (offline, live,
# grown, rewritten), the repo ledger end to end, the demos, the
# tools). Tool-only — no solver needed; the native half of the demo
# parity runs when bin/hlc exists.
tlog-acceptance:
	@echo "[Stage 107 acceptance] running tests/tlog_acceptance.py..."
	@$(PYTHON) tests/tlog_acceptance.py

# ============================================================================
# Stage 108 (v0.127.0-alpha): hls-reverify — memory-safety
# re-verification under -O fast (proof replay)
# ============================================================================

# The replay on any entry file: make reverify F=examples/hmac_proven.hls
reverify:
	@test "x$(F)" != "x" || (echo "Usage: make reverify F=<file.hls> [-- extra flags]" && false)
	@python3 tools/hls-reverify.py $(F)

# The machine report: make reverify-json F=...
reverify-json:
	@test "x$(F)" != "x" || (echo "Usage: make reverify-json F=<file.hls>" && false)
	@python3 tools/hls-reverify.py $(F) --json

# The replay under the LTO inline threshold: make reverify-lto F=...
reverify-lto:
	@test "x$(F)" != "x" || (echo "Usage: make reverify-lto F=<file.hls>" && false)
	@python3 tools/hls-reverify.py $(F) --lto

# A written report: make reverify-report F=... OUT=dir
reverify-report:
	@test "x$(F)" != "x" || (echo "Usage: make reverify-report F=<file.hls> OUT=<dir>" && false)
	@if [ -n "$(OUT)" ]; then O="--out $(OUT)"; else O="--out build"; fi; \
	python3 tools/hls-reverify.py $(F) $$O

# The stage's pinned numbers, through the tool's front door.
reverify-selftest:
	@python3 tools/hls-reverify.py selftest

# Stage 108 acceptance gate: the nine-section battery (the one
# definition against boot.proof's own arithmetic, the baseline
# harvest, the transform, the replay on the optimised IR, the law
# over the whole positive corpus, the refusals, the demos, the
# report, the tools). Tool-only — no solver needed; the native half
# of the demo parity runs when bin/hlc exists.
reverify-acceptance:
	@echo "[Stage 108 acceptance] running tests/reverify_acceptance.py..."
	@$(PYTHON) tests/reverify_acceptance.py

# ============================================================================
# Stage 109 (v0.128.0-alpha): taint-tracking through FFI boundaries
# ============================================================================

# The FFI taint-flow report on any entry file:
# make ffi-taint F=examples/ffi_taint_demo.hls
ffi-taint:
	@python3 tools/hlprove.py $(F) --taint

# Stage 109 acceptance gate: the parser batteries (both front-ends), the
# checker rules (source wrap, sink diagnostics, the well-formedness
# refusals), the audit-parity census, the hlprove --taint report (pinned
# lines), the demo + ok-test parity, the fail tests and the tools.
ffi-taint-acceptance: $(BIN)/hlc
	@echo "[Stage 109 acceptance] running tests/ffi_taint_acceptance.py..."
	@$(PYTHON) tests/ffi_taint_acceptance.py
