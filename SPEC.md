# Halis language specification (HLS) — v0.30.0-alpha

> **Halis** is a high-security, native-compiled programming language
> designed around the philosophy: **safety by default, explicitness for
> auditability, performance via AOT compilation**. Version v0.30.0-alpha
> **perfects Stage 17**: the proof-engine soundness overhaul (every
> false-PROVEN hole closed in both engines — see §26.9), native
> `--contracts` ensures checks at every return, the loop-invariant
> engine (two Kleene rounds + widening + post-fixpoint verification),
> and the z3 python fallback. Version v0.29.0-alpha
> **perfects Stage 16**: bounded channels with backpressure
> (`chan_new_bounded` + blocking `send`), the non-blocking
> `try_send`/`recv_or` pair, and a waiter-aware deadlock detector that
> catches cycles the old guard could not (see §25). Version
> v0.28.0-alpha **completes Stage 17**: contracts & formal verification —
> `requires`/`ensures`, the interval proof engine with `-O fast` check
> elision, `hlprove` (proof reports + the z3 SMT bridge), and `hlmodel`
> (exhaustive finite-state model checking) (see §26). Version
> v0.27.0-alpha completed Stage 16: concurrency with data-race freedom
> (see §25). Version v0.20.0-alpha
> completed Stage 9: the `Net`, `Rand`, and `Proc` effects with five
> builtins (`net_lookup`, `rand_int`, `rand_float`, `rand_seed`,
> `proc_exec`) and the deterministic shared 64-bit LCG.
> Version v0.15.0-alpha added **Stage 15-gamma: Safe C FFI** — a new
> `extern "C" { ... }` block declares external C functions, with the
> interpreter dispatching via ctypes. Version v0.12.0-alpha added
> **Stage 14-alpha: developer tooling** (hls-lsp language server, hlfmt
> formatter, hllint linter).
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

### 2.3. Comments & attributes
- `#` to end of line. No block comments in v0.2.
- EXCEPTION (Stage 28+): `#[...]` is an outer attribute list, not a
  comment (see §27–§31). EXCEPTION (Stage 77): `#![...]` is a
  crate-level attribute (see §31). Only the exact `#`+`[` and
  `#`+`!`+`[` trigraphs open attributes — `#! note` is still a
  comment.

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
program        := crate_attr* (structdef | enumdef | impl | fndef | import)*
crate_attr     := "#![" ("freestanding" | "no_std") "]"   # Stage 77/78, see §31–§32
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
import "core.option"          # freestanding-safe module (Stage 78)
```

- Imports are resolved relative to the importing file's directory, except for
  `std.*` modules (resolved to `<repo>/std/`) and `core.*` modules
  (resolved to `<repo>/core/`, Stage 78) which are resolved by the compiler.
- Each `.hls` file is compiled once per program; circular imports are an error.
- Imported top-level declarations (structs, functions, methods) become visible
  in the importing file. Duplicate names across files are an error.
- In `#![freestanding]` / `#![no_std]` crates (Stage 77/78, see
  §31–§32), `import "std.*"` is rejected — only `core.*` and relative
  imports are allowed.

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
| `net_lookup(host: str)` | `str` | Net | (Stage 9 release) DNS lookup; returns first IPv4 as string; tainted host → error |
| `rand_int(max: int)` | `int` | Rand | (Stage 9 release) uniform random int in `[0, max)`; `max <= 0` → panic |
| `rand_float()` | `float` | Rand | (Stage 9 release) uniform random float in `[0.0, 1.0)` |
| `rand_seed(s: int)` | `void` | Rand | (Stage 9 release) seed the PRNG; same seed → same sequence (deterministic, shared with native) |
| `proc_exec(cmd: str)` | `int` | Proc | (Stage 9 release) run shell command via `system()`; returns exit code (0 on success, 1..255 on failure, 128+signum on signal); tainted cmd → error |
| `join(parts: list[str], sep: str)` | `str` | — | (Stage 19, v0.35.0-alpha) O(n) whole-list join: total length computed once, one allocation, one copy per element |
| `has_feature(name: str)` | `bool` | — | (Stage 21, v0.37.0-alpha) compile-time constant folded from `--target-feature` (exact match; requires a string literal) — the `cfg(feature)` dispatch |
| `simd_cpu_supports(name: str)` | `bool` | — | (Stage 21) runtime CPU probe (CPUID on x86; NEON baseline on aarch64) |

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

## 17. Fine-grained effects & capabilities (Stage 9 — v0.20.0-alpha)

Stage 9 of the roadmap called for splitting the single `IO` effect into
fine-grained, individually-declared capabilities, plus capability tokens
that flow from `main` down through the call graph. The Stage 9 release
(v0.20.0-alpha) **completes the effect taxonomy**: the original five IO
family effects plus three new independent effects (`Net`, `Rand`, `Proc`)
are all active with builtins. The fixpoint analysis tracks effect SETS,
and a function's declared effects are its static capabilities.

### 17.1. The eight active effects

| Effect | Builtins it gates | Capability scope |
|--------|-------------------|------------------|
| `IO` | `print`, `println` | console output |
| `Fs` | `read_file`, `write_file`, `file_exists`, `read_file_tainted` | filesystem access |
| `Clock` | `clock_ms` | monotonic clock read |
| `Args` | `args`, `tainted_args` | command-line arguments |
| `Exit` | `exit` | process termination |
| `Net` | `net_lookup` | DNS resolution (network access) |
| `Rand` | `rand_int`, `rand_float`, `rand_seed` | random number generation |
| `Proc` | `proc_exec` | subprocess control via system() |

`Net`, `Rand`, `Proc` are **independent** effects — they are NOT part
of the IO family. A function must declare them explicitly to use the
corresponding builtins; the blanket `uses IO` does NOT cover them. No
reserved effects remain (the reserved set is empty as of v0.20.0-alpha).

**Shared PRNG:** the `Rand` builtins use a 64-bit LCG with the same
Knuth-MMIX constants in both the Stage-0 interpreter and the native C
runtime. This makes the sequence **deterministic across implementations**
— the same seed produces the same sequence of ints and floats in both
backends. Critical for differential testing: tests using `rand_seed` +
`rand_int` / `rand_float` produce identical output in both backends.

### 17.2. The `uses` clause — declared capabilities

Grammar (Stage 9 release):
```
fndef := "fn" ... ("uses" effect ("," effect)*)? block
effect := "IO" | "Fs" | "Clock" | "Args" | "Exit"
        | "Net" | "Rand" | "Proc"
```

- `uses IO` — **blanket alias**: at parse time, expands to the entire IO
  family `{IO, Fs, Clock, Args, Exit}`. Backwards compatible with all
  v0.3/v0.4 code. **Does NOT include Net, Rand, or Proc** — declare
  those explicitly if your function uses net/rng/subprocess builtins.
- `uses Fs` — only filesystem capability.
- `uses Fs, Clock` — filesystem and clock.
- `uses Net` — network capability (net_lookup).
- `uses Rand` — random-number capability (rand_int, rand_float, rand_seed).
- `uses Proc` — subprocess capability (proc_exec).
- `uses IO, Net` — blanket IO family + network.
- `uses Bogus` — parse error: "unknown effect 'Bogus'; known effects:
  IO, Fs, Clock, Args, Exit, Net, Rand, Proc".
- No `uses` clause — empty declared set (default-deny: pure function).

**Taint sinks among the new builtins:** `net_lookup` and `proc_exec` are
taint sinks (passing a tainted host enables DNS rebinding; passing a
tainted command enables shell injection). The checker rejects tainted
values at those argument positions just like it does for `print`,
`write_file`, etc.

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

### 17.7. Status of Stage 9 release (v0.20.0-alpha)

The Stage 9 release lifts the two remaining limitations of Stage 9-beta:

