# Hieu Louis language specification (HLS) — v0.13.0-alpha

> **Hieu Louis** is a high-security, native-compiled programming language
> designed around the philosophy: **safety by default, explicitness for
> auditability, performance via AOT compilation**. Version v0.13.0-alpha
> adds **Stage 15-alpha: Safe C FFI** — a new `extern "C" { ... }` block
> declares external C functions, with the interpreter dispatching via
> ctypes. Version v0.12.0-alpha added **Stage 14-alpha: developer
> tooling** (hls-lsp language server, hlfmt formatter, hllint linter).
> Version v0.11.0-alpha added **Stage 13-alpha: the hls-pkg package
> manager** with content-addressed dependencies and effect enforcement.
> Version v0.10.0-alpha added **Stage 12-alpha: the LLVM IR text backend**.
> Version v0.9.0-alpha added **Stage 11-alpha: the HLIR (SSA-style IR)
> and optimiser pipeline** (constant folding, copy propagation, DCE).
> Version v0.8.0-alpha extends the Stage 10 taint tracking model with a
> second taint source (`read_file_tainted`), extended `--audit` taint-flow
> reporting, and new pure-query helpers on `tainted[str]`
> (taint_check_byte_at, taint_concat, taint_concat_clean). Version
> v0.7.0-alpha introduces the **Stage 10-alpha taint tracking** model on
> top of the Stage 9 fine-grained effects & capabilities system: a new
> built-in generic type `tainted[T]` lets the compiler statically reject
> passing tainted values to sinks (print, file I/O, exit) — the user must
> sanitise first via `std.sanitize`. Version v0.5.0-alpha introduced the
> **fine-grained effects & capabilities** model: a single `IO` effect
> split into five capabilities (`IO`, `Fs`, `Clock`, `Args`, `Exit`)
> individually declared and statically verified through the call graph.
> `uses IO` remains as a backwards-compatible blanket alias for the
> entire IO family. Version v0.6.0-alpha added the explicit `pure`
> keyword and the `--audit` flag (Stage 9-beta). Every operation is still
> checked, every effect is statically tracked, no null, no undefined
> behaviour, and use-after-move is a compile error (Stage 8-alpha).

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
- Cannot clash with keywords. Keywords (20): `fn let mut return if else while
  for in break continue struct impl import uses true false enum match pure`.
  (`pure` was added in Stage 9-beta / v0.6.0-alpha.)

