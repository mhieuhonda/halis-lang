#!/bin/sh
# ============================================================================
# install.sh — the Halis installer
# ============================================================================
# Installs the prebuilt toolchain from this repository's releases:
#
#   $PREFIX/bin/hlc            the compiler (a launcher — see below)
#   $PREFIX/bin/hlfmt          the formatter
#   $PREFIX/share/halis/       the SDK: bin/, std/, core/, VERSION
#
# Nothing builds here — that is the point. Every release archive
# carries the whole language (bin/hlc, bin/hlfmt, std/, core/), so
# this script's whole job is to fetch the right archive, prove it is
# exactly the one GitHub published, place it, and prove it works.
# No C compiler, no make, no Python: any machine with a POSIX shell,
# curl or wget, tar and a SHA-256 tool gets the toolchain — and the
# acceptance smoke at the end needs nothing beyond the toolchain
# itself, because hlc writes C rather than running it.
#
# The one law this script inherits from the source-building installer
# it replaced: trust the proof, not the promise. There the proof was
# the bootstrap's fixed-point law; here it is a pair of checks
# bracketing the download. Before: the archive's SHA-256 must match
# the release's own SHA256SUMS. After: the installed compiler must
# compile a program that imports std.result from a directory that has
# no std/ of its own — which is what the launcher below is for. A
# corrupt download, a broken mirror, a tampered archive: every one of
# them fails loudly before anything is left on the machine.
#
# Why a launcher: hlc resolves `import "std.*"` and `import "core.*"`
# against the entry file's parent directories and the invoking
# directory — it carries no configured library path. Inside a
# checkout that is exactly right; outside one, a bare compiler cannot
# see the library sitting next to it in the SDK. The launcher closes
# the gap: when the invocation cannot see a local std/ or core/, it
# re-anchors the invoking directory on the installed SDK. A std/ or
# core/ next to the invocation always wins — the compiler walks those
# first, and the launcher steps aside entirely when it sees one.
#
# Run from inside a checkout, the script still installs the release
# binaries — a tree you want to build from belongs to
# `make bootstrap`, not to an installer.
# ============================================================================

set -eu

REPO_URL="https://github.com/mhieuhonda/halis-lang"
API_URL="https://api.github.com/repos/mhieuhonda/halis-lang/releases/latest"
# The release download layout: $DIST_BASE/<tag>/<file>. Overridable
# for a mirror that serves the same shape (HALIS_DIST_SERVER).
DIST_BASE_DEFAULT="https://github.com/mhieuhonda/halis-lang/releases/download"

PREFIX_OPT=
REF_OPT=
NO_WRAPPER=0
UNINSTALL=0

# ---------------------------------------------------------------- helpers --

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_BOLD=$(printf '\033[1m')
    C_DIM=$(printf '\033[2m')
    C_OK=$(printf '\033[32m')
    C_INFO=$(printf '\033[36m')
    C_WARN=$(printf '\033[33m')
    C_ERR=$(printf '\033[31m')
    C_OFF=$(printf '\033[0m')
else
    C_BOLD=''
    C_DIM=''
    C_OK=''
    C_INFO=''
    C_WARN=''
    C_ERR=''
    C_OFF=''
fi

note() { printf '%s\n' "${C_INFO}::${C_OFF} $*"; }
ok()   { printf '%s\n' "${C_OK}ok${C_OFF}  $*"; }
warn() { printf '%s\n' "${C_WARN}::${C_OFF} $*" >&2; }
die()  { printf '%s\n' "${C_ERR}error:${C_OFF} $*" >&2; exit 1; }

step() { # step N TOTAL TEXT
    printf '%s\n' "${C_BOLD}[$1/$2]${C_OFF} $3"
}