| Original limitation | Status in v0.20.0-alpha |
|---------------------|--------------------------|
| `Net`, `Rand`, `Proc` reserved but no builtins | **DONE** — five new builtins (net_lookup, rand_int, rand_float, rand_seed, proc_exec) activate all three effects |
| Capability tokens are not first-class values (can't be passed as args, stored in structs) | DEFERRED — the capability system continues to use implicit declared effects. First-class capability tokens are deferred to a future stage. |
| Per-builtin effect taxonomy is fixed (no user-defined effects) | DEFERRED — user-defined effects are deferred to a future stage. |

**Acceptance criterion (Stage 9):** A program that doesn't declare `uses
Net` CANNOT call `net_lookup` even through 5 function layers — the
compile error points to the exact call chain. Same for `uses Rand` /
`rand_int` and `uses Proc` / `proc_exec`. This is enforced by the same
fixpoint + subset-test mechanism that backs all five IO-family effects.

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

## 20. HLIR — Halis Intermediate Representation (Stage 11-alpha — v0.9.0-alpha)

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
std.str = { git = "https://github.com/.../halis-lang.git", path = "std/str.hls" }

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



---

## 25. Concurrency & async (Stage 16 — v0.27.0-alpha)

Stage 16 adds multi-core parallelism **with data-race freedom proven by
the type system**. The design principle: *no value may be simultaneously
owned by two threads*. Ownership crosses task boundaries only by
transfer; sharing happens only through channels (the one deliberately
shared, internally synchronized object).

### 25.1. Types & builtins

| Construct | Type | Effect | Meaning |
|-----------|------|--------|---------|
| `chan_new() -> Chan[T]` | builtin | Conc | a fresh, empty channel (contextual typing like `map_new`) |
| `chan_new_bounded(cap: int) -> Chan[T]` | builtin | Conc | a fresh **bounded** channel (v0.29.0-alpha): `send` blocks while `cap` messages are pending (backpressure); a literal `cap < 1` is a compile error, a dynamic one a clean panic |
| `spawn(f, a1..aN) -> Task[R]` | builtin | Conc | start a task running `f(a1..aN)`; `R = f`'s return type (`Task[void]` allowed) |
| `select(list[Chan[T]]) -> int` | builtin | Conc | block until any channel is ready; return its index (list order) |
| `ch.send(v: T) -> void` | method | Conc | enqueue `v` (FIFO; on a bounded channel this BLOCKS while the channel holds `cap` messages) |
| `ch.try_send(v: T) -> bool` | method | Conc | non-blocking enqueue (v0.29.0-alpha): `false` iff a bounded channel is full (the value is NOT enqueued); `true` otherwise |
| `ch.recv() -> T` | method | Conc | block while empty; transfers the message's ownership to the receiver |
| `ch.recv_or(default: T) -> T` | method | Conc | non-blocking recv (v0.29.0-alpha): the pending message if one exists, else `default` (the default never crosses a task boundary) |
| `ch.len() -> int` | method | Conc | pending message count |
| `t.join() -> R` | method | Conc | wait for the task; return its result; **join exactly once** (a second join is a runtime panic) |

`Conc` is a NEW effect, independent of the IO family — a program must
declare `uses Conc` explicitly. Every function without a `uses` clause
stays pure AND **deterministic** (spawn introduces observable scheduling
nondeterminism, which is exactly why it is an effect).

### 25.2. The Send rule set (the Send/Sync equivalent)

A type is **Send** iff its values may cross a task boundary:

- `int`, `float`, `bool`, `str`: Send.
- `Chan[T]`: Send iff T is Send (channels are the sharing primitive).
- **`Task[R]`: NOT Send** — a join handle must stay with its spawner.
- `list[T]` / `map[str, T]` / `tainted[T]`: Send iff the element is.
- struct/enum: Send iff every field/payload type is Send (recursive
  types are handled coinductively).

Passing a non-Send value to `spawn`/`send`/`select` is a compile error.

### 25.3. Data-race freedom (the Stage 16 acceptance criterion)

**A program that tries to share a variable with a task outside a
channel is a COMPILE ERROR.** Concretely, every `spawn(...)` argument
and every `ch.send(...)` value of an owned type must be a *fresh*
expression — a literal, a call result, a composite literal, `clone(x)`
or `take(x)`. A bare variable / field / index read is rejected:

```hls
let s: str = "hello"
spawn(worker, s)        # compile error: cannot share variable 's'
spawn(worker, clone(s)) # OK — the task gets a private deep copy
spawn(worker, take(s))  # OK — ownership transfers; s is moved
```

At runtime the boundary hardens this further: owned values that are not
provably private (`clone(...)` results, str literals) are **deep-copied
at the task/channel boundary**, because a value returned by a user
function may alias a binding still live in the sender's thread (HLS
assignment is reference semantics). The result: no non-atomic refcount
is ever touched by two threads.

The refcount model under concurrency:

| Object | Refcount discipline |
|--------|--------------------|
| `Chan[T]` | **atomic** (channels are shared on purpose — `clone(ch)` shares with +1) |
| `Task[R]` | guarded by the runtime mutex |
| everything else | non-atomic, **single-threaded by construction** (ownership transfer only) |

### 25.4. Runtime semantics

- One global mutex + condition variable guard all channel/task state
  (simple, correct; sharding is future work).
- **Bounded channels (v0.29.0-alpha)**: `chan_new_bounded(cap)` makes
  `send` wait while the channel holds `cap` messages — a dequeue
  broadcasts and wakes the blocked senders, so producers are paced by
  their consumers (backpressure). Unbounded channels never block a
  sender. Every value crossing the boundary still obeys the privacy
  rule of §25.3.
- **Deadlock detection (perfected in v0.29.0-alpha)**: the detector
  fires when every thread that could produce work is blocked (in
  `recv`/`select`/`join` — and now also in a full-channel `send`) AND
  no channel has a progress opportunity: a pending message with a
  receiver waiting on it, or free capacity with a sender waiting on
  it. The waiter counters are what make this sound — a woken-but-not-
  yet-scheduled thread is still counted as blocked (its counter only
  drops after the wait re-acquires the lock), so a naive
  "all blocked = deadlock" test can fire while a consumer's message is
  already pending. The pre-v0.29 guard (`no messages pending
  anywhere`) missed real cycles — e.g. a producer blocked sending to a
  full channel nobody consumes hung forever; the program now halts
  with `panic: deadlock: ...` (exit 101). A thread between two
  operations counts as alive, so the detector cannot fire spuriously.
- **Safe-halt**: a `panic` or `exit()` in ANY task halts the whole
  process (tasks share the process fate).
- `spawn` restrictions (v0.27.0-alpha): the target must be a
  non-generic, non-method, non-extern function. Generic targets: wrap
  them in a non-generic function. (No closures exist in HLS, so `spawn`
  takes a function name plus explicit arguments — there is no implicit
  capture, which is precisely what makes the sharing rule enforceable.)
- Interpreter ↔ native parity: the interpreter uses real Python threads
  with the same global-lock design and the same (waiter-aware)
  deadlock detector, so differential testing holds (all `feat_conc_*`
  tests compare outputs).

### 25.5. Determinism guidance

A channel is FIFO and MPMC. Programs are deterministic when each
channel has a single logical consumer (request/reply pairs, actor
mailboxes, fan-in of results in order). Multiple competing receivers on
one channel introduce message-grab races — legal, but nondeterministic
(and hence un-testable differentially).

### 25.6. Actor model

The idiomatic shared-state pattern: a task + a mailbox channel + an
enum-typed message protocol dispatched with `match` (see
`examples/actor_demo.hls`, `tests/ok/feat_conc_actor.hls`). The actor
owns its state exclusively; the mailbox is the only interface. No
locks exist in the language.

### 25.7. Deliberate scope decisions (v0.29.0-alpha)

| Deferred | Rationale |
|----------|-----------|
| `async`/`await` syntax | without closures, async/await is `spawn`+`join` under another name; the explicit form exists today. Deferred until closures (post-v1.0 discussion). |
| Work-stealing scheduler | `spawn` = one OS thread per task (pthread); scaling is demonstrated by `benchmarks/conc_bench.hls`. A user-level scheduler over the channel primitives is the natural Stage 18+ refinement — the worker-pool shape over a BOUNDED channel (`examples/bounded_chan_demo.hls`) is the idiomatic pattern today. |
| LLVM backend / HLIR | `--emit llvm` / `--emit ir` reject concurrency programs with a clean error (the C backend and interpreter are the Stage 16 deliverables). |
| Spawn of generic fns | wrap in a non-generic fn (clear error message). |

Shipped in v0.29.0-alpha (previously deferred): **bounded channels**
(`chan_new_bounded`, blocking send, backpressure) plus the non-blocking
pair `try_send` / `recv_or`, and the waiter-aware deadlock detector
above. The "capacity limits: future stdlib" note from v0.27.0 is
resolved.

---

## 26. Contracts & formal verification (Stage 17 — v0.28.0-alpha)

"Extremely high security" moves from *claimed* to *proven*: functions
declare preconditions (`requires`) and postconditions (`ensures`); the
compiler checks what it can prove, guards what it elides, and the tooling
(`hlprove`, `hlmodel`) turns the contracts into reports, SMT queries, and
exhaustive model checks.

### 26.1. Syntax

Zero or more `requires` clauses followed by zero or more `ensures`
clauses, after the effects clause, before the body:

```hls
fn div(a: int, b: int) -> int
    requires b != 0
{
    return a / b
}

fn add_pos(a: int, b: int) -> int
    requires a >= 0
    requires b >= 0
    ensures result >= a
{
    return a + b
}
```

- Multiple clauses of one kind are combined with `&&`.
- Contract expressions are **pure** and see **only the parameters**
  (plus `result` — the return value — inside `ensures`). Literals,
  arithmetic, comparisons, `len(x)` / `x.len()` and field reads of
  parameters are allowed; calling functions is a compile error.
- `requires` must be `bool`-typed; `ensures` must be `bool`-typed and
  is rejected on `void` functions.
- Extern (FFI) functions may carry `requires` (it documents the C side's
  precondition and is checked at literal call sites).

### 26.2. Static checking (always on)

1. **Validation** — type + purity + scope (above) at definition time.
2. **Call-site constant evaluation** — when every argument is a
   literal, the `requires` is evaluated at compile time; a provably
   FALSE precondition is a **compile error** at the call site
   (`div(10, 0)` never compiles). Unknown (non-literal) arguments
   defer to runtime.

### 26.3. Runtime checking (`--contracts`)

`boot.py --contracts` (interpreter) asserts `requires` at every
contracted fn entry and `ensures` at every return — violations are
clean panics (exit 101). The native backend emits the same entry
assertions under `--contracts`; under `-O fast` it emits them for the
functions that need them (see 26.4).

### 26.4. The interval proof engine & `-O fast`

For every function with a `requires`, the checker seeds integer
interval facts from the conjuncts and propagates them through the body:

- recognised seeds: `x >= k`, `x <= k`, `x > k`, `x < k`, `k <= x`,
  `k >= x`, `x == k`, `x != 0`, `x < s.len()` (symbolic length bound),
  `s.len() >= k` (a MINIMUM LENGTH fact — `s` provably has at least k
  bytes, which proves indices below k in bounds);
- propagation: `let`/`assign` arithmetic (int only), if/else joins
  (interval union), `for i in range(0, K)` with constant K
  (`i in [0, K-1]`), while widening (conservative);
- soundness: variables assigned inside a loop body are widened to TOP
  for that body (loop-carried facts are not assumed).

A check is annotated **PROVEN** only when the interval arithmetic
discharges it exactly:

| Annotation | Meaning | `-O fast` codegen |
|------------|---------|-------------------|
| `ovf_safe` | `a + b` / `a - b` / `a * b` cannot overflow int64 | raw C operator |
| `div_safe` | `a / b` / `a % b`: divisor excludes 0 (and not the INT64_MIN/-1 corner) | raw C operator |
| `bnd_safe` | `xs[i]` / `s.byte_at(i)` / `s.slice(a,b)` provably in bounds | unchecked accessors |

**Elision soundness rule**: a function whose body contains elided
operations ALWAYS emits its `requires` assertion at entry under `-O
fast` — an elided check is only sound when the precondition that proved
it is enforced. The fast path is therefore *guarded* by the proof
obligations, never unconditionally unchecked. Anything the prover
cannot prove keeps its runtime panic check.

Differential testing enforces semantics preservation: every
`feat_contract_*` / `feat_proof_elide` test compares the `-O fast`
native output against the interpreter, byte for byte.

### 26.5. `hlprove` — proof reports, the z3 bridge, invariant suggestions

```
python3 tools/hlprove.py file.hls [--smt] [--z3] [--suggest-invariants]
```

- **Default**: per-function proof report — the seeded facts and the
  count of overflow / division / bounds checks proven elidable (the
  same annotations the codegen honours under `-O fast`).
- `--smt`: writes one `.smt2` (QF_LIA; string lengths abstracted to
  Int) per contracted function with two queries: `requires`
  satisfiability (unsat => the contract is vacuous) and
  `requires && !ensures` (unsat => the ensures is implied). This is
  the roadmap's "SMT solver z3 via a bridge GENERATED FROM HLS";
  `--z3` runs external z3 on the files when it is on PATH.
- `--suggest-invariants`: for every loop — exact bounds for const
  `for i in range(a, b)` loops, the while condition as a candidate
  invariant, and the mutated-variable set. The automatic inference
  rule set from the roadmap.

### 26.6. `hlmodel` — exhaustive finite-state model checking

```
python3 tools/hlmodel.py file.hls --fn step --invariant is_valid --init State.Start
```

For a transition function `fn step(s: State, e: Event) -> State` over
payload-less enums, `hlmodel` enumerates EVERY (state, event) pair and
EXECUTES the function via the interpreter — the full finite domain, no
abstraction:

- every transition must terminate without panic;
- `requires`/`ensures` (if declared) are evaluated per pair;
- with `--invariant fn` and `--init Variant`: BFS over the reachable
  state graph verifies the predicate on every reachable state and
  reports dead (unreachable) states.

### 26.7. The acceptance example — `examples/hmac_proven.hls`

An HMAC-style envelope (ipad/opad block construction with a modular
mixer) whose hot path is **fully proven**: under `-O fast` every
integer multiply in the mixer and every byte access in the block
processors is proven overflow-free / in-bounds and elided (verified:
the fast binary and the interpreter produce identical output). The
only branches left in the hot path are the precondition assertions
guarding the elided operations — the panic checks themselves are gone,
exactly the acceptance criterion: *a core crypto module fully proven
by HLS contracts, no panic checks needed*.

### 26.8. Deliberate scope decisions (v0.30.0-alpha)

| Deferred | Rationale |
|----------|-----------|
| Full SMT encoding of function bodies | the bridge encodes contract queries (satisfiability / implication), not whole-program semantics; the built-in interval prover handles body-level elision. |
| Loop invariant PRECISION beyond widening | v0.30.0-alpha closed the deferred row's gap: the engine now runs two Kleene rounds + the standard widening operator + a post-fixpoint verification pass (see 26.4) — strictly more precise than blanket TOP and still sound. What remains future work is inferring NON-interval invariants (modular/relational facts). |
| Contracts on generics | contracts are checked per-declaration; instantiation-specific bounds (generic `requires`) are future work. |

Shipped in v0.30.0-alpha (previously deferred): **`ensures` runtime
checks in native** — the native `--contracts` build now asserts the
postcondition at EVERY return with `result` bound to the returned value
(the interpreter already checked both clauses; the native backend
checked `requires` only). A violated postcondition panics identically
in both implementations (same message, exit 101 — differentially
tested).

### 26.9. v0.30.0-alpha — the proof-engine soundness overhaul

Deep code review found the interval engine could annotate checks as
PROVEN when they were NOT — each a memory-safety hole under `-O fast`
(some confirmed to SIGSEGV or wrap natively). All of the following are
fixed in BOTH engines (boot/proof.py and the hlc.hls mirror), with
differential regressions (`tests/ok/feat_proof_sound_*.hls`):

| Hole | Old behaviour | Fix |
|------|---------------|-----|
| TOP "fits" int64 | `x + y` with unbounded `y` was `ovf_safe` (native UB wrap) | `fits` requires both bounds KNOWN |
| while-condition facts | annotated with loop-ENTRY facts (false `bnd_safe` at loop-modified indices) | conditions annotated with the loop INVARIANT |
| `for i in range(a, b)` | seeded `i` in `[0, count-1]` (both bounds wrong when `a != 0`) | seeds `[a, b-1]`; non-const iterables seed TOP |
| `i <= s.len()` | proved `xs[i]` (the strict/non-strict delta was ignored) | only the strict `<` (delta -1) proves an index; `<=` is a slice-end bound only |
| stale `len() >= k` | survived reassignment of the owner (`xs = [1]` kept `len >= 3`) | every binding write invalidates minlen / symbolic-len / nz facts |
| stale loop facts | `for` never widened post-loop; stale `!= 0` kept `div_safe` after `y = 0` | post-loop joins; nz invalidation on write |
| slice `a <= b` | granted when `a`'s upper bound was unknown | `a <= b` is a PROVEN obligation |
| INT64_MIN / -1 | skipped when the dividend's lower bound was unknown | unbounded-below counts as possibly INT64_MIN |
| symbolic len arithmetic | `x < s.len()` then `x + 1` crashed the compiler (tuple + int) | symbolic bounds collapse to numeric TOP in arithmetic |
| native symbolic route | looked up the wrong map key — never elided (engine divergence) | lookups resolve the index VARIABLE |
| multi-pass staleness | a `True` from an intermediate analysis pass survived the final pass | every verdict is reset each pass |
| internal fact keys | a variable literally named `__nz__`/`__minlen__` crashed the engine | NUL-prefixed / `~ml~` keys (cannot be identifiers) |
| `const_eval` division | int `/` evaluated in floating point (false call-site violations) | C-style truncated division/remainder |
| SMT `/` `%` | encoded with SMT-LIB Euclidean semantics (wrong z3 verdicts) | `cdiv`/`cmod` helpers encode the C-truncated semantics |

The loop analysis itself was upgraded to the standard abstract-
interpretation shape: two Kleene rounds, the widening operator
(growth → infinity, which PREServes e.g. `i >= 0` across `i = i + 1`),
and a post-fixpoint verification pass (any variable whose body outcome
escapes the invariant goes TOP — the verification is what makes a
bounded number of rounds sound).

---

## 27. Stack-frame layout control (Stage 28 — v0.45.0-alpha)

Stage 28 introduces three new function attributes for kernel /
bare-metal code that needs precise control over its stack frame:

```hls
#[stack_size(N)]   # assert the fn's frame is <= N bytes (compile error if exceeded)
#[no_red_zone]     # disable the x86-64 red zone (required for interrupt handlers)
#[irq_handler]     # emit an IRET-compatible frame (gcc's __attribute__((interrupt)))
```

### 27.1. Attribute syntax

Attributes use the `#[...]` syntax (modelled on Rust's attributes).
The lexer special-cases `#[` (vs `#` for line comments): when `#`
is followed by `[`, the `#` is emitted as a sym token (followed by
the normal `[` sym); otherwise it remains a line comment as before.

Multiple `#[...]` lists may precede a single `fn` (each accumulates):

```hls
#[no_red_zone]
#[irq_handler, stack_size(256)]
fn handle_irq(frame: IrqFrame) -> void { ... }
```

is equivalent to:

```hls
#[no_red_zone, irq_handler, stack_size(256)]
fn handle_irq(frame: IrqFrame) -> void { ... }
```

Within a single list, items are comma-separated. Each `attr` is one
of:

| Attribute                | Stage | Effect                                              |
|--------------------------|-------|-----------------------------------------------------|
| `#[stack_size(N)]`       | 28    | assert the fn's frame is <= N bytes (compile error) |
| `#[no_red_zone]`        | 28    | disable the x86-64 red zone                          |
| `#[irq_handler]`        | 28    | emit an IRET-compatible frame                        |
| `#[inline(always)]`      | 29    | force inline at every call site                     |
| `#[inline(never)]`       | 29    | forbid inlining at every call site                   |
| `#[hot]`                 | 29    | mark the function hot (overrides PGO)                |
| `#[cold]`                | 29    | mark the function cold (overrides PGO)              |
| `#[tail_call]`           | 31    | assert every recursive call is in verified tail     |
|                          |       | position; the codegen emits a parameter-rebinding   |
|                          |       | goto (constant stack at any recursion depth)        |

Mutual exclusivity (compile error if violated):
- `#[hot]` and `#[cold]` cannot both appear on the same function.
- `#[inline(always)]` and `#[inline(never)]` cannot both appear on
  the same function.
- `#[tail_call]` cannot be combined with `#[irq_handler]` (an
  interrupt frame must return via IRETQ, never jump) or with
  `#[inline(always)]` (a tail-call loop cannot also be inlined at
  every call site). Both conflicts are detected at parse time, in
  either attribute order.

Unknown attribute names raise a clear compile error.

### 27.2. `#[stack_size(N)]` — static frame-size bound

The checker runs a static analysis pass on the function body and
estimates the stack frame size in bytes. The estimate is an UPPER
BOUND: it counts every `let` binding (8 bytes — every HLS type lowers
to a C scalar of 8 bytes: int64_t / double / pointer), every `for`
loop (16 bytes — iter variable + index temp + iterator handle), every
call site (16 bytes — gcc's call-frame overhead: return-address slot
+ caller-saved register spills), plus 32 bytes base overhead (saved
RBP / RBX / alignment). The actual frame is always <= the estimate
because gcc may reuse slots across sibling scopes.

If the estimate exceeds N, the checker raises a compile error:

```
#[stack_size(8)]
fn too_big(x: int) -> int {
    let a: int = x + 1
    let b: int = a + 2
    let c: int = b + 3
    return a + b + c
}
```

```
panic: type error: #[stack_size(8)] violated by function 'too_big':
estimated frame size is 64 bytes (the body declares too many locals or
nests too many call sites for the bound). Reduce locals or raise the
bound. (line 2)
```

This makes `#[stack_size(N)]` a SOUND guarantee: if the estimate <= N
then the emitted assembly's frame is also <= N. The acceptance gate
verifies this by compiling the C source with `-ffreestanding
-mgeneral-regs-only -mno-red-zone` and checking the resulting `.o`
file's stack frame size (a defensive check — the compile error in
the checker is the primary guarantee).

### 27.3. `#[no_red_zone]` — disable the x86-64 red zone

The x86-64 System V ABI reserves a 128-byte "red zone" below RSP that
leaf functions may use without decrementing RSP. The CPU may push an
exception or interrupt frame at any point inside the red zone,
corrupting it — interrupt handlers and signal-handler trampolines
MUST disable the red zone.

The codegen emits `__attribute__((optimize("no-red-zone")))` on the
function signature. gcc accepts this attribute (it emits a `-Wattributes`
warning that the attribute "may be ineffective" — a limitation of
gcc's per-function optimise-attribute machinery; the actual codegen
DOES apply the flag). The acceptance gate compiles with
`-Wno-attributes` to suppress the warning.

When `#[irq_handler]` is also set, the `optimize("no-red-zone")`
attribute is omitted (the interrupt attribute automatically disables
the red zone — IRETQ semantics forbid red-zone use).

### 27.4. `#[irq_handler]` — emit an IRET-compatible frame

The codegen emits `__attribute__((interrupt))` on the function
signature. gcc's x86-64 interrupt attribute makes the function:

