# Hieu Louis language specification (HLS) — v0.5.0-alpha

> **Hieu Louis** is a high-security, native-compiled programming language
> designed around the philosophy: **safety by default, explicitness for
> auditability, performance via AOT compilation**. Version v0.5.0-alpha
> introduces the **Stage 9-alpha fine-grained effects & capabilities**
> model: a single `IO` effect is split into five capabilities (`IO`, `Fs`,
> `Clock`, `Args`, `Exit`) that are individually declared and statically
> verified through the call graph. `uses IO` remains as a backwards-compatible
> blanket alias for the entire IO family. Every operation is still checked,
> every effect is statically tracked, no null, no undefined behaviour, and
> use-after-move is a compile error (Stage 8-alpha).

- Source files: `*.hls`
- Self-hosted compiler: `src/hlc.hls` (HLS → C → native)
- Bootstrap seed: `boot/` (Stage-0, used to bootstrap the self-hosting cycle)
- Versioning: `MAJOR.MINOR.PATCH`; the language freezes at v1.0 (see `ROADMAP.md`)

---

## 1. Design philosophy

1. **Safety is the default, not an option.** Overflow-checked arithmetic,
   bounds-checked arrays, safe-halt on divide-by-zero. In v0.2 there is **no
   switch to disable checks** — the "fast unchecked mode" is only unlocked by
   formal proof (Stage 17).
2. **Explicitness for auditability.** Every variable must have a declared
   type. No implicit type inference, no implicit casts, no hidden globals, no
   hidden state. An auditor can read line by line and know exactly what
   happens.
3. **I/O is an effect, effects must be declared.** Reading a single line of a
   `fn` that lacks `uses IO` lets the compiler **guarantee** (via static
   analysis) that the function is pure — no disk writes, no screen output, no
   network or environment reads.
4. **No null.** No null references exist. Uninitialised data does not exist
   (no declaring a variable without assigning it).
5. **Performance via AOT.** HLS compiles to C and then to native machine code.
   No VM, no GC in the v0.2 core (memory model: see section 11).

---

## 2. Lexical rules

### 2.1. Source code
- Source files are UTF-8 byte strings. The v0.2 lexer works on **bytes**.
- Strings in HLS v0.2 are **byte strings**; full Unicode API lands in Stage 6.

### 2.2. Whitespace & newlines
- Space, tab, CR, LF are all whitespace. **Newlines are not grammatically
  significant.**
- Anti-ambiguity rule: a *new statement* cannot start with `(`, `[`, `.` or a
  unary operator. If those tokens appear in statement position, the parser
  treats them as a continuation of the previous expression. So statements like
  `(x);` `[1,2];` `-x;` are always syntax errors (they are meaningless).

### 2.3. Comments
- `#` to end of line. No block comments in v0.2.

### 2.4. Identifiers
- `[A-Za-z_][A-Za-z0-9_]*`. Convention: functions/variables `snake_case`,
  structs `PascalCase`.
- Cannot clash with keywords. Keywords (19): `fn let mut return if else while
  for in break continue struct impl import uses true false enum match`.

### 2.5. Reserved keywords (unused, error if encountered): `secure`, `trait`

### 2.6. Numbers
- Integer: `[0-9][0-9_]*` (underscores for readability: `1_000_000`). Type
  `int` — two's complement, signed 64-bit. Range: −9,223,372,036,854,775,808 …
  9,223,372,036,854,775,807. The literal `-9223372036854775808` (INT64_MIN) is
  valid (the parser folds the minus sign); a positive literal exceeding
  INT64_MAX is a compile error.
- Float: `[0-9][0-9_]* . [0-9][0-9_]*` (digits required on both sides of the
  dot). Type `float` — IEEE 754 binary64. No scientific notation in v0.2.

### 2.7. Strings
- `"..."`, escapes: `\n` `\t` `\\` `\"`. Any other escape is a syntax error.
- Strings containing raw newlines are an error. The empty string `""` is
  valid.

### 2.8. Operators & symbols
```
->  ==  !=  <=  >=  <  >  =  +  -  *  /  %  !  &&  ||  ?
(  )  {  }  [  ]  ,  :  . 
```
Lone `&` and `|` are lexical errors. No bitwise operators in v0.3 (each
bitwise operator will be added with its own checked semantics — later stage).
The `?` postfix operator is the error-propagation operator (section 12).

---

## 3. Types