usage() {
    cat <<'USAGE_EOF'
install.sh — the Halis installer (prebuilt releases)

Usage:
  ./install.sh [options]
  curl -fsSL \
    https://raw.githubusercontent.com/mhieuhonda/halis-lang/main/install.sh \
    | sh

Options:
  --prefix DIR    install under DIR (default: /usr/local when
                  writable, else ~/.local)
  --ref TAG       the release tag to install (v1.0.0). Default: the
                  latest stable release. Tags only — a branch or a
                  commit means building from a clone
  --no-wrapper    install the bare compiler on $PATH with no
                  launcher; std./core. imports then resolve only
                  against the entry file and the invoking directory
  --uninstall     remove exactly what an install recorded
  -h, --help      this text

Environment:
  HALIS_HOME         the SDK root the launcher anchors on, for a
                     relocated SDK
  HALIS_DIST_SERVER  a mirror serving the release layout
                     ($BASE/<tag>/<file>) instead of GitHub itself

Requirements: a POSIX shell, curl or wget, tar (plus unzip or
Windows' own tar for the .zip on Windows), and a SHA-256 tool
(sha256sum or shasum). Nothing compiles.
USAGE_EOF
    exit "${1:-0}"
}

# ------------------------------------------------------------------- args --

while [ $# -gt 0 ]; do
    case $1 in
        --prefix)     [ $# -ge 2 ] || die "--prefix expects a directory"; shift
                      PREFIX_OPT=$1 ;;
        --ref)        [ $# -ge 2 ] || die "--ref expects a release tag (v1.0.0)"; shift
                      REF_OPT=$1 ;;
        --no-wrapper) NO_WRAPPER=1 ;;
        --uninstall)  UNINSTALL=1 ;;
        -h|--help)    usage 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

# Where this script lives — only used to notice a checkout at hand,
# so `sh /path/to/halis-lang/install.sh` works from anywhere.
SELF_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

TMPDIR_BASE=
STAGE_DIR=

cleanup() {
    if [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ]; then
        rm -rf "$STAGE_DIR"
    fi
    if [ -n "$TMPDIR_BASE" ] && [ -d "$TMPDIR_BASE" ]; then
        rm -rf "$TMPDIR_BASE"
    fi
}
trap cleanup EXIT

# -------------------------------------------------------------- uninstall --

have_install_record() { # have_install_record PREFIX
    [ -f "$1/share/halis/.manifest" ]
}

