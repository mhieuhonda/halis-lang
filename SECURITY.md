# Security policy — Halis (HLS)

## Support windows

Every LTS line gets **3 years of bug-fix support** and **5 years of
security support**, counted from the day its base release was cut.
The last two years are security-only: after the third anniversary a
plain bug fix is refused on the line, and after the fifth the line
is dead and takes nothing. The full policy — the phases, the
calendar, what makes a fix a security fix, what the tools refuse —
is published in [LTS.md](LTS.md) and rendered from the constants
the `hls-lts` gates enforce, re-derived on every run
(`make lts-policy-verify`). A security fix meant for backport
carries a `Security: <reason>` trailer in its commit message; the
audit judges every line's history by the day each commit landed.

The current line is `release/1.0` (base `v1.0.0`, cut 2026-10-29):
bug fixes through 2029-10-29, security fixes through 2031-10-29.

## The security model

Halis is designed so that safety is the default state, in four
layers.

### Layer 1 — Compile time

- **Effects system.** A function with no `uses` clause is guaranteed
  unable to perform any I/O — console, file, clock, command-line
  arguments, process exit, DNS, randomness, subprocess, concurrency.
  The nine effects (`IO`, `Fs`, `Clock`, `Args`, `Exit`, `Net`,
  `Rand`, `Proc`, `Conc`) are individually declared and verified
  through the call graph; the analysis is a fixpoint over the static
  call graph, so indirect calls do not escape it. (`uses IO` is a
  backwards-compatible blanket alias for the IO family only.)
- **Taint tracking.** A value wrapped as `tainted[T]` cannot reach a
  sink (`print`, `println`, `read_file`, `write_file`, `file_exists`,
  `exit`, `net_lookup`, `proc_exec`) at compile time. Command-line
  arguments arrive tainted via `tainted_args()`; file content and
  stdin arrive tainted via `read_file_tainted()` / `read_line()`.
  The value must pass through `std.sanitize` or an explicit
  `taint_unwrap()` first.
- **Explicit purity.** `fn f(...) pure` declares purity; the checker
  enforces it transitively and names the witness edge on violation.
- **Absolute static typing.** No implicit casts, no loose inference,
  no shadowing, conditions must be `bool`, every binding has a
  declared type.
- **No null, no uninitialised variables.** Every variable is assigned
  at declaration.
- **Ownership.** Use-after-move is a compile error; `drop`, `clone`
  and `take` make ownership transfer explicit.

### Layer 2 — Runtime

- 64-bit `int` arithmetic with overflow checks on `+ - * / %` and
  unary negation.
- Bounds-checked array and string access; divide-by-zero halts
  safely.
- Runtime errors are controlled `panic`s — exit code 101, no
  undefined behaviour, no silent corruption.

### Layer 3 — Structure

- **No GC, no manual free.** The generated runtime is
  reference-counted with exact retain/release at scope exit; the
  memory-stress suite verifies a flat RSS under a 256 MB
  address-space limit.
- **Data races are impossible by construction.** The Send rule set
  plus the ownership-boundary rule — sharing a variable with a task
  outside a channel is a compile error — reject every data race
  statically.
- **A confined compiler.** The compiler reads and writes nothing
  beyond the named input and output files. `--sandbox DIR`
  restricts the file builtins to DIR and refuses `extern "C"`
  blocks and `uses Proc` programs — both can escape the sandbox.

### Layer 4 — Audit

- `hlc --audit` (and the lab interpreter's equivalent) prints the
  capability and effect tree of every function — declared vs
  computed, OK or VIOLATION named per function — plus the
  taint-flow section: which functions reach each taint source and
  each sink.
- `hls-pkg audit` extends the same report across the whole
  dependency tree; the lockfile freezes the effect surface of every
  dependency, and `hls-pkg verify` re-checks content digests.
- The soundness guarantee ledger (`make guarantee-report`) pins
  fifteen guarantees with their enforcement anchors and negative
  probes, and doubles as the bug-bounty scope document.
- Signed packages (minisign/ed25519), a SHA-256-chained
  transparency log with a gossip protocol, per-release SBOMs
  (CycloneDX + SPDX) and reproducible-build verification close the
  supply chain.

## Honest limits

What Halis does **not** yet protect:

- **FFI trusts the header.** `extern "C"` is sound but a
  mis-declared extern (claiming `int` for a function returning
  `char*`) is unchecked. `hlbindgen` mitigates this by generating
  the block from the C header with `_Static_assert` ABI size checks.
- **Taint is single-label.** `tainted[str, Html]` vs
  `tainted[str, Sql]` is future work; today one sanitisation clears
  the value for every sink.
- **Deep recursion can overflow the native stack.** The interpreter
  raises a clean panic; the native binary uses the OS stack. The
  `#[tail_call]` attribute turns verified tail calls into jumps at
  any depth.
- **The proof engine is a fragment.** It reasons about interval
  arithmetic and syntactic patterns; anything outside that fragment
  keeps its runtime check even under `-O fast`.

A user who wraps every input in `taint_unwrap()` immediately
defeats the taint system — that is an explicit user choice, not a
toolchain bug.

## Reporting a vulnerability

Found a bug that makes the interpreter and the native build
disagree, that emits unsafe C, or that bypasses the taint or effect
system? That is a serious bug.

**Preferred channel:** open a private GitHub Security Advisory at
<https://github.com/mhieuhonda/halis-lang/security/advisories/new>.
The report stays private until a fix is published. Please do not
open a public issue for security reports.

- **Acknowledgement:** within 48 hours.
- **Coordinated disclosure:** a 90-day window. We publish the fix
  and a CVE (where applicable) within the window, and ask reporters
  to wait until the fix is out before public disclosure.
- **Backports:** a fix for a supported line is backported with a
  `Security: <reason>` trailer and lands in the next patch release
  of that line.

## Scope

This policy covers the toolchain itself — the compiler sources, the
generated runtime, the snapshot kit and the standard library. User
programs are governed by the model above, but the toolchain's
guarantees are only as strong as the sanitisers the user actually
calls.