### 2.5. Reserved keywords (unused, error if encountered): `secure`, `trait`,
`tainted` (NOTE: `tainted` is not a keyword — it is a built-in generic type
name recognised by the parser, like `list` and `map`. You can still use
`tainted` as an identifier for variables/functions; only in type position
does it denote the taint wrapper.)

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
->  ==  !=  <=  >=  <  >  =  +  -  *  /  %  !  &&  ||  ?  =>
(  )  {  }  [  ]  ,  :  . 
```
`=>` is the match-arm operator (see §5 grammar). Lone `&` and `|` are
lexical errors. No bitwise operators in v0.3 (each bitwise operator
will be added with its own checked semantics — later stage).
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
fndef          := "fn" Ident typeparams? "(" params? ")" ("->" type)? ("pure" | "uses" efflist)? block
efflist        := effect ("," effect)*
effect         := "IO" | "Fs" | "Clock" | "Args" | "Exit"
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
| `tainted_args()` | `list[tainted[str]]` | Args | (Stage 10-alpha) argv, wrapped as tainted |
| `taint_mark(x: T)` | `tainted[T]` | — | (Stage 10-alpha) wrap any value as tainted |
| `taint_unwrap(x: tainted[T])` | `T` | — | (Stage 10-alpha) explicit untaint (escape hatch) |
| `read_file_tainted(path: str)` | `tainted[str]` | Fs | (Stage 10-beta) read file; result is tainted |

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
- **Method calls are call-graph edges** (v0.16.0-alpha): `x.method()` counts
  exactly like a plain call for effect propagation — the method's computed
  effects flow into the caller's set. Struct field DEFAULT expressions are
  evaluated at each construction (in the calling context), so they form a
  synthetic `@default.<Struct>` node: every function that constructs the
  struct inherits the defaults' effects.
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

## 16. Ownership & memory model (Stage 8 — complete in v0.19.0-alpha)

Stage 8 of the roadmap calls for memory safety without GC, **ending the
arena model**. It shipped in two steps:

- **Stage 8-alpha (v0.4.0-alpha):** the three ownership primitives
  (`drop` / `clone` / `take`) with a static "moved" tracking pass.
- **Stage 8-beta (v0.19.0-alpha):** the **end of the arena** — the
  generated C runtime is now reference-counted, and the codegen performs
  a static **ownership analysis pass** that inserts exact
  retain/release/free at compile time. A memory-stress program now runs
  with a completely flat RSS (verified by `tests/run_tests.sh` section
  3b under a 256 MB address-space limit) and `clone()` supports every
  owned type.

This follows the ROADMAP's explicitly sanctioned downgrade path
("ref-counting + ownership analysis pass"): full borrow-checking syntax
(`&mut`/lifetime annotations) is NOT part of the language; instead the
compiler proves the retention balance statically and the runtime
enforces it with non-atomic reference counts. Observable program
behaviour is unchanged — the aliasing semantics of v0.1–v0.18 are
preserved exactly (assignment still creates a reference, mutation is
visible through all references); what changed is that memory is now
reclaimed deterministically at scope exit.

### 16.1. The three primitives

| Primitive | Type | Behaviour |
|-----------|------|----------|
| `drop(x: T) -> void` | builtin | Marks binding `x` as **moved**. Subsequent use of `x` is a compile error. Runtime: releases `x`'s retain immediately and nulls the binding (the scope-exit cleanup becomes a no-op). |
| `clone(x: T) -> T` | builtin | Returns an **independent deep copy** of `x` (works for **every** owned type: `str`, `list`, `map`, `struct`, `enum`, `tainted[...]`). `x` is NOT moved. |
| `take(x: T) -> T` | builtin | Returns `x`'s value and marks binding `x` as **moved** — the binding's retain transfers to the consumer. |

**Stage 8-beta restriction:** `take()`/`drop()` are rejected inside a
`while` condition or a `for` iterable — the header re-evaluates on every
iteration, so a move would hand NULL to the callee from the second
iteration on. Both compilers enforce this with the error
`take() cannot be used inside a loop condition or iterable (the binding
would be moved on every iteration)`.

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

### 16.7. The memory model (Stage 8-beta — end of the arena)

Every heap value (string, list, map, struct, enum instance) begins with an
`int64_t refcnt` field. The codegen's ownership analysis pass classifies
every expression as **fresh** (carries one unowned retain — literals,
concatenations, `clone`, call results, `pop`, `keys`, list/struct/enum
literals) or **borrowed** (points at a retain owned elsewhere — idents,
field/index access). The discipline:

- **Bindings own one retain**, released by a C cleanup attribute at
  block exit — this covers `break`/`continue`/`return` automatically
  because the C compiler itself runs the cleanups on every control-flow
  path.
- **Function parameters own one retain of each argument** — call sites
  pass fresh values raw and wrap borrowed values in `hl_retain(...)`.
- **Containers own their elements**: `push`/`set`/`map.set` and struct
  constructors store own-wrapped values; element destructors are
  function pointers (`free` for primitive boxes, typed releases for
  pointers) supplied at container creation.
- **`return` of a borrowed value** adds one retain for the caller;
  returning a fresh value transfers it. `return take(x)` nulls the
  binding before the jump so the transfer is exact.
- **Fresh values consumed in borrowed positions** (e.g. the left operand
  of a `+` concat) are hoisted into temporaries with cleanups, so
  nothing leaks even in expression trees.
- `print`/`println`/`panic`/`read_file`/`write_file`/`file_exists`
  **consume** their argument (release after use).

Primitive values (int/float/bool) are never boxed outside containers and
carry no refcount; container boxes are single-owner allocations freed by
the container. The `?` operator retains the payload on success and
retains the error value on the early-return path, so `Result` chains are
leak-free. `match` arm bodies are own-wrapped so the match always yields
an owned value regardless of which arm fired.

Known (documented) limitations of the refcount model:

| Limitation | Explanation |
|------------|-------------|
| Cycles leak | A struct whose field references (a copy of) itself keeps the last retain alive — same trade-off as Swift's non-ARC-optional mode. Cycles are rare because HLS has no references, only values. |
| Deep struct chains recurse on release | Releasing a 1M-node linked struct recurses (stack depth = chain length). Lists/maps/strings release iteratively; the compiler itself (the largest HLS program) uses index pools, not pointer chains. |
| Non-atomic refcounts | Single-threaded by design; Stage 16 (concurrency) will revisit. |
| `exit()`/`panic()` skip cleanups | The process is terminating; reachable-at-exit blocks are bounded by live bindings. |

### 16.8. `clone()` on every owned type (Stage 8-beta)

`clone()` is now supported for `str`, `list[...]`, `map[str, ...]`,
`struct`, `enum`, and `tainted[...]` (which clones as its inner type —
taint is a compile-time property). The native compiler generates one
`hl_clone_<mangled-type>` helper per instantiation, recursively cloning
pointer children; the interpreter uses `deep_clone`. Mutating a clone
never affects the original:

```hls
let a: Outer = Outer { name: "original", inner: Inner { label: "in", nums: [1, 2, 3] } }
let b: Outer = clone(a)
b.inner.nums.push(99)              # only b changes
```

The v0.4.0-alpha limitation table is now resolved in full: clone covers
all owned types, `drop` reclaims at runtime, and the exact-free
requirement is enforced by `tests/run_tests.sh` section 3b.

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

### 17.7. Limitations after Stage 9-beta (v0.6.0-alpha)

| Limitation | When lifted |
|------------|--------------|
| `Net`, `Rand`, `Proc` reserved but no builtins yet | future Stage 9 release |
| Capability tokens are not first-class values (can't be passed as args, stored in structs) | future Stage 9 release (first-class capability type) |
| Per-builtin effect taxonomy is fixed (no user-defined effects) | future Stage 9 release (user-defined effects via a registration mechanism) |

The two original Stage 9-beta targets — `hlc --audit` flag and the
explicit `pure` keyword — **shipped in v0.6.0-alpha**. See §18 below.

---

## 18. Explicit purity (`pure` keyword) & audit mode (Stage 9-beta — v0.6.0-alpha)

Stage 9-beta shipped two features on top of the Stage 9-alpha effects
system:

### 18.1. The `pure` keyword

A function declared `fn f(...) pure` is **explicitly pure**:
- It MUST have no `uses` clause. `pure` and `uses` are mutually
  exclusive at parse time (the parser rejects `fn f(...) pure uses IO`
  with a clear error).
- The checker verifies that its computed effect set is empty. If any
  transitive callee is effectful, the checker reports the violating
  edge with a witness, e.g.:
  ```
  function 'helper' is declared 'pure' but transitively uses effects
  IO (declared pure but callee chain is not pure)
  ```

Purity was previously implicit (a function with no `uses` is pure);
`pure` makes it explicit and self-documenting. The `is_pure` field is
stored on `FnInfo` in `hlc.hls` (renamed from `pure` because `pure` is
now a keyword and cannot be a struct literal field name).

### 18.2. The `--audit` flag

`hlc --audit <file.hls>` and `boot.py --audit <file.hls>` print the
full capability / effect tree of every function in the program:

- Declared effects (or `pure`) per function.
- Computed effects (the fixpoint result) per function.
- An OK/VIOLATION status per function.
- A summary count (how many functions declared pure / with effects).
- The active vs reserved effects table.
- The `uses IO` blanket-alias expansion reminder.

Useful for security review and supply-chain audits.

### 18.3. Reserved-effect reporting

`--audit` also surfaces the reserved-effect table (`Net`, `Rand`,
`Proc`): they are recognized by the parser but error if used in a
`uses` clause. They will be enabled in a later stage, but until then
the compiler rejects any program that tries to use them.

---

## 19. Taint tracking (Stage 10-alpha v0.7.0-alpha + Stage 10-beta v0.8.0-alpha)

Stage 10 ships a **static taint tracking** system that prevents
input-driven vulnerabilities (injection, XSS, path traversal) at the
type level. Stage 10-alpha (v0.7.0-alpha) introduced the `tainted[T]`
type and three taint builtins. Stage 10-beta (v0.8.0-alpha) extends the
model with a second taint source (`read_file_tainted`), extended
`--audit` taint-flow reporting, and new pure-query helpers in
`std.taint` (`taint_check_byte_at`, `taint_concat`, `taint_concat_clean`).

### 19.1. The `tainted[T]` type

`tainted[T]` is a built-in generic type (alongside `list[T]`,
`map[str, T]`). At the C-runtime level, `tainted[T]` is represented the
same as `T` — the taint is a **compile-time property only** in the
native backend. The Stage-0 interpreter uses a runtime wrapper dict
`{"tainted": True, "value": <T>}` to provide defence-in-depth (so a
checker bug doesn't silently let tainted data reach a sink in
interpreted mode). Runtime taint enforcement in the native backend
(defence-in-depth) is deferred to a later Stage 10 sub-release.

The checker rejects passing a `tainted[T]` value to any of these sinks:
`print`, `println`, `read_file`, `read_file_tainted`, `write_file` (both
the path argument and the content argument), `file_exists`, `exit`.

### 19.2. Taint builtins

Four taint builtins (all pure except `tainted_args` and `read_file_tainted`):

| Builtin | Effect | Type | Stage |
|---------|--------|------|-------|
| `tainted_args()` | `Args` | `list[tainted[str]]` | 10-alpha |
| `read_file_tainted(path)` | `Fs` | `str -> tainted[str]` | 10-beta |
| `taint_mark(x)` | (none) | `T -> tainted[T]` | 10-alpha |
| `taint_unwrap(x)` | (none) | `tainted[T] -> T` | 10-alpha |

`tainted_args()` is the **taint source** for command-line inputs —
every program's argv is tainted by default. `read_file_tainted(path)`
is the **second taint source** for file contents — useful when the
file is untrusted (e.g. user uploads, downloaded config). `taint_unwrap`
is the explicit "I accept the risk" escape hatch; the user should
normally use a sanitizer instead.

### 19.3. Sanitisers (`std.sanitize`)

The standard library provides six sanitizers in `std/sanitize.hls`.
Each takes a `tainted[str]` and returns a clean `str`:

| Sanitizer | Behaviour |
|-----------|-----------|
| `sanitize_html(t)` | escapes `< > & " ' /` for safe HTML body rendering |
| `sanitize_html_attr(t)` | escapes for an HTML attribute value |
| `sanitize_path(t)` | rejects empty / NUL / absolute / `..` segments |
| `sanitize_sql_identifier(t)` | only `[A-Za-z_][A-Za-z0-9_]*`; panic otherwise |
| `sanitize_sql_string(t)` | doubles `'` and `\` for SQL string literals |
| `sanitize_command(t)` | rejects 23 shell metacharacters (whitespace, `; | & \` $ ( ) < > ! \ " ' * ? [ ] { }`) |
| `sanitize_filename(t)` | only `[A-Za-z0-9._-]+`, no leading dot |