do_uninstall() { # do_uninstall PREFIX
    prefix=$1
    note "uninstalling from $prefix"
    while read -r path || [ -n "$path" ]; do
        case $path in
            ''|'#'*) continue ;;
            "$prefix"/share/halis|"$prefix"/share/halis/*) ;;
            "$prefix"/bin/hlc|"$prefix"/bin/hlc.exe) ;;
            "$prefix"/bin/hlfmt|"$prefix"/bin/hlfmt.exe) ;;
            *)  die "refusing to remove an unexpected path: $path" ;;
        esac
        rm -rf "$path"
        printf '%s\n' "  removed  $path"
    done < "$prefix/share/halis/.manifest"
    # Debris from an install interrupted mid-swap: staging trees and
    # the previous install moved aside for a rename that never ran.
    rm -rf "$prefix"/share/halis.staging.* "$prefix"/share/halis.old.* 2>/dev/null || true
    ok "Halis is gone from $prefix"
}

if [ "$UNINSTALL" = 1 ]; then
    FOUND=0
    if [ -n "$PREFIX_OPT" ]; then
        have_install_record "$PREFIX_OPT" \
            || die "no recorded install under $PREFIX_OPT (looked for share/halis/.manifest)"
        do_uninstall "$PREFIX_OPT"
        FOUND=1
    else
        if have_install_record /usr/local; then
            do_uninstall /usr/local
            FOUND=1
        fi
        case ${HOME:-} in
            ''|/usr/local) ;;
            *)  if have_install_record "$HOME/.local"; then
                    do_uninstall "$HOME/.local"
                    FOUND=1
                fi ;;
        esac
    fi
    if [ "$FOUND" = 0 ]; then
        die "no recorded install found (looked for share/halis/.manifest)"
    fi
    exit 0
fi

# ----------------------------------------------------------------- prefix --

if [ -n "$PREFIX_OPT" ]; then
    PREFIX=${PREFIX_OPT%/}
    # A relative prefix would write a launcher and a manifest that
    # only work from the directory they were created in.
    case $PREFIX in
        /*) ;;
        *)  PREFIX=$PWD/$PREFIX ;;
    esac
else
    if [ "$(id -u)" = 0 ] || [ -w /usr/local ]; then
        PREFIX=/usr/local
    else
        PREFIX=${HOME:-$PWD}/.local
        note "/usr/local is not writable as this user — installing under $PREFIX"
    fi
fi

# The launcher is generated by a sed substitution over a template;
# these bytes would corrupt it. '/' is fine — the delimiter is '|'.
# (shellcheck disable=SC1003 below: `'\'' here is a QUOTED BACKSLASH
# inside a case pattern, not an escaped quote — POSIX shells all
# match it literally, and the pattern is load-bearing.)
# shellcheck disable=SC1003
case $PREFIX in
    *'|'*|*'&'*|*'\'*) die "the prefix must not contain '|', '&' or '\\': $PREFIX" ;;
esac

# Fail before the long steps, not after them.
mkdir -p "$PREFIX/bin" "$PREFIX/share" 2>/dev/null || true
if [ ! -d "$PREFIX/bin" ] || [ ! -w "$PREFIX/bin" ] \
   || [ ! -d "$PREFIX/share" ] || [ ! -w "$PREFIX/share" ]; then
    die "$PREFIX is not writable — re-run with sudo, or pass --prefix DIR"
fi

SDK_ROOT=$PREFIX/share/halis
DIST_BASE=${HALIS_DIST_SERVER:-$DIST_BASE_DEFAULT}
EXE=
TMPDIR_BASE=$(mktemp -d "${TMPDIR:-/tmp}/halis-install.XXXXXX") || die "mktemp failed"

TOTAL=6

# ------------------------------------------------------------ [1] probe --

step 1 "$TOTAL" "probing the environment"

UNAME_S=$(uname -s)
UNAME_M=$(uname -m)

OS=
ARCH=
ASSET_FMT=

case $UNAME_S in
    Linux*)     OS=linux ;;
    Darwin*)    OS=macos ;;
    MSYS*|MINGW*|CYGWIN*)
                OS=windows
                note "detected $UNAME_S — installing the native (MinGW) toolchain" ;;
    *)          OS=unknown ;;
esac

case $UNAME_M in
    x86_64|amd64)   ARCH=x86_64 ;;
    aarch64|arm64)  ARCH=aarch64 ;;
    *)              ARCH= ;;
esac

case "$OS-$ARCH" in
    linux-x86_64)   ASSET_FMT=tar.gz ;;
    linux-aarch64)  ASSET_FMT=tar.gz ;;
    macos-x86_64)   ASSET_FMT=tar.gz ;;
    macos-aarch64)  ASSET_FMT=tar.gz ;;
    windows-x86_64) ASSET_FMT=zip ;;
    windows-aarch64) ASSET_FMT=zip ;;
    *)  die "no prebuilt toolchain for ${UNAME_S}-${UNAME_M}.
The release set is linux / macos / windows on x86_64 and aarch64.
For anything else (FreeBSD, riscv64 hosts, 32-bit x86), build from a
clone — installation.md, 'Building from a clone'." ;;
esac

# A downloader.
FETCHER=
if command -v curl >/dev/null 2>&1; then
    FETCHER=curl
elif command -v wget >/dev/null 2>&1; then
    FETCHER=wget
else
    die "neither curl nor wget is available — install one, or download the archive by hand (installation.md, 'A manual install')"
fi

# A SHA-256 tool. Verification is not optional: an installer that
# cannot check what it downloaded refuses to install it.
HASHER=
if command -v sha256sum >/dev/null 2>&1; then
    HASHER=sha256sum
elif command -v shasum >/dev/null 2>&1; then
    HASHER=shasum
else
    die "no SHA-256 tool found (sha256sum or shasum) — the installer refuses to place an unchecked download. Install coreutils (or perl's shasum) and re-run."
fi

ok "$OS $ARCH — fetch: $FETCHER, sha256: $HASHER"

if [ -f "$SELF_DIR/Makefile" ] && [ -f "$SELF_DIR/snapshot/hlc.c" ]; then
    note "inside a checkout ($SELF_DIR) — installing the release binaries; to build this tree instead, run 'make bootstrap' there"
fi

# --------------------------------------------------- [2] resolve the tag --

step 2 "$TOTAL" "resolving the release"

api_tag() { # the latest release tag through the API, or nothing
    if [ "$FETCHER" = curl ]; then
        curl -fsSL --retry 3 "$API_URL" 2>/dev/null \
            | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
    else
        wget -qO- "$API_URL" 2>/dev/null \
            | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
    fi
}

redirect_tag() { # the tag the web UI's /releases/latest lands on, or nothing
    loc=
    if [ "$FETCHER" = curl ]; then
        loc=$(curl -fsS -o /dev/null -w '%{redirect_url}' \
              "$REPO_URL/releases/latest" 2>/dev/null || true)
    else
        loc=$(wget -q --spider -S "$REPO_URL/releases/latest" 2>&1 \
              | sed -n 's/.*[Ll]ocation:.*\/tag\///p' | tail -1)
    fi
    case $loc in
        */tag/v*) printf '%s\n' "${loc##*/tag/}" ;;
    esac
}

