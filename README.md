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

Three core guarantees of v0.2:

1. **I/O is a declared effect.** Forget `uses IO` while printing to the
   screen? Compile error — even when the call is indirect through 5 function
   layers.
2. **Every operation is checked.** Integer overflow, divide-by-zero,
   out-of-bounds array access — all halt safely, with no undefined behaviour.
   v0.2 has no switch to disable checks.
3. **No null.** No uninitialised variables, no hidden state, no globals.
   Everything is explicit so it can be audited.

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

# 4. Run the full test suite (56 tests: types, effects, differential, bootstrap)
make test
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
safe panics on integer overflow, `wordcount.hls` reading a real file, and
`web_demo.hls` showing URL parsing, JSON handling and HTML escaping.

## Standard library (Stage 6)

HLS ships with a small pure-HLS standard library focused on web programming:

| Module | What it provides |
|--------|------------------|
| `std.str` | `str_repeat`, `str_reverse`, `str_join`, `str_replace`, `str_to_lower_ascii`, `str_to_upper_ascii`, `str_count`, `str_pad_left`, `str_pad_right`, `str_index_of` |
| `std.math` | `math_abs_int/float`, `math_min/max`, `math_clamp`, `math_power_int/float`, `math_sqrt`, `math_floor`, `math_ceil`, `math_round`, `math_sum_int/float`, `math_avg_float` |
| `std.json` | `json_parse(src) -> JsonValue`, `json_stringify(v) -> str`, plus constructors (`json_null/bool/int/float/str/array/object`) and accessors (`json_object_get`, `json_object_has`, `json_is_*`) |
| `std.url` | `url_parse(s) -> Url`, `url_stringify(u) -> str`, `url_query_parse(qs) -> map[str,str]`, `url_query_stringify(m) -> str`, `url_encode(s)`, `url_decode(s)` |
| `std.html` | `html_escape(s)`, `html_escape_attr(s)`, `html_unescape(s)`, `html_tag(name, attrs, content)`, `html_text(s)` |

Each module is written in HLS itself and can be used inside `hlc` (the
compiler) or any user program. Import with `import "std.json"` (or whichever
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

**v0.2.0 — Stages 1–6 complete** (see ROADMAP):

- ✅ Complete core specification
- ✅ Stage-0 reference (interpreted, with type + effects checking)
- ✅ Self-hosted compiler `hlc.hls` (front-end + C backend)
- ✅ Self-compiling fixed-point; 60/60 tests PASS
- ✅ Module system & standard library (Stage 6): `import`, `std.str`,
  `std.math`, `std.json`, `std.url`, `std.html`
- ⬜ enum/generics, ownership, LLVM, concurrency...

## Contributing

Every contribution must preserve the three core guarantees and pass
`make test` (56 tests, including differential testing of the two
implementations). Every new feature must first be used inside `hlc` itself —
the compiler is always the first customer of the language.

## Licence

[MIT](LICENSE) © 2026 mhieuhonda