### 19.4. Pure queries on tainted values (`std.taint`)

`std/taint.hls` provides pure-query helpers on `tainted[str]` that DO NOT
expose the inner string to the caller — useful for routing on argv
without exposing the inner string to a sink:

- `taint_check_len(t) -> int` (Stage 10-alpha)
- `taint_check_is_empty(t) -> bool` (Stage 10-alpha)
- `taint_check_starts_with(t, prefix) -> bool` (Stage 10-alpha)
- `taint_check_ends_with(t, suffix) -> bool` (Stage 10-alpha)
- `taint_check_equals(t, literal) -> bool` (Stage 10-alpha)
- `taint_check_contains(t, sub) -> bool` (Stage 10-alpha)
- `taint_slice(t, start, end) -> tainted[str]` — the slice result
  REMAINS tainted (a slice of attacker-controlled bytes is still
  attacker-controlled). (Stage 10-alpha)
- `taint_check_byte_at(t, i) -> int` — pure byte-at-index query.
  Returns an int (not a taint vector for any sink). (Stage 10-beta)
- `taint_concat(t1, t2) -> tainted[str]` — concatenate two tainted
  strings; result REMAINS tainted. (Stage 10-beta)
- `taint_concat_clean(t, clean) -> tainted[str]` — concatenate a
  tainted string with a clean literal; result REMAINS tainted. (Stage
  10-beta)