if [ -n "$REF_OPT" ]; then
    case $REF_OPT in
        v[0-9]*) TAG=$REF_OPT ;;
        *)  die "--ref expects a release tag like v1.0.0 — branches and commits are source builds (installation.md, 'Building from a clone')" ;;
    esac
else
    TAG=$(api_tag)
    case $TAG in
        v[0-9]*) ;;
        *)  TAG=$(redirect_tag) ;;
    esac
    case $TAG in
        v[0-9]*) ;;
        *)  die "could not determine the latest release (the GitHub API is rate-limited when unauthenticated). Pass --ref to name one, e.g. --ref v1.0.0" ;;
    esac
fi

ASSET="halis-${TAG}-${OS}-${ARCH}.${ASSET_FMT}"

ok "release $TAG — $ASSET"

# ------------------------------------------------- [3] download & verify --

step 3 "$TOTAL" "downloading and verifying"

fetch() { # fetch URL DEST
    if [ "$FETCHER" = curl ]; then
        curl -fsSL --retry 3 --retry-delay 2 "$1" -o "$2"
    else
        wget -q "$1" -O "$2"
    fi
}

ARCHIVE=$TMPDIR_BASE/$ASSET
SUMS_FILE=$TMPDIR_BASE/SHA256SUMS

fetch "$DIST_BASE/$TAG/SHA256SUMS" "$SUMS_FILE" \
    || die "could not fetch SHA256SUMS for $TAG from
  $DIST_BASE/$TAG/SHA256SUMS
Is $TAG a release tag? (A custom HALIS_DIST_SERVER names a mirror —
is it serving the release layout?)"

fetch "$DIST_BASE/$TAG/$ASSET" "$ARCHIVE" \
    || die "could not fetch $ASSET from
  $DIST_BASE/$TAG/$ASSET"

# The check itself: the checksum list is the release's own voice, and
# the digest is computed here, over the bytes on this disk.
expected=$(awk -v f="$ASSET" '$2 == f {print $1}' "$SUMS_FILE")
n=$(awk -v f="$ASSET" '$2 == f' "$SUMS_FILE" | wc -l | tr -d ' ')
if [ "$n" -ne 1 ] || [ -z "$expected" ]; then
    die "SHA256SUMS does not name $ASSET exactly once ($n entries) — this release does not carry an archive for $OS $ARCH. Pick another --ref, or see installation.md"
fi

