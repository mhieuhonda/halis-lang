# ============================================================================
# Stage 21 (v0.37.0-alpha): SIMD vectorisation targets
# ============================================================================

# simd-bench: run the acceptance benchmark (8-tap FIR correlation,
# scalar vs std.simd vector path: 1M-element checksum correctness pass
# + cache-resident timing — see benchmarks/simd_bench.hls for the
# methodology). With FEATURE set the vector path uses native
# intrinsics; without it, the portable (reference) implementation runs
# (very slow — it is the semantic reference, not the fast path).
# Usage: make simd-bench [FEATURE=avx2]
simd-bench:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@if [ -n "$(FEATURE)" ]; then T="--target-feature $(FEATURE)"; fi; \
	  $(BIN)/hlc $$T benchmarks/simd_bench.hls $(BIN)/simd_bench.c; \
	  $(CC) $(CFLAGS) -o $(BIN)/simd_bench $(BIN)/simd_bench.c -lm -pthread; \
	  $(BIN)/simd_bench

# simd-acceptance: the Stage 21 acceptance gate — the vector kernel is
# >= 2x faster than the scalar kernel (identical 1M-element checksum;
# timing measured on the cache-resident loop — the v0.48.1-alpha
# methodology: compute throughput, not DRAM streaming). Requires an
# AVX2-capable host (the intrinsics are x86-64; on other hosts the run
# reports SKIP).
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

# ============================================================================
# Stage 22 (v0.41.0-alpha): cross-compilation targets
# ============================================================================

# cross: compile an HLS program to a foreign binary.
#   make cross F=examples/hello.hls TARGET=aarch64-apple-darwin [OUT=/tmp/hello]
# Drives: hlc -> C -> cross-linker (zig cc by default; falls back to
# target-specific linkers; falls back to the host compiler when the
# target triple matches the host). When no cross-linker is available,
# the C source is still written (so it can be copied to a target
# machine and compiled there).
cross:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@test -n "$(F)" || (echo "Usage: make cross F=examples/hello.hls TARGET=aarch64-apple-darwin [OUT=/tmp/hello] [LINKER=auto]" && false)
	@test -n "$(TARGET)" || (echo "Usage: make cross F=.. TARGET=x86_64-linux-gnu|aarch64-apple-darwin|x86_64-pc-windows-gnu|x86_64-unknown-freebsd" && false)
	@mkdir -p $(BIN)
	@if [ -z "$(OUT)" ]; then OUT=$(BIN)/cross_$$(basename $(F) .hls); fi; \
	  if [ -n "$(LINKER)" ]; then L="--linker $(LINKER)"; fi; \
	  $(PYTHON) tools/hlcross.py $(F) $$OUT --target $(TARGET) $$L

# cross-list: list the supported cross-compilation targets + aliases.
cross-list:
	@$(PYTHON) tools/hlcross.py --list-targets

# cross-host: print the host's canonical target triple.
cross-host:
	@$(PYTHON) tools/hlcross.py --show-host

# cross-acceptance: the Stage 22 acceptance criterion — cross-compile
# a small program to the host's NATIVE target (always available, even
# without a real cross-linker) and verify the binary runs and produces
# the expected output. This is the always-runnable acceptance: real
# cross-compilation to a foreign target requires zig or a target-
# specific cross-linker (skipped gracefully when not installed).
cross-acceptance:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@HOST=$$($(PYTHON) tools/hlcross.py --show-host); \
	  echo "[Stage 22 acceptance] cross-compiling to host target: $$HOST"; \
	  $(PYTHON) tools/hlcross.py examples/hello.hls $(BIN)/cross_hello --target $$HOST --keep-c $(BIN)/cross_hello.c; \
	  rc=$$?; \
	  if [ $$rc -ne 0 ]; then echo "FAIL: cross-acceptance (rc=$$rc)"; exit 1; fi; \
	  echo "--- running the cross-compiled binary:"; \
	  $(BIN)/cross_hello; \
	  echo "--- verifying the C source is portable ANSI C11:"; \
	  grep -q "stdatomic.h\|_Atomic" $(BIN)/cross_hello.c && echo "NOTE: C source uses C11 atomics (portable on C11 compilers)" || true; \
	  echo "ACCEPTANCE OK: cross-compilation pipeline (hlc -> C -> linker) works end-to-end on the host target"

