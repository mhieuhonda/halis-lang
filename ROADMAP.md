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
| 8 | Ownership & borrow checking (end of arena) | 🔄 | 10–14 weeks |
| 9 | Fine-grained effects & capabilities | ⬜ | 6–8 weeks |
| 10 | Taint tracking & sandbox | ⬜ | 8–10 weeks |
| 11 | SSA IR + optimisation | ⬜ | 10–14 weeks |
| 12 | Native LLVM backend | ⬜ | 10–14 weeks |
| 13 | Package manager `hls-pkg` | ⬜ | 6–8 weeks |
| 14 | Tooling: LSP, formatter, linter | ⬜ | 6–8 weeks |
| 15 | Safe C FFI | ⬜ | 4–6 weeks |
| 16 | Concurrency & async (data-race freedom) | ⬜ | 12–16 weeks |
| 17 | Formal verification & contracts | ⬜ | 10–14 weeks |
| 18 | Testing ecosystem & fuzzing | ⬜ | 4–6 weeks |
| 19 | Documentation, book, playground | ⬜ | 6 weeks |
| 20 | HLS v1.0 — API freeze, LTS, pure-HLS bootstrap | ⬜ | 4 weeks |

Estimated total duration: ~24–30 months (small team of 2–4 full-time).

---

## STAGE 8 — Ownership & borrow checking 🔄 (alpha shipped v0.4.0-alpha)

**Goal:** memory safety WITHOUT GC, ending the arena model.

**Status (v0.4.0-alpha):** the **first subset of Stage 8** has shipped.
The compiler now performs **static ownership tracking** via three new
primitives — `drop`, `clone`, `take` — and use-after-move is a compile
error. The runtime still uses arena allocation; runtime memory reclamation
is the Stage 8-beta target.

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

## STAGE 8 (continued) — full borrow checking & end-of-arena runtime

> The Stage 8-alpha subset has shipped in v0.4.0-alpha (see the top of this
> file). The remaining work below is the Stage 8-beta target.

**Work:**
- Move semantics by default; checked borrows: one mutable borrow OR many
  read-only borrows.
- Exact `free` when leaving scope; statically prove no use-after-free /
  double-free through the type system itself.
- Minimal lifetimes: no lifetime syntax — infer everything, only report errors
  when inference fails.
- New C runtime replacing arena: stack `alloca` + heap `malloc` with static
  free timing.

**Acceptance:** a memory-stress program (web server running 24h) does not
increase RSS; Valgrind/ASan clean.

**Highest risk in the entire roadmap** — budget 30% extra time; may downgrade
to "ref-counting + ownership analysis pass" if full borrow-checking is too
costly.

## STAGE 9 — Fine-grained effects & capabilities ⬜

**Goal:** every effect declared individually and statically verified.

**Work:**
- Split effects: `uses IO`, `uses Net`, `uses Fs`, `uses Clock`, `uses Rand`,
  `uses Proc`.
- Capability tokens: opening a file/network must hold a capability granted by
  `main` — impossible to "sneak" a file read deep inside a library.
- Pure functions explicitly marked & statically guaranteed → eligible for
  memoisation / JIT compilation.
- Default-deny at compile time when a declaration is missing.

**Acceptance:** a program that doesn't declare `uses Net` CANNOT call a socket
even through 5 function layers — the compile error points to the exact call
chain.

## STAGE 10 — Taint tracking & sandbox ⬜

**Goal:** stop input-driven vulnerabilities (injection, XSS, path traversal)
at the type level.

**Work:**
- `tainted[T]` type: data from inputs is automatically `tainted`; only usable
  after `sanitize`.
- Standard normalising filters for SQL/HTML/paths/commands; matches the
  standard sentinel library.
- Sandboxed compile mode: a program only runs inside a granted directory /
  socket set.
- Taint analysis report from the compiler (`hlc --audit`).

**Acceptance:** deliberately using user input in an SQL statement without
sanitising → compile error showing the taint propagation path.

## STAGE 11 — SSA IR + optimisation ⬜

**Goal:** performance on par with C/Rust at `-O2`.

**Work:**
- Mid-level SSA IR (HLIR) written in HLS; HLS→HLIR→C.
- Optimisations: inlining, constant folding, DCE, copy propagation, escape
  analysis, loop-invariant code motion, strength reduction.
- `-O fast` mode skips checks **only when** safety is provable (out-of-bounds
  is impossible, addition cannot overflow) — or when the user signs off on the
  risk.
- Position info in panics (file:line) thanks to IR debug info.

**Acceptance:** standard benchmarks (sieve, json parse, matrix) reach ≥ 95% of
`gcc -O2` performance on equivalent C code; differential tests still 100%
after optimisation.

## STAGE 12 — Native LLVM backend ⬜

**Goal:** drop the C intermediate, emit machine code directly.

**Work:**
- HLIR → LLVM IR (via C++ binding or by emitting `.ll` text).
- Multi-platform: x86-64, AArch64; cross-compile (`--target aarch64-linux`).
- Stack probes (deep recursion no longer segfaults), hot/cold attributes,
  PGO (profile-guided optimisation).
- C backend kept as a fallback and for exotic platforms.

**Acceptance:** thrice-clean bootstrap: HLS→LLVM→native→self-compile, with
output matching the C backend.

## STAGE 13 — Package manager `hls-pkg` ⬜

**Goal:** reuse code with verified provenance.

**Work:**
- `hls-pkg.toml` + content-addressed lockfile: each package identified by the
  SHA-256 of its content + its effect table.
- Enforce package effects: a pure library package CANNOT declare `uses Net`.
- Decentralised (git-based) registry + transparency log.
- `hls-pkg audit`: print the total capabilities/effects of the entire
  dependency tree.

**Acceptance:** install a third-party package, view its effect report, build
bit-for-bit reproducibly from the lockfile.

## STAGE 14 — Tooling: LSP, formatter, linter ⬜

**Goal:** first-class developer experience.

**Work:**
- `hls-lsp`: language server (go-to-definition, completion, rename, real-time
  type/effects diagnostics).
- `hlfmt`: opinionated formatter (like gofmt) — ends style debates.
- `hllint`: safety rules (detect ignored Result, empty unwrap, unnecessary
  effect propagation).
- All three written in HLS, shipped as native binaries.

**Acceptance:** VS Code + Neovim plugins; formatter idempotent (running twice
= running once).

## STAGE 15 — Safe C FFI ⬜

**Goal:** reuse the C ecosystem without breaking the safety enclave.

**Work:**
- `extern "C"` with explicit type table; compiler emits an ABI-compatibility
  checking header.
- Ownership rules across the boundary: data passed into FFI is frozen or
  copied; results must pass through a null/bounds-check layer.
- `bindgen`: emit HLS declarations from C headers with manual effect
  annotations.
- Fence: every FFI call automatically carries the `IO` effect (safe by
  default).

**Acceptance:** call `libcurl` from HLS via the bindgen layer; ASan detects
no errors in the glue code.

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