1. Save every caller-saved register (RAX, RCX, RDX, RSI, RDI, R8-R11,
   XMM0-15) at function entry.
2. Restore them at function exit.
3. Return via `IRETQ` instead of `RET` (the IRETQ instruction pops
   the saved RIP, CS, RFLAGS, RSP from the stack — the same frame
   the CPU pushed when the interrupt was taken).

The checker validates the function signature:
- The function MUST return `void` (gcc's interrupt attribute
  requires this).
- The function MUST take exactly ONE parameter, and that parameter
  MUST be a pointer-typed HLS value (str / list[T] / map[...] /
  tainted[T]-of-pointer / Chan[T] / Task[T] / any user struct —
  these all lower to C pointers). `int`/`float`/`bool` lower to C
  scalars and are rejected (gcc's interrupt attribute would refuse
  the signature).

```hls
struct IrqFrame {
    vector: int,
    error_code: int,
    rip: int
}

#[no_red_zone, irq_handler, stack_size(256)]
fn handle_irq(frame: IrqFrame) -> void {
    let v: int = frame.vector
    let _ack: int = v  # dead, but proves the frame stays small
}
```

The emitted C signature is:

```c
__attribute__((interrupt)) void usf_handle_irq(IrqFrame* u_frame_p) { ... }
```

### 27.5. Freestanding build environment

The Stage 28 acceptance gate compiles the C source under the
freestanding build environment for kernel code:

```bash
gcc -O2 -Wno-attributes -ffreestanding -mgeneral-regs-only \
    -mno-red-zone -fno-stack-protector -fno-pic -c \
    -o kernel_irq.o kernel_irq.c
```

- `-ffreestanding`: no libc, no `main` required (the program may
  define its own entry point; the interrupt handlers are freestanding
  C functions that the kernel registers).
- `-mgeneral-regs-only`: forbid SSE / MMX / AVX instructions (the
  IRETQ frame doesn't save XMM registers, so the function body must
  not use them). This means the body cannot call libc functions
  (which use SSE); the body must be pure arithmetic + struct field
  access.
- `-mno-red-zone`: disable the red zone for the entire translation
  unit (defensive — the per-function `optimize("no-red-zone")`
  attribute is the primary mechanism).
- `-fno-stack-protector`: no stack canaries (kernel code typically
  uses its own stack-protector scheme).
- `-fno-pic`: position-dependent code (kernel code is loaded at a
  fixed address; PIC adds an indirection that slows interrupt entry).

### 27.6. Acceptance

The Stage 28 acceptance criterion: a kernel's interrupt handler
compiles with `#[irq_handler] #[no_red_zone] #[stack_size(256)]` and
the emitted assembly uses <= 256 bytes of stack.

The acceptance gate (`make stack-acceptance`) verifies:

1. The HLS file parses with all three attributes (via `boot/boot.py`
   and via the native `bin/hlc`).
2. The C source contains `__attribute__((interrupt))` on the
   `handle_irq` and `handle_irq_minimal` functions.
3. The C source compiles cleanly under the freestanding build
   environment for kernel code.
4. The static stack-size estimate is within the declared bound (no
   `#[stack_size(N)] violated` compile error).

See `examples/kernel_irq_demo.hls` for the full example.

---

## 28. Inline / hot / cold attributes (Stage 29 — v0.46.0-alpha)

Stage 29 introduces four new function attributes that give the
programmer explicit control over the optimiser's inline / hot / cold
decisions. These override the PGO-derived heuristics (Stage 19) and
the LTO inliner's budget-based decisions (Stage 20):

```hls
#[inline(always)]   # force inline at every call site
#[inline(never)]    # forbid inlining at every call site
#[hot]              # mark the function hot (overrides PGO)
#[cold]             # mark the function cold (overrides PGO)
```

### 28.1. `#[inline(always)]` — force inline at every call site

The codegen emits `static inline __attribute__((always_inline))` on
the function signature. gcc requires BOTH the `inline` keyword AND
the `__attribute__((always_inline))` attribute for the hint to take
effect (a lone `__attribute__((always_inline))` without `inline`
is silently ignored).

The LTO inliner (`lto_can_inline`) honours `#[inline(always)]` by
bypassing:

1. The per-callee statement budget (`LTO_INLINE_MAX_STMTS`, default
   30, tunable via `--lto-threshold`).
2. The per-program inline-site cap (`LTO_INLINE_MAX_SITES`, default
   100).

The recursion check STAYS (inlining a recursive function would loop
forever), and so does the never-return check.

```hls
#[inline(always)]
fn small_hot_helper(x: int) -> int {
    return x + 1
}
```

Emitted C:

```c
static inline __attribute__((always_inline)) int64_t usf_small_hot_helper(int64_t u_x_p) {
    return u_x_p + 1;
}
```

### 28.2. `#[inline(never)]` — forbid inlining at every call site

The codegen emits `__attribute__((noinline))` on the function
signature. The LTO inliner returns false IMMEDIATELY for
`#[inline(never)]` functions (before any other check). The
`__attribute__((noinline))` is a SECOND layer of defence — gcc will
refuse to inline even if the LTO inliner missed it.

```hls
#[inline(never)]
fn big_rare_path(x: int) -> int {
    let mut s: int = 0
    let mut i: int = 0
    while i < x {
        s = s + i * 2
        i = i + 1
    }
    return s
}
```

Emitted C:

```c
__attribute__((noinline)) int64_t usf_big_rare_path(int64_t u_x_p) {
    int64_t u_s = 0;
    int64_t u_i = 0;
    while (u_i < u_x_p) {
        u_s = u_s + u_i * 2;
        u_i = u_i + 1;
    }
    return u_s;
}
```

### 28.3. `#[hot]` — mark the function hot (overrides PGO)

The codegen emits `__attribute__((hot))` on the function signature.
gcc's hot attribute:

1. Hints gcc to inline-aggressively at every call site (the gcc
   inliner respects the hot attribute as a strong hint).
2. Lays out the function near other hot code (improves I-cache
   locality).
3. Applies hot-path optimisations (more aggressive inlining,
   unrolling, vectorisation).

When `--pgo-use` is active, `#[hot]` OVERRIDES the PGO profile's
hot/cold classification — the user's explicit annotation wins.

```hls
#[hot]
fn hot_loop(n: int) -> int {
    let mut i: int = 0
    let mut s: int = 0
    while i < n {
        s = s + i
        i = i + 1
    }
    return s
}
```

Emitted C:

```c
__attribute__((hot)) int64_t usf_hot_loop(int64_t u_n_p) { ... }
```

`#[hot]` is mutually exclusive with `#[cold]` (compile error if both
appear on the same function).

### 28.4. `#[cold]` — mark the function cold (overrides PGO)

The codegen emits `__attribute__((cold))` on the function signature.
gcc's cold attribute:

1. Lays out the function away from hot code (improving I-cache
   locality of the hot path).
2. Applies cold-path optimisations (smaller code, more sharing
   between cold paths — gcc merges cold paths aggressively).

When `--pgo-use` is active, `#[cold]` OVERRIDES the PGO profile's
hot/cold classification.

```hls
#[cold]
fn cold_path(x: int) -> int {
    if x < 0 {
        return 0
    }
    return x
}
```

Emitted C:

```c
__attribute__((cold)) int64_t usf_cold_path(int64_t u_x_p) { ... }
```

`#[cold]` is mutually exclusive with `#[hot]`.

### 28.5. `--opt-stats` — per-function optimisation-decision report

The new `--opt-stats` CLI flag prints a per-function optimisation-
decision report to stdout after codegen (after the C source is
written to the output file). The report covers:

1. A tally of each annotation kind present in the program
   (`#[inline(always)]`, `#[inline(never)]`, `#[hot]`, `#[cold]`,
   `#[irq_handler]`, `#[no_red_zone]`, `#[stack_size(N)]`).
2. The PGO-derived decisions when no annotation overrides (PGO-derived
   hot, cold, static-inline).
3. The LTO inline stats (sites + distinct callees + bodies dropped)
   when `--lto` is active.
4. A per-function table:

```
=== opt-stats ===
  functions in program          : 5
  #[inline(always)] annotations: 1
  #[inline(never)]  annotations: 1
  #[hot]             annotations: 1
  #[cold]            annotations: 1
  PGO profile: (none loaded; --pgo-use <file> to enable)
  LTO: (disabled; --lto to enable)

  per-function decisions:
    name                          inline      hot/cold    frame                source
    ----                          ------      ---------   -----                ------
    small_hot_helper                ALWAYS       -            -                     annotated
    rare_path                       NEVER        COLD         -                     annotated
    hot_loop                        auto         HOT          -                     annotated
    cold_path                       auto         -            -                     annotated
    main                            auto         -            -                     heuristic
```