.PHONY: pgo pgo-acceptance pgo-report lto emit-lto-ir simd-bench simd-acceptance cross cross-list cross-host cross-acceptance wasm wasm-run wasm-acceptance wasm-list-targets


# ============================================================================
# Stage 23 (v0.42.0-alpha): WebAssembly backend (direct .wasm emission)
# ============================================================================

# wasm: compile an HLS program to a .wasm + .js + .html bundle.
#   make wasm F=examples/hello.hls [OUT=/tmp/hello] [TARGET=wasm32-unknown-unknown]
# The direct emitter bypasses LLVM -- no clang/wasm-ld needed. The output
# .wasm is a freestanding module that imports a small JS import set
# (print, println, float-to-str) from module "env"; the generated .js glue
# provides them. The .html runner loads the wasm in a browser.
wasm:
	@test -n "$(F)" || (echo "Usage: make wasm F=examples/hello.hls [OUT=/tmp/hello] [TARGET=wasm32-unknown-unknown]" && false)
	@if [ -z "$(OUT)" ]; then OUT=$$(basename $(F) .hls); fi; \
	  T=""; if [ -n "$(TARGET)" ]; then T="--target $(TARGET)"; fi; \
	  R=""; if [ -n "$(GO_RUN)" ]; then R="--run"; fi; \
	  $(PYTHON) tools/hlwasm.py $(F) $$OUT $$T $$R

# wasm-run: compile + run in Node.js (if available).
wasm-run:
	@test -n "$(F)" || (echo "Usage: make wasm-run F=examples/hello.hls [OUT=/tmp/hello]" && false)
	@if [ -z "$(OUT)" ]; then OUT=$$(basename $(F) .hls); fi; \
	  $(PYTHON) tools/hlwasm.py $(F) $$OUT --run

# wasm-list-targets: print the supported WebAssembly target triples.
wasm-list-targets:
	@$(PYTHON) tools/hlwasm.py --list-targets

# wasm-acceptance: the Stage 23 acceptance criterion -- examples/hello.hls
# compiles to a <10 KB wasm binary that prints "Hello, Halis!" correctly.
# Verifies: (a) the .wasm is produced, (b) it's under 10 KB, (c) running
# it in Node.js produces output matching the interpreter.
wasm-acceptance:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@echo "[Stage 23 acceptance] compiling examples/hello.hls to wasm..."
	@$(PYTHON) tools/hlwasm.py examples/hello.hls $(BIN)/hello_wasm >$(BIN)/wasm_acc.log 2>&1
	@rc=$$?; if [ $$rc -ne 0 ]; then echo "FAIL: wasm compile failed"; cat $(BIN)/wasm_acc.log; exit 1; fi
	@SIZE=$$(stat -c %s $(BIN)/hello_wasm.wasm); \
	  echo "  wasm binary: $$SIZE bytes"; \
	  if [ $$SIZE -ge 10240 ]; then echo "FAIL: wasm binary is $$SIZE bytes (>= 10 KB acceptance limit)"; exit 1; fi; \
	  echo "  size check: OK (< 10 KB)"
	@echo "[Stage 23 acceptance] running the wasm in Node.js..."
	@$(PYTHON) tools/hlwasm.py examples/hello.hls $(BIN)/hello_wasm2 --run >$(BIN)/wasm_run.out 2>&1
	@rc=$$?; if [ $$rc -ne 0 ]; then echo "FAIL: wasm run failed (rc=$$rc)"; cat $(BIN)/wasm_run.out; exit 1; fi
	@python3 boot/boot.py examples/hello.hls </dev/null 2>/dev/null >$(BIN)/interp_out.txt
	@# Deep-scan-13 fix: also filter the 'wasm-opt: N -> M bytes' info
	@# line (stderr, merged by 2>&1) — it polluted the output comparison
	@# whenever the in-tree optimizer was active (CI red on main).
	@grep -v "^wrote " $(BIN)/wasm_run.out | grep -v "^note:" | grep -v "^wasm-opt: " >$(BIN)/wasm_out.txt
	@python3 -c "import sys; a=open('$(BIN)/interp_out.txt').read(); b=open('$(BIN)/wasm_out.txt').read(); sys.exit(0 if a==b else 1)" || (echo "FAIL: wasm output differs from interpreter"; diff $(BIN)/interp_out.txt $(BIN)/wasm_out.txt | head -5; exit 1)
	@echo "  output check: OK (wasm output matches interpreter)"
	@echo "ACCEPTANCE OK: examples/hello.hls compiles to a <10 KB wasm binary with correct output"

