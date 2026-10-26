# ============================================================================
# Makefile — Halis (HLS)
# ============================================================================
CC      ?= gcc
CFLAGS  ?= -O2
PYTHON  ?= python3
HLC     = src/hlc.hls
SNAP    = snapshot/hlc.c
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

.PHONY: all bootstrap bootstrap-nolibc xbootstrap-acceptance refine-acceptance test examples clean run check bench install uninstall audit opt-stats emit-ir emit-llvm fmt lint lsp-check pkg-init pkg-add pkg-lock pkg-audit pkg-verify pkg-build pkg-publish pkg-log pkg-log-verify prove prove-full infer infer-smt model prove-acceptance invariant-acceptance cvc5 cvc5-acceptance shapes shapes-smt shapes-acceptance sidechannel sidechannel-acceptance ct ct-acceptance supply supply-pkg supply-acceptance sbom sbom-pkg sbom-release sbom-acceptance repro repro-pkg repro-release repro-verify repro-acceptance sign-keygen sign sign-verify sign-release sign-verify-release sign-selftest sign-acceptance tlog-verify tlog tlog-gossip tlog-witness tlog-witness-verify tlog-prove tlog-prove-verify tlog-selftest tlog-acceptance reverify reverify-json reverify-lto reverify-report reverify-selftest reverify-acceptance ffi-taint ffi-taint-acceptance sandbox sandbox-run sandbox-emit sandbox-release sandbox-verify-release sandbox-selftest sandbox-acceptance hltest fuzz cov fuzz-acceptance wasm-opt webapp webapp-acceptance serve serve-acceptance wasm-pack wasm-pack-check wasm-pack-pack wasm-pack-acceptance aarch64-bench aarch64-acceptance aarch64-list-targets stack-acceptance inline-acceptance opt-stats-report kernel-attrs escape-acceptance layout-report tail-acceptance tail-report asm-acceptance asm-attrs bench-stdlib spec-check stage32-acceptance async-acceptance stream-acceptance io-acceptance fs-acceptance net-acceptance http-acceptance http2-acceptance json-stream-acceptance regex-acceptance fmt-acceptance hash-acceptance collections-acceptance sync-acceptance thread-acceptance time-acceptance math-acceptance process-acceptance env-acceptance archive-acceptance uuid-ulid-acceptance cli-acceptance tui-acceptance color-acceptance progress-acceptance log-acceptance http-server-acceptance websocket-acceptance cookie-acceptance session-acceptance csrf-acceptance template-acceptance sse-acceptance graphql-acceptance openapi-acceptance jsffi-acceptance dom-acceptance freestanding freestanding-check freestanding-acceptance nostd nostd-acceptance alloc-acceptance mem-acceptance panic-acceptance stackguard-acceptance asmreg-acceptance link-acceptance boot-acceptance idt-acceptance mmio-acceptance port-acceptance dma-acceptance atomics-acceptance irqsafe-acceptance triple triple-x86_64-acceptance triple-aarch64-acceptance triple-riscv64-acceptance debuginfo-acceptance cap cap-acceptance test-linker auditlog-list auditlog-show auditlog-verify auditlog-sign auditlog-verify-sign auditlog-selftest auditlog-acceptance lsp-def-acceptance lsp-hints-acceptance lsp-refactor-acceptance fmt-comments-acceptance fmt-config-acceptance lint-fix-acceptance snapshot-acceptance cases-acceptance dap-acceptance repl-acceptance bench-acceptance rss-acceptance rt-acceptance det-acceptance compat-acceptance migrate-acceptance deprecated-acceptance semver-acceptance lts-acceptance lts-policy lts-policy-verify lts-policy-acceptance xdiff xdiff-acceptance fuzzseed fuzzseed-acceptance guarantee guarantee-report guarantee-acceptance perf perf-gate perf-dashboard perf-acceptance mem mem-gate mem-dashboard memdash-acceptance ctime ctime-gate ctime-dashboard ctime-acceptance size size-gate size-dashboard size-acceptance auditkit-brief auditkit-evidence auditkit-report auditkit-acceptance freeze freeze-verify freeze-report freeze-acceptance snapshot-status snapshot-verify snapshot-regen snapshot-prove snapshot-chain purebootstrap-acceptance finalaudit-brief finalaudit-evidence finalaudit-report finalaudit-gate finalaudit-acceptance

# Main goal: use the full bootstrap chain to build the native compiler
all: bootstrap

# PURE-HLS BOOTSTRAP (Stage 142, v0.161.0-alpha) — no Python anywhere
# in the chain. The seed is the committed C snapshot: the compiler's
# own emission for src/hlc.hls, carried in the tree (snapshot/hlc.c,
# content-addressed by snapshot/MANIFEST.json).
#   1. cc compiles the committed snapshot -> native hlc
#   2. native hlc re-derives its own C from the HLS sources
#   3. the fixed-point law: the derivation must equal the snapshot,
#      byte for byte — a hand-edited snapshot, or a stale one (sources
#      moved without `make snapshot-regen`), fails the build loudly.
# Since v0.164.0-alpha the seed is gone entirely: boot/ left the
# tree, and the interpreter that remains is lab tooling under
# tools/hlinterp/ — no law, no target, no build path names it.
bootstrap:
	@mkdir -p $(BIN)
	@echo "[1/3] cc: compiling the committed C snapshot..."
	@$(CC) $(CFLAGS) -o $(BIN)/hlc $(SNAP) -lm -pthread
	@echo "[2/3] native hlc re-derives itself from the HLS sources..."
	@$(BIN)/hlc $(HLC) $(BIN)/hlc_regen.c
	@echo "[3/3] fixed-point law: the derivation must equal the snapshot..."
	@cmp $(SNAP) $(BIN)/hlc_regen.c || { echo "BOOTSTRAP FAILED: bin/hlc re-derived C that differs from snapshot/hlc.c —"; echo "  the snapshot is stale (src/hlc/** moved without a regen) or hand-edited."; echo "  Run 'make snapshot-regen' and commit snapshot/hlc.c + snapshot/MANIFEST.json together,"; echo "  and cross-check the lineage with 'make snapshot-prove' (freestanding) and 'make snapshot-chain'."; rm -f $(BIN)/hlc_regen.c; exit 1; }
	@rm -f $(BIN)/hlc_regen.c
	@echo "BOOTSTRAP OK: pure-HLS fixed point holds (no Python in the chain)"
	@echo "Native compiler: $(BIN)/hlc"

