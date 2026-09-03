# Security policy — Halis (HLS)

## Security model of the language (v0.7.0-alpha)

Halis is designed so that **safety is the default state**. Four layers
of defence:

### Layer 1 — Compile time (static)
- **Effects system:** a function with no `uses` clause is *guaranteed*
  to be unable to perform any I/O — console, file, clock, command-line
  args, or process exit. The five fine-grained effects (`IO`, `Fs`,
  `Clock`, `Args`, `Exit`) are individually declared and statically
  verified through the call graph. The analysis is a fixpoint over the
  static call graph — there is no escape through indirect calls.
- **Taint tracking (Stage 10-alpha, v0.7.0-alpha):** a value wrapped as
  `tainted[T]` cannot reach a sink (`print`, `println`, `read_file`,
  `write_file`, `file_exists`, `exit`) at compile time. The user must
  sanitise via `std.sanitize` or explicitly untaint via
  `taint_unwrap()`. `tainted_args()` returns the program's argv
  wrapped as `list[tainted[str]]` by default — every command-line
  input is tainted.
- **Explicit purity (`pure` keyword, Stage 9-beta):** a function
  declared `fn f(...) pure` must have no `uses` and must transitively
  call nothing effectful — enforced by the checker with a witness
  edge in the error message.
- **Absolute static typing:** no implicit casts, no loose inference, no
  shadowing, conditions must be `bool`, every binding has a declared type.
- **No null / no uninitialised:** every variable is assigned at declaration.
- **Use-after-move is a compile error (Stage 8-alpha):** the `drop`,
  `clone`, `take` primitives and a `moved` flag on every binding let
  the compiler statically reject use of a binding after ownership has
  been released.

### Layer 2 — Runtime (dynamic)
- 64-bit `int` arithmetic with overflow checks: `+ - * / %` and unary negation
  are all checked.
- Array/string access is bounds-checked; divide-by-zero halts safely.
- Runtime errors are controlled `panic`s: exit code 101, no undefined
  behaviour.

### Layer 3 — Structural (architecture)
- Arena allocation model: **there is no free instruction** → use-after-
  free / double-free are structurally impossible. Exact ownership
  (move semantics, borrow checking, end-of-arena runtime) is the
  Stage 8-beta target.
- The compiler reads/writes nothing beyond the explicitly named input/output
  files.

### Layer 4 — Audit
- `hlc --audit <file.hls>` and `boot.py --audit <file.hls>` print the
  full capability / effect tree of every function in the program
  (declared vs computed, with a clear OK/VIOLATION status per
  function). Useful for security review and supply-chain audits.
- Reserved effect names (`Net`, `Rand`, `Proc`) are recognised but
  error if used — they will be enabled in a later stage, but until
  then the compiler rejects any program that tries to use them.

## What v0.7.0-alpha does NOT yet protect (honesty)
- Full borrow checking / end-of-arena runtime not yet shipped (Stage
  8-beta). The current model uses ownership analysis + arena
  allocation; runtime memory reclamation is deferred.
- Sandboxed compile mode (a program only running inside a granted
  directory / socket set) — Stage 10-beta.
- Taint analysis report from `hlc --audit` (currently the audit flag
  shows the effects tree; taint-flow reporting will be added in
  Stage 10-beta).
- First-class capability tokens (passed as args, stored in structs) —
  future Stage 9 work.
- First-class taint labels (e.g. `tainted[str, Html]` vs
  `tainted[str, Sql]` so HTML-tainted values cannot be used in SQL) —
  future Stage 10 work.
- Very deep recursion can overflow the native stack (Stage 11: stack probes).
- The toolchain is not yet signed (Stage 13: content-addressed packages).

## Reporting a vulnerability
Found a bug that makes the two implementations (Stage-0 vs native) produce
different results, or that emits unsafe C code, or that bypasses the
taint/effect system? That is a serious bug.

**Preferred reporting channel:** open a private GitHub Security Advisory
at
<https://github.com/mhieuhonda/hieu-louis-lang/security/advisories/new>.
This keeps the report private until a fix is published. Please do NOT
open a public issue for security reports — it would expose the
vulnerability before a fix is available.

**Acknowledgement:** we aim to acknowledge reports within 48 hours.

**Coordinated disclosure:** we follow a 90-day coordinated-disclosure
window. We will publish a fix and a CVE (if applicable) within that
window; we ask that reporters wait until the fix is published before
public disclosure.

## Scope
This policy applies to the toolchain itself (`boot/`, `src/hlc.hls`, the
generated runtime). User programs written in HLS are governed by the model
above — but the toolchain's guarantees are only as strong as the
sanitisers the user actually calls. A user who wraps every input in
`taint_unwrap()` immediately defeats the taint system; that's an
explicit user choice, not a toolchain bug.