Columns:
- **inline**: `ALWAYS` (#[inline(always)]), `NEVER`
  (#[inline(never)]), `PGO-inline` (PGO-derived static-inline hint
  for hot small functions), or `auto` (heuristic — gcc decides).
- **hot/cold**: `HOT` (#[hot]), `COLD` (#[cold]), `PGO-hot`
  (PGO-derived hot), `PGO-cold` (PGO-derived cold), or `-` (none).
- **frame**: the Stage 28 frame attributes (`irq`, `no-red-zone`,
  `stack<=N`) or `-` (none).
- **source**: `annotated` (any Stage 28/29 attribute is set),
  `PGO` (PGO-derived without user annotation), or `heuristic`
  (no annotation, no PGO).

### 28.6. `hllint` L011 — inline-always-large

The new `hllint` rule `L011 inline-always-large` warns when
`#[inline(always)]` is on a function whose body exceeds 50
statements:

```
$ hllint big_inline.hls
big_inline.hls:2: warning [L011] function 'big_inline' has #[inline(always)] but 54 statements (>50 — likely a mistake; consider removing the annotation or using #[hot])
```

The threshold (50 statements) mirrors gcc's `-Winline` warning. The
user's intent is almost certainly to use `#[hot]` (let the optimiser
decide based on the profile) or remove the annotation entirely.

### 28.7. Acceptance

The Stage 29 acceptance criterion: the optimiser's inline decisions
match the annotations 100% (verified via `--opt-stats`).

The acceptance gate (`make inline-acceptance`) verifies:

1. The HLS file parses with all four attributes (`#[inline(always)]`,
   `#[inline(never)]`, `#[hot]`, `#[cold]`).
2. The C source contains the right `__attribute__` on each function
   (always_inline on small_hot_helper, noinline on big_rare_path,
   hot on hot_loop, cold on cold_path).
3. `--opt-stats` prints the per-function table with the right
   decisions (ALWAYS / NEVER / HOT / COLD).
4. `--lto` honours the annotations:
   - `#[inline(always)]` => the function is inlined at every call
     site (0 out-of-line calls in the C source).
   - `#[inline(never)]` => the function is NOT inlined (1+ out-of-
     line calls in the C source).
5. `hllint L011` warns on `#[inline(always)]` > 50 statements.

See `examples/inline_attrs_demo.hls` for the full example.

---

## 29. Boxed-vs-stack layout analysis (Stage 30 — v0.47.0-alpha)

Stage 30 introduces **escape analysis** for `list[T]` bindings: a
list that does not escape its creating function is allocated on the
C **stack** as a typed array — zero heap objects, zero refcount
traffic — while a list that does escape keeps the ordinary
reference-counted heap layout ("boxed"). The analysis is automatic
(no annotation needed) and *proven*: the checker classifies every
use of every candidate binding, and a binding is stack-allocated
only when every use is **borrow-safe**.

```hls
fn fib_loop(n: int) -> int {
    #[stack]                        # force the stack layout (checked!)
    let window: list[int] = [0, 1]  # capacity 2, fixed at compile time
    let mut i: int = 0
    while i < n {
        let next: int = window.get(0) + window.get(1)
        window.set(0, window.get(1))
        window.set(1, next)
        i = i + 1
    }
    return window.get(0)            # zero heap objects in this loop
}
```

### 29.1. Candidates

A `let` binding is a layout candidate when ALL of these hold:

1. The type is `list[int]`, `list[float]` or `list[bool]`
   (primitive elements — no per-element refcounting).
2. The initialiser is a NON-EMPTY list literal (the capacity is
   fixed at compile time by the literal length).
3. The enclosing function is non-generic (generic instantiations
   are generated under mangled keys; layout there would be
   instantiation-dependent — a documented Stage 30 limitation).

`mut` bindings are eligible (reassignment is classified as an
escape, see 29.2).

### 29.2. Borrow-safe uses vs escapes

Every occurrence of a candidate binding is classified by the
checker:

**Borrow-safe (the list itself stays in the frame):**

| Use | Example |
|-----|---------|
| `.get(i)` / `.set(i, v)` / `.len()` receiver | `window.get(0)` |
| index base (read or write) | `xs[1]`, `xs[i] = v` |
| for-in iterable | `for v: int in xs { ... }` |

Note the receiver of `.get()` is safe even when the RESULT escapes:
`sum(xs.get(0))` passes an int copy, not the list.

**Escapes (the binding keeps the heap layout):** return value, call
argument, method argument, user-method receiver, struct literal
field, list/map literal element, `clone`/`take`/`drop` argument,
assignment source, operator operand (`xs == ys`), match scrutinee,
whole-binding reassignment (`xs = ...`), and `push`/`pop` receivers
(a fixed-capacity array cannot grow or shrink).

### 29.3. `#[stack]` — force the stack layout (the proof)

`#[stack]` (placed directly before the `let` statement) forces the
stack layout. The checker then REQUIRES the escape analysis to
succeed — any escaping use is a compile error naming the class and
line:

```
#[stack] violated: 'xs' escapes its creating frame — return value
(line 42). A stack-allocated value can NEVER outlive the function
that created it: remove the escaping use or drop the attribute.
```

`push`/`pop` on a `#[stack]` list, non-primitive element types
(`list[str]`), non-literal initialisers, empty literals and generic
enclosing functions are all rejected with precise messages. This is
the roadmap's soundness guarantee, enforced by BOTH the self-hosted
checker and the Stage-0 boot checker.

### 29.4. `#[boxed]` — force the heap layout

`#[boxed]` opts the binding out of the automatic analysis: it keeps
the ordinary reference-counted heap layout even when escape-free
(e.g. when the list will later be returned or grown by design).
`#[stack]` and `#[boxed]` are mutually exclusive. Both are
**let-binding attributes** — placing them before a `fn` (or a fn
attribute such as `#[inline]` before a `let`) is a parse error with
a message pointing at the correct position.

### 29.5. Generated C

A stack-allocated binding becomes a typed array in the creating
frame, with elements emitted as separate sequenced assignments
(preserving the interpreter's left-to-right evaluation and panic
order):

```c
int64_t u_window[2];
u_window[0] = 0;
u_window[1] = 1;
```

`.get`/`.set`/`.len`/`xs[i]`/for-in lower to bounds-checked typed
accessors (`hlc_sg_i64`, `hlc_ss_i64`, `hlc_sg_f64`, ... — emitted
once per program) whose panic message is IDENTICAL to the boxed
runtime's (`"array access out of bounds"`), so the two layouts are
observably indistinguishable: the differential suite (interpreter ↔
native) stays byte-identical, including under `--lto`, `-O fast`,
`--pgo-generate` and `--pgo-use`. Under LTO, inlined callee bodies
fall back to the heap layout (layout keys are per source function).

### 29.6. `--opt-stats` layout report

`--opt-stats` (Stage 29) gains a layout section: a summary
(stack-allocated / boxed / heap-escape / heap-push / tracked) and a
per-binding decision table:

```
list layout decisions (Stage 30):
  binding                              layout     cap   reason
  ------                              ------     ---   ------
  fib_stack::window                     stack      2      #[stack] forced (escape-free)
  poly_eval::coeffs                     stack      3      escape-free (auto)
  make_table::xs                        heap       3      escapes: return value (line 62)
  boxed_demo::xs                        boxed      3      #[boxed] forced heap layout
```

`make layout-report F=<file.hls>` prints it.

### 29.7. Acceptance

The Stage 30 acceptance criterion: `examples/fibonacci.hls`'s inner
loop allocates zero heap objects. The gate
(`make escape-acceptance`) verifies:

1. The C source lays the `#[stack]` window out as a frame array
   (`int64_t u_window[2]`), and `usf_fib_loop` / `usf_spin_fib`
   contain zero `hl_list_new` calls.
2. The deterministic malloc-count gate: the workload spins the
   inner loop 20,000 times; with the stack layout the program
   performs a CONSTANT ≤128 heap allocations total (measured: 90 —
   startup + prints only), while the `#[boxed]` twin of the same
   program performs 1,280,234 — proving both the zero-allocation
   claim and that the counter catches heap traffic.
3. `valgrind --tool=massif` runs too when valgrind is installed
   (the roadmap's literal wording); the malloc interposer is its
   exact-count equivalent where valgrind is absent.
4. Interpreter and native outputs are byte-identical (the layout is
   unobservable).

See `examples/stack_layout_demo.hls` for the full example and
`examples/fibonacci.hls` for the acceptance target.

## 30. Verified tail-call optimisation (Stage 31 — v0.48.0-alpha)

`#[tail_call]` on a function asserts that every recursive call to it
is in VERIFIED tail position. "Verified" is a concrete, mechanical
claim — the compiler proves two properties and then lowers the
recursion to a jump that is sound BY CONSTRUCTION:

```hls
#[tail_call]
fn fib_tail(n: int, a: int, b: int) -> int {
    if n == 0 {
        return a
    }
    return fib_tail(n - 1, b, (a + b) % 1000000007)
}
```

### 30.1. The two verified properties

**Position.** Every self-call must be the ENTIRE return expression
(`return f(...)`), never nested inside an operator, an argument, a
`let` binding or a discarded expression statement. Anything else is
a compile error naming the line:

```
#[tail_call]
fn f(n: int) -> int {
    return f(n - 1) + 1        # NOT tail: the result feeds an operator
}
```

```
panic: type error: #[tail_call] violated: the recursive call to 'f'
at line 3 is not in tail position — every recursive call must be the
entire return expression (`return f(...)`) (line 3)
```

**No cleanup.** Nothing needing release may be created between the
(transformed) call and the function entry. The codegen lowers the
tail call to a `goto` back to the function entry; a `goto` skips
every `__attribute__((cleanup))` variable declared in the body — so
the verifier PROVES the body cannot contain one:

- every parameter, the return type and every `let` binding must be
  `int` / `float` / `bool` (never refcounted);
- every expression in the body must be primitive-typed. The ONE
  exception: a string LITERAL passed directly to `panic` / `print` /
  `println` — consume-builtins in both backends (the argument is
  transferred and released by the callee, never hoisted into a
  cleanup temp, never bound).

```hls
#[tail_call]
fn f(n: int) -> int {
    let s: str = "x"          # REJECTED: refcounted binding
    return f(n - 1)
}
```

```
panic: type error: #[tail_call] violated: 'f' cannot bind non-primitive
values — `let s: str` at line 3. A refcounted binding would need
release at the loop jump, which the verified transform forbids (keep
the body to int / float / bool) (line 3)
```

### 30.2. Scope restrictions (compile errors)

- the function must be a plain `fn` — not generic (the codegen emits
  per-instantiation bodies; the loop keying is per source fn), not a
  method (`self` is a refcounted receiver);
- no `requires` / `ensures` contracts (the postcondition check would
  interpose cleanup between the tail call and the jump);
- the return type must be `int` / `float` / `bool` (the loop feeds a
  value back; `void` has nothing to feed);
- `#[tail_call]` + `#[irq_handler]` or `#[inline(always)]` are
  mutually exclusive (parse-time, either attribute order);
- the function must actually call itself (a `#[tail_call]` fn with
  zero self-calls is a pointless assertion — compile error).

### 30.3. The native transform (C backend)

`gen_fn_body` emits a label after the parameter locals:

```c
int64_t usf_fib_tail(int64_t u_n_p, int64_t u_a_p, int64_t u_b_p) {
    int64_t u_n = u_n_p;
    int64_t u_a = u_a_p;
    int64_t u_b = u_b_p;
hl_tail_restart:;
    if ((u_n == 0)) {
        return u_a;
    }
    {
        int64_t hlc_tc_1 = hl_sub_i64(u_n, 1);      /* args first,  */
        int64_t hlc_tc_2 = u_b;                     /* left->right  */
        int64_t hlc_tc_3 = hl_mod_i64(hl_add_i64(u_a, u_b), 1000000007);
        u_n = hlc_tc_1;                             /* then rebind  */
        u_a = hlc_tc_2;
        u_b = hlc_tc_3;
        goto hl_tail_restart;                       /* a jmp, not   */
    }                                               /* a call       */
}
```

The argument expressions are evaluated into fresh temps FIRST (a
call's exact evaluation order — side effects and panics happen in
source order), then the parameter locals are rebound (plain copies:
no refcounts, no aliasing — the verifier guarantees it), then the
jump. The frame never grows: `fib_tail(1_000_000)` uses the same
stack as `fib_tail(1)`.

PGO / LTO interactions:
- the PGO entry counter sits BEFORE the label, so it counts real
  function entries (one per call, not one per loop iteration);
- `lto_can_inline` never inlines a `#[tail_call]` fn: the label
  would be duplicated across inline clones, and the constant-stack
  loop would become call machinery again. Every call site stays
  out-of-line, which also keeps `lto_calls[key] >= 1`, so phase-B
  DCE can never drop the standalone body containing the loop.

### 30.4. The interpreter mirror (trampoline)

The Stage-0 interpreter implements the same transform as a
trampoline: a verified tail `return f(...)` raises `TailCallSig`
carrying the evaluated arguments; `call_fn` rebinds the parameters
and re-runs the body in the SAME Python frame. `fib_tail(1_000_000)`
needs zero Python recursion (no `RecursionError`). The current-fn
key lives on the same thread-local storage as the panic `line`
number (task threads get it lazily — concurrency is unaffected).

### 30.5. `--opt-stats`

```
#[tail_call]       annotations: 1 (verified sites: 1, stack O(1))
...
  verified tail calls (Stage 31):
    name                          sites   params  ret      stack
    ----                          -----   ------  ---      -----
    fib_tail                       1      3       int      constant
```

`make tail-report F=<file.hls>` prints it.

### 30.6. Acceptance

The Stage 31 acceptance criterion: `examples/fibonacci.hls` rewritten
with `#[tail_call]` runs `fib(1_000_000)` without stack overflow. The
gate (`make tail-acceptance`) verifies:

1. The C source contains the loop label and the parameter-rebinding
   `goto`, and `usf_fib_tail`'s body contains ZERO recursive calls
   (the tail call is a jmp, not a call).
2. Native `fib_tail(1_000_000)` equals the value computed
   independently (Python fast-doubling): 918091266 (mod 1e9+7).
3. The same 1M-deep run succeeds under `ulimit -s 1024` — a 1 MB
   stack. A 1M-deep real call chain at ~48 bytes/frame would need
   ~48 MB; the loop fits in kilobytes. This PROVES the stack usage is
   constant regardless of recursion depth.
4. The interpreter runs the same depth with no `RecursionError`
   (the trampoline), producing byte-identical output (differential).

See `examples/fibonacci.hls` for the acceptance target and
`tests/ok/feat_stage31_tail.hls` for the feature matrix (multiple
tail sites, float / bool accumulators, a `while` loop coexisting
with the tail loop, a panic-literal guard, 50,000-deep runs through
both backends).

---

## 31. Freestanding mode (Stage 77 — v0.96.0-alpha)

`#![freestanding]` turns the whole program into a freestanding crate:
no libc, no OS calls, no `std`, entry `_start`. This is the first
stage of Phase VI (OS development foundation) — the language gains
the capabilities OS authors need, while the hosted language is
unchanged.

```hls
#![freestanding]

fn fib(n: int) -> int {
    if n < 2 {
        return n
    }
    return fib(n - 1) + fib(n - 2)
}

fn main() -> int {
    return fib(10)   # the exit code IS the output (no I/O exists here)
}
```

### 31.1. The three boundaries (all enforced statically)

**Module boundary.** `import "std.*"` is rejected — only `core.*`
(Stage 78) and relative imports are allowed. Rationale: every
`std` module may assume a host; `core` modules are audited to
assume nothing.

**Host boundary.** `extern "C"` / `extern "js"` blocks are rejected —
freestanding code cannot call into a host that does not exist.

**Capability boundary.** Any `uses` clause is rejected — every
function must be pure. Effectful builtins (`println`, `read_file`,
`clock_ms`, `rand_int`, `spawn`, ...) are additionally unreachable:
without a declared capability the existing subset test rejects
every call site.

### 31.2. The float-library denylist

The freestanding C prelude provides no snprintf, no strtod, no
libm — so the builtins that lower to them are rejected at check
time (in BOTH compilers, with the same message):

- the 24 libm-backed `math_*` builtins (`math_sin` … `math_lgamma`);
- `float(s: str)`, `str.to_float()`, `str(float)`, `float.to_str()`;
- `float % float` (lowers to `fmod`).

Everything else float stays available: `+ - * /`, comparisons,
`int`↔`float` casts, `float.to_int()`, `int.to_str()` (hand-rolled
`%lld`, exact for every int64), and the four predicates
`math_isnan` / `math_isinf` / `math_isfinite` / `math_signbit`
(exact bit tests in the prelude, no libm).

### 31.3. The freestanding runtime contract

The C backend emits a freestanding translation unit instead of the
hosted one:

- **Headers:** only `<stdint.h>`, `<stdbool.h>`, `<stddef.h>` (all
  freestanding per C11 §4). No `<stdio.h>`, no `<stdlib.h>`, no
  `<pthread.h>`.
- **Heap:** a static 1 MiB bump arena backs `malloc`/`calloc`/
  `realloc` (`free` is a no-op). Allocation sizes ride in an 8-byte
  header so `realloc` copies the old contents; OOM and size
  overflow trap. Reclamation is deferred to `core.alloc`
  (Stage 79) — a freestanding program that outgrows 1 MiB today
  must be restructured, loudly (a trap) rather than silently.
- **Panics:** `hl_die` / `hl_die_at` / `hl_panic` trap
  (`__builtin_trap`) — there is no stderr and no `exit(101)`
  without an OS. Stage 81 (`#[panic_handler]`) makes this
  user-overridable.
- **Entry:** `void _start(void)` calls the HLS `main`, then exits
  via a raw syscall (x86-64: `rax=60`; AArch64: `x8=93`; RISC-V:
  `a7=93`; other arches trap after main returns).
- **Link recipe:** `gcc -O2 -ffreestanding -nostdlib
  -ffunction-sections -fno-stack-protector -Wl,--gc-sections`.
  Section garbage-collection drops the dead hosted-only runtime
  functions (file IO, sockets, pthreads, libm wrappers) before
  undefined-symbol resolution — only the reached pure functions
  must resolve, and they do via the prelude.

`--pgo-generate` is rejected in freestanding mode (no
atexit/file-IO for the profile).

### 31.4. Attribute rules (shared with §32)

- `#![freestanding]` / `#![no_std]` are the only crate attributes.
  Unknown names, duplicates, and placement after any item are
  compile errors (in both compilers, byte-identical messages
  modulo formatting).
- Crate attributes are honoured ONLY from the entry file — a
  dependency declaring one is rejected.
- `#![freestanding]` implies `#![no_std]`.

`boot.py --audit` reports the crate mode (`std` /
`#![no_std]` / `#![freestanding]`).

---

## 32. `no_std` + the `core` module family (Stage 78 — v0.97.0-alpha)

`#![no_std]` disables `std` exactly like `#![freestanding]`, but
keeps hosted libc: the C backend still emits `main` and links
normally. It is the mode for programs that must not DEPEND on the
hosted standard library (kernels, bootloaders, UEFI apps link this
form) while still running on a host for testing.

```hls
#![no_std]

import "core.option"
import "core.result"

fn parse_port(s: str) -> Result[int, str] {
    let r: Result[int, str] = parse_int(s)
    if result_is_err(r) {
        return Result.Err(result_unwrap_err(r))
    }
    return Result.Ok(result_unwrap(r))
}

fn main() -> int {
    return result_unwrap_or(parse_port("8080"), 1)
}
```

### 32.1. The `core` modules

| Module | Contents |
|--------|----------|
| `core.option` | `enum Option[T]` + `option_unwrap` / `unwrap_or` / `is_some` / `is_none` |
| `core.result` | `enum Result[T, E]` + `unwrap` / `unwrap_or` / `unwrap_err` / `is_ok` / `is_err` + `parse_int` (no-panic integer parsing) |
| `core.iter` | `struct ListIter[T]` + `list_iter` / `iter_next` / `iter_has_next` / `iter_remaining` / `iter_count` / `iter_sum_int` |
| `core.clone` | `clone_list_of` / `clone_some` / `clone_map_of` + the `clone_of` method convention |
| `core.eq` | `eq_list_int` / `eq_list_str` / `eq_list_bool` / `eq_option_int` / `eq_option_str` / `eq_map_str_int` + the `eq_of` method convention |

Every module is pure HLS (`no_std`-clean: no `uses`, no `std`
imports, no `extern`). `core.iter` imports only `core.option`;
`core.clone` / `core.eq` import only `core.option`.

### 32.2. Conventions, not yet traits

HLS has no trait dispatch yet (`trait` is still reserved) and
generic `impl` blocks are not supported yet — so `Iterator`,
`Clone` and `Eq` ship as documented conventions over free generic
functions and concrete methods (the same "monomorphic helpers"
convention `std.tui` uses for `Widget`):

- **Iterator:** an iterator is a struct holding its cursor;
  `iter_next` yields `Option[T]` (`None` = exhausted).
- **Clone:** a type with value-copy semantics provides
  `fn clone_of(self: T) -> T` on its concrete struct.
- **Eq:** a comparable type provides
  `fn eq_of(self: T, other: T) -> bool`. Generic `==` on a type
  parameter is rejected (a `T` may not support comparison), hence
  the concrete `eq_*` helpers.

When dispatch arrives, each convention maps to a trait unchanged
in spirit.

### 32.3. `core` vs `std` duality

`core.option` / `core.result` and `std.option` / `std.result`
declare DIFFERENT `Option` / `Result` types (HLS has no re-export
mechanism) with intentionally identical APIs — porting is a
one-line import change, except `float_parse` (absent from
`core.result`: `str→float` has no freestanding lowering, §31.2).
Never import both spellings into one program (duplicate-type
error, by design — the boundary must stay visible).

`float %`, `str(float)`, `float.to_str()`, `str.to_float()` and
the 24 libm builtins are rejected in `#![freestanding]` mode
(§31.2) but ALLOWED in `#![no_std]` mode (libc is present).


## 33. `core.alloc` — the Alloc protocol (Stage 79 — v0.98.0-alpha)

`core.alloc` is the pluggable allocator protocol. The roadmap's
signature is `trait Alloc { fn alloc(layout: Layout) ->
Result[Ptr, AllocError]; fn dealloc(ptr: Ptr, layout: Layout) ->
void; }`. HLS has no trait dispatch yet (§32.2), so the protocol
ships as FREE functions per concrete allocator type — the same
"monomorphic helpers" convention `core.iter` / `core.clone` /
`core.eq` already use. When `impl` blocks gain type parameters,
each `<type>_alloc` below becomes `impl <Type> { fn alloc(...) }`
unchanged in spirit.

The module is pure HLS, `no_std`-clean: imports only
`core.option` + `core.result`, no `uses`, no `extern`, no
`std.*`. It is importable from `#![no_std]` and `#![freestanding]`
crates identically (no resolver changes — the `core.` prefix is
already uniform since Stage 78).

```hls
#![no_std]

import "core.alloc"
import "core.result"

fn main() -> int {
    let a: BumpAlloc = bump_new(128)
    let l: Layout = result_unwrap(layout_new(16, 8))
    let p1: int = result_unwrap(bump_alloc(a, l))  # 0
    let p2: int = result_unwrap(bump_alloc(a, l))  # 16
    bump_reset(a)                                  # reclaim all
    if bump_used(a) != 0 { return 1 }
    if bump_peak(a) != 32 { return 2 }             # preserved
    return 0
}
```

### 33.1. The Alloc protocol surface

| Type | Role |
|------|------|
| `struct Layout { size, align }` | allocation request (size in bytes, alignment in bytes — a positive power of two ≤ 4096) |
| `enum AllocError { OutOfMemory, BadLayout, DoubleFree, OutOfRange }` | the error set |
| `struct BumpAlloc { capacity, offset, peak, allocations }` | fast, monotonic, no individual reclaim |
| `struct PoolAlloc { block_size, block_count, free_list, in_use, allocations, peak }` | fixed-size block pool with LIFO free list + O(1) double-free detection |
| `struct NullAlloc { _marker }` | sentinel: always `Err(OutOfMemory)` (the placeholder field is required by the grammar) |
| `struct AllocStats { bytes_alloc, bytes_dealloc, allocations, deallocations, peak }` | accounting helper (NOT an allocator) |

Layout helpers: `layout_new` (validates), `layout_for_words`
(convenience for 8-byte-aligned int64/pointer slots),
`layout_pad_to` (the stride the bump allocator advances),
`layout_align_up` (rounds any int up to an alignment),
`layout_resize` (grows the size in place), `layout_size` /
`layout_align` (accessors), `layout_is_power_of_two` (the
validator primitive).

BumpAlloc: `bump_new(capacity)` constructs, `bump_alloc(a, l)`
returns the aligned offset (the cursor rounds up to `l.align`
before recording, then advances by the padded size),
`bump_dealloc` is a no-op (Ok(0) for protocol parity),
`bump_reset` reclaims the whole region in one shot,
`bump_peak` and `bump_allocations` survive the reset (high-water
mark + count across cycles), `bump_full` reports capacity
exhaustion.

PoolAlloc: `pool_new(block_size, count)` initialises the free
list so the FIRST allocation returns offset 0 (then
`block_size`, `2*block_size`, ...), `pool_alloc` pops a block
offset from the free list, `pool_dealloc` validates the offset
is in range, block-aligned, and currently live (else
`Err(OutOfRange)` / `Err(DoubleFree)`) and returns it to the
free list. Layout-size-larger-than-block and
align-larger-than-block requests return `Err(OutOfMemory)`.

NullAlloc: `null_alloc_new()` constructs the sentinel,
`null_alloc_alloc` always returns `Err(OutOfMemory)`,
`null_alloc_dealloc` always returns `Err(OutOfRange)`. The
sentinel is the verifier's hook for Stage 91 (verified
interrupt-safety): an IRQ handler takes a `NullAlloc` and the
checker rejects any code path that calls `*_alloc` on it
(because the result is always `Err` and the user must match).

AllocStats: `stats_record_alloc(size)` updates the cumulative
counter, increments the allocation count, and bumps the peak
if the current in-use byte count exceeds it.
`stats_record_dealloc(size)` updates the cumulative
deallocation counter; the peak is NOT lowered (it records the
high-water mark). `stats_current` is `bytes_alloc -
bytes_dealloc` (a negative value is a leak indicator).

### 33.2. On "pointers"

HLS has no raw pointer type yet. The `int` returned by `*_alloc`
is a LOGICAL ADDRESS — a byte offset into a backing region the
user owns (typically a `list[int]` sized to the allocator's
capacity, or a side table indexed by the returned offset). The
allocator is a pure bookkeeping abstraction: it hands out
offsets, tracks which are live, and (for reclaiming allocators)
returns them on `*_dealloc`. The soundness invariants (no
use-after-free, no double-free) are enforced at the allocator
level, not the pointer level. A future stage adds `Ptr[T]`
raw-pointer types once the verifier can prove the lifetime
invariants; until then, the logical-address contract is the
same one a kernel's pre-paging bump allocator honours.

### 33.3. On `void` dealloc

The roadmap's signature is `fn dealloc(...) -> void`. This
module returns `Result[int, AllocError]` from `*_dealloc`
instead, because the pool allocator can DETECT a double-free or
an out-of-range pointer at dealloc time — silently dropping
that information would be a soundness hole (a kernel that
double-frees a page frame must panic, not continue). The
`Ok(0)` success value mirrors the `void` return; users who
don't care about the error case can `let _ = pool_dealloc(a,
ptr, l)` and ignore it.

### 33.4. Bit operations via builtins, not operators

HLS has no bitwise operators (`&`, `|`, `^`, `~`, `<<`, `>>`)
in its grammar (the language core treats `int` as a
mathematical int64 with checked arithmetic only, §7). The
`layout_is_power_of_two` and `layout_align_up` helpers use the
`int_and` and `int_not` builtins, which compile to a single C
operation (no libc required — they are pure and
freestanding-safe). The bit trick
`n > 0 && int_and(n, n - 1) == 0` works because a power of two
in two's complement has exactly one bit set; subtracting 1
clears that bit and sets all the lower ones, so AND-ing the two
yields zero.

### 33.5. Runtime integration (future)

The roadmap's promise is that `core` uses the user-supplied
allocator for `list[T]` and `map[K, V]`. This is a LATER stage:
it requires a `global_alloc` registration that the language
cannot express yet without traits or a `register_alloc`
builtin. What `core.alloc` PROVES here is that the protocol
itself composes — any user-defined data structure that takes
an allocator can swap bump / pool / null behind the same call
sites. The `examples/alloc_demo.hls` "request router" (a
`BumpAlloc`-backed scratch scope + a `PoolAlloc`-backed route
table with LIFO recycle and double-free detection) is the shape
of the future: user-owned data structures that compose with
any allocator.

## 34. `core.mem` — the physical-page allocator + page tables (Stage 80 — v0.99.0-alpha)

`core.mem` is the memory-management substrate: the two structures
every kernel builds before it can map its first virtual byte. The
module is pure HLS — 95 functions, 3 structs (`FrameAlloc`,
`PageTable`, `AddressSpace`), 1 enum (`MemError`), importing only
`core.option` + `core.result` — so it is `no_std`-clean and
importable from `#![freestanding]` crates identically. As with
`core.alloc` (§33), the module is a faithful BOOKKEEPING MODEL: it
performs the same arithmetic, validation and accounting as the
hardware, but never dereferences an address (HLS has no raw
pointers yet). The `AddressSpace.tables` pool stands in for the
direct map a real kernel uses to touch its own page tables —
`space_table_index(space, base)` finds a table by the physical
address of its frame.