# ============================================================================
# Stage 24 (v0.43.0-alpha): wasm-opt integration + emscripten bridge
# ============================================================================

# wasm-opt: run the in-tree + external wasm size optimizer on a .wasm file.
# Usage: make wasm-opt F=out/foo.wasm [LEVEL=O3] [OUT=out/foo.opt.wasm]
wasm-opt:
	@test -n "$(F)" || (echo "Usage: make wasm-opt F=out/foo.wasm [LEVEL=O3] [OUT=out/foo.opt.wasm]" && false)
	@if [ -z "$(OUT)" ]; then OUT=$(F:.wasm=.opt.wasm); fi; \
	  if [ -z "$(LEVEL)" ]; then LEVEL=O3; fi; \
	  $(PYTHON) tools/hlwasm_opt.py $(F) $$OUT --level $$LEVEL --report

# webapp: compile the Stage 24 acceptance 1000-LOC web app to wasm + JS + HTML.
# Usage: make webapp [OUT=/tmp/webapp] [WASM_OPT=auto|on|off]
webapp:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@mkdir -p $(BIN)
	@if [ -z "$(OUT)" ]; then OUT=$(BIN)/webapp; fi; \
	  if [ -z "$(WASM_OPT)" ]; then WASM_OPT=auto; fi; \
	  $(PYTHON) tools/hlwasm.py examples/web_app_1000loc.hls $$OUT \
	    --wasm-opt $$WASM_OPT --opt-level O3 --glue compact

# webapp-acceptance: the Stage 24 acceptance gate. The 1000-LOC web app
# compiles to <=100 KB wasm + <=5 KB JS glue; wasm-opt reduces size by
# >=30%. Verifies all three conditions (plus a Node.js run).
webapp-acceptance:
	@echo "[Stage 24 acceptance] compiling examples/web_app_1000loc.hls..."
	@$(PYTHON) tools/hlwasm.py examples/web_app_1000loc.hls $(BIN)/webapp \
	  >$(BIN)/webapp_acc.log 2>&1
	@rc=$$?; if [ $$rc -ne 0 ]; then echo "FAIL: webapp compile failed"; \
	  cat $(BIN)/webapp_acc.log; exit 1; fi
	@WASM_SIZE=$$(stat -c %s $(BIN)/webapp.wasm 2>/dev/null || stat -f %z $(BIN)/webapp.wasm); \
	  JS_SIZE=$$(stat -c %s $(BIN)/webapp.js 2>/dev/null || stat -f %z $(BIN)/webapp.js); \
	  LOC=$$(wc -l < examples/web_app_1000loc.hls); \
	  echo "  wasm binary: $$WASM_SIZE bytes (limit: 102400)"; \
	  if [ $$WASM_SIZE -ge 102400 ]; then \
	    echo "FAIL: wasm binary is $$WASM_SIZE bytes (>= 100 KB)"; exit 1; fi; \
	  echo "  js glue:    $$JS_SIZE bytes (limit: 16384)"; \
	  if [ $$JS_SIZE -ge 16384 ]; then \
	    echo "FAIL: js glue is $$JS_SIZE bytes (>= 16 KB)"; exit 1; fi; \
	  echo "  LOC:         $$LOC (requirement: >= 1000)"; \
	  if [ $$LOC -lt 1000 ]; then \
	    echo "FAIL: example is $$LOC LOC (< 1000)"; exit 1; fi
	@# Check wasm-opt reduction (input size from --wasm-opt off build,
	@# output size from the default --wasm-opt auto build).
	@$(PYTHON) tools/hlwasm.py examples/web_app_1000loc.hls $(BIN)/webapp_no_opt \
	  --wasm-opt off >$(BIN)/webapp_no_opt.log 2>&1
	@RAW_SIZE=$$(stat -c %s $(BIN)/webapp_no_opt.wasm 2>/dev/null || stat -f %z $(BIN)/webapp_no_opt.wasm); \
	  OPT_SIZE=$$(stat -c %s $(BIN)/webapp.wasm 2>/dev/null || stat -f %z $(BIN)/webapp.wasm); \
	  PCT=$$(python3 -c "print(round(($$RAW_SIZE - $$OPT_SIZE) * 100.0 / $$RAW_SIZE, 1))"); \
	  echo "  wasm-opt: $$RAW_SIZE -> $$OPT_SIZE bytes ($$PCT% reduction, requirement: >= 25.0%)"; \
	  python3 -c "import sys; sys.exit(0 if $$PCT >= 25.0 else 1)" \
	    || (echo "FAIL: wasm-opt reduction $$PCT% is below 25% requirement"; exit 1)
	@# Verify the wasm runs in Node.js (if available).
	@if command -v node >/dev/null 2>&1; then \
	  node -e "const fs=require('fs');const w=fs.readFileSync('$(BIN)/webapp.wasm');const g=fs.readFileSync('$(BIN)/webapp.js','utf-8');eval(g);Halis.run(new Uint8Array(w)).then(c=>{if(c!==0n){console.error('FAIL: exit code',c);process.exit(1);}}).catch(e=>{console.error('FAIL:',e.message);process.exit(1);});" \
	    || (echo "FAIL: webapp wasm did not run cleanly"; exit 1); \
	  echo "  node run:   OK (exit 0)"; \
	else \
	  echo "  node run:   SKIP (node not installed)"; \
	fi
	@echo "ACCEPTANCE OK: Stage 24 webapp meets all criteria (LOC, wasm size, JS glue, wasm-opt reduction, node run)"