| Type      | Meaning                                  | C representation (backend) |
|-----------|------------------------------------------|----------------------------|
| `int`     | signed 64-bit integer, **overflow-checked** | `int64_t`               |
| `float`   | 64-bit IEEE 754 float                    | `double`                   |
| `bool`    | `true` / `false`                         | `bool`                     |
| `str`     | byte string, explicit length            | `hl_str*`                  |
| `list[T]` | dynamic array of `T`                    | `hl_list*`                 |
| `map[str, T]` | hash map with `str` keys, `T` values | `hl_map*`               |
| `void`    | only used as a return type (empty)      | —                          |
| `Name`    | user-defined struct (reference semantics) | `Name*`                  |
| `Name`    | user-defined enum (sum type, reference semantics) | `Name*`       |
| `Name[T1, T2, ...]` | generic instantiation of a struct/enum | mangled name |

Rules:
- **No implicit casts.** `int → float` requires `x.to_float()`, the reverse
  `x.to_int()`.
- Structs have **reference semantics** (like a checked pointer); assigning a
  struct assigns the reference. Move semantics & ownership arrive in Stage 8.
- Enums have **reference semantics** too — variant values are heap-allocated
  tagged unions. (See section 11b for the memory model.)
- `map` in v0.3 only has `str` keys (general key support: later stage).
- `==`/`!=` comparison applies to: `int`, `float`, `bool`, `str`. `list`,
  `map`, structs, enums **cannot** be compared with `==` in v0.3.
- Ordering comparison `< <= > >=` applies to: `int`, `float` (numeric) and
  `str` (bytewise, like `memcmp`).
- Generic types are **monomorphised**: each instantiation produces its own
  C type/function (no boxing, no virtual dispatch — performance equal to
  hand-written code).

---

## 4. Program structure

A `.hls` file is a sequence of **top-level** declarations (any order, forward
references allowed):

```
program        := (structdef | enumdef | impl | fndef | import)*
structdef      := "struct" Ident typeparams? "{" field ("," field)* ","? "}"
field          := Ident ":" type ("=" expr)?          # default value is optional (Stage 7)
enumdef        := "enum" Ident typeparams? "{" variant ("," variant)* ","? "}"
variant        := Ident ("(" type ("," type)* ","? ")")?
typeparams     := "[" Ident ("," Ident)* "]"
impl           := "impl" Ident "{" fndef* "}"
fndef          := "fn" Ident typeparams? "(" params? ")" ("->" type)? ("uses" "IO")? block
params         := param ("," param)* ","?
param          := "mut"? Ident ":" type
type           := "int" | "float" | "bool" | "str" | "void"
                | "list" "[" type "]"
                | "map" "[" "str" "," type "]"
                | Ident typeargs?                      # struct/enum name, optionally generic
typeargs       := "[" type ("," type)* "]"
import         := "import" string-literal
block          := "{" stmt* "}"
```

- **The `main` function** is required: `fn main() -> int` or `fn main()`; it
  has no parameters. The return value is the process exit code; a void `main`
  returns 0.
- No globals, no global constants. Imports load other `.hls` files (Stage 6).
- Duplicate names (function–function, struct–struct, enum–enum, duplicate
  methods inside one `impl`) are errors. A struct and an enum cannot share a
  name.
- Structs must have at least 1 field. **Struct fields may have default
  values** (Stage 7): `struct Point { x: int, y: int = 0 }`. When a struct
  literal omits a field that has a default, the default is used. Fields with
  defaults must come **after** fields without defaults (so the syntactic
  order is still well-defined).
- **Enums (Stage 7)** declare a sum type. Each variant has 0 or more
  payload types. Example:
  ```
  enum Color { Red, Green, Blue }
  enum Shape {
      Circle(float),
      Rect(float, float),
      Point
  }
  enum Option[T] { Some(T), None }
  enum Result[T, E] { Ok(T), Err(E) }
  ```
- **Type parameters** (generics) appear in `[...]` right after the name. They
  are uppercase by convention but the language does not enforce this.
  Generic functions, structs and enums are monomorphised at the call site /
  use site — every distinct instantiation gets its own generated code.

### 4.1. Imports (Stage 6)

```
import "path/to/file.hls"     # relative path
import "std.str"              # standard library module
```

- Imports are resolved relative to the importing file's directory, except for
  `std.*` modules which are resolved by the compiler.
- Each `.hls` file is compiled once per program; circular imports are an error.
- Imported top-level declarations (structs, functions, methods) become visible
  in the importing file. Duplicate names across files are an error.

---

## 5. Statements