### 34.1. Page geometry and canonical addresses

`mem_page_size()` is 4096, `mem_huge_page_size()` is 2 MiB (the
coverage of one full PT, 512 pages), and the helpers
`mem_is_page_aligned` / `mem_is_huge_aligned` /
`mem_page_align_up` / `mem_page_count` /
`mem_pages_to_bytes` express the usual arithmetic. Alignment is a
LOW-BIT property: high-half canonical virtual addresses are
negative int64s (the kernel text base `0xFFFFFFFF80000000` is
`-2147483648` as an int64) and are legitimately page-aligned, so
the alignment helpers mask bits without a sign check. Callers that
handle PHYSICAL addresses add the explicit `>= 0` check
(`frame_new`, `pte_new`).

The virtual-address decomposition follows x86-64 four-level paging:
`vaddr_pml4_index` / `vaddr_pdpt_index` / `vaddr_pd_index` /
`vaddr_pt_index` extract the four 9-bit index fields with the
LOGICAL right shift `int_shr` (which operates on the unsigned bit
pattern, so negative high-half addresses decompose correctly), and
`vaddr_page_offset` / `vaddr_huge_offset` extract the 12- and
21-bit in-page offsets. `vaddr_is_canonical` implements the full
canonical-form rule — bits 63..48 must equal bit 47 — with one
arithmetic shift: `int_sar(v, 47)` is `0` for the low half, `-1`
for the high half, and neither for an address in the 2^47 canonical
hole. Every mapping entry point rejects non-canonical addresses
exactly where the CPU would raise.

### 34.2. The PTE format

Entries use the real x86-64 layout: flag bits 0..8 (PRESENT,
WRITABLE, USER, PWT, PCD, ACCESSED, DIRTY, HUGE/PS, GLOBAL), the
physical (or next-table) address in bits 12..51 — the full 52-bit
architectural physical address width, enforced by `pte_new` against
`pte_addr_limit()` — and NO_EXECUTE in bit 63. Because bit 63 is
the sign bit of an int64, `pte_flag_no_execute()` is INT64_MIN and
every NX-carrying entry is a NEGATIVE int64; the accessors treat
entries as unsigned bit patterns (`int_and` / `int_shr`), so NX
round-trips on both backends. Two consequences are tested
explicitly (§34.5): `pte_address` is a pure mask (addresses are
stored unshifted, low 12 bits zero by the alignment rule), and no
internal walk may use a negative-entry sentinel (see §34.4).

HLS has no module-level constants, so each flag is a pure function
(`pte_flag_present()`, `pte_flag_writable()`, ...) — the same
convention `std.bits` uses for `bits_pow2`. `pte_new(paddr, flags)`
validates alignment, the 2^52 limit, and that `flags` stays inside
`pte_flags_known_mask()` (bits 0..8 plus 63), returning
`MemError.BadAlignment` / `OutOfRange` / `BadFlags` respectively.
`pte_set_flags` / `pte_clear_flags` compose and strip flags without
touching the address field.

### 34.3. FrameAlloc — the physical-page allocator

A bitmap allocator over ONE physical region
`[base, base + frame_count * 4096)`; `used[i]` is true while frame
i is allocated or reserved. The policies are deterministic (and
therefore differential-testable):

- `frame_alloc_one` — first-fit scan from frame 0.
- `frame_alloc_run(count)` — first-fit CONTIGUOUS free run
  (DMA-style adjacency).
- `frame_alloc_huge` — first-fit 512-frame run whose PHYSICAL
  ADDRESS is 2-MiB aligned. The alignment that matters is the
  address, not the frame index: with a region base of 0x100000 the
  first candidate is frame index 256, because the CPU requires the
  huge-page physical base itself to be 2-MiB aligned. This is where
  `core/alloc.hls`'s Stage 80 note ("larger alignments belong to
  huge pages") lands.
- `frame_free(addr, count)` — every failure mode is a distinct
  kernel bug: `BadRange` (count <= 0), `BadAlignment`
  (misaligned address), `OutOfRange` (outside the region or
  crossing its end), `DoubleFree` (any frame in the run already
  free).
- `frame_reserve(addr, count)` — marks firmware/ACPI/MMIO holes in
  use before any allocation; double reservations fail with
  `InUse`. Reservations raise `peak` (they pin real memory) but not
  `allocations`.

The accounting mirrors `core.alloc`'s `AllocStats`:
`allocations`/`frees` count successful operations, `peak` records
the high-water mark of used frames.

### 34.4. AddressSpace — the 4-level walk

`PageTable` is 512 int64 entries plus the physical `base` of its
frame; `AddressSpace` holds the root PML4 (registered by
`space_new(root_base)`) and the pool `tables: list[PageTable]`.
The pool is the model's direct map: walks resolve a child pointer
by looking up `pte_address(entry)` in the pool.

`space_map(fa, space, vaddr, paddr, pages, flags)` maps 4-KiB
pages, creating intermediate tables on demand — the same
walk-and-create a kernel's early boot mapper performs, with the
table frames allocated FROM the FrameAlloc so the caller sees the
true cost of the map (`frame_used_count` after a fresh-region map
is root + 3 tables). The install has two passes, and the
difference is the safety story:

1. **Prepare** — validate every page (canonical, no present leaf,
   no huge leaf in the way) and COUNT the missing intermediate
   tables exactly. Because pages advance sequentially, the table
   keys `(a)`, `(a,b)`, `(a,b,c)` are lexicographically
   non-decreasing, so each key can only repeat contiguously —
   counting "on key change, if absent" counts every missing table
   exactly once, and a missing ancestor automatically cascades
   into the descendant counts (the walk only reports levels whose
   ancestors are present).
2. **Install** — re-walk and create (`space_ensure_table`), then
   write leaves as `pte_new(paddr + i*4096, flags | PRESENT)`.

If the counted tables exceed `frame_free_count(fa)`, the map fails
with `OutOfFrames` BEFORE pass two touches anything: **failure is
atomic** — an OOM map leaves the address space and the allocator
exactly as they started (probed explicitly by the acceptance
suite). Intermediate tables are linked PRESENT|WRITABLE (plus USER
when the leaf flags request user access — stripping the user bit
mid-path would fault at the missing level) and never NX.

`space_translate(space, vaddr) -> Option[int]` is the pure lookup:
physical address including the in-page or in-huge-page offset, or
`None` when any level is absent. One model subtlety: the walk's
out-pattern keeps leaf presence in a SEPARATE slot
(`found[4]`) because a "-1 means absent" sentinel would be
indistinguishable from a legitimate leaf carrying NX (negative
entry) — table bases are physical and non-negative, so the
sentinel stays safe for them. `space_unmap` /
`space_unmap_huge` are strictly paired with their map APIs
(crossing small/huge is `NotMapped`), zero entries Linux-style,
and keep table frames allocated (reclaiming them needs the
Stage 82+ lifetime story). `space_protect` re-flags existing small
leaves (mprotect semantics: address preserved, flags rebuilt,
PRESENT forced) — the W^X primitive. `mem_identity_map` and
`mem_map_bytes` wrap `space_map` for the two boot idioms
(virtual == physical, and byte-sized regions rounded up to whole
pages).

### 34.5. What the tests prove

`tests/ok/feat_stage80_mem.hls` runs 161 numbered assertions on the
interpreter AND as a native binary (hosted and `-nostdlib` — the
exit codes must match, per the differential convention):
canonical-hole rejection at ±2^47, the KT decompose
(PML4[511]/PDPT[510]), flag-bit values including NX as INT64_MIN,
double-free and reservation misuse detection, first-fit reuse of a
freed frame, huge-frame address alignment against a shifted region
base, the atomic-OOM invariant, translate/unmap/protect round
trips, and huge-page offsets. `examples/mem_demo.hls` composes the
whole surface into a boot story — claim region, reserve the
firmware hole, identity-map the low kernel, higher-half read-only
text, direct-map W^X data window, a 2-MiB device window, lockdown
— and asserts the final accounting: 1 reserved + 1 root + 9 table
frames + 512 huge = 523 frames used, 4 maps, 1 unmap, 0 failures.
## 35. `core.panic` + `#[panic_handler]` (Stage 81 — v0.100.0-alpha)

Stage 81 gives kernels their panic strategy: what runs when
everything else has failed. It has two halves — a pure-HLS library
(`core/panic.hls`: `PanicInfo`, `PanicAction`, `PanicLog`,
`panic_check`) and a language hook (the `#[panic_handler]`
function attribute) that routes every runtime panic through
user code before the default action. The module is `no_std`-clean
(zero imports — only the core language) and importable from
`#![freestanding]` crates identically.