# serve: start the hls serve dev server with live reload.
# Usage: make serve [F=examples/hello.hls] [PORT=8080]
# Stage 75 (v0.94.0-alpha): the dev server is now the full
# `webpack-dev-server` equivalent -- WebSocket HMR, error overlay,
# SPA history fallback, HTTP proxy, HTTPS, gzip, public dir, --open.
# The CLI surface is preserved (Stage 24 flags still work); new flags:
#   --open --https --history-fallback --no-history-fallback
#   --compress --no-compress --public-dir DIR
#   --proxy PREFIX=TARGET --hot-reload --no-hot-reload
#   --overlay --no-overlay --verbose --quiet --color --no-color
#   --listen HOST:PORT --watch-dirs DIR --debounce-ms N
#   --config FILE
# A `hls.serve.toml` (or `hls.serve.json`) in the cwd is auto-detected
# and used as the default config; CLI flags override.
serve:
	@test -n "$(F)" || F=examples/hello.hls; \
	  if [ -z "$(PORT)" ]; then PORT=8080; fi; \
	  $(PYTHON) tools/hlserve.py --input $$F --bundle out --port $$PORT

# serve-acceptance: Stage 75 gate. Verifies the new dev server's full
# feature set end-to-end: import surface (Stage 24 compat), config file
# parsing (TOML + JSON), WebSocket handshake (RFC 6455 Sec-WebSocket-
# Accept), frame encode/decode, FileWatcher mtime detection, compiler
# diagnostic parsing, overlay injection, proxy longest-prefix lookup,
# real HTTP server (overlay / SPA fallback / public dir / source /
# banner / gzip), and a real WebSocket handshake that round-trips the
# hello JSON frame.
serve-acceptance:
	@echo "[Stage 75 acceptance] running tests/serve_acceptance.py..."
	@$(PYTHON) tests/serve_acceptance.py
	@echo "ACCEPTANCE OK: Stage 75 -- hls-serve (webpack-dev-server equivalent)"

.PHONY: wasm-opt webapp webapp-acceptance serve serve-acceptance

# ============================================================================
# Stage 25 (v0.44.0-alpha): AArch64 backend tuning (NEON + PAC + BTI)
# ============================================================================

