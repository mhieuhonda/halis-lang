# ROADMAP — Halis (HLS)

> A multi-stage roadmap that takes Halis from a self-hosting v0.1 core
> to a **highly secure, high-performance, complete v1.0 language** — with
> the entire toolchain written in Halis itself, and **first-class support
> for three target application families**:
>
> 1. **CLI tools** — fast native binaries, predictable startup, low memory.
> 2. **Web applications** — WebAssembly backend, HTTP server stdlib, JS-FFI.
> 3. **Operating-system development** — freestanding mode, no-GC runtime,
>    page-level memory control, bare-metal code generation.
>
> After v1.0, post-1.0 work prioritises the OS-development track: users
> who want to build their own operating system in Halis get first-class
> language + toolchain support. **The Halis project itself does not build
> an OS** — it builds the language in which OTHERS can build one.

**Status legend:** ✅ complete · 🔄 in progress · ⬜ not started
**Standing principle:** every stage closes only when **100% of its acceptance
criteria** are met and the differential test suite (interpreter ↔ native)
remains green.

---

## OVERVIEW

### Phase I — Core language foundation (Stages 1–18, complete)

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 1 | Specification & core design | ✅ | (done) |
| 2 | Bootstrap seed Stage-0 | ✅ | (done) |
| 3 | Self-hosted compiler — front-end | ✅ | (done) |
| 4 | Backend HLS → C + C runtime | ✅ | (done) |
| 5 | Full self-compilation (fixed-point) | ✅ | (done) |
| 6 | Module system & standard library | ✅ | (done) |
| 7 | Advanced type system: enum, Option/Result, generics | ✅ | (done) |
| 8 | Ownership & borrow checking (end of arena) | ✅ | (done) |
| 9 | Fine-grained effects & capabilities | ✅ | (done in v0.20.0-alpha) |
| 10 | Taint tracking & sandbox | ✅ | (done in v0.21.0-alpha) |
| 11 | SSA IR + optimisation | ✅ | (done in v0.21.0-alpha) |
| 12 | Native LLVM backend | ✅ | (done in v0.22.0-alpha) |
| 13 | Package manager `hls-pkg` | ✅ | (done in v0.23.0-alpha) |
| 14 | Tooling: LSP, formatter, linter | ✅ | (release v0.24.0-alpha) |
| 15 | Safe C FFI | ✅ | (release v0.25.0-alpha) |
| 16 | Concurrency & async (data-race freedom) | ✅ | (release v0.29.0-alpha, perfected; v0.33.0-alpha re-verified) |
| 17 | Formal verification & contracts | ✅ | (release v0.30.0-alpha, perfected; v0.33.0-alpha re-verified) |
| 18 | Testing ecosystem & fuzzing | ✅ | (release v0.34.0-alpha) |

### Phase II — Performance, optimisation & platform reach (Stages 19–34)

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 19 | Profile-guided optimisation (PGO) | ✅ | 3–4 weeks |
| 20 | Link-time optimisation (LTO) across crates | ✅ | 3 weeks |
| 21 | SIMD vectorisation (target-feature detection) | ✅ | 5 weeks |
| 22 | Cross-compilation targets (Linux/macOS/Windows/FreeBSD) | ✅ | 4 weeks |
| 23 | WebAssembly backend (`target wasm32`) | ✅ | 6 weeks |
| 24 | `wasm-opt` integration + emscripten bridge | ✅ | 3 weeks |
| 25 | AArch64 backend tuning (Apple Silicon, Graviton) | ✅ | 4 weeks |
| 26 | RISC-V 64 backend (foundation for OS work) | ✅ | 5 weeks |
| 27 | Inline assembly syntax (`asm!`) | ✅ | (done in v0.50.0-alpha) |
| 28 | Stack-frame layout control (for kernel code) | ✅ | 3 weeks |
| 29 | `noinline`/`always_inline`/`cold`/`hot` attributes | ✅ | 2 weeks |
| 30 | Boxed-vs-stack layout analysis (escape analysis) | ✅ | 5 weeks |
| 31 | Tail-call optimisation (verified) | ✅ | 3 weeks |
| 32 | Zero-cost abstractions audit (every stdlib fn under 1 µs) | ✅ | (done) |
| 33 | Async/await zero-runtime futures | ✅ | (done) |
| 34 | Async stream combinators (channels × generators) | ✅ | (done) |

