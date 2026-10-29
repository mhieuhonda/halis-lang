# Installing Halis

This document covers every supported way to put the Halis toolchain —
the `hlc` compiler, the `hlfmt` formatter and the standard library —
on a machine: Linux, macOS, Windows (WSL2 or MSYS2), FreeBSD and
containers.

The short version: a C compiler, GNU make, and one command.

```bash
curl -fsSL \
  https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh \
  | sh
```

## What an install gives you

| Piece | Where | What it is |
|-------|-------|------------|
| `hlc` | `$PREFIX/bin/hlc` | The compiler, behind a small launcher (see [how imports resolve](#how-stdcore-imports-resolve)) |
| `hlfmt` | `$PREFIX/bin/hlfmt` | The opinionated formatter |
| `std/`, `core/` | `$PREFIX/share/halis/` | The standard library and the freestanding runtime, as source |
| `VERSION`, `.manifest` | `$PREFIX/share/halis/` | The record of the install — version, prefix, every path created |

`$PREFIX` is `/usr/local` when that is writable (or the installer runs
as root), otherwise `~/.local`. Pass `--prefix DIR` to choose
explicitly.

## Requirements

The whole list, per platform:

| Platform | Needs |
|----------|-------|
| Linux | `gcc` or `clang`, GNU `make`, and `curl` or `wget` unless installing from a clone |
| macOS | Xcode Command Line Tools (`xcode-select --install`) |
| Windows (WSL2) | the Ubuntu/Debian set above, inside the distro |
| Windows (MSYS2) | `mingw-w64-x86_64-gcc`, `make`, `curl` (see [the Windows section](#windows)) |
| FreeBSD | base `clang`, plus `gmake` (`pkg install gmake`) |

Python is **not** required. Nothing on the install path reads it —
the bootstrap is pure HLS + cc, and the installer honours the same
law.

## What the installer actually does

One rule shapes the whole design: **the installer does not trust the
download, it trusts the build.**

1. **Fetches the sources** — the tarball of the latest release tag
   (or a `--ref` you name: a tag, a branch, or a commit SHA via
   git). If the GitHub API is unreachable, it falls back to the
   pinned stable tag rather than guessing.
2. **Runs `make bootstrap`** — cc compiles the committed C snapshot,
   the native compiler re-derives itself from the HLS sources, and
   the derivation must equal the snapshot *byte for byte*. This is
   the fixed-point law, and it is the integrity check: a tampered or
   truncated tree fails here, loudly, before anything is installed.
   No checksum file is consulted because the build itself is a
   stronger witness than one.
3. **Builds `hlfmt`** with the freshly bootstrapped compiler.
4. **Stages, then swaps** — everything is assembled in a staging
   directory and moved into place with one rename; a failed install
   never touches a working one.
5. **Runs its own acceptance gate** — from a neutral directory,
   through the installed launcher, it compiles and runs a program
   that imports `std.result`. If the smoke does not print, the
   install reports failure instead of success.

The installer never escalates by itself. If the prefix is not
writable it stops and says so — run it under `sudo`, or point
`--prefix` somewhere you own.

## Options

```text
./install.sh [options]
```

| Option | Meaning |
|--------|---------|
| `--prefix DIR` | Install under DIR (default: `/usr/local` when writable, else `~/.local`) |
| `--ref REF` | Build this git ref — a tag (`v1.0.0`), a branch, or a commit SHA. Default: the latest release tag. A branch or SHA implies `--git` |
| `--git` | Clone with git instead of the source tarball |
| `--run-tests` | Run `make test` after the bootstrap (needs `python3`; the differential suites are lab tooling) |
| `--no-wrapper` | Install the bare compiler with no launcher — see [below](#how-stdcore-imports-resolve) for what that costs |
| `--keep-build` | Keep the fetched source tree instead of deleting it (path printed at the end) |
| `--uninstall` | Remove exactly what an install recorded — nothing else |

Environment: `CC` picks the C compiler make should use (`gcc` or
`clang`); `HALIS_HOME` overrides the SDK root the launcher anchors
on, for relocated installs.

Run from inside a checkout, the script builds that tree as-is — no
download, same laws. That is the contributor path: clone, `./install.sh`
if you want the tools on `$PATH`, or plain `make bootstrap` if you
do not.

## Platform notes

### Linux

Install the toolchain set for your distribution, then run the
one-liner:

```bash
sudo apt-get install -y build-essential        # Debian, Ubuntu, Mint
sudo dnf install -y gcc make                   # Fedora, RHEL
sudo pacman -S --needed gcc make               # Arch
sudo apk add build-base                        # Alpine
```

Works on x86_64, aarch64 and riscv64 hosts — the C compiler decides.

### macOS

```bash
xcode-select --install     # once, if the tools are not there yet
```

Then the one-liner. Both Apple silicon and Intel are supported (the
release CI builds and smoke-tests both).

### Windows

Three supported routes, in order of recommendation:

**WSL2 (the simplest).** Inside the distro, Halis installs exactly as
on Linux. Your programs build as Linux binaries.

```powershell
wsl --install -d Ubuntu
# then, inside Ubuntu:
sudo apt-get install -y build-essential
curl -fsSL https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh | sh
```

**MSYS2 (native binaries).** The generated C runtime is
pthreads-based, so the toolchain builds with MinGW-w64 gcc — the same
compiler the release CI uses for the Windows binaries.

```bash
# MSYS2 MINGW64 shell (x86_64) or CLANGARM64 (aarch64):
pacman -S --needed mingw-w64-x86_64-gcc make curl diffutils grep
curl -fsSL https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh | sh
```

Git Bash also carries a MinGW gcc and `make` — if yours does, the
same one-liner works there too.

**Prebuilt compiler only.** Every release publishes native `hlc`
archives for `windows-x86_64` and `windows-aarch64` with a
`SHA256SUMS` alongside. Download, verify, extract, put `hlc.exe` on
`PATH`. Note the honest limit: the release archives carry the
compiler alone — no `std/` — so programs that import the standard
library still need WSL2, MSYS2, or a checkout next to them. The
installer exists precisely to close that gap.

### FreeBSD

The base `clang` compiles the snapshot; only GNU make is missing
from a stock system:

```bash
sudo pkg install -y gmake curl
curl -fsSL https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh | sh
```

The installer probes `gmake` automatically when `make` is not GNU.

### Containers

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL \
      https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh \
      | sh
```

## Verifying the installation

The installer already compiled and ran a `std.result` program before
reporting success — but to check by hand, from any directory:

```bash
cat > hello.hls <<'EOF'
import "std.result"

fn main() -> int uses IO {
    let r: Result[int, str] = Result.Ok(42)
    println("Hello, Halis! " + result_unwrap_or(r, 0).to_str())
    return 0
}
EOF

hlc hello.hls hello.c
cc -o hello hello.c -lm -pthread
./hello        # Hello, Halis! 42
```

The installed version is recorded in `$PREFIX/share/halis/VERSION`.
(`hlc` itself does not carry a `--version` flag yet — the file is the
record.)

## How std./core. imports resolve

This is the one part of an installed Halis worth understanding, so it
is written out rather than hidden.

The compiler resolves `import "std.*"` and `import "core.*"` by
walking **the entry file's directory, then four directories above
it, then the invoking directory**. It has no configured library path
— inside a checkout that is exactly right, and outside one it means
a bare compiler cannot see the library.

The launcher the installer puts at `$PREFIX/bin/hlc` closes the gap:

- If the invocation can already see a `std/` or `core/` (a checkout,
  a vendored project), it steps aside entirely — the compiler runs
  exactly as invoked, and your library wins.
- Otherwise it rewrites the path-bearing arguments to absolute form
  and re-anchors the invoking directory on the installed SDK, so
  `import "std.result"` resolves from anywhere. Relative input and
  output paths keep their meaning; nothing else moves.

To vendor the library into a project instead — the reproducible way,
which pins the exact sources your program builds against:

```bash
ln -s /usr/local/share/halis/std  ~/myproject/std
ln -s /usr/local/share/halis/core ~/myproject/core
# or copy them, for a hermetic tree:
cp -R /usr/local/share/halis/std  ~/myproject/std
cp -R /usr/local/share/halis/core ~/myproject/core
```

`--no-wrapper` installs the compiler without the launcher. Imports
then resolve only against the entry file and the invoking directory —
useful inside a checkout or with vendored libraries, and the installer
says as much when you choose it.

## Upgrading and uninstalling

The same command upgrades: the installer stages the new SDK and swaps
it in with one rename, replacing the previous install. Nothing else
in the prefix is touched.

```bash
curl -fsSL https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh | sh
./install.sh --ref v1.1.0        # a specific line
```

Uninstalling removes exactly what the install recorded — the two
entries in `$PREFIX/bin` and the SDK directory — and refuses to touch
anything else:

```bash
./install.sh --uninstall
./install.sh --uninstall --prefix DIR    # if you chose a prefix
```

## Building from a clone

Contributors do not need the installer — the checkout is the source
of truth:

```bash
git clone https://github.com/mhieuhonda/halis-lang.git
cd halis-lang
make bootstrap          # ~20 s: cc builds the seed, HLS takes over
make run F=examples/hello.hls
make test               # the full suite (this one needs python3)
```

`./install.sh` run from inside the clone builds that tree and puts
the tools on `$PATH` — handy, never required. The bootstrap rules,
the snapshot laws and the commit conventions live in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Optional pieces

**TLS in the standard library.** `net_tls_get` (the TLS builtin
behind `std.net`) compiles in only when libcurl's development files
are present — the build auto-probes `curl-config`. Without them the
builtin panics with a clear "built without libcurl" message and
everything else (TCP/UDP/DNS) works. To enable it, install
`libcurl4-openssl-dev` (Debian/Ubuntu), `libcurl-devel` (Fedora) or
`curl` (Homebrew) **before** running the installer.

**The Python lab tools.** The language server, the debug adapter, the
REPL, the linter, the test runner and the package manager are Python
programs in a checkout (`python3 tools/hls-lsp.py`, …). They are lab
tooling, deliberately outside the install path; run them from a
clone with `python3` 3.8+. The native essentials — `hlc`, `hlfmt` —
never need it.

**Editor support.** The VS Code extension lives in
[editors/vscode/halis/](editors/vscode/halis/) (LSP, formatter,
linter, debugger); Neovim syntax and plugin files in
[editors/neovim/](editors/neovim/).

**Bare metal.** Freestanding work (`#![freestanding]`, `--target
x86_64-unknown-none`, custom link scripts) is done from a checkout —
the tree carries `targets/`, the linker-script machinery and the
acceptance gates an installed SDK does not.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `hlc: command not found` | `$PREFIX/bin` is not on `PATH`. For a `~/.local` install: `export PATH="$HOME/.local/bin:$PATH"` in your shell profile. |
| `error: module not found: std.…` | The compiler ran without the launcher and without a library in reach. Either a `--no-wrapper` install (vendor `std/` and `core/`, or reinstall without the flag), or a partial vendored `std/` shadows the SDK — the missing module is named in the error. |
| `no C compiler found (gcc or clang)` | Install the platform set from the [requirements table](#requirements); on macOS that is `xcode-select --install`. |
| `GNU make is required` | FreeBSD and some minimal systems ship a different `make`: `pkg install gmake` (FreeBSD) or install `make` from your package manager. |
| `BOOTSTRAP FAILED: bin/hlc re-derived C that differs from snapshot/hlc.c` | The fixed-point law refusing a tree that will not prove itself — a truncated or tampered download, almost always. Re-run the installer; if it persists from a clean fetch, [report it](SECURITY.md). |
| `$PREFIX is not writable` | Run under `sudo`, or pass `--prefix DIR` you own (the default flow already falls back to `~/.local` for non-root users). |
| `net_tls_get` panics with "built without libcurl" | Expected when the build could not probe libcurl. Install the dev package (see [optional pieces](#optional-pieces)) and re-run the installer. |
| Could not ask GitHub for the latest release | The unauthenticated API is rate-limited; the installer falls back to the pinned stable tag. Pass `--ref` to choose a version explicitly. |

## Security notes

- Every download travels over HTTPS; the installer fetches only from
  `github.com` / `raw.githubusercontent.com` under this repository's
  name.
- The build is the integrity gate. A tampered source tree cannot
  pass `make bootstrap` — the snapshot re-derivation fails — and the
  acceptance smoke catches a compiler that compiles but does not
  work. No separate checksum is trusted along the way.
- Git tags can, in principle, be re-pointed. For a pinned,
  auditable install, build an exact commit: `--ref <full SHA>` (this
  implies a git clone), and read the release record in
  [RELEASES.md](RELEASES.md).
- The installer records every path it creates in
  `$PREFIX/share/halis/.manifest`, and `--uninstall` refuses to
  remove anything not listed there. It never escalates privileges by
  itself.

To report a vulnerability, see [SECURITY.md](SECURITY.md).