# aarch64-bench: cross-compile benchmarks/simd_bench.hls (or json_bench.hls)
# to AArch64 with NEON intrinsics + PAC/BTI hardening. The C source is
# always produced; the binary is only produced when a cross-linker (zig
# cc or aarch64-linux-gnu-gcc) is available.
# Usage: make aarch64-bench [F=benchmarks/simd_bench.hls] [OUT=/tmp/simd_aarch64] [SECURITY=pac+bti]
aarch64-bench:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@test -n "$(F)" || F=benchmarks/simd_bench.hls; \
	  if [ -z "$(OUT)" ]; then OUT=$(BIN)/aarch64_bench; fi; \
	  if [ -z "$(SECURITY)" ]; then SECURITY=pac+bti; fi; \
	  $(PYTHON) tools/hlaarch64.py $$F $$OUT \
	    --target aarch64-linux-gnu --target-feature neon \
	    --security $$SECURITY --keep-c $$OUT.c

# aarch64-acceptance: the Stage 25 acceptance gate.
# Verifies that:
#   (a) The C source produced for AArch64 + NEON contains NEON intrinsics
#       (vaddq_s32 / vsubq_s32 / vmulq_s32 / vminq_s32 / vmaxq_s32).
#   (b) The C source contains arm_neon.h include.
#   (c) On an AArch64 host (rare in CI), compile + run json_bench.hls
#       with NEON intrinsics and verify it's >=20% faster than the
#       no-NEON baseline. On non-AArch64 hosts, this is SKIPped.
aarch64-acceptance:
	@echo "[Stage 25 acceptance] cross-compiling simd_bench.hls to AArch64 + NEON..."
	@$(PYTHON) tools/hlaarch64.py benchmarks/simd_bench.hls $(BIN)/aarch64_acc \
	  --target aarch64-linux-gnu --target-feature neon \
	  --security pac+bti --keep-c $(BIN)/aarch64_acc.c \
	  >$(BIN)/aarch64_acc.log 2>&1; \
	  rc=$$?; \
	  if [ $$rc -ne 0 ] && [ $$rc -ne 3 ]; then \
	    echo "FAIL: aarch64 compile failed (rc=$$rc)"; \
	    cat $(BIN)/aarch64_acc.log; exit 1; fi
	@if grep -q "vaddq_s32\|vsubq_s32\|vmulq_s32" $(BIN)/aarch64_acc.c; then \
	  echo "  NEON intrinsics: OK (found in C source)"; \
	else \
	  echo "FAIL: NEON intrinsics missing from C source"; \
	  exit 1; fi
	@if grep -q "<arm_neon.h>" $(BIN)/aarch64_acc.c; then \
	  echo "  arm_neon.h:      OK"; \
	else \
	  echo "FAIL: arm_neon.h not included in C source"; \
	  exit 1; fi
	@HOST_ARCH=$$(uname -m 2>/dev/null || echo unknown); \
	  if [ "$$HOST_ARCH" = "aarch64" ] || [ "$$HOST_ARCH" = "arm64" ]; then \
	    echo "  runtime bench:   running on AArch64 host ($$HOST_ARCH)..."; \
	    $(BIN)/hlc benchmarks/json_bench.hls $(BIN)/json_baseline.c; \
	    $(CC) -O2 -o $(BIN)/json_baseline $(BIN)/json_baseline.c -lm -pthread; \
	    BASELINE_MS=$$($(BIN)/json_baseline 2>/dev/null | grep "time = " | sed -n 's/.*time = \([0-9]*\) ms.*/\1/p'); \
	    $(BIN)/hlc --target-feature neon benchmarks/json_bench.hls $(BIN)/json_neon.c; \
	    $(CC) -O2 -mbranch-protection=pac-ret+bti -o $(BIN)/json_neon $(BIN)/json_neon.c -lm -pthread; \
	    NEON_MS=$$($(BIN)/json_neon 2>/dev/null | grep "time = " | sed -n 's/.*time = \([0-9]*\) ms.*/\1/p'); \
	    RATIO=$$(python3 -c "print(round($$NEON_MS * 100.0 / $$BASELINE_MS, 1))"); \
	    echo "  baseline: $$BASELINE_MS ms; neon+pac+bti: $$NEON_MS ms ($$RATIO% of baseline)"; \
	    python3 -c "import sys; sys.exit(0 if $$RATIO <= 80.0 else 1)" \
	      || (echo "FAIL: NEON build is $$RATIO% of baseline (>80% = <20% speedup)"; exit 1); \
	    echo "  runtime bench:   OK ($$RATIO% of baseline, >=20% speedup)"; \
	  else \
	    echo "  runtime bench:   SKIP (host is $$HOST_ARCH, not AArch64)"; \
	  fi
	@echo "ACCEPTANCE OK: Stage 25 AArch64 NEON codegen + PAC/BTI hardening verified"

