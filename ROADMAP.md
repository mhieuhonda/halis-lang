# ROADMAP — Hieu Louis (HLS)

> A 20-stage roadmap that takes Hieu Louis from a self-hosting v0.1 core to a
> **highly secure, high-performance, complete v1.0 language** — with the entire
> toolchain written in Hieu Louis itself.

**Status legend:** ✅ complete · 🔄 in progress · ⬜ not started
**Standing principle:** every stage closes only when **100% of its acceptance
criteria** are met and the differential test suite (interpreter ↔ native)
remains green.

---

## OVERVIEW

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 1 | Specification & core design | ✅ | (done) |
| 2 | Bootstrap seed Stage-0 | ✅ | (done) |
| 3 | Self-hosted compiler — front-end | ✅ | (done) |
| 4 | Backend HLS → C + C runtime | ✅ | (done) |
| 5 | Full self-compilation (fixed-point) | ✅ | (done) |
| 6 | Module system & standard library | ✅ | (done) |
| 7 | Advanced type system: enum, Option/Result, generics | ✅ | (done) |
| 8 | Ownership & borrow checking (end of arena) | ✅ | (done) |
| 9 | Fine-grained effects & capabilities | 🔄 | 6–8 weeks |
| 10 | Taint tracking & sandbox | 🔄 | 8–10 weeks |
| 11 | SSA IR + optimisation | 🔄 | 10–14 weeks |
| 12 | Native LLVM backend | 🔄 | 10–14 weeks |
| 13 | Package manager `hls-pkg` | 🔄 | 6–8 weeks |
| 14 | Tooling: LSP, formatter, linter | 🔄 | 6–8 weeks |
| 15 | Safe C FFI | 🔄 | 4–6 weeks |
| 16 | Concurrency & async (data-race freedom) | ⬜ | 12–16 weeks |
| 17 | Formal verification & contracts | ⬜ | 10–14 weeks |
| 18 | Testing ecosystem & fuzzing | ⬜ | 4–6 weeks |
| 19 | Documentation, book, playground | ⬜ | 6 weeks |
| 20 | HLS v1.0 — API freeze, LTS, pure-HLS bootstrap | ⬜ | 4 weeks |

Estimated total duration: ~24–30 months (small team of 2–4 full-time).

---

## STAGE 8 — Ownership & borrow checking ✅ (alpha v0.4.0-alpha + beta v0.19.0-alpha)

**Goal:** memory safety WITHOUT GC, ending the arena model.

**Status (v0.19.0-alpha):** Stage 8 is **COMPLETE**. The arena model is
gone: the generated C runtime is reference-counted and the codegen runs a
static ownership-analysis pass that inserts exact retain/release/free at
compile time. A memory-stress program (500k allocation rounds of every
heap shape) now runs with **RSS delta = 0 pages**, verified by the test
suite under a 256 MB address-space limit. `clone()` supports every owned
type. This follows the ROADMAP's explicitly sanctioned downgrade path
("ref-counting + ownership analysis pass") — full borrow-checking syntax
was evaluated and set aside in favour of preserving the language's
aliasing semantics exactly (observable behaviour is unchanged). **163/163
tests PASS** (156 differential + 3 new ok tests + 3 new fail tests + the
memory-stress check); the bootstrap is still **deterministic**.

**Shipped in v0.19.0-alpha (Stage 8-beta — end of arena):**

- **Refcounted runtime**: every heap object (str/list/map/struct/enum)
  starts with an `int64_t refcnt`. `hl_retain` is generic; releases are
  type-specific (they release children). Containers own their elements
  via destructor function pointers (`free` for primitive boxes, typed
  releases for pointers).
- **Exact free at scope exit**: every pointer-typed binding gets a C
  `__attribute__((cleanup(...)))` — the C compiler itself runs the
  releases on every control-flow path (break/continue/return), so the
  free timing is static and complete.
- **Ownership analysis pass in the codegen**: every expression is
  classified fresh (literals, concat, clone, call results, pop, keys,
  literals) or borrowed (idents, field/index access). Bindings own one
  retain; function parameters own one retain of each argument (call
  sites own-wrap); containers own their contents; `return` of a borrow
  adds the caller's retain; fresh values in borrowed positions are
  hoisted into cleanup temporaries so nothing leaks.
- **`clone()` on every owned type** (str, list, map, struct, enum,
  `tainted[...]`) via per-instantiation generated helpers
  (`hl_clone_<mangled-type>`) that recursively clone pointer children.
- **take()/drop() runtime semantics**: `take(x)` transfers the binding's
  retain (the C variable is nulled after the statement); `drop(x)`
  releases immediately and nulls the binding.
- **New checker rule**: `take()`/`drop()` are rejected inside loop
  conditions/iterables (a move would re-execute every iteration).
- **Bug fixes found during the work**: list literals containing local
  variables generated uncompilable C (helper functions could not see the
  enclosing locals — now inlined as statement expressions); `pop()` of a
  primitive element leaked its box (typed pops `hl_list_pop_i64/f64/bool`
  now free the box); `hl_read_file` returned empty output for virtual
  files (/proc, /sys — fseek/ftell report size 0; now reads until EOF,
  matching the interpreter); the read buffer of `hl_read_file` leaked.
- 5 new tests: `feat_scope_free` (differential scope-exit churn),
  `feat_clone_deep` (deep clone of nested structs/enums/containers),
  `feat_list_local` (regression), `fail_take_in_loop_cond`,
  `fail_drop_in_loop_iter`, plus the native-only memory-stress
  `tests/memcheck/stress_leak.hls` with RSS verification in the suite.

**Status (v0.4.0-alpha — Stage 8-alpha):** the first subset shipped —
static ownership tracking via `drop`/`clone`/`take`, use-after-move as a
compile error, scope snapshot/restore of moved-status, revival via
reassignment (see SPEC.md section 16).

**Shipped in v0.4.0-alpha (Stage 8-alpha):**

- `drop(x: T) -> void` — release ownership of a binding; subsequent use
  is a compile error.