digest() { # digest FILE -> the hex SHA-256, alone on stdout
    if [ "$HASHER" = sha256sum ]; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

actual=$(digest "$ARCHIVE")
if [ "$actual" != "$expected" ]; then
    printf '%s\n' "  expected $expected" >&2
    printf '%s\n' "  actual   $actual" >&2
    die "checksum mismatch for $ASSET — a corrupted download or a broken mirror. Re-run the installer; if it persists, report it (SECURITY.md)"
fi
ok "$ASSET matches SHA256SUMS ($(wc -c < "$ARCHIVE") bytes)"

# ---------------------------------------------------- [4] unpack & check --

step 4 "$TOTAL" "unpacking the SDK"

UNPACK_DIR=$TMPDIR_BASE/unpack
mkdir -p "$UNPACK_DIR"

case $ASSET in
    *.tar.gz)
        tar -xzf "$ARCHIVE" -C "$UNPACK_DIR" \
            || die "the archive did not unpack cleanly (a verified download that will not extract — report this)" ;;
    *.zip)
        if command -v unzip >/dev/null 2>&1; then
            unzip -q "$ARCHIVE" -d "$UNPACK_DIR" \
                || die "the archive did not unpack cleanly"
        elif [ -x /c/Windows/System32/tar.exe ]; then
            # Windows' own bsdtar (10 1803+) reads zips; the MSYS2
            # tar on PATH ahead of it is GNU tar and does not.
            /c/Windows/System32/tar.exe -xf "$ARCHIVE" -C "$UNPACK_DIR" \
                || die "the archive did not unpack cleanly"
        else
            die "no unzip on this system — 'pacman -S unzip' (MSYS2), or extract the archive by hand (installation.md, 'A manual install')"
        fi
        ;;
    *)  die "unknown archive format: $ASSET" ;;
esac

# Exactly one top-level directory, and it is the SDK.
SDK_SRC=
for d in "$UNPACK_DIR"/halis-*/; do
    [ -d "$d" ] || continue
    if [ -n "$SDK_SRC" ]; then
        die "the archive holds more than one top-level directory"
    fi
    SDK_SRC=${d%/}
done
[ -n "$SDK_SRC" ] || die "the archive has no halis-* top-level directory — not a Halis SDK archive"

# The pieces the SDK is not whole without. An archive with hlc but
# no std/ is a release from before the archives grew up — v1.0.0
# shipped the compiler alone; the error names the way out.
sdk_has_bin() { # sdk_has_bin NAME
    [ -f "$SDK_SRC/bin/$1" ] || [ -f "$SDK_SRC/bin/$1.exe" ]
}
sdk_missing=
sdk_has_bin hlc   || sdk_missing="bin/hlc"
sdk_has_bin hlfmt || sdk_missing="bin/hlfmt"
[ -d "$SDK_SRC/std" ]  || sdk_missing="std/"
[ -d "$SDK_SRC/core" ] || sdk_missing="core/"
if [ -n "$sdk_missing" ]; then
    die "the $TAG archive lacks $sdk_missing — releases before the full-SDK ones shipped the compiler alone. Pick a later release (--ref), or build from source (installation.md, 'Building from a clone')"
fi
if [ -f "$SDK_SRC/bin/hlc.exe" ]; then
    EXE=.exe
fi

STD_N=$(find "$SDK_SRC/std" -name '*.hls' | wc -l | tr -d ' ')
CORE_N=$(find "$SDK_SRC/core" -name '*.hls' | wc -l | tr -d ' ')
ok "the SDK is whole: bin/hlc$EXE, bin/hlfmt$EXE, std/ ($STD_N .hls files), core/ ($CORE_N .hls files)"

# ------------------------------------------------------------ [5] install --

step 5 "$TOTAL" "installing under $PREFIX"

STAGE_DIR=$PREFIX/share/halis.staging.$$
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR" || die "could not stage into $PREFIX/share"

# The archive layout is the SDK layout: bin/ + std/ + core/ as-is,
# plus whatever documentation travelled along.
cp -R "$SDK_SRC/bin"  "$STAGE_DIR/bin"  || die "could not stage bin/"
cp -R "$SDK_SRC/std"  "$STAGE_DIR/std"  || die "could not stage std/"
cp -R "$SDK_SRC/core" "$STAGE_DIR/core" || die "could not stage core/"
if [ -d "$SDK_SRC/examples" ]; then
    cp -R "$SDK_SRC/examples" "$STAGE_DIR/examples" || die "could not stage examples/"
fi
for f in LICENSE README.md; do
    if [ -f "$SDK_SRC/$f" ]; then
        cp "$SDK_SRC/$f" "$STAGE_DIR/$f" || die "could not stage $f"
    fi
