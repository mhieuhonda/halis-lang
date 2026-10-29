<div align="center">

<img src="docs/halis-logo.jpg" alt="The Halis logo" width="190">

# Halis

**A secure, self-hosting, native-first programming language.**

Safety is the default. Effects are declared. Nothing hides.

[![Release](https://img.shields.io/github/v/release/mhieuhonda/halis-lang?color=228be6)](https://github.com/mhieuhonda/halis-lang/releases)
[![License](https://img.shields.io/badge/license-MIT-40c057)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-halis--lang-ae3ec9)](https://github.com/mhieuhonda/halis-lang#readme)
[![Updated](https://img.shields.io/github/last-commit/mhieuhonda/halis-lang?label=updated&color=fab005)](https://github.com/mhieuhonda/halis-lang/commits)

[Specification](SPEC.md) · [Releases](RELEASES.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [LTS policy](LTS.md)

</div>

---

## Why Halis

Halis exists because of one belief: **safety is not optional, and
performance is not the price of safety.** A function without a
`uses` clause is *guaranteed pure by the compiler* — not by
convention, not by review, by construction. Every arithmetic
operation is checked, every array access is bounds-checked, and the
release binary is a native executable with no interpreter, no
garbage collector and no runtime hiding underneath.

```hls
# No `uses` clause: the compiler guarantees this function is pure.
fn sum_squares(n: int) -> int {
    let mut total: int = 0
    let mut i: int = 0
    while i < n {
        total = total + i * i
        i = i + 1
    }
    return total
}

# I/O is a declared capability — remove `uses IO` and this will not compile.
fn main() -> int uses IO {
    println("sum = " + sum_squares(100).to_str())
    return 0
}
```

Halis is written in Halis. The compiler is 100% self-hosted, and the
bootstrap proves a fixed point on every build: the compiler built
from the committed snapshot must re-derive that snapshot
**byte for byte**.

## The guarantees

1. **I/O is a declared effect.** Nine effects — `IO`, `Fs`, `Clock`,
   `Args`, `Exit`, `Net`, `Rand`, `Proc`, `Conc` — are declared on
   the function that needs them and verified through the whole call
   graph. There is no escape through indirect calls.
2. **Every operation is checked.** Integer overflow, division by
   zero and out-of-bounds access halt safely. There is no switch
   that disables the checks; the only way a check disappears is a
   formal proof that it is dead.
3. **No null. No uninitialised state, no globals, no hidden
   anything.** Everything is explicit so that it can be audited.
4. **Memory safety without a GC.** Use-after-move is a compile
   error; `clone()` deep-copies; the runtime frees exactly at scope
   exit. The memory-stress suite runs hundreds of thousands of
   allocation rounds with a flat RSS, under a 256 MB address-space
   limit in CI.
5. **Data races are impossible by construction.** Values only cross
   task boundaries through channels; sharing a variable with a task
   outside a channel is a compile error.
6. **Untrusted input is tainted by default.** `tainted[T]` values
   cannot reach a sink — print, files, exit, DNS, subprocess —
   until they pass through `std.sanitize` or an explicit untaint.
7. **The whole program is auditable.** `hlc --audit` prints the
   capability and taint-flow report of every function: declared vs
   computed, with the violation named.

Each guarantee is pinned in a machine-checked ledger — fifteen
soundness rows with their enforcement anchors and negative probes
(`make guarantee-report`), and the scope is the published
bug-bounty document.

## A taste of the language

```hls
import "std.result"
import "std.sanitize"

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

enum Shape {
    Circle(float),
    Rect(int, int)
}

fn area(s: Shape) -> float {
    return match s {
        Circle(r) => 3.14159 * r * r,
        Rect(w, h) => (w * h).to_float()
    }
}

# Error handling without exceptions: Result + the `?` propagate
fn answer() -> Result[int, str] {
    let n: Result[int, str] = int_parse("41")
    n?
    return Result.Ok(result_unwrap(n) + 1)
}

fn main() -> int uses IO, Args {
    let p: Point = Point { x: 3, y: 4 }
    p.translate(1)
    println("dist2 = " + p.dist2().to_str())    # dist2 = 32

    let a: Result[int, str] = answer()
    println("answer = " + result_unwrap_or(a, 0).to_str())    # answer = 42

    # Command-line input arrives tainted; sanitise or untaint first
    let argv: list[tainted[str]] = tainted_args()
    if argv.len() > 1 {
        println("hello, " + sanitize_filename(argv.get(1)))
    }
    return 0
}
```

More programs live in [`examples/`](examples/) — from a word count
over a real file to a complete HTTP server with sessions, CSRF
protection and server-sent events.

## Self-hosting, proven every build

```
snapshot/hlc.c ── cc ──► bin/hlc ── compiles ──► src/hlc.hls
(committed seed)          (native)                   │
     ▲                                                 │
     └────────── must re-derive byte for byte ─────────┘
```

`make bootstrap` compiles the committed C snapshot — the compiler's
own emission, carried in the tree and content-addressed by
`snapshot/MANIFEST.json` — and requires the resulting compiler to
re-derive that snapshot exactly. A stale or hand-edited snapshot
fails the build loudly. `make snapshot-chain` goes further: it runs
the whole bootstrap as two independent ignitions, byte-compares
every hop — each round against the snapshot, round against round —
and prints the chain digest; `CC2=clang` proves a different compiler
in the first slot lands on the same bytes.

No Python is involved in any build, release or snapshot path. The
interpreter under `tools/hlinterp/` is lab tooling for the
differential suites; nothing on a lifecycle path reads it.

## Quick start

Requirements: `gcc` or `clang`, and `make`. That is the whole list.

```bash
git clone https://github.com/mhieuhonda/halis-lang.git
cd halis-lang

make bootstrap          # ~20 s: cc builds the seed, HLS takes over
make run F=examples/hello.hls

make test              # ok/ + fail/ corpus, differential, bootstrap
                        # determinism, LLVM, memory stress, fmt, lint
```

Day-to-day commands:

| Command | What it does |
|---------|--------------|
| `make run F=prog.hls` | Compile to C, build natively, run |
| `make check F=prog.hls` | Type- and effect-check without running |
| `make audit F=prog.hls` | Capability + taint-flow report of the program |
| `make fmt` / `make lint` | Format and lint the tree |
| `bin/hlc --emit llvm prog.hls out.ll` | Emit LLVM IR instead of C |
| `make lto F=prog.hls` | Whole-program link-time optimisation, then run |
| `make pgo` | Profile-guided build of the compiler itself |

## The toolchain

The entire toolchain is written in Halis and compiles itself:

| Tool | What it does |
|------|--------------|
| `hlc` | The compiler — HLS to C, LLVM IR or HLIR; checker, proof engine and optimiser included |
| `hlfmt` | Opinionated formatter with a team config file; comments preserved in every position |
| `hllint` | Safety-first linter with `--fix` autofix and a `#[deprecated]` rule |
| `hls-lsp` | Language server: hover, definitions, references, rename, completion, inlay hints |
| `hls-dap` | Debug adapter: breakpoints, stepping, variable views, a REPL in the paused frame |
| `hls-repl` | Interactive REPL — `:type`, `:effects`, `:audit`; the session is a program |
| `hltest` | In-language test runner: table tests, snapshot testing, `--grep` |
| `hls-bench` | Criterion-style micro-benchmarks with baselines and comparisons |
| `hls-pkg` | Package manager: content-addressed deps, lock, audit, verify, publish with a transparency log |
| `hls-rt` / `hls-det` / `hls-rss` | Soft-real-time, deterministic-scheduler and RSS-stability certification |
| `hls-perf` / `hls-mem` / `hls-ctime` / `hls-size` | The four regression dashboards — performance, memory, compile time, binary size |
| `hls-freeze` | The frozen API surface: digest-pinned, amendment-chain ledger, drift refused |
| `hls-guarantee` | Re-derives the soundness ledger and renders the bug-bounty scope |
| `hls-compat` / `hls-migrate` | Replay every prior release's corpus; upgrade old trees |
| `hls-lts` / `hls-rc` | The support-line and release-candidate kits — policy as arithmetic, not prose |
| `hlbindgen` | C-header to safe `extern "C"` blocks with ABI size checks |
| `hlprove` / `hlmodel` | Proof reports with z3/CVC5 SMT bridges; finite-state model checking |
| `hls-fuzz` / `hls-fuzzseed` | Differential fuzzer and corpus seeding from real packages |
| `hls-auditkit` / `hls-finalaudit` | Third-party audit engagements: briefs, evidence, findings, the seal |
| `hlserve` / `hls-wasm-pack` | Dev server and ready-to-publish WebAssembly packages |

## Standard library

Sixty-plus pure-HLS modules, grouped by the three application
families Halis set out to serve:

| Group | Modules |
|-------|---------|
| Core | `str`, `math`, `fmt`, `list`, `map`, `set`, `collections`, `bits`, `option`, `result`, `hash`, `regex`, `template`, `log` |
| System | `io`, `fs`, `env`, `process`, `thread`, `sync`, `time`, `stream`, `async`, `archive`, `uuid` |
| Web | `http`, `http2`, `http_router`, `http_server`, `websocket`, `sse`, `cookie`, `session`, `csrf`, `graphql`, `openapi`, `dom`, `jsffi`, `url`, `html` |
| Data | `json`, `json_stream`, `csv`, `config`, `crypto`, `sha1`, `sha256`, `base64`, `hex`, `simd` |
| CLI | `cli`, `tui`, `color`, `progress`, `complete`, `man`, `hlsdoc`, `hlscli` |
| Security | `taint`, `sanitize`, `csrf`, `crypto` |
| Bare metal | `core.alloc`, `core.mem`, `core.interrupt`, `core.mmio`, `core.port`, `core.dma`, `core.atomics`, `core.boot`, `core.asm`, `core.stack`, `core.section`, `core.rt` |
| Testing | `test`, `quickcheck`, `bench` |

Every module is pure HLS, declares its capabilities honestly, and
ships with known-answer tests in `tests/ok/` and a runnable example
in `examples/`.

## Targets

| Backend | Triples and notes |
|---------|-------------------|
| Native C | `x86_64`/`aarch64`/`riscv64` Linux, macOS, Windows, FreeBSD — the default path |
| LLVM IR | Text emission for any LLVM-supported target |
| WebAssembly | `wasm32` with `std.jsffi` / `std.dom`, optimised and packaged by `hls-wasm-pack` |
| Bare metal | `x86_64-unknown-none`, `aarch64-unknown-none`, `riscv64-unknown-none` — Multiboot2/Limine headers, IDT/GDT syntax, MMIO, DMA-safe buffers, custom link scripts and sections, DWARF 5 |

Halis does not build an operating system; it builds the language in
which others do — `#![freestanding]`, `#![no_std]`, page-level memory
control and volatile I/O included.

## Verification and releases

- **Differential everywhere.** The interpreter, the native build and
  the wasm backend must produce identical bytes on the same program;
  the differential suites run on every PR and the halt classes are
  compared, not just the output.
- **Regression dashboards.** Performance, RSS, compile time and
  binary size are recorded per commit; a regression without a written
  sanction fails the gate.
- **Independent audits.** Two third-party audit kits — the standing
  engagement kit and the final-audit kit that pins the frozen
  surface, the snapshot manifest and the guarantee ledger — with the
  independence of every evidence pass labelled and the closure seal
  enforced by `make finalaudit-gate`.
- **A frozen API.** The v1.0 surface travels at a pinned digest with
  a hash-chained amendment ledger; `make freeze-verify` refuses drift
  on every commit.
- **Release gates.** `make release-acceptance` judges the tree as a
  legal state of the release line, and `hls-lts status` reads the
  clock of the opened line.

## Releases and support

**v1.0.0 is the current stable release** — see [RELEASES.md](RELEASES.md)
for the record of every tag and the discipline for the next ones.

The line `release/1.0` is the LTS stream: bug-fix backports only,
each carrying a `Backported-from:` trailer, patch releases cut
without gaps, and the whole history auditable by the day each commit
landed. Every LTS line gets **3 years of bug-fix support** and
**5 years of security support**; the full policy — the phases, the
calendar law, what the tools refuse — is published in
[LTS.md](LTS.md) and rendered from the constants the gates enforce.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Documentation

| Document | What it covers |
|----------|----------------|
| [SPEC.md](SPEC.md) | The language reference — lexical rules, types, effects, taint, ownership, IR, every subsystem |
| [installation.md](installation.md) | Installing the toolchain — every supported platform |
| [RELEASES.md](RELEASES.md) | The release record and support discipline |
| [SECURITY.md](SECURITY.md) | The security model, its honest limits, and how to report |
| [LTS.md](LTS.md) | The support policy — rendered, not maintained |
| [RC.md](RC.md) | The last release candidate's record |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to build, test and land changes |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | Community standards |
| [editors/vscode/halis/](editors/vscode/halis/) | The VS Code extension — LSP, formatter, linter, debugger |

## Contributing

Every contribution must preserve the guarantees above and pass
`make test`. Every new language feature is used inside the compiler
itself before it ships — `hlc` is always the language's first
customer. The `main` branch is protected; work lands through pull
requests with CI green on the gcc/clang and Python-version matrix.

Start with [CONTRIBUTING.md](CONTRIBUTING.md) — it walks the
bootstrap, the snapshot rules, the code style and the commit format.

## License

[MIT](LICENSE) © 2026 mhieuhonda