### 19.5. Taint-flow audit (Stage 10-beta — v0.8.0-alpha)

The `--audit` flag now reports the taint flow of the program in addition
to the per-function effect tree (Stage 9-beta). Specifically, it lists:

- Functions calling each taint source: `tainted_args`, `read_file_tainted`.
- (Planned: functions calling each taint sink: `print`, `println`,
  `read_file`, `write_file`, `file_exists`, `exit`.)
- (Planned: functions calling the explicit untaint: `taint_unwrap`,
  each `std.sanitize.*` helper.)

This is useful for security review and supply-chain audits — at a
glance the auditor can see which functions handle attacker-controlled
input and which functions reach sinks.

### 19.6. What Stage 10 still does NOT do

- Sandboxed compile mode (a program only running inside a granted
  directory / socket set) — later Stage 10 sub-release.
- First-class taint labels (e.g. `tainted[str, Html]` vs
  `tainted[str, Sql]` so HTML-tainted values cannot be used in SQL
  even after `sanitize_html`) — future Stage 10 work.
- Runtime taint flag in the native backend (defence-in-depth) — future
  Stage 10 work. The Stage-0 interpreter already has this via its
  wrapper dict.
- Taint sources beyond argv and file content (e.g. `read_line` if added,
  HTTP request body if added) — currently only argv and read_file_tainted
  are taint sources.