### Phase III — Standard library expansion (Stages 35–52, complete)

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 35 | `std.io` — buffered readers/writers, `Read`/`Write` traits | ✅ | 4 weeks |
| 36 | `std.fs` — path abstraction, directory walk, permissions | ✅ | 4 weeks |
| 37 | `std.net` — TCP/UDP sockets, DNS, TLS via libcurl | ✅ | 6 weeks |
| 38 | `std.http` — HTTP/1.1 server + client (RFC 7230) | ✅ | 6 weeks |
| 39 | `std.http2` — HTTP/2 + ALPN negotiation | ✅ | 5 weeks |
| 40 | `std.json` streaming parser (constant-memory) | ✅ | 3 weeks |
| 41 | `std.regex` — NFA-based regex (no ReDoS) | ✅ | (done in v0.60.0-alpha) |
| 42 | `std.fmt` — printf-style + custom `Display` impls | ✅ | (done in v0.61.0-alpha) |
| 43 | `std.hash` — SipHash, xxHash, FNV, cityHash | ✅ | (done in v0.62.0-alpha) |
| 44 | `std.collections` — BTreeMap, HashSet, LinkedList, RingBuf | ✅ | (done in v0.63.0-alpha) |
| 45 | `std.sync` — Mutex, RwLock, Condvar, OnceCell | ✅ | (done in v0.64.0-alpha) |
| 46 | `std.thread` — OS threads (preemptive scheduler) | ✅ | (done in v0.65.0-alpha) |
| 47 | `std.process` — spawn, pipe, signal, exit-code | ✅ | (done in v0.66.0-alpha) |
| 48 | `std.env` — environment variables, current dir | ✅ | (done in v0.67.0-alpha) |
| 49 | `std.time` — monotonic clock, sleep, deadline arithmetic | ✅ | (done in v0.68.0-alpha) |
| 50 | `std.math` — IEEE-754 edge cases, special functions | ✅ | (done in v0.69.0-alpha) |
| 51 | `std.archive` — tar, zip, gzip (no unsafe decompression) | ✅ | (done in v0.70.0-alpha) |
| 52 | `std.uuid` v7 + `std.ulid` (lexicographically sortable) | ✅ | (done in v0.71.0-alpha) |

### Phase IV — CLI tooling track (Stages 53–62)

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 53 | `std.cli` — argument parser (subcommands, env, defaults) | ✅ | (done in v0.72.0-alpha) |
| 54 | `std.tui` — terminal raw mode, ANSI escapes, screen grid | ✅ | (done in v0.73.0-alpha) |
| 55 | `std.color` — terminal color detection, truecolor fallback | ✅ | (done in v0.74.0-alpha) |
| 56 | `std.progress` — progress bars, spinners, ETA | ✅ | (done in v0.75.0-alpha) |
| 57 | `std.log` — structured logging (JSON + human formats) | ✅ | (done in v0.76.0-alpha) |
| 58 | `std.config` — TOML + YAML + env-layered config loader | ✅ | (done in v0.79.0-alpha) |
| 59 | `std.complete` — shell-completion generator (bash/zsh/fish) | ✅ | (done in v0.80.0-alpha) |
| 60 | `hls-cli` — `cargo`-style launcher (`hls new`, `hls run`, `hls build`) | ✅ | (done in v0.81.0-alpha) |
| 61 | `hls-doc` — rustdoc-style API docs generator | ✅ | (done in v0.82.0-alpha) |
| 62 | Man-page generator (`--man` produces nroff) | ✅ | (done in v0.77.0-alpha) |

### Phase V — Web application track (Stages 63–76)

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 63 | `std.http.router` — path params, middleware, sub-routers | ✅ | (done in v0.78.0-alpha) |
| 64 | `std.http.server` — multi-thread, keep-alive, HTTP/2 push | ✅ | (done in v0.83.0-alpha) |
| 65 | `std.websocket` — RFC 6455 server + client | ✅ | (done in v0.84.0-alpha) |
| 66 | `std.cookie` — signed cookies, SameSite, secure flag | ✅ | (done in v0.85.0-alpha) |
| 67 | `std.session` — server-side sessions (in-memory + file) | ✅ | (done in v0.86.0-alpha) |
| 68 | `std.csrf` — double-submit + sync-token patterns | ✅ | (done in v0.87.0-alpha) |
| 69 | `std.template` — compile-time HTML templates (no XSS) | ✅ | (done in v0.88.0-alpha) |
| 70 | `std.sse` — server-sent events (one-way streaming) | ✅ | (done in v0.89.0-alpha) |
| 71 | `std.graphql` — schema-first server (parser + resolver) | ✅ | (done in v0.90.0-alpha) |
| 72 | `std.openapi` — generate OpenAPI 3.1 from handler types | ✅ | (done in v0.91.0-alpha) |
| 73 | `std.jsffi` — bind to JavaScript globals from `wasm32` | ✅ | (done in v0.92.0-alpha) |
| 74 | `std.dom` — server-side rendering (no client JS needed) | ✅ | (done in v0.93.0-alpha) |
| 75 | `hls-serve` — `webpack-dev-server` equivalent for HLS | ✅ | 5 weeks (done in v0.94.0-alpha) |
| 76 | `hls-wasm-pack` — publish-ready wasm + JS glue | ✅ | (done in v0.95.0-alpha) |