```hls
#[panic_handler]
fn panic_handler(msg: str) -> void uses IO {
    println("kernel panic: " + msg)   # hosted: log over IO
}

fn main() -> int uses IO {
    let _: int = panic("demo fault")  # handler runs, then exit 101
    return 0
}
```

### 35.1. The handler contract

`#[panic_handler]` marks the program's single panic handler:

- Exactly one per program (a duplicate is a compile error naming
  both functions — the native hook is a single function pointer).
- A top-level, non-generic, non-extern `fn` with exactly one `str`
  parameter (the panic message) returning `void`. Methods,
  generics, wrong arity, non-`str` parameters and non-`void`
  returns are all compile errors, in both compilers with
  byte-identical messages modulo formatting.
- `#[panic_handler]` and `#[irq_handler]` are mutually exclusive
  (a handler must return normally; an interrupt frame returns via
  IRETQ).
- Effects are governed by the crate mode, not by Stage 81: a
  hosted handler may declare `uses IO` / `uses Exit` (log the
  fault, choose the halt action); a `#![no_std]` /
  `#![freestanding]` handler is effect-free by the existing mode
  bans and records into a `PanicLog`.

Invocation: the handler runs ONCE per panic with the panic
message, then the default action still proceeds (hosted: report
on stderr + `exit(101)`; freestanding: `__builtin_trap`). A
handler that wants a different halt action calls `exit(code)`
itself (hosted) — the exit propagates and wins. The handler
receives the bare message in every mode (uniform across hosted /
freestanding, so the same handler observes the same input on both
backends); the source location stays in the default report.

### 35.2. Reentrancy and ownership

Two hazards, both closed:

- **Reentrancy.** The handler runs while the runtime is already
  mid-panic (the allocator may be broken). A nested panic inside
  the handler — including OOM while wrapping the message — falls
  straight through to the default action instead of recursing
  (native: the `hl_in_panic` guard, raised BEFORE the message is
  wrapped; interpreter: the `_in_panic_hook` flag with the same
  discipline). A buggy handler can never mask the original fault.
- **Ownership.** The compiled handler takes ownership of its `str`
  parameter (cleanup attribute releases it on return). The native
  hook therefore always passes a COPY (`hl_str_from(msg->data,
  msg->len)`); the caller's string — which the default report
  prints afterwards — is untouched. (The interpreter needs no
  copy: Python strings are garbage-collected.)

Background-task panics (stream combinators on worker threads)
bypass the hook and halt directly, like interrupts arriving in a
context the handler cannot own. The LLVM and wasm backends keep
the default action in this stage; the override wires the C
backend (native + freestanding).

### 35.3. The `core.panic` surface

| Item | Role |
|------|------|
| `struct PanicInfo { message, file, line }` | the fault report (empty `file` or non-positive `line` = no location; negative lines rejected) |
| `panic_format(info)` | renders `"msg (at file:line)"` with a location, else the bare message |
| `enum PanicAction { Trap, Exit, Reboot }` | the halt policy, with `panic_action_name` + `panic_action_code` (Trap/Exit → 101, Reboot → 0 so a supervisor restarts the image) |
| `panic_default_code()` | 101 — the runtime's default halt code |
| `struct PanicLog { entries, capacity, dropped }` | bounded post-mortem ring: full pushes evict the oldest (FIFO) and count the loss in `dropped`; OOB reads panic; `panic_log_clear` keeps the capacity |
| `panic_check(cond, msg)` | the invariant helper (`if !cond { panic(msg) }`) |

Under `--lto` the handler is rooted explicitly (`lto_reachable`):
it is only ever reached via the `hl_panic_hook` pointer, which
the checker's call graph cannot see — without the root, phase A
would drop the body of a handler that is never called directly.
The entry points (`_start` / `main`) arm the hook before any user
code runs; programs without a handler leave it NULL and behave
exactly as before.

### 35.4. What the tests prove

`tests/ok/feat_stage81_panic.hls` runs 58 numbered assertions on
the interpreter AND as a native binary (hosted and `-nostdlib`):
info geometry, format strings, action codes, log eviction order,
drop counting, clear-and-reuse, and the passing side of
`panic_check` — plus the attribute itself (an effect-free handler
the checker accepts and both backends wire). `make
panic-acceptance` (7 sections) proves the firing paths:
interpreter and native binaries print the handler marker BEFORE
the default report (exit 101), an `exit(42)` handler wins on both
backends, a nested handler panic still reports the original fault,
and the three fail programs (`dup`, `bad_sig`, `bad_ret`) are
rejected by boot AND the self-hosted checker with the same
`panic_handler` message.

## 36. Deterministic stack size + guard pages (Stage 82 — v0.101.0-alpha)

Stage 82 gives kernels their stack discipline: how big the stack is,
why it cannot grow, and what happens when a task touches the page
below it. It has three halves — a pure-HLS library (`core/stack.hls`)
that models the region a kernel sets up, a language guarantee (the
`#![stack_size(N)]` crate attribute) that makes the whole program's
worst-case stack chain a checked fact, and a runtime wiring (the C
backend's task stacks) that pins real thread stacks to a
deterministic size with a real guard page. The module is
import-free — zero `core.*` dependencies, like `core/panic.hls` — so
it is importable from `#![no_std]` and `#![freestanding]` crates
identically, and its guard failures panic through the Stage 81 path
(a `#[panic_handler]` observes a guard hit like any other fault).

```hls
#![stack_size(4096)]   # the whole-program budget, verified statically

import "core.stack"

fn main() -> int uses IO {
    # Plan a task stack from the workload: 64-byte worst frame,
    # depth 1, one page of slack, one guard page, top at 1 MiB.
    let cfg: StackConfig = stack_plan_new(64, 1, 1, 1048576)
    println("usable: " + stack_usable_bytes(cfg).to_str())
    let last: int = stack_probe(cfg, stack_usable_bytes(cfg))  # ok
    let _: int = stack_probe(cfg, stack_usable_bytes(cfg) + 1) # guard page hit
    return 0
}
```

### 36.1. The static budget: `#![stack_size(N)]`

The crate-level attribute (parsed only at the top of the entry
file, at most once, like `#![freestanding]`/`#![no_std]`) declares
the program's deterministic stack budget in bytes. The checker —
both compilers, with byte-identical messages — then proves it:

- **Frame estimate.** Every function's worst-case frame is a
  conservative upper bound on what gcc can emit for the
  type-correct source: 32 bytes of base overhead, 8 bytes per
  parameter and per `let` slot, 16 bytes per call site (caller-saved
  spills + return address), 16 bytes per `for` loop — the same
  estimator the per-fn `#[stack_size(N)]` attribute (section 27)
  has used since Stage 28. `asm!` operands are not modeled
  (register-resident per the ABI).
- **Call graph.** The analysis walks the same edges the effects
  fixpoint consumes: user functions and methods, plus the synthetic
  `@default.<Struct>` nodes whose default expressions evaluate
  inside the constructing frame (their own cost is 0, their callees
  continue the chain). Extern functions contribute 0 — the C
  callee's stack is the C world's business; the caller's 16-byte
  call-site term already covers the transition. Generic functions
  keep one node per declared key: every type lowers to an 8-byte
  scalar, so a frame never depends on the substitution.
- **Recursion is rejected.** A cycle in the call graph means the
  chain depth depends on the input — no static budget can bound it,
  so any cycle is a compile error naming the cycle
  (`a -> b -> a`). `#[tail_call]` fns compile to loops, but the
  budget is a plain-call analysis: a tail-recursive fn still forms a
  cycle here. Bound the recursion or drop the budget.
- **The worst chain must fit.** The longest chain (memoized DFS,
  sorted iteration so the reported path is deterministic — the
  bootstrap self-compilation must stay byte-identical) is compared
  against N; over-budget programs name the chain and the excess.
  `--audit` (boot) and the `-O` stats report (hlc) print the
  verified worst chain for the crate.

Because the estimator is an upper bound, `estimate <= chain <= N`
is transitive: the compiled program's stack use is bounded by N on
every execution path that terminates. That is what "deterministic
stack size" means here — the same source always allocates the same
stack, and no input can push it further.

Per-fn `#[stack_size(N)]` validation is now enforced by BOTH
compilers. Stage 28 shipped it native-only; boot parsed the
attribute and silently ignored the bound (a program boot accepted,
hlc rejected). Stage 82 ports the estimator to the boot checker and
closes the gap.

### 36.2. The `core.stack` surface

| Item | Role |
|------|------|
| `stack_page_size()` / `stack_page_align` / `stack_page_align_up` / `stack_pages_for` | the 4-KiB page arithmetic (negative inputs panic — there is no aligned form of a negative address) |
| `struct StackConfig { top, size, guard_pages }` | the region picture: page-aligned usable bytes growing down from `top`, guard pages below; `stack_config_new` validates all three invariants (positive aligned size, ≥ 1 guard page, aligned top high enough) |
| `stack_usable_bytes` / `stack_guard_bytes` / `stack_total_footprint` / `stack_bottom` / `stack_guard_base` | the geometry (guard base = the lowest mapped address) |
| `enum StackFault { None, GuardPage, BadOffset }` | the probe verdict, with `stack_fault_name` (serial logs) and `stack_fault_code` (0/1/2, declaration order — enums are matched, not compared) |
| `stack_offset_valid` / `stack_classify_offset` | the non-panicking classification (offset in [1..size] usable; size < offset ≤ size + guard = the guard page) |
| `stack_probe` / `stack_try_probe` | the write model: the address, or the guard-page panic ("stack overflow: guard page hit at offset N"); the try form returns -1 instead of panicking |
| `stack_remaining` / `stack_has_overflow` | the high-water predicates (remaining refuses offsets past the region) |
| `stack_frame_cost` / `stack_chain_bytes` / `stack_max_depth` / `stack_chain_fits` | the cost model, mirroring the checker's estimator constants exactly, so plans and proofs agree |
| `stack_plan_new` | workload → config: chain + one unconditional slack page (interrupt frames, ABI padding), rounded to pages |
| `stack_canary_value` / `stack_canary_ok` | the 0x5A5A..A5 canary (6510615555426900570): a disturbed slot is caught before the corrupted value matters |

The probe functions are the software twin of the MMU: a write at
`top - offset` is classified the same way hardware classifies the
address, and the guard verdict panics with the message a fault
handler would print — routed through the Stage 81 panic path, so a
`#[panic_handler]` sees "stack overflow: guard page hit at offset N"
like any other kernel fault.

### 36.3. The runtime: deterministic task stacks + a real guard page

In the C backend, every spawned task thread (`spawn`,
`async_spawn`, `async_spawn_stream`, `async_spawn_take`,
`stream_merge`) is created through `hl_thread_start`, which pins
`pthread_attr_setstacksize` to `HL_TASK_STACK_BYTES` (1 MiB usable)
and `pthread_attr_setguardsize` to `HL_TASK_GUARD_BYTES` (4 KiB) —
glibc maps the guard PROT_NONE, so the first overflowing write takes
a fault on the guard instead of silently corrupting whatever mapping
the allocator placed next to the stack. Two properties fall out:

- **Determinism.** The glibc default stack varies with
  `ulimit -s`, the ABI, and the machine; N tasks now occupy exactly
  N × (1 MiB + 4 KiB) of stack on every host, so task memory
  footprint is reproducible.
- **Containment.** An overflow is a fault at a known address (the
  guard page), not a write into a neighbor — the runtime twin of
  the `stack_probe` contract, and the same shape a kernel gives an
  IRQ stack.

A task whose Halis-level crate passed `#![stack_size(N)]` for
N ≤ 1 MiB cannot reach the guard through HLS code (its worst chain
is proven smaller than the budget, which is smaller than the
stack); the guard therefore guards against the things the static
analysis does not model — a C-library call's internal recursion,
an FFI callee's frames, a broken `asm!` block. The freestanding
backend keeps the Stage 77 `__builtin_trap` panic semantics: the
bootloader owns the boot stack, and placing a task's guard pages is
exactly the kernel work `core.stack` lets its author plan.

### 36.4. What the tests prove

`tests/ok/feat_stage82_stack.hls` runs 69 numbered assertions on the
interpreter AND as a native binary (hosted and `-nostdlib`): page
arithmetic, config geometry + the passing side of every validation,
offset classification (including the boundaries 1 / size / size+1 /
guard end), probes and predicates, the frame-cost and chain math,
planning (including depth 0), fault names + codes, the canary pair,
and a stack plan feeding a Stage 81 `PanicLog`. `make
stackguard-acceptance` (7 sections) proves the enforcement paths:
the hosted demo hits the guard on both backends (marker, exit 101),
the ok-test is clean in `no_std` / `freestanding` / bare-hosted
modes, `--audit` reports the verified worst chain, the three fail
programs (over-budget, recursion cycle, per-fn frame bound) are
rejected by boot AND the self-hosted checker with the same messages,
and a spawn-using program's emitted C carries
`hl_thread_start` / `HL_TASK_STACK_BYTES` /
`pthread_attr_setguardsize` (the guard, really there).

## 37. Inline-asm register constraints: clobber, input, output (Stage 83 — v0.102.0-alpha)

Stage 83 makes every `asm!` register binding honest. Stage 27 shipped
the four operand forms (`in` / `out` / `inout` / `late_out`) and the
option list; what it did NOT own was the register file. A named
register was a string that mostly translated to a GCC constraint
letter, sub-registers were accepted and silently widened, `r8`–`r15`
reached GCC as an invalid constraint string, floats lowered to the
general-purpose class, and there was no way to say which registers an
asm block destroys beyond the blanket `cc`/`memory` defaults. Kernels
are written at exactly this boundary — a syscall stub that forgets
`rcx`/`r11` is a miscompile the linker will never catch — so the
register file is now a checked fact, enforced identically by both
compilers, and modelled as a pure-HLS library (`core/asm.hls`) that
kernel build tooling can query at plan time.

### 37.1. The language: explicit clobbers + exact register bindings

The `clobber(...)` clause joins the operand list (anywhere in it,
like `options`, at most once per `asm!`):

```hls
# The x86-64 write(1, buf, n) syscall, register-exact:
let mut ret: int = 1                  # SYS_write, seeded into rax
asm!("syscall",
     in("rdi") fd, in("rsi") buf_addr, in("rdx") n,
     inout("rax") ret,
     clobber("rcx", "r11"),           # the syscall instruction's destroys
     options(nomem))                  # "cc" stays: syscall rewrites RFLAGS
```

The rules, each a compile error with the same message from both
compilers:

- **A named register binds exactly that register.** `int` and `bool`
  operands require a 64-bit general-purpose name (`rax`..`r15`);
  `in("eax") x` is rejected — `asm! register 'eax' is 32-bit; an int
  operand needs a 64-bit register — use 'rax'` — instead of silently
  widening to GCC's `a` class (which binds RAX for a 64-bit operand,
  so the source lied about the hardware). `float` operands require
  SSE: the bare `reg` class lowers to the `x` class, or a specific
  `xmm0..xmm15` name pins the register. x87/MMX classes (`f`, `t`,
  `u`, `y`) and GP classes for floats, SSE names/classes for ints,
  and integer-immediate classes for float immediates are all rejected
  with the class named.
- **The compiler owns `rsp` and `rbp`.** Neither the stack pointer
  nor the frame pointer — in any spelling (`esp`, `sp`, `spl`, `ebp`,
  `bp`, `bpl`) — can be bound to an operand or named as a clobber.
  An asm block that moves `rsp` must restore it before falling
  through; declaring the clobber is a lie the compiler would believe
  at the cost of every stack reference it emits.
