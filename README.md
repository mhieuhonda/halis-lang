<div align="center">

![Halis logo](Halis.png)

# Halis

**A high-security, self-hosting, native-compiled programming language**

`hlc` is written 100% in Halis itself. The compiler self-compiles,
and two compilation passes produce **byte-identical** output.

[Specification](SPEC.md) · [20-stage roadmap](ROADMAP.md) · [Security](SECURITY.md)

</div>

---

## Why Halis?

Halis (HLS) exists because of one belief: **safety is not optional, and
performance is not the price of safety**.

```hls
# A function without 'uses IO' is GUARANTEED pure by the compiler
fn sum_squares(n: int) -> int {
    let mut total: int = 0
    let mut i: int = 0
    while i < n {
        total = total + i * i
        i = i + 1
    }
    return total
}

fn main() -> int uses IO {
    println("sum = " + sum_squares(100).to_str())
    return 0
}
```

Seven core guarantees of v0.20.0-alpha:

1. **I/O is a declared effect.** Forget `uses IO` while printing to the
   screen? Compile error — even when the call is indirect through 5 function
   layers.
2. **Every operation is checked.** Integer overflow, divide-by-zero,
   out-of-bounds array access — all halt safely, with no undefined behaviour.
   v0.5 has no switch to disable checks.
3. **No null. No uninitialised variables, no hidden state, no globals.**
   Everything is explicit so it can be audited.
4. **(Stage 8, complete) Memory safety without GC.** Use-after-move is a
   compile error, `clone()` deep-copies every owned type, and the
   generated runtime is reference-counted with **exact free at scope
   exit** — a memory-stress program runs with a flat RSS (verified in CI
   under a 256 MB address-space limit). No arena, no GC, no leaks.
5. **(NEW in v0.5.0-alpha) Fine-grained effects & capabilities.** The
   single `IO` effect is split into five capabilities — `IO`, `Fs`,
   `Clock`, `Args`, `Exit` — each individually declared and statically
   verified. `uses IO` remains as a backwards-compatible blanket alias.
   A function with no `uses` clause is statically guaranteed pure.
   See [SPEC.md section 9 & 17](SPEC.md).
6. **(Stage 9-beta, v0.6.0-alpha) Explicit `pure` keyword + `--audit`
   flag.** A function declared `pure` must have no `uses` and must
   transitively call nothing effectful — enforced by the checker with
   a witness edge in the error message. `hlc --audit` and
   `boot.py --audit` print the full capability / effect tree of every
   function in the program — declared vs computed, with a clear
   OK/VIOLATION status per function. Useful for security review and
   supply-chain audits.
7. **(NEW in v0.7.0-alpha) Taint tracking.** The new built-in generic
   type `tainted[T]` wraps any value as potentially-attacker-controlled.
   The checker statically rejects passing a tainted value to a sink
   (print, println, write_file, read_file, file_exists, exit) — the
   user must sanitise first via the standard library
   `sanitize_html` / `sanitize_path` / `sanitize_sql_identifier` /
   `sanitize_sql_string` / `sanitize_command` / `sanitize_filename`
   helpers, or use the explicit `taint_unwrap()` escape hatch.
   `tainted_args()` returns the program's argv wrapped as
   `list[tainted[str]]`, so every command-line input is tainted by
   default. See [SPEC.md section 19](SPEC.md).

## Self-hosting — proof, not promise

```
                    ┌────────────────────────────────────────────┐
                    │                                            │
  src/hlc.hls ──► boot/ (Stage-0, seed) ──► hlc.c (pass 1)       │
  (compiler in         used ONLY ONCE to        │                │
   HLS, ~3000 lines)   bootstrap the cycle      ▼                │
                                           gcc -O2                │
                                                │                  │
                                                ▼                  │
                                        bin/hlc  (native) ───────┤
                                                │  recompiles self │
                                                ▼                  │
                                        hlc.c (pass 2)             │
                                                │                  │
                                 diff pass 1 vs pass 2 = 0 bytes ─┘
```

