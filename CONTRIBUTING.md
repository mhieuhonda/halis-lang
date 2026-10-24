# Contributing to Halis (HLS)

Thank you for your interest in contributing to Halis! This document
describes how to set up your environment, the conventions we follow, and
how to submit changes.

## Quick start

```bash
git clone https://github.com/mhieuhonda/halis-lang.git
cd halis-lang
make bootstrap    # pure-HLS: cc compiles the committed C snapshot,
                 # hlc re-derives it, the fixed-point law must hold
                 # (no Python in the chain — Stage 142)
make test         # full suite: interpreter, native, differential,
                 # bootstrap determinism, LLVM IR, memory-stress, fmt, lint
```

Requirements: gcc or clang. Python 3.8+ is optional and lab-only
(the interpreter under `tools/hlinterp/` drives the differential
suites and the Python tools; the seed itself left the tree in
v0.164.0-alpha — nothing on any build or snapshot path reads it).

## Branch model

| Branch                           | Purpose                                       |
|----------------------------------|-----------------------------------------------|
| `main`                           | The roadmap branch. Stages 1–17 complete (current: v0.30.0-alpha). Each new roadmap stage (18–20) lands here when ready. |
| `feature/community-extensions`   | Non-roadmap upgrades: new stdlib modules, tooling, examples, CI/CD. Smaller, more frequent releases cut from here. |
| `feature/*` / `fix/*`             | Short-lived topic branches.                   |

We **never** rewrite history on `main` or on `feature/community-extensions`.
Every commit must build (`make bootstrap`) and pass the full test suite
(`make test`).

### CI/CD architecture

Four workflows live under `.github/workflows/`:

| Workflow     | Trigger                                   | What it does |
|--------------|-------------------------------------------|--------------|
| `ci.yml`     | push, pull_request, `v*-alpha` tags       | The main gate (see branch protection above). Docs-only changes skip the heavy jobs. |
| `release.yml`| `v*-alpha` tags, manual dispatch          | 6-platform native `hlc` builds + SHA256SUMS + GitHub Release. |
| `nightly.yml`| daily 18:17 UTC                           | Full sequential battery, 30-min fuzz, cvc5/z3, cross-triples, size/mem/freeze gates; auto-issues on failure. |
| `codeql.yml` | pushes to main, weekly                    | CodeQL (security-extended) over Python + the workflows. |

Every third-party action is pinned to an exact commit SHA (checked by
Dependabot, one grouped weekly PR). `bin/` is cached in CI keyed on the
content hash of `snapshot/**` + `src/**`, so unchanged sources never pay
the snapshot compile twice.

### The committed C snapshot (Stage 142)

`snapshot/hlc.c` — the compiler's own emission for `src/hlc.hls`,
carried in the tree — is the seed the pure bootstrap compiles, and
`snapshot/MANIFEST.json` records what it was cut from (the exact import
closure, content-addressed). Two rules for contributors:

- **Never edit either file by hand.** The fixed-point law
  (`make bootstrap`) and the manifest laws (`make snapshot-verify`)
will both refuse a hand edit, naming the rule that fired.
- **If you changed anything under `src/hlc.hls` / `src/hlc/`, run
  `make snapshot-regen` and commit `snapshot/hlc.c` +
  `snapshot/MANIFEST.json` TOGETHER with your change** (or neither).
  A comment-only edit regenerates the manifest only; a semantic edit
  re-derives the C at the fixed point. `make snapshot-prove` runs the
  freestanding lineage (the no-libc compiler deriving the same
  bytes — the second lineage, and since v0.164.0-alpha the only
  one: the seed left the tree, and the retired `--seed` flag refuses
  by name). `make snapshot-chain` re-runs the whole bootstrap as two
  independent ignitions and byte-compares every hop — run it before
  tagging a release (`CC2=clang` for the cross-compiler form). Since
  Stage 143 the kit itself is Halis — `tools/hls-snap.hls`, compiled
  by `bin/hlc` — so none of these steps needs Python.

### Branch protection (enforced on `main`)

The `main` branch is protected. The rules in effect:

- **Pull requests are required** for all non-admin pushers. Direct
  pushes to `main` are limited to the repo owner (admin).
- **CI must pass** on every PR: the aggregate `CI gate` check.
  Behind it run the lint job (actionlint, shellcheck at error
  severity, Python byte-compile, hllint over compiler/stdlib/core,
  spec-check), `make bootstrap` with the byte-identical
  self-compilation diff plus the no-libc cross-bootstrap ladder and
  the chain proof, the eight differential suites in parallel, the
  gcc/clang cc grid over the pure chain, macOS and Windows smokes,
  and the tooling job (hltest, seeded fuzz, LSP smoke). All of them
  must pass before a PR can merge.
- **Linear history** is enforced — no merge commits. Rebase your PR
  before merging.
- **No force-push, no branch deletion** on `main`.
- **Conversation resolution**: all review conversations on a PR must be
  marked as resolved before the PR can merge.

To work on a feature: fork the repo (or branch off `main`), commit,
push to your branch, open a PR. Do not push to `main` directly unless
you are the owner.

## How to contribute