- **One register, one operand.** Two operands bound to the same
  physical register are rejected — GCC would conflate them into one
  allocation slot and the template would read/write a single
  register while the source named two values.
- **The clobber list may not overlap a bound operand.** A clobber
  entry says "this register is destroyed and holds nothing of
  yours"; rebinding it as an input would read a destroyed value, as
  an output would be overwritten. The analysis runs on physical
  identity (rax/eax/ax/al/ah are one register — deliberately
  conservative, `al`+`ah` overlaps too) and on the six fixed
  single-register GCC letters (`a`/`b`/`c`/`d`/`S`/`D` →
  rax/rbx/rcx/rdx/rsi/rdi). The message quotes the operand:
  `asm! clobber 'rax' overlaps operand 0 (in "rax") — a clobbered
  register cannot also be bound to an operand`.
- **The list itself must be well-formed.** Every entry is a full
  64-bit GP or SSE name (narrow spellings are rejected — GCC treats
  a sub-register clobber as clobbering the whole register and newer
  GCC rejects the narrow form), duplicates are rejected (exact or
  alias), and `cc`/`memory` are banned outright: flag and memory
  effects are controlled by `options(preserves_flags)` /
  `options(nomem)`, the default list always contains both, so an
  explicit entry is either redundant or contradictory. (Banning them
  also kills a real miscompile shape: `clobber("memory")` without
  `nomem` emitted `"memory", "memory"`.)

### 37.2. The lowering: local register variables

Three operand shapes cannot be expressed as a GCC constraint letter,
and all three now lower through GCC **local register variables**:

- `r8`–`r15`: GCC has no single-letter constraint for the extended
  registers (Stage 27 passed the string `"r8"` into the constraint
  position, which GCC rejects as an unknown constraint — extended
  registers were unusable).
- Pinned `xmm0..xmm15`: floats bound to a specific SSE register.
- Every `bool` operand bound to a named register: the C `bool` is
  one byte wide, so the letter path (`"a"` with a bool operand)
  would bind AL — the byte form — instead of the named 64-bit
  register.

The emitted shape:

```c
register int64_t hl_asm_r_1 __asm__("r10") = (int64_t)(u_a);
__asm__ __volatile__("add %1, %0" : "+r"(hl_asm_r_1) : "r"(hl_asm_r_7) : "cc", "rcx");
u_a = hl_asm_r_1;    /* writeback — automatic, any lvalue form */
```

Outputs write back through ident lvalues (`x = t;`), field lvalues
(the base expression is materialised once into a C temp so a call in
the base cannot double-evaluate), and list-index lvalues — the typed
stack-list setter (`hlc_ss_*`) for stack-allocated lists, which also
fixes the Stage 27 index path panicking on
"stack-allocated list used as a value", and `hl_list_set` + boxing
otherwise. Bool writebacks convert (`u_hit = (t != 0);`).

### 37.3. The `core.asm` surface

| Item | Role |
|------|------|
| `asm64_gp_regs()` / `asm64_sse_regs()` | the 16 GP names in canonical order, the 16 SSE names |
| `asm_reg_width(name)` | 8/16/32/64 for every x86-64 spelling (`r9d` = 32, `ah` = 8), 0 for unknown names |
| `asm_reg_base(name)` | physical identity: `"eax"` → `"rax"`, `"r9d"` → `"r9"`, `"ah"` → `"rax"`, `""` for non-registers |
| `asm_regs_overlap(a, b)` | same physical register (conservative: `al`+`ah` overlaps) |
| `asm_valid_clobber(name)` | a full 64-bit GP/SSE name; never sp/bp; never `cc`/`memory` (option-controlled) |
| `enum AsmFault { None, BadName, SubRegister, StackPointer, FramePointer, DuplicateClobber, OverlapsOperand, ReservedClobber }` | the validator's verdict, with `asm_fault_name`/`asm_fault_code` (the core.stack convention) |
| `asm_clobber_fault(cs)` / `asm_operand_conflict(reg, cs)` | the compiler's clobber rules as a pure query — first fault wins in the compiler's check order; the conflict lookup returns the offending entry |
| `asm_sysv_arg_reg(n)` / `asm_sysv_ret_reg()` / `asm_sysv_fp_ret_reg()` | the SysV call ABI: rdi rsi rdx rcx r8 r9; rax; xmm0 |
| `asm_syscall_arg_reg(n)` | the syscall ABI — argument 4 is **r10**, not rcx (rcx is destroyed by the instruction) |
| `asm_syscall_clobbers()` | `["rcx", "r11"]` — the architecture-defined destroys of `syscall` |
| `asm_caller_saved()` / `asm_callee_saved()` | the preservation sets (9 / 6 registers) |
| `asm_regs_to_save(used)` | what an ISR/trampoline stub must push (callee-saved ∩ used, deterministic order) |
| `asm_scratch_reg(avoid)` | the first caller-saved GP register not spoken for — the safe scratch for a stub whose clobbers are fixed |
| `asm_check_invariants()` | a boot-time self-test of the whole module (0 = self-consistent) |

The module is import-free — zero `core.*` dependencies, like
`core/stack.hls` — so it is importable from `#![no_std]` and
`#![freestanding]` crates identically. It exists so the register
facts the compiler enforces are queryable where kernels plan:
a test can assert a stub's clobber list equals
`asm_syscall_clobbers()` before the kernel ever boots.

### 37.4. What the tests prove

`tests/ok/feat_stage83_asmreg.hls` runs 67 numbered assertions on
the interpreter AND as a native binary (hosted and `-nostdlib`):
width and base lookup for every spelling class, overlap analysis,
the SSE classification, the clobber validator (every fault plus the
clean list), operand conflicts, the SysV-vs-syscall ABI tables
(including the rcx→r10 argument-4 move), the preservation sets, the
stub helpers, and the module self-check — while declaring (never
executing) asm blocks with clobber lists, 64-bit names, an extended
register, a pinned XMM operand and a bool through a named register.
`make asmreg-acceptance` (7 sections) proves the enforcement paths:
valid syscall-shaped/xmm/bool programs are accepted by both
checkers; every diagnostic class (width, SSE/GP mismatch, x87,
immediate-float, sp/bp, operand overlap, alias duplicate, stack-
pointer clobber, sub-register clobber, reserved `cc`/`memory`,
double clause, non-string entry) fires with its exact message from
boot AND the self-hosted compiler; the demo compiles `-Werror`,
runs `DEMO OK` with the register-variable lowering visible in the
emitted C (`__asm__("r10")`, `"+a"` on the syscall stub, explicit
clobber lists); and `--audit` reports every `core.asm` function
pure. Bootstrap self-compilation remains byte-identical.

---

## 38. Linker-script integration + custom sections (Stage 84 — v0.103.0-alpha)

A kernel is not just code: it is a *placement*. The boot stub has to
sit where the firmware will jump, the hot ISR path wants its own cache
line, the device window must be non-executable, and the image needs a
`.bss` the loader knows to zero. The Halis C backend already emits
`__attribute__((section(...)))`; Stage 84 lets a Halis program *say
so*, and — the part that actually matters — proves the linker will
honour it.

### 38.1. `#[section("NAME")]` and `#[align(N)]`

```halis
#![freestanding]
#![link_script("link.ld")]

#[section(".text.boot_stub")]
#[align(4096)]
fn boot_stub() -> int { ... }          # the firmware jumps here

#[section(".text.isr_stub")]
fn isr_stub(frame: list[int]) -> void { ... }   # IRET-compatible

#[section(".device.rng")]
fn rng_read() -> int { ... }            # mapped NX, page-aligned
```

The lowering is one C attribute, on the prototype and the definition:

```c
__attribute__((aligned(4096))) __attribute__((section(".text.boot_stub"), used)) int64_t usf_boot_stub(void);
```

`used` is load-bearing, not decoration. A function parked in a section
of its own is usually one nothing in the image *calls* yet — a boot
stub, a trap handler, a cold path only hardware reaches — and
`--gc-sections` (which every freestanding link in this repo uses)
would drop exactly the code the author went out of their way to place.

**The name rules.** A section name is 1..=64 bytes; the first byte is
`.`, `_` or a letter; the rest are letters, digits, `.`, `_`, `$`; `..`
is rejected; and the six compiler-owned output sections (`.text`,
`.rodata`, `.data`, `.bss`, `.halis.metadata`, `.halis.sections`) may
not be named. The character class is deliberately tiny: it makes the
name safe to splice into a C string literal — no quote, no backslash,
no newline, no control byte — and safe to hand to a linker as an
input-section wildcard. A digit may not lead, because `0text` reads as
a linker expression rather than a section name.

**The alignment rules.** `#[align(N)]` takes a power of two in
`1..=65536`, once per function. gcc's `aligned` attribute silently
*rounds* a non-power-of-two up, so accepting one would place the
function at an alignment the author never asked for. The rule is
stated, not approximated.

**Conflicts.** `#[section]` and `#[inline(always)]` are mutually
exclusive in either order — an always-inlined function emits no body to
place. A second `#[section]` or a second `#[align]` on one function is
a merge mistake, not a second placement. The argument must be a string
*LITERAL*: the linker needs the name long before any runtime value
exists, so a computed name is meaningless rather than merely
unsupported at run time.

### 38.2. `#![link_script("link.ld")]` — and what it proves

Naming the script turns two silent failures into compile errors:

1. **A section the script never places.** `ld` treats the function as
   an orphan input section, parks it at an address the author never
   asked for, or — with `--gc-sections` and no `KEEP` — drops it.
2. **A script that is not there**, or has no `SECTIONS` block, so the
   whole `-T script` link silently does not happen.

The path resolves relative to the *entry file's* directory, not the
process cwd. The compiler reads the script at check time, extracts
every output-section name and every input-section pattern inside its
`SECTIONS` block, and requires each `#[section]` to be covered under
GNU ld's matching rule: a pattern ending in `*` matches by **prefix**,
anything else matches exactly. So `KEEP(*(.text .text.*))` covers
`.text.boot_stub` and `.text.isr_stub`; an output section named
`.device` covers `.device` but not `.device.rng`.

```halis
#[section(".elsewhere.thing")] on function 'elsewhere' is not placed
by the linker script place.ld — the linker would discard the function.
Add an output section that matches it (e.g. `.elsewhere.thing : {
KEEP(*(.elsewhere.thing)) }`).
```

Without `#![link_script]` the attribute is still emitted and a
`#[section]` still works — the placement is simply gcc's default
script's, not yours. `--audit` says which of the two you have.

### 38.3. The `core.section` module

The rules above, plus the layout arithmetic, as values a kernel author
can call at build time (`core/section.hls`, pure, `no_std`- and
freestanding-clean):

| Surface | What it answers |
|---|---|
| `section_check` / `section_check_why` | the `#[section]` name verdict, as a stable `SectionFault` lattice (None/Empty/TooLong/BadStart/BadChar/DoubleDot/Reserved) — the compilers reject the same inputs with the same reasons |
| `section_align_ok` | the `#[align(N)]` rule |
| `section_kind` + `section_kind_{has_bytes,writable,executable}` | what a name implies about page flags: `.text.boot` is executable and never writable; `.bss` occupies a real VMA but contributes no file bytes |
| `parse_link_script` | the output sections of a script, in order, with their `ALIGN`, `(NOLOAD)`, pinned address and input patterns |
| `script_covers` / `script_placed_patterns` | which output section swallows a given input section |
| `layout_image` | the placement solver: honour `ALIGN`, jump to a pinned `. = addr`, advance the counter, and return `Err(Overlap)` when two sections claim the same bytes |
| `layout_load_end` / `layout_file_bytes` | what the image costs, versus where the loadable part ends |
| `layout_contains` / `layout_section_at` | "is this pointer inside my image, and in which section" — the check a kernel makes before honouring one a bootloader handed it |
| `layout_render` | an `ld -Map`-shaped report |

Two properties worth stating. First, a `NOLOAD` section may share
bytes with its neighbour on purpose — a guard page is carved out of the
space after `.bss` — so the overlap check only compares file-bearing
sections. Second, the scanner is **comment-aware in place**, not
strip-then-scan: HLS strings are immutable, so building a second copy
by appending one character at a time copies the whole prefix each time
and reading a 1-KiB script that way allocates on the order of a
megabyte — more than the entire 1-MiB freestanding bump arena. A
kernel author reading its own script in early boot would trap.

The compilers carry the same reader: `boot/linkerscript.py` for Stage-0
and `src/hlc/linkerscript.hls` for the self-hosted compiler, both
answering "is this section placed?" identically.

### 38.4. What the tests prove

`tests/ok/feat_stage84_section.hls` runs 114 numbered assertions on the
interpreter AND as a native binary (hosted and `-nostdlib`): the whole
name/alignment validator including the 64-byte boundary, the section
kinds and their flag predicates, the script reader (order, `ALIGN`,
`(NOLOAD)`, a pinned address, input patterns, comments, an unterminated
comment, a script with no `SECTIONS`), GNU ld prefix matching, the
placement solver including a pinned jump and a 4-KiB `ALIGN` round-up,
and every fault it can report (`Overlap`, `BadAlign`, `NegativeSize`).

`make link-acceptance` (7 sections) proves the enforcement paths: both
compilers reject all 13 fail programs with *byte-identical*
diagnostics; the C carries `section(..., used)` and `aligned(N)` on
both the prototype and the definition and still links `-Werror`; a
covered `#[section]` compiles, links and runs under the shipped
`link.ld`; the freestanding reader links `-nostdlib` inside the 1-MiB
arena; `hlfmt` round-trips both attributes and `hllint` stays clean;
and `--audit` / `--opt-stats` report the script, the named sections and
the annotation counts. Bootstrap self-compilation remains
byte-identical.

---

## 39. Multiboot2 + Limine boot protocol headers (Stage 85 — v0.104.0-alpha)

A firmware finds a kernel the same way it finds any other kernel: it
scans the loaded image for a known byte pattern. Multiboot2 looks for
the 32-bit magic `0xE85250D6` on an 8-byte boundary; Limine looks for a
request list delimited by its own two-word end marker. Neither is
reachable by a call, and neither is produced by "just writing the
struct" — so Stage 85 adds both halves: the crate attribute that makes
the C backend emit the header, and `core/boot.hls`, the shapes a kernel
*receives*, as values.

### 39.1. `#![boot_header(multiboot2 | limine)]`

```halis
#![freestanding]
#![link_script("link.ld")]
#![boot_header(limine)]

fn main() -> int { ... }      // the C backend synthesises `_start`
```

The attribute takes a bare **protocol identifier** (not a string — a
protocol is a name, not a path), may appear **once** per crate, is
**entry-file-only** like every other crate attribute, and **requires
`#![freestanding]`**: a hosted image is linked with a libc that owns
`_start`, so a boot header in it is decoration rather than something a
firmware could ever load.

**Multiboot2.** A `const volatile` blob in `.multiboot_header`:

```c
__attribute__((section(".multiboot_header"), used, aligned(8)))
const volatile hl_mb2_hdr hl_mb2_header = { 0xE85250D6u, 0u, 40u, 397258498u };
/* the module tag (type 3, size 16, start = end = 0) and the end tag */
```

The length is fixed at 40 bytes (16 fixed + 16 module + 8 end), and
the checksum is the negated 32-bit sum of magic + architecture +
length, so **all four fields sum to zero** — which is the entire
validity test, and is why the compiler computes the value rather than
asking you to. The module tag carries `start == end == 0`: the
standard way to say "the kernel is not itself a module", with the image
bounds read from the ELF program headers instead.