```
stmt := let | assign | "if" ... | "while" ... | "for" ... | "return" ...
      | "break" | "continue" | "match" ... | callstmt

let     := "let" "mut"? Ident ":" type "=" expr
assign  := lvalue "=" expr
lvalue  := Ident (("." Ident) | ("[" expr "]"))*
if      := "if" expr block ("else" (if | block))?
while   := "while" expr block
for     := "for" Ident ":" type "in" expr block
return  := "return" expr?
match   := "match" expr "{" arm ("," arm)* ","? "}"     # Stage 7
arm     := pattern "=>" expr
pattern := (Ident ".")? Ident ("(" Ident ("," Ident)* ")")?  # constructor pattern
        | "_"                                            # wildcard
callstmt:= call-expression          # expression-statements must be function/method calls
```

Rules:
- `let` declares a binding. **`mut` governs REASSIGNMENT of the binding**:
  only `let mut x` (or a `mut x` parameter) may write `x = new_value`.
- Field assignment (`p.x = v`) and index assignment (`xs[i] = v`) mutate the
  **contents** of data through a reference — allowed on any binding
  (consistent with `xs.push(v)`).
- **No shadowing:** declaring a name already visible in an enclosing scope is
  an error. Sibling scopes (two different loops both naming `i`) are fine.
  **Pattern bindings in `match` arms** introduce a new scope: each arm's
  bindings shadow the outer scope for the duration of that arm only — this
  is the one and only shadowing exception, and it is safe because arms are
  mutually exclusive.
- `if`/`while`: the condition **must be `bool`** — no "truthiness". In
  condition position and the `for` iterable position (the position directly
  before a `{` block), struct literals must be wrapped in parentheses to
  disambiguate from the block.
- `for x: T in expr`: `expr` must be `list[T]`. The list length is
  **snapshotted once** on entry; elements appended during iteration are not
  visited. The loop variable `x` is immutable and only exists inside the loop
  body.
- `return` without a value is only for `void`-returning functions. For
  functions with a return type, **every path must return** (conservative
  flow analysis; `while` is not considered a return path). A `match` is
  considered a return path iff it is exhaustive and every arm returns.
- `break`/`continue` are only valid inside a loop body.
- An expression-statement must be a **call** (function or method). `x + 1;` is
  an "expression has no effect" error.
- `match` (Stage 7): the scrutinee expression must have an enum type. Arms
  are checked for **exhaustiveness** — either every variant is covered, or a
  wildcard `_ =>` arm is present. Every arm's expression must have the same
  type, which becomes the type of the `match` expression. A `match` is
  itself an expression and can appear wherever expressions can (with the
  usual caveat that in condition / iterable position, struct literals
  inside arms are unaffected because the `{` of the match is the
  delimiter).

---

## 6. Expressions & precedence

From lowest to highest:

| Precedence | Operator          | Notes |
|-----------|--------------------|---------|
| 1 (low)   | `\|\|`             | short-circuit |
| 2         | `&&`               | short-circuit |
| 3         | `==` `!=`          | type-dependent (section 3) |
| 4         | `<` `<=` `>` `>=`  | int/float/str |
| 5         | `+` `-`            | `+` on str is concatenation |
| 6         | `*` `/` `%`        | int: checked; float: IEEE |
| 7         | `!` `-` (unary)   | negation / checked unary minus |
| 8         | `?` (postfix)      | error-propagation (Stage 7, section 12) |
| 9 (high)  | `.` `[` `(`        | postfix: field, index, call |

Operands:
- Literals: `int`, `float`, `true`, `false`, `str`.
- `Ident` — variable/parameter (type from declaration).
- `(expr)` — grouping.
- `[e1, e2, ...]` — list literal. Element type inferred from context (declared
  type at `let`, parameter type, return type, element type of an enclosing
  literal). The empty literal `[]` requires a contextual type. All elements
  must have the exact same type.
- `Name { f1: e1, f2: e2, ... }` — struct literal: **all non-defaulted fields,
  in declared order** (field names written explicitly for auditability).
  Fields with default values may be omitted. Only allowed where context
  permits (not directly as an `if`/`while` condition).
- `Name.Variant` — enum variant with no payload. (Stage 7)
- `Name.Variant(a1, a2, ...)` — enum variant with payload(s). (Stage 7)
- `match scrutinee { arm, arm, ... }` — match expression (Stage 7, section 5).
- Function call `f(a, b)`, method call `x.m(a, b)`, field access `x.f`,
  index access `xs[i]` (only `list`; `i` must be `int`, runtime bounds check).
- `expr?` — error-propagation operator (Stage 7, section 12). The operand
  must have an enum type with an `Err` variant (one payload) or a `None`
  variant (no payload). The expression yields the unwrapped success value.
  On the error variant, the enclosing function immediately returns the
  error value (which must be assignable to the enclosing function's return
  type).
- Operands of `&&`/`||` must be `bool`.

---