done
# .zip extraction owes us no modes; the binaries get them back.
chmod 0755 "$STAGE_DIR/bin/"* || die "could not set the modes under bin/"

{
    printf 'version=%s\n' "$TAG"
    printf 'installed=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo unknown)"
    printf 'prefix=%s\n' "$PREFIX"
} > "$STAGE_DIR/VERSION"

# The uninstall record: every path this install creates, judged
# before it is ever acted on. Removing an install touches nothing
# else — not the prefix, not anything sharing it.
{
    printf '%s\n' "$SDK_ROOT"
    printf '%s\n' "$PREFIX/bin/hlc"
    printf '%s\n' "$PREFIX/bin/hlfmt"
    if [ -n "$EXE" ]; then
        printf '%s\n' "$PREFIX/bin/hlc.exe"
        printf '%s\n' "$PREFIX/bin/hlfmt.exe"
    fi
} > "$STAGE_DIR/.manifest"

# A courtesy word before anything is replaced: a file on the way out
# that no recorded install put there.
for f in "$PREFIX/bin/hlc" "$PREFIX/bin/hlfmt"; do
    if [ -e "$f" ] \
       && { [ ! -f "$SDK_ROOT/.manifest" ] || ! grep -qxF "$f" "$SDK_ROOT/.manifest"; }; then
        warn "$f exists but no recorded Halis install put it there — it will be replaced"
    fi
done

# The swap: stage first, rename second — a failed install never
# touches a working one, and the window is one rename wide.
if [ -d "$SDK_ROOT" ]; then
    OLD=$SDK_ROOT.old.$$
    mv "$SDK_ROOT" "$OLD"
    mv "$STAGE_DIR" "$SDK_ROOT"
    rm -rf "$OLD"
    note "replaced the previous install under $SDK_ROOT"
else
    mv "$STAGE_DIR" "$SDK_ROOT"
fi
STAGE_DIR=

install_bin() { # install_bin SRC DST — copy, then 0755
    cp "$1" "$2" || die "could not place $2"
    chmod 0755 "$2" || die "could not set the mode of $2"
}

# -------------------------------------------------------- the launcher --

# The launcher the compiler lacks: hlc resolves std./core. imports
# against the entry file's parent directories and the invoking
# directory — there is no configured library path. The script below
# re-anchors that last hop on the installed SDK whenever the
# invocation cannot see a std/ or core/ of its own, rewriting every
# path-bearing argument to absolute form so nothing else moves.
if [ "$NO_WRAPPER" = 1 ]; then
    install_bin "$SDK_ROOT/bin/hlc$EXE" "$PREFIX/bin/hlc.new"
    mv -f "$PREFIX/bin/hlc.new" "$PREFIX/bin/hlc$EXE"
else
    WRAPPER=$TMPDIR_BASE/hlc.launcher
    cat > "$WRAPPER" <<'LAUNCHER_EOF'
#!/bin/sh
# hlc — the installed launcher for the Halis compiler.
#
# The compiler resolves `import "std.*"` and `import "core.*"`
# against the entry file's directory, its ancestors, and the invoking
# directory — it carries no configured library path. This launcher
# closes that gap for an installed toolchain: when the invocation
# cannot see a local std/ or core/, it rewrites the path-bearing
# arguments to absolute form and re-anchors the invoking directory on
# the installed SDK, so the installed library is found from anywhere.
# A std/ or core/ next to the invocation always wins — the compiler
# walks those first, and this script passes through untouched when it
# sees one. (An entry file inside a checkout keeps its own walk: the
# compiler tries the entry file's directories before the invoking
# directory, so a project's library beats the SDK either way.)

set -eu

root='__HALIS_SDK_ROOT__'
if [ -n "${HALIS_HOME:-}" ]; then
    root=$HALIS_HOME
fi

real=$root/bin/hlc
if [ ! -f "$real" ]; then
    real=$root/bin/hlc.exe
fi

if [ ! -f "$real" ]; then
    printf '%s\n' "hlc: the Halis SDK at $root is missing or incomplete." >&2
    printf '%s\n' "     Re-run the installer, or point HALIS_HOME at the SDK." >&2
    exit 127
fi