**Limine.** A `const volatile uint64_t hl_limine_reqs[]` in
`.limine_requests`: the start delimiter, a base-revision-3 tag, the
five requests a kernel asks for (memory map, framebuffer, RSDP, HHDM,
stack size), and the end delimiter. Every request ID is four 64-bit
words whose first two are common to the protocol, and **more than half
the remaining words are above 2^63** — hence the `UL` suffix on every
literal in the emitted C. A typo in an ID is the worst kind of boot
bug: the bootloader silently ignores the request and the kernel comes
up with no memory map.

`const volatile` and `used` are both load-bearing. `const` because the
firmware never writes it; `volatile` so the compiler does not elide the
loads as dead stores to a const object; `used` because **nothing in the
image calls it** — under `--gc-sections`, which every freestanding link
in this repo uses, an un-`used` header is simply gone.

### 39.2. The Stage 84 integration

The header is just another output section, and it is the one section a
firmware will not find unless the script KEEPs it — a bootloader that
scans the image and finds nothing refuses to boot, with no diagnostic
anywhere. So the Stage 84 placement check is pointed at it: a crate
that declares both `#![link_script]` and `#![boot_header]` and whose
script does not place the header's section is a **compile error**
naming the section and the output section to add. The shipped
`link.ld` places (and KEEPs) both `.multiboot_header` and
`.limine_requests`.

### 39.3. `core/boot.hls` — the received side

Pure HLS, `core.*` imports only, no effects. A boot protocol is
exactly where a kernel is most likely to be wrong and least likely to
find out, so every constant and every predicate is callable from a
test.

**Multiboot2.** `mb2_magic()`, `mb2_arch_i386()`, the tag-type
lattice with `mb2_tag_name`, `mb2_tag_is_information` (the header is
what the loader scans for; the information structure is what EBX points
at — conflating them is a classic early-boot bug), `mb2_field32` /
`mb2_sum32` / `mb2_checksum` (the 32-bit rule), `mb2_header_ok`,
`mb2_header_new` (which appends the mandatory end tag itself),
`mb2_tag_at` / `mb2_tag_count` / `mb2_tag_present`, and
`mb2_header_well_formed` (the end tag is present, every tag's size
covers its prefix, and the declared length equals the sum of the parts).

**Limine.** Every request ID as a `list[int]` of four words
(`limine_id_memmap`, `..._framebuffer`, `..._rsdp`, `..._hhdm`,
`..._stack_size`, `..._bootloader_info`), the start and end
delimiters, `limine_base_revision` / `limine_base_revision_honoured`
(the loader zeroes the third word when it honoured the request and
leaves it when it did not — the spec says the *executable* is the one
that must notice), `limine_id_is` (exact four-word comparison: a
prefix match would collide with every feature a future loader adds),
`limine_feature_find` / `_requested` / `limine_features_unique` (a
loader must REFUSE an image carrying two of the same request) and
`limine_features_terminated` (the end marker counts only in the last
two words).

**One memory-map model for two wire encodings.** E820 numbers
"available" as 1; Limine numbers "usable" as 0. `mem_kind_from_e820`
and `mem_kind_from_limine` are the only two functions that know that,
and both land in the same `MemKind` — so a physical-memory manager
never sees a wire value. The predicates are the ones a kernel actually
asks: `mem_usable_bytes`, `mem_reclaimable_bytes`, `mem_largest_usable`,
`mem_find_usable` (the first region that **satisfies** the request, or
`-1` — never the best partial fit), `mem_count_usable` (zero means the
map was misparsed, and is the single most useful assertion a kernel can
make about its boot information), `mem_contains`, `mem_sorted` (both
protocols *guarantee* sorted order, which is what licenses a binary
search), `mem_sort` for a firmware that breaks it, and
`mem_no_overlaps` — which compares only **file-bearing** regions,
because a framebuffer sharing a page with a usable region is normal
while two usable regions sharing a byte is a double allocation waiting
to happen.

`mem_kind_reclaimable` is *ordered*: `Usable` is free now;
`BootloaderReclaimable` and `AcpiReclaimable` only after the loader and
ACPI are done (taking the former on day one overwrites the page tables
the kernel is running on); `AcpiNvs` is battery-backed settings the
user expects to survive a reboot and is never reclaimable; `BadMemory`
is unreliable; and `Unknown` — an E820 type above 5 — is never
reclaimable, because a kernel that guessed would hand the firmware's
own scratch to its page allocator.

**The framebuffer.** `fb_new` is the **checked** constructor (for a
mode you programmed or a test built — a pitch narrower than one line
panics); `fb_raw` is the **unchecked** one, for firmware bytes a parser
must not die on, and `fb_geometry_ok` is what rejects them. `fb_size`
is `pitch * height`, never `width * height * bpp / 8`, because a mode
may pad every line — a kernel that computed the stride from the width
would shear the whole image.

**The report.** `boot_report(protocol, regions, framebuffer, fb_ok)`
is what a kernel prints once, at entry: the protocol, the usable-region
count, the largest usable region, the full map, the framebuffer
geometry — and a `WARNING:` line for each way the map can be unusable
(unsorted, overlapping usable regions, no usable memory at all).

### 39.4. `hex64` renders a word, not a number

`core.section`'s `hex64` prints a signed int64 as **16 hex digits**,
always. A boot protocol is full of unsigned 64-bit words, and more than
half the Limine request IDs are above 2^63 — so they arrive in an
`int` as negative numbers, and printing them signed would send the
reader looking for a word the spec says is `0xe304...`. The nibbles
are read out of the bit pattern with `int_shr` (a logical shift, so the
walk terminates after exactly 16 steps for a negative value too) rather
than by negating and complementing: a value of int64's *minimum* cannot
be negated at all, and complement-then-increment is two ways to get the
same answer wrong.

### 39.5. What the tests prove

`tests/ok/feat_stage85_boot.hls` runs 157 numbered assertions on the
interpreter AND as a native binary (hosted and `-nostdlib`): the
Multiboot2 constants and the 32-bit checksum rule, header construction
and the mandatory end tag, tag walking and the malformed-tag cases, all
six Limine request IDs and both delimiters with their exact-match
semantics, the base-revision round trip, both memory-map wire encodings,
reclaimability, every physical-memory-manager question, the sorted /
overlapping / repair paths, the framebuffer (checked and unchecked),
`hex64`'s full-width rendering, and the boot report's warnings.

`make boot-acceptance` (7 sections) proves the enforcement paths: both
compilers reject all five fail programs with *byte-identical*
diagnostics; the emitted C carries the magic, architecture, 40-byte
length, checksum, module and end tags, and both Limine delimiters and
request IDs; a **real `-T link.ld` link** is inspected with `objdump`
and the header really is in its section, really is 8-byte aligned, and
really starts with the protocol's own bytes; a linker script that drops
the header section is a compile error while the shipped one is
accepted; the four checked constructors are *accepted* by the checker
and panic at run time; `boot_demo` runs identically on the interpreter
and natively; `boot_kernel` links as a freestanding image carrying
`.limine_requests`; and `--audit` names the header and its section on
both front-ends. Bootstrap self-compilation remains byte-identical.

---

## 40. `core.interrupt` — IDT/GDT declaration (Stage 86 — v0.105.0-alpha)

A kernel enters a handler through an IDT gate, and a gate holds a
**32-bit offset**. That single fact shapes this stage: an IDT cannot be
a static initialiser, because a handler's address is not a constant
expression — which is why every kernel in every language fills its IDT
by hand at boot. Stage 86 moves the *declaration* into the source and
the *arithmetic* into a testable module.

### 40.1. `#[irq_handler(N)]`

```halis
#[irq_handler(14)]
fn on_page_fault(frame: list[int]) -> void { ... }

#[irq_handler(8)]
fn on_double_fault(frame: list[int]) -> void { ... }
```

The bare `#[irq_handler]` (Stage 28) still declares only "this is an
interrupt handler" and is unchanged. The vector form additionally
declares **which** vector, and the compiler then does three things it
could not do before:

* **Checks the declaration.** `N` must be 0..255 (an IDT has 256
  entries; a loader that clamped it would install the handler somewhere
  nobody expected). A function may carry one, and **no two functions may
  claim the same vector** — two handlers for 14 means one of them is
  never entered, and the CPU's gate table can hold only one.
* **Emits a save/restore stub per vector.** `hl_irq_stub_14` is a naked
  function whose body is the classic push-all / call / unwind:

  ```asm
  pushq rax rbx rcx rdx rsi rdi rbp r8 r9 r10 r11     ; 88 bytes
  movl  $14, %eax            ; the vector, for the error-code cases
  movq  %rsp, %rdi           ; the saved frame, as the one parameter
  call  usf_on_page_fault
  addq  $88, %rsp
  iretq
  ```

  The push order is the reverse of the pop, so `add $88, %rsp` +
  `iretq` is correct: RSP on entry points at the saved RIP, and `iretq`
  consumes RIP/CS/RFLAGS from there. Callee-saved registers
  (rbx, rbp, r12-r15) are deliberately untouched — the callee preserves
  them.
* **Emits the binding table**, so a kernel calls one function at boot
  instead of writing a descriptor by hand:

  ```c
  const struct hl_irq_binding hl_idt_bindings[] = {
      { 14u, 0u, hl_irq_stub_14 },
      { 8u,  0u, hl_irq_stub_8  },
      { 0u,  0u, NULL }        /* terminator */
  };
  void hl_idt_install(const struct hl_irq_binding *b, uint32_t n, uint16_t cs);
  ```

  The installer builds the 256-entry table, loads the IDTR with `lidt`,
  and skips a row whose handler is NULL — so a reserved vector stays
  reserved. The gate's `cs` is a parameter, not a constant: the
  8259-era `0x08` is right only if the kernel's GDT says so.

### 40.2. Why a stub, and not gcc's `interrupt` attribute

gcc's `__attribute__((interrupt))` is the obvious way to express an ISR
and the wrong one for a compiler-generated body. It **rejects SSE
anywhere inside the function**, and a Halis body always uses it: the
frame arrives in a vector register, and the cleanup machinery spills
with aligned moves. A Stage 28 `#[irq_handler]` taking a `list[int]`
therefore failed to compile with

```
error: sorry, unimplemented: SSE instructions aren't allowed in an
interrupt service routine
```

So the vector form does **not** carry the attribute: the save/restore
lives in asm, where it belongs, and the Halis function is an ordinary C
function the stub *calls*. The **bare** form keeps gcc's attribute,
because a kernel that assigns no vector also supplies no stub, and has
no other way to get one.

### 40.3. The IST is its own byte

The gate's layout has a field that is easy to put in the wrong place:

```
 0-1  offset low      4     IST index (bits 0-2) + reserved
 2-3  code selector   5     gate type (0-3), zero, DPL, P
 6-7  offset mid      8-11  offset high
12-15 reserved
```

The IST lives in the gate's **own byte at offset 4**, not in the low
nibble of the type/attribute byte at offset 5 — packing it there
overwrites the **gate type**, producing a perfectly well-formed
interrupt gate that is not an interrupt gate. `core.interrupt`'s
`idt_ist_pack` / `idt_ist_unpack` / `idt_ist_byte_ok` therefore take and
return the IST byte, and the upper five bits of it are RESERVED: an IST
above 7 is undefined behaviour, not "clamped to 7".

### 40.4. `core/interrupt.hls` — the descriptor arithmetic

Pure HLS, one `core` import, no effects. A descriptor is not four
equal fields, and a kernel that encodes it wrong gets a table that
*loads* and then faults at the first far jump:

```
byte 0-1  limit[15:0]        hi bits  0-7   base[23:16]
byte 2-3  base[15:0]         hi bits  8-11  limit[19:16]
byte 4    base[23:16]        hi bits 12-15  flags nibble (AVL/L/DB/G)
byte 5    flags + limit[19:16]   hi bits 16-23  access
byte 6    access             hi bits 24-31  base[31:24]
byte 7    base[31:24]
```

Three errors the module makes impossible and the tests prove:

* **The limit's top nibble shares a byte with the flags.** Putting it at
  bits 0-3 instead of 8-11 truncates every limit above 64 KiB to
  `0xFFFF` — which still looks like a perfectly valid 64-KiB segment.
* **L is bit 6 of the flags byte** (bit 2 of its nibble), *not* bit 3,
  which is `limit[19:16]`. Reading L as bit 3 makes every 64-bit
  segment behave as 16-bit while the table still loads.
* **The class bit is `0x08` and the S bit is `0x10`.** Reading one as
  the other makes every data descriptor look like code and every code
  descriptor look like a TSS.

`gdt_check` reports all of that as a stable `GdtFault` lattice —
`NotPresent` (a `P=0` descriptor #GPs on load), `SystemSegment`,
`ClassMismatch`, `LongWithDb` (architecturally invalid), `LongWithoutCode`,
`DplOutOfRange` — so a kernel validates a table it did not build
(a firmware- or bootloader-supplied one) before loading it.

**Selectors.** `gdt_selector(index, rpl)` is `index << 3 | rpl` — the
index occupies bits 3..15, so the largest well-formed GDT selector is
`(1023 << 3) | 3 = 8187`, **not** 8191, which has the TI bit set and
therefore addresses an LDT that does not exist. `gdt_selector_ok`
rejects a TI'd selector.

**The TSS.** The packed Limine handoff form is 108 bytes plus a 16-byte
descriptor, whose bit 4 of the second byte is the **busy** bit — set by
the CPU when a task is active, and which a kernel must preserve across
a task switch or the CPU refuses the next switch. `tss_is_busy` /
`tss_set_busy` read and write exactly that bit and nothing else.

**The page-fault error code**, decoded by bit rather than by a
hand-written table: bit 0 `P`, bit 1 `W/R`, bit 2 `U/S`, bit 3 reserved,
bit 4 `I/D`. The reserved bit is checked **first**, because every other
answer is meaningless once it is set. `page_fault_is_present` is the
predicate that decides whether a fault is a first touch (handle it) or a
permissions bug (panic) — getting it backwards turns every first touch
into a bug report.

**The exception vectors** — 32 names, which of them push an error code
(8, 10-14, 17, 21; getting that wrong is not a crash, it is a handler
reading its fault address from the wrong stack slot), which are faults
and which are traps, and the convention that the **double fault's
handler runs on IST 1** because it happened when the kernel could not
even push a frame.

**The 8259 PIC.** Remap bases 0x20 / 0x28, IRQ numbering, the EOI each
interrupt needs (and that an *exception* never does), and the
`pic_remap_ok` test that catches a base inside the exception range or a
pair that overlaps.

### 40.5. What the tests prove

`tests/ok/feat_stage86_interrupt.hls` runs 159 numbered assertions on
the interpreter AND as a native binary (hosted and `-nostdlib`): every
selector bound, the access byte's class and S bits, the flags nibble,
the full descriptor round trip including a TSS base in the high byte
and a limit above 64 KiB, `gdt_check`'s six faults, the TSS busy bit
round trip, the IST field and its limit, the gate types, the 32-bit
offset split and the reachability rule, the whole page-fault decode
table, the exception vectors, and the 8259 remap.

`make idt-acceptance` (7 sections) proves the language half: the bare
form still compiles and still gets gcc's attribute, the vector form
compiles and gets a stub instead; the emitted C carries one naked stub
per vector, the push/`add $88`/`iretq` sequence, `movl $N, %eax` for
the error-code vectors, and `hl_idt_install` with its `lidt`; the
compiled object links `-Werror` and `nm` finds `hl_irq_stub_14`,
`hl_idt_bindings`, `hl_idt_install`, `hl_idt` and `hl_idtr`; all six
fail programs are rejected by both compilers with identical text; and
`interrupt_demo` produces byte-identical output on the interpreter and
natively. Bootstrap self-compilation remains byte-identical.