# Stage 143 (v0.162.0-alpha): the snapshot kit, in Halis; the chain
# law joined in v0.163.0-alpha. The tool that guards the bootstrap is
# itself built by it: hlc (compiled from the committed snapshot)
# compiles tools/hls-snap.hls, cc links bin/hls-snap — no Python
# anywhere on the lifecycle. The five commands below are the ONLY
# legal way to touch snapshot/hlc.c (four to mutate it, one to prove
# the chain it travels) — never edit it by hand. The kit's scratch
# files (bin/hls-snap-*) live in the build directory and are wiped by
# `make clean`.
# The wiring is deliberately BOOTSTRAP-FREE: regen and status must
# stay runnable on a drifted tree (where `make bootstrap` fails by
# design — that failure IS the fixed-point law naming the drift),
# so a missing bin/hlc is filled by one bare cc on the snapshot —
# the kit's own commands do their own proving. Prerequisites are the
# kit's sources only (tools/hls-snap.hls + std/), never $(SNAP): a
# make-level refusal must not hide the tool's own named laws.
# hlfmt — the formatter, in Halis (the byte-exact native twin of the
# Python hlfmt that retired). Same prerequisites discipline as the
# snapshot kit: the tool's sources + std + core, never $(SNAP).
$(BIN)/hlfmt: tools/hlfmt.hls $(wildcard std/*.hls) $(wildcard core/*.hls) | $(BIN)/hlc
	@mkdir -p $(BIN)
	@echo "[hlfmt] hlc compiles the formatter (self-hosted, no python)..."
	@$(BIN)/hlc tools/hlfmt.hls $@.c
	@$(CC) $(CFLAGS) -o $@ $@.c -lm -pthread
	@rm -f $@.c

# hls-lts — the LTS policy tool, in Halis (the native twin of the
# Python hls-lts that retired; the render it pins is the committed
# LTS.md, byte for byte).
$(BIN)/hls-lts: tools/hls-lts.hls $(wildcard std/*.hls) $(wildcard core/*.hls) | $(BIN)/hlc
	@mkdir -p $(BIN)
	@echo "[hls-lts] hlc compiles the LTS policy tool (self-hosted, no python)..."
	@$(BIN)/hlc tools/hls-lts.hls $@.c
	@$(CC) $(CFLAGS) -o $@ $@.c -lm -pthread
	@rm -f $@.c

$(BIN)/hls-snap: tools/hls-snap.hls $(wildcard std/*.hls)
	@mkdir -p $(BIN)
	@test -x $(BIN)/hlc || { echo "[snapkit] cc: a compiler from the committed snapshot (bootstrap-free)..."; $(CC) $(CFLAGS) -o $(BIN)/hlc $(SNAP) -lm -pthread; }
	@echo "[snapkit] hlc compiles the snapshot kit (self-hosted, no python)..."
	@$(BIN)/hlc tools/hls-snap.hls $(BIN)/hls-snap.c
	@$(CC) $(CFLAGS) -o $(BIN)/hls-snap $(BIN)/hls-snap.c -lm -pthread

# ----------------------------------------------------------------------------
# The self-hosted lab tools: hlc compiles them, cc links them — no
# Python. Each follows the snapshot-kit pattern (a missing bin/hlc is
# compiled from the committed C snapshot, never a Python bootstrap):
#   hls-spec-check   — the Stage 32 generic-specialisation verifier
#   simd-ratio       — the Stage 21 SIMD acceptance measurement
#   pgo-ratio        — the Stage 19 PGO acceptance measurement
#   fix-makefile-indent — the recipe-indent repair tool
# ----------------------------------------------------------------------------
$(BIN)/hls-spec-check: tools/hls-spec-check.hls $(wildcard std/*.hls)
	@mkdir -p $(BIN)
	@test -x $(BIN)/hlc || { echo "[toolkit] cc: a compiler from the committed snapshot (bootstrap-free)..."; $(CC) $(CFLAGS) -o $(BIN)/hlc $(SNAP) -lm -pthread; }
	@echo "[toolkit] hlc compiles hls-spec-check (self-hosted, no python)..."
	@$(BIN)/hlc tools/hls-spec-check.hls $(BIN)/hls-spec-check.c
	@$(CC) $(CFLAGS) -o $(BIN)/hls-spec-check $(BIN)/hls-spec-check.c -lm -pthread

$(BIN)/simd-ratio: scripts/simd_ratio.hls $(wildcard std/*.hls)
	@mkdir -p $(BIN)
	@test -x $(BIN)/hlc || { echo "[toolkit] cc: a compiler from the committed snapshot (bootstrap-free)..."; $(CC) $(CFLAGS) -o $(BIN)/hlc $(SNAP) -lm -pthread; }
	@$(BIN)/hlc scripts/simd_ratio.hls $(BIN)/simd-ratio.c
	@$(CC) $(CFLAGS) -o $(BIN)/simd-ratio $(BIN)/simd-ratio.c -lm -pthread

$(BIN)/pgo-ratio: scripts/pgo_ratio.hls $(wildcard std/*.hls)
	@mkdir -p $(BIN)
	@test -x $(BIN)/hlc || { echo "[toolkit] cc: a compiler from the committed snapshot (bootstrap-free)..."; $(CC) $(CFLAGS) -o $(BIN)/hlc $(SNAP) -lm -pthread; }
	@$(BIN)/hlc scripts/pgo_ratio.hls $(BIN)/pgo-ratio.c
	@$(CC) $(CFLAGS) -o $(BIN)/pgo-ratio $(BIN)/pgo-ratio.c -lm -pthread

$(BIN)/fix-makefile-indent: scripts/fix_makefile_indent.hls
	@mkdir -p $(BIN)
	@test -x $(BIN)/hlc || { echo "[toolkit] cc: a compiler from the committed snapshot (bootstrap-free)..."; $(CC) $(CFLAGS) -o $(BIN)/hlc $(SNAP) -lm -pthread; }
	@$(BIN)/hlc scripts/fix_makefile_indent.hls $(BIN)/fix-makefile-indent.c
	@$(CC) $(CFLAGS) -o $(BIN)/fix-makefile-indent $(BIN)/fix-makefile-indent.c -lm -pthread

snapshot-status: $(BIN)/hls-snap
	@$(BIN)/hls-snap status

snapshot-verify: $(BIN)/hls-snap
	@$(BIN)/hls-snap verify --deep

snapshot-regen: $(BIN)/hls-snap
	@$(BIN)/hls-snap regen

# Stage 143 (v0.163.0-alpha, the record corrected): the chain law —
# the bit-for-bit reproducible bootstrap chain. `make snapshot-chain`
# re-runs the whole bootstrap TWICE through the self-hosted kit, as
# two independent ignitions, and byte-compares every hop: each
# round's emission equals the snapshot (the fixed point) AND the
# previous round's emission (the repeatability the fixed point alone
# does not name). CC2=clang puts a second C compiler in round two's
# slot — the chain's tracked artifact is what the HLS compiler emits,
# not what cc links, so any conforming cc must land on the same
# bytes. The command closes with the chain digest — the one number
# two machines, two checkouts and two compilers must agree on.
snapshot-chain: $(BIN)/hls-snap
	@$(BIN)/hls-snap chain $(if $(CC2),--cc2 $(CC2),)

snapshot-prove: $(BIN)/hls-snap
	@$(BIN)/hls-snap prove

# The acceptance gates, self-hosted: every tests/acc/*_acceptance.hls
# battery is compiled NATIVE by bin/hlc + cc and run as a binary —
# the gate itself is Halis, the way the kit is. A gate may still
# spawn python3 tools/hlinterp/boot.py where a leg exercises the
# interpreter BY DESIGN (the differential oracle) or a Python tool
# that has not moved yet; the harness is tests/acc/harness.hls and
# the scratch world lives under tests/.acc-scratch/ (gitignored,
# wiped per run). The mk/*.mk targets depend on $(BIN)/acc-<gate>.
$(BIN)/acc-%: tests/acc/%_acceptance.hls tests/acc/harness.hls $(wildcard std/*.hls) $(BIN)/hlfmt | $(BIN)/hlc
	@mkdir -p $(BIN)
	@echo "[acc] hlc compiles the $* gate (self-hosted, no python)..."
	@$(BIN)/hlc tests/acc/$*_acceptance.hls $@.c
	@$(CC) $(CFLAGS) -o $@ $@.c -lm -pthread

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

# Only check types + effects (no execution). Self-hosted since
# v0.164.0-alpha: checking IS the compile pipeline's front half,
# so the native compiler does it and the C file is a byproduct.
check:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@$(BIN)/hlc $(F) $(BIN)/hls_check.c

# Full test suite
test:
	bash tests/run_tests.sh

# Run the benchmarks folder (interpreter + native paths)
bench:
	@bash benchmarks/run_bench.sh

# Print the capability / effect tree of every function in a program
# (self-hosted: hlc --audit)
audit:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@$(BIN)/hlc --audit $(F)

# Stage 11 (v0.9.0-alpha): print the HLIR of a program. Lab
# tooling: the interpreter's printer, off every build path.
emit-ir:
	@$(PYTHON) tools/hlinterp/boot.py --emit ir $(F)

# Stage 12 (v0.10.0-alpha): print the LLVM IR of a program. Lab
# tooling: the interpreter's printer, off every build path.
emit-llvm:
	@$(PYTHON) tools/hlinterp/boot.py --emit llvm $(F)

# Stage 11: run the optimiser and print per-pass statistics
# (self-hosted: hlc --opt-stats)
opt-stats:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@$(BIN)/hlc --opt-stats $(F) $(BIN)/hls_stats.c

# Stage 14 (v0.12.0-alpha): opinionated formatter. The formatter is
# Halis now (tools/hlfmt.hls, the full port of the Python years):
# hlc compiles it, cc links bin/hlfmt, no interpreter in the loop.
fmt: $(BIN)/hlfmt
	@$(BIN)/hlfmt $(F)

# Stage 14: linter
lint:
	@$(PYTHON) tools/hllint.py $(F)

# Stage 14: one-shot LSP diagnostics (for non-LSP editors)
lsp-check:
	@$(PYTHON) tools/hls-lsp.py --check $(F)

# Stage 113 acceptance gate: the eight-section battery for go-to-
# definition across packages (the std family, the sibling, the
# package layers, the layer law, the guards, the externals, the
# completion + LRU, the smoke + corpus). Protocol-level - the gate
# drives the real server over stdio JSON-RPC; the native half of
# the corpus parity runs when gcc exists.
lsp-def-acceptance:
	@echo "[Stage 113 acceptance] running tests/lsp_def_acceptance.py..."
	@$(PYTHON) tests/lsp_def_acceptance.py

# Stage 114 acceptance gate: the eight-section battery for inlay
# hints (the call sites, the layer law, the guards, the match
# bindings, the redundancies, the protocol, the utf-16 line, the
# smoke + corpus). Protocol-level - the gate drives the real server
# over stdio JSON-RPC.
lsp-hints-acceptance:
	@echo "[Stage 114 acceptance] running tests/lsp_hints_acceptance.py..."
	@$(PYTHON) tests/lsp_hints_acceptance.py

# Stage 115 acceptance gate: the eight-section battery for the refactor
# actions (the scope law, the renames, the inlines, the extracts, the
# protocol, the utf-16 line, the edits land, the smoke + corpus).
# Protocol-level - the gate drives the real server over stdio JSON-RPC
# and applies every offered edit before re-checking and re-running it.
lsp-refactor-acceptance: $(BIN)/hlfmt
	@echo "[Stage 115 acceptance] running tests/lsp_refactor_acceptance.py..."
	@$(PYTHON) tests/lsp_refactor_acceptance.py

# Stage 116 acceptance gate: the eight-section battery for hlfmt's
# comment preservation (the sigils, the duplication law, the
# positions, the re-indent law, the preservation law, the one-liners,
# the differential, the corpus). The differential half compiles the
# stage's ok fixture on bin/hlc and gcc, so the native build is a
# prerequisite.
fmt-comments-acceptance: $(BIN)/acc-fmt_comments
	@echo "[Stage 116 acceptance] running tests/acc/fmt_comments_acceptance.hls..."
	@$(BIN)/acc-fmt_comments

# Stage 117 acceptance gate: the eight-section battery for hlfmt's team
# configuration file (.hlfmt.toml): the discovery walk (nearest file
# wins, --config pins, --no-config ignores), the five knobs, the
# strict grammar (a typo is exit 2, never a style), --print-config,
# the per-config fixed points, the default law, the interpreter-vs-
# native differential under a non-default config, and the corpus. The
# differential half compiles the stage's ok fixture on bin/hlc and
# gcc, so the native build is a prerequisite.
fmt-config-acceptance: $(BIN)/acc-fmt_config
	@echo "[Stage 117 acceptance] running tests/acc/fmt_config_acceptance.hls..."
	@$(BIN)/acc-fmt_config

# Stage 118 acceptance gate: the eight-section battery for hllint's
# autofix mode (the scope renames, the discard wraps, the signature
# cleanups, the deletions, the safety net, the fixed point, the
# interpreter-vs-native differential before and after the fix, and the
# corpus). The differential half compiles the stage's ok fixture on
# bin/hlc and gcc, so the native build is a prerequisite.
lint-fix-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 118 acceptance] running tests/lint_fix_acceptance.py..."
	@$(PYTHON) tests/lint_fix_acceptance.py

# Stage 119 (v0.138.0-alpha): snapshot-testing acceptance gate.
# The differential half compiles the stage's ok fixture on
# bin/hlc and gcc, so the native build is a prerequisite.
snapshot-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 119 acceptance] running tests/snapshot_acceptance.py..."
	@$(PYTHON) tests/snapshot_acceptance.py

# Stage 120 (v0.139.0-alpha): parameterised (table-driven) testing
# acceptance gate. The differential half compiles the stage's ok
# fixture on bin/hlc and gcc, so the native build is a prerequisite.
cases-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 120 acceptance] running tests/cases_acceptance.py..."
	@$(PYTHON) tests/cases_acceptance.py

# Stage 121 (v0.140.0-alpha): VS Code extension debugger (DAP)
# acceptance gate. Nine sections over the REAL adapter subprocess:
# the protocol lifecycle, breakpoint verification and snapping, the
# stack, stepping, the variables view, console evaluation, panic
# stops, protocol robustness, and the editor's debug contributions.
dap-acceptance: $(BIN)/hlfmt
	@echo "[Stage 121 acceptance] running tests/dap_acceptance.py..."
	@$(PYTHON) tests/dap_acceptance.py

# Stage 123 (v0.141.0-alpha): the interactive REPL acceptance gate.
# Eight sections over the REAL repl subprocess (piped stdin): the
# session-as-a-program model, definitions at the prompt, the no-replay
# guarantee, :type, :effects, the session commands (:audit/:env/:load/
# :reset), panics and refusals, and the corpus + transcript parity
# (the canonical session's `=` lines are the fixture program's output,
# natively byte-identical when bin/hlc + gcc exist).
repl-acceptance: $(BIN)/hlfmt
	@echo "[Stage 123 acceptance] running tests/repl_acceptance.py..."
	@$(PYTHON) tests/repl_acceptance.py


# Stage 124 (v0.142.0-alpha): the criterion-style benchmark runner
# acceptance gate. Eight sections over the REAL hls-bench CLI: the
# convention (suites, units, refusals), the synthesized driver and its
# wire protocol, the statistics (quantiles, Tukey outliers, seeded
# bootstrap, Welch p), the criterion-layout report and its change
# matrix, the baseline store (save/compare/refusals), exit codes and
# the CLI matrix, the fixed-plan interpreter-vs-native differential,
# and the corpus hygiene. The differential half compiles on bin/hlc
# and gcc, so the native build is a prerequisite.
bench-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 124 acceptance] running tests/bench_acceptance.py..."
	@$(PYTHON) tests/bench_acceptance.py


# Stage 125 (v0.143.0-alpha): the RSS-stability verification gate.
# Eight sections over the REAL hls-rss CLI: the convention and its
# refusal matrix (markers, look-alikes, argv plan-driving), the
# sampler and the protocol shape (cycle-aligned points, the honest
# time-based fallback), the statistics (Theil-Sen against
# hand-computed data, the seeded bootstrap's determinism, the
# verdict lattice), the interposer and the fresh-builtin census (the
# balance every Stage 125 expr_fresh registration owes, cwd_get's
# FOREIGN_FREE honesty, the in-scope exit(0) retention), the report
# and --json layout, the two-run retained comparison (the exact-free
# live delta, the honest skip for argv-ignoring workloads, the
# --no-malloc-count skip), the differential and the leak matrix (the
# fixture byte-identical on both backends, churn/server STABLE, the
# canary LEAK with both instruments firing, the match-in-concat
# hoisting-cycle regression), and the corpus hygiene. The gate
# compiles on bin/hlc and gcc, so the native build is a prerequisite.
rss-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 125 acceptance] running tests/rss_acceptance.py..."
	@$(PYTHON) tests/rss_acceptance.py
	@echo "ACCEPTANCE OK: Stage 125 -- GC-free runtime verification (RSS stability)"


# Stage 126 (v0.144.0-alpha): the soft-real-time verification gate.
# Eight sections over the REAL hls-rt CLI and the REAL instrument: the
# convention and its refusal matrix (the axes, the all-zero budget that
# is not a mode), the wrap driven from its own C harness (the roll and
# snapshot semantics, realloc deltas, the live sum, the fenced report,
# the totals re-derivation), the runtime wiring (weak hooks, the arm
# call, the marker scan; nothing in a --no-libc or freestanding
# emission; an uninstrumented binary ignores its own markers), the
# steady certification (a constant ledger line, live flat, --json
# agreement, argv passthrough), the enforcement matrix (the three
# canaries die on their own axes with the canonical message, --warmup
# rescues exactly the fat-first cycle, --no-enforce measures and still
# refuses), the honesty gates (no markers, uncovered work, protocol
# look-alikes), the report and the statistics against hand-computed
# values, and the differential + corpus hygiene. The gate compiles on
# bin/hlc and gcc, so the native build is a prerequisite.
rt-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 126 acceptance] running tests/rt_acceptance.py..."
	@$(PYTHON) tests/rt_acceptance.py
	@echo "ACCEPTANCE OK: Stage 126 -- soft-real-time mode (bounded allocation per cycle)"



# Stage 127 (v0.145.0-alpha): the deterministic-scheduler gate. Eight
# sections over the REAL hls-det CLI and BOTH real runtimes: the
# convention and its refusal matrix (the runs/timeout gates, the
# composable flags), the scheduler policy as HAND-COMPUTED interleaving
# traces asserted line-by-line on both backends (the rotation, the
# park/wake order with the wakee ahead of the waker, the bounded
# backpressure with the re-park), the runtime wiring (the region and
# the arm call in the hosted emission, the stub macros and none of
# the mode in a --no-libc emission, the unarmed parity), the
# determinism certification (every fixture through the CLI, --json,
# --runs 8, argv passthrough), the fairness matrix (the recv_or poll
# loop's exact rotation answer, the hltest --det witness), the
# deadlock and panic parity (both schedulers, both backends, both
# modes, the canonical message, the certified deterministic death),
# the honesty gates (the clock workload refused, the forged step line,
# the validator's unit checks), and the differential + corpus hygiene
# (stdout/trace/exit identical across backends for the whole fixture
# set, hlfmt/hllint/checker green, no litter). The gate compiles on
# bin/hlc and gcc, so the native build is a prerequisite.
det-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 127 acceptance] running tests/det_acceptance.py..."
	@$(PYTHON) tests/det_acceptance.py
	@echo "ACCEPTANCE OK: Stage 127 -- deterministic-scheduler option (testing concurrency)"


# Stage 128 (v0.146.0-alpha): the backwards-compatibility gate. Eight
# sections over the REAL hls-compat CLI, the REAL git history (every
# release tag's ok corpus) and the REAL ledger: the convention and its
# refusal matrix (missing/corrupt/forged ledgers are exit 2), the
# census and the keying laws (version sort, content addressing,
# import-closure contexts, toolchain fingerprints), the ledger
# discipline (every sha spot-checked against the git object it names;
# --update-ledger drafts honestly and never writes), the certification
# (the full union corpus replays green, the json carrying every
# verdict), the regression alarm (a probe commit tagged into real
# history is caught with its error line quoted; every direction of
# ledger drift fails the verdict), the time-travel witness (five
# releases, the hand-computed agreement matrix, every waiver verified
# against the history it claims), the native differential (149
# historical programs byte-identical across today's backends), and the
# corpus hygiene (fmt/lint/checker green, no litter). The gate needs
# bin/hlc + gcc, so the native build is a prerequisite.
compat-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 128 acceptance] running tests/compat_acceptance.py..."
	@$(PYTHON) tests/compat_acceptance.py
	@echo "ACCEPTANCE OK: Stage 128 -- backwards-compatibility test suite (every prior version's ok/)"


# Stage 129 (v0.147.0-alpha): the migration gate. Eight sections over
# the REAL hls-migrate CLI and the REAL migration bed
# (tests/migrate_tree, a v0.34-era tree carrying every pre-migration
# pattern): the convention and its refusal matrix (malformed versions,
# a backwards walk, unknown rule ids, non-.hls files — all exit 2), the
# registry and the unit laws (the oldest-first six, the release-tag
# grammar, the version sort, the five libxxhash XXH64 vectors, the
# hex64 word law, the narrow-GPR map's completeness, the string
# decoder's escape set), the plan over the bed (18 findings, 17 auto +
# 1 manual, precise line:col, and the bed byte-identical afterwards —
# a plan never touches a file), the walk (--apply --verify: 17 edits,
# all rewritten files green through the real checker, the .bak of the
# original beside each, the '..' import left untouched, the migrated
# app and digest RUNNING on both backends), the version gate (the
# roadmap phrase --from v0.34.0-alpha --to v1.0 fires exactly the four
# post-v0.34 rules; a v0.110 dialect and the since==from boundary are
# CLEAN; --only/--skip compose), the idempotence and honesty gates
# (the fixed point, the first-backup-wins, unified diffs, the json
# census, the undecodable golden as MANUAL advice), the verify gate
# (a migrated file that still fails the checker is exit 1, named; a
# MANUAL-carrying file is skipped, named), and the corpus hygiene
# (fmt/lint/checker/native green, the pristine bed still failing in
# exactly the pre-migration places, no .bak litter). The gate needs
# bin/hlc + gcc for its native legs, so the native build is a
# prerequisite.
migrate-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 129 acceptance] running tests/migrate_acceptance.py..."
	@$(PYTHON) tests/migrate_acceptance.py
	@echo "ACCEPTANCE OK: Stage 129 -- migration tooling (hls migrate)"

# Stage 131 (v0.149.0-alpha): the semver-enforcement gate. Eight
# sections over the REAL hls-pkg CLI and the REAL enforcement module
# (tools/hpkg_parts/hpkg_semver.py): the convention (check/cmp/bump/sat
# exits 0/1/2 with the grammar named in every refusal), the grammar and
# the spec §11 precedence law (the canonical chain, build metadata
# ignored, numeric < alphanumeric, leading zeros refused, tag_version
# stripping one v), the requirement dialect (caret, tilde, comparators,
# wildcards, hyphen ranges, conjunctions, the pre-release opt-in rule,
# and the named refusals: '||', partials after comparators, wildcards
# under ^/~), the manifest enforcement over a REAL local git repo
# (lock validates the own version, judges a tag against its
# requirement - green, violating, non-semver, manifest-less branch -
# build refuses before compiling, add --version checks the grammar at
# the keyboard), the publish laws (a version REQUIRED and valid, the
# immutability contract: identical no-op / different-content refusal /
# build-metadata identity), the verify drift law (edited, dropped and
# legacy lockfiles all fail verification, named; a tampered lockfile
# version fails the satisfaction re-check), the pure-dep correction (a
# clean pure audit is recorded PURE; an unauditable dep still lands in
# the full fail-closed set), and the corpus showcase (the semver laws
# as a pure-HLS program - checker, interpreter, native differential,
# fmt-canonical, lint-clean). The gate runs its own isolated ledger,
# cache and audit log via the Stage 131 env overrides, so it never
# touches the developer's real state.
semver-acceptance: $(BIN)/hlfmt
	@echo "[Stage 131 acceptance] running tests/semver_acceptance.py..."
	@$(PYTHON) tests/semver_acceptance.py
	@echo "ACCEPTANCE OK: Stage 131 -- semantic-versioning enforcement in hls-pkg"

# Stage 132 (v0.150.0-alpha): the LTS-policy gate. Eight sections
# over the REAL hls-lts CLI and REAL scratch git repositories (an
# LTS gate that did not drive git would be a memo, not a policy):
# the convention (exits 0/1/2, every refusal names the rule), the
# init law (a >= 1.0.0 release base, the .hls-lts.json declaration,
# the branch created at the base, idempotent re-init, a different
# base refused even when the tag does not exist), the classifier
# (the message table, the fail-closed default, and the diff gate: a
# fix adding a std fn is a FEATURE in disguise, an in-place edit
# stays BUGFIX, a tools/ def is implementation detail, SPEC.md edits
# are language changes), the backport law (the green pick with its
# Backported-from trailer and git's -x line, the dry run, refusals
# by class with the branch byte-identical, the duplicate pick, the
# dirty worktree, the conflicted pick), the tag law (the
# empty-release refusal, v1.0.1 cut after a backport, minor/pre-
# release/gap/non-semver refusals named), the audit law (a clean
# branch passes, a rogue commit, a forged trailer, a ghost sha, an
# unclassified direct commit, a tag gap and an out-of-line tag are
# all violations named sha by sha), the release train (init ->
# backport -> v1.0.1 -> backport -> v1.0.2 -> audit green -> status
# shows the chain), and the corpus showcase (the policy laws as a
# pure-HLS program - checker, interpreter, native differential,
# fmt-canonical, lint-clean). The gate builds its own scratch repos
# under a temp root it removes afterwards; the developer's real
# repository is never touched.
lts-acceptance: $(BIN)/hlfmt $(BIN)/hls-lts
	@echo "[Stage 132 acceptance] running tests/lts_acceptance.py..."
	@$(PYTHON) tests/lts_acceptance.py
	@echo "ACCEPTANCE OK: Stage 132 -- LTS branch policy (backport-only after v1.0)"

# Stage 145 (v0.166.0-alpha): the support-window gate. The published
# policy is LTS.md at the repository root, and the document is a
# RENDER, not a memo: `hls-lts policy` derives it from the tool's
# own constants (3 years bug-fix, 5 years security), two renders are
# byte-identical, and `make lts-policy-verify` re-derives the
# committed copy on every run — a hand-edited policy is visible as a
# diff, and the tree says no until it is regenerated. The window law
# itself lives in the Stage 132 tool: `backport` refuses plain fixes
# on a security-only line and everything on a dead one, `tag-patch`
# cuts no release past end of life and no chore-only release in the
# security phase, `audit` judges history by the day each commit
# landed, and `--at` pins the clock so the gates never depend on
# when they run.
lts-policy: $(BIN)/hls-lts
	@$(BIN)/hls-lts policy

lts-policy-verify: $(BIN)/hls-lts
	@$(BIN)/hls-lts policy --check

# The Stage 145 acceptance gate: nine sections over the REAL policy
# CLI and REAL scratch git repositories whose birthdays are
# fabricated on command (GIT_COMMITTER_DATE), because a support
# window cannot wait three years to be tested: the convention (the
# --at grammar, the missing-policy refusals, the exits), the
# calendar law (anniversaries, the February-29 clamp, the horizon
# day that belongs to the phase it closes), the phase machine
# (active / security / eol over fabricated bases, --json exact), the
# marking law (the Security trailer grammar, the fail-closed
# default, the empty value that is no claim), the backport window
# law (the plain fix refused in security-only with both horizons
# named, the marked one landing, the dead line refusing everything,
# dry runs that reflect the phase), the tag law (no release from a
# dead line, the chore-only security release refused, the release
# carrying a marked fix cutting green), the audit law (the unmarked
# landing in security-only, the commit and the tag past end of
# life), the publication law (the render deterministic, the
# committed LTS.md matching, the drifted copy refused) — and the
# corpus showcase, the window law as a pure-HLS program.
lts-policy-acceptance: $(BIN)/hlfmt $(BIN)/hls-lts
	@echo "[Stage 145 acceptance] running tests/lts_policy_acceptance.py..."
	@$(PYTHON) tests/lts_policy_acceptance.py
	@echo "ACCEPTANCE OK: Stage 145 -- LTS policy published (3 years bug-fix, 5 years security)"

# Stage 133 (v0.151.0-alpha): the cross-impl differential table. The
# default sweep sits the purity-gated corpus (tests/ok + the
# tests/xdiff fixtures) in front of all four implementations and
# compares bytes and halt classes; the ledger keeps the six real
# findings of the first sweep loud and sanctioned. `make xdiff` is
# the fast core (every verdict class in ~a minute); `make
# xdiff-acceptance` is the gate: eight sections over the real CLI,
# the real backends and the negative space of the ledger law.
xdiff:
	@$(PYTHON) tools/hls-xdiff.py --core

xdiff-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 133 acceptance] running tests/xdiff_acceptance.py..."
	@$(PYTHON) tests/xdiff_acceptance.py
	@echo "ACCEPTANCE OK: Stage 133 -- cross-impl differential suite (3+ backends)"

# Stage 134 (v0.152.0-alpha): fuzz corpus seeding from real-world
# packages. `make fuzzseed` harvests the real-world corpus — the
# hls-pkg trees when present, plus the project's own std/, core/,
# examples/, tests/ok/ and benchmarks/ — into fuzz-corpus/seed/, each
# seed a type-correct harness with provenance, census and smoke class
# in the manifest. `make fuzzseed-acceptance` is the gate: eight
# sections over the real CLI, the real parser+checker, the real
# native backend, the real fuzzer and the fixture package
# (tests/fuzzseed_pkg) laid out exactly as hls-pkg lays a downloaded
# dependency out.
fuzzseed:
	@$(PYTHON) tools/hls-fuzzseed.py --limit 200 --mutate 1

fuzzseed-acceptance: bin/hlc
	@echo "[Stage 134 acceptance] running tests/fuzzseed_acceptance.py..."
	@$(PYTHON) tests/fuzzseed_acceptance.py
	@echo "ACCEPTANCE OK: Stage 134 -- fuzz corpus seeding from real-world packages"

# Stage 135 (v0.153.0-alpha): the soundness guarantee ledger —
# bug-bounty-eligible guarantees, documented AND re-demonstrated.
# `make guarantee` re-demonstrates the whole ledger against the live
# tree: every anchor grepped at its enforcement point, every probe
# re-run through the real checker/interpreter (refusal messages and
# panic exits pinned), the SECURITY.md channel anchors confirmed.
# `make guarantee-acceptance` is the gate: eight sections over the
# real CLI, the real checker and the negative space (a tampered tree
# voids its guarantees by name; a violated ledger refuses its
# report; two reports at one tree are byte-identical).
guarantee:
	@$(PYTHON) tools/hls-guarantee.py verify

guarantee-report:
	@$(PYTHON) tools/hls-guarantee.py report --out SOUNDNESS.md

guarantee-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 135 acceptance] running tests/guarantee_acceptance.py..."
	@$(PYTHON) tests/guarantee_acceptance.py
	@echo "ACCEPTANCE OK: Stage 135 -- bug-bounty-eligible soundness guarantees documented"

# Stage 136 (v0.154.0-alpha): the per-commit performance regression
# dashboard. hls-bench (Stage 124) measures; hls-perf remembers and
# judges: every record is one measurement pinned to one clean commit
# in the append-only .hlperf/history.jsonl ledger, the gate classifies
# the newest record against the prior record of the SAME instrument
# (Stage 124's own Welch + seeded-bootstrap law) and fails on any
# unsanctioned regressed-or-vanished bench, sanctions are written
# excuses with an author, and the dashboard renders the ledger
# deterministically (sorted walks, the sha256 ledger digest embedded,
# no wall-clock unless --stamp — two renders of one ledger are
# byte-identical). `make perf` records the tracked suite
# (benchmarks/regression_watch.hls) at the current commit; `make
# perf-gate` is the CI leg; `make perf-dashboard` regenerates
# PERFORMANCE.md; `make perf-acceptance` is the gate: eight sections
# over the real CLI, the real runner, controlled ledgers and the
# negative space (floor refusals, instrument mismatches, corrupt
# lines, the dirty-tree refusal on a throwaway clone).
perf:
	@$(PYTHON) tools/hls-perf.py record

perf-gate:
	@$(PYTHON) tools/hls-perf.py gate

perf-dashboard:
	@$(PYTHON) tools/hls-perf.py dashboard --out PERFORMANCE.md

perf-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 136 acceptance] running tests/perf_acceptance.py..."
	@$(PYTHON) tests/perf_acceptance.py
	@echo "ACCEPTANCE OK: Stage 136 -- performance regression dashboard (per-commit)"

# Stage 137 (v0.155.0-alpha): the memory regression dashboard, RSS
# over time. hls-rss (Stage 125) measures one workload once; hls-mem
# remembers and judges: every record is one measurement of the
# tracked suite (benchmarks/rss_watch/ — four deterministic,
# allocation-shaped canaries: the resident-set floor, string growth,
# heap churn, boxed objects) pinned to one clean commit in the
# append-only .hlmem/history.jsonl ledger, where every workload must
# certify STABLE before it can be filed (the floor law — a canary
# that leaks is a bug, not a record). The gate classifies the newest
# record against the prior record of the same instrument (same suite
# digest AND same page size — a page is a unit) with the page law:
# the metric is the marker median (the resident set at the cycle
# markers, measured under setarch -R with the layout pinned);
# significance is the seeded bootstrap CI of the median change
# excluding zero (RSS is integer pages, often zero variance — a
# t-test would call a deterministic growth no-change), and the
# noise band is floored at 16 pages. `make mem` records at the
# current commit; `make mem-gate` is the CI leg; `make mem-dashboard`
# regenerates MEMORY.md; `make memdash-acceptance` is the gate:
# eight sections over the real CLI, the real instrument, controlled
# ledgers and the negative space (floor refusals, unit mismatches,
# corrupt lines, the dirty-tree refusal on a throwaway clone).
mem:
	@$(PYTHON) tools/hls-mem.py record

mem-gate:
	@$(PYTHON) tools/hls-mem.py gate

mem-dashboard:
	@$(PYTHON) tools/hls-mem.py dashboard --out MEMORY.md

memdash-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 137 acceptance] running tests/memdash_acceptance.py..."
	@$(PYTHON) tests/memdash_acceptance.py
	@echo "ACCEPTANCE OK: Stage 137 -- memory regression dashboard (RSS over time)"

# Stage 138 (v0.156.0-alpha): the compile-time regression dashboard.
# The instrument is the compiler itself, and Halis ships two of
# them, so they are two instruments, never compared: stage0 (the
# bootstrap chain — tools/hlinterp/boot.py running src/hlc.hls) and native
# (bin/hlc, the self-hosted compiler). Both time the same quantity —
# one full HLS-to-C compile of every workload of the tracked suite
# (benchmarks/ctime_watch/ — five deterministic, compile-shaped
# workloads: the token stream, the parse tree, the type checker,
# the effect graph, the code generator) — and every record is
# pinned to one clean commit in the append-only .hlctime/
# history.jsonl ledger, with the compiler's OWN identity pinned
# twice over: the sha256 of the compiler source and the sha256 of
# the binary actually timed. The stale-binary law bites at record
# time — a native record whose bin/hlc was already filed against a
# different compiler source is refused (run `make bootstrap`
# first), while a doc-only commit's identical binary is expected.
# The gate classifies the newest record against the prior record of
# the same instrument (Stage 124's own Welch + seeded-bootstrap
# law, a 10% noise band honest about wall time) and fails on any
# unsanctioned regressed-or-vanished workload; sanctions are
# written excuses with an author; the dashboard renders the ledger
# deterministically (sorted walks, the sha256 ledger digest
# embedded, no wall-clock unless --stamp — two renders of one
# ledger are byte-identical). `make ctime` records the tracked
# suite at the current commit through the stage0 chain
# (`--instrument native` for bin/hlc); `make ctime-gate` is the CI
# leg; `make ctime-dashboard` regenerates COMPILETIME.md; `make
# ctime-acceptance` is the gate: eight sections over the real CLI,
# both real compilers, the real tracked suite and the negative
# space (floor refusals, the stale-binary refusal, instrument
# mismatches, corrupt lines, the dirty-tree refusal on a throwaway
# clone).
ctime:
	@$(PYTHON) tools/hls-ctime.py record

ctime-gate:
	@$(PYTHON) tools/hls-ctime.py gate

ctime-dashboard:
	@$(PYTHON) tools/hls-ctime.py dashboard --out COMPILETIME.md

ctime-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 138 acceptance] running tests/ctime_acceptance.py..."
	@$(PYTHON) tests/ctime_acceptance.py
	@echo "ACCEPTANCE OK: Stage 138 -- compile-time regression dashboard (per-commit)"

# Stage 139 (v0.157.0-alpha): the binary-size regression dashboard.
# Binary size is the axis with NO NOISE — the emitted C is byte-exact
# by the bootstrap invariant and the link recipe is fixed, so the
# ledger keeps no samples, runs no statistics and hides behind no
# noise band: ONE BYTE of growth is a regression the gate names.
# The tracked suite (benchmarks/size_watch/ — five deterministic,
# size-shaped workloads: the runtime floor, the string surface, the
# per-function call machinery, the type-driven struct emission, the
# match dispatch) is measured on five axes — emitted C bytes, linked
# binary bytes, and the size(1) text/data/bss breakdown — and the
# native record carries one more workload: hlc_self, the compiler's
# OWN girth. Every record is pinned to one clean commit in the
# append-only .hlsize/ ledger with the compiler's identity pinned
# twice (source digest + binary digest) and the MACHINE pinned too
# (cc, arch, recipe — sizes from two toolchains are not the same
# language, and the gate refuses to translate). The stale-binary law
# bites at record time; sanctions are written excuses with an
# author; the dashboard renders the ledger deterministically (two
# renders byte-identical). `make size` records through bin/hlc;
# `make size-gate` is the CI leg; `make size-dashboard` regenerates
# BINARYSIZE.md; `make size-acceptance` is the gate: eight sections
# over the real CLI, both real instruments, the real tracked suite
# and the negative space (floor refusals, the stale-binary refusal,
# instrument and machine mismatches, corrupt lines, the dirty-tree
# refusal on a throwaway clone).
size:
	@$(PYTHON) tools/hls-size.py record

size-gate:
	@$(PYTHON) tools/hls-size.py gate

size-dashboard:
	@$(PYTHON) tools/hls-size.py dashboard --out BINARYSIZE.md

size-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 139 acceptance] running tests/size_acceptance.py..."
	@$(PYTHON) tests/size_acceptance.py
	@echo "ACCEPTANCE OK: Stage 139 -- binary-size regression dashboard (per-commit)"

# Stage 140 (v0.158.0-alpha): the third-party audit kit. The
# independent, paid security audit as a chained ledger
# (.hlaudit/engagement.jsonl, machine-local like every ledger in
# this tree). The scope is derived LIVE from the Stage 135
# guarantee ledger (hls-guarantee is imported at run time, never
# copied); the brief (the RFP) is a deterministic render of that
# scope and may run before any ledger exists, because solicitation
# precedes contract; `evidence` re-runs the whole scope through the
# real checker and appends one chained transcript naming WHO ran
# it — a pass by a roster member is INDEPENDENT, anyone else's is
# SELF-ASSESSED, and the report refuses to call a self-run pass
# independent. Findings are the auditors' to file (the project
# cannot author one): a severity from the closed five-word audit
# vocabulary, a guarantee row from the scope, and a REPRODUCER (a
# path that exists or inline text, never both, never neither). The
# triage is the project's to give (an auditor cannot respond to
# one): one verdict, a written reason, and for an acceptance the
# remediation commit itself, 40 hex naming a commit that exists.
# The chain is the family's one arithmetic (imported from
# hltlog_parts since Stage 13); every reader verifies it first and
# names the broken line. The state (vacant/open/underway/reported/
# responded/remediated) is derived from the census, never stored.
# `make auditkit-brief` renders the RFP; `make auditkit-evidence`
# files a pass through the project's own identity (honestly
# labelled SELF-ASSESSED); `make auditkit-report` renders the
# deliverable; `make auditkit-acceptance` is the gate: eight
# sections over the real CLI, the real scope, the real checker and
# the negative space (off-roster filings, no-reproducer findings,
# reasons-less triages, broken chains, tampered ledgers, the dirty
# window, the scope that moved).
auditkit-brief:
	@$(PYTHON) tools/hls-auditkit.py brief --out AUDITBRIEF.md

auditkit-evidence:
	@$(PYTHON) tools/hls-auditkit.py evidence

auditkit-report:
	@$(PYTHON) tools/hls-auditkit.py report --out AUDITREPORT.md

auditkit-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 140 acceptance] running tests/auditkit_acceptance.py..."
	@$(PYTHON) tests/auditkit_acceptance.py
	@echo "ACCEPTANCE OK: Stage 140 -- independent security audit (third-party, paid)"


# Stage 141 (v0.159.0-alpha): the API freeze — syntax + stdlib locked.
# tools/hls-freeze.py derives the language's WHOLE public surface LIVE
# from the real front-ends (the boot lexer/parser tables, the checker's
# own builtin tables, the self-hosted keyword chain grepped from
# src/hlc/types.hls, the attribute vocabulary read out of the parser's
# own census, the container/concurrency methods demonstrated through
# the real checker, every std//core/ module parsed) and freezes it
# content-addressed into tests/api_freeze.json — committed, because
# the frozen API is exactly the part that travels with the tree.
# `make freeze` prints the quick verdict; `make freeze-verify` is the
# anti-rot pass: the live surface must equal the frozen one (REMOVED /
# ADDED / CHANGED, each named at its dotted path — post-freeze
# additions are drift too: the API is locked, not merely
# backwards-compatible), the digest must agree with the document's own
# surface, and the amendment chain (tests/api_freeze_amendments.jsonl,
# the family's one arithmetic since Stage 13) must replay clean.
# The surface moves ONLY through `tools/hls-freeze.py amend --reason
# --by` — a ledger entry with the exact delta, not an edit.
# `make freeze-report` renders the frozen API document (API.md,
# deterministic per freeze, never rendered from a drifted tree);
# `make freeze-acceptance` is the gate: eight sections over the real
# freeze and the negative space (an independent re-derivation of the
# whole stdlib census by the gate's own parser walk; the two-front-end
# keyword agreement; removal/mutation/addition drifts named at their
# paths; the amend law through the family's imported arithmetic; the
# tampered chain and the hand-edited document both named; the report's
# determinism and its verify-first refusal; and the corpus showcase —
# the decision procedure pinned in pure HLS, byte-identical on the
# interpreter and natively).
freeze:
	@$(PYTHON) tools/hls-freeze.py status

freeze-verify:
	@$(PYTHON) tools/hls-freeze.py verify

freeze-report:
	@$(PYTHON) tools/hls-freeze.py report --out API.md

freeze-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 141 acceptance] running tests/freeze_acceptance.py..."
	@$(PYTHON) tests/freeze_acceptance.py
	@echo "ACCEPTANCE OK: Stage 141 -- API freeze (syntax + stdlib locked)"

# Stage 142 (v0.161.0-alpha): the pure-bootstrap acceptance gate.
# Self-contained: it builds its own compilers on disposable copies of
# the tree under a python-free PATH, so it takes no bin/hlc prerequisite.
purebootstrap-acceptance:
	@echo "[Stage 142 acceptance] running tests/purebootstrap_acceptance.py..."
	@$(PYTHON) tests/purebootstrap_acceptance.py
	@echo "ACCEPTANCE OK: Stage 142 -- pure-HLS bootstrap (no Python in the build)"


# Stage 144 (v0.165.0-alpha): the FINAL independent third-party
# security audit — the engagement that gates the v1.0 release
# candidates. Stage 140 built the engagement kit; this is the audit
# that cannot be outlived: the signature pins THREE anchors at once
# (the Stage 135 guarantee scope, the Stage 141 API freeze with its
# amendment chain head, the Stage 142/143 snapshot manifest), every
# evidence pass re-derives them live and records the drift as a
# fact, and the signoff refuses to close on any anchor that moved
# — a moved surface is a new engagement, not an amendment. Closure
# requires an INDEPENDENT pass (the audit does not close on the
# project's own evidence), the latest pass green, and every
# acceptance's remediation commit LANDED: an ancestor of HEAD, and
# not older than the finding it fixes. The signoff seals the ledger
# (.hlfa/final.jsonl, machine-local like every ledger in this tree,
# chained with the family's one arithmetic) — after the seal nothing
# appends. `gate` is what the release process runs before cutting a
# candidate: sealed tree an ancestor of the current HEAD (the
# candidate is cut from the audited tree or its descendants, never
# around the audit), anchors still pinned, fixes still landed —
# anything else exits 1 with the named refusal.
finalaudit-brief:
	@$(PYTHON) tools/hls-finalaudit.py brief --out FINALBRIEF.md

finalaudit-evidence:
	@$(PYTHON) tools/hls-finalaudit.py evidence

finalaudit-report:
	@$(PYTHON) tools/hls-finalaudit.py report --out FINALREPORT.md

finalaudit-gate:
	@$(PYTHON) tools/hls-finalaudit.py gate

finalaudit-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 144 acceptance] running tests/finalaudit_acceptance.py..."
	@$(PYTHON) tests/finalaudit_acceptance.py
	@echo "ACCEPTANCE OK: Stage 144 -- independent third-party security audit (final)"


# Stage 130 (v0.148.0-alpha): the deprecation gate. Eight sections
# over BOTH real front-ends, the real linter and the real formatter:
# the convention (the three refusal fixtures fail boot AND hlc with
# the same core message; L013 carries no [fixable] tag), the parse
# laws (bare/message/absent forms, escapes decoded with the lexer's
# own rules, duplicates refused, externs and let-bindings refuse it,
# the unknown-attribute list names deprecated), the self-hosted
# parity (hlc compiles the showcase; the emitted C carries NO
# deprecation machinery — zero codegen; the self-compile fixed point
# holds), the same-file lint (both forms at their lines, the live
# replacements silent, a deprecated fn's own body silent, --rule and
# --strict), the cross-module lint (a notice in an imported module
# warns in the importer; the ambiguous-method silence; the
# unambiguous `method` qualifier), the honesty gates (--fix leaves a
# deprecated-call file byte-identical; hlfmt preserves both forms
# byte for byte; a broken crate degrades to the same-file registry),
# the differential (the ok fixture green on BOTH backends byte for
# byte, its five-warning L013 census pinned exactly) and the corpus
# hygiene (fmt/canonical, checker green, the demo's two warnings, no
# litter). The gate needs bin/hlc + gcc for the native legs, so the
# native build is a prerequisite.
deprecated-acceptance: $(BIN)/hlfmt bin/hlc
	@echo "[Stage 130 acceptance] running tests/deprecated_acceptance.py..."
	@$(PYTHON) tests/deprecated_acceptance.py
	@echo "ACCEPTANCE OK: Stage 130 -- deprecation mechanism (@deprecated attribute + lint)"



# ---------------------------------------------------------------------------
# Section targets live in mk/ (included in original section order,
# so this Makefile parses exactly like the former single-file version).
include mk/20-testing.mk mk/30-lto.mk mk/40-backends.mk mk/50-pkg-contracts.mk mk/60-advanced.mk mk/70-stdlib-core.mk mk/80-stdlib.mk mk/90-stdlib-late.mk mk/95-osdev.mk