### Phase VI — OS development foundation (Stages 77–96)

> These stages give the LANGUAGE the capabilities OS developers need.
> Halis itself does not write an OS; these features let users do so.

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 77 | `#![freestanding]` mode (no libc, no OS calls) | ✅ | (done in v0.96.0-alpha) |
| 78 | `#![no_std]` core-only stdlib subset | ✅ | (done in v0.97.0-alpha) |
| 79 | `core.alloc` — pluggable allocator trait | ✅ | (done in v0.98.0-alpha) |
| 80 | `core.mem` — physical-page allocator, page tables | ✅ | (done in v0.99.0-alpha) |
| 81 | Panic-handler override (kernel panic strategy) | ✅ | (done in v0.100.0-alpha) |
| 82 | Stack-overflow guard page + deterministic stack size | ⬜ | 3 weeks |
| 83 | Inline-asm register constraints (clobber, input, output) | ⬜ | 5 weeks |
| 84 | Linker-script integration (`link.ld`) + custom sections | ⬜ | 4 weeks |
| 85 | Multiboot2 + Limine-compliant boot protocol headers | ⬜ | 4 weeks |
| 86 | `core.interrupt` — IDT/GDT declaration syntax | ⬜ | 6 weeks |
| 87 | `core.mmio` — memory-mapped-IO helpers (volatile reads/writes) | ⬜ | 3 weeks |
| 88 | `core.port` — x86 I/O port invariants (`inb`/`outb` typesafe) | ⬜ | 2 weeks |
| 89 | DMA-safe buffer types (no GC moves, no virtual remap) | ⬜ | 4 weeks |
| 90 | `core.sync.nolock` — lock-free atomics, seqlock, RCU | ⬜ | 5 weeks |
| 91 | Verified interrupt-safety (no alloc in IRQ context) | ⬜ | 4 weeks |
| 92 | Cross-bootstrappable build (Stage-0 → freestanding hlc) | ⬜ | 4 weeks |
| 93 | `target x86_64-unknown-none` — bare-metal triple | ⬜ | 3 weeks |
| 94 | `target aarch64-unknown-none` — bare-metal ARM | ⬜ | 3 weeks |
| 95 | `target riscv64-unknown-none` — bare-metal RISC-V | ⬜ | 3 weeks |
| 96 | ELF symbol-table emission + debug-info (DWARF 5) | ⬜ | 6 weeks |

### Phase VII — Verification, security & supply chain (Stages 97–112)

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 97 | SMT-based loop-invariant inference (auto-discovery) | ⬜ | 6 weeks |
| 98 | Refinement types (lightweight, opt-in) | ⬜ | 7 weeks |
| 99 | `hlprove --cvc5` — CVC5 SMT backend | ⬜ | 3 weeks |
| 100 | Separation-logic fragment (heap shapes) | ⬜ | 8 weeks |
| 101 | Cryptographic side-channel analysis pass | ⬜ | 5 weeks |
| 102 | Constant-time verifier (verify code is branch-free on secrets) | ⬜ | 4 weeks |
| 103 | `hls-audit` — supply-chain effect report (transitive) | ⬜ | 4 weeks |
| 104 | SBOM generation (CycloneDX + SPDX) per release | ⬜ | 3 weeks |
| 105 | Reproducible-build verification across distros | ⬜ | 4 weeks |
| 106 | Signed packages (minisign, ed25519) | ⬜ | 3 weeks |
| 107 | Transparency log Gossip protocol (multi-source verify) | ⬜ | 4 weeks |
| 108 | Memory-safety re-verification under `-O fast` (proof replay) | ⬜ | 4 weeks |
| 109 | Taint-tracking through FFI boundaries | ⬜ | 5 weeks |
| 110 | Sandboxed package execution (seccomp-bpf filter) | ⬜ | 5 weeks |
| 111 | Capability token types (`Cap[Net]` as a value, not just effect) | ⬜ | 6 weeks |
| 112 | Audit-log signing (every privileged op hashed + chained) | ⬜ | 3 weeks |

