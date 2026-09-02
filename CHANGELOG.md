# Changelog — Hieu Louis (HLS)

All notable changes to Hieu Louis are documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases on `main` follow the 20-stage roadmap (see [ROADMAP.md](ROADMAP.md)).
Releases on `feature/community-extensions` carry non-roadmap upgrades:
new stdlib modules, tooling, examples, and CI/CD improvements.

## [unreleased] — Stage 10-alpha (v0.7.0-alpha)

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
    - `sanitize_command` — rejects 26 shell metacharacters.
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
