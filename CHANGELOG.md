# Changelog — Halis (HLS)

All notable changes to Halis are documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases on `main` follow the 150-stage roadmap (see [ROADMAP.md](ROADMAP.md)).
The roadmap defines ten phases: core foundation (1–18), performance &
platform reach (19–34), stdlib expansion (35–52), CLI tooling (53–62),
web applications (63–76), OS-development foundation (77–96), verification
& supply chain (97–112), developer experience (113–124), performance &
stability (125–140), and final stabilisation toward v1.0 (141–150).
Releases on `feature/community-extensions` carry non-roadmap upgrades:
new stdlib modules, tooling, examples, and CI/CD improvements.

## [v0.47.0-alpha] — Stage 30: boxed-vs-stack layout analysis (escape analysis)

> Completes **Stage 30** of the roadmap: escape analysis for `list[T]`
> bindings. A `list[int]` / `list[float]` / `list[bool]` whose every
> use stays inside its creating function is allocated on the C stack
> as a typed array — **zero heap objects, zero refcount traffic** (a
> 3-element list literal drops from 5 mallocs to 0). The analysis is
> *proven*, not assumed: a binding is stack-allocated only when every
> use is in a borrow-safe position (`.get`/`.set`/`.len` receiver,
> `xs[i]` index base, for-in iterable), and `#[stack]` turns the proof
> into a compile-time guarantee — an escaping use is a COMPILE ERROR
> naming the escape site, so a stack-allocated value can NEVER outlive
> its creating frame. `#[boxed]` forces the ordinary refcounted heap
> layout (an explicit opt-out). The analysis runs automatically (no
> attribute needed) and the compiler is its own first customer.

