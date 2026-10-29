# Contributing to Halis

Thank you for your interest in contributing. This document covers the
build, the conventions, the laws your change must keep, and how to
land it.

## Quick start

```bash
git clone https://github.com/mhieuhonda/halis-lang.git
cd halis-lang
make bootstrap    # cc compiles the committed C snapshot, HLS takes
                 # over, the fixed-point law must hold — no Python
make test         # ok/ + fail/ corpus, differential, bootstrap
                 # determinism, LLVM, memory stress, fmt, lint
```

Requirements: `gcc` or `clang`, and `make`. Python 3.8+ is optional
and lab-only — the interpreter under `tools/hlinterp/` drives the
differential suites and the Python lab tools; nothing on any build,
release or snapshot path reads it.

## Branch model

| Branch | Purpose |
|--------|---------|
| `main` | Development. The next minor release grows here. |
| `release/1.0` | The LTS line. Bug-fix backports only, each with a `Backported-from:` trailer; patch releases are cut by `hls-lts tag-patch`. |
| `feature/<topic>`, `fix/<topic>` | Short-lived topic branches, PRs against `main`. |

History is never rewritten on `main` or on `release/1.0`. Every
commit must build (`make bootstrap`) and pass the suite
(`make test`).

### CI/CD architecture

Four workflows live under `.github/workflows/`:

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| `ci.yml` | push, pull_request, tags | The main gate: bootstrap, snapshot verify, the chain proof, differential suites, the cc grid, macOS/Windows smokes, tooling. Docs-only changes skip the heavy jobs; a tag push must carry its `## [vX.Y.Z]` section in RELEASES.md. |
| `release.yml` | version tags, manual dispatch | Multi-platform native `hlc` builds + SHA256SUMS + GitHub Release. |
| `nightly.yml` | daily | The full sequential battery: cross-triples, size/memory/freeze gates, the chain and the pure bootstrap, the proof engines, seeded fuzz. |
| `codeql.yml` | pushes to `main`, weekly | CodeQL (security-extended) over the lab tooling and the workflows. |

Every third-party action is pinned to an exact commit SHA (checked
by Dependabot, one grouped weekly PR). `bin/` is cached in CI keyed
on the content hash of `snapshot/**` + `src/**`.

### The committed C snapshot

`snapshot/hlc.c` — the compiler's own emission for `src/hlc.hls`,
carried in the tree — is the seed the pure bootstrap compiles.
`snapshot/MANIFEST.json` records what it was cut from, content-
addressed. Two rules:

- **Never edit either file by hand.** The fixed-point law
  (`make bootstrap`) and the manifest laws (`make snapshot-verify`)
  both refuse a hand edit, naming the rule that fired.
- **If you changed anything under `src/hlc.hls` or `src/hlc/`, run
  `make snapshot-regen` and commit `snapshot/hlc.c` +
  `snapshot/MANIFEST.json` TOGETHER with your change** (or neither).
  `make snapshot-prove` runs the freestanding lineage — the no-libc
  compiler re-deriving the same bytes. `make snapshot-chain` re-runs
  the whole bootstrap as two independent ignitions and byte-compares
  every hop; run it before tagging a release (`CC2=clang` for the
  cross-compiler form).

The kit that guards the snapshot — `tools/hls-snap.hls` — is Halis
itself, compiled by `bin/hlc`; none of these steps needs Python.

### Branch protection (enforced on `main`)

- **Pull requests are required** for all non-admin pushers; direct
  pushes are limited to the repo owner.
- **CI must pass** on every PR: the lint job (actionlint, shellcheck,
  hllint over compiler/stdlib/core, spec-check), the byte-identical
  self-compilation, the no-libc cross-bootstrap ladder and the chain
  proof, the differential suites, the gcc/clang grid, the macOS and
  Windows smokes, and the tooling job.
- **Linear history** — no merge commits; rebase before merging.
- **No force-push, no branch deletion.**
- All review conversations must be resolved before merge.

## How to contribute

1. **Open an issue** describing what you want to change and why, and
   wait for a maintainer response — it avoids wasted work if the
   change is out of scope.
2. **Fork or branch** off `main`, naming the branch `feature/<topic>`
   or `fix/<topic>`.
3. **Make your changes** following the conventions below.
4. **Run the gates:**
   ```bash
   make test
   make bootstrap        # the pure-HLS fixed point
   make snapshot-chain   # the chain, twice over, byte for byte
   ```
   If you touched the compiler sources, also `make snapshot-regen`
   and commit the moved snapshot with your change.