`make bootstrap` performs the entire chain above and confirms the
**determinism** of the self-compilation process. From Stage 5 onwards, the
language grows in itself.

## Quick start

Requirements: Python 3.8+ (only for the Stage-0 seed), gcc or clang.

```bash
# 1. Run directly via Stage-0 (interpreted)
python3 boot/boot.py examples/hello.hls

# 2. Build the native compiler via the bootstrap chain
make bootstrap
#    → bin/hlc  (the HLS compiler written in HLS, compiled native)

# 3. Compile your program to a native binary
make run F=examples/primes.hls

# 4. Run the full test suite (interpreter + native + differential +
#    bootstrap determinism + LLVM IR + memory-stress RSS check + fmt +
#    lint + proof/contracts). See CHANGELOG.md for the current count.
make test

# 5. Try the fine-grained effects (v0.7.0-alpha)
python3 boot/boot.py examples/effects_demo.hls arg1 arg2

# 6. Audit the capability / effect tree of a program
make audit F=examples/effects_demo.hls

# 7. Run the new taint tracking demo (v0.7.0-alpha)
python3 boot/boot.py examples/taint_demo.hls "first arg" "/api/users"

# 8. Run the Stage 10-beta demo (read_file_tainted + extended audit, v0.8.0-alpha)
python3 boot/boot.py examples/taint_beta_demo.hls examples/data.txt

# 9. Stage 11-alpha: print HLIR / optimiser stats (v0.9.0-alpha)
python3 boot/boot.py --emit ir examples/optimize_demo.hls
python3 boot/boot.py --opt-stats examples/optimize_demo.hls

# 10. Stage 12-alpha: print LLVM IR (v0.10.0-alpha)
python3 boot/boot.py --emit llvm examples/llvm_demo.hls
python3 boot/boot.py --emit llvm --target aarch64-linux examples/llvm_demo.hls

# 11. Stage 13-alpha: package manager (v0.11.0-alpha)
python3 tools/hls-pkg.py init mypkg
cd mypkg
python3 ../tools/hls-pkg.py add std.str https://github.com/mhieuhonda/halis-lang.git std/str.hls
python3 ../tools/hls-pkg.py lock    # resolves deps + enforces effect surface
python3 ../tools/hls-pkg.py audit  # total effect report of dep tree
python3 ../tools/hls-pkg.py verify # check SHA-256 hashes still match
python3 ../tools/hls-pkg.py build  # compile main.hls

# 12. Stage 14-alpha: tooling (v0.12.0-alpha)
python3 tools/hlfmt.py examples/hello.hls            # format to stdout
python3 tools/hlfmt.py -c examples/hello.hls          # check formatting
python3 tools/hllint.py examples/hello.hls            # lint
python3 tools/hllint.py --list                        # list rules
python3 tools/hls-lsp.py --check examples/hello.hls   # one-shot diagnostics

# 13. Stage 15-alpha: Safe C FFI (v0.13.0-alpha)
python3 boot/boot.py examples/ffi_demo.hls           # call libc functions
python3 tools/hlbindgen.py /usr/include/stdlib.h      # generate extern block from C header
```

## Language example

```hls
# Struct + methods — structs have reference semantics
struct Point {
    x: int,
    y: int
}

impl Point {
    fn dist2(self: Point) -> int {
        return self.x * self.x + self.y * self.y
    }
    fn translate(mut self: Point, dx: int) -> void {
        self.x = self.x + dx
    }
}

fn main() -> int uses IO {
    let p: Point = Point { x: 3, y: 4 }
    p.translate(1)
    println("dist2 = " + p.dist2().to_str())     # dist2 = 32

    # List + map (insertion-ordered)
    let counts: map[str, int] = map_new()
    counts.set("hieu", 1)
    counts.set("louis", 2)

    # Strings are byte strings, full operation set
    let s: str = "  Halis  "
    println("[" + s.trim() + "]")

    # for-in loop: length snapshotted once
    for i: int in range(0, 5) {
        print(i.to_str() + " ")
    }
    println("")
    return 0
}
```

