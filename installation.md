# Installing Halis

This document covers every supported way to put the Halis toolchain —
the `hlc` compiler, the `hlfmt` formatter and the standard library —
on a machine: Linux, macOS, Windows (MSYS2, Git Bash or WSL2) and
containers, plus the from-source path for everything else.

The short version: a shell, and one command.

```bash
curl -fsSL \
  https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh \
  | sh
```

Nothing compiles. Since the release after v1.0.0 every release
archive carries the whole toolchain — the compiler and the formatter
as binaries, the standard library as source — so installing Halis is
a download, a checksum and a copy, not a build.

## What an install gives you

| Piece | Where | What it is |
|-------|-------|------------|
| `hlc` | `$PREFIX/bin/hlc` | The compiler, behind a small launcher (see [how imports resolve](#how-stdcore-imports-resolve)) |
| `hlfmt` | `$PREFIX/bin/hlfmt` | The opinionated formatter |
| `std/`, `core/` | `$PREFIX/share/halis/` | The standard library and the freestanding runtime, as source |
| `examples/`, `LICENSE`, `README.md` | `$PREFIX/share/halis/` | The canonical first program and the papers |
| `VERSION`, `.manifest` | `$PREFIX/share/halis/` | The record of the install — version, prefix, every path created |

`$PREFIX` is `/usr/local` when that is writable (or the installer runs
as root), otherwise `~/.local`. Pass `--prefix DIR` to choose
explicitly.

## Requirements

Far less than a source build asks for — no C compiler, no make, no
Python anywhere on the install path:

| Platform | Needs |
|----------|-------|
| Linux | `curl` or `wget`, `tar`, and `sha256sum` (coreutils) or `shasum` |
| macOS | the same set — all of it ships with the system |
| Windows (MSYS2) | the base shell plus `curl`; `pacman -S curl` if yours lacks it |
| Windows (Git Bash) | nothing — curl, tar and sha256sum all ship with Git for Windows |
| Windows (WSL2) | the Ubuntu/Debian set above, inside the distro |

The Linux binaries are statically linked, so the same archive runs on
glibc and musl alike — Debian, RHEL, Alpine, NixOS, old and new. The
macOS binaries target macOS 11 (Big Sur) and later.

## What the installer actually does

1. **Picks your archive** — the platform is probed from `uname`, and
   the matching release archive is chosen from the latest stable
   release (or the tag you name with `--ref`).
2. **Proves it is the one GitHub published** — the release's
   `SHA256SUMS` is fetched and the archive's digest is computed and
   compared, locally, before anything is unpacked. A corrupted
   download or a broken mirror stops the install right there.
3. **Unpacks and checks the SDK is whole** — `bin/hlc`, `bin/hlfmt`,
   `std/` and `core/` must all be present; an archive that predates
   the full-SDK releases (v1.0.0 shipped the compiler alone) is
   refused with a pointer to a newer one.
4. **Stages, then swaps** — everything is assembled in a staging
   directory and moved into place with one rename; a failed install
   never touches a working one.
5. **Runs its own acceptance gate** — from a neutral directory,
   through the installed launcher, it compiles a program that imports
   `std.result`, and the formatter formats that program twice (the
   second pass must equal the first — the formatter's own
   idempotence law). Compiling Halis to C needs no C toolchain, so
   this gate runs on machines that own no compiler at all; when one
   happens to be present, the smoke program is linked and run too.

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
| `--ref TAG` | Install this release tag (`v1.0.1`), not the latest. Tags only — branches and commits are source builds |
| `--no-wrapper` | Install the bare compiler with no launcher — see [below](#how-stdcore-imports-resolve) for what that costs |
| `--uninstall` | Remove exactly what an install recorded — nothing else |
| `-h`, `--help` | The usage text |

Environment: `HALIS_HOME` overrides the SDK root the launcher anchors
on, for relocated installs. `HALIS_DIST_SERVER` points the installer
at a mirror serving the release layout (`$BASE/<tag>/<file>`) — an
offline mirror of your own, or a caching proxy; the checksum still
has to match, but note that both the archive and its checksum then
come from the mirror, so the mirror is a trust decision you are
making consciously.

Run from inside a checkout, the script still installs the release
binaries — building the tree at hand is `make bootstrap`'s business,
not an installer's. The installer says as much when it notices.

## Platform notes

### Linux

Works on x86_64 and aarch64, any distribution: the binaries are
static, the library is source. Nothing to install beyond curl and
coreutils — on a truly minimal image that means:

```bash
apk add curl coreutils        # Alpine
apt-get install -y curl       # Debian, Ubuntu
dnf install -y curl           # Fedora, RHEL
```

then the one-liner. 32-bit x86 and riscv64 hosts have no prebuilt
archive — see [building from a clone](#building-from-a-clone).

### macOS

Both Apple silicon and Intel, macOS 11 and later, straight from the
one-liner — `curl`, `tar` and `shasum` all ship with the system. If
`~/.local` ends up the prefix (no writable `/usr/local`), add
`~/.local/bin` to `PATH` as the installer suggests.

### Windows

Three supported routes, in order of recommendation:

**Git Bash (the simplest).** Git for Windows carries everything the
installer needs — sh, curl, tar, sha256sum — and Windows 10's own
`tar` covers the `.zip` extraction even where `unzip` is absent. Open
Git Bash and run the one-liner unchanged.

**MSYS2 (the full POSIX shell).** The release binaries are built with
MinGW-w64 gcc — the same toolchain MSYS2 uses.

```bash
# MSYS2 MINGW64 shell (x86_64) or CLANGARM64 (aarch64):
pacman -S --needed curl
curl -fsSL https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh | sh
```

The `hlc` on `$PATH` is the launcher (a shell script); the compiler
itself is `hlc.exe`, and both run from any MSYS2 / Git Bash prompt.

**WSL2 (a Linux inside Windows).** Inside the distro, Halis installs
exactly as on Linux — same one-liner, Linux binaries, Linux
filesystem. Your programs build as Linux binaries.

```powershell
wsl --install -d Ubuntu
# then, inside Ubuntu:
sudo apt-get install -y curl
curl -fsSL https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh | sh
```

**PowerShell, no shell at all.** Download the `windows-x86_64.zip`
(or `-aarch64.zip`) from the release page, verify it against
`SHA256SUMS`, and extract it anywhere. Add the `bin` folder to `PATH`
for `hlc.exe` and `hlfmt.exe`. The one wrinkle is import resolution:
`hlc` finds `std/` through the entry file's directories and the
invoking directory, so either run it from the extracted folder
(`cd halis-v1.0.1; .\bin\hlc.exe C:\proj\hello.hls C:\proj\hello.c`),
or vendor `std/` and `core/` into your project — both are documented
in [how imports resolve](#how-stdcore-imports-resolve). For the full
experience, use Git Bash or MSYS2 and the installer, which sets all
of this up.

### FreeBSD, and everything else unlisted

No prebuilt archive is cut for FreeBSD (or 32-bit x86, or riscv64
hosts). The source path is short and the base system almost has it —
`clang` ships with FreeBSD, GNU make does not:

```bash
sudo pkg install -y gmake curl git
git clone https://github.com/mhieuhonda/halis-lang.git
cd halis-lang
gmake bootstrap && gmake bin/hlfmt
```

See [building from a clone](#building-from-a-clone) for the walk-through.

### Containers

The one-liner works in any image with curl and coreutils — no
build-essential, no toolchain layers:

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl coreutils \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL \
      https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh \
      | sh
```

Pin the version for reproducible images — the installer names the
tag, and the checksum pins the bytes:

```dockerfile
RUN curl -fsSL \
      https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh \
      | sh -s -- --ref v1.0.1
```

## Verifying the installation

The installer already compiled and formatted a `std.result` program
before reporting success — but to check by hand, from any directory:

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

## A manual install

The installer is convenience, not ceremony. To place a release by
hand:

1. Download the archive for your platform and the `SHA256SUMS` from
   [the releases page](https://github.com/mhieuhonda/halis-lang/releases).
2. Verify: `sha256sum --check SHA256SUMS` (or `shasum -a 256
   --check SHA256SUMS` on macOS).
3. Extract it where you want the SDK — the archive is one directory
   holding `bin/`, `std/`, `core/`, `examples/`, `LICENSE`,
   `README.md` and `VERSION`.
4. Put `bin` on `PATH` — and mind [how imports resolve](#how-stdcore-imports-resolve):
   either run `hlc` with the SDK root as the invoking directory, or
   vendor `std/` and `core/` next to your program, or copy the
   launcher trick out of `install.sh` (it is one self-contained
   script, and it is meant to be read).

## Upgrading and uninstalling

The same command upgrades: the installer stages the new SDK and swaps
it in with one rename, replacing the previous install. Nothing else
in the prefix is touched.

```bash
curl -fsSL https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh | sh
./install.sh --ref v1.1.0        # a specific line
```

Uninstalling removes exactly what the install recorded — the entries
in `$PREFIX/bin` and the SDK directory — and refuses to touch
anything else:

```bash
./install.sh --uninstall
./install.sh --uninstall --prefix DIR    # if you chose a prefix
```

## Building from a clone

Contributors — and anyone on a platform without prebuilt archives —
work from the source, and the checkout is the source of truth:

```bash
git clone https://github.com/mhieuhonda/halis-lang.git
cd halis-lang
make bootstrap          # ~20 s: cc compiles the committed C snapshot, HLS takes over
make bin/hlfmt          # the formatter, compiled by the fresh hlc
make run F=examples/hello.hls
make test               # the full suite (this one needs python3)
```

The bootstrap laws, the snapshot discipline and the commit
conventions live in [CONTRIBUTING.md](CONTRIBUTING.md). On FreeBSD,
substitute `gmake` for `make` (`pkg install gmake`).

A source build is also how you get the pieces the release binaries
deliberately leave out — see the next section.

## Optional pieces

**TLS in the standard library.** `net_tls_get` (the TLS builtin
behind `std.net`) compiles in only when libcurl's development files
are present at build time. The release binaries are built without
it — on purpose, so every platform behaves identically — so the
builtin panics with a clear "built without libcurl" message there;
everything else (TCP/UDP/DNS) works. To have it, build from source
with `libcurl4-openssl-dev` (Debian/Ubuntu), `libcurl-devel`
(Fedora) or `curl` (Homebrew) installed before `make bootstrap`.

**The Python lab tools.** The language server, the debug adapter,
the REPL, the linter, the test runner and the package manager are
Python programs in a checkout (`python3 tools/hls-lsp.py`, …). They
are lab tooling, deliberately outside the install path; run them
from a clone with `python3` 3.8+. The native essentials — `hlc`,
`hlfmt` — never need it.

**Editor support.** The VS Code extension lives in
[editors/vscode/halis/](editors/vscode/halis/) (LSP, formatter,
linter, debugger); Neovim syntax and plugin files in
[editors/neovim/](editors/neovim/). Copy them from a clone.

**Bare metal.** Freestanding work (`#![freestanding]`, `--target
x86_64-unknown-none`, custom link scripts) is done from a checkout —
the tree carries `targets/`, the linker-script machinery and the
acceptance gates an installed SDK does not.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `hlc: command not found` | `$PREFIX/bin` is not on `PATH`. For a `~/.local` install: `export PATH="$HOME/.local/bin:$PATH"` in your shell profile. |
| `error: module not found: std.…` | The compiler ran without the launcher and without a library in reach. Either a `--no-wrapper` install (vendor `std/` and `core/`, or reinstall without the flag), or a partial vendored `std/` shadows the SDK — the missing module is named in the error. |
| `no prebuilt toolchain for …` | Your platform is outside the release set (linux/macos/windows on x86_64/aarch64). [Build from a clone](#building-from-a-clone). |
| `checksum mismatch` | A corrupted download or a broken mirror. Re-run; if it persists, [report it](SECURITY.md). |
| `the vX.Y.Z archive lacks std/` | That release predates the full-SDK archives — v1.0.0 shipped the compiler alone. Install a later release: `--ref v1.0.1` or newer, or the latest. |
| `SHA256SUMS does not name …` | The release carries no archive for your platform, or `HALIS_DIST_SERVER` points at a mirror with a different layout. Pick another `--ref`, or unset the mirror. |
| `the installed compiler did not compile the smoke program` | The binary refuses to run on this machine — wrong architecture is the usual suspect. Check `uname -m` against the archive you installed; report anything else. |
| `$PREFIX is not writable` | Run under `sudo`, or pass `--prefix DIR` you own (the default flow already falls back to `~/.local` for non-root users). |
| `net_tls_get` panics with "built without libcurl" | Expected with the release binaries. Build from source with libcurl's dev package present (see [optional pieces](#optional-pieces)) to enable it. |
| could not determine the latest release | The unauthenticated GitHub API is rate-limited. Pass `--ref` to name a version explicitly. |
| could not fetch SHA256SUMS for … / could not fetch … | The tag is not a release tag, the network is down, or `HALIS_DIST_SERVER` points at a mirror that does not serve the release layout. Check the tag, retry, and mind the proxy — `curl` honours `http_proxy`/`https_proxy`. |

## Security notes

- Every download travels over HTTPS; the installer fetches only from
  `github.com` under this repository's name, or from a mirror you
  explicitly chose with `HALIS_DIST_SERVER`.
- The checksum is the gate. The archive's SHA-256 must match the
  release's own `SHA256SUMS` before anything is unpacked — and since
  the archive and the checksums normally travel together from
  GitHub, the release pipeline's record is what you are checking
  against, not a promise from the wire.
- Git tags can, in principle, be re-pointed. For a pinned, auditable
  install, read the release record in [RELEASES.md](RELEASES.md)
  alongside the tag you install, and prefer LTS lines for anything
  long-lived (the [LTS policy](LTS.md) fixes the support windows).
- The installer records every path it creates in
  `$PREFIX/share/halis/.manifest`, and `--uninstall` refuses to
  remove anything not listed there. It never escalates privileges by
  itself.

To report a vulnerability, see [SECURITY.md](SECURITY.md).