5. **Commit with a clear message** (see the format below).
6. **Open a pull request** against `main`; reference the issue
   (`Closes #N`).
7. **Respond to review** by pushing to the same branch; do not
   force-push unless asked.

## Conventions

### File layout

```
src/hlc.hls              # Compiler entry point (imports src/hlc/*.hls)
src/hlc/                 # Compiler modules: types, lexer, parser,
                         # checker, proof, codegen, PGO, SIMD, LTO...
snapshot/                # The committed C snapshot + manifest — the
                         # bootstrap seed; mutate only via
                         # make snapshot-regen, never by hand
core/                    # Freestanding runtime for bare-metal targets
std/                     # Standard library (pure HLS); large modules
                         # are a facade + parts under std/<module>/
tools/                   # The toolchain (mostly Halis) and the lab
                         # interpreter under tools/hlinterp/
examples/                # Demonstration programs
tests/ok/                # Valid programs (must compile + run)
tests/fail/              # Programs that MUST be rejected
tests/*_acceptance.py    # The acceptance batteries (one per law)
tests/suites/            # make test sections, sourced in order
Makefile, mk/            # Core targets; stage sections live in mk/
```

### Standard library modules

Every module under `std/` must:

- Be **pure HLS** — no Python, no C.
- Declare **no `uses` clause** unless the module is explicitly about
  I/O (e.g. `std.time` reads the clock). Pure modules can be used
  inside the compiler itself.
- Be **self-contained** — import only other `std/` or `core/`
  modules.
- Ship a **companion test** under `tests/ok/feat_stdlib_<name>.hls`
  with known-answer vectors where possible.
- Ship a **companion example** under `examples/<name>_demo.hls`
  showing realistic usage.

### HLS code style

- 4-space indentation, no tabs (enforced by `.editorconfig`).
- `snake_case` for functions and variables, `PascalCase` for
  structs and enums.
- Every public function carries a one-line doc comment: what it
  does, and any panic conditions.
- Use `panic` **only for programming bugs**. Expected errors use
  `Result[T, E]` from `std.result`.
- No bitwise operators (`&`, `|`, `^`, `<<`, `>>`) — the grammar
  has none. Decompose bit manipulations into multiply / modulo /
  division on `int64`, or use `std.bits`.

### Commit messages

```
<area>: <imperative summary, <= 70 chars>

<optional body, wrapped at 72 chars>
```

Areas used in this repo:

- `compiler:` — changes to `src/hlc.hls` / `src/hlc/`
- `stdlib:` — changes to `std/` or `core/`
- `tools:` — changes to `tools/`
- `tests:` — changes to `tests/`
- `docs:` — documentation only
- `ci:` — workflows, dependabot, gitignore, editorconfig
- `build:` — Makefile, packaging

## The laws your change must keep

1. **I/O is a declared effect.** A function that does not declare
   `uses` must be statically guaranteed to perform none — console,
   files, clock, network, randomness, subprocess, concurrency.
2. **Every operation is checked.** No new "fast mode" that skips
   overflow or bounds checks; only the proof engine may retire a
   check, and only with a proof.
3. **No null, no uninitialised state, no hidden globals.**
4. **The API is frozen.** The v1.0 surface travels at a pinned
   digest; growing it requires `hls-freeze amend` with a written
   reason and an author, and the amendment ledger carries the
   change. A PR that moves the surface without an amendment is
   rejected.
5. **The differential law.** The interpreter and the native build
   must produce identical output on every test program. If you add
   a compiler feature, add at least one differential test under
   `tests/ok/` that exercises it; if you change semantics, the
   existing tests must still pass — otherwise it is a breaking
   change and belongs to the next minor.

A PR that breaks any of these is rejected, regardless of the
performance benefit.

## Releasing

Releases are cut by maintainers:

- **On `main`:** the next minor grows release by release; a version
  tag requires its `## [vX.Y.Z]` section in [RELEASES.md](RELEASES.md)
  (the CI gate refuses a tag without one), and the release workflow
  builds the artifacts.
- **On `release/1.0`:** bug fixes on `main` are backported with
  `hls-lts backport` (the tool writes the `Backported-from:`
  trailer and refuses everything that is not a bug fix), and patch
  releases are cut with `hls-lts tag-patch`. The line's history is
  audited by the day each commit landed.

## License

By contributing, you agree that your contributions are licensed
under the MIT License (see [LICENSE](LICENSE)).