See also: [examples/](examples/) — including `secure_demo.hls` demonstrating
safe panics on integer overflow, `wordcount.hls` reading a real file,
`web_demo.hls` showing URL parsing, JSON handling and HTML escaping,
`enum_demo.hls` / `option_demo.hls` / `result_demo.hls` demonstrating Stage 7
features (enums, match, `?` operator, Option/Result), the new
`ownership_demo.hls` demonstrating Stage 8-alpha's `drop`/`clone`/`take`,
and `effects_demo.hls` demonstrating Stage 9-alpha's fine-grained effects
(`uses Fs`, `uses Clock`, `uses Args`).

## Standard library (Stage 6 + Stage 7)

HLS ships with a small pure-HLS standard library focused on web programming
and error handling:

| Module | What it provides |
|--------|------------------|
| `std.str` | `str_repeat`, `str_reverse`, `str_join`, `str_replace`, `str_to_lower_ascii`, `str_to_upper_ascii`, `str_count`, `str_pad_left`, `str_pad_right`, `str_index_of` |
| `std.math` | `math_abs_int/float`, `math_min/max`, `math_clamp`, `math_power_int/float`, `math_sqrt`, `math_floor`, `math_ceil`, `math_round`, `math_sum_int/float`, `math_avg_float` |
| `std.json` | `json_parse(src) -> JsonValue`, `json_stringify(v) -> str`, plus constructors (`json_null/bool/int/float/str/array/object`) and accessors (`json_object_get`, `json_object_has`, `json_is_*`) |
| `std.url` | `url_parse(s) -> Url`, `url_stringify(u) -> str`, `url_query_parse(qs) -> map[str,str]`, `url_query_stringify(m) -> str`, `url_encode(s)`, `url_decode(s)` |
| `std.html` | `html_escape(s)`, `html_escape_attr(s)`, `html_unescape(s)`, `html_tag(name, attrs, content)`, `html_text(s)` |
| `std.option` (Stage 7) | `enum Option[T] { Some(T), None }`, `option_unwrap`, `option_unwrap_or`, `option_is_some`, `option_is_none` |
| `std.result` (Stage 7) | `enum Result[T, E] { Ok(T), Err(E) }`, `result_unwrap`, `result_unwrap_or`, `result_err_or`, `result_is_ok`, `result_is_err`, `int_parse(s) -> Result[int, str]`, `float_parse(s) -> Result[float, str]` |
| `std.bits` (non-roadmap) | `bits_shl/shr/sar`, `bits_and/or/xor/not`, `bits_get/set`, `bits_popcount/clz/ctz`, `bits_byte`, `bits_bytes_be/le`, `bits_from_bytes_be/le` (HLS has no bitwise operators — these helpers are pure functions built on arithmetic) |
| `std.set` (non-roadmap) | `set_str_new`, `set_str_from_list`, `set_str_add`, `set_str_contains`, `set_str_size`, `set_str_to_list`, `set_str_union`, `set_str_intersect`, `set_str_diff`, `set_str_equal` (string sets backed by `map[str, bool]`) |

Each module is written in HLS itself and can be used inside `hlc` (the
compiler) or any user program. Import with `import "std.option"` (or whichever
module you need).

## Repository layout