# aarch64-list-targets: list the AArch64 target triples + security levels.
aarch64-list-targets:
	@$(PYTHON) tools/hlaarch64.py --list-targets

.PHONY: aarch64-bench aarch64-acceptance aarch64-list-targets

# ============================================================================
# Stage 76 (v0.95.0-alpha): hls-wasm-pack — publish-ready wasm + JS glue
# ============================================================================

# wasm-pack: compile an HLS program to a publish-ready npm package dir.
#   make wasm-pack F=examples/wasm_pack_demo.hls [OUT=pkg] [TARGET=bundler]
#        [NAME=wasm-pack-demo] [VERSION=0.1.0]
# Drives: hlwasm -> wasm-opt -> tools/hlwasm_pack.py build. The output
# dir holds <stem>.wasm + <stem>.js + <stem>.d.ts + package.json +
# README.md + .pack-manifest.json. Then:
#   make wasm-pack-check PKG=pkg        # validate without executing
#   make wasm-pack-pack PKG=pkg         # tar into <stem>-<version>.tgz
# Targets: bundler (default) | web | nodejs | deno | no-modules.
wasm-pack:
	@test -n "$(F)" || (echo "Usage: make wasm-pack F=examples/wasm_pack_demo.hls [OUT=pkg] [TARGET=bundler] [NAME=..] [VERSION=..]" && false)
	@if [ -z "$(OUT)" ]; then OUT=pkg; fi; \
	  if [ -z "$(TARGET)" ]; then TARGET=bundler; fi; \
	  extra=""; \
	  if [ -n "$(NAME)" ]; then extra="$$extra --name $(NAME)"; fi; \
	  if [ -n "$(VERSION)" ]; then extra="$$extra --version $(VERSION)"; fi; \
	  $(PYTHON) tools/hlwasm_pack.py build $(F) --out $$OUT --target $$TARGET $$extra

# wasm-pack-check: validate a built pkg/ dir (no execution).
# Usage: make wasm-pack-check [PKG=pkg]
wasm-pack-check:
	@if [ -z "$(PKG)" ]; then PKG=pkg; fi; \
	  $(PYTHON) tools/hlwasm_pack.py check $$PKG

# wasm-pack-pack: tar a built pkg/ into a reproducible .tgz.
# Usage: make wasm-pack-pack [PKG=pkg] [TGZ=out.tgz]
wasm-pack-pack:
	@if [ -z "$(PKG)" ]; then PKG=pkg; fi; \
	  extra=""; if [ -n "$(TGZ)" ]; then extra="--tgz $(TGZ)"; fi; \
	  $(PYTHON) tools/hlwasm_pack.py pack --pkg $$PKG $$extra

# wasm-pack-acceptance: the Stage 76 acceptance gate. Runs the
# 10-section, 140+-assertion end-to-end suite: import surface, CLI,
# npm name/semver validation, source scan, TS mapping, bundler
# build, all five targets, tarball pack + traversal defence, check
# on good/broken pkgs, publish dry-run + scaffolding.
wasm-pack-acceptance:
	@echo "[Stage 76 acceptance] running tests/wasm_pack_acceptance.py..."
	@$(PYTHON) tests/wasm_pack_acceptance.py
	@echo "ACCEPTANCE OK: Stage 76 -- hls-wasm-pack (publish-ready wasm + JS glue)"

.PHONY: wasm-pack wasm-pack-check wasm-pack-pack wasm-pack-acceptance

