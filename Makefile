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

.PHONY: all stage0 bootstrap bootstrap-nolibc xbootstrap-acceptance refine-acceptance test examples clean run check bench install uninstall audit opt-stats emit-ir emit-llvm fmt lint lsp-check pkg-init pkg-add pkg-lock pkg-audit pkg-verify pkg-build pkg-publish pkg-log pkg-log-verify prove prove-full infer infer-smt model prove-acceptance invariant-acceptance cvc5 cvc5-acceptance shapes shapes-smt shapes-acceptance sidechannel sidechannel-acceptance ct ct-acceptance supply supply-pkg supply-acceptance sbom sbom-pkg sbom-release sbom-acceptance repro repro-pkg repro-release repro-verify repro-acceptance sign-keygen sign sign-verify sign-release sign-verify-release sign-selftest sign-acceptance tlog-verify tlog tlog-gossip tlog-witness tlog-witness-verify tlog-prove tlog-prove-verify tlog-selftest tlog-acceptance reverify reverify-json reverify-lto reverify-report reverify-selftest reverify-acceptance ffi-taint ffi-taint-acceptance sandbox sandbox-run sandbox-emit sandbox-release sandbox-verify-release sandbox-selftest sandbox-acceptance hltest fuzz cov fuzz-acceptance wasm-opt webapp webapp-acceptance serve serve-acceptance wasm-pack wasm-pack-check wasm-pack-pack wasm-pack-acceptance aarch64-bench aarch64-acceptance aarch64-list-targets stack-acceptance inline-acceptance opt-stats-report kernel-attrs escape-acceptance layout-report tail-acceptance tail-report asm-acceptance asm-attrs bench-stdlib spec-check stage32-acceptance async-acceptance stream-acceptance io-acceptance fs-acceptance net-acceptance http-acceptance http2-acceptance json-stream-acceptance regex-acceptance fmt-acceptance hash-acceptance collections-acceptance sync-acceptance thread-acceptance time-acceptance math-acceptance process-acceptance env-acceptance archive-acceptance uuid-ulid-acceptance cli-acceptance tui-acceptance color-acceptance progress-acceptance log-acceptance http-server-acceptance websocket-acceptance cookie-acceptance session-acceptance csrf-acceptance template-acceptance sse-acceptance graphql-acceptance openapi-acceptance jsffi-acceptance dom-acceptance freestanding freestanding-check freestanding-acceptance nostd nostd-acceptance alloc-acceptance mem-acceptance panic-acceptance stackguard-acceptance asmreg-acceptance link-acceptance boot-acceptance idt-acceptance mmio-acceptance port-acceptance dma-acceptance atomics-acceptance irqsafe-acceptance triple triple-x86_64-acceptance triple-aarch64-acceptance triple-riscv64-acceptance debuginfo-acceptance cap cap-acceptance test-linker auditlog-list auditlog-show auditlog-verify auditlog-sign auditlog-verify-sign auditlog-selftest auditlog-acceptance lsp-def-acceptance lsp-hints-acceptance lsp-refactor-acceptance fmt-comments-acceptance fmt-config-acceptance lint-fix-acceptance snapshot-acceptance cases-acceptance dap-acceptance repl-acceptance bench-acceptance rss-acceptance rt-acceptance det-acceptance compat-acceptance migrate-acceptance deprecated-acceptance semver-acceptance lts-acceptance xdiff xdiff-acceptance fuzzseed fuzzseed-acceptance guarantee guarantee-report guarantee-acceptance perf perf-gate perf-dashboard perf-acceptance mem mem-gate mem-dashboard memdash-acceptance

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
lsp-refactor-acceptance:
	@echo "[Stage 115 acceptance] running tests/lsp_refactor_acceptance.py..."
	@$(PYTHON) tests/lsp_refactor_acceptance.py

# Stage 116 acceptance gate: the eight-section battery for hlfmt's
# comment preservation (the sigils, the duplication law, the
# positions, the re-indent law, the preservation law, the one-liners,
# the differential, the corpus). The differential half compiles the
# stage's ok fixture on bin/hlc and gcc, so the native build is a
# prerequisite.
fmt-comments-acceptance: bin/hlc
	@echo "[Stage 116 acceptance] running tests/fmt_comments_acceptance.py..."
	@$(PYTHON) tests/fmt_comments_acceptance.py