```
halis-lang/
├── SPEC.md              # Language constitution (full v0.30 spec)
├── ROADMAP.md           # 20-stage roadmap to v1.0
├── SECURITY.md          # Threat model & security policy
├── boot/                # Stage-0: bootstrap seed (pure Python, ~3,200 lines)
│   ├── lexer.py         #   lexer (~150 lines)
│   ├── parser.py        #   syntax → AST (~620 lines)
│   ├── checker.py       #   type check + effects + taint analysis (~1,500 lines)
│   ├── interp.py        #   evaluator (reference semantics, ~660 lines)
│   └── boot.py          #   CLI (~290 lines)
├── src/
│   └── hlc.hls          # ★ COMPILER written 100% in HLS (~6,000 lines)
│                        #   lexer → parser → checker → C codegen → self-compile
├── std/                 # Standard library (Stage 6 + Stage 10, in HLS)
├── examples/            # hello, fibonacci, primes, wordcount, secure_demo, ...
├── tests/
│   ├── ok/              #   60 valid programs (incl. safe panics + Stage 9 demos)
│   ├── fail/            #   60 programs that MUST be rejected (types/effects/taint)
│   └── run_tests.sh     #   549 tests: ok/fail/differential/bootstrap fixed-point
├── Makefile             # bootstrap · test · run · examples · audit · opt-stats · emit-ir · emit-llvm
└── bin/                 # (generated) native hlc
```

The `tools/` directory (Stage 11+) holds the SSA IR + optimiser
(`tools/ir/`), the LLVM IR text backend (`tools/llvm_emit.py`), and the
package manager (`tools/hls-pkg.py`). These are Python tools today;
re-implementing them in HLS itself is a Stage 11/12/13 release target.

## Design philosophy (abridged)

| Principle | Realisation |
|-----------|-------------|
| Safety by default | Checked arithmetic, checked array bounds — no off switch |
| Explicitness for audit | Mandatory types, no shadowing, no implicit casts, no hidden state |
| I/O as an effect | `uses IO` statically verified, fixpoint over the call graph |
| No null | No null references, no uninitialised variables |
| Performance via AOT | HLS → C → machine code; future generics will monomorphise |
| Small core | Everything else extends via the standard library, no syntax bloat |

Full details: [SPEC.md](SPEC.md) · Stage-by-stage roadmap:
[ROADMAP.md](ROADMAP.md).

## Status

