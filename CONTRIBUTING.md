# Contributing to Halis (HLS)

Thank you for your interest in contributing to Halis! This document
describes how to set up your environment, the conventions we follow, and
how to submit changes.

## Quick start

```bash
git clone https://github.com/mhieuhonda/hieu-louis-lang.git
cd hieu-louis-lang
make bootstrap    # builds the native compiler via the bootstrap chain
make test         # 187 tests: interpreter, native, differential, bootstrap
```

Requirements: Python 3.8+ (only for the Stage-0 seed), gcc or clang.

## Branch model

| Branch                           | Purpose                                       |
|----------------------------------|-----------------------------------------------|
| `main`                           | The roadmap branch. Stages 1–9 complete; Stage 10-alpha, 10-beta, 11–15 alpha/beta shipped (current: v0.20.0-alpha). Each new roadmap stage lands here when ready. |
| `feature/community-extensions`   | Non-roadmap upgrades: new stdlib modules, tooling, examples, CI/CD. Smaller, more frequent releases cut from here. |
| `feature/*` / `fix/*`             | Short-lived topic branches.                   |

We **never** rewrite history on `main` or on `feature/community-extensions`.
Every commit must build (`make bootstrap`) and pass the full test suite
(`make test`).

### Branch protection (enforced on `main`)

The `main` branch is protected. The rules in effect:

- **Pull requests are required** for all non-admin pushers. Direct
  pushes to `main` are limited to the repo owner (admin).
- **CI must pass** on every PR: the full 2×2 matrix of Python
  3.8/3.11 × gcc/clang runs `make test`, `make bootstrap`, and the
  example programs. All four cells must pass before a PR can merge.
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
   make bootstrap    # confirms deterministic self-compilation
   ```
   Both must succeed before you push.
5. **Commit with a clear message** (see "Commit messages" below).
6. **Open a pull request** against the appropriate base branch. Reference
   the issue number in the PR description (`Closes #N`).
7. **Respond to review feedback** — push additional commits to the same
   branch; do not force-push unless asked.

## Conventions

### File layout

```
src/hlc.hls              # The compiler, written 100% in HLS
boot/                    # Stage-0 bootstrap seed (pure Python)
std/                     # Standard library modules (pure HLS)
examples/                # Demonstration programs
tests/ok/                # Valid programs (must compile + run)
tests/fail/              # Programs that MUST be rejected
tests/run_tests.sh       # Differential test runner
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
- `compiler:` — changes to `src/hlc.hls`
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