## 20. HLIR — Hieu Louis Intermediate Representation (Stage 11-alpha — v0.9.0-alpha)

The mid-level IR is built from the AST (post-type-check) and fed to an
optimiser pipeline. It is a *light* SSA form: HLS already disallows
shadowing and uninitialised variables, so every binding has exactly one
definition point at the source level — the IR inherits "implicit SSA"
for free.

### 20.1. IR structure

- `Instr` — a single instruction with `dest` (SSA name or None), `op`,
  `args` (operands), `line`, `attrs`.
- `Block` — a linear sequence of `Instr`s ending in a terminator
  (branch / jump / return / panic).
- `HLIRFunction` — params, return type, effects, blocks.
- `HLIRModule` — functions.

### 20.2. Op codes

`const`, `binop`, `unop`, `call`, `method`, `builtin`, `load`, `store`,
`list_new`, `list_get`, `list_set`, `list_len`, `map_new`, `map_get`,
`map_set`, `struct_new`, `struct_get`, `struct_set`, `branch`, `jump`,
`return`, `panic`, `match`, `qmark`.

### 20.3. Optimiser pipeline

1. `constant_fold` — fold literal arithmetic and string concatenation.
   Tracks constants propagated through `OP_LOAD` (the IR's `let` lowering).
   Respects `OP_STORE` mutations.
2. `copy_propagate` — replace `%t1 = %t0` uses with `%t0`.
3. `dead_code_elim` — remove instructions whose result is never used and
   that have no side effects.

### 20.4. `-O fast` mode

Annotates provably-safe binops (e.g. `a + 0`, `a * 0`) with
`attrs["safe_overflow"] = True` so the codegen can skip the C-level
overflow check. Today the codegen ignores this annotation; consuming
it is the Stage 11 release target.

### 20.5. CLI flags

- `boot.py --emit ir FILE.hls` — print the HLIR of every function.
- `boot.py --opt-stats FILE.hls` — run the optimiser, print per-pass
  statistics (instructions before / after / removed, per function and
  total).

## 21. LLVM IR text backend (Stage 12-alpha — v0.10.0-alpha)

A separate backend that emits LLVM IR text (`.ll`) from a checked HLS
program. The IR can be assembled by `llc` or `clang` (when available)
into a native binary. The C backend remains the primary codegen path;
the LLVM backend is a parallel infrastructure.

### 21.1. Type mapping

| HLS type | LLVM type |
|----------|-----------|
| `int` | `i64` |
| `float` | `double` |
| `bool` | `i1` |
| `str` | `ptr` (pointer to `%hl_str`) |
| `void` | `void` |
| `list[T]`, `map[str,T]`, `struct`, `enum`, `tainted[T]` | `ptr` (opaque) |

### 21.2. Arithmetic

Integer arithmetic uses `llvm.sadd/ssub/smul.with.overflow.i64` with
explicit overflow-path branches to `hl_die`. Division by zero is checked
before `sdiv`/`srem`. Float arithmetic uses `fadd`/`fsub`/`fmul`/`fdiv`/
`frem` (no overflow check needed).

### 21.3. CLI flags

- `boot.py --emit llvm FILE.hls` — print the LLVM IR of the program.
- `--target TRIPLE` — set the LLVM target triple (e.g. `aarch64-linux`).

### 21.4. Limitations (Stage 12 release targets)

- Full method dispatch (today method calls are emitted as opaque calls
  to `hl_method_<name>`).
- Full struct/enum/list/map lowering with typed field access.
- Match expression lowering (today `match` falls through to a runtime
  dispatch).
- Stack probes for deep recursion.
- PGO (profile-guided optimisation).
- Verify the IR text assembles correctly via `llc`/`clang`.
- Thrice-clean bootstrap: HLS→LLVM→native→self-compile.

## 22. Package manager `hls-pkg` (Stage 13-alpha — v0.11.0-alpha)

A content-addressed package manager CLI with the full manifest →
lockfile → audit → build cycle. Dependencies are verified by SHA-256 of
resolved file content, and the package's declared `effects.allowed`
surface is enforced: if any dependency's computed effects are not in the
allowed set, the lock fails.

### 22.1. Commands

- `hls-pkg init NAME` — create a new package skeleton.
- `hls-pkg add NAME GIT PATH [--tag T | --branch B]` — add a git dep.
- `hls-pkg lock` — resolve deps, compute SHA-256, write `hls-pkg.lock`.
- `hls-pkg audit` — print the total effect report of the dep tree.
- `hls-pkg verify` — verify lockfile SHA-256 hashes still match.
- `hls-pkg build [--entry main.hls]` — compile the package.

### 22.2. Manifest format (`hls-pkg.toml`)

```toml
[package]
name = "mylib"
version = "0.1.0"

[dependencies]
std.str = { git = "https://github.com/.../hieu-louis-lang.git", path = "std/str.hls" }

[effects]
allowed = []   # empty = pure library
```

### 22.3. Lockfile format (`hls-pkg.lock`, JSON)

Records per-package: `name`, `source`, `sha256`, `effects`,
`transitive_effects`, `resolved_path`.

## 23. Developer tooling (Stage 14-alpha — v0.12.0-alpha)

Three tools provide the core developer experience:

### 23.1. `hlfmt` — opinionated formatter

- 4-space indentation; no tabs.
- One statement per line.
- Single space after commas, colons, around binary operators.
- **Idempotent: running twice = running once.**
- Subcommands: `hlfmt FILE` (print), `hlfmt -w FILE` (write),
  `hlfmt -c FILE` (check), `hlfmt -d FILE` (diff).
- Limitation: strips `#` comments (the HLS lexer treats them as
  whitespace). Comment preservation is a Stage 14 release target.

### 23.2. `hllint` — safety rules linter

10 rules: `L001` unused-binding, `L002` unused-function,
`L003` unused-struct-field, `L004` ignored-result,
`L005` explicit-unwrap, `L006` unnecessary-effects,
`L007` dead-code-after-return, `L008` long-function,
`L009` shadowing, `L010` empty-impl.

Subcommands: `hllint FILE`, `hllint --strict FILE`,
`hllint --rule L001 FILE`, `hllint --list`.

### 23.3. `hls-lsp` — language server

Minimal LSP server over JSON-RPC stdio:
- `initialize` / `shutdown` / `exit`.
- `textDocument/didOpen` / `didChange` / `didClose`.
- `textDocument/hover` — show the inferred type of an identifier.
- `textDocument/definition` — find the function/struct/enum definition.
- `textDocument/completion` — keyword + identifier completion.
- `textDocument/publishDiagnostics` — runs the checker, publishes errors.
- `--check FILE` one-shot mode prints diagnostics to stdout.

## 24. Safe C FFI (Stage 15-alpha — v0.13.0-alpha)

A new `extern "C" { ... }` block declares external C functions. The
checker enforces that every extern fn declares `uses IO` (or `pure`) —
the safe default for FFI is to assume side effects. The interpreter
calls the C function via ctypes.

### 24.1. Syntax

```hls
extern "C" {
    fn abs(n: int) -> int pure
    fn strlen(s: str) -> int uses IO
}
```

### 24.2. Type mapping (interpreter)

| HLS type | ctypes |
|----------|--------|
| `int` | `c_int64` |
| `float` | `c_double` |
| `bool` | `c_bool` |
| `str` | `c_char_p` (null-terminated; caller must ensure no embedded NULs) |
| `void` | no return |
| other | `c_void_p` (opaque pointer) |

### 24.3. `hlbindgen` — C header → HLS extern generator

Parses simple C function declarations (`int foo(char* s, long n);`),
maps C types to HLS types, emits an `extern "C" { ... }` block with
`uses IO` on every function (safe default).

### 24.4. Limitations (Stage 15 release targets)

- `extern` is not yet supported in the self-hosted `hlc.hls` (only
  `boot/` supports it).
- C codegen for extern fns (forward declarations) is not implemented.
- Ownership rules across the FFI boundary are not enforced.
- `bindgen` improvements: struct/enum generation, macro expansion,
  `#include` resolution, `const`/`volatile` qualifiers.
- ABI-compatibility checking header.
- Re-implement `hlbindgen` in HLS itself.


