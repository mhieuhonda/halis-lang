# Halis releases

The release record for the Halis toolchain. Every tag cut on this
repository has a home on this page; the discipline is enforced on the
CI side (a tag push without a matching `## [vX.Y.Z]` section below is
refused by the pipeline).

Versioning follows [Semantic Versioning]. Support follows the
published [LTS policy](LTS.md): every LTS line gets **3 years of
bug-fix support** and **5 years of security support**, counted from
the day its base release was cut.

**Current stable: v1.0.1** — released 2026-10-29, LTS line
`release/1.0` (opened at v1.0.0; bug fixes through **2029-10-29**,
security fixes through **2031-10-29**).

---

## [v1.0.1] — 2026-10-29 — the installer's own release

The first patch on the 1.0 line, cut from main where the
release-binary work landed. v1.0.0's archives still carried the bare
compiler — `hlc`, the licence, the readme — so the installer's
whole-SDK check refused the only stable release there was, and the
one-liner in the README had nothing to install. v1.0.1's archives are
the SDK the pipeline builds since: `bin/hlc`, `bin/hlfmt`, `std/`,
`core/`, the canonical example and a VERSION record, on all six
platforms, under one `SHA256SUMS`.

Fixed with it:

- **The Release pipeline's Windows legs** prove the formatter's fixed
  point in a form the Windows CRT cannot scramble. A MinGW program's
  redirected stdout is a text-mode stream — every `\n` it emits lands
  in the file as `\r\n` — so the byte law is judged in memory
  (`hlfmt -c`) and the emitted stream is compared modulo exactly that
  CR. The Linux and macOS legs gain the same in-memory check; their
  byte-for-byte diff keeps its shape.
- **install.sh** records in `.manifest` only the bin entries an
  install actually creates — the launcher or the bare compiler, never
  both, and the formatter under the name it carries on this OS — so
  `--uninstall` no longer names a path that was never written. The
  acceptance gate calls the formatter by its real name on Windows
  (`hlfmt.exe`), and an upgrade that switches the compiler's spelling
  removes the one the previous install left behind, when that
  install's own record claims it.
- **installation.md** speaks the installer's errors as the installer
  says them, and the bootstrap comment matches Stage 142 — the
  committed C snapshot, no seed.

---

## [v1.0.0] — 2026-10-29 — the first LTS release

The stable release everything before it was walking toward. The
candidate line (rc.1 → rc.4) rehearsed the cut; this is the cut
itself. The version markers — the snapshot manifest, the editor
manifest, this record — moved to 1.0.0 with one voice, the release
candidate anchor retired with the release commit, the tag was cut,
and `hls-lts init --tag v1.0.0` opened the support line `release/1.0`.
From this day the published LTS policy is the clock: bug fixes
through 2029-10-29, security fixes through 2031-10-29.

What v1.0 ships:

- **The language.** Nine statically tracked effects (`IO`, `Fs`,
  `Clock`, `Args`, `Exit`, `Net`, `Rand`, `Proc`, `Conc`) with
  default-deny purity; taint tracking (`tainted[T]`, taint sources
  and sinks, the `std.sanitize` family); ownership with `drop` /
  `clone` / `take` and use-after-move rejected at compile time;
  checked arithmetic and bounds on every operation; contracts,
  refinement types and a proof engine that can retire checks it has
  proven dead; generics, enums with exhaustive `match`, `?` error
  propagation, and no null, no globals, no undefined behaviour.
- **A self-hosted toolchain.** The compiler (`hlc`) is written in
  Halis; the bootstrap is pure HLS from the committed C snapshot
  (`snapshot/hlc.c`) and proves the fixed-point law on every build;
  `make snapshot-chain` re-runs the bootstrap as two independent
  ignitions and byte-compares every hop. The formatter, linter,
  language server, debug adapter, REPL, benchmark runner, package
  manager, snapshot kit, freeze tool and release kit are all Halis
  too.
- **Targets.** Native C, LLVM IR, WebAssembly (`wasm32` with JS-FFI
  and DOM bindings), and the bare-metal triples `x86_64-unknown-none`,
  `aarch64-unknown-none`, `riscv64-unknown-none` — with
  `#![freestanding]` / `#![no_std]` modes, boot protocols, interrupt
  and MMIO syntax for OS development.
- **A standard library** of 60+ pure-HLS modules across the three
  application families the project promised from day one: CLI tools,
  web applications, and operating-system development.
- **A verification estate.** Fifteen documented soundness guarantees
  with a bug-bounty scope, cross-implementation differential suites
  (interpreter, native C, wasm, LLVM), RSS-stability certification,
  a deterministic scheduler, and a fuzz corpus seeded from real
  packages.
