# Changelog — Halis (HLS)

All notable changes to Halis are documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases on `main` follow the 20-stage roadmap (see [ROADMAP.md](ROADMAP.md)).
Releases on `feature/community-extensions` carry non-roadmap upgrades:
new stdlib modules, tooling, examples, and CI/CD improvements.

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