## 7. Arithmetic semantics — "every operation is checked"

| Operation | Semantics |
|-----------|-----------|
| `a + b` (int) | 64-bit add; overflow → `panic "integer overflow"` |
| `a - b`, `a * b` (int) | same — overflow is a panic |
| `-a` (int) | `-INT64_MIN` is a panic |
| `a / b` (int) | `b == 0` → panic; `INT64_MIN / -1` → panic (overflow) |
| `a % b` (int) | remainder sign follows the **dividend** (like C); checked like division |
| `a / b` (float) | IEEE 754 (divide-by-0 → `inf`/`nan` — no panic) |
| `xs[i]` | `0 <= i < len` — out of range → `panic "array access out of bounds"` |
| `s.byte_at(i)` | bounds check as above |
| `s.slice(a, b)` | requires `0 <= a <= b <= len` — violation is a panic |

`float` arithmetic follows IEEE 754, no checks. Printing a float uses the
`%.6f` format.

---

## 8. Builtin functions (global)

| Function | Type | Effect | Notes |
|-----|------|----------|---------|
| `print(s: str)` | `void` | IO | print without newline |
| `println(s: str)` | `void` | IO | print with newline |
| `panic(msg: str)` | never returns | — | halt program, exit code 101 |
| `exit(code: int)` | never returns | Exit | exit with `code` |
| `str(x)` | `str` | — | `x ∈ {int, float, bool, str}` |
| `int(s: str)` | `int` | — | error if string is not a valid integer literal |
| `len(x)` | `int` | — | `str` (byte count), `list`, `map` |
| `range(a: int, b: int)` | `list[int]` | — | `[a, b)` — `a >= b` → empty |
| `map_new()` | `map[str, T]` | — | `T` taken from the surrounding context |
| `read_file(path: str)` | `str` | Fs | read entire file; I/O error → panic |
| `write_file(path: str, content: str)` | `void` | Fs | write entire file; error → panic |
| `args()` | `list[str]` | Args | command-line arguments; `args()[0]` is the program |
| `clock_ms()` | `int` | Clock | milliseconds (monotonic clock) |
| `chr(i: int)` | `str` | — | 1-byte string; `i` outside 0..255 → panic |
| `file_exists(path: str)` | `bool` | Fs | returns `true` if `path` is a regular file |
| `drop(x: T)` | `void` | — | (Stage 8-alpha) release ownership of `x`; `x` becomes moved |
| `clone(x: T)` | `T` | — | (Stage 8-alpha) return an independent deep copy of `x` |
| `take(x: T)` | `T` | — | (Stage 8-alpha) move `x`'s value out; `x` becomes moved |

`int(s)`: allows a leading minus sign, only accepts digits 0–9, value must
fit in int64 range, otherwise panics with "cannot convert string to int".

## 8b. Builtin methods

**str:** `len() -> int`, `byte_at(i: int) -> int`, `slice(a: int, b: int) -> str`,
`find(sub: str) -> int` (−1 if not found), `contains(sub: str) -> bool`,
`starts_with(p: str) -> bool`, `ends_with(p: str) -> bool`,
`split(sep: str) -> list[str]` (empty sep → panic), `trim() -> str` (strip
bytes ≤ 0x20 from both ends), `to_int() -> int`, `to_float() -> float`
(invalid string → panic), `to_str() -> str`.

**int:** `to_str() -> str`, `to_float() -> float`, `abs() -> int`
(`abs(INT64_MIN)` → panic).

**float:** `to_str() -> str` (`%.6f`), `to_int() -> int` (truncate towards 0),
`abs() -> float`.

**bool:** `to_str() -> str`.

**list[T]:** `len() -> int`, `push(v: T)`, `get(i: int) -> T` (bounds check),
`set(i: int, v: T)`, `pop() -> T` (empty → panic).

**map[str, T]:** `len() -> int`, `set(k: str, v: T)`, `get_or(k: str, dflt: T) -> T`,
`has(k: str) -> bool`, `keys() -> list[str]` (**insertion order**).

**struct:** user-defined methods via `impl`. Methods must have a first
parameter named `self` of that struct type: `fn get_x(self: Point) -> int { ... }`.
To mutate fields: declare `mut self: Point`.

---

## 9. The effects system — v0.5's security heart (Stage 9-alpha)

- **Five fine-grained effects** (Stage 9-alpha): `IO` (console print),
  `Fs` (filesystem), `Clock` (monotonic clock), `Args` (command-line args),
  `Exit` (process exit). Each builtin maps to exactly one effect (see §8).
- **Reserved effect names** (recognized but no builtins yet, error if used):
  `Net`, `Rand`, `Proc`. These will be enabled in a later stage.
