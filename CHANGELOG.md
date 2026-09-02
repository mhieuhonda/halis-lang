# Changelog — Hieu Louis (HLS)

All notable changes to Hieu Louis are documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases on `main` follow the 20-stage roadmap (see [ROADMAP.md](ROADMAP.md)).
Releases on `feature/community-extensions` carry non-roadmap upgrades:
new stdlib modules, tooling, examples, and CI/CD improvements.

## [unreleased] — Stage 14-alpha (v0.12.0-alpha)

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