# A library at hand: run the compiler exactly as invoked.
if [ -d ./std ] || [ -d ./core ]; then
    exec "$real" "$@"
fi

# Absolutise against the invoking directory, before it changes.
abs() {
    case $1 in
        /*) printf '%s\n' "$1" ;;
        *)  printf '%s/%s\n' "$PWD" "$1" ;;
    esac
}

# Rewrite argv in place — the rotate idiom: consume from the front,
# append to the back. The grammar, from `hlc` usage:
#     hlc [flags] <input.hls> <output.c|->
#     hlc --audit <input.hls>
# Path-bearing options: --pgo-use, --symbols, --audit.
# Value options that are not paths: -O, --target, --target-feature,
# --lto-threshold. Everything else passes through untouched, so a
# flag this script does not name cannot be mangled by it.
left=$#
expect=
while [ "$left" -gt 0 ]; do
    arg=$1
    shift
    out=$arg
    if [ -n "$expect" ]; then
        if [ "$expect" = path ]; then
            case $arg in
                -*) ;;  # stdout '-', or a flag where a path belonged
                *)  out=$(abs "$arg") ;;
            esac
        fi
        expect=
    else
        case $arg in
            --pgo-use|--symbols|--audit)
                expect=path ;;
            -O|--target|--target-feature|--lto-threshold)
                expect=val ;;
            -) ;;
            -*) ;;
            *)
                case $arg in
                    /*) ;;
                    *)  out=$(abs "$arg") ;;
                esac ;;
        esac
    fi
    set -- "$@" "$out"
    left=$((left - 1))
done

cd "$root"
exec "$real" "$@"
LAUNCHER_EOF

    sed "s|__HALIS_SDK_ROOT__|$SDK_ROOT|" "$WRAPPER" > "$PREFIX/bin/hlc.new" \
        || die "could not write the launcher"
    chmod 0755 "$PREFIX/bin/hlc.new"
    mv -f "$PREFIX/bin/hlc.new" "$PREFIX/bin/hlc"
fi

# The formatter needs no anchor — it formats the file it is given.
install_bin "$SDK_ROOT/bin/hlfmt$EXE" "$PREFIX/bin/hlfmt.new"
mv -f "$PREFIX/bin/hlfmt.new" "$PREFIX/bin/hlfmt$EXE"

# -------------------------------------------------------- [6] acceptance --

step 6 "$TOTAL" "acceptance — a real program, through the install"

# The installer's own gate, run the way a user would: from a neutral
# directory, through $PATH, with the standard library imported. The
# compile itself needs no C toolchain — hlc writes C; linking it is
# the user's business — so this gate runs on machines that own no
# compiler at all. Without the launcher there is no SDK resolution to
# prove, so the bare-binary smoke drops the import and the limitation
# is named instead.
SMOKE=$TMPDIR_BASE/acceptance
mkdir -p "$SMOKE"
if [ "$NO_WRAPPER" = 1 ]; then
    cat > "$SMOKE/smoke.hls" <<'SMOKE_EOF'
fn main() -> int uses IO {
    println("Hello, Halis!")
    return 0
}
SMOKE_EOF
    HLC_CMD=$PREFIX/bin/hlc$EXE
else
    cat > "$SMOKE/smoke.hls" <<'SMOKE_EOF'
import "std.result"

# A std-library round trip: if this compiles from a directory that
# owns no std/ of its own, the binary, the launcher and the installed
# library all did their part — and no C compiler was needed to
# prove it.
fn main() -> int uses IO {
    let r: Result[int, str] = Result.Ok(42)
    println("Hello, Halis! " + result_unwrap_or(r, 0).to_str())
    return 0
}
SMOKE_EOF
    HLC_CMD=$PREFIX/bin/hlc
fi

if ! ( cd "$SMOKE" && "$HLC_CMD" smoke.hls smoke.c ); then
    die "the installed compiler did not compile the smoke program —
  the binary refuses to run (wrong architecture? an emulator?) or the
  launcher cannot see the SDK; nothing else was left behind"
fi
[ -s "$SMOKE/smoke.c" ] || die "the compiler produced no C"
grep -q 'int main' "$SMOKE/smoke.c" \
    || die "the compiler produced C without a main — report this"

# The formatter, twice: once to format, once to prove the output is
# a fixed point — the formatter's own law, checked on the install.
"$PREFIX/bin/hlfmt" "$SMOKE/smoke.hls" > "$SMOKE/smoke.fmt.hls" \
    || die "the installed formatter does not run"
"$PREFIX/bin/hlfmt" "$SMOKE/smoke.fmt.hls" > "$SMOKE/smoke.fmt2.hls" \
    || die "the formatter refused its own output — report this"
cmp -s "$SMOKE/smoke.fmt.hls" "$SMOKE/smoke.fmt2.hls" \
    || die "the formatter is not idempotent over its own output — report this"

# When a C toolchain happens to be at hand, run the program too —
# the fuller proof, when it is free.
CCTOOL=
for c in cc gcc clang; do
    if command -v "$c" >/dev/null 2>&1; then
        CCTOOL=$c
        break
    fi
done
if [ -n "$CCTOOL" ]; then
    if ( cd "$SMOKE" && "$CCTOOL" -O2 -o smoke smoke.c -lm -pthread ) \
       && OUT=$( cd "$SMOKE" && ./smoke ); then
        case $OUT in
            *'Hello, Halis!'*) ok "smoke ran: $OUT" ;;
            *) warn "the smoke program ran but printed unexpectedly: $OUT" ;;
        esac
    else
        warn "the smoke C would not build with $CCTOOL — the toolchain itself is fine; your C setup is its own business"
    fi
else
    note "no C compiler on this machine — skipped the link-and-run leg (proving the toolchain needed none)"
fi

if [ "$NO_WRAPPER" = 1 ]; then
    warn "installed with --no-wrapper: std./core. imports resolve only against the entry file and the invoking directory — vendor std/ and core/ into a project, or run from a checkout, to import the library"
fi

# --------------------------------------------------------------- summary --

printf '\n'
printf '%s\n' "${C_BOLD}Halis $TAG is installed.${C_OFF}"
printf '\n'
printf '  %s\n' "${C_DIM}what landed where${C_OFF}"
if [ "$NO_WRAPPER" = 1 ]; then
    printf '  %-32s the compiler (--no-wrapper — no launcher)\n' "$PREFIX/bin/hlc$EXE"
else
    printf '  %-32s the compiler launcher\n' "$PREFIX/bin/hlc"
fi
printf '  %-32s the formatter\n' "$PREFIX/bin/hlfmt$EXE"
printf '  %-32s the SDK the launcher anchors on:\n' "$SDK_ROOT"
printf '      %-28s %s\n' "bin/hlc, bin/hlfmt" "the native binaries"
printf '      %-28s %s\n' "std/, core/" "the standard library, as source"
printf '      %-28s %s\n' "VERSION, .manifest" "the record of this install"
printf '\n'
printf '  %s\n' "${C_DIM}first program${C_OFF}"
printf '    cat > hello.hls <<'"'"'EOF'"'"'\n'
printf '    fn main() -> int uses IO {\n'
printf '        println("Hello, Halis!")\n'
printf '        return 0\n'
printf '    }\n'
printf '    EOF\n'
printf '    hlc hello.hls hello.c && cc -o hello hello.c -lm -pthread && ./hello\n'
if [ "$NO_WRAPPER" = 1 ]; then
    printf '    %s\n' "${C_DIM}(import the library from a checkout, or vendor std/ and core/ next to the program)${C_OFF}"
else
    printf '    %s\n' "${C_DIM}(imports of std./core. resolve against the installed SDK from any directory)${C_OFF}"
fi
printf '\n'

case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *)
        warn "$PREFIX/bin is not on this shell's PATH — add it:"
        printf '    export PATH="%s:%s"\n' "$PREFIX/bin" "\$PATH"
        printf '\n'
        ;;
esac

printf '  %s\n' "${C_DIM}uninstall${C_OFF}"
printf '    sh install.sh --uninstall          %s\n' \
       "${C_DIM}(or --prefix DIR, if you chose one)${C_OFF}"
printf '\n'
ok "done"

