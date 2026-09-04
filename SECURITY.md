# Security policy — Halis (HLS)

## Security model of the language (v0.30.0-alpha)

Halis is designed so that **safety is the default state**. Four layers
of defence:

### Layer 1 — Compile time (static)
- **Effects system:** a function with no `uses` clause is *guaranteed*
  to be unable to perform any I/O — console, file, clock, command-line
  args, process exit, network, randomness, subprocess, or concurrency.
  The eight fine-grained effects (`IO`, `Fs`, `Clock`, `Args`, `Exit`,
  `Net`, `Rand`, `Proc`, `Conc`) are individually declared and statically
  verified through the call graph. The analysis is a fixpoint over the
  static call graph — there is no escape through indirect calls.
  (`uses IO` is a backwards-compatible blanket alias for the IO family:
  IO + Fs + Clock + Args + Exit.)
- **Taint tracking (Stage 10, released v0.21.0-alpha):** a value wrapped
  as `tainted[T]` cannot reach a sink (`print`, `println`, `read_file`,
  `write_file`, `file_exists`, `exit`, `net_lookup`, `proc_exec`) at
  compile time. The user must sanitise via `std.sanitize` or explicitly
  untaint via `taint_unwrap()`. `tainted_args()` returns the program's
  argv wrapped as `list[tainted[str]]` by default — every command-line
  input is tainted. `read_file_tainted(path)` and `read_line()` are
  additional taint sources (file content and stdin are tainted by
  default).
- **Explicit purity (`pure` keyword, Stage 9-beta):** a function
  declared `fn f(...) pure` must have no `uses` and must transitively
  call nothing effectful — enforced by the checker with a witness
  edge in the error message.
- **Absolute static typing:** no implicit casts, no loose inference, no
  shadowing, conditions must be `bool`, every binding has a declared type.
- **No null / no uninitialised:** every variable is assigned at declaration.
- **Use-after-move is a compile error (Stage 8, released v0.19.0-alpha):**
  the `drop`, `clone`, `take` primitives and a `moved` flag on every
  binding let the compiler statically reject use of a binding after
  ownership has been released. The generated C runtime is
  reference-counted with exact retain/release at scope exit — the
  memory-stress acceptance test runs 500k allocation rounds with
  RSS delta = 0, enforced under a 256 MB `ulimit -v` in the test suite.

### Layer 2 — Runtime (dynamic)
- 64-bit `int` arithmetic with overflow checks: `+ - * / %` and unary negation
  are all checked.
- Array/string access is bounds-checked; divide-by-zero halts safely.
- Runtime errors are controlled `panic`s: exit code 101, no undefined
  behaviour.

### Layer 3 — Structural (architecture)
- Reference-counted runtime with exact free at scope exit (Stage 8,
  v0.19.0-alpha — the end of the arena model): use-after-free /
  double-free are structurally impossible because there is no manual
  `free`, and the codegen's ownership-analysis pass inserts exact
  retain/release on every control-flow path. The memory-stress
  acceptance test verifies RSS stays flat under a 256 MB address-space
  limit.
- Data races are impossible by construction (Stage 16, v0.27.0-alpha):
  the Send rule set plus the ownership-boundary rule (sharing a
  variable with a task outside a channel is a compile error) means
  the type system rejects every data race at compile time.
- The compiler reads/writes nothing beyond the explicitly named
  input/output files. `--sandbox DIR` (Stage 10 release) further
  restricts the four file builtins to DIR and rejects `extern "C"`
  and `uses Proc` programs (both can escape the sandbox).

### Layer 4 — Audit
- `hlc --audit <file.hls>` and `boot.py --audit <file.hls>` print the
  full capability / effect tree of every function in the program
  (declared vs computed, with a clear OK/VIOLATION status per
  function), plus the taint-flow section listing which functions call
  each taint source and each taint sink. Useful for security review
  and supply-chain audits.
- The active effect set is `IO, Fs, Clock, Args, Exit, Net, Rand, Proc,
  Conc`. `Conc` (Stage 16) is the concurrency effect — declare
  `uses Conc` for `spawn` / `join` / channels / `select`.
- `hlc --audit` and `boot.py --audit` accept an additional `--sandbox`
  flag for the sandboxed-compile mode (Stage 10 release).

## What v0.30.0-alpha does NOT yet protect (honesty)
- First-class capability tokens (passed as args, stored in structs) —
  future work.
- First-class taint labels (e.g. `tainted[str, Html]` vs
  `tainted[str, Sql]` so HTML-tainted values cannot be used in SQL) —
  future Stage 10+ work.
- Very deep recursion can overflow the native stack (a future stage
  may add stack probes); the interpreter raises a clean `panic` under
  RecursionError, but the native binary still uses the OS stack.
- The transparency log of `hls-pkg` is integrity-protected
  (SHA-256-chained) but is NOT yet cryptographically signed (a future
  stage may add minisign / sigstore integration).
- The native FFI (`extern "C"`) is sound but trusts the C header's
  types: a mis-declared `extern` (e.g. claiming `int` for a function
  that returns `char*`) is unchecked and will misbehave at runtime.
  `hlbindgen` mitigates this by generating the `extern` block from the
  C header directly, with `_Static_assert` size checks in the ABI
  header.
- The proof engine (`hlprove` / `-O fast`) is sound but only reasons
  about interval arithmetic and a few syntactic patterns. Anything
  outside that fragment (non-linear arithmetic, non-numeric
  invariants, heap-shape properties) is not yet proven; `-O fast`
  conservatively keeps the runtime check in those cases.

## Reporting a vulnerability
Found a bug that makes the two implementations (Stage-0 vs native) produce
different results, or that emits unsafe C code, or that bypasses the
taint/effect system? That is a serious bug.

**Preferred reporting channel:** open a private GitHub Security Advisory
at
<https://github.com/mhieuhonda/halis-lang/security/advisories/new>.
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
