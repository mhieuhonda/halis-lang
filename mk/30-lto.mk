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

# Stage 20 perfection (v0.39.0-alpha): LTO stats + tunable threshold.
#   lto-stats F=prog.hls       : compile with --lto and print the
#                                inline / DCE / dedup summary.
#   lto-threshold F=prog N=20  : compile with a custom inline budget
#                                (default 30; lower = less inlining,
#                                higher = more inlining + bloat).
#   lto-bench F=prog.hls       : compile + measure binary size on a
#                                stdlib-heavy program (plain vs LTO).
lto-stats:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@test -n "$(F)" || (echo "Usage: make lto-stats F=examples/foo.hls" && false)
	@mkdir -p $(BIN)
	@$(BIN)/hlc --lto-stats $(F) $(BIN)/hls_lto_stats.c
	@$(CC) $(CFLAGS) -o $(BIN)/hls_lto_stats $(BIN)/hls_lto_stats.c -lm -pthread

lto-threshold:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@test -n "$(F)" || (echo "Usage: make lto-threshold F=examples/foo.hls [N=20]" && false)
	@mkdir -p $(BIN)
	@if [ -z "$(N)" ]; then N=20; fi; \
	  $(BIN)/hlc --lto-threshold $$N --lto-stats $(F) $(BIN)/hls_lto_thr.c; \
	  $(CC) $(CFLAGS) -o $(BIN)/hls_lto_thr $(BIN)/hls_lto_thr.c -lm -pthread

lto-bench:
	@test -x $(BIN)/hlc || $(MAKE) bootstrap
	@test -n "$(F)" || (echo "Usage: make lto-bench F=examples/foo.hls" && false)
	@mkdir -p $(BIN)
	@$(BIN)/hlc $(F) $(BIN)/hls_plain.c
	@$(BIN)/hlc --lto $(F) $(BIN)/hls_lto.c
	@$(CC) $(CFLAGS) -o $(BIN)/hls_plain $(BIN)/hls_plain.c -lm -pthread
	@$(CC) $(CFLAGS) -o $(BIN)/hls_lto $(BIN)/hls_lto.c -lm -pthread
	@sz_plain=$$(stat -c %s $(BIN)/hls_plain); \
	  sz_lto=$$(stat -c %s $(BIN)/hls_lto); \
	  pct=$$((sz_lto * 100 / sz_plain)); \
	  echo "plain binary: $$sz_plain bytes"; \
	  echo "LTO binary  : $$sz_lto bytes ($$pct% of plain, $$((100 - pct))% drop)"

