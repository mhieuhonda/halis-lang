# Changelog — Hieu Louis (HLS)

All notable changes to Hieu Louis are documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases on `main` follow the 20-stage roadmap (see [ROADMAP.md](ROADMAP.md)).
Releases on `feature/community-extensions` carry non-roadmap upgrades:
new stdlib modules, tooling, examples, and CI/CD improvements.

## [unreleased] — feature/community-extensions

### Added
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
- **Differential tests** for every new stdlib module under `tests/ok/`.
- **`CONTRIBUTING.md`**, **`CODE_OF_CONDUCT.md`**, **`CHANGELOG.md`**
  themselves.

### Fixed
- Initial high/low 32-bit halves of the FNV-1a 64-bit offset basis were
  transcribed incorrectly in the initial implementation of
  `crypto_fnv1a_64_hex`. Corrected to `hi = 3421674724 (0xcbf29ce4)`,
  `lo = 2216829733 (0x84222325)`.

### Changed
- **`.gitignore`** expanded to cover editor scratch files, profiling
  artifacts, and the test scratch files created by `run_tests.sh`.

### Test suite
- **92/92 PASS** (was 78/78 baseline). The 14 new tests are differential:
  they run through both the Stage-0 interpreter and the native compiler
  and require identical output.

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