# Stage 117 acceptance gate: the eight-section battery for hlfmt's team
# configuration file (.hlfmt.toml): the discovery walk (nearest file
# wins, --config pins, --no-config ignores), the five knobs, the
# strict grammar (a typo is exit 2, never a style), --print-config,
# the per-config fixed points, the default law, the interpreter-vs-
# native differential under a non-default config, and the corpus. The
# differential half compiles the stage's ok fixture on bin/hlc and
# gcc, so the native build is a prerequisite.
fmt-config-acceptance: bin/hlc
	@echo "[Stage 117 acceptance] running tests/fmt_config_acceptance.py..."
	@$(PYTHON) tests/fmt_config_acceptance.py

# Stage 118 acceptance gate: the eight-section battery for hllint's
# autofix mode (the scope renames, the discard wraps, the signature
# cleanups, the deletions, the safety net, the fixed point, the
# interpreter-vs-native differential before and after the fix, and the
# corpus). The differential half compiles the stage's ok fixture on
# bin/hlc and gcc, so the native build is a prerequisite.
lint-fix-acceptance: bin/hlc
	@echo "[Stage 118 acceptance] running tests/lint_fix_acceptance.py..."
	@$(PYTHON) tests/lint_fix_acceptance.py

# Stage 119 (v0.138.0-alpha): snapshot-testing acceptance gate.
# The differential half compiles the stage's ok fixture on
# bin/hlc and gcc, so the native build is a prerequisite.
snapshot-acceptance: bin/hlc
	@echo "[Stage 119 acceptance] running tests/snapshot_acceptance.py..."
	@$(PYTHON) tests/snapshot_acceptance.py

# Stage 120 (v0.139.0-alpha): parameterised (table-driven) testing
# acceptance gate. The differential half compiles the stage's ok
# fixture on bin/hlc and gcc, so the native build is a prerequisite.
cases-acceptance: bin/hlc
	@echo "[Stage 120 acceptance] running tests/cases_acceptance.py..."
	@$(PYTHON) tests/cases_acceptance.py

# Stage 121 (v0.140.0-alpha): VS Code extension debugger (DAP)
# acceptance gate. Nine sections over the REAL adapter subprocess:
# the protocol lifecycle, breakpoint verification and snapping, the
# stack, stepping, the variables view, console evaluation, panic
# stops, protocol robustness, and the editor's debug contributions.
dap-acceptance:
	@echo "[Stage 121 acceptance] running tests/dap_acceptance.py..."
	@$(PYTHON) tests/dap_acceptance.py

# Stage 123 (v0.141.0-alpha): the interactive REPL acceptance gate.
# Eight sections over the REAL repl subprocess (piped stdin): the
# session-as-a-program model, definitions at the prompt, the no-replay
# guarantee, :type, :effects, the session commands (:audit/:env/:load/
# :reset), panics and refusals, and the corpus + transcript parity
# (the canonical session's `=` lines are the fixture program's output,
# natively byte-identical when bin/hlc + gcc exist).
repl-acceptance:
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
bench-acceptance: bin/hlc
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
rss-acceptance: bin/hlc
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
rt-acceptance: bin/hlc
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
det-acceptance: bin/hlc
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
compat-acceptance: bin/hlc
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
migrate-acceptance: bin/hlc
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
semver-acceptance:
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
lts-acceptance:
	@echo "[Stage 132 acceptance] running tests/lts_acceptance.py..."
	@$(PYTHON) tests/lts_acceptance.py
	@echo "ACCEPTANCE OK: Stage 132 -- LTS branch policy (backport-only after v1.0)"

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

xdiff-acceptance: bin/hlc
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

guarantee-acceptance: bin/hlc
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

perf-acceptance: bin/hlc
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

memdash-acceptance: bin/hlc
	@echo "[Stage 137 acceptance] running tests/memdash_acceptance.py..."
	@$(PYTHON) tests/memdash_acceptance.py
	@echo "ACCEPTANCE OK: Stage 137 -- memory regression dashboard (RSS over time)"

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
deprecated-acceptance: bin/hlc
	@echo "[Stage 130 acceptance] running tests/deprecated_acceptance.py..."
	@$(PYTHON) tests/deprecated_acceptance.py
	@echo "ACCEPTANCE OK: Stage 130 -- deprecation mechanism (@deprecated attribute + lint)"



# ---------------------------------------------------------------------------
# Section targets live in mk/ (included in original section order,
# so this Makefile parses exactly like the former single-file version).
include mk/20-testing.mk mk/30-lto.mk mk/40-backends.mk mk/50-pkg-contracts.mk mk/60-advanced.mk mk/70-stdlib-core.mk mk/80-stdlib.mk mk/90-stdlib-late.mk mk/95-osdev.mk
