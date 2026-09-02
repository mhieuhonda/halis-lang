<div align="center">

# Hieu Louis

**A high-security, self-hosting, native-compiled programming language**

`hlc` is written 100% in Hieu Louis itself. The compiler self-compiles,
and two compilation passes produce **byte-identical** output.

[Specification](SPEC.md) · [20-stage roadmap](ROADMAP.md) · [Security](SECURITY.md)

</div>

---

## Why Hieu Louis?

Hieu Louis (HLS) exists because of one belief: **safety is not optional, and
performance is not the price of safety**.

```hls
# A function without 'uses IO' is GUARANTEED pure by the compiler
fn sum_squares(n: int) -> int {
    let mut total: int = 0
    let i: int = 0
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

Three core guarantees of v0.5.0-alpha:

1. **I/O is a declared effect.** Forget `uses IO` while printing to the
   screen? Compile error — even when the call is indirect through 5 function
   layers.
2. **Every operation is checked.** Integer overflow, divide-by-zero,
   out-of-bounds array access — all halt safely, with no undefined behaviour.
   v0.5 has no switch to disable checks.
3. **No null. No uninitialised variables, no hidden state, no globals.**
   Everything is explicit so it can be audited.
4. **(Stage 8-alpha) Use-after-move is a compile error.** The ownership
   primitives — `drop`, `clone`, `take` — let the compiler track when a
   binding has been moved, and statically reject any subsequent use.
5. **(NEW in v0.5.0-alpha) Fine-grained effects & capabilities.** The
   single `IO` effect is split into five capabilities — `IO`, `Fs`,
   `Clock`, `Args`, `Exit` — each individually declared and statically
   verified. `uses IO` remains as a backwards-compatible blanket alias.
   A function with no `uses` clause is statically guaranteed pure.
   See [SPEC.md section 9 & 17](SPEC.md).

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

# 4. Run the full test suite (100 tests: types, effects, ownership, differential, bootstrap)
make test

# 5. Try the new fine-grained effects (v0.5.0-alpha)
python3 boot/boot.py examples/effects_demo.hls arg1 arg2
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
    let s: str = "  Hieu Louis  "
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

Each module is written in HLS itself and can be used inside `hlc` (the
compiler) or any user program. Import with `import "std.option"` (or whichever
module you need).

## Repository layout

```
hieu-louis-lang/
├── SPEC.md              # Language constitution (full v0.2 spec)
├── ROADMAP.md           # 20-stage roadmap to v1.0
├── SECURITY.md          # Threat model & security policy
├── boot/                # Stage-0: bootstrap seed (pure Python, ~1,400 lines)
│   ├── lexer.py         #   lexer
│   ├── parser.py        #   syntax → AST
│   ├── checker.py       #   type check + effects analysis
│   ├── interp.py        #   evaluator (reference semantics)
│   └── boot.py          #   CLI
├── src/
│   └── hlc.hls          # ★ COMPILER written 100% in HLS (~3,000 lines)
│                        #   lexer → parser → checker → C codegen → self-compile
├── std/                 # Standard library (Stage 6, in HLS)
├── examples/            # hello, fibonacci, primes, wordcount, secure_demo
├── tests/
│   ├── ok/              #   14 valid programs (incl. safe panics)
│   ├── fail/            #   22 programs that MUST be rejected (types/effects)
│   ├── snapshots/       #   expected outputs
│   └── run_tests.sh     #   56 tests: ok/fail/differential/bootstrap fixed-point
├── Makefile             # bootstrap · test · run · examples
└── bin/                 # (generated) native hlc
```

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

**v0.5.0-alpha — Stages 1–7 complete, Stage 8-alpha + Stage 9-alpha shipped**:

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
- 🔄 **Stage 8-alpha — Ownership primitives** (v0.4.0-alpha):
  `drop(x)` / `clone(x)` / `take(x)` builtins, with compile-time
  use-after-move tracking. Bindings carry a `moved` flag in both Stage-0
  and `hlc.hls`.
- 🔄 **Stage 9-alpha — Fine-grained effects & capabilities** (v0.5.0-alpha):
  Single `IO` effect split into five — `IO`, `Fs`, `Clock`, `Args`, `Exit`.
  `uses` clause now accepts a comma-separated list; `uses IO` is a
  backwards-compatible blanket alias. Capability subset semantics
  (`declared ⊇ computed`) with default-deny for pure functions. **100/100
  tests PASS**.
- ⬜ Stage 8-beta (full borrow checker, end-of-arena runtime),
  Stage 9-beta (`Net`/`Rand`/`Proc` builtins, first-class capability tokens),
  fine-grained taint, SSA IR, LLVM, concurrency...

## Contributing

Every contribution must preserve the core guarantees and pass
`make test` (100 tests, including differential testing of the two
implementations). Every new feature must first be used inside `hlc` itself —
the compiler is always the first customer of the language.

## Licence

[MIT](LICENSE) © 2026 mhieuhonda