**v0.33.0-alpha — Stage 16 & 17 perfection re-verified + Deep-scan-12 (22 bug fixes across the IR optimiser, LLVM backend, stdlib, bindgen, package manager, LSP, linter, formatter, model checker, and URL module). 549/549 tests PASS, bootstrap deterministic, differential suite (interpreter ↔ native, including -O fast) byte-identical.** Previous release v0.32.0-alpha merged Dependabot PRs (action-gh-release 3, checkout 7, setup-python 7) and the deep-scan-and-beyond-v1 branch (PR #22) which closed all 7 open issues (#15-#21). Stage 17 was perfected in v0.30.0-alpha (proof-engine soundness overhaul + native ensures + loop-invariant engine).

- ✅ Complete core specification
- ✅ Stage-0 reference (interpreted, with type + effects checking)
- ✅ Self-hosted compiler `hlc.hls` (front-end + C backend)
- ✅ Self-compiling fixed-point; bootstrap is deterministic
- ✅ Module system & standard library (Stage 6): `std.str`, `std.math`,
  `std.json`, `std.url`, `std.html`
- ✅ **Stage 7 — Advanced type system**: `enum` + `match` (with
  exhaustiveness checking), `Option[T]`/`Result[T, E]` in stdlib,
  `?` error-propagation operator, monomorphising generics on functions /
  structs / enums, struct default field values, recursive enums
- ✅ **Stage 8 — Ownership & memory model, COMPLETE** (alpha v0.4.0-alpha
  + beta v0.19.0-alpha): `drop(x)` / `clone(x)` / `take(x)` builtins with
  compile-time use-after-move tracking; **end of the arena** — the
  generated C runtime is refcounted and the codegen's ownership-analysis
  pass inserts exact retain/release at scope exit (cleanup attributes
  cover every control-flow path). `clone()` supports every owned type
  (str / list / map / struct / enum / tainted) via per-instantiation
  helpers. The memory-stress acceptance test runs 500k allocation rounds
  with **RSS delta = 0**, enforced under a 256 MB `ulimit -v` in the
  test suite. **173/173 tests PASS** (Stage 8 final test count; the
  current count after all Stage 9 release work is **185/185**).
- ✅ **Stage 9-alpha — Fine-grained effects & capabilities** (v0.5.0-alpha):
  Single `IO` effect split into five — `IO`, `Fs`, `Clock`, `Args`, `Exit`.
  `uses` clause now accepts a comma-separated list; `uses IO` is a
  backwards-compatible blanket alias. Capability subset semantics
  (`declared ⊇ computed`) with default-deny for pure functions.
- ✅ **Stage 9-beta — Explicit `pure` keyword + `--audit` flag**
  (v0.6.0-alpha): `fn f(...) pure` declares purity explicitly; mutually
  exclusive with `uses` at parse time. `hlc --audit` and
  `boot.py --audit` print the full capability / effect tree of every
  function in the program.
- ✅ **Stage 10-alpha — Taint tracking & sandbox** (v0.7.0-alpha):
  New built-in generic type `tainted[T]` wraps any value as potentially
  attacker-controlled. Three new builtins: `tainted_args()` returns the
  program's argv wrapped as `list[tainted[str]]`; `taint_mark(x)` wraps
  any value; `taint_unwrap(x)` is the explicit untaint escape hatch. The
  checker statically rejects passing `tainted[T]` to any sink
  (`print`, `println`, `read_file`, `write_file`, `file_exists`,
  `exit`). Two new stdlib modules: `std.taint` (pure-query helpers on
  tainted strings — length, prefix/suffix, contains, slice that stays
  tainted) and `std.sanitize` (six sanitizers: `sanitize_html`,
  `sanitize_html_attr`, `sanitize_path`, `sanitize_sql_identifier`,
  `sanitize_sql_string`, `sanitize_command`, `sanitize_filename`).
  The new `examples/taint_demo.hls` exercises the full flow. **135/135
  tests PASS**.
- ✅ **Stage 10-beta — Taint tracking extended** (v0.8.0-alpha):
  Second taint source `read_file_tainted(path: str) -> tainted[str]`
  (file content is tainted by default — useful for untrusted
  uploads / downloaded config). Extended `--audit` flag with a
  taint-flow section listing which functions call each taint
  source and each taint sink. Three new pure-query helpers in
  `std.taint`: `taint_check_byte_at`, `taint_concat`,
  `taint_concat_clean`. New JSON typed value accessors
  (`json_bool_value` / `json_int_value` / `json_float_value` /
  `json_str_value`). Bug fixes: `str.to_float` accepts scientific
  notation; `float.to_int` range-checks; `html_escape` escapes
  forward slash per OWASP; JSON parser handles UTF-16 surrogate
  pairs; base64_decode validates padding placement; URL parser
  uses the FIRST `@` for userinfo split (defensive).
  The new `examples/taint_beta_demo.hls` exercises the flow.
  **145/145 tests PASS**.
- ✅ **Stage 9 release — Complete fine-grained effects & capabilities**
  (v0.20.0-alpha): The three reserved effects `Net`, `Rand`, `Proc`
  are now active with five new builtins — `net_lookup`,
  `rand_int`, `rand_float`, `rand_seed`, `proc_exec`. The shared
  64-bit LCG makes random sequences **deterministic across the
  interpreter and native binary** — the same seed produces the same
  sequence, critical for differential testing. `net_lookup` and
  `proc_exec` are also TAINT SINKS (tainted host → DNS rebinding;
  tainted command → shell injection). **185/185 tests PASS**.
- ✅ **Stage 10 release — Taint tracking & sandbox** (v0.21.0-alpha):
  `--sandbox DIR` restricts filesystem builtins to DIR (both
  interpreter and native runtime). New taint source `read_line() ->
  tainted[str]`. `--sandbox` rejects `extern "C"` blocks (FFI
  bypasses the sandbox). 191/191 tests PASS.
- ✅ **Stage 11 release — SSA IR + optimisation** (v0.21.0-alpha):
  Two new optimiser passes — `inline_small` (inlines small pure
  functions at their call sites) and `licm` (hoists loop-invariant
  expressions out of loop bodies). 191/191 tests PASS.
- ✅ **Stage 12 release — Native LLVM backend** (v0.22.0-alpha):
  The LLVM IR text backend now lowers struct literals, enum literals,
  match expressions, the `?` operator, struct field access and
  assignment via a typed runtime API. The `noreturn` attribute is
  attached to `hl_die`/`hl_panic`/`hl_exit` so the optimiser cannot
  DCE the trailing `unreachable`. 199/199 + 13/13 LLVM tests PASS.
- ✅ **Stage 13 release — Package manager `hls-pkg`** (v0.23.0-alpha):
  Transparency log (append-only, SHA-256-chained JSON-lines),
  multi-file packages (directory deps with content-addressed hashing),
  version verification (records + verifies the git commit SHA).
  New CLI: `hls-pkg publish` + `hls-pkg log [--verify]`. 199/199 +
  13/13 LLVM tests PASS.
- ✅ **Stage 14 release — Tooling** (v0.24.0-alpha, perfected
  v0.26.0-alpha): `hls-lsp` language server, `hlfmt` formatter,
  `hllint` linter (control-flow-aware `?`-unwrap analysis).
- ✅ **Stage 15 release — Safe C FFI** (v0.25.0-alpha, perfected
  v0.26.0-alpha): `extern "C"` blocks, `hlbindgen` C-header → HLS
  generator (structs, enums, ABI header with `_Static_assert` size
  checks), ownership-across-boundary enforcement.
- ✅ **Stage 16 release — Concurrency & async, data-race freedom**
  (v0.27.0-alpha): `spawn(f, args) -> Task[R]` (real threads —
  pthread native / Python-thread interpreter), `Chan[T]`
  message-passing channels (unbounded MPMC FIFO, blocking recv),
  `select(list[Chan[T]])`, actor model via enum mailboxes, and a new
  **`Conc` effect**. **Data races are impossible by construction:**
  the Send rule set (Task handles are the first non-Send type) plus
  the ownership boundary rule — *sharing a variable with a task
  outside a channel is a compile error*. Runtime hardening deep-copies
  non-provable values at the boundary, channels use atomic
  refcounts, and a built-in deadlock detector halts all-blocked
  programs with a clean panic. 426/426 tests PASS.
- ✅ **Stage 17 release — Formal verification & contracts**
  (v0.28.0-alpha): `requires`/`ensures` contracts (validated, pure,
  params-only scope; `result` in ensures), **static call-site
  checking** (`div(10, 0)` is a compile error), `--contracts` runtime
  assertions, the **interval proof engine** — symbolic length and
  minimum-length facts from contracts prove overflow/division/bounds
  checks dead — and **`-O fast`** elides exactly those (soundness:
  every elision is guarded by the precondition that proved it).
  Tools: `hlprove` (proof reports + the z3 SMT-LIB2 bridge +
  loop-invariant suggestions) and `hlmodel` (exhaustive finite-state
  model checking by executing the transition relation; BFS
  reachability + invariants). Acceptance: `examples/hmac_proven.hls`
  — an HMAC envelope whose hot path is fully proven (fast output is
  byte-identical to the interpreter). 459/459 tests PASS.
- ⬜ testing ecosystem (Stage 18), docs/book/playground (Stage 19),
  v1.0 (Stage 20)...

## Contributing

Every contribution must preserve the core guarantees and pass
`make test` (the full suite: interpreter + native + differential +
bootstrap determinism + LLVM IR + memory-stress + fmt + lint +
proof/contracts). Every new feature must first be used inside `hlc`
itself — the compiler is always the first customer of the language.

**Branch protection:** the `main` branch is protected — all non-admin
contributors must open a pull request. CI must pass on every PR (full
test suite + bootstrap determinism + example programs, on the 2×2
matrix of Python 3.8/3.11 and gcc/clang). Direct pushes to `main` are
limited to the repo owner. Linear history is enforced (no merge
commits); force-push and branch deletion are disabled.

## Licence

[MIT](LICENSE) © 2026 mhieuhonda