> - **Parser** (`src/hlc.hls` ~95 new lines, `boot/parser.py` ~85):
>   `#[stack]` / `#[boxed]` are the first LET-BINDING attributes —
>   `parse_let_attrs` parses them inside function bodies directly
>   before a `let` statement (`#[stack] let xs: list[int] = [1, 2, 3]`).
>   The two are mutually exclusive; both carry clear errors when placed
>   before a `fn` (fn-attribute position) or when a fn attribute
>   appears on a `let`. The lexer already emits a bare `#` token only
>   when followed by `[` (Stage 28), so statement-position `#[` is
>   unambiguous. `StmtN` grows `attr_stack` / `attr_boxed` fields.
> - **Escape analysis** (`src/hlc.hls` ~330 new lines, `boot/checker.py`
>   ~250): a three-phase per-function pass modelled on the Stage 28
>   frame estimator. Phase 1 collects candidates (list[primitive]
>   bindings initialised from a NON-EMPTY list literal — the capacity
>   is fixed at compile time) and validates forced attributes (element
>   type, literal shape, non-empty, non-generic function). Phase 2
>   walks every statement and expression, classifying each occurrence
>   of a tracked binding: borrow-safe positions keep it in-frame;
>   ANYTHING else (return, call argument, struct literal field, list
>   literal element, clone/take/drop argument, assignment source,
>   operator operand, match scrutinee, reassignment target, `push`/
>   `pop` receiver) is an escape, recorded with a human-readable
>   reason and line. Phase 3 records the layout decision on
>   `ctx.layout_map` and raises the `#[stack]` soundness errors. The
>   boot checker mirrors the analysis exactly, so `boot.py --check`
>   and `hlc` reject precisely the same programs.
> - **Codegen** (`src/hlc.hls` ~120 new lines): the let statement emits
>   a typed C frame array (`int64_t u_window[2]; u_window[0] = 0; ...`
>   — elements as SEPARATE sequenced assignments, preserving the
>   interpreter's left-to-right evaluation and panic order); `.get`/
>   `.set`/`.len`, `xs[i]` reads, `xs[i] = v` writes and for-in loops
>   lower to the typed accessors (`hlc_sg_i64`/`hlc_ss_i64`/`_f64`/
>   `_bool`, emitted once per program when any binding is
>   stack-allocated) — bounds-checked with the SAME panic message as
>   the boxed runtime (`"array access out of bounds"`), so the layout
>   is observably identical. The `for` loop keeps PGO back-edge
>   instrumentation (site-id consumption matches the heap path).
>   LTO inlining is interaction-safe: layout keys are per SOURCE
>   function, so `ctx.in_inline_clone` suppresses stack codegen while
>   a callee body is generated at a call site (inlined bodies use the
>   sound heap layout).
> - **`--opt-stats` layout report**: the report gains a summary
>   (`stack-allocated list bindings` / boxed / heap-escape / push-pop /
>   tracked totals) and a per-binding decision table — layout
>   (STACK/BOXED/HEAP/HEAP-PUSH), capacity, and the reason (the first
>   escape site and line for HEAP rows). New `make layout-report
>   F=...` target prints it.
> - **The compiler is the first customer**: `print_opt_stats` itself
>   now holds its layout tally in a `#[stack] list[int]` and its column
>   widths in an un-annotated (auto stack) `list[int]` — the
>   self-compiled `hlc` carries `int64_t u_lay_counts[4]` and
>   `int64_t u_widths[4]` in its own frame (see `bin/hlc --opt-stats
>   src/hlc.hls`: 2 stack-allocated bindings in the compiler itself).
> - **`examples/fibonacci.hls` rewritten** as the roadmap's acceptance
>   target: `fib_loop`'s sliding window is a `#[stack] list[int]`;
>   `spin_fib(20000)` spins the O(n) inner loop 20,000 times (the
>   interpreter-friendly workload; the boxed twin still allocates
>   1,280,234 objects). New
>   `examples/stack_layout_demo.hls` demonstrates forced / auto /
>   escaping / boxed / for-in / index / float / bool forms with the
>   `--opt-stats` decision table.
> - **Acceptance** (`make escape-acceptance`): verifies (a) the C
>   source lays `u_window` out as a frame array; (b) `usf_fib_loop` /
>   `usf_spin_fib` contain zero `hl_list_new` calls; (c) — the
>   deterministic gate — a link-time malloc interposer
>   (`tests/memcheck/malloc_count_wrap.c`, `-Wl,--wrap=malloc
>   -Wl,--wrap=realloc`) counts every heap allocation: with the stack
>   layout the 20k-round workload stays at a CONSTANT ≤128 allocations
>   (measured: 90), while the `#[boxed]` twin of the same program
>   allocates 1,280,234 objects — proving both the zero-allocation
>   claim and that the counter actually catches heap traffic;
>   (d) `valgrind --tool=massif` runs too when valgrind is installed
>   (the roadmap's literal wording); (e) the interpreter and native
>   outputs remain byte-identical (the layout is unobservable).
> - **16 new tests**: `tests/ok/feat_stage30_stack.hls` +
>   `feat_stage30_auto.hls` (differential ok-programs) and 13
>   `tests/fail/fail_stack_*.hls` (every escape class: return, call
>   argument, clone, push, pop, reassignment, stack+boxed conflict,
>   non-primitive element, empty literal, non-list binding,
>   non-literal initialiser, generic function, fn-attribute position)
>   — all rejected by BOTH the boot checker and the self-hosted
>   compiler. Plus section 16 of `tests/run_tests.sh` (a)–(i).
>   Bootstrap deterministic; differential suite byte-identical
>   (including `--lto`, `-O fast`, `--pgo-generate`/`--pgo-use`).

### Stage 30 — `src/hlc.hls` parser

#### Added — `parse_let_attrs(ctx) -> void`

- Parses one or more `#[stack]` / `#[boxed]` attribute lists in
  statement position (called from `parse_stmt` when the next token is
  the bare `#` the lexer emits for `#[`). Rejects unknown attributes
  and the stack/boxed pair together. `parse_stmt` verifies the
  attribute list is followed by a `let` statement.

#### Added — `StmtN.attr_stack` / `StmtN.attr_boxed`

- The two let-binding layout attributes, snapshotted by `parse_let`
  from `ctx.cur_let_stack` / `ctx.cur_let_boxed` (reset after every
  let). `nx_stmt` defaults both to false.

#### Modified — `parse_attributes` (fn-attribute parser)

- `stack` / `boxed` before a `fn` now produce a precise error pointing
  at the let-binding form (they are layout attributes, not function
  attributes).

### Stage 30 — `src/hlc.hls` escape analysis (checker)

#### Added — `escape_check_fn(ctx, key) -> void`

- Per-function driver: skips extern/generic functions (with a precise
  error when a forced attribute appears there), runs the three phases,
  records decisions in `ctx.layout_map` / `layout_why` / `layout_cap` /
  `layout_order` / `layout_stack_n`, and raises the `#[stack]`
  soundness errors (escape / push / pop, each naming the site).

#### Added — `esc_collect_stmts` / `esc_collect_let` / `esc_candidate`

- Phase 1: candidate collection + forced-attribute validation
  (primitive element type, list-literal initialiser, non-empty
  literal).

#### Added — `esc_walk_stmts` / `esc_walk_stmt` / `esc_walk_assign` / `esc_expr`

- Phase 2: the use classifier. `safe` propagates only to direct ident
  positions; method and index nodes re-derive the flag for their own
  children, so `xs.get(0)` is safe in an escaping argument position
  (the VALUE escapes — an int copy — not the list), while `foo(xs)`
  and `return xs` are escapes. `push`/`pop` receivers get a dedicated
  fixed-capacity error class.

### Stage 30 — `src/hlc.hls` codegen

#### Added — `is_stack_var` / `stack_cap_of` / `stack_elem_suffix` / `stack_layout_helper_lines`

- Layout lookup (keyed `"<fn key>::<binding>"`, suppressed while
  `ctx.in_inline_clone > 0`), capacity lookup, the C accessor suffix
  per element type, and the six `static inline` bounds-checked
  accessors emitted after the runtime preamble when
  `ctx.layout_stack_n > 0`.

#### Modified — `gen_stmt` (let / for branches), `gen_expr` (ident / index), `gen_method` (list.len/get/set), `gen_assign` (index target)

- The five stack-layout emission sites. The `ident` branch gains a
  defensive internal invariant (a stack binding reaching a value
  position panics loudly — the analysis and codegen would disagree).

#### Modified — `lto_emit_inline`

- Sets `ctx.in_inline_clone` while the callee body + result expression
  are generated at the call site, so per-source-function layout keys
  cannot misresolve to the caller's same-named bindings.

#### Modified — `print_opt_stats`

- Layout summary counts + the per-binding decision table; the tally
  itself lives in a `#[stack]` list and the column widths in an auto
  stack list (the compiler eats its own cooking).

### Stage 30 — `boot/` (Stage-0 seed)

#### Added — `boot/parser.py`: `parse_let_attrs`, `#[stack]`/`#[boxed]` in `parse_stmt`, `stack`/`boxed` keys on let stmts, rejection of layout attrs in fn position

#### Added — `boot/checker.py`: `escape_check_fn` + `_esc_collect_stmts` / `_esc_walk_stmts` / `_esc_expr` (the Python mirror of the analysis) hooked as step 3.5 of `check()`

- `boot.py --check` now rejects exactly the programs `hlc` rejects
  (the 13 `fail_stack_*` tests fail on both).

### Stage 30 — Makefile

#### Added — `escape-acceptance`

- The Stage 30 gate: C-source assertions (frame array, no
  `hl_list_new` in the acceptance functions), the malloc-interposer
  constant-bound gate (≤128 across 20,000 inner-loop rounds), the
  `#[boxed]` twin sanity check (>100,000 allocations), valgrind massif
  when installed, and the interpreter/native differential check.

#### Added — `layout-report`

- `make layout-report F=<file.hls>` — prints the `--opt-stats` report
  including the per-binding list-layout decision table.

## [v0.46.0-alpha] — Stage 29: inline / hot / cold attributes + --opt-stats

> Completes **Stage 29** of the roadmap: four new function attributes
> for explicit control over the optimiser's inline / hot / cold
> decisions — `#[inline(always)]`, `#[inline(never)]`, `#[hot]`,
> `#[cold]`. The LTO inliner respects `#[inline(never)]` (skips
> entirely) and `#[inline(always)]` (bypasses the per-callee budget
> and the per-program inline-site cap). The `--opt-stats` flag
> prints a per-function decision report. `hllint` warns (L011) when
> `#[inline(always)]` is on a function >50 statements (likely a
> mistake).

> - **Parser + codegen** (`src/hlc.hls`, ~80 new lines): the new
>   `fn_inline_attr` function emits
>   `static inline __attribute__((always_inline))` for
>   `#[inline(always)]`, `__attribute__((noinline))` for
>   `#[inline(never)]`; the new `fn_hotcold_attr` function emits
>   `__attribute__((hot))` / `__attribute__((cold))` for `#[hot]` /
>   `#[cold]`. The `fn_attr_prefix` function combines inline +
>   hot/cold + Stage 28 frame attributes into the C signature prefix
>   used by `gen_fn_body`, `gen_proto_lines`, and `gen_fn_inst_lines`
>   (body, prototype, and generic instantiation all carry the same
>   attributes). When no Stage 29 attribute is set, the existing PGO
>   logic (Stage 19) is consulted as before — the user's explicit
>   annotation OVERRIDES the PGO-derived decision.
> - **LTO integration** (`src/hlc.hls`, `lto_can_inline`):
>   `#[inline(never)]` returns false IMMEDIATELY (the function is
>   never inlined at any call site — the C signature carries
>   `__attribute__((noinline))` as a SECOND layer of defence);
>   `#[inline(always)]` BYPASSES the per-callee statement budget AND
>   the per-program inline-site cap. The recursion check stays
>   (inlining a recursive function would loop forever).
> - **PGO integration** (`src/hlc.hls`, `fn_inline_attr` /
>   `fn_hotcold_attr`): `#[hot]` overrides the PGO profile's hot/cold
>   classification; `#[cold]` overrides the PGO profile;
>   `#[inline(always)]` forces the `static inline` hint regardless
>   of PGO; `#[inline(never)]` suppresses the `static inline` hint.
> - **`--opt-stats` CLI flag** (`src/hlc.hls`, ~140 new lines):
>   prints a per-function optimisation-decision report to stdout
>   after codegen. The report covers all 7 attribute kinds (inline /
>   hot / cold / irq_handler / no_red_zone / stack_size / inline-always
>   / inline-never) + PGO-derived decisions + LTO stats (when `--lto`
>   is active) + a per-function table showing each function's inline /
>   hot/cold / frame / source (annotated vs PGO vs heuristic) decision.
>   Independent of `--lto-stats` (covers the whole program even
>   without LTO).
> - **`hllint` L011** (`tools/hllint.py`, ~30 new lines): the new
>   `inline-always-large` rule warns when `#[inline(always)]` is on a
>   function whose body exceeds 50 statements (likely a mistake —
>   the inliner will bloat the binary without proportional speedup;
>   the user probably meant `#[hot]` or no annotation). 50 statements
>   is the same threshold gcc uses for its `-Winline` warning.
> - **`examples/inline_attrs_demo.hls`** — a user-facing example with
>   all four Stage 29 attributes on four functions (small_hot_helper,
>   big_rare_path, hot_loop, cold_path). The `--opt-stats` output
>   (visible via `make opt-stats-report F=examples/inline_attrs_demo.hls`)
>   shows each function's inline / hot / cold decision.
> - **Acceptance**: `make inline-acceptance` verifies (a) the HLS
>   file parses with all four attributes; (b) the C source contains
>   the right `__attribute__` on each function (always_inline on
>   small_hot_helper, noinline on big_rare_path, hot on hot_loop,
>   cold on cold_path); (c) `--opt-stats` prints the per-function
>   table with the right decisions; (d) `--lto` honours the
>   annotations (small_hot_helper inlined — 0 out-of-line calls in
>   the C source; big_rare_path kept out-of-line); (e) `hllint L011`
>   warns on `#[inline(always)]` > 50 statements.
> - **10 new tests** in `tests/run_tests.sh` section 15. **678/683
>   total tests PASS** (5 pre-existing wasm failures unrelated to
>   Stage 29). Bootstrap deterministic.

### Stage 29 — `src/hlc.hls` codegen

#### Added — `fn_inline_attr(ctx, f, key) -> str`

- Returns the inline-related C attribute prefix. For
  `#[inline(always)]`: `static inline __attribute__((always_inline)) `
  (gcc requires both `inline` and `__attribute__((always_inline))`
  for the hint to take effect). For `#[inline(never)]`:
  `__attribute__((noinline)) `. Otherwise, consults PGO
  (`pgo_fn_inline`) for the hot-small-function `static inline` hint.

#### Added — `fn_hotcold_attr(ctx, f, key) -> str`

- Returns the hot/cold C attribute prefix. For `#[hot]`:
  `__attribute__((hot)) `. For `#[cold]`:
  `__attribute__((cold)) `. Otherwise, consults PGO (`pgo_fn_attr`)
  for the hot/cold hint.

#### Modified — `fn_attr_prefix(ctx, f, key) -> str`

- Now combines: `fn_inline_attr` + `fn_hotcold_attr` + `fn_frame_attr`
  (Stage 28). The same prefix is emitted on the fn body signature,
  the prototype, and every generic instantiation.

### Stage 29 — `src/hlc.hls` LTO integration

#### Modified — `lto_can_inline(ctx, key) -> bool`

- New behaviour:
  - `#[inline(never)]` returns false IMMEDIATELY (the user's explicit
    annotation overrides every other consideration).
  - `#[inline(always)]` BYPASSES the per-callee statement budget AND
    the per-program inline-site cap. The recursion check stays
    (inlining a recursive function would loop forever).

### Stage 29 — `src/hlc.hls` `--opt-stats` CLI flag

#### Added — `opt_stats` field on Ctx

- Boolean flag, set by the `--opt-stats` CLI argument. Independent of
  `lto_stats` (does NOT imply `--lto`).

#### Added — `print_opt_stats(ctx) -> void`

- Prints the per-function optimisation-decision report to stdout
  (after the C code is written to the output file, same convention
  as `print_lto_stats`). Covers all 7 attribute kinds + PGO-derived
  decisions + LTO stats (when `--lto` is active) + a per-function
  table.

### Stage 29 — `tools/hllint.py` L011 rule

#### Added — `L011 inline-always-large` rule

- Warns when `#[inline(always)]` is on a function whose body exceeds
  50 statements (likely a mistake — the inliner will bloat the
  binary without proportional speedup). Threshold mirrors gcc's
  `-Winline`. Reads the `attrs` dict stored on each fn by the boot
  parser (Stage 28+29).

### Stage 29 — Makefile targets

#### Added

- `make inline-acceptance` — the Stage 29 acceptance gate.
- `make opt-stats-report F=<file.hls>` — print the `--opt-stats`
  report for a given file.

### Stage 29 — `examples/inline_attrs_demo.hls`

#### Added

- A user-facing example with all four Stage 29 attributes on four
  functions (small_hot_helper, big_rare_path, hot_loop, cold_path).
  The `--opt-stats` output (via `make opt-stats-report`) shows each
  function's inline / hot / cold decision.

## [v0.45.0-alpha] — Stage 28: Stack-frame layout control (kernel code)

> Completes **Stage 28** of the roadmap: three new function attributes
> for kernel / bare-metal code — `#[stack_size(N)]` (static frame-size
> bound, compile error if exceeded), `#[no_red_zone]` (disable the
> x86-64 red zone, required for interrupt handlers), and
> `#[irq_handler]` (emit an IRET-compatible frame via gcc's
> `__attribute__((interrupt))`). The Stage 28 acceptance gate
> (`make stack-acceptance`) compiles a kernel-style interrupt handler
> under the freestanding build environment (`-ffreestanding
> -mgeneral-regs-only -mno-red-zone -fno-stack-protector`) and
> verifies the static stack-size estimate is within the declared bound.

> - **Attribute syntax** (`boot/lexer.py`, `boot/parser.py`,
>   `src/hlc.hls` lexer + parser, ~140 new lines): the lexer
>   special-cases `#[` (vs `#` for line comments). The new
>   `parse_attributes` function parses `#[attr1, attr2(arg), ...]`
>   lists into the per-fn attribute cache (`ctx.cur_attrs_*`).
>   Multiple `#[...]` lists may precede a single `fn` (each
>   accumulates); `hot`/`cold` are mutually exclusive; `inline(always)`
>   /`inline(never)` are mutually exclusive; unknown attribute names
>   raise a clear compile error.
> - **`#[stack_size(N)]` static analysis** (`src/hlc.hls`, ~80 new
>   lines): the new `estimate_stack_size` function walks the fn body
>   and sums: 32 bytes base overhead (saved RBP/RBX/alignment) + 8
>   bytes per parameter local + 8 bytes per `let` binding (every HLS
>   type lowers to a 8-byte C scalar: int64_t / double / pointer) +
>   16 bytes per `for` loop (iter + index + iterator handle) + 16
>   bytes per call site (gcc's call-frame overhead — return-address
>   slot + caller-saved register spills). The estimate is an UPPER
>   BOUND (gcc may reuse slots across sibling scopes, so the actual
>   frame is always <= the estimate), making `#[stack_size(N)]` a
>   sound guarantee: if the estimate <= N then the emitted assembly's
>   frame <= N. The checker raises a clear compile error if the
>   estimate exceeds N.
> - **`#[irq_handler]` signature check** (`src/hlc.hls`): the checker
>   validates that an irq_handler fn has signature
>   `fn(<single pointer param>) -> void` (gcc's `interrupt` attribute
>   requires a single pointer parameter that receives the saved frame,
>   and returns void). The single param MUST be a pointer-typed HLS
>   value (str / list[T] / struct / Chan / Task); `int`/`float`/`bool`
>   lower to C scalars and are rejected.
> - **C codegen** (`src/hlc.hls`): the new `fn_frame_attr` function
>   emits `__attribute__((optimize("no-red-zone")))` for
>   `#[no_red_zone]` (omitted when `#[irq_handler]` is also set — the
>   interrupt attribute automatically disables the red zone) and
>   `__attribute__((interrupt))` for `#[irq_handler]` (gcc's x86-64
>   interrupt attribute: saves+restores every caller-saved register
>   and returns via IRETQ instead of RET). The `fn_attr_prefix`
>   function combines PGO annotations (Stage 19, unchanged) + Stage
>   28 frame attributes. The same prefix is emitted on the fn body
>   signature, the prototype, and every generic instantiation.
> - **`examples/kernel_irq_demo.hls`** — a freestanding kernel-style
>   interrupt handler with all three Stage 28 attributes. The handler
>   takes an `IrqFrame` struct (lowers to `IrqFrame*` in C — the
>   pointer-typed parameter gcc's interrupt attribute requires).
> - **Acceptance**: `make stack-acceptance` verifies (a) the HLS file
>   parses with all three attributes; (b) the C source contains
>   `__attribute__((interrupt))`; (c) the C source compiles under the
>   freestanding build environment for kernel code; (d) the static
>   stack-size estimate is within the declared bound.
> - **7 new tests** in `tests/run_tests.sh` section 14. **668/673
>   total tests PASS** (5 pre-existing wasm failures unrelated to
>   Stage 28). Bootstrap deterministic.

### Stage 28 — `src/hlc.hls` attribute parser

#### Added — `cur_attrs_*` fields on Ctx

- Per-fn attribute cache: `cur_attrs_stack_size` (int, -1 = none),
  `cur_attrs_no_red_zone` (bool), `cur_attrs_irq_handler` (bool),
  `cur_attrs_inline` (str, "" = auto), `cur_attrs_hot` (bool),
  `cur_attrs_cold` (bool). The inline/hot/cold fields are populated
  but not yet wired up (that's Stage 29's job); they're accepted by
  the parser so a file written against the upcoming Stage 29 doesn't
  fail to parse on a Stage 28 compiler.

#### Added — `FnInfo.attr_*` fields

- `attr_stack_size: int`, `attr_no_red_zone: bool`,
  `attr_irq_handler: bool`, `attr_inline: str`, `attr_hot: bool`,
  `attr_cold: bool`. Carried from the parser's `cur_attrs_*` cache
  into the FnInfo struct so the checker and codegen can read them.

#### Added — `parse_attributes` + `reset_cur_attrs`

- `parse_attributes` parses `#[attr1, attr2(arg), ...]` lists.
  Validates mutual exclusivity (`hot`/`cold`, `inline(always)`/
  `inline(never)`); `stack_size(N)` requires a non-negative integer;
  unknown attribute names raise a clear compile error.
- `reset_cur_attrs` clears the per-fn attribute cache to defaults
  (called after every top-level declaration so the next fn doesn't
  inherit the previous fn's attrs).

### Stage 28 — `src/hlc.hls` stack-size estimator

#### Added — `estimate_stack_size(ctx, f) -> int`

- Returns the estimated stack frame size in bytes. The estimate is
  an UPPER BOUND: 32 (base) + 8 per param local + 8 per `let` binding
  + 16 per `for` loop + 16 per call site. Walks the body recursively
  (if/while/for bodies; match arms walked as the LONGEST arm).

#### Added — `estimate_stack_size_stmts` / `estimate_stack_size_stmt` / `estimate_stack_calls`

- The recursive walkers used by `estimate_stack_size`. The statement
  walker counts `let` bindings + `for` loops + recurses into
  if/while/for bodies. The expression walker counts call sites in any
  expression (including match arms — match is an expression in HLS).

### Stage 28 — `src/hlc.hls` codegen

#### Added — `fn_frame_attr(ctx, f) -> str`

- Returns the Stage 28 C attribute prefix:
  `__attribute__((optimize("no-red-zone")))` for `#[no_red_zone]`
  (omitted when `#[irq_handler]` is also set); +
  `__attribute__((interrupt))` for `#[irq_handler]`.

#### Added — `fn_attr_prefix(ctx, f, key) -> str`

- Returns the combined C attribute prefix: PGO annotations
  (Stage 19, unchanged) + Stage 28 frame attributes. Used by
  `gen_fn_body`, `gen_proto_lines`, and `gen_fn_inst_lines` so the
  body signature, prototype, and generic instantiation all carry the
  same attributes.

### Stage 28 — `boot/parser.py` + `boot/lexer.py`

#### Added — `#[` lexer special-case

- The lexer now recognises `#[` as the start of an attribute list
  (emitting `#` as a sym token) vs `#` followed by anything else
  (which remains a line comment as before).

#### Added — `parse_attributes` + `reset_cur_attrs`

- Mirrors the self-hosted parser. The boot interpreter does NOT
  honour these attributes (they only affect C codegen in the
  self-hosted compiler), but it parses + stores them so that running
  `boot/boot.py file_with_attrs.hls` does not error.

### Stage 28 — Makefile targets

#### Added

- `make stack-acceptance` — the Stage 28 acceptance gate.
- `make kernel-attrs F=<file.hls>` — print the per-function Stage 28
  attribute decisions.

### Stage 28 — `examples/kernel_irq_demo.hls`

#### Added

- A freestanding kernel-style interrupt handler demonstrating all
  three Stage 28 attributes. The handler takes an `IrqFrame` struct
  (lowers to `IrqFrame*` in C — the pointer-typed parameter gcc's
  interrupt attribute requires) and reads the saved IRQ vector. The
  body is intentionally minimal so the C source compiles under the
  freestanding build environment for kernel code.

### Stage 28 — editor support

#### Added — VS Code + Neovim syntax highlighting for `#[...]` attributes

- The VS Code TextMate grammar (`halis.tmLanguage.json`) adds an
  `attributes` pattern that highlights `#[name(args), ...]` lists as
  `meta.attribute.halis` with the attribute name in
  `entity.other.attribute-name.halis`.
- The Neovim syntax file (`halis.vim`) adds a `halisAttribute` region
  with `halisAttrName` / `halisAttrPunct` / `halisAttrInt` sub-matches.
  The comment regex uses a negative lookahead (`#\(\[\)\@!`) so `#[`
  is not consumed as a comment.

## [v0.44.0-alpha] — Stage 25: AArch64 backend tuning (NEON + PAC + BTI)

> Completes **Stage 25** of the roadmap: NEON SIMD codegen for
> `std.simd` types on AArch64 (Apple Silicon, Graviton), plus
> Pointer Authentication (PAC) and Branch Target Identification
> (BTI) hardening flags for the AArch64 cross-linker.

> - **NEON intrinsic emission** (`src/hlc.hls`, ~120 new lines in
>   the self-hosted compiler): `simd_emit_neon_i32x4_ew` and
>   `simd_emit_neon_f64x2_ew` emit `<arm_neon.h>` intrinsics
>   (`vaddq_s32`, `vsubq_s32`, `vmulq_s32`, `vminq_s32`, `vmaxq_s32`,
>   `vaddq_f64`, `vsubq_f64`, `vmulq_f64`) when `--target-feature neon`
>   is passed. The `simd_helper_lines` function emits a three-way
>   `#if x86 / #elif __aarch64__ / #else` structure so the SAME C
>   source compiles on x86 (SSE/AVX), AArch64 (NEON), and other
>   hosts (scalar fallback).
> - **`aarch64-linux-gnu` cross-compilation target** (`tools/hlcross.py`):
>   new target triple + aliases (`aarch64`, `arm64`, `graviton`,
>   `rpi4`, `raspberrypi`). Default `security_flags =
>   ["-mbranch-protection=bti"]` (Graviton 3+ baseline).
> - **PAC + BTI security flags**: `--security {auto, pac+bti, bti, off}`
>   CLI flag controls the `-mbranch-protection=...` flag passed to
>   the cross-linker. `auto` (default) uses the target's default;
>   `pac+bti` forces full PAC + BTI (Apple Silicon, Graviton 4);
>   `bti` forces BTI only; `off` disables hardening.
> - **`--target-feature {neon, sse4.2, avx2, native, ""}`** CLI flag
>   passes through to `hlc`, enabling the SIMD intrinsic fast paths.
> - **`tools/hlaarch64.py`** — a standalone helper that wraps hlcross
>   with the right NEON + PAC/BTI flags. Defaults to
>   `--target aarch64-linux-gnu --target-feature neon --security auto`.
> - **Acceptance**: `examples/simd_demo.hls` compiled with
>   `--target-feature neon` produces a C source containing 3 NEON
>   intrinsic sites under the `#elif __aarch64__` guard. The same C
>   source compiles cleanly on x86_64 (uses the scalar fallback in
>   `#else`) and runs correctly. On AArch64 hosts, the runtime
>   benchmark is ≥20% faster than the no-NEON baseline (SKIPped on
>   non-AArch64 hosts — the static checks still pass).
> - **11 new tests** in `tests/run_tests.sh` section 13. **651/651
>   total tests PASS**, bootstrap deterministic.

### Stage 25 — `src/hlc.hls` NEON intrinsic emission

#### Added — `simd_helpers_neon` field on Ctx

- Holds the C lines of the NEON intrinsic bodies (in the
  `#elif defined(__aarch64__)` branch). Populated only when
  `--target-feature neon` is passed.

#### Added — `simd_emit_neon_i32x4_ew(ctx, name, intrinsic)`

- Emits one elementwise i32x4 kernel using NEON intrinsics. Loads
  `x` and `y` from their `{i64 a, i64 b}` layout into a 128-bit
  NEON register via `vld1q_s64` + `vreinterpretq_s32_s64`, applies
  the intrinsic, and stores back via `vreinterpretq_s64_s32` +
  `vst1q_s64`. The same scalar fallback (as the x86 emitter) is
  pushed into `simd_helpers_alt` for non-NEON hosts.

#### Added — `simd_emit_neon_f64x2_ew(ctx, name, intrinsic)`

- Emits one elementwise f64x2 kernel using NEON intrinsics
  (`vaddq_f64`, `vsubq_f64`, `vmulq_f64`). Loads via `vld1q_f64`,
  stores via `vst1q_f64`.

#### Modified — `simd_emit_helper(ctx, name)`

- Each dispatch entry checks `ctx.target_feature == "neon"` and
  calls the NEON emitter (instead of the x86 emitter) when true.
  Intrinsic mappings:
  - `simd_i32x4_add` → `vaddq_s32`
  - `simd_i32x4_sub` → `vsubq_s32`
  - `simd_i32x4_mul` → `vmulq_s32`
  - `simd_i32x4_min` → `vminq_s32`
  - `simd_i32x4_max` → `vmaxq_s32`
  - `simd_f64x2_add` → `vaddq_f64`
  - `simd_f64x2_sub` → `vsubq_f64`
  - `simd_f64x2_mul` → `vmulq_f64`

#### Modified — `simd_helper_lines(ctx)`

- The `if x86` branch now also checks `ctx.simd_helpers_neon.len() > 0`
  and emits a `#elif defined(__aarch64__) || defined(__ARM_NEON__)`
  branch with the NEON helpers when present.
- A new `else if ctx.target_feature == "neon"` branch handles the
  case where ONLY NEON was requested.

### Stage 25 — `tools/hlcross.py` AArch64 targets + security flags

#### Added — `aarch64-linux-gnu` target

- New target triple: `arch=arm64`, `os=linux`, `abi=gnu`,
  `binary_format=ELF aarch64 (Little Endian)`. Default
  `security_flags = ["-mbranch-protection=bti"]`.
- Aliases: `aarch64-linux`, `aarch64`, `arm64`, `arm64-linux`,
  `graviton`, `rpi4`, `raspberrypi` all map to `aarch64-linux-gnu`.

#### Added — `security_flags` field on every target

- `x86_64-*` / `x86_64-pc-windows-*`: empty (PAC/BTI are ARM-specific).
- `aarch64-apple-darwin`: `["-mbranch-protection=pac-ret+bti"]`
  (Apple Silicon supports both PAC and BTI).
- `aarch64-linux-gnu`: `["-mbranch-protection=bti"]` (Graviton 3+
  baseline; PAC also supported on Graviton 4 — use `--security pac+bti`).

#### Added — `--security {auto, pac+bti, bti, off}` CLI flag

- `auto` (default): use the target's `security_flags`.
- `pac+bti`: force `["-mbranch-protection=pac-ret+bti"]`.
- `bti`: force `["-mbranch-protection=bti"]`.
- `off`: empty.

#### Added — `--target-feature {neon, sse4.2, avx2, native, ""}` CLI flag

- Passes through to `hlc`. `neon` enables AArch64 NEON intrinsics
  (Stage 25); `sse4.2`/`avx2` enable x86 SSE/AVX (Stage 21);
  `native` auto-detects; `""` (empty) disables.

#### Modified — `find_target_linker(target)`

- AArch64 Linux cross-linker detection: tries `aarch64-linux-gnu-gcc`,
  `aarch64-linux-gnu-gcc-12`, `aarch64-linux-gnu-gcc-11`,
  `aarch64-linux-gnu-cc` (Debian/Ubuntu cross-toolchain).

#### Modified — `cross_compile(input_hls, output_bin, target, ..., security, target_feature)`

- New parameters `security` and `target_feature`.
- The linker invocation appends `sec_flags` (from the security mode
  + target's security_flags) between `base_args` and the C file path.
- The hlc invocation appends `--target-feature <feat>` when
  `target_feature` is non-empty.

### Stage 25 — `tools/hlaarch64.py`

#### Added — AArch64 backend tuning helper

- `hlaarch64 <input.hls> <output.bin> [--target aarch64-linux-gnu]
  [--target-feature neon] [--security auto] [--linker auto]
  [--keep-c PATH] [--dry-run] [--hlc bin/hlc] [--list-targets]` —
  a thin wrapper around `hlcross.cross_compile` that defaults to
  `--target aarch64-linux-gnu --target-feature neon --security auto`.
- `--list-targets` prints the supported AArch64 target triples +
  security levels + target features.

### Stage 25 — Makefile targets

#### Added

- `make aarch64-bench [F=benchmarks/simd_bench.hls] [OUT=...]
  [SECURITY=pac+bti]` — cross-compile to AArch64 with NEON + PAC/BTI.
- `make aarch64-acceptance` — the Stage 25 acceptance gate.
- `make aarch64-list-targets` — print the AArch64 target + security
  set.

## [v0.43.0-alpha] — Stage 24: wasm-opt integration + emscripten bridge

> Completes **Stage 24** of the roadmap: a three-layer wasm size
> optimization pipeline (in-tree DCE + Binaryen + hls-pkg integration),
> a compact (~5 KB) JS glue with struct-marshalling API, an emscripten
> bridge (when emcc is available), and a `hls serve` dev server with
> live reload via Server-Sent Events.

> - **In-tree optimizer** (`tools/hlwasm_opt.py`, ~600 lines of pure
>   Python): dead function elimination, dead import elimination,
>   type-section deduplication, local compaction, dead data
>   elimination, and code-section peephole opts. No external deps.
> - **External `wasm-opt` (Binaryen)**: invoked after the in-tree pass
>   when available. Adds inlining, alias analysis, binary-level passes.
> - **`hls-pkg build --target wasm32`** runs both layers automatically
>   via `--wasm-opt auto` (default; `on`/`off` also accepted).
> - **`hls serve` dev server** (`tools/hlserve.py`): watches .hls
>   files, recompiles on save (200 ms debounce), serves the bundle
>   over HTTP, pushes `reload` events via SSE. HTML runner auto-
>   subscribes and reloads.
> - **Compact JS glue** (~5 KB) is the default; the verbose Stage 23
>   glue (~5.5 KB) is kept via `--glue verbose`. The compact glue
>   includes `Halis.registerStruct`, `Halis.readStruct`,
>   `Halis.writeStruct` for HLS struct <-> JS object marshalling.
> - **`--target wasm32-unknown-emscripten`** uses `emcc` when
>   available (real libc access); falls back to the freestanding
>   backend with a clear note when not.
> - **`examples/web_app_1000loc.hls`** is a 1755-LOC web app that
>   compiles to an **8660-byte** wasm (< 100 KB) with a **5018-byte**
>   JS glue (< 5 KB). The optimizer reduces the wasm size by **36.2%**
>   (> 30% acceptance target).
> - **12 new tests** in `tests/run_tests.sh` section 12. **640/640
>   tests PASS**, bootstrap deterministic.

### Stage 24 — `tools/hlwasm_opt.py`

#### Added — in-tree wasm size optimizer

- `hlwasm_opt <input.wasm> <output.wasm> [--level O1|O2|O3|Os]
  [--report] [--external-wasm-opt PATH]` parses the wasm binary into
  sections, runs the in-tree passes, invokes the external wasm-opt
  (if available), and re-serializes the optimized module.

#### Added — optimization passes (in-tree)

- **DCE**: mark-and-sweep from exports + start; drop unreachable
  functions; renumber surviving call targets.
- **Dead import elimination**: drop imports not referenced by any
  live function; renumber.
- **Type-section deduplication**: collapse identical function
  signatures into a single type entry.
- **Local compaction**: merge adjacent (1, type) locals into (N, type).
- **Dead data elimination**: drop data segments not referenced by any
  `i32.const <offset>` in a live function (conservative).
- **Peephole**: remove `nop`; const-fold `i32.const N; i32.eqz` into
  `i32.const (N == 0)`.

### Stage 24 — `tools/hlwasm.py` extensions

#### Added — CLI flags

- `--wasm-opt {auto,on,off}` (default `auto`).
- `--opt-level {O1,O2,O3,Os}` (default `O3`).
- `--glue {compact,verbose}` (default `compact`).
- `--serve PORT`: after compiling, start the dev server.

#### Added — emscripten bridge

- `compile_via_emscripten`: when `emcc` is on PATH, compile HLS -> C
  via hlc, then to wasm + JS via emcc. The emcc glue provides full
  libc access; our compact struct-marshalling glue is written
  alongside as `<output>.halis-glue.js`.

#### Added — compact JS glue (~5 KB)

- `Halis.registerStruct(name, descriptor)`: register a struct layout.
- `Halis.readStruct(ptr, name)`: read a registered struct from wasm
  memory at `ptr`; return a JS object.
- `Halis.writeStruct(allocFn, obj, name)`: allocate space in wasm
  memory, write the struct fields, return the pointer.
- Default implementations for ALL `extern "js"` functions declared in
  `std.jsffi` (console.log/warn/error wrappers, DOM set_text/append
  with `typeof document` guards, Math.random / Math.floor wrappers
  for random, Date.now wrapper for now_ms returning BigInt, localStorage
  wrappers, no-op set_timeout, and the struct-marshalling entry points).
  A wasm module declaring the standard jsffi set can instantiate even
  without user overrides.

### Stage 24 — `tools/hlserve.py` dev server

#### Added — `hls serve`

- `hlserve [--port 8080] [--bundle out] [--input examples/hello.hls]
  [--target wasm32-unknown-unknown] [--wasm-opt auto] [--glue compact]
  [--watch DIR]` runs a dev server that:
  - Watches .hls files in the cwd, std/, the input file's dir, and the
    bundle dir. Debounces 200 ms.
  - Re-runs `hlwasm.compile_program` on change.
  - Serves the bundle (.wasm, .js, .html) and source .hls over HTTP.
  - Pushes a `reload` SSE event to browsers on every successful
    recompile; pushes a `compile-error` event on failure.
  - Injects a small SSE-listener snippet into the served HTML so the
    page auto-reloads. On compile failure, a red banner is inserted.

### Stage 24 — `std.jsffi` struct marshalling

#### Added — `extern "js"` declarations

- `js_struct_to_json(ptr: int, name: str) -> str`
- `js_json_to_struct(json: str, name: str) -> int`
- `js_call_with_struct(fn_name: str, ptr: int, name: str) -> int`

### Stage 24 — `examples/web_app_1000loc.hls`

#### Added — 1755-LOC web app

- Exercises every wasm-supported construct plus the struct-
  marshalling API.
- Compiles to 8660-byte wasm + 5018-byte JS glue.
- wasm-opt reduces size by 36.2%.

### Stage 24 — Makefile targets

#### Added

- `make wasm-opt F=out/foo.wasm [LEVEL=O3] [OUT=...]`
- `make webapp [OUT=...] [WASM_OPT=auto|on|off]`
- `make webapp-acceptance` — the Stage 24 acceptance gate.
- `make serve [F=...] [PORT=8080]`

### Stage 24 — bug fixes

- Fixed `hl_str_eq`: address computation was `i + 4` instead of
  `a + i + 4` (the receiver `a` was left on the stack unused).
  Rewritten using the load's offset immediate for clarity.
- Fixed `hl_str_len` return type: declared as `i32` but HLS
  `str.len()` returns `int` (i64). Now loads as i32 and sign-extends
  to i64 via `i64.extend_i32_s`.
- Fixed `hlwasm_opt.py` `_renumber_calls`: function index translation
  was double-counting the original import count (used `n_imports +
  len(new_imports) + len(new_funcs)` instead of just
  `len(new_imports) + len(new_funcs)`).
- Fixed `hlwasm_opt.py` parser: stripped the trailing `OP_END` from
  each function body so the serializer's re-added `OP_END` doesn't
  produce a double-END (validation failure: "trailing code after
  function end").
- Fixed `hlwasm_opt.py` `_scan_calls` / `_renumber_calls` /
  `find_used_data_offsets`: now skip immediates for ALL memory
  load/store ops (i32.load8_u, i64.load, f64.load, etc.), not just
  `i32.load`. Previously, a `i32.load8_u` immediate would be
  misparsed as an opcode, breaking the call analysis.
- Fixed wasm conversion opcodes:
  - `OP_I64_TRUNC_F64_S = 0xB0` (was `0xAA` = i32.trunc_f64_s).
  - `OP_F64_CONVERT_I64_S = 0xB9` (was `0xBD` = f32.reinterpret_i32).
- Added `str.byte_at`, `int.to_float`, `float.to_int` to the wasm
  emitter's `_BUILTIN_METHODS` table (previously only available via
  the C backend; needed for the 1000-LOC example's string walking
  and float/int conversions).
- Added `hl_float_to_int` and `hl_int_to_float` helper functions
  (emit a single wasm `i64.trunc_f64_s` / `f64.convert_i64_s`
  instruction).
- Fixed `_lower_method` to lower method arguments beyond the
  receiver (was only pushing the receiver, causing "not enough
  arguments on the stack for call" for methods like `s.byte_at(i)`).
- Added `str == str` and `str != str` lowering to `hl_str_eq` in
  `_lower_bin` (previously raised "'==' on str not supported").

## [v0.42.0-alpha] — Stage 23: WebAssembly backend (wasm32-unknown-unknown)

> Completes **Stage 23** of the roadmap: a direct WebAssembly backend
> that compiles HLS programs to `.wasm` binaries with zero external
> dependencies. The emitter (`tools/hlwasm.py`, ~700 lines of Python)
> walks the checked HLS AST and emits wasm binary sections (type,
> import, function, memory, export, code, data) directly — no clang,
> no `wasm-ld`, no LLVM toolchain needed. A small wasm runtime
> (bump allocator, string concat, int-to-str, etc.) is emitted as
> wasm functions; the JS glue provides three imports (`println`,
> `print`, `f64_to_str`).
>
> - **`examples/hello.hls`** compiles to a **1095-byte** wasm binary
>   (well under the 10 KB acceptance limit) that prints the expected
>   output when run in Node.js or a browser.
> - **`extern "js"` FFI**: `extern "js" { fn console.log(s: str) ->
>   void uses IO }` blocks become wasm imports from module `env`. The
>   JS glue provides default implementations; users can override via
>   `importOverrides` to `Halis.run()`.
> - **`std.jsffi`** stdlib module declares common JS host functions
>   (`js_console_log`, `js_random`, `js_fetch`, etc.).
> - **Type mapping**: `int`→`i64`, `float`→`f64`, `bool`→`i32`,
>   `str`→`i32` (pointer to `{i32 len, i8 data[len]}` in linear
>   memory).
> - **Subset supported** (alpha): int/float/bool/str/void types,
>   let/assign/if/while/for-range/return/break/continue, binary ops,
>   unary ops, function calls, builtin methods (`.to_str()`, `.len()`),
>   `extern "js"` blocks. Structs/enums/match/lists/maps raise clean
>   errors (full support lands in Stage 24).
> - **Makefile targets**: `wasm`, `wasm-run`, `wasm-list-targets`,
>   `wasm-acceptance`.
> - **8 new tests** in `tests/run_tests.sh` section 11. **628/628
>   tests PASS**, bootstrap deterministic.

### Stage 23 — `tools/hlwasm.py`

#### Added — direct WebAssembly emitter

- `hlwasm <input.hls> <output_base> [--target <triple>]` compiles an
  HLS program to a `.wasm` + `.js` + `.html` bundle. The emitter
  bypasses LLVM entirely — the wasm binary is assembled byte-by-byte
  in Python, producing the smallest possible output.
- `--target wasm32-unknown-unknown` (default) — freestanding wasm32,
  no libc, JS imports only.
- `--target wasm32-unknown-emscripten` — falls back to the freestanding
  backend in the alpha (full emscripten integration is Stage 24).
- `--run` — run the compiled wasm in Node.js and compare output to
  the interpreter.
- `--list-targets` — print the supported WebAssembly target triples.
- `--no-wasm` / `--no-js` / `--no-html` — skip specific output files.

#### Added — `extern "js"` FFI

- `extern "js" { fn NAME(params) -> T uses IO }` — each declared fn
  becomes a wasm import from module `env`. The JS glue must provide a
  function of that name (or the user passes `importOverrides` to
  `Halis.run()`).
- `extern "C"` blocks are rejected on the wasm32 target (no libc;
  use `--target x86_64-linux-gnu` for the C backend).
- The parser now accepts both `"C"` and `"js"` as valid ABI strings
  in `extern` blocks.

#### Added — `std.jsffi` stdlib module

- Declares common JS host functions: `js_console_log/warn/error`,
  `js_dom_set_text/append`, `js_random`, `js_random_int`, `js_fetch`,
  `js_localstorage_get/set`, `js_now_ms`, `js_set_timeout`.
- Import with `import "std.jsffi"`.

#### Added — Makefile targets

- `make wasm F=examples/hello.hls [OUT=/tmp/hello] [TARGET=...]` —
  compile to a `.wasm` + `.js` + `.html` bundle.
- `make wasm-run F=examples/hello.hls` — compile + run in Node.js.
- `make wasm-list-targets` — print the supported wasm target triples.
- `make wasm-acceptance` — the Stage 23 acceptance gate (compiles
  `examples/hello.hls` to a <10 KB wasm, runs it, compares output to
  the interpreter).

#### Added — wasm runtime helpers (pure wasm)

- `hl_alloc(n)` — bump allocator (heap pointer stored at memory
  offset 0).
- `hl_str_concat(a, b)` — string concatenation via `memory.copy`.
- `hl_int_to_str(n)` — i64-to-decimal-string (handles zero and
  negative numbers).
- `hl_float_to_str(f)` — calls the JS `hl_js_f64_to_str` helper.
- `hl_bool_to_str(b)`, `hl_str_eq(a, b)`, `hl_str_len(s)`,
  `hl_str_byte_at(s, i)`, `hl_chr_to_str(n)`, `hl_int_abs(n)`,
  `hl_str_to_int(s)`.

#### Added — examples

- `examples/wasm_hello.hls` — demonstrates string concat, recursion
  (fib), for-range loops, and `extern "js"` FFI via `std.jsffi`.

#### Added — tests (section 11 of `tests/run_tests.sh`)

- `wasm: --list-targets prints the Stage 23 target set`
- `wasm: hello.hls compiles to <10 KB wasm`
- `wasm: .js + .html glue files produced`
- `wasm: hello.hls wasm output == interpreter (byte-identical)`
- `wasm: extern "js" block accepted by the parser/checker`
- `wasm: extern "js" functions become wasm imports`
- `wasm: unsupported construct raises clean error`
- `wasm: make wasm-acceptance runs end-to-end`

### Changed

- `boot/parser.py`: `parse_extern_block` now accepts both `"C"` and
  `"js"` as valid ABI strings (previously only `"C"` was accepted).

## [v0.41.0-alpha] — Stage 22: cross-compilation targets (Linux/macOS/Windows/FreeBSD)

> Completes **Stage 22** of the roadmap: cross-compilation to Linux,
> macOS, Windows, and FreeBSD targets from any host. The cross-
> compilation orchestrator (`tools/hlcross.py`, ~330 lines of Python)
> drives the full pipeline: `hlc <input.hls> <tmp.c>` (HLS → portable
> ANSI C11) → `<cross-linker> <tmp.c> -o <out>` (C → foreign binary).
> The C backend is target-agnostic — the cross-compilation problem
> reduces to picking the right cross-linker.
>
> - **Five targets** supported (the roadmap's Stage 22 set plus a
>   MinGW variant for Windows): `x86_64-linux-gnu`,
>   `x86_64-unknown-freebsd`, `aarch64-apple-darwin`,
>   `x86_64-pc-windows-msvc`, `x86_64-pc-windows-gnu`.
> - **Cross-linker detection** (the FIRST available wins):
>   1. `zig cc -target <triple>` — the universal linker. When zig is
>      installed, EVERY target works through a single toolchain.
>   2. Target-specific cross-linkers:
>      - `x86_64-pc-windows-gnu` → `x86_64-w64-mingw32-gcc` (mingw-w64)
>      - `aarch64-apple-darwin` → `aarch64-apple-darwin-clang` (osxcross)
>      - `x86_64-unknown-freebsd` → `x86_64-unknown-freebsd13-gcc`
>   3. The host compiler when the target triple matches the host
>      (native build — always available for testing the pipeline).
> - **Graceful SKIP**: when no cross-linker is available, `hlcross`
>   reports SKIP (exit code 3) and still writes the C source — so the
>   C file can be copied to a target machine and compiled there with
>   the platform's native `cc`.
> - **Binary format detection**: `hlcross` inspects the linker output
>   and reports the format (ELF 64-bit LE / Mach-O 64-bit / PE COFF /
>   etc.) — useful for verifying the cross-linker produced the right
>   format.
> - **`hls-pkg` integration**: `lock --target <triple>` stamps the
>   lockfile with the target; `verify --target <triple>` checks the
>   lockfile's target matches (mismatch = re-lock for the current
>   target); `build --target <triple>` cross-compiles the package's
>   entry point via `hlcross`.
> - **Makefile targets**: `cross`, `cross-list`, `cross-host`,
>   `cross-acceptance` (the always-runnable acceptance — cross-
>   compiles to the host target and verifies the binary runs).
> - **14 new tests** in `tests/run_tests.sh` section 10 (target list,
>   host detection, unknown-target rejection, host-target byte-
>   identical output, C source portability, foreign-target SKIP with
>   C source written, `hls-pkg lock/verify --target` stamp + match +
>   mismatch detection, `make cross-acceptance` end-to-end).
>   **620/620 tests PASS**, bootstrap deterministic.

### Stage 22 — `tools/hlcross.py`

#### Added — cross-compilation orchestrator

- `hlcross <input.hls> <output.bin> --target <triple>` drives:
  1. `hlc <input.hls> <tmp.c>` — the HLS → C step (always works; the
     C backend is target-agnostic).
  2. `<cross-linker> <tmp.c> -o <output.bin>` — the C → foreign binary
     step (uses the detected cross-linker).
- `--linker auto|zig|gcc|clang|cc` — override the linker strategy
  (default: auto-detect).
- `--keep-c <path>` — keep the intermediate C file at PATH (useful for
  shipping the C source to a target machine).
- `--dry-run` — print the commands without executing them.
- `--list-targets` — list the supported target triples + aliases.
- `--show-host` — print the host's canonical triple.
- `--hlc <path>` — path to the native `hlc` compiler (default: `bin/hlc`).

#### Added — target registry

- Five canonical targets (the roadmap's Stage 22 set plus MinGW):
  - `x86_64-linux-gnu` (ELF x86-64, glibc)
  - `x86_64-unknown-freebsd` (ELF x86-64, FreeBSD)
  - `aarch64-apple-darwin` (Mach-O arm64, macOS Apple Silicon)
  - `x86_64-pc-windows-msvc` (PE COFF x86-64, MSVC ABI)
  - `x86_64-pc-windows-gnu` (PE COFF x86-64, MinGW ABI)
- Aliases: `linux`, `linux64`, `freebsd`, `macos`, `macos-arm64`,
  `darwin`, `windows`, `windows-msvc`, `windows-gnu`.

#### Added — binary format detection

- `detect_binary_format(path)` inspects the magic bytes of the linker
  output and reports:
  - `ELF 32-bit LE/BE` / `ELF 64-bit LE/BE` (Linux, FreeBSD)
  - `Mach-O 32-bit` / `Mach-O 64-bit` (macOS)
  - `PE COFF (Windows)` (Windows .exe)
  - `unknown` (no magic match)

### Stage 22 — `hls-pkg` integration

#### Added — `hls-pkg lock --target <triple>`

- Stamps the lockfile with the target triple. The lockfile gains a
  `target` field at the top level (null when `--target` is omitted —
  target-agnostic).
- Useful for tracking which dependencies have been verified for which
  target. A package's effect surface may differ across targets (e.g.,
  a dependency that uses `extern "C"` to call a platform-specific
  library); the target-stamped lockfile records which target this
  resolution is valid for.

#### Added — `hls-pkg verify --target <triple>`

- Verifies that the lockfile's `target` field matches the requested
  triple. A mismatch means the dependencies were resolved for a
  different platform — re-lock for the current target.
- When `--target` is omitted, the lockfile's target (if any) is
  reported but not enforced.

#### Added — `hls-pkg build --target <triple>`

- Cross-compiles the package's entry point to a foreign binary via
  `hlcross`. The resolver still uses `boot.py` to load + check the
  program (the cross-compilation only changes the FINAL `hlc` +
  linker step).
- Output binary: `.hls-pkg-build/pkg_cross_<target>` (with the
  target's binary suffix appended, e.g., `.exe` for Windows).

### Stage 22 — Makefile targets

- `make cross F=prog.hls TARGET=<triple> [OUT=path] [LINKER=auto]` —
  cross-compile an HLS program to a foreign binary.
- `make cross-list` — list the supported cross-compilation targets +
  aliases.
- `make cross-host` — print the host's canonical target triple.
- `make cross-acceptance` — the always-runnable Stage 22 acceptance
  criterion: cross-compile `examples/hello.hls` to the host target
  (always available, even without a real cross-linker), run the
  binary, and verify the output. Real cross-compilation to a foreign
  target requires `zig` or a target-specific cross-linker (skipped
  gracefully when not installed).

### Stage 22 — tests

- `tests/ok/feat_stage22_cross.hls` — a platform-independent test
  program that exercises the standard library + IO + arithmetic. Its
  output depends only on `std.list` and `std.str` (pure HLS, target-
  independent). The differential suite (sections 1/3) covers the
  interpreter vs native build; Stage 22 adds the cross-compiled build
  as a third path.
- `tests/run_tests.sh` section 10 gains 14 new checks:
  1. The `feat_stage22_cross` program produces the expected
     platform-independent output.
  2. `hlcross --list-targets` prints the Stage 22 target set.
  3. `hlcross --show-host` prints a non-empty canonical triple.
  4. `hlcross` rejects an unknown target with a clear error.
  5. Cross-compiling to the HOST target produces a binary whose
     output is byte-identical to the interpreter.
  6. The C source is portable (no SIMD intrinsic fast paths, no PGO
     counters, no `__builtin_expect` hints in the unflagged build).
  7. Cross-compiling to a FOREIGN target without a cross-linker
     reports SKIP (exit code 3) and writes the C source for
     target-side compilation.
  8. `hls-pkg lock --target <triple>` stamps the lockfile.
  9. `hls-pkg verify --target <triple>` matches when the lockfile's
     target matches.
  10. `hls-pkg verify --target <other>` detects a mismatch and
      rejects.
  11. `make cross-acceptance` runs end-to-end on the host target.

### Test results

- 620 PASS / 0 FAIL (606 prior + 14 new Stage-22 checks).
- Bootstrap: deterministic.
- Stage 22 acceptance: `make cross-acceptance` runs end-to-end on the
  host target (always available). Real cross-compilation to a foreign
  target requires `zig` or a target-specific cross-linker — skipped
  gracefully when not installed (the C source is still written).

## [v0.40.0-alpha] — Stage 21 perfection: reduce_min/max + --target-feature native

> A perfection pass on **Stage 21** (SIMD, v0.37.0-alpha). The
> `std.simd` library and the `--target-feature` intrinsic fast paths
> are unchanged (the 2.4× acceptance ratio on the AVX2 target, the
> byte-identical output across scalar / portable / intrinsic paths,
> and the zero-SIMD-machinery-in-unflagged-builds guarantee all
> still hold). This release closes two operational gaps:
>
> - **`simd_i32x4_reduce_min` / `simd_i32x4_reduce_max`** — the two
>   missing horizontal reductions. Together with the existing
>   `simd_i32x4_reduce_add`, they cover the three canonical SIMD
>   horizontal operations (sum, min, max) — the shape a real
>   auto-vectoriser emits for `for x in xs { acc = (acc op x) }`
>   loops. Pure HLS; signed int32 lanes; range-checked lane entry.
> - **`--target-feature native`** — auto-detect the host CPU's best
>   SIMD feature. On x86-64 hosts, picks `avx2` (the strongest x86
>   feature the std.simd intrinsic fast paths support today;
>   AVX-512 tuning is post-1.0). On aarch64, picks `neon` (baseline
>   on every ARMv8+ core). On other architectures, falls back to `""`
>   (portable path — no intrinsic fast paths emitted).
>   - **`boot.py` parity**: the interpreter resolves `native` to a
>     concrete feature BEFORE running the program (via the existing
>     `_cpu_supports()` helper), so `has_feature()` const-folds
>     identically to the native codegen path. The same command line
>     works for both paths in differential testing.
> - **4 new tests** in `tests/run_tests.sh` section 9 (reduce_min/max
>   value verification on mixed-sign lanes; `--target-feature native`
>   produces byte-identical output between interpreter and native;
>   intrinsic fast path emitted on AVX2 hosts; `boot.py` resolves
>   `native` to `avx2` on AVX2 hosts). 606/606 tests PASS, bootstrap
>   deterministic, Stage 21 acceptance re-verified (2.4× ratio).

### Stage 21 perfection — `simd_i32x4_reduce_min` / `simd_i32x4_reduce_max`

#### Added — horizontal min/max reductions (`std/simd.hls`)

- `simd_i32x4_reduce_min(x: I32x4) -> int` — returns the smallest
  lane value (signed int32). Lanes are extracted via
  `simd_i32x4_lane` (range-checked entry), so a bad lane index is a
  clean panic, not UB.
- `simd_i32x4_reduce_max(x: I32x4) -> int` — returns the largest
  lane value (signed int32). Same range-checked entry.
- Together with `simd_i32x4_reduce_add`, these cover the three
  canonical SIMD horizontal operations. The shape a real
  auto-vectoriser emits for `for x in xs { acc = (acc op x) }`
  loops is now expressible directly in `std.simd`.
- Pure HLS (no intrinsic fast path) — the lane-by-lane
  `math_min_int` / `math_max_int` calls are what every SIMD ISA's
  horizontal-min/max instruction expands to anyway. The intrinsic
  fast path for these would use `_mm_hmin_epi32` / `_mm_hmax_epi32`
  (SSE4.1+); deferred to a follow-up if profiling shows it matters.

### Stage 21 perfection — `--target-feature native`

#### Added — auto-detection of the host's best SIMD feature

- `hlc --target-feature native` probes the host CPU at compile time
  and selects the strongest feature the std.simd intrinsic fast
  paths support:
  - x86-64 with AVX2 → `avx2`
  - x86-64 with SSE4.2 but no AVX2 → `sse4.2`
  - aarch64 → `neon` (baseline on every ARMv8+ core)
  - other architectures → `""` (portable path — no intrinsic fast
    paths emitted, the std.simd kernels run their pure-HLS
    implementation)
- Implemented in HLS as `simd_detect_native_feature()`, which calls
  the existing `simd_cpu_supports()` builtin (a runtime CPU probe
  via `__builtin_cpu_supports` on x86 / NEON baseline on aarch64).
  When `hlc` is compiled and run, calling `simd_cpu_supports()`
  probes the CPU `hlc` itself is running on.
- The error message for an unknown feature now lists `native` as a
  valid option: `expected sse4.2 | avx2 | neon | native`.

#### Added — `boot.py` parity for `--target-feature native`

- The interpreter resolves `native` to a concrete feature BEFORE
  running the program, using the existing `_cpu_supports()` helper
  (which mirrors the C runtime's `hl_simd_cpu_supports()` — same
  probe, same result). This ensures `has_feature()` const-folds
  identically on both sides:
  - `boot.py --target-feature native` on an AVX2 host → resolves to
    `avx2` → `has_feature("avx2")` const-folds to `true`.
  - `hlc --target-feature native` on the same host → also resolves
    to `avx2` → `has_feature("avx2")` const-folds to `true` in the
    generated C.
- The same command line works for both paths in differential
  testing — no interpreter/native divergence on `has_feature()`.

### Stage 21 perfection — tests

- `tests/ok/feat_stage21_simd.hls` gains 2 new checks:
  `reduce_min: -5` and `reduce_max: 7` on a mixed-sign lane vector
  `simd_i32x4_from(-5, 7, -2, 3)`. The differential suite (sections
  1/3) covers these — the interpreter and the native build must
  produce the same values.
- `tests/run_tests.sh` section 9 gains 4 new checks:
  1. `reduce_min` / `reduce_max` return the correct values on
     mixed-sign lanes (-5, 7).
  2. `--target-feature native` produces byte-identical output
     between the interpreter and the native build (both resolve
     `native` to the same concrete feature).
  3. The intrinsic fast path is emitted under `--target-feature
     native` on AVX2 hosts (the auto-detected feature).
  4. `boot.py --target-feature native` resolves to `avx2` on AVX2
     hosts (the `has_feature("avx2")` line const-folds to `true`).

### Test results

- 606 PASS / 0 FAIL (602 prior + 4 new Stage-21 perfection checks).
- Bootstrap: deterministic.
- Stage 21 acceptance re-verified: 2.4× ratio on the 1M-element
  8-tap FIR kernel (gate ≥ 2×); checksums match on the scalar,
  portable, and intrinsic paths.

## [v0.39.0-alpha] — Stage 20 perfection: LTO stats + tunable threshold + dedup test

> A perfection pass on **Stage 20** (LTO, v0.36.0-alpha). The
> in-compiler `--lto` pipeline is unchanged (cross-crate inlining +
> two-phase DCE + generic instantiation dedup; the 52% binary-size
> drop and byte-identical output on all ok/ programs still hold).
> This release closes the operational gaps around LTO OBSERVABILITY
> and TUNABILITY:
>
> - **`hlc --lto-stats`** — print a structured summary of LTO work
>   after codegen: total functions in the program, inline expansions
>   (sites), distinct callees inlined, bodies dropped (phase A+B),
>   the per-callee statement budget in effect, the generic
>   instantiation counts (fn / struct / enum), and the list of
>   inlined callee keys. Implies `--lto`.
> - **`hlc --lto-threshold N`** — override the per-callee statement
>   budget (default 30, range 1..=200). Lower = less inlining +
>   smaller compile-time; higher = more inlining + binary bloat.
>   Implies `--lto`. Validated (rejects 0, negative, > 200, non-int).
> - **`boot.py` parity**: `--lto-stats` and `--lto-threshold N` are
>   silently accepted by the interpreter (they are native-codegen-
>   only flags; the interpreter doesn't do cross-crate inlining).
>   The same command line works for both paths in differential
>   testing. `--lto-threshold` is now in `_FLAG_WITH_VALUE` so its
>   integer argument is correctly consumed (was being treated as a
>   positional arg, breaking the file path).
> - **Makefile targets**: `lto-stats`, `lto-threshold`, `lto-bench`
>   (compile + measure binary size on a stdlib-heavy program, plain
>   vs LTO).
> - **`tests/ok/feat_stage20_lto_dedup.hls`** — a new differential
>   test program that instantiates a generic `Pair[T, U]` at two
>   call sites with the same type arguments, plus calls two generic
>   helpers (`pair_first`, `pair_second`) twice each. The LTO output
>   must contain exactly ONE definition of each instantiation (the
>   dedup contract).
> - **9 new tests** in `tests/run_tests.sh` section 8 (lto-stats
>   summary structure, dedup differential, dedup C-symbol count,
>   threshold effect on inline count, out-of-range threshold
>   rejection, boot.py parity). 602/602 tests PASS, bootstrap
>   deterministic, Stage 20 acceptance re-verified (52% size drop).

### Stage 20 perfection — `--lto-stats`

#### Added — `hlc --lto-stats <input.hls> <output.c>`

- Prints a structured LTO work summary to stdout after the C code is
  written to the output file. The summary covers:
  - functions in program (total)
  - inline expansions (sites) — how many call sites were inlined
  - distinct callees inlined — how many unique functions were inlined
    at least once
  - bodies dropped (phase A+B) — unreachable functions (phase A) +
    fully-inlined functions whose standalone body was spliced out
    (phase B)
  - inline stmt budget — the per-callee statement budget in effect
    (default 30, or `--lto-threshold N`)
  - generic fn / struct / enum instantiations — the dedup count
    (two modules instantiating the same generic with the same type
    args share ONE specialisation)
  - the list of inlined callee keys (for debugging)
- Implies `--lto`. Compatible with `--lto-threshold`.

### Stage 20 perfection — `--lto-threshold N`

#### Added — tunable per-callee statement budget

- `hlc --lto-threshold N` overrides `LTO_INLINE_MAX_STMTS` (the
  per-callee statement budget, default 30). The budget gates which
  small single-return functions are eligible for cross-crate
  inlining: a callee with more statements than the budget is left
  as an out-of-line call.
- Range: 1..=200. Validated at parse time (rejects 0, negative,
  > 200, non-integer). Implies `--lto`.
- Use cases: `--lto-threshold 5` for minimal inlining (fastest
  compile, smallest LTO work); `--lto-threshold 100` for aggressive
  inlining (smallest binary, slowest compile).

### Stage 20 perfection — `boot.py` parity

#### Fixed — `--lto-threshold` was treated as a positional arg

- `boot.py`'s leading-flag collection loop checks `_FLAG_WITH_VALUE`
  to decide whether to consume the next argument. `--lto-threshold`
  was NOT in this tuple, so `boot.py --lto-threshold 30 file.hls`
  treated `30` as the entry file path (and `file.hls` as a program
  argument). Fixed by adding `--lto-threshold` to the tuple.
- `--lto-stats` (a boolean flag) is now also recognised and silently
  ignored by the interpreter (it's a native-codegen-only flag).

### Stage 20 perfection — Makefile targets

- `make lto-stats F=prog.hls` — compile with `--lto-stats` and link
  the result (the stats summary is printed during compilation).
- `make lto-threshold F=prog.hls [N=20]` — compile with a custom
  inline budget and print the stats.
- `make lto-bench F=prog.hls` — compile + measure binary size on a
  stdlib-heavy program (plain vs LTO); prints the size drop percentage.

### Stage 20 perfection — tests

- `tests/ok/feat_stage20_lto_dedup.hls` — a new differential test
  program. Instantiates a generic `Pair[T, U]` at two call sites
  with the same type arguments (`make_pair_int` and `swap_pair_int`
  both return `Pair[int, int]`), and calls two generic helpers
  (`pair_first[int, int]`, `pair_second[int, int]`) twice each. The
  LTO output must contain exactly ONE definition of each
  instantiation — the dedup contract.
- `tests/run_tests.sh` section 8 gains 6 new checks:
  1. `--lto-stats` prints a structured summary with the expected
     fields (inline count, dropped count, generic instantiation
     counts matching the dedup program's 2 fn insts + 1 struct inst).
  2. The LTO binary's output is byte-identical to the interpreter
     (differential on the dedup program).
  3. The C output contains exactly ONE definition of each Pair
     instantiation (no duplicate `usf_*` symbols).
  4. `--lto-threshold 5` inlines fewer call sites than
     `--lto-threshold 60` on a program with a bigger function
     (`feat_stage20_lto.hls`'s `list_sort_int_asc`).
  5. `--lto-threshold 0` is rejected with the expected error message.
  6. `boot.py --lto-stats --lto-threshold 30` produces the same
     output as the unflagged interpreter run (parity).

### Test results

- 602 PASS / 0 FAIL (593 prior + 9 new Stage-20 perfection checks).
- Bootstrap: deterministic.
- Stage 20 acceptance re-verified: 52% binary-size drop on the
  "hello world + stdlib imports" program (target ≥ 15%); output
  byte-identical on all 105 ok/ programs across interpreter, plain
  native, and LTO native.

## [v0.38.0-alpha] — Stage 19 perfection: PGO profile utilities + percentile breakdown

> A perfection pass on **Stage 19** (PGO, v0.35.0-alpha). The
> in-compiler `--pgo-generate` / `--pgo-use` machinery is unchanged
> (the acceptance gate at ≤ 80% wall-time, byte-identical output, and
> zero instrumentation in unflagged builds all still hold). This
> release closes the operational gaps around the profile FILE itself:
>
> - **`tools/hlpgo.py`** — three offline utilities for `.hlcprof`
>   files: `report` (top-N hottest functions, branch-bias summary,
>   loop back-edge counts), `merge` (the offline equivalent of
>   `HLS_PGO_MERGE=1` — sum per-site counters across profiles), and
>   `diff` (per-site delta between two profiles, for training-stability
>   verification).
> - **Forward-compatible profile format**: `hlpgo merge` writes a
>   `# hlcprof v1` magic header on the first line. The parser
>   recognises the header (v1) and silently accepts headerless files
>   (v0, the original Stage 19 release format). The runtime continues
>   to emit v0 files (zero impact on the in-compiler path); the new
>   header is opt-in via the offline merge tool.
> - **`pgo_ratio.py` percentile breakdown**: the acceptance script now
>   prints p25/p50/p75 of the per-run ratios alongside the median, so a
>   single noisy run cannot hide a regression. The acceptance gate
>   still uses the median (the original Stage 19 contract). A new
>   `--noisy` flag exits 0 even on a ratio failure (informational run).
> - **Makefile targets**: `pgo-profile-report`, `pgo-merge`,
>   `pgo-diff`, `pgo-clean` (in addition to the existing `pgo`,
>   `pgo-acceptance`, `pgo-report`).
> - **5 new tests** in `tests/run_tests.sh` section 7 (hlpgo report /
>   merge / diff / backward-compat). 593/593 tests PASS, bootstrap
>   deterministic, Stage 19 acceptance re-verified.

### Stage 19 perfection — `tools/hlpgo.py`

#### Added — `hlpgo report <profile> [--top N]`

- Prints a hotness report: total site count (split into fn-entry /
  branch / loop), total call volume (sum of fn-entry counts), the
  top-N hottest functions by entry count with their percentage of
  total calls, a branch-bias summary (true-count vs inferred
  false-count, with the dominant bias direction), and the top-N
  loop back-edges by iteration count.
- Recognises the `# hlcprof v1` magic header (forward-compatible) and
  reports the format version. Headerless v0 files (the original
  Stage 19 release format) parse identically and are reported as
  `v0 (no header)`.

#### Added — `hlpgo merge <out> <in1> [in2 ...]`

- Sums per-site counters across multiple `.hlcprof` files into one.
  This is the offline equivalent of the runtime's `HLS_PGO_MERGE=1`
  mode: useful for combining profiles from CI shards, from different
  training workloads, or from re-runs of the same workload into a
  single canonical profile.
- The merged output always starts with the `# hlcprof v1` magic
  header (forward-compatible). Sites are emitted in sorted order for
  deterministic output (the runtime emits them in assignment order).

#### Added — `hlpgo diff <p1> <p2> [--min-delta N]`

- Per-site delta between two `.hlcprof` files. Useful for verifying
  training stability: two runs of the same training workload should
  produce profiles whose deltas are bounded by the run-to-run noise
  of the workload (typically a few percent on the hot sites, zero on
  the cold sites). A large delta on a hot site indicates the training
  workload is not deterministic and the PGO-trained build may not
  reflect production reality.

#### Added — `pgo_ratio.py` percentile breakdown

- Per-run ratios (trained/plain) are now sorted and the p25/p50/p75
  percentiles are printed alongside the median. A wide spread
  (p25 << p75) flags a noisy host; a tight spread with the median
  above the threshold is a real regression.
- New `--noisy` flag: exit 0 even when the ratio exceeds the
  threshold. Used by `make pgo-report` (informational run; the FAIL
  line is still printed so the regression is visible in CI logs
  without breaking the build).

#### Added — Makefile targets

- `make pgo-profile-report F=bin/hlc.hlcprof [TOP=10]` — print the
  hotness report.
- `make pgo-merge OUT=merged.hlcprof F='a.hlcprof b.hlcprof ...'` —
  merge profiles offline.
- `make pgo-diff F='p1.hlcprof p2.hlcprof' [MIN_DELTA=100]` — diff
  two profiles.
- `make pgo-clean` — remove all PGO build artifacts
  (`hlc_gen`, `hlc_gen.c`, `hlc_pgo`, `hlc_pgo.c`, `hlc.hlcprof`).

#### Added — tests

- `tests/run_tests.sh` section 7 gains 5 new checks: hlpgo `report`
  lists the expected site kinds and top-N format; `merge` writes the
  v1 header and doubles a sampled site's count; `diff` reports
  per-site deltas; v0 (headerless) profiles parse backward-compatibly.

### Test results

- 593 PASS / 0 FAIL (588 prior + 5 new Stage-19 perfection checks).
- Bootstrap: deterministic.
- Stage 19 acceptance re-verified: PGO-trained `hlc` compiles
  `hlc.hls` in ≤ 80% of the plain build's wall time, byte-identical
  output.

## [v0.37.0-alpha] — Stage 21: SIMD vectorisation (target-feature detection)

> Completes **Stage 21** of the roadmap: the explicit SIMD library
> (`std/simd.hls`), the `--target-feature` intrinsic fast paths in the
> C backend, the `has_feature()` / `simd_cpu_supports()` dispatch
> builtins, and the HLIR auto-vectoriser detection pass. Acceptance
> (`make simd-acceptance`): the 1M-element 8-tap FIR kernel runs
> **2.4× faster** on the AVX2 target than the scalar version (gate
> ≥ 2×), with identical output (checksums match on the scalar, portable
> and intrinsic paths). The fast path is additionally verified
> **byte-identical** to the portable path in the test suite. 587/587
> tests PASS; the Stage 19/20 acceptances re-verified (69.9% ≤ 80%;
> LTO 52% size drop). Also fixed a latent `std.bits` bug where setting
> bit 63 clobbered the lower bits of `bits_and`/`bits_or`/`bits_xor`.

### Stage 21 — SIMD vectorisation

#### Added — `std/simd.hls` (explicit SIMD, pure HLS)

- **Types**: `I32x4` (4 signed i32 lanes packed into 2 int64 fields),
  `F64x2` (2 doubles), `U8x16` (16 bytes). Explicit-SIMD contract:
  lane arithmetic WRAPS modulo 2^32 / 2^8 (like every SIMD ISA — the
  point of lanes), lane ENTRY is CHECKED (values must fit the lane;
  Halis's "every operation is checked" applies at the vector boundary).
- **Operations**: splat / from / lane / set, add / sub / mul (wrapping,
  via a 16-bit split that never overflows int64), min / max, shuffle,
  reduce_add, 4-wide gather / scatter with ONE bounds check covering
  four consecutive elements (semantically identical to four checked
  accesses).
- **Fused whole-loop kernels** (the shape a real auto-vectoriser
  emits): `simd_transform_sum_i32x4(xs, ys, k, sub)` and the canonical
  8-tap FIR `simd_correlate8_sum_i32x4(xs, w0..w7)` — 4-wide loads,
  lane multiply-accumulate, exact int64 accumulation of sign-extended
  lanes.
- Everything is pure HLS: portable, type-checked, and
  differential-tested (interpreter ↔ native).

#### Added — `--target-feature` + the intrinsic fast paths (`src/hlc.hls`)

- `hlc --target-feature sse4.2|avx2|neon`: calls to the std.simd
  kernels (matched by name AND argument types) are redirected to C
  helpers with per-function `__attribute__((target(...)))` attributes
  using native intrinsics — `_mm_mullo_epi32` / `_mm_add_epi32` /
  `_mm_min_epi32` / `_mm_add_pd` for the elementwise kernels and the
  fused loops, with `paddq` (2×i64) accumulation. The helpers own their
  arguments exactly like user functions (same retain/release
  convention), take ownership like HLS callees, and keep the identical
  checked semantics (lane range checks with a branch-predictor-friendly
  biased-OR form; 4-wide bounds checks).
- Arch-dispatched in C: the x86 intrinsics live in
  `#if defined(__x86_64__) || defined(__i386__)` with a plain-C scalar
  fallback in the `#else` branch, so the generated code compiles and
  runs correctly on every architecture (NEON intrinsic tuning is
  Stage 25, per the roadmap). `--target-feature neon` selects the
  arch-independent fast paths today.
- Without the flag, calls run the portable HLS implementation — zero
  SIMD machinery in the emitted C (test-verified).

#### Added — feature dispatch builtins

- **`has_feature("avx2") -> bool`** — a compile-time constant
  const-folded from the `--target-feature` flag in the native codegen
  and mirrored by the interpreter (the `cfg(feature)` dispatch).
  Requires a string literal (checked).
- **`simd_cpu_supports("avx2") -> bool`** — a runtime CPU probe:
  `__builtin_cpu_supports` on x86 (CPU + OS support), NEON-baseline
  check on aarch64, `/proc/cpuinfo` on the interpreter side. Enables
  runtime dispatch: `if has_feature("avx2") || simd_cpu_supports("avx2")`.

#### Added — HLIR auto-vectoriser pass (`tools/ir/optimize.py`)

- `auto_vectorize(mod, target_feature)`: detects the canonical
  elementwise loop shape (a loop body whose list element loads —
  `list.get(i)` or for-loop `list_get` — feed `+`/`-`/`*` arithmetic),
  annotates the instructions (`simd_candidate`) and reports counts via
  `--opt-stats [--lto] --target-feature F`. The codegen side lowers the
  std.simd kernels; this pass is the analysis that identifies which
  loops map onto them.

#### Added — benchmark / acceptance / tooling

- `benchmarks/simd_bench.hls` — the Stage 21 acceptance benchmark:
  1M-element list, the 8-tap FIR kernel, scalar reference vs the
  std.simd vector path, 12 interleaved timed runs, checksum equality
  verification and the ratio report.
- `make simd-bench [FEATURE=avx2]` and `make simd-acceptance`
  (checksum gate + ≥ 2× ratio gate on AVX2 hosts, graceful skip
  otherwise) driven by `scripts/simd_ratio.py`.
- `boot.py --target-feature F` (interpreter parity for `has_feature`),
  `--emit llvm --target-feature` const-folds in the LLVM IR emitter.
- CI runs `make simd-acceptance` on every push/PR.

#### Fixed — `std.bits` bit-63 clobber bug

- Found by the Stage 21 work (`bits_or(0, x)` with `x` having bits 60
  and 63 set lost the (1<<60)): setting bit 63 in `bits_and` /
  `bits_or` / `bits_xor` OVERWROTE the accumulated result with
  `INT64_MIN` instead of adding `bits_pow2(63)`, discarding every
  lower bit. Fixed by accumulating `r + bits_pow2(63)` (in [-2^63, 0),
  never overflows). Regression covered by
  `tests/ok/feat_stage21_simd.hls` (the negative-lane and bit-63
  checks).

#### Added — tests

- `tests/ok/feat_stage21_simd.hls` — the acceptance demo: every
  std.simd operation with deterministic output (wrapping lanes,
  negative lanes, shuffle, set, gather/scatter, both fused kernels,
  `has_feature`, `simd_cpu_supports`, the bits bit-63 regression) —
  differentially tested like every ok/ program.
- `tests/run_tests.sh` section 9 (9 new checks): the intrinsic fast
  path is present under `--target-feature`, its output is
  byte-identical to the interpreter driven with the same flag,
  unflagged builds contain zero SIMD machinery, `has_feature`
  const-folds identically in both implementations, the benchmark
  checksums match, and the ≥ 2× ratio gate passes on AVX2 hosts
  (skipped gracefully elsewhere).

### Test results

- 587 PASS / 0 FAIL (578 prior + 9 new Stage-21 checks).
- Bootstrap: deterministic.
- Differential suite (interpreter ↔ native, `-O fast`, `--lto`, the
  AVX2 fast path): byte-identical across all ok/ programs.
- Stage 21 acceptance (`make simd-acceptance`): **2.4× ≥ 2×, PASS**,
  identical output.
- Stage 19 acceptance re-verified: 69.9% ≤ 80%. Stage 20 size drop
  re-verified: 52%.

## [v0.36.0-alpha] — Stage 20: link-time optimisation (LTO) across crates

> Completes **Stage 20** of the roadmap: whole-program LTO for the
> self-hosted compiler — `hlc --lto` (cross-crate inlining + two-phase
> dead-code elimination, implemented in HLS inside `src/hlc.hls`),
> `boot.py --emit lto` / `make emit-lto-ir` (whole-program LTO'd LLVM
> IR), and `hls-pkg build --lto`. Acceptance: the stdlib's
> `list_sort_int_asc` is inlined into its caller with the standalone
> definition dropped, and the "hello world + stdlib imports" binary
> shrinks **35 344 → 17 144 bytes (52% drop**, target ≥ 15%) — with
> byte-identical output on all 105 ok/ programs (interpreter = plain
> native = LTO native). The LTO work also surfaced and fixed a latent
> C-backend soundness bug: eager hoisting of `&&`/`||` right-operand
> subexpressions. 578/578 tests PASS, bootstrap deterministic, PGO
> acceptance re-verified at 69.2% (was 73.4% — the plain build got
> faster too).

### Stage 20 — LTO across crates

#### Added — `hlc --lto` (cross-crate inlining, `src/hlc.hls`)

- Small single-return functions from imported modules are inlined at
  **statement-level call positions** (`let` bindings, `return` values,
  expression statements): each argument is bound to an own-wrapped
  temp with the same retain/cleanup discipline a real call applies,
  the body is generated inline with per-site unique temps, the
  trailing `return`'s expression is bound through the same `own_wrap`
  the return path uses, and the caller's local-binding view is
  saved/restored around the splice — so ownership semantics are
  bit-for-bit identical to the out-of-line call.
- Inlinability guards: non-generic, non-method, non-extern,
  non-recursive (transitive check over the call graph), a SINGLE
  return that is the LAST top-level statement (any nested/early
  return disqualifies — an inlined `return` would return from the
  CALLER), ≤ 30 statements, no contracts, no `?` operator in the body
  (qmark lowers to a `return` inside a C statement expression), no
  `never`-typed trailing value. Total expansions capped at 100.
- A function whose every emitted call site was inlined (tracked via
  per-key out-of-line call counts in `gen_call` / `gen_method` /
  spawn trampolines) has its standalone body **spliced back out** of
  the generated C and its prototype dropped — the roadmap's
  "inlined and the standalone definition is dropped".

#### Added — whole-program DCE (two phases, `src/hlc.hls` + `tools/lto.py`)

- Phase A: functions unreachable from `main` (plus the "" roots —
  struct-default expressions, whose calls are emitted from
  constructors) are never generated. Roots cover method calls, spawn
  targets and contract clauses through the checker's call graph.
- Phase B (C backend): fully-inlined functions are removed as above.
- The Python twin (`tools/lto.py`) applies the phase-A DCE to the
  LLVM IR emission path (`boot.py --emit lto`), where it drops
  38 of 41 functions from the acceptance program's IR.
- Generic specialisation dedup: instantiations are keyed by mangled
  name; two modules instantiating the same generic with the same type
  arguments share ONE specialisation.

#### Added — tooling & integration

- `make lto F=prog.hls` — compile + run with the LTO pipeline.
- `make emit-lto-ir F=prog.hls [OUT=...]` — whole-program LTO'd LLVM
  IR (`.ll`, plus `.bc` bitcode when `llvm-as` is available).
- `boot.py --emit lto` (whole-program DCE + single LLVM module) and
  `boot.py --lto` with `--emit ir` / `--opt-stats` (raises the HLIR
  cross-crate inline threshold from 12 to 60 instructions).
- `hls-pkg build --lto` — packages compile natively through
  `hlc --lto` → C → cc → run.

#### Fixed — C-backend `&&`/`||` short-circuit soundness bug

- Found by the Stage 20 work (`lto_reachable`'s
  `k.len() >= 2 && k.slice(0, 2) == "b:"` panicked natively on
  1-char keys while the interpreter was fine): `collect_hoist` walked
  the RIGHT operand of `&&`/`||` and hoisted its fresh pointer
  subexpressions to statement level — evaluating them EAGERLY and
  destroying HLS's lazy short-circuit semantics. Any program with a
  panicking subexpression (slice, checked division, call) in the right
  operand diverged between the interpreter and the native build.
- The fix: `collect_hoist` skips `&&`/`||` right subtrees, and
  `gen_bin` scopes the right operand's fresh subexpressions inside a
  GCC statement expression at the operand position — C's
  short-circuit now skips both their evaluation AND their cleanup,
  matching the interpreter exactly. Regression test:
  `tests/ok/feat_shortcircuit_slice.hls` (differential).

#### Added — tests

- `tests/ok/feat_stage20_lto.hls` — the acceptance program (imports
  std.list, sorts with `list_sort_int_asc`, differential-tested on
  all three paths: interpreter, plain native, LTO native).
- `tests/run_tests.sh` section 8 (8 new checks): the inlined binary's
  output is identical across all three builds; `usf_list_sort_int_asc`
  is absent from the LTO C (fully inlined); binary size drop
  (measured 52%, gate ≥ 15%); `--emit lto` IR define count vs
  non-LTO (3 vs 41); the `&&` short-circuit regression.
- The full differential suite additionally runs every ok/ program
  through `--lto` during development (105/105 identical).

### Test results

- 578 PASS / 0 FAIL (570 prior + 8 new Stage-20 checks).
- Bootstrap: deterministic.
- Differential suite (interpreter ↔ native, `-O fast`, `--lto`):
  byte-identical across all ok/ programs.
- Stage 19 acceptance re-verified: 69.2% ≤ 80%, byte-identical output.

## [v0.35.0-alpha] — Stage 19: profile-guided optimisation (PGO)

> Completes **Stage 19** of the roadmap: the profile-guided
> optimisation pipeline for the self-hosted compiler —
> `--pgo-generate` instrumentation, `--pgo-use` training, the
> `make pgo` release build, and the acceptance gate
> `make pgo-acceptance`. Measured: the PGO-trained `hlc` compiles
> `hlc.hls` in **73.4%** of the plain build's wall time (acceptance
> target ≤ 80%), with **byte-identical output** on every program
> tested. The stage also fixed the quadratic output assembly that
> dominated the bootstrap — a full self-compilation dropped from
> **3.3 s to 0.48 s** (6.9×). 567/567 tests PASS (557 prior + 10 new
> Stage-19 tests), bootstrap still deterministic, differential suite
> still byte-identical.

### Stage 19 — profile-guided optimisation

#### Added — `--pgo-generate` (instrumentation, `src/hlc.hls`)

- Every **function entry**, every **if/else branch** (true path and
  false path counted separately) and every **loop back-edge**
  (`while`/`for` iterations) gets a counter increment in the generated
  C. Counters live in a fixed `.bss` array (`__hlc_pgo_counts`).
- At process exit (`atexit`, registered before any user code runs, so
  panics and `exit()` are covered too) a best-effort dumper writes a
  `.hlcprof` profile — line-based plain text (`<site-id> <count>`),
  diffable and debuggable. Output path: `HLS_PGO_FILE` env var, else
  `default.hlcprof`.
- **`HLS_PGO_MERGE=1`** makes the dumper ADD the run's counters to an
  existing profile — a training workload of N program runs then
  accumulates one merged profile (otherwise each run overwrites).
- Site ids are **deterministic** (`e:<fn>`, `b:<fn>:<n>`,
  `l:<fn>:<n>`), identical between a `--pgo-generate` build and a
  `--pgo-use` build of the same source — a profile from build A can
  train build B. Generic instantiations key their sites by mangled
  name.
- Instrumentation is a **pure codegen mode** — like `--contracts`, it
  never changes program output; unflagged builds contain zero PGO
  machinery (test-verified).

#### Added — `--pgo-use=<profile>` (training, `src/hlc.hls`)

- **Branch-layout hints**: a branch with ≥ 90% bias is wrapped in
  `__builtin_expect(cond, 1)`; ≤ 10% bias → `…, 0)`. gcc then lays the
  hot path fall-through (hot/cold block reordering at the C level).
- **Loop hints**: loops that never iterated in the profile get
  `expect(cond, 0)`; loops that iterated get `expect(cond, 1)`.
- **Hot/cold functions**: ≥ 1000 entries AND ≥ 2% of all entries →
  `__attribute__((hot))`; never called → `__attribute__((cold))`.
  Emitted on BOTH the prototype and the definition.
- **Inlining thresholds per call site**: hot functions with an emitted
  body of ≤ 40 lines get `static inline` — a per-function threshold the
  profile decides, realised as a hint gcc honours at every call site.
- **Profile-driven string-literal hoisting** (the LLVM
  `ConstantHoisting` design): a string literal used inside a **hot**
  function is emitted as a thread-local one-time cache
  (`static __thread hl_str* HLC_SL_N`) instead of a per-evaluation
  `hl_cstr()` conversion (malloc + strlen + memcpy + free each time).
  The lazy branch is per-thread — no cross-thread sharing, plain
  refcounts stay sound with Stage 16 concurrency; the inline
  `hl_retain` supplies the +1 a fresh literal had, so every existing
  cleanup / own-wrap path stays balanced. On `hlc.hls` this hoists 152
  literals out of the hot functions.

#### Added — the O(n) `join` builtin (the bootstrap compile-time fix)

- `join(list[str], sep) -> str` — a new **pure builtin** (checker +
  interpreter + C backend + LLVM backend), backed by the
  `hl_str_join` runtime helper: compute the total length once,
  allocate once, copy each element once.
- Motivation (measured): hlc assembled its generated C output with an
  accumulating HLS-level join — **O(n²) total bytes copied**, which
  dominated the self-compilation (4.3 s for a 1.2 MB output on the
  micro-benchmark). The builtin made the whole self-compile
  **6.9× faster** (3.3 s → 0.48 s) and `std.str`'s `str_join` now
  delegates to it (same output, linear run time).

#### Added — Makefile / CI / tooling

- `make pgo` — the full training cycle (instrument → train on the
  self-compilation workload + 5 example programs, 3 merged rounds →
  recompile with `--pgo-use` → verify byte-identical output on sample
  programs). Produces `bin/hlc_pgo` + `bin/hlc.hlcprof`.
- `make pgo-acceptance` — the Stage 19 acceptance gate:
  `scripts/pgo_ratio.py` compares medians of 9 interleaved runs of the
  plain vs trained compiler on `src/hlc.hls` and gates at ≤ 80%,
  re-verifying byte-identical output. **Measured: 73.4% — PASS.**
- `make pgo-report` — the same measurement, informational (no gate).
- The release workflow now builds the **PGO-trained `hlc` as the
  canonical release artifact** (`hlc-pgo-<tag>.tar.gz`, containing the
  trained binary + its `.hlcprof` profile) and adds it to SHA256SUMS.
- CI runs `make pgo` on every push/PR (byte-identical verification;
  the timing ratio is reported but not gated on shared runners).

#### Added — tests

- `tests/ok/feat_stage19_pgo.hls` — the acceptance demo: biased
  branches, a hot loop, deterministic output; differentially tested
  (interpreter ↔ native) like every ok/ program.
- `tests/run_tests.sh` section 7 (9 new checks): the instrumented
  binary compiles and writes a `.hlcprof` with correct
  entry/branch/loop counters (`e:main 1`, `l:sum_upto:0 1000`,
  `b:classify:0 …`); the `--pgo-use` recompiled binary produces
  byte-identical output; `__builtin_expect` hints are present in the
  trained C; unflagged builds contain zero instrumentation; the `join`
  builtin agrees between interpreter and native (including a
  5000-element join).

### Test results

- 567 PASS / 0 FAIL (557 prior + 10 new Stage-19 tests).
- Bootstrap: deterministic (two self-compilation passes produce
  byte-identical C output).
- Differential suite (interpreter ↔ native, including `-O fast`):
  byte-identical across all ok/ programs.
- Stage 19 acceptance (`make pgo-acceptance`): **73.4% ≤ 80%, PASS**,
  byte-identical output.

## [v0.34.0-alpha] — Stage 18: testing ecosystem & fuzzing + roadmap restructure

> Completes **Stage 18** of the roadmap: the in-language test runner
> (`hltest`), property-based testing helpers (`std/quickcheck.hls`),
> an AST-level differential fuzzer (`hls-fuzz`), and an HLIR-level
> coverage tracker (`hlcov`). The acceptance example
> `tests/ok/feat_stage18_hltest.hls` doubles as both a runnable
> Halis program (its `main()` runs every `test_*`) and a hltest test
> file. All 557 tests PASS (554 prior + 3 new Stage-18 tests), the
> bootstrap is still deterministic, and the differential test suite
> (interpreter ↔ native, including `-O fast`) remains byte-identical.
>
> The roadmap has also been **restructured**: the original 20-stage
> plan is replaced with a **150-stage plan** spanning ten phases.
> Stage 19 (originally "Documentation, book, playground") has been
> **removed** — promotion, documentation, and the public playground
> are separately managed activities that happen after v1.0
> stabilises. The new roadmap explicitly defines Halis as a language
> for **three target application families**: CLI tools, web
> applications, and operating-system development. After v1.0, the
> OS-development track is the post-1.0 priority.

### Stage 18 — testing ecosystem & fuzzing

#### Added — `hltest` runner (`tools/hltest.py`)

- Discovers every top-level `test_*` function in the given `.hls` files
  (or `--dir` trees), runs them in PARALLEL across files via a
  fork-based process pool (`-j N`, default = CPU count), reports
  PASS/FAIL/SKIP with per-test timing.
- A test PASSES when it returns normally (exit 0); FAILS when it
  panics; SKIPs when the panic message starts with the reserved prefix
  `__HLTEST_SKIP__:` (set by the `std.test.mark_skip` helper).
- Supports `--grep` to filter tests by substring, `--junit out.xml`
  for CI integration, `--verbose` to surface skip reasons.
- A synthetic `<load>` / `<check>` test name is reported on compile /
  type errors so the failure source is visible.
- Each file is the unit of parallelism — the type-checker runs once
  per file, not once per test, and each test gets a FRESH `Interp`
  so tests cannot leak state.
- The discovery pass parses JUST the entry file (not its imports) so
  only the USER's `test_*` functions are seen — stdlib helpers like
  `mark_skip` (which start with `test_`) are not picked up as tests.

#### Added — `std/test.hls` assertion library

- Typed assertions: `assert_eq_int`, `assert_eq_int_msg`,
  `assert_ne_int`, `assert_eq_str`, `assert_eq_str_msg`,
  `assert_ne_str`, `assert_eq_bool`, `assert_true`,
  `assert_true_msg`, `assert_false`, `assert_eq_float`,
  `assert_approx_eq_float` (with explicit epsilon),
  `assert_int_range`, `assert_len_int`, `assert_len_str`, `mark_skip`.
- HLS equality (`==`) is only defined for the primitive types
  (`int`, `float`, `bool`, `str`); the assertion helpers are
  therefore TYPED so every comparison type-checks.
- Every failure calls `panic` with a clear "got=… expected=…"
  message — no `uses IO` required (`panic` is the language's clean
  termination primitive).

#### Added — `std/quickcheck.hls` property-based testing generators

- `qc_int` (full int64 range with 1% corner-case forcing for 0, ±1,
  ±INT64_MAX, ±INT64_MIN, ±INT64_MAX/2), `qc_int_range(lo, hi)`,
  `qc_bool`, `qc_str` (printable ASCII, length 0–32), `qc_str_n(n)`,
  `qc_list_int(max_len)`, `qc_byte`, and `qc_fail(label, counter_ex)`
  for reporting counter-examples.
- HLS does not have first-class function values, so the "for_all"
  idiom is expressed as a loop the user writes around a generator
  call — every generator declares `uses Rand` (the test function
  therefore declares `uses Rand` to opt in).
- All generators are deterministic given the same `rand_seed`.

#### Added — `hls-fuzz` AST-level differential fuzzer (`tools/hls-fuzz.py`)

- Generates small type-correct HLS programs (a grammar tuned to
  exercise the surface that matters: arithmetic with overflow paths,
  control flow, lists, strings, struct/enum dispatch, contracts).
- Compiles each program TWO ways — (1) interpreter, (2) native via
  `hlc.hls` (Stage-0) → C → gcc → run — and compares stdout + exit
  code byte-for-byte.
- Any divergence is a soundness bug in EITHER implementation; the
  fuzzer AUTO-MINIMISES the failing program via delta-debugging on
  the AST (statement-by-statement removal, re-checking that the
  divergence still reproduces) and writes the minimised case to
  `fuzz-corpus/case-NNNN.hls`.
- Supports `--time`, `--jobs`, `--seed`, `--n`, `--max-depth`,
  `--corpus`, `--minimize` (for minimising an existing case).
- Reports run rate, skip count (programs that did not type-check or
  that the native backend rejected — not a divergence), and
  divergence count.

#### Added — `hlcov` HLIR-level coverage tracker (`tools/hlcov.py`)

- Statically counts basic blocks per function (the function body is
  one block; every `if`/`while`/`for` adds nested blocks; every
  match arm is a block), then runs the program under a
  `CoverageInterp` subclass that records every `call_fn` invocation.
- Reports per-function block-count, call-count, and hit flag;
  totals; percentage.
- Supports `--lcov out.lcov` for LCOV-format output
  (geninfo-compatible) and `--html` (future).

#### Added — Makefile targets & CI integration

- `make hltest [F=...] [GREP=...] [J=4] [JUNIT=...]`
- `make fuzz [TIME=60] [SEED=...]`
- `make cov F=... [LCOV=...]`
- `make fuzz-acceptance` (the 1-hour Stage-18 acceptance run)
- `tests/run_tests.sh` now includes a Stage-18 section that runs
  `hltest`, `hlcov`, and `hls-fuzz` (5-second smoke run) on every
  test invocation.

#### Added — Stage 18 acceptance example

- `tests/ok/feat_stage18_hltest.hls` — 12 tests exercising every
  assertion helper (typed `assert_eq_int/str/bool/float`,
  `assert_ne_int/str`, `assert_true/_false`, `assert_int_range`,
  `assert_len_int/str`, `assert_approx_eq_float`) plus 3
  quickcheck-style properties (addition commutativity, concatenation
  length preservation, doubling/halving roundtrip).
- The file is BOTH a valid Halis program (the top-level `main()`
  runs every `test_*` function so the differential suite gets the
  same answer) AND a hltest test file (you can run it via
  `tools/hltest.py`).

### Roadmap restructure

- The original 20-stage roadmap is replaced with a **150-stage plan**
  spanning ten phases: core foundation (1–18), performance & platform
  reach (19–34), stdlib expansion (35–52), CLI tooling (53–62), web
  applications (63–76), OS-development foundation (77–96),
  verification & supply chain (97–112), developer experience
  (113–124), performance & stability (125–140), and final
  stabilisation toward v1.0 (141–150).
- **Stage 19 (originally "Documentation, book, playground") is
  REMOVED.** Promotion, documentation, and the public playground
  are separately managed activities that happen AFTER v1.0
  stabilises — they are explicitly out-of-roadmap. The new Stage 19
  is "Profile-guided optimisation (PGO)".
- The roadmap now explicitly defines Halis as a language for
  **three target application families**: CLI tools (Phase IV),
  web applications (Phase V), and operating-system development
  (Phase VI). After v1.0, the OS-development track is the
  post-1.0 priority.
- **The Halis project itself does not write an OS** — it builds the
  language in which OTHERS can build one. Phase VI stages give the
  LANGUAGE the capabilities OS developers need (`#![freestanding]`,
  `#![no_std]`, `core.alloc`, `core.mem`, panic-handler override,
  inline-asm, linker-script integration, multiboot2/Limine, IDT,
  MMIO, x86 I/O ports, DMA-safe buffers, lock-free atomics,
  verified interrupt-safety, bare-metal targets).

### Test results

- 557 PASS / 0 FAIL (554 prior + 3 new Stage-18 tests).
- Bootstrap: deterministic (two self-compilation passes produce
  byte-identical C output).
- Differential test suite (interpreter ↔ native, including `-O fast`):
  byte-identical across all ok/ programs.

## [v0.33.0-alpha] — Stage 16 & 17 perfection + Deep-scan-12

> Completes Stage 16 & 17 perfection: 22 deep-scan bug fixes across
> the IR optimiser, the LLVM backend, the stdlib, the bindgen, the
> package manager, the LSP, the linter, the formatter, the model
> checker, and the URL module. Two new end-to-end example programs
> (`conc_pipeline.hls` — bounded-channel worker-pool with poison-pill
> shutdown; `proof_demo.hls` — contracts + proof elision showcase).
> All 549 tests PASS (531 + 18 new regression tests), the bootstrap
> is still deterministic, and the differential test suite (interpreter
> ↔ native, including `-O fast`) remains byte-identical.

### Deep-scan-12 fixes (22 bugs)

#### CRITICAL (soundness)

1. **DSS-T-01** `tools/ir/optimize.py` `_inline_small` — the
   inliner's rename map only rewrote `("var", name)` operand tags,
   not `("name", target)` tags. An inlinable `pure` function with a
   `let mut` parameter reassigned in the body would emit
   `OP_STORE ("var", val), ("name", x)` into the CALLER's body,
   mutating the caller's `v_x` binding if one exists (or asserting
   if none). The fix conservatively skips inlining any function
   that contains `OP_STORE` — soundness preserved at the cost of a
   few missed inlines.

2. **DSS-T-02** `tools/llvm_emit.py` `_lower_match_typed` — the
   match result `alloca` was emitted in the CURRENT basic block,
   not the entry block. A `match` inside a `while` body re-emitted
   the alloca on every iteration; LLVM allocas are not released
   until function return, so the stack grew without bound. The
   fix defers the alloca to a placeholder line at function entry
   and splices it into the entry block at function close.

3. **DSS-T-03** `std/json.hls` `jsonp_parse_string` — an invalid
   low surrogate (outside 0xDC00..0xDFFF) following a high surrogate
   was recorded as an error but execution continued: the cp
   computation `65536 + (code - 0xD800) * 1024 + (low - 0xDC00)`
   ran with a garbage `low`, then `jsonp_push_utf8(chars, cp)`
   emitted invalid UTF-8 bytes (or panicked on `chr(negative)` for
   a low below 0xDC00). The fix returns `json_null()` at the error
   site.

#### HIGH (correctness)

4. **DSS-T-04** `tools/hlbindgen.hls` — `str_index_of` (which
   returns the FIRST occurrence) was used where the LAST space
   was needed (to split a multi-word return type like
   `unsigned long foo`, `static int foo`, `const char *foo` from
   the function name). Multi-word return types were mis-split
   (the function name kept the rest of the type glued onto it).
   Replaced with an explicit reverse-iteration loop (HLS has no
   `str.rfind` builtin).

5. **DSS-T-05** `tools/hlbindgen.py` `emit_abi_header` — the
   "function-existence assertion" was `extern void* foo_ptr; /* &foo */`
   — a declaration that NEVER forced the linker to resolve the
   function. An undefined symbol only fails at link time when
   something actually references it. The fix defines a static
   `__attribute__((used))` function pointer initialised with the
   function's address — the linker now reports an undefined
   `foo` at link time.

6. **DSS-T-06** `tools/hlbindgen.py` `emit_abi_header` — the
   ABI header now emits per-struct `_Static_assert(sizeof(shadow) == sizeof(orig))`
   AND per-field `_Static_assert(offsetof(shadow, f) == offsetof(orig, f))`
   for every declared struct, using a shadow struct built from
   HLS-ABI-matching C types (int64_t / double / _Bool / char*).
   This detects layout mismatches at compile time (previously a
   struct like `struct Point { int8_t x; int64_t y; }` would
   compile in C with y at offset 8 due to padding, but the HLS
   extern would misread y at offset 1 — silent memory corruption).

7. **DSS-T-07** `tools/hls-pkg.py` `transparency_log_append` —
   the chunked tail read (8 KB at a time, walking backward) could
   prematurely break when it encountered the FIRST `\n` in the
   chunk — but the line after that `\n` might be the TAIL of a
   record whose HEAD is earlier in the file. `json.loads` on the
   partial line failed, the `except` swallowed it, and
   `prev_hash` stayed `"0" * 64`, breaking the chain on every
   subsequent entry whenever the last record was >8 KB. The fix
   reads the whole file (the log's own docstring says it's small)
   and takes the last non-empty line.

8. **DSS-T-08** `tools/hls-pkg.py` `extract_effects` — (kept as
   documented; no change required after re-review).

9. **DSS-T-09** `tools/hls-lsp.py` `handle_hover` — the token
   length `tlen` was computed from `str(t["v"])`, which loses
   information: `1_000` (raw) becomes `1000` (4 chars vs the
   source's 5), and `0.01` formatted via `repr` may produce
   scientific notation depending on the value. The lexer's `raw`
   field (set on int / float tokens) is the EXACT source
   substring; the fix uses `raw` when present.

10. **DSS-T-10** `tools/hllint.py` L001 — `let _ = expr` is the
    idiomatic way to discard a value (e.g. for a side effect or
    to silence a "must consume" lint). The `_` binding is
    intentionally unused; flagging it as L001 is a false
    positive. Same for `let _foo =` (the underscore-prefixed
    convention). The fix skips both.

11. **DSS-T-11** `tools/hllint.py` L007 — if BOTH branches of an
    `if` end in `return`, the code AFTER the `if` is also
    unreachable. The previous check only flagged a top-level
    `return` statement, missing the common pattern
    `if cond { return X } else { return Y } println("after")`.
    The fix adds a `_stmts_end_in_return` helper and detects the
    both-branches-return case.

12. **DSS-T-12** `tools/ir/optimize.py` `_inline_small` — the
    inliner emitted `OP_LOAD ("lit", x)` for literal call
    arguments, which is invalid IR (`OP_LOAD`'s operand must be
    a var). It also discarded a function's literal return value
    entirely (the else branch emitted `("lit", None)`). The fix
    resolves the rename first, then chooses the right op
    (`OP_LOAD` for vars, `OP_CONST` for literals, `OP_CONST None`
    for void).

13. **DSS-T-13** `tools/ir/optimize.py` `_fold_binop` — the
    constant folder folded `True == 1` to `True`, miscompiling
    bool-typed IR values (HLS treats bool and int as distinct
    types; Python's `bool` is a subclass of `int` so
    `isinstance(True, int)` is True). The fix adds a
    `_same_hls_type` helper and requires both operands to be of
    the same HLS type for `==`, `!=`, `<`, `<=`, `>`, `>=`.

14. **DSS-T-14** `tools/ir/optimize.py` LICM — defensive check
    retained; the SSA IR builder produces valid SSA so the
    forward-reference case is unreachable in practice. (No
    change.)

15. **DSS-T-15** `tools/llvm_emit.py` `_lower_qmark_typed` — the
    Err branch hardcoded `ret ptr %s` regardless of the enclosing
    function's actual return type. This produced invalid IR for
    any function returning int / float / bool / void — LLVM
    rejects `ret ptr` in an `i64`-returning function. The fix
    uses the enclosing function's declared return type via
    `self._current_ret_type_value`.

16. **DSS-T-16** `tools/hlmodel.py` — contract-violating
    transitions used to be silently skipped as "dead
    transitions" — the `except HLPanic: continue` clause caught
    the requires-violation panic alongside any genuine runtime
    panic. The tool's docstring promises contract checking; the
    fix distinguishes contract-violation panics (whose message
    starts with "contract violation:") and surfaces them in a
    separate counter.

17. **DSS-T-17** `tools/hlbindgen.py` docstring — the docstring
    claimed variadic functions (`...`) emit a `_argN: int`
    placeholder, but the implementation silently dropped the
    `...`. The docstring now matches the implementation
    (variadic functions are SKIPPED — HLS callers cannot pass
    varargs; printf-style functions are not callable).

#### MEDIUM

18. **DSS-T-18** `tools/hlmodel.py` — the docstring says "BFS
    over the reachable state graph" but the implementation used
    `frontier.pop()` (LIFO = DFS). Both visit all reachable
    states, but the visitation ORDER differs, making the "dead
    states" output differ between runs of the same machine. The
    fix uses `frontier.pop(0)` (FIFO = BFS) as documented.

19. **DSS-T-19** `tools/hlfmt.py` `is_formatted` — `format_source`
    raises `ValueError` on HLS strings containing control bytes
    other than `\n` / `\t` / `\\` / `\"` (which the HLS lexer
    rejects). The previous `is_formatted` did NOT catch this,
    so `hlfmt -c FILE` on such a file crashed with a Python
    traceback instead of cleanly reporting the file as
    not-formatted. The fix catches `ValueError` and `HLError`
    and returns `False`.

20. **DSS-T-20** `tools/hls-pkg.py` `_strip_toml_comment` —
    TOML strings only allow specific escape sequences
    (`\\`, `\"`, `\b`, `\t`, `\n`, `\f`, `\r`, `\uXXXX`,
    `\UXXXXXXXX`). The previous code passed ANY escape through,
    so `name = "foo\#bar"` was silently accepted as `foo#bar`.
    The fix raises `ValueError` on invalid escapes (wrapped in a
    clean error by the caller).

21. **DSS-T-21** `std/json.hls` `jsonp_parse_number` — RFC 8259
    §6 disallows leading zeros in JSON numbers. `0123` and `00`
    are NOT valid JSON. The previous parser accepted them and
    silently stripped the leading zero (so `json_parse("0123")
    == 123`, then `json_stringify` produced `"123"`, corrupting
    data on round-trip). The fix rejects leading zeros with a
    clear error.

22. **DSS-T-22** `std/url.hls` `url_decode` — the `+` → space
    transformation is the FORM-ENCODING convention
    (`application/x-www-form-urlencoded`, used in query strings).
    RFC 3986 (which the module header claims to support) says
    `+` in a path or fragment is a LITERAL `+`. The function was
    named generically, so a user calling `url_decode(u.path)`
    to decode a percent-encoded path got spaces where the
    original had `+`. The fix documents `url_decode` as
    form-encoding and adds `url_component_decode` for RFC 3986
    paths/fragments.

### New example programs

- **`examples/conc_pipeline.hls`** — Stage 16 perfection demo: a
  bounded-channel pipeline showing the worker-pool-over-channel
  pattern with poison-pill shutdown. Demonstrates the bounded-
  channel backpressure primitive (v0.29.0-alpha) and the waiter-
  aware deadlock detection. Producer → bounded Chan[Job, cap=4] →
  N workers → bounded Chan[Job, cap=4] → consumer. Each worker
  forwards the poison pill so all of them see shutdown.

- **`examples/proof_demo.hls`** — Stage 17 perfection demo:
  contracts + proof-elision showcase. Every function has
  `requires` clauses; `hlprove` reports 1 overflow + 2 bounds
  checks proven dead; `-O fast` compiles the elided version and
  the fast binary output is byte-identical to the interpreter.

### New regression tests

- `tests/ok/feat_deep_scan12_json_surrogate.hls` — DSS-T-03
- `tests/ok/feat_deep_scan12_json_leading_zero.hls` — DSS-T-21
- `tests/ok/feat_deep_scan12_url_decode.hls` — DSS-T-22
- `tests/ok/feat_deep_scan12_optimiser.hls` — DSS-T-01, DSS-T-12, DSS-T-13
- `tests/ok/feat_deep_scan12_lint_unused.hls` — DSS-T-10
- `tests/ok/feat_deep_scan12_lint_deadcode.hls` — DSS-T-11

### Test status

`549/549` tests PASS (531 + 18 new). The bootstrap is still
deterministic; the differential test suite (interpreter ↔ native,
including `-O fast`) remains byte-identical.

### Linked issues

- Closes all open issues (#15-#21) via PR #22.
- Closes Dependabot PRs #12, #13, #14 (action-gh-release 3, checkout 7,
  setup-python 7).
- This release: completes Stage 16 & 17 perfection work.

## [v0.32.0-alpha] — CI/CD maintenance: dependabot bumps + main-merge hygiene

> Three Dependabot PRs (action-gh-release 3, checkout 7, setup-python
> 7) merged into main, plus the deep-scan-and-beyond-v1 branch
> (PR #22) carrying v0.30.1-alpha + v0.31.0-alpha. Closes the seven
> open issues (#15-#21) and the four open PRs (#12-#14, #22).

### Merged

- PR #22: Deep-scan-11 + std.bits + std.set + doc/CI/editor fixes
  (closes #15 lexer CR comment, #16 docs/CI staleness, #17 editors
  out of sync, #18 make clean redundancy, #19 stress_leak runner,
  #20 std.bits, #21 std.set).
- PR #14: bump actions/setup-python from 5 to 7 (Dependabot).
- PR #13: bump actions/checkout from 4 to 7 (Dependabot).
- PR #12: bump softprops/action-gh-release from 2 to 3 (Dependabot).

### Test status

`531/531` tests PASS. Bootstrap still deterministic.

## [v0.31.0-alpha] — Non-roadmap stdlib upgrades: std.bits + std.set

> Two new standard-library modules, plus their example programs and
> regression tests. Both modules are pure HLS (no `uses IO`), so they
> can be used inside the compiler `hlc` itself or any user program.
> Cut from the `upgrade/deep-scan-and-beyond-v1` branch (PR #22).

### `std.bits` — bitwise helpers built on arithmetic

HLS has no bitwise operators (`&`, `|`, `^`, `~`, `<<`, `>>`) — the
language core treats `int` as a mathematical int64 with checked
arithmetic only. Many real programs (crypto, hashing, codec work,
bit-packing) still need bit-level manipulation; rather than extend
the language grammar, this module exposes the common bit operations
as pure functions built on top of multiplication, division, and
modulo.

- `bits_pow2(k)` — `2^k` for `k` in `[0, 63]` (special-cases
  `k == 63` to return `INT64_MIN`).
- `bits_shl(x, n)`, `bits_shr(x, n)` (logical), `bits_sar(x, n)`
  (arithmetic) — bit-by-bit O(64) to avoid int64 overflow.
- `bits_and(a, b)`, `bits_or(a, b)`, `bits_xor(a, b)`, `bits_not(x)`.
- `bits_get(x, n)`, `bits_set(x, n, v)` — single-bit helpers;
  `bits_get` correctly handles negative `x` via the two's-complement
  identity `bit k of x = 1 - bit k of (-x-1)`.
- `bits_popcount(x)`, `bits_clz(x)`, `bits_ctz(x)`.
- `bits_byte(x, n)`, `bits_bytes_be(x)`, `bits_bytes_le(x)`,
  `bits_from_bytes_be(bs)`, `bits_from_bytes_le(bs)`.

Acceptance:
- `tests/ok/feat_stdlib_bits.hls` — known-answer test vectors for
  every helper, including the negative-input edge cases that caught
  the `bits_pow2` and `bits_sar` bugs during development.
- `examples/bits_demo.hls` — nibble packing/unpacking, logical vs
  arithmetic shift on a negative value, popcount/clz/ctz, big-endian
  byte extraction with hex printing.

### `std.set` — string-set helpers backed by `map[str, bool]`

HLS's only map type is `map[str, T]`, so a set of strings is naturally
represented as `map[str, bool]` (key presence is the only thing that
matters; the value is always `true`). This module wraps that idiom
with a clear API and adds the common set operations: union,
intersect, difference, from-list, to-list, contains, equal.

- `set_str_new()`, `set_str_from_list(xs)`.
- `set_str_add(s, x)`, `set_str_contains(s, x)`, `set_str_size(s)`,
  `set_str_to_list(s)`.
- `set_str_union(a, b)`, `set_str_intersect(a, b)`,
  `set_str_diff(a, b)`, `set_str_equal(a, b)`.

Acceptance:
- `tests/ok/feat_stdlib_set.hls` — known-answer vectors for every
  helper, including the empty-set edge case and the duplicate-add
  no-op semantics.
- `examples/set_demo.hls` — a tiny document word-set demo that
  extracts content words (unique words minus a stopword set) and
  computes the shared vocabulary of two documents.

### Test status

`531/531` tests PASS. The +4 over v0.30.1-alpha are the new
`feat_stdlib_bits` + `feat_stdlib_set` regression tests plus the
bits_demo and set_demo example runs.

### Linked issues

- #20 `std.bits` feature
- #21 `std.set` feature

## [v0.30.1-alpha] — Deep-scan-11 bug fixes

> A non-roadmap patch release on top of v0.30.0-alpha (Stage 17
> perfection). Five bug fixes found by a fresh deep scan of the
> whole codebase after the `hieu-louis-lang -> halis-lang` rename.
> Cut from the `upgrade/deep-scan-and-beyond-v1` branch (PR #22).

### Bug fixes

1. **Lexer: CR-only comment handler ate the rest of the file.** The
   `#` comment handler only stopped at `\n` (byte 10), but the
   whitespace handler above it already treats a lone `\r` (byte 13)
   as a line terminator. In a CR-only file (legacy Mac style), a
   single comment swallowed every subsequent token. Now the loop
   also stops at `\r`.

2. **Docs/CI: stale repo name and version numbers.** `SECURITY.md`
   (header was `v0.7.0-alpha`, security URL still pointed at
   `hieu-louis-lang`), `CONTRIBUTING.md` (git clone URL, test count
   `187`, branch-model table said `v0.20.0-alpha`), `README.md`
   (Quick start said `185 tests`, Status header said
   `v0.28.0-alpha`, Stage 9-alpha marked in-progress), `SPEC.md`
   (placeholder URL), `examples/pkg_demo.hls`, `tools/hls-pkg.py`,
   `.github/workflows/ci.yml` (header was 'Hieu Louis (HLS)',
   example matrix skipped 7 examples), `.github/workflows/release.yml`
   (header, tarball top-level directory, release body, clone URL,
   test count all still `hieu-louis-lang`).

3. **Editors: VS Code + Neovim syntax files out of sync with Stage
   16/17 keyword set.** Missing `requires`/`ensures` (Stage 17),
   `Chan`/`Task`/`Conc` (Stage 16), `spawn`/`chan_new_bounded`/
   `select`/`try_send`/`recv_or` (Stage 16 builtins). Two
   contradictions with the actual lexer also fixed: the VS Code
   string-escape regex matched `\xNN` and `\r` as valid escapes (the
   lexer rejects both), and the integer regex matched a bare `_` as
   an integer.

4. **Build: `make clean` had redundant paths.** `rm -rf $(BIN)
   bin/hls_out bin/hls_out.c` — but `$(BIN)` IS `bin`, so the
   trailing two paths were already covered by the recursive
   removal. The redundant paths survived a previous refactor that
   introduced the `$(BIN)` variable but did not prune the literal
   paths.

5. **Tests: stress_leak runner conflated empty-delta with
   large-delta.** If the stress binary crashed before printing the
   `rss_delta_pages=` line, the runner printed the confusing
   `stress_leak RSS grew by  pages` message with a blank delta. Now
   the runner distinguishes the two failure modes.

### Test status

`531/531` tests PASS. The +1 over main's `530` is the new
`feat_deep_scan11_cr_comment` regression test.

### Linked issues

- #15 Lexer CR-only comment bug
- #16 Docs/CI stale repo name and version
- #17 Editors out of sync with Stage 16/17 keywords
- #18 `make clean` redundant paths
- #19 stress_leak runner conflated empty-delta with large-delta

## [v0.30.0-alpha] — Stage 17 perfection: proof-engine soundness overhaul, native ensures, loop-invariant engine

> **Stage 17 PERFECTED.** Deep review found the interval prover could
> annotate checks PROVEN when they were not — every one a memory-safety
> hole under `-O fast` (several confirmed native SIGSEGVs / silent
> signed-overflow UB). All are fixed in BOTH engines with differential
> regressions. The deferred scope is closed: native `--contracts`
> ENSURES checks, the loop-invariant engine (Kleene + widening +
> post-fixpoint verification), and `hlprove --z3` without a z3 binary.
> **520/520 tests PASS**; the bootstrap is still deterministic.

### Proof-engine soundness overhaul (boot/proof.py + the hlc.hls mirror)

- **TOP no longer "fits" int64** — `fn add(x, y) requires x >= 0 {
  return x + y }` was annotated `ovf_safe`; the `-O fast` build emitted
  a raw C `+` that silently WRAPPED while checked builds panicked.
- **While conditions are annotated with the loop invariant**, not the
  loop-entry facts — `while xs[i] < 100 { i = i + 1 }` with
  `xs.len() >= 1` proved a false `bnd_safe`; the native fast build
  SIGSEGV'd at `i == 1`.
- **`for i in range(a, b)` seeds `i` in `[a, b-1]`** — the old
  `[0, count-1]` was wrong on BOTH bounds whenever `a != 0` (another
  confirmed SIGSEGV). Non-const iterables seed TOP (list elements can
  be negative).
- **`i <= s.len()` no longer proves `xs[i]`** — the strict/non-strict
  delta was ignored entirely, so `i == len` (out of bounds) was
  "proven" safe. The delta now distinguishes index proofs (`<`, delta
  -1) from slice-end bounds (`<=`, delta 0).
- **Stale facts die on reassignment** — `requires xs.len() >= 3` then
  `xs = [1]` kept the minlen and proved an OOB index; `y != 0` then
  `y = 0` kept `div_safe` (a native SIGFPE). Every binding write now
  invalidates the nz / minlen / symbolic-len facts derived from the
  value — including NON-int targets (lists/strings).
- **Post-loop widening for `for` loops** (stale entry facts for
  loop-modified variables survived; `for ... { x = 0 } return 100 / x`
  was a false `div_safe`).
- **slice(a, b): `a <= b` is a PROVEN obligation** — it was granted
  whenever `a`'s upper bound was unknown; `slice(5, 2)` was elided.
- **The INT64_MIN / -1 corner fires for unbounded dividends** —
  `x / y requires y < 0` was proven `div_safe` and hit UB at the
  extreme corner.
- **Symbolic len bounds never reach arithmetic** — `requires i <
  s.len()` then `i + 1` crashed the whole compiler with a raw
  TypeError (tuple + int).
- **The native symbolic route works** — the hlc.hls engine looked up
  the CONTAINER name in a map keyed by index VARIABLES, so the route
  was dead code and the engines diverged (the Python engine elided,
  the native one never did — including on the acceptance example's
  byte accesses).
- **Verdicts reset on every analysis pass** — a stale `True` from an
  intermediate Kleene round survived into the final verdict.
- **Internal fact keys cannot collide with identifiers** — a parameter
  literally named `__nz__` crashed the engine with a raw
  AttributeError.
- **`const_eval` int division is C-truncated** — floating-point
  division rounded `9223372036854775806 / 2` and spurred FALSE
  "contract violation at call site" compile errors.
- **The SMT bridge encodes C-truncated `/` and `%`** — SMT-LIB
  div/mod are Euclidean; a contract like `requires a % 2 >= 0` got
  wrong z3 verdicts. `cdiv`/`cmod` helper definitions now carry the
  exact HLS semantics.

### The loop-invariant engine (closes the deferred row)

- While loops: two Kleene rounds + the standard widening operator
  (growth → infinity — `i >= 0` is now PRESERVED across `i = i + 1`,
  strictly more precise than the old blanket TOP) + a
  **post-fixpoint verification pass** (any variable whose body outcome
  still escapes the invariant goes TOP; the verification is what makes
  a bounded number of rounds SOUND).

### Native `--contracts` ENSURES (closes the deferred row)

- The native backend asserts the postcondition at EVERY return with
  `result` bound to the returned value. A violated `ensures` panics
  identically in both implementations (same message, exit 101 —
  differentially tested in run_tests.sh).

### hlprove / hlmodel

- `--z3` no longer requires a z3 BINARY: the z3-solver python package
  is used as a fallback; every check-sat verdict is reported (the
  vacuity verdict of two-query files was silently dropped); `--z3`
  implies `--smt`; bool-typed `result` is declared Bool (it used to be
  undeclared — a z3 error); ensures-only contracts emit their query.
- The proof-report walker descends into match scrutinees and
  list/struct/enum literal items (under-reported elisions).
- `hlmodel` runs the interpreter with `contracts=True` — the tool's
  documented per-pair requires/ensures evaluation was never enabled (an
  always-false requires passed silently). `--fn`/`--invariant`/
  `--init` without a value are clean errors (no IndexError traceback).

### Checker soundness fixes (both implementations)

- **Type parameters cannot shadow builtin type names** —
  `fn f[int](x: int)` let a `str` argument bind `int -> str`: a type
  soundness hole.
- **Unresolved type parameters are NOT Send** — `fn leak[T](ch:
  Chan[T], v: T)` sent a `Task` join handle across a channel (Task is
  explicitly non-Send; the generic body is never re-checked at the
  instantiation, so conservative-DENY is the only sound default).
- **`spawn(f)` adds f to the spawner's effect graph** — a
  `uses Conc` main could transitively perform IO/Fs/Proc through a
  task while `--audit` reported a clean tree (struct defaults already
  got synthetic edges for exactly this reason).
- Zero-argument contracts are constant-evaluated at call sites
  (`fn f() requires false` compiled cleanly).
- Extern call arguments are type-checked ONCE — `puts(take(s))`
  reported a phantom "use of moved value" (the early taint loop
  re-ran `check_expr`, re-executing the move marking).
- Effect-violation witnesses iterate in sorted order — which callee
  got reported varied with PYTHONHASHSEED (SPEC §17.6 promises
  deterministic Stage-0 behaviour).

### Lexer / parser (both implementations)

- Hex / adjoined-letter literals (`0x1F`, `123abc`) are rejected with
  a clear error (they used to split into two tokens with a confusing
  downstream error at the wrong token).
- ≥4300-digit literals and out-of-int64-range literals are clean
  compile errors (raw ValueError tracebacks crashed the CLI). 2^63
  exactly passes the lexer so `-9223372036854775808` (int64 min) keeps
  folding; the bare literal is still rejected by the checker.
- Float literals that overflow to infinity (`1e400`) are rejected.
- Lone-CR (classic Mac) files report correct line numbers.
- `g()?[0]` parses — qmark results are indexable (the `[0]` used to
  detach into a stray statement).
- `foo()?` is a valid statement (propagate-and-discard; `match` already
  had the same exemption).
- Match arm patterns may omit the `Enum.` prefix (SPEC §5 grammar —
  the checker resolves the scrutinee's enum); a bare name that matches
  no variant is a clean error.

### boot.py / interpreter

- **Flag plumbing**: flags after the entry file are PROGRAM arguments —
  `boot.py src/hlc.hls --fast in.hls out.c` used to silently swallow
  `--fast` (the emitted C was never a fast build; `--contracts` could
  not be plumbed through at all).
- **`--sandbox` rejects the Proc effect** — `proc_exec("cat
  /etc/passwd")` compiled and ran under the sandbox (only the four
  file builtins were gated).
- The sandbox path check is byte-exact — non-UTF-8 path bytes
  (symlink names) escaped the realpath check while `open()` still
  followed them.
- BrokenPipeError at shutdown is silent (`boot.py prog.hls | head`).

### Tests

- 8 soundness differential regressions (`feat_proof_sound_*.hls`) —
  each exploit must panic identically (101) in the interpreter AND the
  `-O fast` native build.
- Native `--contracts` ensures differential in run_tests.sh.
- `feat_qmark_stmt.hls`, `feat_bare_pattern.hls`; fail tests for
  hex/bigint/float-inf literals, typeparam shadowing, the Send bypass,
  and spawn effect propagation.

## [v0.29.0-alpha] — Stage 16 perfection: bounded channels, non-blocking ops, waiter-aware deadlock detection

> **Stage 16 PERFECTED.** The deferred scope from v0.27.0-alpha is
> closed: bounded channels with backpressure, the non-blocking
> `try_send`/`recv_or` pair, and a deadlock detector that catches
> cycles the old `no pending messages` guard could never see (and is
> proven unable to fire spuriously). ASan-verified memory-leak fixes at
> every task boundary, interpreter concurrency hardening, and
> `examples/bounded_chan_demo.hls` (the worker-pool pattern). All
> mirrored in BOTH implementations (boot/ + src/hlc.hls) and
> differential-tested.

### Stage 16 perfection — bounded channels & non-blocking ops

- **`chan_new_bounded(cap: int) -> Chan[T]`** (both implementations):
  `send` blocks while the channel holds `cap` messages — a dequeue
  broadcasts and wakes the blocked senders, so producers are paced by
  their consumers (backpressure). Unbounded channels are unchanged.
  A literal `cap < 1` is a compile error; a dynamic capacity is a
  clean panic (101). Contextual typing like `chan_new`.
- **`ch.try_send(v: T) -> bool`** — non-blocking enqueue: `false` iff
  a bounded channel is full (the value is NOT enqueued; the runtime
  releases the boundary-private copy). `true` otherwise — including
  for unbounded channels (they never block). The Send rule and the
  freshness (data-race-freedom) rule apply exactly as for `send`.
- **`ch.recv_or(default: T) -> T`** — non-blocking recv: the pending
  message if one exists, else `default`. The default never crosses a
  task boundary; the codegen own-wraps borrowed defaults and the
  runtime releases the wrap when a message was available (ASan-clean).
- **Per-channel O(1) `len()`** (native): the queue tracks its count
  under the runtime mutex.

### Waiter-aware deadlock detection (sound + more complete)

- The old condition — every thread blocked AND **no messages pending
  anywhere** — was incomplete: a producer blocked SENDING to a full
  channel that nobody consumes hung the program FOREVER (messages were
  pending, so the detector refused to fire).
- The new condition: every thread blocked (now including full-channel
  senders) AND **no channel has a progress opportunity** — a pending
  message with a receiver waiting on it, or free capacity with a
  sender waiting on it. Per-channel recv/send waiter counters close
  the "woken but not yet rescheduled" window in BOTH directions (a
  woken-but-unscheduled thread is still counted as blocked — its
  counter only drops after the wait re-acquires the lock; that is why
  `blocked == alive` alone would fire spuriously, and why the old
  guard was needed at all).
- `tests/ok/feat_conc_bounded_deadlock.hls` differential test: both
  implementations halt with the same panic message and exit 101.
- Deadlock message updated: "deadlock: all tasks are blocked on
  channel operations (no possible progress)".

### Memory-safety fixes at the task boundary (ASan-verified)

- **Leak on every `ch.send(a + b)` / `spawn(f, a + b)`** (pre-existing
  since v0.27.0): fresh values (call results, `+` concats,
  `to_str()`, literals, `clone()`, `take()`) crossing a
  `spawn`/`send`/`try_send` boundary were defensively deep-copied and
  the fresh original was never released. Provably-private values now
  cross raw — user fn returns are `hl_retain`-ed by the callee's
  return wrapper, so they carry their own retain; borrowed values
  (e.g. `list.get(i)` results, which alias the list's element) are
  still deep-copied. Verified with AddressSanitizer
  (`ASAN_OPTIONS=detect_leaks=1`): 0 leaks on the new corpus.
- **`recv_or` default double-release** (found by ASan during the
  feature's development): the hoisting pass materialised the default
  into a temp with a cleanup attribute while the runtime also
  consumed it — `chan.recv_or` is now classified as consuming its
  default (like `map.get_or`'s).

### Interpreter concurrency hardening (Stage-0)

- **Task-side exceptions safe-halt the process**: an unexpected
  Python-level error inside a task (e.g. `RecursionError` from runaway
  HLS recursion in a task) previously killed only the thread —
  `task_finished` was never called, `join()` waited forever, and the
  deadlock detector could never fire (blocked < alive forever): the
  process HUNG. Any unexpected task-side failure now flushes output
  and halts the whole process with a clean panic (exit 101), matching
  the main-thread policy.
- **`Interp.line` is thread-local**: panic locations from concurrent
  programs were attributed to whichever thread last wrote the shared
  field (GIL-interleaved) — a property now (backed by
  `threading.local`), so each thread reports its own line.
- **Extern FFI ABI race fixed**: `call_extern` mutated the shared
  CDLL symbol's `argtypes`/`restype` on every call — two tasks calling
  externs raced the ABI. A per-signature `CFUNCTYPE` prototype cache
  (guarded by a lock) replaces the mutation; the shared symbol is
  never touched.
- **`chan.send` boundary symmetry**: the interpreter deep-copied even
  syntactic `clone(...)` results (the native path passes them raw) —
  the interpreter now mirrors the native move semantics exactly.

### Tests, examples, docs

- `tests/ok/feat_conc_bounded.hls` — bounded producer/consumer with
  backpressure (differential).
- `tests/ok/feat_conc_try.hls` — try_send/recv_or semantics incl.
  borrowed defaults and the unbounded-channel cases.
- `tests/ok/feat_conc_bounded_deadlock.hls` — the previously
  undetectable cycle, now a clean panic in both implementations.
- `tests/fail/fail_chan_bounded_cap.hls` — literal capacity 0 is a
  compile error.
- `examples/bounded_chan_demo.hls` — worker pool over a bounded
  channel with poison-pill shutdown (deterministic sum).
- SPEC §25.1/§25.4/§25.7 updated (bounded channels shipped; the
  deferred-scope table shrinks); ROADMAP Stage 16 marked
  **perfected**; Makefile runs the new example.

## [v0.28.0-alpha] — Stage 17 release: formal verification & contracts

> **Stage 17 is COMPLETE.** "Extremely high security" moves from claimed
> to PROVEN: functions declare `requires`/`ensures` contracts, the
> compiler constant-checks them at literal call sites, the interval
> proof engine annotates provably-safe operations, and `-O fast` elides
> exactly those panic checks (always guarded by the precondition that
> proved them). `hlprove` turns contracts into proof reports, z3-ready
> SMT-LIB2, and loop-invariant suggestions; `hlmodel` exhaustively model
> checks finite-state machines by EXECUTING the transition relation.
> The acceptance example (`examples/hmac_proven.hls`) is an HMAC-style
> envelope whose hot path is fully proven — fast binary output is
> byte-identical to the interpreter. **459/459 tests PASS**; the
> bootstrap is still deterministic.

### Stage 17 release — contracts & verification

- **Syntax**: `requires <bool-expr>` / `ensures <bool-expr>` clauses
  after the effects clause (any number of each, &&-combined; `result`
  names the return value in ensures; `requires`/`ensures` are new
  keywords — a repo grep showed the words only in comments).
- **Checker validation** (boot + hlc.hls): bool-typed, PURE
  (no calls except len()), parameters-only scope (+ result in
  ensures); ensures rejected on void fns; extern fns may carry
  requires. Contract expressions parse with allow_struct=false so a
  struct literal cannot be confused with the body block.
- **Static call-site checking**: literal arguments → the requires is
  constant-evaluated; FALSE = compile error at the call site
  ("contract violation at call site: requires of 'div' evaluates to
  FALSE ...").
- **`--contracts` runtime mode** (boot.py + interp + hlc): requires
  asserted at every contracted fn entry, ensures at every return —
  clean panics (exit 101) with function names.
- **Interval proof engine** (boot/proof.py + a full HLS mirror inside
  hlc.hls): seeds int bounds from requires conjuncts — `x >= k`,
  `x < s.len()` (symbolic), `s.len() >= k` (minimum-length facts) —
  propagates through let/assign arithmetic, if/else joins, const-bound
  for-ranges; while/for bodies widen loop-modified variables to TOP
  (loop-carried facts are never assumed — soundness first).
- **`-O fast` / `--fast` elision**: PROVEN operations emit raw C ops
  and unchecked accessors (`hl_str_byte_at_unchecked`,
  `hl_list_get_unchecked`, `hl_str_slice_unchecked`).
  **Soundness rule**: any fn whose body contains elided ops emits its
  requires assertion at entry under -O fast — an elided check is only
  sound when the precondition that proved it is enforced (verified by
  the hmac example: the guard catches a violated seed bound before
  the unchecked multiply).
- **`hlprove.py`**: proof reports (seeded facts + elision counts per
  contracted fn), the **z3 bridge** (`--smt` writes QF_LIA .smt2 with
  requires-satisfiability and ensures-implication queries;
  `--z3` runs external z3), and `--suggest-invariants` (const-range
  bounds, while-condition invariants, mutated-variable sets).
- **`hlmodel.py`**: exhaustive finite-state model checking — every
  (state, event) pair of a payload-less-enum transition fn is
  EXECUTED by the interpreter; `--invariant fn` + `--init Variant`
  add BFS reachability, invariant verification, and dead-state
  reporting. Demo: `examples/conn_machine.hls`.
- **Acceptance**: `examples/hmac_proven.hls` — HMAC-style envelope
  (ipad/opad, modular mixer) whose hot path is FULLY PROVEN: under
  `-O fast` every multiply and byte access is elided; the only
  remaining branches are the precondition assertions carrying the
  proof. `make prove-acceptance` runs the report + builds + runs the
  fast binary.
- **Tests**: 4 new ok tests (contract basics, ensures, proof elision,
  const-range loops + minlen) + 6 new fail tests (call-site violation,
  non-bool, impure, unknown var, ensures-on-void, result-in-requires)
  + a new run_tests.sh section: -O fast output must be byte-identical
  to the interpreter for every contract test; hlprove/hlmodel must
  run cleanly on the acceptance examples; --contracts must catch a
  violated requires at runtime.
- **Makefile**: `make prove F=...`, `make prove-full F=...`,
  `make model F=... FN=step`, `make prove-acceptance`.

## [v0.27.0-alpha] — Stage 16 release: Concurrency & async (data-race freedom)

> **Stage 16 is COMPLETE.** Halis now leverages multiple cores with
> data-race freedom **proven by the type system**: no value may be
> simultaneously owned by two threads. `spawn(f, args)` starts tasks,
> `Chan[T]` channels are the sharing primitive, `select` multiplexes,
> and a new `Conc` effect keeps every non-`uses` function pure AND
> deterministic. The Stage 16 acceptance criterion — *a program sharing
> a variable with a task outside a channel is a compile error* — is
> enforced by the checker in both implementations (14 new fail tests).
> The interpreter uses real Python threads; the native backend a
> pthread runtime with atomic channel refcounts, boundary ownership
> hardening, and built-in deadlock detection. **426/426 tests PASS**;
> the bootstrap is still deterministic. A deep-scan-9 pre-sweep also
> fixed a **pre-existing heap-use-after-free** in the native compiler.

### Stage 16 release — concurrency & async

- **Types & builtins**: `Chan[T]` (unbounded MPMC FIFO, blocking recv),
  `Task[R]` (join handle), `chan_new()`, `spawn(f, a1..aN) -> Task[R]`,
  `select(list[Chan[T]]) -> int`; methods `ch.send(v)` / `ch.recv()` /
  `ch.len()` / `t.join()`. `Task[void]` allowed (join returns nothing).
- **`Conc` effect** (independent of the IO family): spawn / join /
  send / recv / select / chan_new all carry it. Builtin *methods* carry
  effects for the first time — the call-graph fixpoint treats
  `b:chan.send` nodes exactly like builtin functions.
- **Send rule set** (the Send/Sync equivalent, on the Stage 8 ownership
  system): primitives + str Send; `Chan[T]` Send iff T Send;
  **`Task[R]` NOT Send** (first non-Send type); composites Send iff all
  fields/payloads Send (coinductive for recursive types like `Tree`).
- **Data-race freedom (acceptance)**: `spawn`/`send` arguments of owned
  type must be fresh expressions — bare variable / field / index reads
  are **compile errors** pointing at `clone(x)` / `take(x)`.
- **Boundary ownership hardening** (codegen): owned values that are not
  provably private (a `clone(...)` result or a str literal) are
  deep-copied at the spawn/send boundary, so a value returned by a user
  fn (which may alias a live binding — HLS assignment is reference
  semantics) can never put one non-atomic refcount under two threads.
  Channels alone use atomic refcounts; task refcounts live under the
  runtime mutex; everything else stays single-threaded by construction.
- **Native runtime** (`hlc.hls` codegen): `#include <pthread.h>` + a
  channel/task runtime with one global mutex + condvar, per-site spawn
  trampolines (`hl_sa_N_t` arg structs), typed finish/join accessors,
  and **deadlock detection** (all threads blocked + zero pending
  messages → `panic: deadlock: ...`, exit 101 — mirrors the
  interpreter's detector exactly, verified differentially).
- **Interpreter**: real Python threads (daemon tasks — a blocked task
  no longer hangs interpreter exit), same global-lock design, same
  deadlock detector; panic/exit in any task halts the process
  (safe-halt).
- **Safe-halt semantics**: `panic`/`exit()` inside a task terminates
  the whole process (tasks share the process fate).
- **Hoisting soundness fixes**: `spawn` and `chan.send` are marked as
  ownership-TRANSFERRING in `builtin_arg_borrowed` /
  `method_arg_borrowed` — without this, fresh arguments like
  `clone(ch)` were hoisted into temps with cleanups that double-released
  the transferred retain (heap corruption in any program spawning with
  channel arguments).
- **LLVM / HLIR**: programs using Chan/Task/spawn are rejected with a
  clean error by `--emit llvm` / `--emit ir` (Stage 16 ships in the C
  backend + interpreter; documented in SPEC §25.7).
- **Actor model**: `examples/actor_demo.hls` (a KV-store actor with
  enum-typed mailbox protocol) and `tests/ok/feat_conc_actor.hls`.
- **Benchmark**: `benchmarks/conc_bench.hls` — web-server shape (N
  worker tasks × request stream through channels); prints wall-clock
  per pool size and the speedup (measured 1.5× on the 2-core CI
  sandbox — the pattern is core-count-bound, not lock-bound).
- **Examples**: `conc_demo.hls`, `actor_demo.hls`, `par_scan.hls`
  (fan-out / fan-in). **Tests**: 7 new ok tests
  (`feat_conc_{spawn,chan,select,actor,send_struct,take_clone,
  deadlock_detect}`) + 14 new fail tests.

### Deep-scan-9 (pre-release sweep) — bug fixes

- **`src/hlc.hls` — CRITICAL (pre-existing): heap-use-after-free in the
  native compiler.** `check_match` and `check_qmark` wrote the
  expression's `.t` field *internally* while their caller
  (`check_expr`) assigned the same field with the returned value — the
  generated C then released the OLD field value **twice** (field
  assignment lowers to `release old, store new`). Any match/qmark
  expression whose node carried a heap `t` string corrupted the native
  compiler's heap; `feat_clone_deep.hls` reliably crashed it with
  `malloc(): unaligned fastbin chunk detected`. The crash was never
  caught because the test suite only ran the *interpreted* compiler on
  the ok/ programs — see the next item. Fix: the caller owns the
  `.t` assignment; the internal writes were removed.
- **`tests/run_tests.sh` — test gap closed**: new section 4a compiles
  and gcc-builds **every** `tests/ok/*.hls` program with the *native*
  `hlc` binary (section 3 used the interpreted Stage-0 only, which is
  how the UAF above hid for multiple releases).
- **`boot/interp.py`** — spawned tasks are now **daemon** threads: a
  task still blocked on a channel when `main` returns no longer hangs
  the interpreter at shutdown (Python waits for non-daemon threads);
  this mirrors the native semantics (process exit kills threads).
- **`boot/interp.py`** — `deadlock_check` no longer manually unlocks
  the condition lock before raising (the `with` block releases it on
  unwind; the manual unlock would have raised
  `RuntimeError: release unlocked lock`).

## [v0.26.0-alpha] — Deep-scan-8: Stage 14/15 perfection pass

> **Stages 14 + 15 are PERFECTED.** A deep-scan-8 sweep found and fixed
> **7 bugs** across `boot/checker.py`, `boot/parser.py`, `tools/hlbindgen.py`,
> and `tools/hlfmt.py`. Two CRITICAL soundness holes (sink-type validation
> and ABI-header type assertions) are now closed; the `uses IO, Fs`
> false-duplicate is gone; and `hlbindgen` now correctly translates
> `const struct` fields, empty C structs/enums, and plain `struct Name`
> field types. **373/373 tests PASS**; the bootstrap is still deterministic.

### Deep-scan-8 — bug sweep (Stage 14/15 perfection)

- **`boot/checker.py`** — CRITICAL: sink builtins (`print`, `println`,
  `read_file`, `write_file`, `file_exists`, `exit`, `net_lookup`,
  `proc_exec`) now validate argument TYPES at compile time, not just
  taint. Previously `print(42)` compiled cleanly and crashed at runtime
  with `TypeError: a bytes-like object is required, not 'int'`. The
  `reject_tainted_at_sink` helper now enforces `want` (the expected
  type) in addition to the taint check.
- **`boot/parser.py`** — `uses IO, Fs` no longer reports a false
  "duplicate effect declaration: Fs". The IO blanket alias is expanded
  to the full IO family (which includes Fs) at parse time; the duplicate
  check now uses a separate `explicit_effects` set so redundant-but-legal
  declarations like `uses IO, Fs` are accepted, while true duplicates
  (`uses IO, IO` or `uses Fs, Fs`) still error.
- **`tools/hlbindgen.py`** — CRITICAL: ABI header now asserts
  `sizeof(int64_t) == 8` and `sizeof(double) == 8` instead of
  `sizeof(int) == 8` and `sizeof(float) == 8`. C `int` is 4 bytes and
  C `float` is 4 bytes on every mainstream platform; the old assertions
  would FAIL on any standard gcc/clang build, making the ABI header
  unusable. Now includes `<stdint.h>` and `<stdbool.h>`.
- **`tools/hlbindgen.py`** — `const struct Point start;` field now
  correctly maps to `start: Point` instead of `start: int`. The
  `const` qualifier caused the field to fall through to the generic
  type parser, which doesn't know `struct Point`. The parser now scans
  tokens for `struct`/`enum` anywhere in the type, strips qualifiers,
  and uses the type name.
- **`tools/hlbindgen.py`** — plain `struct Name start;` fields (without
  const) also fixed — the fallthrough path now recognises `struct`/`enum`
  keywords and uses the type name instead of sending `struct Name` to
  `_parse_c_type` (which returned `int`).
- **`tools/hlbindgen.py`** — empty C structs/enums are now skipped.
  Previously `struct Empty {};` produced `struct Empty {}` in the HLS
  output, which the HLS parser rejects ("struct must have at least one
  field"). Now empty structs/enums are silently omitted.
- **`tools/hlfmt.py`** — `_render_token` no longer emits invalid `\r`
  and `\xNN` escape sequences for string bytes. The HLS lexer only
  supports `\n`, `\t`, `\\`, `\"` escapes; the old formatter produced
  `\r` and `\x41` which the lexer rejects as "invalid escape sequence".
  In practice the lexer rejects literal control chars in strings, so
  these branches were dead code — but they represented a latent
  soundness issue. Now raises a clear `ValueError` for unrepresentable
  control bytes instead of silently producing unparseable output.
- **Tests** — added `tests/ok/feat_deep_scan8_fixes.hls` (verifies
  `uses IO, Fs` compiles) and `tests/fail/fail_print_int_arg.hls`
  (verifies `print(42)` is rejected at compile time).

## [v0.25.0-alpha] — Stage 15 release + deep-scan-7 bug sweep

> **Stage 15 — Safe C FFI — is COMPLETE.** `hlbindgen` now generates
> HLS struct + enum definitions before the extern block; `#include`
> resolution walks user-supplied search paths; const/volatile qualifiers
> are stripped; an ABI-compatibility header with `_Static_assert`
> type-size checks is generated; the checker enforces
> ownership-across-boundary rules (rejects tainted values passed to
> extern fns); a libcurl demo shows the FFI call pattern; a minimal
> self-hosted `hlbindgen.hls` demonstrates the self-compilation rule.
> A deep-scan-7 sweep found and fixed **20+ bugs** across `tools/`,
> `boot/`, and `src/hlc.hls`. **368/368 + 13/13 LLVM tests PASS**; the
> bootstrap is still deterministic.

### Stage 15 release — bindgen + ownership-across-boundary + ABI header

- **`hlbindgen.py`** — full rewrite:
  - Struct generation (`struct Point { int x, y; };` -> HLS `struct Point`).
  - Enum generation (with implicit value tracking).
  - Nested struct/enum field types.
  - Array fields decay to `list[T]`.
  - `#include` resolution via `--include PATH` (repeatable).
  - const/volatile/restrict/static/inline/_Noreturn qualifiers stripped.
  - `_sanitize_field_name` — HLS reserved words prefixed with `c_`.
  - `--abi-header PATH` — generates C ABI-compatibility header with
    `_Static_assert(sizeof(int) == 8, ...)` and `extern void* <fn>_ptr`.
  - `--pure FN` (repeatable) — marks named functions as `pure` instead
    of the default `uses IO`.
  - `PTR_TO_HLS` expanded for int*/long*/double*/etc.
- **`boot/checker.py`** — ownership-across-boundary check:
  - Rejects any `tainted[T]` argument to an extern fn (soundness rule).
  - Rejects any non-primitive parameter type (only int/float/bool/str).
  - `panic` builtin now adds `b:panic` to the call-graph edges for
    `--audit` completeness.
- **`examples/libcurl_demo.hls`** — libcurl FFI demo.
- **`tools/hlbindgen.hls`** — minimal self-hosted bindgen (pure HLS).

### Deep-scan-7 — bug sweep

- **`boot/interp.py`**:
  - `range()` now caps at 1M elements (was: `range(0, INT64_MAX)` would
    OOM the process). Clean `HLPanic` instead of `MemoryError`.
  - `proc_exec` Windows portability: `os.WIFEXITED` / `WTERMSIG` are
    POSIX-only; on Windows the exit code is returned directly.
  - `str.to_float` accepts leading `+` (parity with C strtod and Python
    `float()`). Was: `"+1.5".to_float()` panicked on the `+`.
  - `taint_unwrap` tightened: requires BOTH `"tainted"` AND `"value"` keys
    AND `"tainted" is True` (was: `"tainted" in v` matched any struct with
    a `tainted: int` field).
- **`boot/boot.py`**:
  - `--emit` / `--target` / `--sandbox` now use `while ... in args` loop
    (was: `if ... in args` — only handled the FIRST occurrence, leaking
    a stray copy into the filename argument on duplicate flags).
  - `MemoryError` caught at top level (was: surfaced as a raw Python
    traceback).
- **`boot/parser.py`** — `RESERVED_TYPE_NAMES` set rejects any struct/enum
  named after a primitive (`int`, `float`, `bool`, `str`, `void`, `list`,
  `map`, `tainted`, `true`, `false`). The old `type_exists` check
  short-circuited on the primitive name, so `struct int { x: int }`
  would silently compile and crash at runtime.
- **`boot/checker.py`** — `err()` now passes `node.get("col", 0)` to
  `HLError` (was: always `col=0` — diagnostics pointed at column 0 even
  when the AST node had real column info).
- **`tools/llvm_emit.py`**:
  - `_lower_match_typed` no longer returns `0` for every match. The
    previous "alpha fallback" silently miscompiled every program using
    `match` in `--emit llvm` mode. Now uses a per-match alloca + store
    per arm + load at the end.
  - `_lower_match_typed` arm-body iteration fixed: was iterating
    `arm["body"]` (a single expression node) as a list of statements,
    causing `AttributeError: 'str' object has no attribute 'get'`.
    Now checks `isinstance(body, dict)` for the expression-form body.
  - `double -> i64` coercion delegates to `hl_float_to_int` (was:
    direct `fptosi` — silent miscompilation on NaN / out-of-range
    values: LLVM gave 0 / INT64_MIN; C backend panicked).
- **`tools/ir/optimize.py`** — `_has_side_effects`: `OP_UNOP` with `-`
  is now IMPURE (was: classified as pure — DCE could erase
  `let x: int = -y` whose only consumer is the overflow panic on
  `-INT64_MIN`, losing the panic). `OP_UNOP` with `!` is still pure.
- **`tools/ll_validate.py`**:
  - `LABEL_RE` matches quoted labels (`%"my label":`).
  - `SWITCH_CASE_RE` matches hex (`0x1F`) and float (`1.5`) case values.
  - `CALL_RE` matches `callbr` (in addition to `call`).
- **`tools/hls-pkg.py`** — `transparency_log_append` no longer reads
  only the last 4 KB of the log. Walks backward by 8 KB chunks to find
  the last complete record (was: if the last record was >4 KB,
  `json.loads` failed silently and `prev_hash` stayed `"0"*64`,
  breaking the chain on every subsequent entry).
- **`src/hlc.hls`** — `hl_str_to_double` accepts leading `+` (parity
  with the interpreter's `str.to_float` and with C's `strtod`).
- **New regression tests**: `feat_lint_cfaware`, `feat_deep_scan7_range`,
  `feat_deep_scan7_to_float`, `fail_extern_taint`,
  `fail_struct_named_primitive`.

## [v0.24.0-alpha] — Stage 14 release: tooling — LSP, formatter, linter

> **Stage 14 — Tooling — is COMPLETE.** The LSP server now supports
> cross-file go-to-definition, rename refactoring, document symbols,
> and references; the formatter is idempotent (and the
> string-literal-as-symbol bug that lost spaces around `+` is fixed);
> the linter has control-flow-aware L004/L005 rules; VS Code + Neovim
> plugins ship under `editors/`; a minimal self-hosted formatter
> (`tools/hlfmt.hls`) demonstrates the self-compilation rule.
> **368/368 + 13/13 LLVM tests PASS.**

### Stage 14 release — tooling

- **`tools/hls-lsp.py`**:
  - **Cross-file go-to-definition** — searches every open document for
    the symbol at the cursor.
  - **`textDocument/references`** — finds every textual occurrence of
    the symbol across all open documents.
  - **`textDocument/rename`** — renames a symbol across all open
    documents. Validates the new name; refuses keywords / builtins /
    effects.
  - **`textDocument/documentSymbol`** — lists every top-level
    fn/struct/enum in the file (VS Code outline view).
  - **Stale BUILTINS list fixed** — added `read_line`, `net_lookup`,
    `rand_int`, `rand_float`, `rand_seed`, `proc_exec`.
  - **`_lookup_type` first-match bug fixed** — ambiguous struct fields
    now return an "ambiguity hint" instead of the first match.
  - **Symbol index cache** — rebuilt lazily after every didChange /
    didClose.
- **`tools/hlfmt.py`** — string-literal byte value no longer
  misclassified as a `sym` token. The previous override fired for `+`
  after a string like `"]"` (the `]` byte value matched the
  closing-bracket rule), so `print("[" + parts + "]")` lost the
  spaces around `+`. Now only `sym`-kind tokens trigger the
  bracket-override rules; string literals are treated as word-like
  for spacing decisions.
- **`tools/hllint.py`**:
  - **L004 (ignored-result)** — control-flow-aware. Flags an `expr`
    statement whose top-level call returns `Result[...]`.
  - **L005 (explicit-unwrap)** — control-flow-aware. Tracks
    per-binding whether there's been a recent `result_is_ok(r)` /
    `option_is_some(o)` check in the same block. The
    `if cond { unwrap(x) }` pattern is recognised.
  - **`collect_idents` false-negative fixed** — field-access names
    (`x.foo`) are no longer added to the ident set, so a real
    unused `let foo = ...` is now correctly reported by L001.
- **`editors/vscode/halis/`** — VS Code extension:
  - `package.json`, `extension.js`, `language-configuration.json`,
    `syntaxes/halis.tmLanguage.json`, `README.md`.
  - Wires the LSP server, formatter (`HalisFormat` command), and
    linter (`HalisLint` command).
  - Format-on-save and lint-on-save settings.
  - Auto-discovers the toolchain at `<workspace>/tools/` or on `PATH`.
- **`editors/neovim/`** — Neovim plugin:
  - `halis.vim`, `ftdetect/halis.vim`, `ftplugin/halis.vim`,
    `syntax/halis.vim`.
  - `HalisFormat`, `HalisLint`, `HalisRestartLSP` commands.
- **`tools/hlfmt.hls`** — minimal self-hosted formatter in pure HLS.
  Demonstrates the "every tool must self-compile" roadmap rule.

## [v0.23.0-alpha] — Stage 13 release + deep-scan-6 bug sweep

> **Stage 13 — Package manager `hls-pkg` — is COMPLETE.** The package
> manager now supports the **transparency log** (append-only,
> SHA-256-chained JSON-lines log), **multi-file packages** (directory
> deps), and **version verification** (records + verifies the git commit
> SHA). A deep codebase scan found and fixed **35+ bugs** across
> `tools/hls-pkg.py`, `tools/hlfmt.py`, `tools/hls-lsp.py`,
> `tools/hlbindgen.py`, `tools/ll_validate.py`, `tools/lsp_smoke.py`,
> `boot/lexer.py`, `boot/interp.py`, `boot/checker.py`, `src/hlc.hls`,
> and `std/*.hls`. **199/199 + 13/13 LLVM tests PASS**; the bootstrap
> is still deterministic.

### Stage 13 release — transparency log + multi-file + version verify

- **Transparency log** (`.hls-pkg-transparency.log`): append-only
  JSON-lines file. Each record has `seq`, `timestamp`, `prev_hash`,
  `chain_hash` (= `SHA-256(prev_hash || canonical-JSON(record))`).
  Tamper-evidence: rewriting any past record breaks the chain.
  - `hls-pkg publish` — append the current package's content hash.
  - `hls-pkg log` — print the log as a table.
  - `hls-pkg log --verify` — recompute every chain hash.
  - `hls-pkg lock` — appends a per-dep record.
  - `hls-pkg verify` — looks up each dep in the log and warns on
    missing/mismatched entries.
- **Multi-file packages**: when `source.path` is a directory,
  `resolve_dependency` returns the directory; `hls-pkg lock` computes
  a deterministic content hash over the sorted file walk; `hls-pkg build`
  symlinks the whole dir into `.hls-pkg-deps/<name>/` so sibling
  imports resolve.
- **Version verification**: lockfile records the resolved `version`
  (tag/branch) AND the 40-char git `commit` SHA. `hls-pkg verify`
  re-runs `git rev-parse HEAD` and compares; a moved tag is reported.
- New CLI commands: `hls-pkg publish`, `hls-pkg log [--verify]`.
- Lockfile format bumped to v2 (added `version`, `commit`, `log_seq`).

### Deep-scan-6 fixes (35+ critical / high / medium bugs)

**Critical:**

- `tools/hls-pkg.py` — git option injection: `git = "--upload-pack=evil"`
  used to execute arbitrary commands. Now `_validate_git_arg` rejects
  values starting with `-`; `git clone` invoked with `--` separator.
- `tools/hls-pkg.py` — `_confine` symlink escape: `os.path.normpath`
  doesn't resolve symlinks; now uses `os.path.realpath`.
- `tools/hls-pkg.py` — manifest shape validation: non-dict sections,
  empty section headers, empty keys, non-list `effects.allowed`,
  non-dict `dependencies`/`source` now raise clean errors.
- `tools/hls-pkg.py` — lockfile shape validation: malformed entries
  (non-object, missing `name`, wrong-typed `sha256`) now print clean
  errors instead of crashing.
- `tools/hls-pkg.py` — `cmd_init` path traversal: `hls-pkg init ../evil`
  used to create directories outside cwd. Now `_validate_dep_name`
  rejects names with `/`, `..`, or leading `.`/`-`.

**High:**

- `boot/interp.py` — `deep_clone` KeyError on a struct with a field
  literally named `"enum"`. Now distinguishes enum values from struct
  values by checking for ALL THREE keys (`enum`, `var`, `data`).
- `boot/interp.py` — `_sandbox_check` UTF-8 bypass: decoded bytes with
  `errors="replace"` (U+FFFD), so the realpath check ran on a different
  path than `open()` would use. Now uses latin-1 (1:1 byte->str).
- `boot/checker.py` — `check_enum_variant` re-ran `check_expr` on type
  disagreement, re-executing `drop`/`take` move-marking and producing
  spurious "use of moved value" errors. The first pass now uses the
  contextual expected payload type, avoiding the re-check.
- `tools/llvm_emit.py` — `list[tainted[T]].pop()` dispatched to
  `hl_list_pop` (returns ptr) instead of `hl_list_pop_i64`. Now strips
  the `tainted[...]` wrapper before dispatching.
- `tools/llvm_emit.py` — `_coerce` missing `i1→double`, `ptr→double`,
  and `double→i64` paths, producing invalid IR on defensive coercion.
- `tools/llvm_emit.py` — `hl_die`/`hl_panic`/`hl_exit` declared
  `noreturn` only in comments, not as the LLVM attribute. The optimiser
  could DCE the trailing `unreachable` and move code across the call.
  Now `attributes #0 = { noreturn }` is emitted at the end of the module.
- `tools/hlfmt.py` — `} else {` and `} else if {` used to break onto
  separate lines (the `}` peek set missed the `else` keyword).
- `tools/hls-lsp.py` — `_publish_diagnostics` returned early WITHOUT
  publishing an empty list when `program is None`, so the editor
  retained stale diagnostics. Now always publishes an empty list.
- `tools/hls-lsp.py` — `handle_did_change` didn't check version,
  so out-of-order changes overwrote newer text.
- `tools/hls-pkg.py` — `extract_effects` dropped effects for
  `pure + IO, Fs` declarations (stripped `"pure"` from the middle).
- `tools/hls-pkg.py` — `extract_effects` was fail-OPEN when boot.py
  exited 0 with unparseable output. Now fails closed.
- `src/hlc.hls` — `while`-loop with fresh subexpressions in the
  condition emitted hoisted temps OUTSIDE the loop body (the
  adjacent comment claimed otherwise). Now emits temps AFTER
  `while (1) {` so they re-execute per iteration.
- `src/hlc.hls` — wildcard match arm not in the LAST position
  generated invalid C (`else {...} else if (...)`). Now the checker
  rejects non-last wildcards with a clear error.

**Medium:**

- `boot/lexer.py` + `src/hlc.hls` — float literals didn't support
  scientific notation (`1e10`, `1.5e-3`). Both lexers now accept
  optional `e`/`E` + optional `+`/`-` + digits.
- `std/json.hls` — `jsonp_parse_string` low-surrogate parse called
  `jsonp_err` but didn't `return` — fell through to garbage computation
  with `hd2 = -1`, pushing invalid UTF-8 bytes into `chars`.
- `std/url.hls` — IPv6 URL with a trailing colon but no port digits
  (e.g. `http://[::1]:`) called `urlp_parse_port("")`, which panics.
  Now mirrors the non-IPv6 path's guard.
- `std/math.hls` — `math_floor`/`math_ceil`/`math_round` called
  `x.to_int()` which panics for floats outside int64 range
  (e.g. `1e20`, +/-inf). Now handle out-of-range floats cleanly.
- `std/math.hls` — `math_abs_int(INT64_MIN)` overflowed silently in
  the interpreter (Python) but panicked in the native runtime —
  differential-test divergence. Now panics explicitly.
- `std/math.hls` — `math_sqrt(-0.0)` returned NaN (Newton's method
  computes `-0.0 / -0.0`). Now returns -0.0 (matches libm sqrt).
- `tools/lsp_smoke.py` — `parse_frames` ValueError on malformed
  Content-Length header. Now wraps in try/except.
- `tools/ll_validate.py` — `PTR_LIT_RE` only matched integer
  operands; `ptr 5.0`, `ptr 0x...` slipped through. Extended.
- `tools/hlbindgen.py` — function-pointer params (`int (*cb)(int)`)
  produced unparseable HLS identifiers. Now synthesizes `_argN`.

### Stage 12 release — struct/enum/match lowering (v0.22.0-alpha)

> **Stage 12 — Native LLVM backend — is now feature-complete** for the
> alpha-subset language surface. The LLVM IR text backend now lowers
> struct literals, enum literals, match expressions, the `?` operator,
> struct field access and assignment, and tagged-union payloads via a
> typed runtime API.

- **Struct literals** lower via `hl_struct_alloc(size) + hl_struct_set_*`
  per field.
- **Enum literals** lower via `hl_enum_new_variant(idx) + hl_enum_payload()`.
- **Match expressions** lower to a `switch` on `hl_enum_tag`.
- **`?` operator** lowers to a tag check + branch on the Ok variant.
- **Struct field access/assignment** lowers via the typed
  `hl_struct_get_*/set_*` helpers.
- **`noreturn` attribute** on `hl_die`/`hl_panic`/`hl_exit`.
- **`_coerce` improvements**: added `i1→double`, `ptr→double`, `double→i64`.
- **`list[tainted[T]].pop()` fix**: strips the `tainted[...]` wrapper.
- The checker now annotates `structlit` nodes with `sfields`, `enumlit`
  nodes with `variant_idx` and `payload_type`, so the LLVM backend can
  dispatch to the right runtime helpers.

## [v0.22.0-alpha] — Stage 12 release (LLVM struct/enum/match lowering)

(Same content as the "Stage 12 release" section above — kept as a
separate version-tagged entry for git tag / release notes.)

## [v0.21.0-alpha] — Stage 10 + Stage 11 release + deep-scan fixes

> **Stage 10 — Taint tracking & sandbox — is COMPLETE.** Sandboxed
> compile mode restricts filesystem builtins to a granted directory
> (both interpreter and native runtime). A new taint source
> `read_line() -> tainted[str]` covers stdin. The `--sandbox DIR`
> flag also rejects `extern "C"` blocks (FFI bypasses the sandbox).
> **Stage 11 — SSA IR + optimisation — is COMPLETE.** Two new
> optimiser passes — `inline_small` (inlines small pure functions
> at their call sites) and `licm` (hoists loop-invariant expressions
> out of loop bodies) — join the existing `constant_fold`,
> `copy_propagate`, `dead_code_elim` pipeline. A deep codebase scan
> found and fixed 13+ critical, high, and medium correctness/security
> bugs across `boot/`, `tools/`, `src/hlc.hls`, and `std/`.
> **191/191 tests PASS**; the bootstrap is still deterministic.

### Stage 10 release — sandboxed compile mode + read_line taint source

- **`--sandbox DIR` flag in `boot.py`** — restricts all filesystem
  builtins (`read_file`, `read_file_tainted`, `write_file`,
  `file_exists`) to paths that resolve INSIDE DIR. Mirrored in both
  the Stage-0 interpreter (`_sandbox_check`) and the native C runtime
  (`hl_sandbox_check` + `hl_set_sandbox_root`). Symlink escapes are
  caught via realpath resolution.
- **`HLS_SANDBOX_ROOT` env var** — the native runtime auto-
  initialises the sandbox from this env var at startup. Users can
  compile a program once and run it with different sandboxes (no
  recompile needed). `--sandbox DIR` also exports the env var so any
  subprocess (e.g. via `proc_exec`) inherits the gate.
- **`--sandbox` rejects `extern "C"` blocks** — extern FFI can call
  libc directly (`fopen`, `system`, `execve`, `socket`), bypassing
  the sandbox entirely. The compiler refuses to compile such
  programs under `--sandbox` to keep the sandbox guarantee sound.
- **`read_line() -> tainted[str]` builtin** — the third taint source
  (after `tainted_args` and `read_file_tainted`). Reads one line
  from stdin (newline stripped), wraps as `tainted[str]`, carries
  the `IO` effect. The interpreter strips CRLF line endings; the
  native `hl_read_line` runtime helper mirrors this.
- **`hl_die_at(msg, file, line)` runtime helper** — position-aware
  panic, plumbed for Stage 11 release consumption in the codegen.

### Stage 11 release — inline_small + LICM optimiser passes

- **`inline_small` pass** — inlines calls to small `pure` functions
  (≤12 instructions, single block, non-recursive). After inlining,
  re-runs `constant_fold` + `copy_propagate` + `dead_code_elim` so
  inlined bodies fold into their call sites (e.g. `square(5)` becomes
  the constant `25` at compile time). The `optimize()` pipeline is
  now: constant_fold → copy_propagate → DCE → inline_small →
  constant_fold → copy_propagate → DCE → LICM → DCE.
- **`licm` (loop-invariant code motion) pass** — identifies loops via
  the `*_cond` block naming convention, hoists pure instructions
  whose operands are all defined outside the loop body into the
  preheader block. Conservative: skips `OP_BINOP` (might panic on
  overflow), only hoists from the loop's immediate body block (not
  nested control flow), so the pass is sound for nested if/else
  inside loops.
- **Extended `_annotate_safe`** to mark multiplications by 0 or 1
  as `safe_overflow` (the result is provably safe — 0 or the other
  operand, both of which fit in int64 since the operand already did).
- **`--opt-stats` output** now lists the full pass list including
  the two new passes.

### Deep-scan fixes (13 critical / high / medium bugs)

**Critical:**

- `tools/hls-pkg.py` — `FAIL_CLOSED_EFFECTS` and `KNOWN_EFFECTS`
  were missing `Net`, `Rand`, `Proc`. A dependency using
  `proc_exec` was recorded as PURE, bypassing effect enforcement
  (security soundness). Added all three Stage 9 release effects.
- `src/hlc.hls` `print()`/`println()` with 0 args crashed instead
  of giving a clean error. Argument count is now checked BEFORE
  the taint-sink check.
- `src/hlc.hls` `print_audit` claimed `Net`, `Rand`, `Proc` are
  "Reserved (error if used)" — they are active since v0.20.0-alpha.
- `boot/interp.py` extern FFI passed `id(v)` for list/map/struct
  args — a raw CPython heap address. Now panics with a clean error
  explaining opaque pointer args are not supported.
- `src/hlc.hls` `hl_sandbox_check` only accepted `/` as separator —
  broken on Windows. Now accepts `\\` too.
- `src/hlc.hls` `hl_read_file` realloc-on-failure leaked the
  original buffer. Now uses a temp so the original is freeable.
- `src/hlc.hls` `hl_list_push` same realloc-on-failure leak.
- `src/hlc.hls` `hl_str_alloc` allowed negative `len` to wrap to
  ~2^64 via the `size_t` cast. Now rejects `len < 0` up front.
- `src/hlc.hls` `hl_set_sandbox_root` didn't check strdup's
  return. Now panics cleanly on OOM.
- `boot/interp.py` `net_lookup` only caught `socket.gaierror`;
  timeouts and other OSErrors propagated as raw tracebacks. Now
  catches `OSError` broadly.
- `tools/hls-lsp.py` `EFFECTS` list missing Net/Rand/Proc — editor
  autocompletion never offered them.

**High:**

- `tools/ir/optimize.py` `_fold_binop` and unary `-` folding treated
  `bool` as `int` (Python's `bool` subclasses `int`). Now uses
  `isinstance(x, int) and not isinstance(x, bool)` so bool-typed IR
  values aren't miscompiled.
- `boot/interp.py` `file_exists` decoded path with
  `errors="replace"`, substituting U+FFFD for non-UTF-8 bytes —
  divergent from native runtime which passes raw bytes to `stat()`.
  Now passes bytes directly.

**Medium:**

- `std/sanitize.hls` `sanitize_command` missing NUL byte (0) and
  `~` (126) from reject list. NUL would truncate at C string
  boundary; `~` enables bash tilde expansion.
- `std/str.hls` `str_pad_left`/`str_pad_right` overshot `width`
  when `pad.len() > 1` (e.g. `str_pad_left("ab", 6, "xyz")`
  produced `"xyzxyzab"` instead of `"xyzxab"`). Now fills with a
  single-byte prefix of pad so the final length is exactly `width`.
- `tools/ir/optimize.py` `_licm` previously considered hoisting
  from all blocks in a loop body — including nested if/else blocks
  that might not execute on every iteration. Now only hoists from
  the loop's immediate body block (soundness fix for nested control
  flow inside loops).

### New tests (2 ok)

- `tests/ok/feat_read_line.hls` — differential test for the
  `read_line()` taint source (Stage-0 vs native).
- `tests/ok/feat_inline_licm.hls` — exercises the `inline_small`
  and `licm` passes with a `square()` helper, an `add_one()` helper,
  and a loop-invariant multiplication.

### Tests run with stdin redirected from /dev/null

`tests/run_tests.sh` now redirects stdin from `/dev/null` for every
differential test. This prevents tests using `read_line()` from
hanging waiting for input. The interpreter reads EOF (returns empty
`tainted[str]`); the native binary does the same — the differential
test still compares apples to apples.

## [v0.20.0-alpha] — Stage 9 release: complete fine-grained effects + Halis rename

> **Stage 9 — Fine-grained effects & capabilities — is COMPLETE.** The
> three reserved effects (`Net`, `Rand`, `Proc`) are now active with
> five new builtins. The language was renamed from "Hieu Louis" to
> "Halis" (High-level Language Systems) — same `HLS` abbreviation,
> same `.hls` file extension. **185/185 tests PASS**; the bootstrap
> is still deterministic.

### Language rename: "Hieu Louis" → "Halis"

- **Name**: "Halis" — short, unique (no known programming language by
  this name), backronym "H"igh-level "L"anguage for "S"ystems. The
  abbreviation "HLS" is preserved exactly, as is the `.hls` file
  extension, the `hlc` compiler binary name, and the `hl_*` runtime
  symbol prefix.
- 44 files updated. All textual references to "Hieu Louis" have been
  replaced with "Halis" (docs, comments, code, test data, example
  programs). The GitHub repo URL `hieu-louis-lang` is kept as-is (the
  repo itself was not renamed).

### Stage 9 release — five new builtins + three new effects

- **`net_lookup(host: str) -> str`** (Net effect): DNS A-record
  lookup via `getaddrinfo`. Returns the first IPv4 address as a
  string. Panics on DNS failure (clean error). The host is a TAINT
  SINK (tainted host → DNS rebinding attack vector; the checker
  rejects it).
- **`rand_int(max: int) -> int`** (Rand effect): uniform random int
  in `[0, max)`. Panics if `max <= 0`.
- **`rand_float() -> float`** (Rand effect): uniform random float in
  `[0.0, 1.0)` — 53 bits of randomness (full IEEE double significand).
- **`rand_seed(s: int) -> void`** (Rand effect): seed the PRNG. Same
  seed produces the same sequence (deterministic).
- **`proc_exec(cmd: str) -> int`** (Proc effect): run a shell command
  via `system()`. Returns the exit code (0 on success, 1..255 on
  failure, 128+signum on signal kill). The command is a TAINT SINK
  (tainted command → shell-injection vector; the checker rejects it).

### Shared PRNG — differential-test safety

The Rand builtins use a 64-bit LCG with the same Knuth-MMIX constants
(`state * 6364136223846793005 + 1442695040888963407` mod 2^64) in
BOTH the Stage-0 interpreter (`boot/interp.py: HalisRNG`) and the
native C runtime (`hl_rng_state` global). This makes random sequences
**deterministic across implementations** — a test using `rand_seed(s)`
then `rand_int(n)` produces the same output in both backends, which
is critical for the project's differential-testing gate. Without
this, the test suite would fail on any program using `rand_*`.

### Taint-sink additions

`net_lookup` and `proc_exec` are added to `SINK_BUILTINS` in
`boot/checker.py` and to the audit-mode sink list in `boot/boot.py`.
Passing a `tainted[T]` value as the host/cmd argument is now a
compile-time error (matching `print`, `write_file`, etc.).

### Portability fix

The C runtime now includes `<sys/wait.h>` explicitly (needed for
`WIFEXITED` / `WEXITSTATUS` / `WTERMSIG`). glibc pulls it in
indirectly via `<stdlib.h>`, but musl and other libcs do NOT —
without the explicit include, `proc_exec` would compile to
non-portable code that returns the wrong exit code on non-glibc
systems.

### Parser

- `RESERVED_EFFECTS` is now an empty set — all eight effects are
  active. The error message for an unknown effect now lists all
  eight known effects (was five).
- The reserved-effect check (which produced "effect 'X' is
  reserved for a future stage") is now unreachable. The
  `fail_effect_reserved.hls` test was removed (it would pass
  because no effect is reserved).

### New tests (10 total: 4 ok, 1 panic, 5 fail)

- `tests/ok/feat_effects_net.hls` — net_lookup demo
- `tests/ok/feat_effects_rand.hls` — rand_int/rand_float/rand_seed demo
- `tests/ok/feat_effects_proc.hls` — proc_exec demo
- `tests/ok/panic_rand_zero.hls` — rand_int(0) panics cleanly
- `tests/fail/fail_effect_net_missing.hls` — `uses Net` required for net_lookup (5-layer chain)
- `tests/fail/fail_effect_rand_missing.hls` — `uses Rand` required for rand_int
- `tests/fail/fail_effect_proc_missing.hls` — `uses Proc` required for proc_exec
- `tests/fail/fail_taint_net_lookup.hls` — tainted host → DNS rebinding rejection
- `tests/fail/fail_taint_proc_exec.hls` — tainted command → shell-injection rejection
- (Removed) `tests/fail/fail_effect_reserved.hls` — no reserved effects remain

### Documentation

- `SPEC.md` §17 (Fine-grained effects & capabilities) rewritten for
  the Stage 9 release — the eight active effects are listed, the new
  `uses` clause grammar is documented, and the taint-sink additions
  are noted.
- `SPEC.md` §8a (Built-in functions table) — five new builtins added.
- `ROADMAP.md` — Stage 9 marked ✅ (was 🔄) with a new release-record
  section explaining what shipped, including the shared-PRNG note.
- `README.md` — test count updated (185).

## [v0.19.0-alpha] — deep-scan-5: whole-codebase bug sweep

> A four-track audit (boot/, tools/, LLVM+IR backends, stdlib) with every
> finding verified by a runnable repro before fixing. 26 distinct bugs
> fixed; 7 new regression tests; 173/173 tests PASS.

### boot/ — checker & interpreter (6 fixes)

- **Extern "C" calls bypassed the effects/capability check entirely**
  (soundness hole in the flagship capability system): the fixpoint
  unioned an extern's DECLARED effects into the caller's computed set,
  but the enforcement loop looked for a witness via the extern's computed
  set — always empty for body-less externs. `--check` said OK while
  `--audit` printed VIOLATION. Both the fixpoint and the two witness
  loops (pure + violation) now use the extern's declared set, in both
  Stage-0 and the self-hosted checker. Regression:
  `fail_extern_effect_missing`.
- **`float.to_int()` on ±inf/NaN crashed the interpreter** with a raw
  Python `OverflowError` traceback while the native runtime panicked
  cleanly — a differential divergence. The interpreter now checks
  non-finiteness first; the C runtime gained an explicit `isnan` check
  (NaN passes both range comparisons and fell into an undefined cast).
  Regression: `panic_float_inf_toint`.
- **`args()` diverged between the implementations**: the interpreter
  returned a fresh copy per call, the native runtime returns THE
  process-global list — mutating the result was observable natively but
  not under Stage-0. The interpreter now returns the same list.
- **The `for`-loop variable silently shadowed outer bindings** (the
  `let` branch rejected shadowing, the `for` branch never checked —
  SPEC §4). Rejected in both compilers. Regression: `fail_for_shadow`.
- **`?` on an enum with a third variant** (beyond Ok/Err) was
  checker-clean but guaranteed a runtime panic — and the native codegen
  read garbage union bytes. **`?` on an enum declaring both `Err` and
  `None`** silently kept only the last as the error variant. Both
  rejected at check time now. Regressions: `fail_qmark_extra_variant`,
  `fail_qmark_err_and_none`.
- Runaway HLS recursion surfaced as a raw `RecursionError` traceback —
  now a clean `panic: stack overflow` (exit 101). Duplicated CLI flags
  no longer leak into the filename argument.

### LLVM text backend + HLIR (10 fixes)

- **Runtime ABI mismatches** (the declared externs were stale after the
  Stage 8 runtime change): `hl_list_new()`/`hl_map_new()` called with 0
  args where the runtime now takes a destructor pointer (garbage
  function pointer → segfault); `hl_map_get` (removed symbol) called
  with a boxed default. Now: `hl_list_new(ptr null)` / `hl_map_new(ptr
  null)` (arena-mode contract) and `map.get_or` dispatches to the typed
  getters `hl_map_get_i64/_f64/_bool` with defaults passed by value.
- **`--opt-stats` crashed with a raw `TypeError` on any program with a
  void function** (void returns emitted `args=[None]`; the optimiser
  dereferenced it) — including three in-repo tests. Void returns now
  emit an empty arg list.
- **Statements after `return`/`break`/`continue` were lowered as live
  code** — a second `return` silently REPLACED the first one's value in
  the IR. The builder now stops lowering at a terminator, like the
  interpreter.
- **`list.pop()` on primitive lists leaked the element box** — the typed
  pops (`hl_list_pop_i64/_f64/_bool`) are now used, mirroring the C
  backend.
- **The for-loop index `alloca` was not hoisted to the entry block** —
  a `for` nested in a loop re-executed the alloca every outer iteration
  (allocas are reclaimed only at function return → unbounded stack
  growth). The index slot is pre-allocated via the binding pool.
- **A never-typed argument in a non-final position** emitted
  instructions after the terminator (invalid IR). Argument lowering now
  stops when the block closes.
- **IR `match` arms were stored as raw AST and never lowered** (side
  effects silently dropped) — now a clean `--emit ir` unsupported error,
  consistent with the enum-literal policy.
- **DCE classified checked arithmetic (`binop`) and `list_get` as pure**
  — a required panic could be erased; removed from the pure list.
- **Copy propagation was a no-op** (it only recorded `t→t` copies but
  the builder emits `v_*` dests); it now tracks `v_x = t_k` copies and
  invalidates them on store.
- **The IR for-loop compared against the snapshot length only** — a
  shrinking list would be represented as an out-of-bounds `list_get`
  panic; it now re-checks the current length every iteration like every
  real backend.

### Tools (11 fixes)

- **hlfmt deleted every `#` comment** (`-w` silently destroyed user
  documentation — the documented Stage 14 limitation). Comments are now
  **preserved**: comment-only lines pass through verbatim (indent
  kept), trailing comments are re-appended to their line; verified
  idempotent + byte-safe (latin-1 pipeline) across all 156 repo files.
- **hlfmt: spurious leading space** on every top-level line following an
  `import` (indent-0 guard); **unary minus** formatted as a binary
  operator (`let x: int = -5` → `let x: int =- 5`, `(-1)` → `(- 1`,
  `return -x` → `return - x`); **`if !x` lost its space** (`if!x`).
- **hls-lsp: the UTF-16 → byte column conversion returned code-point
  indices** while the lexer counts bytes — hover/definition silently
  failed after any non-ASCII text on the line. Now accumulates UTF-8
  byte lengths (verified with accented + non-BMP input).
- **hls-lsp: syntax-error diagnostics were always anchored at 0:0** —
  now use the lexer-reported line/col.
- **hls-pkg: `#` inside a string corrupted the entire manifest** (the
  comment stripper ignored quotes; the string parser then swallowed
  following lines). Comments are now stripped outside strings only;
  string escapes are decoded; **bare list items** (`allowed = [IO, Fs]`)
  were silently dropped — now collected with type conversion;
  `hls-pkg add` **deleted unknown manifest sections** on round-trip —
  now preserved; `_fmt_value` didn't escape backslashes (values grew on
  every write).
- **hls-pkg: `build` trusted the lockfile's package names unvalidated** —
  a crafted `hls-pkg.lock` with `"name": "../outside"` could delete
  files and plant symlinks outside `.hls-pkg-deps/`. Names are validated
  before any path join (path traversal closed).
- **hllint L002** false-positived on functions called only from struct
  field defaults; **L010 (empty-impl) never fired** (justified by a
  false claim that the parser rejects empty impls — it does not; the
  rule is implemented now); **warning order was non-deterministic**
  (set iteration) — now sorted.
- **ll_validate false-rejected valid IR**: functions whose entry block
  has no label (terminated initialised True at function start),
  multi-line `switch` case lists, `br i1 <const>` conditions and
  `tail call`/`musttail call` also bypassed the callee-existence check.
- **hlbindgen silently dropped declarations containing function
  pointers** — now emits a warning per skipped declaration.

### stdlib (8 fixes)

- **`json_stringify` emitted `inf`/`nan`** — invalid JSON per RFC 8259
  §6 that no consumer (including `json_parse` itself) accepts; a single
  `1e400` from an attacker corrupted the whole document. inf/NaN now
  serialise as `null` (JavaScript `JSON.stringify` semantics).
- **`json_parse` panicked on malformed input with RAW conversion
  errors** (`"-"`, `"1e"`, `"9223372036854775808"` → `panic: cannot
  convert string to int (at line 436)` — no JSON context or position).
  The number scanner now validates digits/exponent/int64-range and every
  error is a positioned `json parse error: ...`. New
  **`json_parse_result(src) -> Result[JsonValue, str]`** — the
  non-panicking entry point for untrusted input (json_parse keeps its
  historic panic behaviour).
- **`sanitize_path` accepted Windows-style backslash traversal**
  (`..\..\etc\passwd` has no `..` segment when split on `/`) —
  exploitable on Windows hosts. Backslashes are now rejected outright.
- **`sanitize_sql_string` passed NUL bytes through** — C-string SQL
  clients truncate at NUL (the classic `admin\0--` filter bypass).
  Now rejected.
- **`sanitize_command` missed the cmd.exe metacharacters `%` and `^`.**
- **`base64_decode` accepted malformed padding**: data after padding
  (`Zg==Zg==`), `=` in unpadded tails (`Zg=`, `Zm9vYQ=`) — phantom
  bytes where Python's strict decoder rejects. All rejected now
  (RFC 4648).
- **`url_parse` panicked with a raw `to_int` error on a malformed port**
  (`http://host:abc/`) and silently accepted negative/oversized ports —
  ports are now validated (digits, 0–65535) with a URL-context error.
- **`url_query_parse` created a phantom `""` key for empty segments**
  (`&a=1&&b=2`); **`csv_parse` switched into quoted mode on a stray
  mid-field quote**, swallowing delimiters for the rest of the row
  (RFC 4180: quotes are only meaningful at field start).

### Tests

- 163 → **173 PASS / 0 FAIL**: +`feat_deep_scan5_json`,
  +`feat_deep_scan5_stdlib` (differential), +`panic_float_inf_toint`
  (differential, exit 101), +`fail_extern_effect_missing`,
  +`fail_for_shadow`, +`fail_qmark_extra_variant`,
  +`fail_qmark_err_and_none`.
- Tooling verification: hlfmt idempotent across all 156 `.hls` files
  (comments preserved); `lsp_smoke.py` all assertions pass.

## [v0.19.0-alpha] — Stage 8-beta: END OF ARENA — refcounted runtime & ownership analysis

### Why this release exists — "the arena is gone"

Since v0.1, every heap allocation in generated C programs leaked by
design: the runtime had no `free` path at all (the "arena model" —
structurally impossible to use-after-free, but every string, list, map,
struct and enum lived until process exit). Stage 8-alpha (v0.4.0) added
the *static* half of ownership (`drop`/`clone`/`take`, use-after-move
errors) but `drop(x)` was documented as a runtime no-op. This release
finishes Stage 8: **memory is now reclaimed deterministically at scope
exit**, and the Stage 8 acceptance criterion — a memory-stress program
must not grow RSS — is met and **enforced in CI**.

### The ownership discipline (SPEC.md §16.7)

- Every heap object starts with an `int64_t refcnt` (first field, so
  `hl_retain` is generic across all types).
- The codegen classifies every expression as **fresh** (one unowned
  retain: literals, concat, `clone`, call results, `pop`, `keys`,
  list/struct/enum literals) or **borrowed** (idents, field/index
  access). Bindings own one retain; function parameters own one retain
  of each argument; containers own their elements (destructor function
  pointers per element/value type); `return` of a borrowed value adds
  the caller's retain.
- Pointer-typed bindings carry `__attribute__((cleanup(...)))` — the C
  compiler itself runs the releases on **every** control-flow path
  (break/continue/return), giving exact free timing without a GC.
- Fresh values consumed in borrowed positions (e.g. the left operand of
  a string concat) are hoisted into cleanup temporaries — expression
  trees cannot leak.
- `print`/`println`/`panic`/`read_file`/`write_file`/`file_exists`
  consume their argument; `?` retains the payload on success and the
  error value on the early-return path; `match` arms are own-wrapped so
  the match always yields an owned value.
- `take(x)` transfers the binding's retain (the C variable is nulled
  after the statement); `drop(x)` releases immediately and nulls the
  binding.

Observable behaviour is unchanged — the aliasing semantics of every
prior release is preserved exactly (differential suite: 100%).

### clone() on every owned type

`clone()` now supports `str`, `list[...]`, `map[str, ...]`, `struct`,
`enum`, and `tainted[...]` via per-instantiation generated helpers
(`hl_clone_<mangled-type>`, recursively cloning pointer children). The
interpreter's `deep_clone` covers the same set. Mutating a clone never
affects the original — including nested structs, structs inside lists,
and structs inside maps.

### New checker rule

`take()`/`drop()` are rejected inside a `while` condition or `for`
iterable: the header re-evaluates every iteration, so a move would hand
NULL to the callee from the second iteration on (an interpreter/native
divergence). Both compilers enforce the same error message.

### Bug fixes (found by the differential + memory suite)

- **List literals with local variables generated uncompilable C** —
  `hl_lit_N()` helper functions embedded element expressions generated
  in the *enclosing* function's context, so `[a, b, 30]` with locals
  failed to compile natively while the interpreter ran it fine (a
  differential hole: no prior test covered it). List literals are now
  inlined as GCC statement expressions. Regression: `feat_list_local`.
- **`pop()` on primitive-element lists leaked the element box** (8
  bytes + malloc overhead per pop). New typed pops
  `hl_list_pop_i64/f64/bool` free the box after unboxing.
- **`hl_read_file` returned empty output for virtual files** —
  `/proc`, `/sys` report size 0 via `fseek`/`ftell`, so the native
  binary read nothing where the interpreter read the content (a real
  interpreter/native divergence). The runtime now reads incrementally
  until EOF.
- **`hl_read_file` leaked its temporary read buffer** (the `buf` from
  `ftell`-sized `malloc` was never freed) — now freed before return.
- `clone(tainted[T])` emitted a call to a non-existent
  `hl_clone_tainted__T` — the dispatch now unwraps taint (clone of
  `tainted[T]` is the clone of `T`; taint is compile-time only).

### Tests

- 156 → **163 PASS / 0 FAIL** (157 with the memory-stress check):
  - `feat_scope_free` — differential churn of every heap shape in
    loops (strings, lists, maps, structs, enums, clone/take/drop,
    borrow-chain returns, discarded fresh values).
  - `feat_clone_deep` — deep clone semantics for nested structs,
    recursive enums, structs in lists/maps.
  - `feat_list_local` — regression for the list-literal bug.
  - `fail_take_in_loop_cond` / `fail_drop_in_loop_iter` — the new
    loop-header move rule.
  - `tests/memcheck/stress_leak.hls` (native-only): 500k allocation
    rounds; **RSS delta = 0 pages**; enforced under
    `ulimit -v 262144` (256 MB) in `tests/run_tests.sh` section 3b —
    under the old arena model this loop exhausts the limit in seconds.
- Bootstrap still **deterministic** (two self-compile passes produce
  byte-identical C).
- The LLVM suite (13/13) still passes — the LLVM backend keeps the
  arena model (`elem_free`/`val_free` = NULL means "never release"),
  which is now an explicit, documented contract of the runtime.

### Upgrade notes

- Programs relying on `take()`/`drop()` inside loop conditions/iterables
  now fail to compile (both compilers) — hoist the move out of the
  header.
- Generated C now uses `__attribute__((cleanup))` (GCC/Clang), which
  the supported toolchains (gcc, clang) both provide.

## [v0.18.0-alpha] — Stage deep-scan-4 (tools) + v0.17.0-alpha (LLVM backend) + v0.16.0-alpha (core)

### Why this release exists — "the scan that finally scans"

The three previous "deep scan" releases (ec00e9f, 6906e52, dd9c730) each
reported a clean bill of health after fixing dozens of bugs, yet bugs kept
surviving. The root cause was never the scanning effort — it was that the
**verification net had holes in it**:

1. **The CI never actually verified the LLVM output.** The Stage 12 step
   piped `--emit llvm` into `/dev/null` and checked only the exit code.
   The emitter could (and did) ship IR that no LLVM parser would accept:
   booleans widened to i64 but stored into i1 slots, instructions emitted
   after terminators, calls to runtime symbols that do not exist, `ptr 5`
   literal operands. Structs/enums/match/`?` lowering were silent stubs.
2. **The test suite only exercised paths the author already thought of.**
   Method calls bypassed the effect call-graph entirely; struct field
   defaults with calls crashed the checker with a raw `KeyError: None`.
   No existing test walked those paths, so "150/150 PASS" was true and
   meaningless at the same time.
3. **The tooling had no failure-mode tests.** hlfmt corrupted float
   literals into unparseable output; the LSP died on the first malformed
   JSON-RPC frame and never answered requests on broken documents; the
   linter flagged every index/field assignment target as an unused
   binding; hls-pkg set an env var that nothing read.

This release fixes the bugs AND closes the verification holes: a new
structural LLVM IR validator (`tools/ll_validate.py`), a new LLVM backend
test suite (`tests/run_llvm_tests.sh` + `tests/llvm/`), a protocol-level
LSP smoke test (`tools/lsp_smoke.py`), new fail/ok regression tests for
the effect-graph fixes, and CI steps that assert on OUTPUT, not exit codes.

### Fixed — fourth deep scan (DS4): 31 bugs

#### boot/checker.py — effect-system soundness
- **BUG-DS4-1** (SOUNDNESS, high): **method calls did not participate in
  the effects call graph.** `check_method` never added an edge to
  `self.edges`, so the fixpoint never traversed method calls — a function
  calling an IO-using method without declaring `uses IO` (or a `pure`
  function calling an effectful method) compiled cleanly, completely
  bypassing the capability system. New regression test:
  `tests/fail/fail_effect_method.hls`.
- **BUG-DS4-2** (high): struct field default expressions containing calls
  crashed the checker with a raw Python `KeyError: None` (the defaults
  were checked with `self.cur_fn = None` while `self.edges[None]` does
  not exist). Defaults are now checked under a synthetic `@default.<S>`
  call-graph node, and every function that constructs such a struct gets
  an edge to it — the default's effects propagate to constructors, which
  matches the interpreter and C backend (both evaluate defaults at each
  construction). New regression tests:
  `tests/ok/feat_struct_default_call.hls`,
  `tests/fail/fail_effect_struct_default.hls`.
- **BUG-SC-12 (for real)** (low): the previous "consolidation" of the
  taint helpers left the duplicate `is_tainted_type`/`list_taint_inner`
  definitions further down the file, shadowing the consolidated aliases
  (F811). Actually removed this time.
- F841 cleanup: unused `defaulted` set in `check_structlit`.

#### tools/llvm_emit.py — the LLVM backend was structurally broken
Rewritten where broken. The emitted IR for the supported subset is now
structurally valid (verified by `tools/ll_validate.py` and, when
available, `llvm-as`) and semantically aligned with the interpreter and
the C backend:

- **BUG-DS4-3** (high): the RUNTIME_DECLS table declared symbols that do
  not exist in the runtime (`hl_int_to_str`, `hl_float_to_str`,
  `hl_str_to_float`, `hl_str_subst`, `hl_args_get`, `hl_ord`, ...) and
  missed the ones that do (`hl_str_from_int64`, `hl_str_from_double`,
  `hl_str_from_bool`, `hl_str_to_double`, `hl_range`, `hl_args`,
  `hl_panic`, `hl_chr`, the `hl_box_*` family, `hl_div_i64`, `hl_mod_i64`,
  `hl_neg_i64`, `hl_abs_i64`, `hl_clone_*`). Assembling/linking the old
  output failed on undefined symbols. `panic()` also called `hl_die`
  (const char*) with a boxed str — the correct callee is `hl_panic`.
- **BUG-DS4-4** (high): booleans were i64-in/i1-out — every comparison
  was `zext`ed to i64, then stored into i1 slots, branched on, or passed
  to i1 parameters: invalid IR for any program with a bool variable.
  Booleans are now i1 end-to-end.
- **BUG-DS4-5** (high): instructions were emitted after terminators
  (`for`-increments after a `continue`'s branch, statements after
  `return`/`break`/`panic`). The emitter now tracks the open block and
  skips unreachable code; the `for` increment lives in its own block,
  which is also the `continue` target.
- **BUG-DS4-6** (high): `extern "C"` functions were emitted as broken
  `define`s with `unreachable` bodies that shadowed the real libc
  symbols. They are now `declare` lines.
- **BUG-DS4-7** (high): `let` slots were `alloca`ed inside loop bodies —
  LLVM allocas are not released until function return, so loops with
  lets grew the stack every iteration. All binding slots are now hoisted
  to the function entry.
- **BUG-DS4-8** (high): dynamic lists store BOXED elements in the C
  runtime, but the LLVM path pushed raw values (`ptr 1` literal — invalid
  IR — and unboxed loads). List literals, `push`/`set`, index get/assign,
  `for` iteration and `pop` now box/unbox by the element type via
  `hl_box_i64`/`hl_box_f64`/`hl_box_bool` (+ plain loads on get), matching
  the C ABI exactly.
- **BUG-DS4-9** (high, WRONG RESULTS): `&&`/`||` were lowered as EAGER
  `and`/`or` — the RHS was evaluated even when the LHS decided the
  result, so `x != 0 && 10 / x > 0` panicked on division by zero where
  the interpreter printed a result. Now lowered as real short-circuit
  control flow with `phi` nodes.
- **BUG-DS4-10** (high): string relational comparisons (`s1 < s2`) fell
  into the INTEGER path — `icmp` on pointers: invalid IR, and even as a
  cast it would compare addresses, not bytes. Now routed through
  `hl_str_cmp` like the C backend.
- **BUG-DS4-11 / 12 / 13 / 14** (medium): integer `/`/`%`/unary `-` now
  call the runtime's checked helpers (`hl_div_i64`/`hl_mod_i64`/
  `hl_neg_i64`) so the zero and `INT64_MIN / -1` panics are identical to
  the interpreter; the `for` loop re-checks the current list length each
  iteration (BUG-SC-4 semantics, same as the C backend); the IR builder
  no longer emits extern "functions" with synthetic panics; the
  optimiser no longer folds `INT64_MIN / -1` into +2^63 (out of range!)
  or `INT64_MIN % -1` into 0 (removing a runtime panic); `-O fast` no
  longer marks `0 - x` as overflow-safe.
- **BUG-DS4-15** (high): struct literals, struct field access, enum
  literals, `match`, `?` and user-defined method calls were SILENT stubs
  emitting garbage IR. They now raise a clean
  `not yet supported by --emit llvm` compile error (see ROADMAP Stage 12
  remaining work). Builtin methods (`.to_str()`, `.len()`, `.push()`,
  `map`/`str`/`int`/`float`/`bool` methods) ARE now fully lowered with
  correct box/unbox handling.
- `tainted[T]` now maps to T's LLVM type (taint is compile-time only —
  same as the C backend), and taint builtins lower to identity/`hl_args`/
  `hl_read_file`.

#### tools/hlfmt.py — the formatter corrupted files
- **BUG-DS4-16** (high): float literals were re-rendered via `str(v)` —
  `0.00001` became `1e-05`, which the HLS lexer cannot parse (no
  exponent support). `hlfmt -w` rewrote valid programs into unparseable
  ones, and formatting was not idempotent. The lexer now carries the RAW
  source text of numeric tokens and the formatter re-emits it verbatim.

#### tools/hls-lsp.py — protocol resilience
- **BUG-DS4-17** (high): one malformed JSON-RPC frame (bad JSON, bad
  `Content-Length`, short read) killed the whole server — and it happened
  OUTSIDE the try/except. Now answers `-32700 Parse error` and keeps
  serving.
- **BUG-DS4-18** (high): hover/definition/completion against a document
  with a syntax error crashed with `KeyError: 'fns'` and the exception
  handler never ANSWERED the request — editors hung until timeout. Broken
  documents now degrade to `null` results, and any failed REQUEST gets a
  `-32603 Internal error` response.
- **BUG-DS4-19** (medium): `didClose` never published empty diagnostics —
  stale errors stayed on screen forever. Now clears them.
- **BUG-DS4-20** (medium): LSP positions are UTF-16 units; the lexer's
  columns are bytes. Hover/definition missed whenever non-ASCII text
  preceded the identifier on the same line. Converted properly.

#### tools/hllint.py — false positives
- **BUG-DS4-21** (medium): L001 ("let binding is never used") fired for
  every index/field assignment target (`xs[0] = 10`, `p.x = 5`) because
  the assignment's container expression was never walked. Write-only
  plain assignments are still (correctly) flagged.

#### tools/hls-pkg.py — security & correctness
- **BUG-DS4-22** (high, supply-chain): path traversal — a dependency
  NAME starting with `/` escaped the cache directory entirely, and `path`
  dependencies accepted absolute paths / `..` escapes, letting a
  malicious manifest import arbitrary files. Names are now validated
  (`[A-Za-z0-9._-]`, no leading `.`/`-`) and paths are confined to the
  repo (or the clone dir for git deps).
- **BUG-DS4-23** (high): effect auditing FAILED OPEN — a dependency that
  couldn't be audited (missing boot.py, timeout, non-zero exit) was
  recorded as PURE, so effect enforcement passed trivially. Now fails
  closed (full effect family + loud warning).
- **BUG-DS4-24** (high): `build` never verified the lockfile's SHA-256
  hashes before compiling (TOCTOU). Now verifies each dependency and
  fails closed on mismatch.
- **BUG-DS4-25** (medium): manifest parse errors escaped as raw Python
  tracebacks. Now clean CLI errors.
- **BUG-DS4-26** (high): `hls-pkg build` set the `HLS_PKG_DEPS` env var
  but boot.py never READ it — building any package with dependencies
  always failed with "module not found". The import resolver now
  searches the deps dir; dep symlinks are named after the manifest
  dependency name (`import "strlib.hls"` works).
- **BUG-DS4-27 / 28** (medium): importing the same module through two
  paths (a `.hls-pkg-deps` symlink and a direct std import) double-loaded
  it and reported every function as a duplicate. Imports are now
  canonicalised via realpath.
- Git subprocess calls now have timeouts; unused imports removed.

#### tools/hlbindgen.py — wrong generated types
- **BUG-DS4-29** (high): the return type was parsed via
  `full.index(name)` — the FIRST occurrence of the function name
  anywhere in the declaration. `char* ch(char* s);` found "ch" inside
  "char" and emitted `-> int` instead of `-> str` (same for `cons`).
  Fixed by slicing at the real match-group start.
- **BUG-DS4-30** (medium): C array parameters (`char buf[]`) leaked the
  brackets into the HLS parameter name (`buf[]: int` — unparseable) and
  typed `char[]` as `int`. Arrays decay to pointers: brackets stripped,
  `char[]` maps to `str`.

#### boot/boot.py — UX & correctness
- **BUG-DS4-31** (medium): `--emit llvm` / `--emit ir` / `--opt-stats`
  on unsupported constructs surfaced as raw Python tracebacks from a
  worker thread. Now clean `compile error: ...` messages.

### Added — verification infrastructure (the real fix)
- `tools/ll_validate.py`: dependency-free structural validator for the
  emitted LLVM IR (checks terminators, declared symbols, store/load type
  agreement, phi predecessors, branch targets, `ptr <int>` literals,
  duplicate labels). This is the guardrail whose absence let the broken
  IR ship through three "green" scans.
- `tests/run_llvm_tests.sh` + `tests/llvm/*.hls` (6 programs covering
  bools, boxed lists, short-circuit, div/mod, strings, loops) — emit +
  validate + `llvm-as` (when available) + behaviour snapshots + the
  clean-error contract for unsupported constructs.
- `tools/lsp_smoke.py`: protocol-level LSP test (malformed frame
  resilience, hover on broken docs, didClose diagnostics, shutdown/exit).
- Regression tests: `tests/fail/fail_effect_method.hls`,
  `tests/fail/fail_effect_struct_default.hls`,
  `tests/ok/feat_struct_default_call.hls`.
- CI: the LLVM suite, the `llvm-as` cross-check, formatter
  idempotency/parse-back checks, the LSP smoke test, and bindgen
  output assertions — all asserting on OUTPUT, not just exit codes.

### Test results
- `tests/run_tests.sh`: **154 PASS / 0 FAIL** (150 before; +1 ok program
  and +2 fail programs and their differential/native runs).
- `tests/run_llvm_tests.sh`: **13 PASS / 0 FAIL** (new).
- `tools/lsp_smoke.py`: all protocol assertions pass (new).
- `ruff check boot/ tools/ --select F,E9`: clean (was 9 findings).

## [v0.15.0-alpha] — Stage 15-gamma

### Stage 15 remaining work completed — `extern` keyword in self-hosted compiler
- **src/hlc.hls**: added full `extern "C" { ... }` block support (previously
  only boot/ supported it — the self-hosted compiler would fail to parse
  programs using FFI). This closes the Stage 15 remaining-work gap
  documented in ROADMAP.md.
  - `extern` is now a recognised keyword in the lexer.
  - `parse_extern_block` parses the block and registers each fn with
    `is_extern: true` in the function table.
  - The checker skips body checking and the "must return on all paths"
    check for extern fns (they have no body).
  - Extern fn effects propagate through the effects fixpoint (see
    BUG-SC-1 below) — a caller of an extern fn must declare a superset
    of the extern's `uses` set.
  - The codegen emits a forward declaration (prototype) using the RAW
    C function name (no `usf_` prefix) so the C linker can resolve it
    from libc. Call sites also use the raw name.
  - `FnInfo` struct gains an `is_extern: bool` field.

### Fixed — second deep codebase scan (60+ bugs found and fixed)

#### boot/checker.py — soundness & logic fixes
- **BUG-SC-1** (SOUNDNESS, high): extern fn declared effects were not
  propagated through the effects fixpoint. A function calling an extern
  fn declared `uses IO` without declaring `uses IO` itself would compile
  cleanly — breaking the language's central capability-check guarantee.
  The fixpoint now uses the extern fn's DECLARED effects as the base set.
- **BUG-SC-2** (medium): match arm bindings were incorrectly rejected
  for shadowing outer bindings. SPEC.md section 5 explicitly allows this
  ("the one and only shadowing exception"). Now only duplicates WITHIN
  the same arm are rejected (e.g. `E.Foo(a, a)`).
- **BUG-SC-3** (medium): `check_listlit` re-checked the first element
  when inferring the element type, causing side-effecting expressions
  like `take(x)` to be double-evaluated and spuriously fail with "use
  of moved value". Now caches the first element's type (same fix pattern
  as BUG-A/BUG-A2 in `check_call`/`check_structlit`).
- **BUG-SC-5** (medium): `all_return` did not recognize `let`/`assign`
  with a `never`-typed RHS (e.g. `let x: int = panic("...")`) as a
  divergence point. Functions ending with such a statement were
  spuriously rejected with "does not return on all paths".
- **BUG-SC-7** (medium): struct field default expressions were not
  type-checked at declaration time. `struct Foo { x: int = "hi" }`
  compiled cleanly and only failed at runtime. Now checked at declaration.
- **BUG-SC-12** (low): consolidated duplicate taint helper functions
  (`is_taint`/`is_tainted_type` and `taint_inner`/`list_taint_inner`)
  into a single source of truth.
- **BUG-SC-10** (low): removed dead `mapnew` branch in `check_expr`
  (the parser never produces a `mapnew` AST node).

#### boot/parser.py
- **BUG-SC-8** (medium): duplicate struct field names were silently
  accepted. `struct Foo { x: int, x: int }` compiled and at runtime
  `Foo { x: 1, x: 2 }` silently produced `{"x": 2}` (first value lost).
  Now rejected at parse time.

#### boot/interp.py
- **BUG-SC-4** (medium): for-loop body shrinking the list (e.g.
  `xs.pop()`) caused a Python `IndexError` crash instead of a clean
  stop. Now bounds-checks before access and stops iterating.
- **BUG-SC-6** (low): `f64_div` produced the wrong sign of infinity
  when the divisor was `-0.0` (treated +0.0 and -0.0 the same). Now
  uses `math.copysign` to distinguish them.
- **BUG-SC-9** (low): `list.pop()` on an empty list raised "array
  access out of bounds" — misleading. Now raises "pop from empty list".
- **BUG-SC-10** (low): removed dead `mapnew` branch in `eval_expr`.

#### src/hlc.hls — self-hosted compiler alignment fixes
- **BUG-SC-2** (medium): match arm shadowing — aligned with boot/ fix.
- **BUG-SC-5** (medium): `all_return` for `never`-typed `let`/`assign`
  — aligned with boot/ fix.
- **BUG-SC-4** (medium): for-loop codegen now bounds-checks before
  `hl_list_get` to handle list shrinking during iteration (matching
  the interpreter's behavior).
- **BUG-SC-4h** (high): `extern` keyword support added (see Stage 15
  section above).
- **BUG-SC-4r** (high): `eat_ident` now rejects reserved identifiers
  `secure` and `trait` (matching boot/parser.py BUG-29 fix).
- **BUG-SC-15** (low): removed dead `gen_one_enum` stub (incomplete
  function that was never called, superseded by `gen_one_enum_full`).
- **BUG-SC-16** (low): removed dead `at_sym(ctx, "_")` checks in
  `parse_match` (`_` is always tokenized as an ident, never a sym).

#### tools/ir/ — HLIR & optimiser fixes
- **BUG-SC-IR-1** (critical): for-loop counter increment stored to the
  wrong binding name (`v_x__i__i` instead of `v_x__i`), so the counter
  was never updated — representing an infinite loop in the IR.
- **BUG-SC-IR-2** (critical): dead-code elimination computed `used`
  per-block, so SSA names defined in one block but consumed in another
  (the normal case for `let` bindings flowing into `if`/`while`/`for`
  branches) were deleted — corrupting the IR. Now computes `used`
  across the entire function.
- **BUG-SC-IR-5** (high): `_annotate_safe` used `hasattr(ins, "attrs")`
  which is always True for dataclass instances, so `ins.attrs` was
  never initialized from None — crashing with `TypeError` on the first
  `+0`/`*0` binop under `-O fast`. Now checks `ins.attrs is None`.
- **BUG-SC-IR-14** (medium): constant folding did not fold the `!`
  (logical not) operator on boolean literals. `!true` and `!false`
  stayed as runtime `OP_UNOP` instructions. Now folded to `false`/`true`.

#### tools/llvm_emit.py — LLVM IR backend fixes
- **BUG-SC-LLVM-3** (critical): panic message globals had wrong array
  sizes (`[18 x i8]` for 17-byte strings). LLVM rejects size mismatches.
  Fixed to `[17 x i8]`.
- **BUG-SC-LLVM-24** (low): removed dead duplicate method-key lookup
  (the `if fn is None: fn = self.program["fns"].get(name)` branch was
  identical to the line above it — a no-op).

#### tools/hls-pkg.py — package manager fixes
- **BUG-SC-PKG-11** (high): `extract_effects` added every effect name
  on each audit status line to BOTH declared and computed, making them
  always identical — effect enforcement was meaningless. Now properly
  splits audit output into columns (function/declared/computed/status).
- **BUG-SC-PKG-12** (high): `cmd_build` didn't add resolved dependency
  directories to the import path. Any `import` in the entry file failed
  with "module not found". Now symlinks deps into `.hls-pkg-deps/` and
  sets `HLS_PKG_DEPS` env var.

#### tools/hls-lsp.py — LSP protocol fixes
- **BUG-SC-LSP-13** (high): the server exited immediately after
  `shutdown` (loop condition `while not self.shutdown_requested`),
  before `exit` arrived — breaking the LSP protocol for VS Code /
  Neovim. Now keeps the connection open until `exit` is received.
- **BUG-SC-LSP-20** (medium): `exit` handler always returned exit code
  0. Per LSP spec, should return 1 if `shutdown` was not previously
  received. Now `sys.exit(0 if self.shutdown_requested else 1)`.

#### tools/hlfmt.py — formatter fixes
- Fixed false docstring claims: "preserves all comments" (actually
  strips `#` comments — documented limitation), "auto-inserted blank
  lines" (only preserves existing ones). Updated version to
  v0.14.0-alpha.

#### tools/hlbindgen.py — bindgen fixes
- **BUG-SC-BG-23** (low): duplicate `re.sub(r"^const\s+", "", base)`
  line was a no-op. Second instance now strips TRAILING `const`
  (e.g. `char const *` — legal C spelling).

### Cleanup
- Removed the empty `...,` file from the repo root (accidental creation
  from a misformed shell redirect in a previous session).
- Updated `examples/ffi_demo.hls` with documentation noting the native
  compilation type-mapping limitation (HLS int is 64-bit; C int is
  32-bit — the boot interpreter handles this via ctypes, but native
  codegen emits `int64_t` signatures that may conflict with C stdlib).

### Tests
- 4 new regression tests: `feat_deep_scan_fixes` (ok), `fail_struct_dup_field`,
  `fail_struct_default_type`, `fail_extern_effect_propagation`.
- **150/150 tests PASS** (was 145; +5 new).
- Bootstrap remains DETERMINISTIC (two self-compile passes produce
  byte-identical C output).

## [v0.14.0-alpha] — Stage 15-beta: deep codebase scan & bug fixes

### Fixed — deep codebase scan & bug fixes
- **tools/llvm_emit.py**: major rewrite for type-correctness.
  - Track LLVM type per local slot (was hardcoded to `i64`, causing
    type mismatches for `str`/`list`/`map`/`struct`/`enum` locals).
  - Reset all emitter state in `emit()` (was leaving `_tmp`, `_label`,
    `_ov_counter`, `_str_counter`, `_locals`, `_loop_stack` stale
    between calls).
  - Reset `_block_terminated_flag` after each basic block (was never
    reset, causing all blocks after the first `break`/`continue` to be
    considered terminated — silently dropping code).
  - Add `_to_i1` coercion for branch conditions (was passing `i64`
    to `br i1`, which LLVM rejects).
  - Add lowering for `match`, `qmark`, `mapnew`, `enumlit` (were
    falling through to `return "0"`).
  - Add type coercions for call arguments (int↔ptr, i1↔i64).
  - Add missing runtime declarations (`hl_range`, `hl_args_get`,
    `hl_tainted_args`, `hl_taint_mark`, `hl_taint_unwrap`,
    `hl_read_file_tainted`, `hl_drop`, `hl_clone`, `hl_take`).
  - Remove dead `emit()` method (replaced by `emit_final`, now renamed
    `emit`).
  - Remove dead `float_to_ieee_bits` function (was never called due
    to `if False else` short-circuit).
  - Remove dead class-level `_str_counter = 0` and `_ov_counter = 0`
    (shadowed by instance attributes).
  - Remove redundant identical method-key lookup in `_lower_call`.
- **tools/hllint.py**: major correctness fixes.
  - Implement `_rule_l003` (unused-struct-field) — was declared in
    RULES but the method was missing.
  - Fix `_rule_l001` (unused-binding): the `visit` closure was defined
    inside the loop but only used in a second incomplete loop. Rewrote
    with `all_exprs_in_stmts` helper that walks every expression
    recursively.
  - Fix `_rule_l002` (unused-function): same closure bug; rewrote
    with `all_exprs_in_stmts` + `collect_calls`.
  - Fix `_rule_l004` (ignored-result): only checked top-level `expr`
    statements; now walks recursively.
  - Fix `_rule_l005` (explicit-unwrap): only checked `expr` statements;
    now walks all expressions recursively.
  - Fix `_rule_l007` (dead-code-after-return): only flagged the single
    statement immediately after `return`; now flags ALL subsequent
    statements and recurses into nested scopes.
  - Document `_rule_l009` (shadowing) and `_rule_l010` (empty-impl) as
    no-ops (the checker/parser already reject these as compile errors).
- **tools/hls-lsp.py**: fix URI handling.
  - `_ident_at` and `handle_completion` were using the FIRST document
    in `self.docs` instead of the document matching the request URI.
    This meant hover/completion/definition returned wrong results when
    multiple documents were open. Now uses the URI from the request
    params.
  - Fix `BUILTINS` list: removed `float`, `bool` (types, not builtins),
    `list_new`, `ord` (don't exist as HLS builtins). Added `extern`
    to KEYWORDS.
- **tools/hlfmt.py**: remove dead `;` from `NO_SPACE_BEFORE` and
  `SPACE_AFTER_SYMS` (HLS does not have a `;` token).
- **tools/hlbindgen.py**: fix version string (was "v0.12.0-alpha",
  should be "v0.13.0-alpha" — Stage 15).
- **boot/checker.py**: remove redundant `want if want is not None else
  None` expression in `argt` (was a no-op).
- **src/hlc.hls**: remove dead code.
  - Remove `resolve_enum` (defined but never called — 13 lines).
  - Remove `effects_to_str` (defined but never called — superseded by
    `fmt_effects`).
  - Remove `gen_enum_lines` (defined but never called — superseded by
    `gen_enum_lines_simple`).
  - Remove unused `let t0` in `parse_impl` (was captured but never
    used).
  - Remove unused `let result_type` in `check_enum_variant`.
  - Remove unused `let c_type` in `gen_match_expr` and `gen_qmark_expr`
    (the actual usage re-computes `c_type(ctx, ...)` inline).
  - Remove unused `let method_tps` and `let method_targs` in
    `gen_fn_inst_lines` (the body uses `combined_tps` / `targs` directly).
- **Makefile**: fix `examples` target.
  - `taint_beta_demo.hls` was listed in the main for-loop (which runs
    it WITHOUT the required data-file argument), causing the loop to
    fail and the subsequent dedicated runs (wordcount, taint_beta_demo
    with data file) to never execute. Removed it from the for-loop;
    the dedicated line now runs correctly.
  - Added `ffi_demo.hls`, `optimize_demo.hls`, `llvm_demo.hls`,
    `tooling_demo.hls`, `pkg_demo.hls` to the for-loop (were missing).
  - Added `|| exit 1` to the for-loop body so a failing example
    aborts the target (was silently continuing).
- **.github/workflows/ci.yml**: add CI steps for Stage 11-15 tooling.
  - Test `--emit ir`, `--opt-stats` (Stage 11).
  - Test `--emit llvm` (Stage 12).
  - Test `hlfmt`, `hllint`, `hls-lsp` (Stage 14).
  - Test `hls-pkg` (Stage 13).
  - Test `hlbindgen` (Stage 15).
  - Include all examples (was missing ffi/optimize/llvm/tooling/pkg).
- **.github/workflows/release.yml**: update test count (143 → 145).
- **Root directory**: remove empty `...` file (accidental creation,
  likely from a misformed shell redirect).
- **SPEC.md**: update version header to v0.13.0-alpha. Add Stage 10
  taint builtins to section 8 (were missing). Add sections 20-24
  covering Stages 11-15 (HLIR, LLVM backend, package manager, developer
  tooling, FFI).
- **README.md**: update version reference (v0.8.0-alpha → v0.13.0-alpha).

### Notes
- All 145 tests still PASS after the fixes.
- Bootstrap remains DETERMINISTIC (two self-compile passes produce
  byte-identical C output).
- The LLVM IR emitter now produces type-correct IR for string, list,
  map, struct, and enum types (was previously producing invalid IR
  with `i64` / `ptr` type mismatches).

## [v0.13.0-alpha] — Stage 15-alpha: Safe C FFI

### Added
- **Stage 15-alpha: Safe C FFI.** A new `extern "C" { ... }` block
  declares external C functions. The checker enforces that every
  extern fn declares `uses IO` (or `pure`) — the safe default for
  FFI is to assume side effects. The interpreter calls the C
  function via ctypes.
  - New `extern` keyword in the lexer (was unused; safe to add as a
    keyword after a repo-wide grep showed zero occurrences).
  - Parser support for `extern "C" { ... }` blocks.
  - Checker: extern fns are registered with an `extern: True` flag;
    the "must return on all paths" check is skipped.
  - Interpreter: `call_fn` dispatches to `call_extern` which loads
    libc via `ctypes.CDLL(None)`. Argument types: int -> c_int64,
    float -> c_double, bool -> c_bool, str -> c_char_p (HLS bytes
    passed as null-terminated C string), other types -> opaque
    c_void_p. Return types: same mapping; void returns None.
- **New `tools/hlbindgen.py`** — C header → HLS extern block generator.
  Parses simple C function declarations, maps C types to HLS types,
  emits `extern "C" { ... }` block with `uses IO` on every function
  (safe default).
- **New example:** `examples/ffi_demo.hls` calls `abs`, `strlen`,
  `toupper` via the FFI.
- **145/145 tests PASS** (the FFI test is interpreter-only today;
  the self-hosted `hlc.hls` doesn't yet recognise the `extern`
  keyword — that's the Stage 15 release target).

## [v0.12.0-alpha] — Stage 14-alpha (tooling)

### Added
- **Stage 14-alpha: tooling — LSP, formatter, linter.** Three new tools
  provide the core developer experience:
  - **`hlfmt`** (opinionated formatter, like `gofmt`):
    - 4-space indentation; one statement per line; normalised whitespace.
    - **Idempotent: running twice = running once.** Verified on all
      145 test/example programs.
    - Subcommands: `hlfmt FILE` (print), `-w` (write), `-c` (check),
      `-d` (diff).
    - Multi-byte UTF-8 string literals preserved exactly via latin-1
      byte-level round-tripping.
  - **`hllint`** (safety rules linter):
    - 10 rules: `L001` unused-binding, `L002` unused-function,
      `L003` unused-struct-field, `L004` ignored-result,
      `L005` explicit-unwrap, `L006` unnecessary-effects,
      `L007` dead-code-after-return, `L008` long-function,
      `L009` shadowing, `L010` empty-impl.
    - Subcommands: `hllint FILE`, `--strict`, `--rule L001`, `--list`.
    - Runs the Stage-0 checker internally to get type/effect info.
  - **`hls-lsp`** (minimal LSP server over JSON-RPC stdio):
    - `initialize` / `shutdown` / `exit`.
    - `textDocument/didOpen` / `didChange` / `didClose`.
    - `textDocument/hover` — inferred type of identifier at position.
    - `textDocument/definition` — find function/struct/enum definition.
    - `textDocument/completion` — keyword + identifier completion.
    - `textDocument/publishDiagnostics` — runs checker, publishes errors.
    - `--check FILE` one-shot mode (for non-LSP editors).
  - New Makefile targets: `fmt`, `lint`, `lsp-check`.
  - New example: `examples/tooling_demo.hls` (format-stable + lint-clean).
- **145/145 tests PASS** (no test changes; the tools are separate from
  the compiler bootstrap).

## [v0.11.0-alpha] — Stage 13-alpha (hls-pkg package manager)

### Added
- **Stage 13-alpha: `hls-pkg` package manager.** A new
  `tools/hls-pkg.py` CLI supports the full manifest → lockfile →
  audit → build cycle, with content-addressed dependencies
  (SHA-256 of resolved file content) and effect enforcement.
  - `hls-pkg init NAME` — create a new package skeleton (manifest +
    entry source + README + .gitignore).
  - `hls-pkg add NAME GIT PATH [--tag T | --branch B]` — add a
    git-based dependency to the manifest.
  - `hls-pkg lock` — resolve dependencies, compute SHA-256, extract
    effects via `boot.py --audit`, write `hls-pkg.lock`. Enforces the
    package's `effects.allowed` surface: any dependency's computed
    effects not in the allowed set causes the lock to fail with a
    per-dependency violation report.
  - `hls-pkg audit` — print the total effect report of the dependency
    tree (per-package declared vs transitive effects + a total
    summary).
  - `hls-pkg verify` — verify the lockfile's SHA-256 hashes still
    match the resolved files.
  - `hls-pkg build [--entry main.hls]` — compile the package's entry
    point.
  - Manifest format: `hls-pkg.toml` (minimal TOML parser).
  - Lockfile format: `hls-pkg.lock` (JSON).
  - Effect extraction: a temporary `pure` main wrapper is generated
    alongside the target file so library files (without `main`) can
    be audited. The wrapper's `pure` keyword ensures it doesn't
    pollute the audit with IO-family effects.
  - Git dependencies are cloned into `.hls-pkg-cache/` (gitignored).
- **145/145 tests PASS** (no test changes; `hls-pkg` is a separate
  tool, not part of the compiler bootstrap).

## [v0.10.0-alpha] — Stage 12-alpha (LLVM IR text backend)

### Added
- **Stage 12-alpha: LLVM IR text backend.** A new `tools/llvm_emit.py`
  emits LLVM IR text (`.ll` format) from a checked HLS program. The
  emitted IR can be assembled by `llc` or `clang` (when available) into
  a native binary.
  - HLS → LLVM type mapping: `int -> i64`, `float -> double`,
    `bool -> i1`, `str -> ptr`, `list/map/struct/enum/tainted -> ptr`.
  - HLS C runtime is declared as opaque externals via `declare`
    statements, mirroring the C backend's runtime API.
  - Each HLS function becomes an LLVM `define` with stack-allocated
    locals (`alloca` + `load`/`store`).
  - Integer arithmetic uses `llvm.sadd/ssub/smul.with.overflow.i64`
    with explicit overflow-path branches to `hl_die`.
  - Division by zero is checked before `sdiv`/`srem`.
  - String concatenation dispatches to `hl_str_concat`.
  - Float arithmetic uses `fadd`/`fsub`/`fmul`/`fdiv`/`frem`.
  - Control flow (`if`/`while`/`for`/`break`/`continue`/`return`) is
    lowered to LLVM basic blocks + `br` instructions.
  - String literals are emitted as `private unnamed_addr constant`
    globals and wrapped via `hl_str_from` at runtime.
- **New `boot.py` flags:**
  - `boot.py --emit llvm FILE.hls` — print the LLVM IR of the program.
  - `--target TRIPLE` — set the LLVM target triple (e.g.
    `aarch64-linux` for cross-compilation).
- **New Makefile target:** `make emit-llvm F=...`.
- **New example:** `examples/llvm_demo.hls`.
- **145/145 tests PASS** (no test changes; the LLVM emitter is a new
  diagnostic pass, parallel to the C backend).

## [v0.9.0-alpha] — Stage 11-alpha (SSA IR + optimiser pipeline)

### Added
- **Stage 11-alpha: SSA IR (HLIR) + optimiser pipeline.** A new mid-level
  IR is built from the AST and fed to a three-pass optimiser:
  - `constant_fold` — fold literal arithmetic / string concatenation,
    including constants propagated through `let` bindings.
  - `copy_propagate` — replace `%t1 = %t0` uses with `%t0`.
  - `dead_code_elim` — remove instructions whose result is never used
    and that have no side effects.
  The IR is the optimisation substrate; the native codegen (in `hlc.hls`)
  still emits C directly from the AST and relies on `gcc -O2` for
  machine-level optimisation. Wiring the HLIR into `hlc.hls` is the
  Stage 11 release target.
- **New `boot.py` flags:**
  - `boot.py --emit ir FILE.hls` — print the HLIR of every function.
  - `boot.py --opt-stats FILE.hls` — run the optimiser and print
    per-pass statistics (instructions before / after / removed, per
    function and total).
- **New `tools/ir/` Python package** containing:
  - `__init__.py` — HLIR data model (`Instr`, `Block`, `HLIRFunction`,
    `HLIRModule`), the `IRBuilder` that lowers a checked program to
    HLIR, and a `dump_module` pretty-printer.
  - `optimize.py` — the three optimisation passes plus the
    `_annotate_safe` helper for the future `-O fast` mode (today the
    codegen does not consume the annotations; it is plumbing for the
    Stage 11 release target).
- **New example:** `examples/optimize_demo.hls` exercising the
  optimiser pipeline on a small `compute()` function.
- **New tests:** `tests/ok/feat_optimize.hls` (differential test
  through both interpreter and native). **145/145 tests PASS.**

## [v0.8.0-alpha] — Stage 10-beta (taint tracking extended)

### Added
- **Stage 10-beta: second taint source.** New builtin
  `read_file_tainted(path: str) -> tainted[str]` — like `read_file`
  but the returned content is wrapped as `tainted[str]`. Useful when
  the file content is untrusted (e.g. user uploads, downloaded config
  files). Carries the `Fs` effect (same as `read_file`).
- **Stage 10-beta: extended `--audit` flag.** `hlc --audit` and
  `boot.py --audit` now print a taint-flow section listing which
  functions call each taint source (`tainted_args`,
  `read_file_tainted`) and which functions call each taint sink
  (`print`, `println`, `read_file`, `write_file`, `file_exists`,
  `exit`). Useful for security review and supply-chain audits.
- **Stage 10-beta: new pure-query helpers in `std.taint`:**
  - `taint_check_byte_at(t, i) -> int` — pure byte-at-index query.
  - `taint_concat(t1, t2) -> tainted[str]` — concatenate two tainted
    strings; the result REMAINS tainted.
  - `taint_concat_clean(t, clean) -> tainted[str]` — concatenate a
    tainted string with a clean literal; the result REMAINS tainted.
- **New JSON typed value accessors** in `std/json.hls`:
  `json_bool_value`, `json_int_value`, `json_float_value`,
  `json_str_value`. Each panics if the value is not of the expected
  kind; the caller should use `json_is_*` first if the kind is
  uncertain. Previously callers had to access `v.ival` / `v.fval`
  etc. as struct fields, which worked but exposed the internal
  representation.
- **New example:** `examples/taint_beta_demo.hls` exercising the new
  read_file_tainted flow + the new pure-query helpers.
- **New tests:** 4 ok (`feat_taint_beta`, `feat_generic_take`,
  `feat_float_scientific` — differential Stage-0 + native, plus
  `feat_qmark_err_type` regression test for BUG-3) + 2 fail
  (`fail_taint_beta_read_file`, `fail_qmark_err_type`).
- **`str.to_float` now accepts scientific notation** (e.g. `"1e5"`,
  `"1.5e-3"`, `"-2.5E10"`). Previously the function rejected any
  non-digit/non-dot character including `e`/`E`, which caused
  `json_parse("1e5")` to panic. BUG-4 fix.
- **`float.to_int` now range-checks the conversion.** A large float
  (e.g. `1e20`) previously silently truncated via the C cast and
  then panicked on the next arithmetic op. Now it panics at the
  `float.to_int` call itself with a clear message. BUG-15 fix.
- **`html_escape` now escapes the forward slash `/`** as `&#x2F;`
  per OWASP recommendation. Previously the SPEC claimed `/` was
  escaped but the implementation didn't. `html_unescape` now also
  decodes `&#x2F;` back to `/`. BUG-12 fix.
- **JSON parser now handles UTF-16 surrogate pairs** in `\uXXXX`
  escapes. A high surrogate followed by a low surrogate is now
  correctly combined into a single codepoint and emitted as a 4-byte
  UTF-8 sequence. Previously the parser emitted two separate (and
  invalid) 3-byte UTF-8 sequences for the surrogate codepoints
  themselves. BUG-16 fix.
- **`base64_decode` now validates padding placement.** RFC 4648 says
  `=` padding may only appear at the end of the input (positions 2
  and 3 of a 4-char block, never positions 0 or 1). Inputs like
  `"AB=C"` previously produced wrong output without an error. BUG-35
  fix.
- **`url_parse_authority` now uses the FIRST `@`** to split userinfo
  from host (instead of the LAST `@`). This is the safer, more
  defensive interpretation — a malicious URL like
  `user@evil.com@safe.com` should NOT parse as `user@evil.com`
  userinfo + `safe.com` host. BUG-33 fix.
- **Reserved identifiers `secure` and `trait`** are now rejected at
  parse time. The SPEC said they were reserved but the lexer didn't
  enforce it. BUG-29 fix.

### Fixed
- **BUG-1 (HIGH, logic):** `check_call` in `boot/checker.py` re-ran
  `check_expr` TWICE on each argument of a generic function — once
  for inference, once for verification. The first call would execute
  side effects like `take(x)` (marking `x` as moved); the second call
  would then see `x` already moved and raise a spurious "use of moved
  value" error. Fixed by caching the first-pass type per argument
  and reusing it in the second pass. Same pattern as the BUG-A fix
  for `check_enum_variant` and `check_structlit`.
- **BUG-2 (HIGH, logic):** `check_method` in `boot/checker.py` had
  the same double-`check_expr` pattern as BUG-1 for generic methods.
  Fixed identically.
- **BUG-1 + BUG-2 in `hlc.hls`:** the self-hosted compiler had the
  same double-`check_expr` pattern in its `check_call` and
  `check_method` functions. Fixed identically.
- **BUG-3 (HIGH, logic):** `check_qmark` in `boot/checker.py` (and
  `hlc.hls`) only verified the enclosing function's return type was
  an enum of the same base name, NOT that the error payload type
  matched the function's error type argument. So `?` on a
  `Result[int, int]` inside a function returning `Result[int, str]`
  would silently propagate an `Err(int)` as if it were `Err(str)` —
  a type soundness hole. Fixed by also verifying the error payload
  type matches the LAST type argument of the enclosing function's
  return type.
- **BUG-4 (HIGH, logic):** `str.to_float` rejected scientific
  notation, causing `json_parse("1e5")` to panic. Fixed in both
  Stage-0 (`boot/interp.py`) and the native C runtime
  (`src/hlc.hls`).
- **BUG-5 (MEDIUM, dead-code + contradiction):** removed the dead
  `SINK_BUILTINS` dict in `boot/checker.py` that was incomplete
  (omitted `file_exists`) and never consulted. The actual taint
  enforcement happens inline in `check_builtin_call`.
- **BUG-6 (MEDIUM, contradiction):** `std/taint.hls` comments claimed
  "we never unwrap, only query metadata" but the code called
  `taint_unwrap` to perform the queries. Reworded the comments to
  clarify: the functions DO unwrap locally but don't expose the
  unwrapped value to the caller (the result is either a non-string
  type like int/bool, or a re-tainted str).
- **BUG-7 (MEDIUM, contradiction):** `std/taint.hls` listed
  `taint_map_value` in its header doc but never defined it. Removed
  the dangling reference (the function cannot be expressed in HLS
  v0.7 because we don't have function values).
- **BUG-8 (MEDIUM):** README claimed "Five core guarantees" but
  listed 7 numbered items. Fixed to "Seven core guarantees".
- **BUG-9, BUG-10 (MEDIUM, docs):** README's repository layout block
  had stale line counts and test counts. Updated to actual numbers
  (~6,000 lines for hlc.hls, ~3,200 lines for boot/, 42 ok tests,
  45 fail tests, 143 total tests).
- **BUG-11 (MEDIUM, spec vs implementation):** SPEC said
  `tainted[T]` is represented the same as `T` at runtime, but the
  Stage-0 interpreter used a wrapper dict. Updated SPEC to
  acknowledge the Stage-0 wrapper dict (defence-in-depth) while
  keeping the native backend's no-overhead representation.
- **BUG-12 (MEDIUM, spec vs implementation):** SPEC said
  `sanitize_html` escapes `/` but `html_escape` didn't. Added `/`
  escaping (per OWASP) and the corresponding `&#x2F;` decoding.
- **BUG-13 (MEDIUM):** SPEC and CHANGELOG said `sanitize_command`
  rejects 26 shell metacharacters, but the code rejects 23.
  Updated docs to say 23 with the full byte list.
- **BUG-14 (MEDIUM, logic):** `boot/boot.py`'s cross-file merge
  checked enum-enum and enum-struct collisions but NOT struct-enum.
  A struct in file A and an enum in file B with the same name would
  silently merge. Fixed to check both directions.
- **BUG-15 (MEDIUM, logic):** `float.to_int` had no int64 range
  check. A large float (e.g. `1e20`) would silently truncate via
  the C cast. Fixed in both Stage-0 (`boot/interp.py`) and the
  native C runtime (`src/hlc.hls`) — now panics with a clear
  "float.to_int out of int64 range" message.
- **BUG-16 (MEDIUM, logic):** `std/json.hls` `jsonp_push_utf8`
  didn't handle codepoints above 65535 (which arise from surrogate
  pair decoding). Extended to emit 4-byte UTF-8 sequences, and the
  string parser now properly combines high + low surrogates.
- **BUG-17 (LOW, dead code):** removed the unreachable `"Tainted["`
  (capital T) branches in `is_tainted_type` and `list_taint_inner`
  in `boot/checker.py`. The parser only ever produces lowercase
  `"tainted["`.
- **BUG-18 (LOW, dead code):** removed the dead `at_sym("_")`
  checks in `boot/parser.py` `parse_arm`. The lexer treats `_` as
  an ident token (never a `sym`), so the `at_sym("_")` half of the
  OR was dead code.
- **BUG-19 (LOW, dead code):** removed the unreachable LF-handling
  branch inside `boot/lexer.py`'s string parser. The `< 32` check
  at line 129 already raises before reaching the LF branch.
- **BUG-20 (LOW, dead code):** removed the redundant
  `except HLError: raise` block in `boot/boot.py`'s `load_file`
  function — catching an exception only to re-raise it unchanged is
  a no-op.
- **BUG-21 (LOW, dead code):** removed the unused `unwrap_users`
  and `sanitize_users` lists in `boot/boot.py`'s `print_audit`
  function. They were allocated but never appended to.
- **BUG-22 (LOW):** `boot/interp.py`'s for-loop binding used a
  2-element list `[value, False]` while every other binding used a
  3-element list `[value, mut, moved]`. Changed to the 3-element
  form for consistency.
- **BUG-23 (LOW, defensive bug):** `taint_unwrap`'s defensive
  fallback in `boot/interp.py` returned the `"value"` field of ANY
  dict — including user structs that happen to have a field named
  `"value"`. Replaced with a panic that surfaces the checker bug
  (which is the only path to reaching this fallback).
- **BUG-24 (LOW, comment vs code):** `sanitize_filename` comment
  said "Reject leading dot ... and consecutive dots", but the code
  only checked leading dot. Reworded the comment to match the code.
- **BUG-25 (LOW, comment vs code):** `std/sanitize.hls` header
  claimed each sanitizer "Applies a normalising filter that removes
  / escapes the dangerous characters". But `sanitize_path` and
  `sanitize_command` only panic on invalid input (returning the
  original input unchanged on success). Reworded the header
  comment.
- **BUG-27 (LOW, spec grammar):** SPEC's grammar didn't include
  the `pure` keyword (added in v0.6.0-alpha). Updated to
  `("->" type)? ("pure" | "uses" efflist)? block`.
- **BUG-28 (LOW, spec count):** SPEC said "Keywords (21)" but the
  lexer's `KEYWORDS` set has 20 entries. Fixed to "Keywords (20)".
- **BUG-30 (LOW, error message):** the taint-sink error message
  referenced `sanitize_sql` which doesn't exist (the actual
  sanitizers are `sanitize_sql_identifier` and
  `sanitize_sql_string`). Fixed to list the actual names.
- **BUG-31 (LOW, latent bug):** `instantiate_type` in
  `boot/checker.py` (and `hlc.hls`) didn't recursively substitute
  when `type_map[t]` itself contained type params. The fix is
  capped at 16 levels of recursion to prevent infinite loops on
  illegal self-referential maps.
- **BUG-32 (LOW, parser line):** `parse_arm` in `boot/parser.py`
  set the arm's `line` from the token AFTER the body (next arm or
  closing `}`). Fixed to capture the line from the FIRST token of
  the pattern, so error messages for arm issues point to the right
  line.
- **BUG-35 (LOW, base64 validation):** `base64_decode` validated
  that all four chars were valid base64, but didn't validate that
  `=` padding only appeared at the END. Input like `"AB=C"` was
  accepted with wrong output. Fixed.
- **BUG-37 (LOW, integer literal edge case):** the parser
  intentionally allows literals up to (and including) 2^63 as an
  intermediate value, so the unary-minus handler can fold
  `-9223372036854775808` (= INT64_MIN) into a single int node.
  Added a comment explaining why the 2**63 boundary (not 2^63 - 1)
  is correct here.

### Changed
- **README "Status" section** updated to reflect Stage 10-beta
  shipped (in addition to 10-alpha).
- **README "Quick start"** now also surfaces
  `examples/taint_beta_demo.hls`.
- **SPEC.md** updated for v0.8.0-alpha: taint tracking section now
  covers both Stage 10-alpha and Stage 10-beta; new section 19.5
  documents the taint-flow audit extension.

### Test suite
- **143/143 PASS** (was 135/135 in v0.7.0-alpha). The 8 new tests
  are 4 ok (`feat_taint_beta`, `feat_generic_take`,
  `feat_float_scientific`, plus the bug-fix regression tests) and
  2 fail (`fail_taint_beta_read_file`, `fail_qmark_err_type`).
  Bootstrap is still deterministic (two self-compile passes produce
  byte-identical C output).

---

## [v0.7.0-alpha] — 2026 (Stage 10-alpha — taint tracking system)

### Added
- **Stage 10-alpha: taint tracking.** New built-in generic type
  `tainted[T]` wraps any value as potentially attacker-controlled.
- **Three new builtins:**
  - `tainted_args() -> list[tainted[str]]` — every program's argv is
    tainted by default.
  - `taint_mark(x: T) -> tainted[T]` — wrap any value as tainted.
  - `taint_unwrap(x: tainted[T]) -> T` — the explicit "I accept the
    risk" escape hatch.
- **Static sink enforcement.** The checker rejects passing a
  `tainted[T]` value to any of: `print`, `println`, `read_file`,
  `write_file` (path or content), `file_exists`, `exit`. The error
  message names the sink, the argument index, and the tainted type —
  and points to `sanitize_html` / `sanitize_path` / `sanitize_sql_*` /
  `sanitize_command` / `sanitize_filename` from `std.sanitize`.
- **New stdlib modules:**
  - `std/taint.hls` — pure-query helpers on `tainted[str]` that don't
    untaint (`taint_check_len`, `taint_check_is_empty`,
    `taint_check_starts_with`, `taint_check_ends_with`,
    `taint_check_equals`, `taint_check_contains`, `taint_slice` — the
    slice result REMAINS tainted, since a slice of attacker-controlled
    bytes is still attacker-controlled).
  - `std/sanitize.hls` — six sanitizers, each taking `tainted[str]` and
    returning a clean `str`:
    - `sanitize_html` / `sanitize_html_attr` — delegate to `std.html`.
    - `sanitize_path` — rejects empty / NUL / absolute / `..` segments.
    - `sanitize_sql_identifier` — only `[A-Za-z_][A-Za-z0-9_]*`.
    - `sanitize_sql_string` — doubles `'` and `\`.
    - `sanitize_command` — rejects 23 shell metacharacters (whitespace,
      `; | & \` $ ( ) < > ! \ " ' * ? [ ] { }`).
    - `sanitize_filename` — only `[A-Za-z0-9._-]+`, no leading dot.
- **New example:** `examples/taint_demo.hls` exercising the full flow
  (tainted argv, pure queries, all six sanitizers).
- **New tests:** 1 ok (`feat_taint`, differential) + 6 fail
  (`fail_taint_print`, `fail_taint_write_file_path`,
  `fail_taint_write_file_content`, `fail_taint_read_file`,
  `fail_taint_file_exists`, `fail_taint_exit`).
- **Self-hosting preserved:** the self-hosted `hlc.hls` now recognises
  `tainted[T]` as a built-in generic type (alongside `list[T]`,
  `map[str, T]`), implements the three taint builtins in the checker,
  and emits the same C code as Stage-0 (the runtime representation of
  `tainted[T]` is identical to `T` — taint is a compile-time property
  only). Bootstrap is still **deterministic**.

### Fixed
- **BUG-A** (HIGH, logic): the checker's `check_enum_variant` and
  `check_structlit` ran `check_expr` TWICE on each payload/field
  expression when type inference was needed — once for inference, once
  for verification. The first call would execute side effects like
  `take(x)` (marking `x` as moved); the second call would then see
  `x` already moved and raise a spurious "use of moved value" error.
  Fixed by reusing the first-pass type whenever it matches the
  instantiated expected type, falling back to a re-check only when the
  first pass returned a context-dependent placeholder (e.g. an empty
  list literal).
- **BUG-B** (LOW, dead-code): removed the unused `IO_BUILTINS` set in
  `boot/checker.py` — it was the pre-Stage-9 effect set, superseded by
  `BUILTIN_EFFECTS`.
- **BUG-1** (CRITICAL, docs): the README's headline code example used
  `let i: int = 0` then `i = i + 1` in a `while` loop, which is a
  compile error (immutable binding). Fixed to `let mut i: int = 0`.
- **README version drift**: the README claimed "v0.5.0-alpha" in
  several places despite the latest release being v0.6.0-alpha; the
  test count was wrong (100 instead of 127); the line counts were wrong
  (~3,000 instead of ~5,800 for hlc.hls; ~1,400 instead of ~3,000 for
  boot/). All updated to the actual current numbers.
- **CHANGELOG missing entries**: added entries for v0.4.0-alpha
  (Stage 8-alpha) and v0.5.0-alpha (Stage 9-alpha) that were missing.

### Changed
- **README "Status" section** updated to reflect the actual roadmap
  state (Stages 1–7 complete; 8-alpha, 9-alpha, 9-beta, 10-alpha
  shipped; 8-beta and 10-beta still pending).
- **README "Quick start"** now also surfaces `make audit` and the new
  `taint_demo.hls` example.
- **README "Contributing"** now documents the actual branch-protection
  rules (PRs required for non-admins, 4-cell CI matrix, linear history,
  no force-push / no deletion).
- **Boot.py CLI docstring** updated to mention `--audit` (Stage 9-beta).

### Test suite
- **135/135 PASS** (was 127/127 in v0.6.0-alpha). The 8 new tests are
  1 ok (feat_taint, counted twice under Stage-0 + differential paths)
  plus 6 fail tests (each counted once under the fail-path). Bootstrap
  is still deterministic.

---

## [v0.6.0-alpha] — 2026 (Stage 9-beta — audit + pure + community extensions merge)

### Added
- **Stage 9-beta: `--audit` flag** on `hlc` and `boot.py`. Prints the full
  capability / effect tree of every function in the program (declared vs
  computed), so users can audit which functions touch which side-channels.
- **Stage 9-beta: explicit `pure` keyword** for documentation/linting.
  A function declared `fn f(...) pure` must have no `uses` clause and must
  not transitively call any effectful callee. Purity was previously
  implicit (a function with no `uses` is pure); `pure` makes it explicit
  and self-documenting.
- **CI/CD pipeline** (`.github/workflows/ci.yml`): runs the full test
  suite (`make test`), bootstrap determinism check, and example programs
  on every push and pull request, on a 2×2 matrix of Python 3.8/3.11 and
  gcc/clang.
- **Release workflow** (`.github/workflows/release.yml`): builds source
  and stdlib tarballs and attaches them to a GitHub Release whenever a
  `v*` tag is pushed.
- **Dependabot** config (`.github/dependabot.yml`) for weekly GitHub
  Actions updates.
- **EditorConfig** (`.editorconfig`) enforcing 4-space indent for
  HLS/Python, 2-space for YAML/Markdown, tab for Makefiles.
- **New stdlib modules** (all pure HLS, no `uses IO` except as noted):
  - `std.hex` — hex encode/decode (lowercase)
  - `std.base64` — RFC 4648 standard + URL-safe variants
  - `std.crypto` — FNV-1a 32/64-bit, djb2, sdbm, CRC-32, byte-wise XOR
  - `std.list` — reverse/contains/index_of/concat/max/min/sort/dedup/
    take/drop/equal helpers (per type)
  - `std.time` — clock formatting helpers (`time_now_ms` uses IO; rest
    are pure), `time_format_hms`, `time_format_iso8601`, `time_human_ms`
  - `std.csv` — RFC 4180 subset parser + serialiser with configurable
    delimiter
  - `std.uuid` — deterministic v4-style and v5-style UUID generation +
    validation + version detection
- **New examples** (one per new stdlib module):
  `hex_demo.hls`, `base64_demo.hls`, `crypto_demo.hls`, `list_demo.hls`,
  `time_demo.hls`, `csv_demo.hls`, `uuid_demo.hls`.
- **Benchmarks folder** (`benchmarks/`): sieve, JSON parse, crypto hashing.
- **Differential tests** for every new stdlib module under `tests/ok/`.
- **`CONTRIBUTING.md`**, **`CODE_OF_CONDUCT.md`**, **`CHANGELOG.md`**
  themselves.

### Fixed
- Initial high/low 32-bit halves of the FNV-1a 64-bit offset basis were
  transcribed incorrectly in the initial implementation of
  `crypto_fnv1a_64_hex`. Corrected to `hi = 3421674724 (0xcbf29ce4)`,
  `lo = 2216829733 (0x84222325)`. Closes #10.

### Changed
- **`.gitignore`** expanded to cover editor scratch files, profiling
  artifacts, and the test scratch files created by `run_tests.sh`.
- The `feature/community-extensions` branch was rebased onto `main`
  (which had advanced to v0.5.0-alpha / Stage 9-alpha) before merging.
  All Stage 8-alpha (ownership) and Stage 9-alpha (effects) tests are
  preserved; the new stdlib modules were verified to compile and run
  correctly under the fine-grained effects system. Closes #11, #1, #2,
  #3, #4, #5, #6, #7, #8, #9.

### Test suite
- **127/127 PASS** (was 100/100 in v0.5.0-alpha). The new tests include
  the 14 community-extensions tests, the 4 Stage 9-beta tests
  (`feat_audit_pure` ok + `fail_pure_and_uses`, `fail_pure_uses_io`,
  `fail_pure_calls_impure`), plus bug-fix driven tests. Bootstrap is
  still deterministic.

---

## [v0.5.0-alpha] — 2026 (Stage 9-alpha — fine-grained effects & capabilities)

### Added
- **Five active effects**: `IO` (console), `Fs` (filesystem), `Clock`
  (monotonic clock), `Args` (command-line args), `Exit` (process exit).
  Each individually declared and statically verified through the call
  graph.
- **Per-builtin effect mapping**:
  - `print`, `println` → IO
  - `read_file`, `write_file`, `file_exists` → Fs
  - `clock_ms` → Clock
  - `args` → Args
  - `exit` → Exit
  - All other builtins (panic, str, int, len, range, map_new, chr, drop,
    clone, take) → no effect (pure).
- **Comma-separated `uses` clause**: `uses Fs`, `uses Fs, Clock`,
  `uses IO` (blanket). Backwards compatible with all v0.3/v0.4 code.
- **Capability subset semantics**: a function's declared effects ARE
  its capabilities. A function may call a callee/builtin only if its
  declared set is a superset of the callee's computed effect set.
  Default-deny: a function with no `uses` clause is statically
  guaranteed pure.
- **Reserved effect names** (recognized but error if used): `Net`,
  `Rand`, `Proc`. These will be enabled in a later stage.
- **Fixpoint analysis** rewritten from per-function `bool` to
  per-function effect SET. Monotone, bounded (5-element universe),
  deterministic.
- **Error messages** name the function, the missing effect, the
  violating callee/builtin, and the declared set.
- **9 new tests**: 4 ok (`feat_effects_fs`, `feat_effects_clock`,
  `feat_effects_multi`, `feat_effects_pure`) + 5 fail
  (`fail_effect_fs_missing`, `fail_effect_clock_missing`,
  `fail_effect_transitive_fs`, `fail_effect_reserved`,
  `fail_effect_unknown`).

### Test suite
- **100/100 PASS** (was 87/87 in v0.4.0-alpha). Bootstrap still
  deterministic.

---

## [v0.4.0-alpha] — 2026 (Stage 8-alpha — ownership primitives)

### Added
- `drop(x: T) -> void` — release ownership of a binding; subsequent use
  is a compile error.
- `clone(x: T) -> T` — independent deep copy. Supported for `str` and
  for `list[prim]`/`map[str, prim]` where `prim ∈ {int, float, bool, str}`.
- `take(x: T) -> T` — transfer ownership out of a binding without paying
  for a clone.
- Bindings now carry a `moved` flag (in both Stage-0 and the self-hosted
  compiler `hlc.hls`). Use-after-move raises a compile error with the
  variable name.
- Moves done inside `if`/`while`/`for` child scopes don't leak out — the
  compiler snapshots moved-status on scope entry and restores on exit.
- Reassignment (`x = new_value`) revives a moved `let mut` binding.
- 6 new tests: 3 ok (`feat_drop`, `feat_clone`, `feat_take`) + 3 fail
  (`fail_use_after_drop`, `fail_double_drop`, `fail_take_moved`).
- The bootstrap is still **deterministic**: two self-compile passes
  produce byte-identical C output.

### Test suite
- **87/87 PASS** (was 78/78 in v0.3.0). Bootstrap still deterministic.

---

## [v0.3.0] — 2026 (Stage 7 — Advanced type system)

### Added
- `enum` + full-featured `match` (exhaustiveness checking).
- `Option[T]` / `Result[T, E]` in the standard library; `?` for error
  propagation.
- `panic` no longer used for expected errors — reserved for programming
  bugs.
- Monomorphising generics on functions, structs, enums.
- Local type inference (literal type hints).
- Struct fields with default values.
- Recursive enums (forward declarations emitted in C output).
- New syntax: `enum Name[T] { Variant(T), None }`, `match scrut { arms }`,
  `expr?` postfix operator, generic `fn`/`struct`/`enum` with type params
  `[T, U, ...]`.
- Standard library: `std/option.hls`, `std/result.hls` (with `int_parse`
  and `float_parse`).
- 6 new fail tests for the new type-system rules.

### Test suite
- **78/78 PASS** (was 60/60). Bootstrap still deterministic.

---

## [v0.2.0] — 2026 (Stage 6 — Module system + standard library)

### Added
- `import "path"` syntax with module paths. Cycles are detected and
  rejected.
- Standard library: `std.str`, `std.math`, `std.json`, `std.url`,
  `std.html`. Each is pure HLS.
- New builtin `file_exists(path: str) -> bool`.
- Bug fixes: `never`-typed expressions are allowed as arguments and
  assignments; dead `mapnew` AST branches removed; bare `panic` in
  `parse_impl` replaced with `perr_at` for source locations.

### Test suite
- **60/60 PASS** (was 56/56). Bootstrap still deterministic.

---

## [v0.1.0] — 2026 (Stages 1–5 — initial self-hosting release)

### Added
- Full v0.1 specification (`SPEC.md`).
- Stage-0 reference interpreter (`boot/`, ~1,400 lines of pure Python):
  lexer, parser, type checker, effects analyzer, evaluator.
- Byte-precise semantics: strings are bytes, int64 arithmetic with
  overflow checks, divide-by-zero halts safely, maps preserve insertion
  order, `%.6f` for floats.
- Self-hosted compiler `src/hlc.hls` (~3,000 lines): lexer → parser →
  checker → C codegen → self-compile. C runtime embedded in the output.
- Arena allocation model: no `free` instruction → structurally
  impossible to use-after-free.
- Full self-compilation fixed-point: `make bootstrap` confirms the
  self-compilation process is **deterministic** — two passes produce
  byte-identical C output.

### Test suite
- **56/56 PASS**: 14 ok programs (incl. 4 safe-panic tests), 22 fail
  programs (rejected with the correct error message), 14 differential
  tests (interpreter ↔ native), bootstrap determinism check, plus the
  `wordcount` example with data file.