### Phase VIII — Developer experience & ecosystem (Stages 113–124)

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 113 | LSP: goto-definition across packages | ⬜ | 4 weeks |
| 114 | LSP: inlay hints (types, parameter names) | ⬜ | 3 weeks |
| 115 | LSP: refactor actions (rename, extract, inline) | ⬜ | 6 weeks |
| 116 | `hlfmt` — preserve comments in all positions | ⬜ | 3 weeks |
| 117 | `hlfmt` — configuration file (`.hlfmt.toml`) for teams | ⬜ | 2 weeks |
| 118 | `hllint` — autofix mode (`--fix`) | ⬜ | 4 weeks |
| 119 | `hltest` — snapshot testing (`assert_snapshot!`) | ⬜ | 3 weeks |
| 120 | `hltest` — parameterised tests (table-driven) | ⬜ | 3 weeks |
| 121 | VS Code extension: debugger integration (DAP) | ⬜ | 6 weeks |
| 122 | `hldoc` — searchable web docs (offline + online) | ⬜ | 5 weeks |
| 123 | `hls-repl` — interactive REPL with :type/:effects/:audit | ⬜ | 5 weeks |
| 124 | `hls-bench` — criterion-style micro-benchmark runner | ⬜ | 4 weeks |

### Phase IX — Performance, runtime & stability (Stages 125–140)

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 125 | Garbage-collector-free runtime verification (RSS stability) | ⬜ | 3 weeks |
| 126 | Soft-real-time mode (bounded allocation per cycle) | ⬜ | 6 weeks |
| 127 | Deterministic-scheduler option (testing concurrency) | ⬜ | 4 weeks |
| 128 | Backwards-compatibility test suite (every prior version's ok/) | ⬜ | 5 weeks |
| 129 | Migration tooling (`hls migrate v0.34 -> v1.0`) | ⬜ | 4 weeks |
| 130 | Deprecation mechanism (`@deprecated` attribute + lint) | ⬜ | 3 weeks |
| 131 | Semantic-versioning enforcement in `hls-pkg` | ⬜ | 3 weeks |
| 132 | LTS branch policy (backport-only after v1.0) | ⬜ | 2 weeks |
| 133 | Cross-impl differential suite (3+ backends) | ⬜ | 6 weeks |
| 134 | Fuzz corpus seeding from real-world packages | ⬜ | 4 weeks |
| 135 | Bug-bounty-eligible soundness guarantees documented | ⬜ | 3 weeks |
| 136 | Performance regression dashboard (per-commit) | ⬜ | 5 weeks |
| 137 | Memory regression dashboard (RSS over time) | ⬜ | 4 weeks |
| 138 | Compile-time regression dashboard | ⬜ | 4 weeks |
| 139 | Binary-size regression dashboard | ⬜ | 3 weeks |
| 140 | Independent security audit (third-party, paid) | ⬜ | 8 weeks |

### Phase X — Final stabilisation & v1.0 (Stages 141–150)

| # | Stage | Status | Estimated effort |
|---|-------|:------:|:----------------:|
| 141 | API freeze — syntax + stdlib locked | ⬜ | 4 weeks |
| 142 | Pure-HLS bootstrap (remove `boot/` Python seed) | ⬜ | 8 weeks |
| 143 | Bit-for-bit reproducible bootstrap chain | ⬜ | 4 weeks |
| 144 | Independent third-party security audit (final) | ⬜ | 6 weeks |
| 145 | LTS policy published (3 years bug-fix, 5 years security) | ⬜ | 2 weeks |
| 146 | v1.0 Release Candidate 1 (feature freeze) | ⬜ | 4 weeks |
| 147 | v1.0 Release Candidate 2 (bug-fix only) | ⬜ | 4 weeks |
| 148 | v1.0 Release Candidate 3 (final dry-run) | ⬜ | 4 weeks |
| 149 | v1.0 Release Candidate 4 (sign-off) | ⬜ | 2 weeks |
| 150 | **HLS v1.0 — LTS release** | ⬜ | 2 weeks |

> **Note on Stage 19 (originally "Documentation, book, playground"):**
> The documentation/book/playground stage has been **removed** from the
> roadmap. Promotion, documentation, and the public playground are
> separately managed activities that happen **after** v1.0 stabilises —
> they are explicitly out-of-roadmap. The roadmap is exclusively about
> building the language and toolchain; how the resulting language is
> communicated to the world is the maintainer's separate concern.

Estimated total duration: ~36–48 months (small team of 2–4 full-time).

---


---

## Per-stage completion details

This roadmap tracks **what is planned and its status** (tables above).
What was actually built per release is recorded in
[CHANGELOG.md](CHANGELOG.md); language design details live in
[SPEC.md](SPEC.md). The verbose per-stage completion notes that used to
follow this line were removed as redundant (they duplicated the
changelog) — see git history if needed.