- **`uses IO` is a blanket alias** — backwards compatible with all v0.3/v0.4
  code. At parse time, `uses IO` expands to the entire IO family
  `{IO, Fs, Clock, Args, Exit}`, granting every currently-defined effect.
- **Fine-grained declaration** (NEW in v0.5.0-alpha): a function can declare
  only the specific effects it needs — `uses Fs`, `uses Clock`, or
  combinations like `uses Fs, Clock`. The declared set is a **capability**:
  the function may call only builtins/callees whose computed effect set is a
  subset of the declared set.
- The analysis is a **monotone fixpoint on the static call graph**: each
  function's computed effect set is the union of its builtins' effects and
  the computed sets of all its callees. The fixpoint converges because the
  effect universe is finite (5 elements).
- **Default-deny**: a function with no `uses` clause has an empty declared
  set. Any builtin call (or call to a callee with a non-empty computed set)
  is a compile error.
- Violations → compile error, naming the function, the missing effect, and
  the violating callee.
- Consequence: every function without a `uses` clause is **guaranteed pure**
  (no possible I/O, filesystem, clock, args, or exit side effect). This is
  the foundation for later optimisations and verification.

Example:

```hls
fn double(x: int) -> int {          # PURE — guaranteed by the compiler
    return x * 2                    # no `uses` clause => no capabilities
}

fn greet(name: str) -> int uses IO {
    println("Hello " + name)        # IO must be declared
    return 0
}

# Fine-grained: only filesystem capability, nothing else.
fn read_config(path: str) -> str uses Fs {
    return read_file(path)
}

# Combination: only filesystem and clock.
fn save_with_timestamp(path: str) -> int uses Fs, Clock {
    let t: int = clock_ms()
    write_file(path, "ts=" + t.to_str())
    return t
}
```

The fixpoint analysis works through the entire call graph:

```hls
fn log_to_file(path: str, msg: str) -> void uses Fs {
    write_file(path, msg)             # Fs effect (direct)
}

fn log_warning(path: str, msg: str) -> void uses Fs {
    log_to_file(path, "[WARN] " + msg)  # Fs effect (transitive)
}

fn main() -> int uses IO {
    # IO blanket grants Fs too, so the call to log_warning is satisfied.
    log_warning("/tmp/app.log", "started")
    return 0
}
```

If `log_warning` had no `uses` clause, the compiler would report:
```
function 'log_warning' calls 'log_to_file' which requires effect 'Fs'
not declared (declared: (none - pure); missing: Fs)
```

---

## 10. The v0.3 memory model (honest & deliberate)

- v0.3 uses **arena allocation**: every string/list/map/struct/enum/variant
  is allocated and **never freed** during the process lifetime. Short
  programs (CLIs, the compiler itself) never have a problem.
- This is a deliberate decision to keep the v0.3 core small, verifiable, with
  no use-after-free, no double-free **structurally** (there is no `free`!).
- Ownership / borrow checker and exact memory reclamation: **Stage 8** of the
  roadmap.
- Deep recursion: v0.3 has no stack overflow check yet (Stage 11).

## 11. Errors & panics

- Compile errors (type, effect, syntax): halt at compile time, with line
  numbers.
- `panic(msg)`: prints `panic: <msg>` (with location when running on Stage-0)
  to stderr, exits with code **101**.
- v0.3 introduces **controlled error handling** via `Result[T, E]` and the
  `?` operator (section 12). `panic` is now **reserved for programming bugs**
  — invariant violations, impossible states. Expected failures (file not
  found, parse error, invalid input) must be reported via `Result`, not
  `panic`.

## 11b. Enum values & memory layout

- An enum value is a heap-allocated tagged union: a tag identifying the
  variant, plus (optional) a payload slot.
- Variants with no payload (`None`, `Red`, `Point`) carry no payload —
  the value is still heap-allocated for uniform reference semantics.
- Variants with one or more payloads carry the payload values inline in the
  union. Payloads use the same C representation as ordinary values (so a
  `str` payload is a `hl_str*`, a `list[T]` payload is a `hl_list*`, an `int`
  payload is an `int64_t`, a nested enum/struct payload is a pointer).
- The C backend generates a `typedef struct { int tag; union { ... } data; } Name;`
  per enum, and a `Name*` constructor per variant. Constructors of
  zero-payload variants return the same shared singleton (or a fresh
  allocation — the choice is invisible to the program because enums are
  compared by tag, not by pointer equality).
- The interpreter represents an enum value as a Python dict
  `{"enum": "Color", "var": "Red", "data": [...]}`.