1. **Open an issue** describing what you want to change and why. Wait for
   a maintainer response before sending code — this avoids wasted work
   if the change is out of scope.
2. **Fork the repository** and create a topic branch from `main` (for
   roadmap-aligned changes) or from `feature/community-extensions` (for
   non-roadmap upgrades). Name the branch `feature/<topic>` or `fix/<topic>`.
3. **Make your changes** following the conventions below.
4. **Run the full test suite**:
   ```bash
   make test
   make bootstrap        # confirms the pure-HLS fixed point
   make snapshot-chain   # the chain, twice over, byte for byte
   ```
   All must succeed before you push. If you touched the compiler
   sources, also run `make snapshot-regen` (see the snapshot rules
   above) and commit the moved snapshot with your change.
5. **Commit with a clear message** (see "Commit messages" below).
6. **Open a pull request** against the appropriate base branch. Reference
   the issue number in the PR description (`Closes #N`).
7. **Respond to review feedback** — push additional commits to the same
   branch; do not force-push unless asked.

## Conventions

### File layout

```
src/hlc.hls              # The compiler entry point (imports src/hlc/*.hls)
src/hlc/                 # Compiler modules: types, lexer, parser, checker,
                         # proof, check_expr, check_call, check_match,
                         # runtime, codegen, pgo, simd, lto, gen_expr,
                         # gen_stmt, gen_fn, main
snapshot/                # The committed C snapshot + manifest (Stage 142):
                         # the seed the pure bootstrap compiles; mutate only
                         # through make snapshot-regen, never by hand
boot/                    # The seed demoted to oracle (pure Python):
                         # differential suites, snapshot prove / regen --seed,
                         # and the maintenance ladder make bootstrap-seed;
                         # checker.py/interp.py are facades over the
                         # boot/checking/ and boot/interp_parts/ packages
std/                     # Standard library modules (pure HLS); large
                         # modules are a facade + internal parts under
                         # std/<module>/
examples/                # Demonstration programs
tests/ok/                # Valid programs (must compile + run)
tests/fail/              # Programs that MUST be rejected
tests/run_tests.sh       # Differential test runner (sources tests/suites/)
tests/suites/            # Test suites, one per stage group
Makefile                 # Core targets; stage sections live in mk/
mk/                      # Makefile stage sections (included in order)
```

### Standard library modules

Every module under `std/` must:

- Be **pure HLS** — no Python, no C.
- Declare **no `uses IO`** unless the module is explicitly about I/O
  (e.g. `std.time` reads the clock). Pure modules can be used inside the
  compiler `hlc` itself.
- Be **self-contained** — only import other `std/` modules.
- Ship a **companion test** under `tests/ok/feat_stdlib_<name>.hls` that
  exercises the public API with known-answer test vectors where possible.
- Ship a **companion example** under `examples/<name>_demo.hls` showing
  realistic usage.

### HLS code style

- 4-space indentation, no tabs (enforced via `.editorconfig`).
- `snake_case` for functions and variables, `PascalCase` for structs
  and enums.
- Every public function must have a one-line doc comment above it
  explaining what it does and any panic conditions.
- Use `panic` **only for programming bugs**. Expected errors must use
  `Result[T, E]` from `std.result`.
- No bitwise operators (`&`, `|`, `^`, `<<`, `>>`) — HLS does not
  have them. Decompose bit manipulations into multiply / modulo / division
  on `int64`.

### Commit messages

Format:
```
<area>: <imperative summary, <= 70 chars>

<blank line>
<body, wrapped at 72 chars>
<blank line>
<footer>
```

Areas used in this repo:
- `stdlib:`  — changes to `std/`
- `compiler:` — changes to `src/hlc.hls` / `src/hlc/`
- `boot:`    — changes to `boot/`
- `tests:`   — changes to `tests/`
- `docs:`    — documentation only
- `ci:`      — GitHub Actions / dependabot / .gitignore / .editorconfig
- `build:`   — Makefile / packaging

### The three guarantees

Every contribution must preserve the three core guarantees of Halis:

1. **I/O is a declared effect.** A function that does not declare
   `uses IO` must be statically guaranteed to perform no I/O.
2. **Every operation is checked.** No new "fast mode" that skips overflow
   or bounds checks.
3. **No null.** No uninitialised variables, no nullable references.

A PR that breaks any of these guarantees will be rejected, regardless of
performance benefits.

## Differential testing

The final gate of every PR is differential testing: the Stage-0
interpreter and the native compiler must produce identical output on
every test program. Any discrepancy is a bug, no exceptions.

If you add a new compiler feature, you MUST add at least one differential
test in `tests/ok/` that exercises it. If you change the semantics of
an existing feature, the existing differential tests must still pass —
otherwise the change is a breaking change and requires a major version
bump.

## Releasing

Releases are cut by maintainers by pushing a tag `vX.Y.Z` to `main` or
`feature/community-extensions`. The `.github/workflows/release.yml`
workflow builds a source tarball and creates a GitHub Release with
auto-generated release notes.

See [CHANGELOG.md](CHANGELOG.md) for the list of released versions.

## License

By contributing, you agree that your contributions are licensed under the
MIT License (see [LICENSE](LICENSE)).