- **Governance that a tool can read.** The API surface travels
  frozen and digest-pinned with a hash-chained amendment ledger; the
  LTS policy is rendered from the constants its gates enforce;
  two independent third-party security-audit kits (the engagement,
  the evidence passes, the findings, the seal) travelled with the
  release candidates; the compatibility suite replays every prior
  release's accepted corpus against today's toolchain.

Support for this release runs under the [LTS policy](LTS.md). The
road that produced it — 150 stages, ten phases, from the bootstrap
seed to a compiler that proves its own bootstrap byte for byte — is
history now; the working record of what came before it is the git
history itself.

---

## The candidate line

Four release candidates rehearsed the release between 2026-10-28 and
2026-10-29, one round each, each anchored in the tree
(`bin/hls-rc declare`) and each closed under the feature freeze
(`bin/hls-rc gate` — fixes and chores land, features are named and
refused). The last candidate's record is kept in [RC.md](RC.md).

### [v1.0.0-rc.4] — 2026-10-29 — sign-off

The final approval. `bin/hls-rc signoff` cuts the signer's name into
the anchor — the tree must be green at the signing moment, and
`signoff --check` re-derives the verdict at every gate rather than
trusting an archived signature. One signature per candidate, the
record speaking it exactly once, the successor line naming v1.0.0.

### [v1.0.0-rc.3] — 2026-10-28 — final dry-run

The release day walked end to end on a scratch world before it was
walked on the real tree: the markers moved, the anchor retired, the
tag cut, the LTS line opened, the windows becoming the clock — and
the morning after rehearsed with it, the backport and the first
patch release included.

### [v1.0.0-rc.2] — 2026-10-28 — bug-fix round

The hardening pass. A missing input file is named at exit 2 on both
front-ends, never a panic on a path the user typed; an unwritable
destination is refused before codegen; a broken output pipe dies
quietly. The freeze stayed green across the round.

### [v1.0.0-rc.1] — 2026-10-28 — feature freeze

The candidate declared. The readiness checklist held on all its
laws, the anchor was written, and from the anchor to the release
every commit was judged: BUGFIX and CHORE may land, FEATURE and
UNKNOWN are named sha by sha and refused.

---

## The alpha era (v0.1.0 → v0.166.0-alpha)

One hundred and ninety alpha releases between 2026-08-27 and
2026-10-28 built everything above, in ten phases. The per-release
narratives of that era lived in the changelog that this page
replaces; the record that matters is what the releases left behind.

| Era | Versions | What it built |
|-----|----------|---------------|
| Foundation | v0.1.0 – v0.4 | The bootstrap seed, the self-hosted compiler, the fixed point, modules, enums and generics, ownership |
| Security model | v0.5 – v0.9 | Fine-grained effects, the `pure` keyword, taint tracking, the sandbox |
| Backends & optimisation | v0.21 – v0.37 | HLIR, LLVM, WebAssembly, PGO, LTO, SIMD, cross-compilation, verified tail calls, escape analysis |
| Concurrency & proofs | v0.27 – v0.35 | Data-race-free concurrency, contracts, the proof engine, the testing ecosystem and fuzzing |
| Standard library | v0.53 – v0.71 | I/O, fs, net, HTTP/1.1 + HTTP/2, JSON streaming, regex, hashing, collections, sync, threads, time, archives, UUID/ULID |
| CLI track | v0.72 – v0.82 | Argument parsing, TUI, colour, progress, structured logging, config, shell completion, docs and man pages |
| Web track | v0.78 – v0.95 | Routers, HTTP server, WebSocket, cookies, sessions, CSRF, templates, SSE, GraphQL, OpenAPI, JS-FFI, SSR, wasm packaging |
| OS-dev foundation | v0.96 – v0.115 | Freestanding mode, `no_std` core, allocators, page tables, interrupt/MMIO/port syntax, boot protocols, bare-metal triples, DWARF 5 |
| Verification & supply chain | v0.116 – v0.131 | Invariant inference, refinement types, side-channel analysis, constant-time verification, SBOM, signed packages, transparency logs, capability tokens, audit-log signing |
| DX & governance | v0.132 – v0.166 | LSP/DAP/REPL/benchmarks, snapshot testing, compatibility replay, migration tooling, deprecation, semver enforcement, the four regression dashboards, independent audits, the API freeze, the pure-HLS bootstrap, the LTS policy |

---

## Versioning and support

- **Semantic Versioning** governs all tags; `hls-pkg` enforces it on
  dependencies (requirement dialect, publish immutability, verify
  drift).
- **LTS lines** are major.minor streams declared with
  `hls-lts init --tag vX.Y.0`; a line's branch takes backports only,
  cut patch releases with no gaps, and is audited by the day each
  commit landed. See [LTS.md](LTS.md).
- **Pre-1.0 releases are unsupported.** The alpha tags remain
  available for the compatibility suite's time-travel replays, but
  no promise attaches to them.

[Semantic Versioning]: https://semver.org/spec/v2.0.0.html