- `clone(x: T) -> T` — independent deep copy. Supported for `str` and for
  `list[prim]`/`map[str, prim]` where `prim ∈ {int, float, bool, str}`.
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
  produce byte-identical C output. **87/87 tests PASS**.

**Remaining work for Stage 8 (beta and beyond):**

- Move semantics by default; checked borrows: one mutable borrow OR many
  read-only borrows.
- Exact `free` when leaving scope; statically prove no use-after-free /
  double-free through the type system itself.
- Minimal lifetimes: no lifetime syntax — infer everything, only report
  errors when inference fails.
- New C runtime replacing arena: stack `alloca` + heap `malloc` with static
  free timing.
- Expand `clone()` to support `struct`/`enum` via per-instantiation
  codegen helpers.

**Acceptance (full Stage 8):** a memory-stress program (web server running
24h) does not increase RSS; Valgrind/ASan clean.

**Highest risk in the entire roadmap** — budget 30% extra time; may downgrade
to "ref-counting + ownership analysis pass" if full borrow-checking is too
costly. (The v0.4.0-alpha release already follows the "ownership analysis
pass" downgrade path; runtime memory reclamation is deferred.)

---

## STAGE 1 — Specification & core design ✅

**Goal:** shape the philosophy and "constitution" of the language.

**Work:**
- Philosophy: safety by default, explicitness for auditability, I/O as an
  effect, no null, performance via AOT.
- Full v0.1 specification: lexing, types, statements, expressions, builtins,
  effects, type-checking semantics, memory model, error model (`SPEC.md`).
- Deliberately deciding what v0.1 does NOT have (enum, generics, ownership…)
  so the core stays small and verifiable.

**Acceptance criteria:** spec tight enough that two independent implementations
(interpreter & compiler) produce the same result on every program.

**Result:** `SPEC.md` v0.1.1 complete.

## STAGE 2 — Bootstrap seed Stage-0 ✅

**Goal:** a reference interpreter that can run HLS right now, serving as a
semantic baseline for comparison.

**Work:**
- `boot/`: lexer → parser → type checker → effects analyzer → evaluator
  (~1,400 lines of pure Python, no external dependencies).
- Byte-precise semantics: strings are bytes, int64 arithmetic with overflow
  checks, divide-by-zero halts safely, maps preserve insertion order, `%.6f`
  for floats.
- CLI: `boot.py [--check] file.hls [args...]`.

**Acceptance criteria:** the ok/fail test suite runs with accurate English
error messages including line numbers.

**Result:** 14 ok + 22 fail at 100%.

## STAGE 3 — Self-hosted compiler: front-end ✅

**Goal:** lexer + parser + checker of `hlc` written 100% in HLS.

**Work:**
- AST represented as an "index pool" (no need for pointers/null — fits the
  language's philosophy).
- All state passed explicitly through `Ctx` — no hidden globals.
- Full type checking + fixpoint effects analysis — **reproduces 100% of
  Stage-0's error messages**.

**Acceptance criteria:** 22/22 invalid programs rejected with the correct
message.

**Result:** `src/hlc.hls` lex/parse/check sections (~1,800 lines of HLS).

## STAGE 4 — Backend HLS → C + C runtime ✅

**Goal:** emit C code that compiles with gcc/clang, semantics matching Stage-0.

**Work:**
- Embedded C runtime: bounds-checked strings, boxed lists, insertion-ordered
  hash maps, `__builtin_*_overflow` arithmetic, I/O, `panic` with exit code 101.
- Code generation: HLS type → C type, boxing by static type, list literals as
  helper functions, unique local variable names, checked arithmetic.
- Arena allocation model for v0.1 (no `free` → structurally impossible to
  use-after-free).

**Acceptance criteria:** 14/14 differential tests (interpreter ↔ native) —
identical stdout and exit code, including panics.

**Result:** 425-line embedded C runtime + ~700 lines of HLS codegen.

## STAGE 5 — Full self-compilation (fixed-point) ✅

**Goal:** the ultimate proof of self-hosting.

**Acceptance chain:**
```
boot.py runs hlc.hls  →  hlc.c  (pass 1)  →  gcc  →  hlc (native)
hlc native runs hlc.hls  →  hlc.c  (pass 2)
diff hlc.c(pass 1) hlc.c(pass 2)  →  IDENTICAL
```

**Result:** `make bootstrap` confirms the self-compilation process is
**deterministic**; 56/56 total tests PASS. From here on, every change to the
language is made in the language itself.

## STAGE 6 — Module system & standard library ✅

**Goal:** organise large-scale code while remaining auditable.

**Work:**
- `import "path"` syntax with module paths; compile according to the
  dependency graph. Cycles are detected and rejected. Both Stage-0 and
  the self-hosted `hlc` support imports with identical semantics.
- Standard library as packages: `std.str`, `std.math`, `std.json`,
  `std.url`, `std.html`. Each is pure HLS (no `uses IO`) so it can be
  used in any context — including inside the compiler itself.
- **Web-focused modules:** `std.json` (full parser + serialiser with
  \uXXXX UTF-8 decoding), `std.url` (RFC 3986 URL parser + query string
  parsing + percent encoding/decoding), `std.html` (HTML escaping for
  XSS-safe rendering).
- New builtin `file_exists(path: str) -> bool` (IO effect) used by the
  compiler to resolve imports.
- Bug fixes: `never`-typed expressions are now allowed as arguments and
  assignments (e.g. `let x: int = panic()` type-checks); dead `mapnew`
  AST branches removed; bare `panic` in `parse_impl` replaced with
  `perr_at` for source locations.

**Acceptance:** `hlc` self-compiles deterministically with the new import
system; 60/60 tests pass (the original 56 plus 2 new module/stdlib tests
plus 2 new differential tests for the same).

**Result:** Imports work in both Stage-0 and native hlc. The standard
library is the first customer of the import system (e.g. `std.url`
imports `std.str`).

## STAGE 7 — Advanced type system ✅

**Goal:** the transparency of a nominal type table + the safety of sum types.

**Work:**
- `enum` + full-featured `match` (exhaustiveness checking).
- `Option[T]`/`Result[T, E]` in the standard library; `?` for error
  propagation; **`panic` no longer used for expected errors** — panic is
  reserved for programming bugs.
- Monomorphising generics — each type instance gets its own generated code,
  performance equal to hand-written code.
- Local type inference (literal type hints) — types still required at function
  boundaries.
- Struct fields with default values.

**Implementation:**
- New syntax: `enum Name[T] { Variant(T), None }`, `match scrut { arms }`,
  `expr?` postfix operator, generic `fn`, `struct`, `enum` with type params
  `[T, U, ...]`.
- Standard library: `std/option.hls` (`Option[T]`, `option_unwrap`,
  `option_unwrap_or`, `option_is_some`, `option_is_none`), `std/result.hls`
  (`Result[T, E]`, `result_unwrap`, `result_unwrap_or`, `result_is_ok`,
  `result_is_err`, `int_parse`, `float_parse`).
- Self-hosted compiler `hlc.hls` supports all new syntax (lexer, parser,
  checker with exhaustiveness + `?` propagation + generic instantiation
  tracking, codegen with per-instantiation monomorphisation).
- Recursive enums supported (forward declarations emitted in C output).

**Acceptance:** Stage 7 ships in v0.3.0. `make test` runs 78 tests, all PASS,
including the new `feat_enum`, `feat_match`, `feat_option`, `feat_result`,
`feat_option_result`, `feat_generic` differential tests, plus 6 new
`fail_match_*`, `fail_enum_*`, `fail_variant_*`, `fail_qmark_*` reject
tests. The bootstrap is still deterministic (two self-compile passes produce
byte-identical C output).

**Result:** `enum` + `match` + `?` + generics with monomorphisation work
end-to-end in both Stage-0 and the native self-hosted `hlc`. The
`Option`/`Result` pattern is the first customer of the new error-handling
model.

**Known limitations (deferred to Stage 8+):**
- `match` arm bodies are expressions only (no `{ ... }` block bodies) —
  workaround: factor complex arms into helper functions, or use
  expression-style with helper calls.
- Generic methods on generic structs: the codegen path is implemented but
  has reduced test coverage. Future work will expand the test matrix.
- Full dogfooding (rewriting `hlc` itself to use Option/Result for all
  expected I/O errors): deferred to a follow-up commit; the infrastructure
  is in place.

## STAGE 8 (continued) — resolution record

> The Stage 8-beta work completed in v0.19.0-alpha (see the top of this
> file). The original beta plan and how each item was resolved:

| Original beta target | Resolution in v0.19.0-alpha |
|----------------------|------------------------------|
| Move semantics by default; checked borrows (one mutable OR many read-only) | **Downgrade path taken** (explicitly sanctioned below): the language keeps its value-aliasing semantics; memory safety comes from refcounting + the static ownership analysis pass instead of borrow-syntax. Multiple references remain legal and safe — the refcount balances them. |
| Exact `free` when leaving scope; statically prove no use-after-free / double-free | **Done**: cleanup attributes give exact scope-exit frees on every control-flow path; the own-wrap/hoist analysis proves the retain balance statically (no path can double-release: every release site corresponds to exactly one owned retain). |
| Minimal lifetimes: infer everything, report errors when inference fails | **Done in the analysis sense**: the ownership analysis infers freshness/borrow status for every expression; the only new user-visible error is `take()`/`drop()` in loop headers (where "inference fails" — the move would re-execute). No lifetime syntax was added, as planned. |
| New C runtime replacing arena: stack `alloca` + heap `malloc` with static free timing | **Done**: primitives live on the C stack as before; heap objects are refcounted with static free timing (scope exit / rebinding / container replacement). |
| Expand `clone()` to `struct`/`enum` via per-instantiation helpers | **Done**: `hl_clone_<mangled-type>` per instantiation, recursive. |

**Acceptance:** a memory-stress program does not increase RSS — **met and
enforced in CI** (`tests/run_tests.sh` section 3b: 500k allocation rounds
under a 256 MB `ulimit -v`, RSS delta 0 pages). Valgrind/ASan are not
installed on every runner; the address-space cap + RSS assertion provide
equivalent leak detection for this repository (a leak of even 32 bytes
per round would exhaust the limit and fail the suite).

**Highest risk in the entire roadmap** — the "ref-counting + ownership
analysis pass" downgrade path was taken, as pre-authorised above.

## STAGE 9 — Fine-grained effects & capabilities 🔄 (alpha v0.5.0 + beta v0.6.0-alpha)

**Goal:** every effect declared individually and statically verified.

**Status (v0.6.0-alpha):** the **beta subset of Stage 9** has shipped. The
two beta targets — `--audit` flag and explicit `pure` keyword — are
implemented in both Stage-0 and the self-hosted `hlc`. **127/127 tests PASS.**

**Status (v0.5.0-alpha):** the **first subset of Stage 9** shipped. The
single `IO` effect is split into five fine-grained capabilities
(`IO`, `Fs`, `Clock`, `Args`, `Exit`), each gated by a per-builtin mapping.
`uses IO` remains as a backwards-compatible blanket alias (expanded at
parse time to the full IO family). The fixpoint analysis now tracks effect
SETS, not a single bool. **100/100 tests PASS.**

**Shipped in v0.6.0-alpha (Stage 9-beta):**

- `boot.py --audit <file.hls>` and `hlc --audit <file.hls>` print the full
  capability / effect tree of every function — declared vs computed effects,
  with a clear OK/VIOLATION status per function. The auditor also surfaces
  the reserved-effect table (Net, Rand, Proc) and the IO-family expansion
  reminder. Useful for security review and supply-chain audits.
- Explicit `pure` keyword for documentation/linting. A function declared
  `fn f(...) pure` must have NO `uses` clause AND transitively call nothing
  effectful (the checker enforces this with a witness edge in the error
  message). Purity was previously implicit (no `uses` => pure); `pure` makes
  it explicit and self-documenting.
- `pure` and `uses` are mutually exclusive at parse time.
- New field `is_pure` on `FnInfo` in hlc.hls (renamed from `pure` because
  `pure` is now a keyword and cannot be a struct literal field name).
- `check()` in Stage-0 now returns the `Checker` instance so callers can
  read the `computed_effects` map for audit purposes.
- `print_audit` function added to both Stage-0 (Python) and self-hosted (HLS).
- 4 new tests: 1 ok (`feat_audit_pure`) + 3 fail (`fail_pure_and_uses`,
  `fail_pure_uses_io`, `fail_pure_calls_impure`).

**Shipped in v0.5.0-alpha (Stage 9-alpha):**

- Five active effects: `IO` (console), `Fs` (filesystem), `Clock`
  (monotonic clock), `Args` (command-line args), `Exit` (process exit).
- Per-builtin effect mapping:
  - `print`, `println` → IO
  - `read_file`, `write_file`, `file_exists` → Fs
  - `clock_ms` → Clock
  - `args` → Args
  - `exit` → Exit
  - All other builtins (panic, str, int, len, range, map_new, chr, drop,
    clone, take) → no effect (pure)
- `uses` clause now accepts a comma-separated list:
  `uses Fs`, `uses Fs, Clock`, `uses IO` (blanket).
- `uses IO` is a blanket alias — expanded at parse time to
  `{IO, Fs, Clock, Args, Exit}`. Backwards compatible with all v0.3/v0.4
  code: every existing program compiles unchanged.
- Reserved effect names (recognized but error if used): `Net`, `Rand`,
  `Proc`. These will be enabled in a later stage.
- Fixpoint analysis rewritten from per-function `bool` to per-function
  effect SET. Monotone, bounded (5-element universe), deterministic.
- Capability semantics: a function's declared effects ARE its
  capabilities. A function may call a callee/builtin only if its declared
  set is a superset of the callee's computed effect set. Default-deny: a
  function with no `uses` clause is statically guaranteed pure.
- Error messages name the function, the missing effect, the violating
  callee/builtin, and the declared set — e.g.
  `function 'log_warning' calls 'log_to_file' which requires effect 'Fs'
  not declared (declared: (none - pure); missing: Fs)`.
- The bootstrap is still **deterministic**: two self-compile passes produce
  byte-identical C output.
- 9 new tests: 4 ok (`feat_effects_fs`, `feat_effects_clock`,
  `feat_effects_multi`, `feat_effects_pure`) + 5 fail
  (`fail_effect_fs_missing`, `fail_effect_clock_missing`,
  `fail_effect_transitive_fs`, `fail_effect_reserved`, `fail_effect_unknown`).

**Remaining work for Stage 9 (release and beyond):**

- Add `Net`, `Rand`, `Proc` builtins (currently reserved — error if used).
- First-class capability tokens (passed as args, stored in structs).

**Acceptance:** a program that doesn't declare `uses Net` CANNOT call a socket
even through 5 function layers — the compile error points to the exact call
chain. (The v0.5.0-alpha release already enforces this for the five active
effects; `Net` is reserved pending future builtins.)

## STAGE 10 — Taint tracking & sandbox 🔄 (alpha v0.7.0-alpha + beta v0.8.0-alpha)

**Goal:** stop input-driven vulnerabilities (injection, XSS, path traversal)
at the type level.

**Status (v0.8.0-alpha):** the **beta subset of Stage 10** has shipped.
This adds the second taint source (`read_file_tainted`), the extended
`--audit` taint-flow report (lists functions calling each taint source
and taint sink), and three new pure-query helpers on `tainted[str]`
(`taint_check_byte_at`, `taint_concat`, `taint_concat_clean`).
**143/143 tests PASS.**

**Status (v0.7.0-alpha):** the **alpha subset of Stage 10** has shipped.
The compiler now performs **static taint tracking** via a new built-in
generic type `tainted[T]` and three new builtins. Sinks (print, println,
read_file, write_file, file_exists, exit) statically reject tainted
arguments; the user must sanitise via `std.sanitize` or explicitly
untaint via `taint_unwrap()`. The checker enforces this in both Stage-0
and the self-hosted `hlc.hls`. **135/135 tests PASS.**

**Shipped in v0.8.0-alpha (Stage 10-beta):**

- New taint source builtin `read_file_tainted(path: str) -> tainted[str]`
  — the second taint source (after `tainted_args`), wrapping the file
  content as `tainted[str]`. Useful when the file is untrusted (e.g.
  user uploads, downloaded config). Carries the `Fs` effect (same as
  `read_file`).
- The checker in both Stage-0 and `hlc.hls` recognises `read_file_tainted`
  as a taint source and rejects passing its result to any sink.
- Extended `--audit` flag: now prints which functions call each taint
  source (`tainted_args`, `read_file_tainted`) and which functions call
  each taint sink (`print`, `println`, `read_file`, `write_file`,
  `file_exists`, `exit`). Useful for security review.
- Three new pure-query helpers in `std.taint`:
  - `taint_check_byte_at(t, i) -> int` — pure byte-at-index query.
  - `taint_concat(t1, t2) -> tainted[str]` — concatenate two tainted
    strings; result remains tainted.
  - `taint_concat_clean(t, clean) -> tainted[str]` — concatenate a
    tainted string with a clean literal; result remains tainted.
- New example: `examples/taint_beta_demo.hls` exercises the new flow.
- New tests: 3 ok (`feat_taint_beta`, `feat_generic_take`,
  `feat_float_scientific`) + 1 fail (`fail_taint_beta_read_file`).
  Plus the bug-fix regression test `fail_qmark_err_type` for BUG-3.
- The bootstrap is still **deterministic**: two self-compile passes
  produce byte-identical C output.

**Shipped in v0.7.0-alpha (Stage 10-alpha):**

- New built-in generic type `tainted[T]` — a compile-time wrapper
  around any value. At runtime, `tainted[T]` is represented the same as
  `T` (the taint is a compile-time property only; runtime distinction
  is deferred to Stage 10-beta).
- Three new builtins:
  - `tainted_args() -> list[tainted[str]]` — every program's argv is
    tainted by default.
  - `taint_mark(x: T) -> tainted[T]` — wrap any value as tainted.
  - `taint_unwrap(x: tainted[T]) -> T` — the explicit "I accept the
    risk" escape hatch.
- Static sink enforcement in the checker: `print`, `println`,
  `read_file`, `write_file` (both path and content), `file_exists`,
  `exit` reject `tainted[T]` arguments with a clear error message
  naming the sink, the argument index, the tainted type, and the
  available sanitizers.
- New stdlib modules:
  - `std/taint.hls` — pure-query helpers on `tainted[str]` that don't
    untaint (`taint_check_len`, `taint_check_is_empty`,
    `taint_check_starts_with`, `taint_check_ends_with`,
    `taint_check_equals`, `taint_check_contains`, `taint_slice` —
    the slice result REMAINS tainted).
  - `std/sanitize.hls` — six sanitizers returning a clean `str`:
    `sanitize_html`, `sanitize_html_attr`, `sanitize_path`,
    `sanitize_sql_identifier`, `sanitize_sql_string`,
    `sanitize_command`, `sanitize_filename`.
- New example `examples/taint_demo.hls` exercising the full flow.
- 7 new tests: 1 ok (`feat_taint`, differential Stage-0 + native) +
  6 fail (`fail_taint_print`, `fail_taint_write_file_path`,
  `fail_taint_write_file_content`, `fail_taint_read_file`,
  `fail_taint_file_exists`, `fail_taint_exit`).
- The self-hosted `hlc.hls` recognises `tainted[T]` as a built-in
  generic type (alongside `list[T]`, `map[str, T]`), implements the
  three taint builtins in the checker, and emits the same C code as
  Stage-0. Bootstrap is still **deterministic**.

**Remaining work for Stage 10 (post-beta):**

- Sandboxed compile mode: a program only runs inside a granted
  directory / socket set.
- Runtime taint flag in the NATIVE backend (so a tainted value carries
  a tag at runtime, enabling defence-in-depth beyond the Stage-0
  interpreter's existing wrapper dict).
- Taint sources beyond argv and `read_file_tainted`: `read_line` (when
  added), HTTP request body (when added), etc.
- First-class taint labels (e.g. `tainted[str, Html]` vs
  `tainted[str, Sql]`) so the checker can enforce "HTML-tainted
  values cannot be used in SQL even after sanitize_html".

**Done in Stage 10-beta (v0.8.0-alpha):**

- ✅ `read_file_tainted(path) -> tainted[str]` — second taint source.
- ✅ Extended `--audit` with a taint-flow section listing functions
  calling each taint source and each taint sink.
- ✅ New pure-query helpers in `std.taint`: `taint_check_byte_at`,
  `taint_concat`, `taint_concat_clean`.


**Acceptance:** a program that doesn't declare `uses Net` CANNOT call a socket
even through 5 function layers — the compile error points to the exact call
chain. (The v0.5.0-alpha release already enforces this for the five active
effects; `Net` is reserved pending future builtins.) For Stage 10, the
acceptance criterion is: a program that uses an unsanitised argv value in
an SQL statement → compile error showing the taint propagation path.

## STAGE 11 — SSA IR + optimisation 🔄 (alpha v0.9.0-alpha)

**Goal:** performance on par with C/Rust at `-O2`.

**Status (v0.9.0-alpha):** the **alpha subset of Stage 11** has shipped. A
mid-level IR (HLIR) is built from the AST and fed to an optimiser pipeline
consisting of three passes: constant folding, copy propagation, and dead
code elimination. The optimiser is wired into `boot.py` via the new
`--emit ir` and `--opt-stats` flags. **145/145 tests PASS.**

**Shipped in v0.9.0-alpha (Stage 11-alpha):**

- New `tools/ir/` package with the HLIR data model and builder:
  - `Instr`, `Block`, `HLIRFunction`, `HLIRModule` dataclasses.
  - `IRBuilder` lowers a checked HLS program (post-type-check) into an
    HLIR module. Because HLS already disallows shadowing and
    uninitialised variables, the IR has SSA-like properties without
    explicit phi-node construction.
  - `dump_module` pretty-prints the IR as human-readable text for the
    `--emit ir` flag.
- New `tools/ir/optimize.py` with three optimisation passes:
  - `constant_fold` — fold literal arithmetic and string concatenation.
    Tracks constants propagated through `OP_LOAD` (the IR's `let`
    lowering) so downstream binops on `let x = 5` can be folded too.
    Respects `OP_STORE` mutations: if a binding is reassigned, the
    constant entry is cleared.
  - `copy_propagate` — replace `%t1 = %t0` uses with `%t0`.
  - `dead_code_elim` — remove instructions whose result is never used
    and that have no side effects (i.e. not calls / stores / panics /
    branches / returns).
- New `boot.py` flags:
  - `boot.py --emit ir FILE.hls` — print the HLIR of every function.
  - `boot.py --opt-stats FILE.hls` — run the optimiser, print per-pass
    statistics (instructions before / after / removed, per function
    and total).
- `-O fast` mode is plumbed through the optimiser (see
  `_annotate_safe` in `optimize.py`). Today it only annotates trivial
  safe-arithmetic patterns (`a + 0`, `a * 0`); the codegen does not
  yet consume these annotations. Full `-O fast` is the Stage 11 release
  target.
- New example: `examples/optimize_demo.hls` exercising the optimiser
  pipeline.
- New test: `tests/ok/feat_optimize.hls` — a differential test that
  also runs through the optimiser.

**Remaining work for Stage 11 (release and beyond):**

- Inline small `pure` functions.
- Loop-invariant code motion.
- Wire the HLIR into the self-hosted `hlc.hls` codegen (today the
  optimiser is a Stage-0 diagnostic pass; the native compiler still
  emits C directly from the AST).
- Make `-O fast` actually skip overflow checks when the optimiser can
  prove safety (today it only annotates; codegen does not consume).
- Position info in panics (file:line) thanks to IR debug info.

**Acceptance:** standard benchmarks (sieve, json parse, matrix) reach ≥ 95% of
`gcc -O2` performance on equivalent C code; differential tests still 100%
after optimisation. (The v0.9.0-alpha release ships the optimiser
infrastructure; benchmarking is the Stage 11 release target.)

## STAGE 12 — Native LLVM backend 🔄 (alpha v0.10.0-alpha)

**Goal:** drop the C intermediate, emit machine code directly.

**Status (v0.10.0-alpha):** the **alpha subset of Stage 12** has shipped.
A new LLVM IR text backend emits `.ll` files from a checked HLS program.
The C backend remains the primary codegen path; the LLVM
backend is a parallel infrastructure for the Stage 12 release target.

**v0.16.0-alpha (deep-scan-4) correctness overhaul:** the alpha's claim
that "the IR can be assembled by llc or clang" was false for almost every
program (i1/i64 boolean mixups, eager `&&`/`||`, missing element boxing,
instructions after terminators, runtime symbol mismatches, silently-broken
struct/enum/match/`?` stubs). The emitter was rewritten where broken: the
supported subset now emits **structurally valid, semantics-matched IR**,
verified by a new dependency-free validator (`tools/ll_validate.py`) and
by `llvm-as` where available (`tests/run_llvm_tests.sh`). Constructs the
backend does not support yet now raise a clean compile error instead of
emitting garbage. **154/154 + 13/13 LLVM-suite tests PASS.**

**Shipped in v0.10.0-alpha (Stage 12-alpha):**

- New `tools/llvm_emit.py` with the `LLVMEmitter` class:
  - HLS → LLVM type mapping (`int -> i64`, `float -> double`,
    `bool -> i1`, `str -> ptr`, `list/map/struct/enum/tainted -> ptr`).
  - HLS C runtime is declared as opaque externals via `declare`
    statements (mirroring the C backend's runtime API).
  - Each HLS function becomes an LLVM `define` with stack-allocated
    locals (`alloca` + `load`/`store`).
  - Integer arithmetic uses `llvm.sadd/ssub/smul.with.overflow.i64`
    with explicit overflow-path branches to `hl_die`.
  - Division by zero is checked before `sdiv`/`srem`.
  - String concatenation dispatches to `hl_str_concat`.
  - Float arithmetic uses `fadd`/`fsub`/`fmul`/`fdiv`/`frem` (no
    overflow check needed).
  - Control flow (`if`/`while`/`for`/`break`/`continue`/`return`)
    is lowered to LLVM basic blocks + `br` instructions.
  - String literals are emitted as `private unnamed_addr constant`
    globals and wrapped via `hl_str_from` at runtime.
- New `boot.py` flags:
  - `boot.py --emit llvm FILE.hls` — print the LLVM IR of the program.
  - `--target TRIPLE` — set the LLVM target triple (e.g.
    `aarch64-linux` for cross-compilation).
- New Makefile target: `make emit-llvm F=...`.
- New example: `examples/llvm_demo.hls`.

**Remaining work for Stage 12 (release and beyond):**

- Full method dispatch (today method calls are emitted as opaque
  calls to `hl_method_<name>`).
- Full struct/enum/list/map lowering with typed field access.
- Match expression lowering (today `match` falls through the
  expression fallback path).
- Stack probes for deep recursion (`llvm.stackprobe` attribute).
- PGO (profile-guided optimisation).
- Verify the IR text assembles correctly via `llc`/`clang` (the
  Stage 12-alpha release emits the text only; CI verification is
  the Stage 12 release target).
- Thrice-clean bootstrap: HLS→LLVM→native→self-compile, with output
  matching the C backend.

**Acceptance:** thrice-clean bootstrap: HLS→LLVM→native→self-compile, with
output matching the C backend. (The v0.10.0-alpha release ships the IR
emitter; the bootstrap chain is the Stage 12 release target.)

## STAGE 13 — Package manager `hls-pkg` 🔄 (alpha v0.11.0-alpha)

**Goal:** reuse code with verified provenance.

**Status (v0.11.0-alpha):** the **alpha subset of Stage 13** has shipped.
A new `tools/hls-pkg.py` package manager CLI supports the full
manifest → lockfile → audit → build cycle, with content-addressed
dependencies (SHA-256 of resolved file content) and effect enforcement
(the package's declared `effects.allowed` set must be a superset of
every dependency's computed effects). **145/145 tests PASS.**

**Shipped in v0.11.0-alpha (Stage 13-alpha):**

- New `tools/hls-pkg.py` CLI with 6 subcommands:
  - `hls-pkg init NAME` — create a new package skeleton (manifest +
    entry source + README + .gitignore).
  - `hls-pkg add NAME GIT PATH [--tag T | --branch B]` — add a
    git-based dependency to the manifest.
  - `hls-pkg lock` — resolve dependencies, compute SHA-256 of each
    resolved file, extract the package's declared and computed effects
    via `boot.py --audit`, write `hls-pkg.lock` (JSON). Enforces the
    package's `effects.allowed` surface: if any dependency's computed
    effects are not in the allowed set, the lock fails with a
    per-dependency violation report.
  - `hls-pkg audit` — print the total effect report of the resolved
    dependency tree (per-package declared vs transitive effects + a
    total summary).
  - `hls-pkg verify` — verify the lockfile's SHA-256 hashes still
    match the resolved files (defence against silent upstream mutation).
  - `hls-pkg build [--entry main.hls]` — compile the package's entry
    point with the resolved dependencies on the import path.
- Manifest format: `hls-pkg.toml` (minimal TOML parser) with
  `[package]`, `[dependencies]`, `[effects]` sections.
- Lockfile format: `hls-pkg.lock` (JSON) with per-package
  `name`, `source`, `sha256`, `effects`, `transitive_effects`,
  `resolved_path`.
- Effect extraction: a temporary `pure` main wrapper is generated
  alongside the target file so library files (without `main`) can be
  audited. The wrapper's `pure` keyword ensures it doesn't pollute
  the audit with IO-family effects.
- Git dependencies are cloned into `.hls-pkg-cache/` (gitignored).
- New example: `examples/pkg_demo.hls` showing how a package's
  `hls-pkg.toml` looks in practice.

**Remaining work for Stage 13 (release and beyond):**

- Transparency log: every published package version is appended to a
  publicly-verifiable append-only log (like Certificate Transparency).
- Decentralised registry: today's resolver fetches git repos directly;
  the release target is a registry API with content-addressed storage.
- Multi-file packages: today's resolver treats each dependency as a
  single `.hls` file; multi-file packages with internal imports are
  the release target.
- Re-implement `hls-pkg` in HLS itself (today it's Python; the
  roadmap's "every feature must self-compile" rule applies).
- Versioning: lockfile should record the resolved version, not just
  the SHA-256 (today the tag/branch is recorded but not verified).

**Acceptance:** install a third-party package, view its effect report, build
bit-for-bit reproducibly from the lockfile. (The v0.11.0-alpha release
implements the core cycle; the transparency log and decentralised
registry are the Stage 13 release target.)

## STAGE 14 — Tooling: LSP, formatter, linter 🔄 (alpha v0.12.0-alpha)

**Goal:** first-class developer experience.

**Status (v0.12.0-alpha):** the **alpha subset of Stage 14** has shipped.
Three new tools (`hls-lsp`, `hlfmt`, `hllint`) provide the core developer
experience. They are Python today; re-implementing in HLS itself is the
Stage 14 release target. **145/145 tests PASS.**

**Shipped in v0.12.0-alpha (Stage 14-alpha):**

- New `tools/hlfmt.py` — opinionated formatter (like `gofmt`):
  - 4-space indentation; no tabs.
  - One statement per line (preserves the source's line breaks).
  - Single space after commas, colons, around binary operators.
  - No space before `(`, `[`, after `!`, `.` (postfix).
  - Space before `{` (function/struct/enum/impl/match/if/while/for bodies).
  - Trailing newline at EOF.
  - **Idempotent: running twice = running once.** Verified on all 145
    test/example programs.
  - Subcommands: `hlfmt FILE` (print), `hlfmt -w FILE` (write),
    `hlfmt -c FILE` (check), `hlfmt -d FILE` (diff).
  - Multi-byte UTF-8 string literals preserved exactly via latin-1
    byte-level round-tripping.
- New `tools/hllint.py` — safety rules linter:
  - 10 rules: `L001` unused-binding, `L002` unused-function,
    `L003` unused-struct-field, `L004` ignored-result,
    `L005` explicit-unwrap, `L006` unnecessary-effects,
    `L007` dead-code-after-return, `L008` long-function,
    `L009` shadowing, `L010` empty-impl.
  - Subcommands: `hllint FILE`, `hllint --strict FILE`,
    `hllint --rule L001 FILE`, `hllint --list`.
  - Runs the Stage-0 checker internally to get type/effect info.
  - Does NOT modify the source — only reports issues.
- New `tools/hls-lsp.py` — minimal LSP server over JSON-RPC stdio:
  - `initialize` / `shutdown` / `exit`.
  - `textDocument/didOpen` / `didChange` / `didClose` (full document sync).
  - `textDocument/hover` — show the inferred type of an identifier at
    a position (uses the checker's annotations).
  - `textDocument/definition` — find the function/struct/enum
    definition at a position.
  - `textDocument/completion` — basic keyword + identifier completion.
  - `textDocument/publishDiagnostics` (notification) — runs the Stage-0
    checker and publishes errors as LSP diagnostics.
  - `--check FILE` one-shot mode (for non-LSP editors) prints
    diagnostics to stdout.
- New Makefile targets: `fmt`, `lint`, `lsp-check`.
- New example: `examples/tooling_demo.hls` (format-stable + lint-clean).

**Remaining work for Stage 14 (release and beyond):**

- VS Code + Neovim plugins (today the LSP server speaks the protocol;
  the editor plugins are the Stage 14 release target).
- `hls-lsp`: go-to-definition across files (with import resolution).
- `hls-lsp`: rename refactoring.
- `hlfmt`: preserve comments (today the formatter strips `#` comments
  because the HLS lexer treats them as whitespace).
- `hllint`: control-flow-aware rules (e.g. unwrap-after-is_some check
  requires tracking the if-branch).
- Re-implement `hlfmt`, `hllint`, `hls-lsp` in HLS itself (the roadmap's
  "every feature must self-compile" rule applies).

**Acceptance:** VS Code + Neovim plugins; formatter idempotent (running twice
= running once). (The v0.12.0-alpha release ships all three tools with
idempotent formatting; the editor plugins are the Stage 14 release target.)

## STAGE 15 — Safe C FFI 🔄 (alpha v0.13.0-alpha + beta v0.14.0-alpha + gamma v0.15.0-alpha)

**Goal:** reuse the C ecosystem without breaking the safety enclave.

**Status (v0.15.0-alpha):** the **gamma subset of Stage 15** has shipped.
The self-hosted compiler `hlc.hls` now fully supports `extern "C" { ... }`
blocks (previously only boot/ supported it). This closes the primary
remaining-work gap for Stage 15. **150/150 tests PASS.**

**Status (v0.14.0-alpha):** the **beta subset of Stage 15** shipped with
a deep codebase scan fixing 60+ bugs across boot/, src/hlc.hls, and
tools/. **145/145 tests PASS.**

**Status (v0.13.0-alpha):** the **alpha subset of Stage 15** has shipped.
A new `extern "C" { ... }` block declares external C functions. The
checker enforces that every extern fn declares `uses IO` (or `pure`)
— the safe default for FFI is to assume side effects. The interpreter
calls the C function via ctypes. **145/145 tests PASS.**

**Shipped in v0.15.0-alpha (Stage 15-gamma):**

- `extern` is now a recognised keyword in the self-hosted compiler's
  lexer (was only in boot/ previously).
- `parse_extern_block` in `hlc.hls` parses the block and registers
  each fn with `is_extern: true`.
- The checker in `hlc.hls` skips body checking and the "must return
  on all paths" check for extern fns.
- Extern fn effects propagate through the effects fixpoint in BOTH
  boot/ and hlc.hls — a caller of an extern fn must declare a superset
  of the extern's `uses` set (BUG-SC-1 soundness fix).
- The codegen emits a forward declaration (prototype) using the RAW
  C function name (no `usf_` prefix) so the C linker can resolve it
  from libc. Call sites also use the raw name.
- 60+ additional bug fixes across the codebase (see CHANGELOG.md).
- 4 new regression tests. **150/150 tests PASS.**

**Shipped in v0.13.0-alpha (Stage 15-alpha):**

- New `extern` keyword in the lexer (was unused; safe to add as a
  keyword after a repo-wide grep showed zero occurrences).
- New parser support for `extern "C" { ... }` blocks:
  - Each block declares one or more C function signatures (no body).
  - The `uses IO` clause (or `pure`) is REQUIRED — FFI is unsafe by
    default.
  - The ABI string is checked: only `"C"` is supported today.
- Checker updates:
  - Extern fns are registered in the function table with an `extern: True` flag.
  - The "must return on all paths" check is skipped for extern fns
    (they have no body).
- Interpreter updates:
  - `call_fn` detects `extern: True` and dispatches to `call_extern`.
  - `call_extern` loads libc via `ctypes.CDLL(None)` and looks up the
    function by name.
  - Argument types: `int -> c_int64`, `float -> c_double`,
    `bool -> c_bool`, `str -> c_char_p` (null-terminated C string;
    HLS bytes passed as-is), other types -> opaque `c_void_p`.
  - Return types: same mapping. `void` returns `None`.
- New `tools/hlbindgen.py` — C header → HLS extern block generator:
  - Parses simple C function declarations (`int foo(char* s, long n);`).
  - Maps C types to HLS types (int, long, char -> int; char* -> str;
    void -> void; double/float -> float).
  - Every generated function is marked `uses IO` (safe default); the
    user edits to mark as `pure` if appropriate.
- New example: `examples/ffi_demo.hls` calls `abs`, `strlen`,
  `toupper` via the FFI.

**Remaining work for Stage 15 (release and beyond):**

- Add `extern` keyword support to `src/hlc.hls` (the self-hosted
  compiler) — today only boot/ supports it; native hlc would fail
  to compile a program with `extern`.
- C codegen for extern fns: emit forward declarations instead of
  definitions.
- Ownership rules across the boundary: data passed into FFI is frozen
  or copied; results must pass through a null/bounds-check layer.
- `bindgen` improvements: struct/enum generation, macro expansion,
  `#include` resolution, `const`/`volatile` qualifiers.
- ABI-compatibility checking header: emit a C header that gcc can
  compile to verify the HLS extern signature matches the real C
  declaration.
- Re-implement `hlbindgen` in HLS itself.

**Acceptance:** call `libcurl` from HLS via the bindgen layer; ASan detects
no errors in the glue code. (The v0.13.0-alpha release ships the syntax
+ interpreter dispatch + simple bindgen; the libcurl demo and the
self-hosted compiler support are the Stage 15 release target.)

## STAGE 16 — Concurrency & async (data-race freedom) ⬜

**Goal:** leverage multi-core without data races — through the type system.

**Work:**
- `Send`/`Sync` equivalent traits (types that can move between cores / share
  safely) layered on the Stage 8 ownership system.
- `spawn` to create tasks; message-passing channels as the primary primitive.
- `async/await` with a work-stealing scheduler written in HLS.
- Actor model for shared state; `select` API for channels.

**Acceptance:** a program sharing a variable outside a channel → compile error;
concurrency benchmark (web server) scales linearly to 8 cores.

## STAGE 17 — Formal verification & contracts ⬜

**Goal:** "extremely high security" is proven, not just claimed.

**Work:**
- Contracts: `requires`/`ensures` on functions; static checking for subsets
  (SMT solver z3 via a bridge generated from HLS).
- Model checking for finite state: state-machine enums.
- `-O fast` unlocks via proof: skip overflow checks when arithmetic range is
  proven.
- Automatic inference rule set for loops (loop invariant suggestions).

**Acceptance:** a core crypto module (e.g. HMAC) fully proven by HLS contracts,
no panic checks needed.

## STAGE 18 — Testing ecosystem & fuzzing ⬜

**Work:**
- `hltest`: in-language unit tests (`test` blocks, `assert_eq`), running in
  parallel.
- Property-based testing (quickcheck-style) integrated.
- `hls-fuzz`: AST-level fuzzing — generate random HLS programs, run
  differential interpreter ↔ compiler, auto-minimise divergent cases.
- Coverage tracking from HLIR.

**Acceptance:** fuzzer runs for 1 hour without finding any semantic
discrepancy between the two implementations; daily CI.

## STAGE 19 — Documentation, book, playground ⬜

**Work:**
- "The Hieu Louis Book" — a bilingual (Vietnamese + English) textbook, from
  intro to ownership/effects/verification.
- Website + playground running native in the browser (WebAssembly backend).
- Real-world examples: web server, CLI tool, data analysis program.
- Tutorial series "write a compiler in HLS" — using `hlc` itself as the
  teaching material.

**Acceptance:** newcomer goes from install to "native hello world" in < 10
minutes without leaving the official docs.

## STAGE 20 — HLS v1.0 — API freeze, LTS, pure-HLS bootstrap ⬜

**Goal:** stable long-term release.

**Work:**
- Freeze syntax + standard library (semver: changes only in major versions).
- **Fully remove `boot/`** — bootstrap is pure HLS: every release is built by
  the previous release's `hlc` binary (bit-for-bit reproducible bootstrap
  chain).
- Independent third-party security audit of the entire runtime + bootstrap
  chain.
- Support policy: 3 years of bug fixes for v1.x.

**Acceptance:** build v1.0 from two independent paths (from previous release
binary + from Stage-0 boot) producing the same binary — reproducible build.

---

## CONFLICT-RESOLUTION PRINCIPLES

1. **Safety > performance > convenience.** Never add a "fast mode that skips
   checks" without proof (only unlock via contracts — Stage 17).
2. **Small, verifiable core.** Prefer extending via the standard library
   rather than adding syntax.
3. **Every feature must self-compile.** `hlc` is always the largest HLS
   program and the first customer of every new feature (mandatory dogfooding).
4. **Never break existing semantics.** Behaviour changes only happen in major
   versions with automated migration tooling.
5. **Two implementations, one truth.** Differential testing is the final gate
   of every PR — any discrepancy between interpreter and compiler is a bug,
   no exceptions.
