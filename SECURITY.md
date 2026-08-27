# Security policy — Hieu Louis (HLS)

## Security model of the language (v0.2)

Hieu Louis is designed so that **safety is the default state**. Three layers
of defence:

### Layer 1 — Compile time (static)
- **Effects system:** a function that does not declare `uses IO` is
  *guaranteed* to be unable to perform I/O (print, file read/write,
  command-line args, exit, clock). The analysis is a fixpoint over the static
  call graph — there is no escape through indirect calls.
- **Absolute static typing:** no implicit casts, no loose inference, no
  shadowing, conditions must be `bool`, every binding has a declared type.
- **No null / no uninitialised:** every variable is assigned at declaration.

### Layer 2 — Runtime (dynamic)
- 64-bit `int` arithmetic with overflow checks: `+ - * / %` and unary negation
  are all checked.
- Array/string access is bounds-checked; divide-by-zero halts safely.
- Runtime errors are controlled `panic`s: exit code 101, no undefined
  behaviour.

### Layer 3 — Structural (architecture)
- v0.2 arena allocation model: **there is no free instruction** → use-after-
  free / double-free are structurally impossible. (Exact ownership: Stage 8.)
- The compiler reads/writes nothing beyond the explicitly named input/output
  files.

## What v0.2 does NOT yet protect (honesty)
- No taint tracking / sandbox yet (Stage 10).
- No fine-grained capability tokens yet (Stage 9).
- Very deep recursion can overflow the native stack (Stage 11: stack probes).
- The toolchain is not yet signed (Stage 13: content-addressed packages).

## Reporting a vulnerability
Found a bug that makes the two implementations (Stage-0 vs native) produce
different results, or that emits unsafe C code? That is a serious bug. Please
open an issue with the `security` label and a minimal reproducer.

## Scope
This policy applies to the toolchain itself (`boot/`, `src/hlc.hls`, the
generated runtime). User programs written in HLS are governed by the model
above.