## 12. The `?` error-propagation operator (Stage 7)

`expr?` is a postfix operator that **propagates errors**.

- The operand `expr` must have an enum type. The enum must have either:
  - an `Err` variant with exactly one payload (the error type), or
  - a `None` variant with no payload (treating absence as the "error").
- For `Result[T, E]`: `expr?` checks the tag. If `Ok(v)`, the expression
  yields `v` (type `T`). If `Err(e)`, the enclosing function **immediately
  returns `Result.Err(e)`** — the enclosing function's return type must be
  assignable from `Result.Err(e)` (usually it must return `Result[_, E]`).
- For `Option[T]`: `expr?` checks the tag. If `Some(v)`, yields `v`. If
  `None`, the enclosing function immediately returns `Option.None` (the
  enclosing function must return some `Option[_]`).
- The `?` operator cannot be used in `main` (main has no return type to
  propagate into — use `match` or `panic` in main).
- Inside an arm of a `match`, `?` propagates out of the enclosing function,
  not out of the `match` (this matches Rust's semantics).

Example:

```hls
import "std.result"
import "std.option"

fn parse_pos(s: str) -> Result[int, str] {
    # int_parse returns Result[int, str]; ? propagates Err early.
    let n: int = int_parse(s)?
    if n < 0 {
        return Result.Err("negative")
    }
    return Result.Ok(n)
}

fn first(xs: list[int]) -> Option[int] {
    if xs.len() == 0 {
        return Option.None
    }
    return Option.Some(xs.get(0))
}

fn use_first(xs: list[int]) -> Option[int] {
    let v: int = first(xs)?       # returns Option.None if first() returned None
    return Option.Some(v + 1)
}
```

## 13. What v0.5 deliberately does NOT have

| Feature | Stage |
|---------|-------|
| Bitwise operators (`&` `\|` `^` `<<` `>>`) with checked semantics | later |
| Full borrow checking (one mut borrow OR many shared) | 8-beta |
| Capability tokens for `Net`/`Rand`/`Proc` effects (reserved names, no builtins yet) | 9-beta |
| Taint tracking (`tainted[T]`), sandboxed compile mode | 10 |
| SSA IR + optimisation | 11 |
| Direct LLVM backend | 12 |
| Closures, function pointers, async | 16 |
| Catching panics | not planned (panic = bug, by design) |

---

## 14. Complete example program

```hls
# primes.hls — Sieve of Eratosthenes, demonstrating types, loops, lists
fn sieve(n: int) -> list[int] {
    let flags: list[bool] = []
    let i: int = 0
    while i < n {
        flags.push(i >= 2)
        i = i + 1
    }
    let result: list[int] = []
    let p: int = 2
    while p < n {
        if flags.get(p) {
            result.push(p)
            let multiple: int = p * p
            while multiple < n {
                flags.set(multiple, false)
                multiple = multiple + p
            }
        }
        p = p + 1
    }
    return result
}

fn main() -> int uses IO {
    let primes: list[int] = sieve(100)
    let i: int = 0
    while i < primes.len() {
        print(primes.get(i).to_str() + " ")
        i = i + 1
    }
    println("")
    return 0
}
```

## 14b. Stage-7 example — enum + match + `?`

```hls
import "std.result"
import "std.option"

enum Tree {
    Leaf,
    Node(int, Tree, Tree)
}

fn sum(t: Tree) -> int {
    return match t {
        Tree.Leaf => 0,
        Tree.Node(v, l, r) => v + sum(l) + sum(r)
    }
}

fn parse_pair(s: str) -> Result[int, str] {
    let parts: list[str] = s.split(",")
    if parts.len() != 2 {
        return Result.Err("expected two parts")
    }
    let a: int = int_parse(parts.get(0))?
    let b: int = int_parse(parts.get(1))?
    return Result.Ok(a + b)
}

fn main() -> int uses IO {
    let t: Tree = Tree.Node(1, Tree.Node(2, Tree.Leaf, Tree.Leaf), Tree.Leaf)
    println("sum = " + sum(t).to_str())        # sum = 3
    let r: Result[int, str] = parse_pair("3,4")
    return match r {
        Result.Ok(v) => v,
        Result.Err(_) => 1
    }
}
```

## 15. Stage-0 vs native semantic compatibility

The two implementations (the reference interpreter `boot/` and the
self-hosted compiler `src/hlc.hls`) must produce **identical output** on the
same program (differential testing — see `tests/run_tests.sh`). The only
allowed difference: panic messages on Stage-0 include the line location, the
native version does not (debug info: Stage 11).

---

## 16. Ownership primitives (Stage 8-alpha — v0.4.0-alpha)

Stage 8 of the roadmap calls for full ownership & borrow checking (memory
safety without GC, ending the arena model). That stage is the highest-risk
item in the entire roadmap. To reduce risk and ship value early, v0.4.0-alpha
introduces **the first subset of Stage 8: three ownership primitives that
give the compiler a static "moved" tracking pass**. The runtime still uses
arena allocation; runtime memory reclamation is deferred to Stage 8-beta.

### 16.1. The three primitives

| Primitive | Type | Behaviour |
|-----------|------|----------|
| `drop(x: T) -> void` | builtin | Marks binding `x` as **moved**. Subsequent use of `x` is a compile error. Runtime: no-op (arena mode). |
| `clone(x: T) -> T` | builtin | Returns an **independent deep copy** of `x`. `x` is NOT moved. |
| `take(x: T) -> T` | builtin | Returns `x`'s value and marks binding `x` as **moved**. The value is now "owned" by the expression context. |

### 16.2. Use-after-move is a compile error

Once a binding is moved (via `drop(x)` or `take(x)`), any subsequent
reference to `x` produces a compile-time error:

```hls
fn main() -> int uses IO {
    let s: str = "hello"
    drop(s)
    println(s)            # compile error: use of moved value: s
    return 0
}
```

The error is raised at the use site, with the offending variable name. The
underlying value is still in memory (the runtime is arena-based), but the
compiler refuses to let you reference it.

### 16.3. Revival via reassignment

A moved `let mut` binding can be **revived** by reassignment:

```hls
fn main() -> int uses IO {
    let mut s: str = "first"
    drop(s)               # s is now moved
    s = "second"          # s is revived — fresh ownership
    println(s)            # OK: prints "second"
    return 0
}
```

Field/index assignment on a moved binding is **not** revived — only whole-
binding assignment (`x = ...`) revives.

### 16.4. `clone()` — independent deep copy

`clone(x)` returns an independent copy: mutating the clone does not affect
the original. This is the primary tool for code that needs to share data
without giving up ownership.

```hls
fn main() -> int uses IO {
    let xs: list[int] = [1, 2, 3]
    let ys: list[int] = clone(xs)
    ys.push(4)                      # only ys changes
    println(xs.len().to_str())      # 3
    println(ys.len().to_str())      # 4
    return 0
}
```

### 16.5. `take()` — explicit ownership transfer

`take(x)` is for transferring ownership out of a binding when you no longer
need it locally. Common use: passing a value to a consuming function without
paying for a `clone`.

```hls
fn consume(s: str) -> int {
    return s.len()
}

fn main() -> int uses IO {
    let s1: str = "hello hieu"
    let n: int = consume(take(s1))   # s1 is now moved
    println("consumed=" + n.to_str())
    # println(s1)                    # would be a compile error: use of moved value
    return 0
}
```

### 16.6. Scope-local moves

A move done inside an `if`/`while`/`for` body does **not** leak out of the
body. The compiler takes a snapshot of the moved-status on entry to a child
scope and restores it on exit. This means:

```hls
fn main() -> int uses IO {
    let s: str = "hello"
    if true {
        drop(s)              # s is moved inside this block
    }
    println(s)               # OK — s is usable again outside the if
    return 0
}
```

The rationale: the `if` body may not execute at all, so post-`if` code must
remain valid for every path. The conservative model "moves don't escape
child scopes" matches this requirement.

### 16.7. Limitations in v0.4.0-alpha (Stage 8-beta targets)

| Limitation | Stage 8-beta target |
|------------|---------------------|
| `clone()` not yet supported on `struct`/`enum` (or `list[struct]`/`map[str, struct]`) | per-instantiation clone helpers generated at codegen time |
| `drop(x)` is a runtime no-op (arena model still in use) | refcounted or borrow-checked runtime that actually reclaims memory |
| No full borrow checker (multiple shared borrows are still allowed) | one mut OR many shared — see ROADMAP Stage 8 |
| No lifetime annotations or inference | "minimal lifetimes: infer everything, only report errors when inference fails" (per ROADMAP) |

These limitations are deliberate and documented. They will be lifted in
subsequent alpha/beta releases as the Stage 8 work proceeds.

---

## 17. Fine-grained effects & capabilities (Stage 9-alpha — v0.5.0-alpha)

Stage 9 of the roadmap calls for splitting the single `IO` effect into
fine-grained, individually-declared capabilities, plus capability tokens
that flow from `main` down through the call graph. v0.5.0-alpha ships the
**first subset of Stage 9**: the effect taxonomy is split, the fixpoint
analysis tracks effect SETS, and a function's declared effects are its
static capabilities (the "token" model).

### 17.1. The five active effects

| Effect | Builtins it gates | Capability scope |
|--------|-------------------|------------------|
| `IO` | `print`, `println` | console output |
| `Fs` | `read_file`, `write_file`, `file_exists` | filesystem access |
| `Clock` | `clock_ms` | monotonic clock read |
| `Args` | `args` | command-line arguments |
| `Exit` | `exit` | process termination |

Reserved (recognized but error if used — no builtins yet): `Net`, `Rand`,
`Proc`. These will be enabled in later stages.

### 17.2. The `uses` clause — declared capabilities

Grammar (Stage 9-alpha):
```
fndef := "fn" ... ("uses" effect ("," effect)*)? block
effect := "IO" | "Fs" | "Clock" | "Args" | "Exit"
        | "Net" | "Rand" | "Proc"   # reserved, error if used
```

- `uses IO` — **blanket alias**: at parse time, expands to the entire IO
  family `{IO, Fs, Clock, Args, Exit}`. Backwards compatible with all
  v0.3/v0.4 code.
- `uses Fs` — only filesystem capability.
- `uses Fs, Clock` — filesystem and clock.
- `uses Net` — parse error: "effect 'Net' is reserved for a future stage".
- `uses Bogus` — parse error: "unknown effect 'Bogus'; known effects: IO,
  Fs, Clock, Args, Exit".
- No `uses` clause — empty declared set (default-deny: pure function).

### 17.3. Capability semantics — declared ⊇ computed

A function's declared effects ARE its capabilities. The compiler computes,
for each function, the SET of effects its body transitively requires (the
union of its builtins' effects and its callees' computed effect sets,
iterated to a fixpoint). The capability check is a subset test:

```
declared_effects(function) ⊇ computed_effects(function)
```

If `computed - declared` is non-empty, the compiler reports the missing
effect, the violating callee/builtin, and the function name.

### 17.4. Default-deny — `main` is the root capability holder

A function with no `uses` clause has an empty declared set. It cannot call
any builtin with an effect, and cannot transitively call any function
whose computed effect set is non-empty. This is **default-deny**: purity
is the default, capabilities must be explicitly requested.

`main` is the root capability holder — it can declare any effect. Library
functions can be more restrictive: a library that only needs to read files
can declare `uses Fs` and be statically guaranteed to never touch the
clock, never read command-line args, never print to the console.

### 17.5. Example — transitive capability propagation

```hls
# Library function: filesystem capability only.
fn load_config(path: str) -> str uses Fs {
    return read_file(path)
}

# Library function: filesystem + clock capabilities.
fn save_with_ts(path: str, content: str) -> void uses Fs, Clock {
    let t: int = clock_ms()
    write_file(path, content + " @ " + t.to_str())
}

# Application code: IO blanket grants all five effects.
fn main() -> int uses IO {
    let cfg: str = load_config("/etc/app.conf")
    save_with_ts("/var/log/app.log", "started")
    println("loaded " + cfg.len().to_str() + " bytes")
    return 0
}
```

If `load_config` accidentally called `println`, the compiler would report:

```
function 'load_config' calls 'println' which requires effect 'IO'
not declared (declared: Fs; missing: IO)
```

### 17.6. Implementation notes

- The fixpoint is monotone and bounded (5-element effect universe). The
  iteration order is deterministic in both Stage-0 (Python `dict` order)
  and the self-hosted compiler (`ctx.fn_order` list), so the bootstrap
  fixed-point test (Stage 5) is preserved.
- The C codegen is unaffected — `uses` clauses are pure compile-time
  annotations; no runtime effect tracking is emitted.
- Existing v0.3/v0.4 code with `uses IO` continues to compile unchanged
  (the parse-time expansion to the IO family is transparent).

### 17.7. Limitations in v0.5.0-alpha (Stage 9-beta targets)

| Limitation | Stage 9-beta target |
|------------|---------------------|
| `Net`, `Rand`, `Proc` reserved but no builtins yet | add network/random/process builtins + their effect mappings |
| Capability tokens are not first-class values (can't be passed as args, stored in structs) | first-class capability type, e.g. `cap[Net]` |
| No `hlc --audit` flag to print the full capability tree | add an audit subcommand that prints per-function declared/computed effects |
| No pure-function annotation (purity is implicit via no `uses`) | explicit `pure` keyword for documentation / linting |
| Per-builtin effect taxonomy is fixed (no user-defined effects) | user-defined effects via a registration mechanism (later stage) |

These limitations are deliberate and documented. They will be lifted in
subsequent alpha/beta releases as the Stage 9 work proceeds.
