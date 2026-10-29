#!/bin/sh
# ============================================================================
# install.sh — the Halis installer
# ============================================================================
# Builds the toolchain from source and installs it under a prefix:
#
#   $PREFIX/bin/hlc            the compiler (a launcher — see below)
#   $PREFIX/bin/hlfmt          the formatter
#   $PREFIX/share/halis/       the SDK: bin/, std/, core/, VERSION
#
# One law governs the whole script: the installer does not trust the
# download, it trusts the build. Everything is compiled from the tree
# by `make bootstrap`, and the bootstrap refuses a tampered one — the
# compiler built from the committed C snapshot must re-derive that
# snapshot byte for byte, or the install stops before anything lands
# on the machine. No checksum file is consulted because none is
# needed: the fixed-point law IS the integrity check, and it runs on
# every install, not only on release day.
#
# Why source, not the release binaries: a Halis program that writes
# `import "std.result"` needs the library next to it, and the compiler
# resolves that import against the entry file's parent directories
# and the invoking directory — it carries no configured library path.
# The release archives ship the compiler alone, so an install that is
# useful outside a checkout must place std/ and core/ somewhere
# stable and make the compiler find them. This script does exactly
# that, and the launcher it puts on $PATH closes the gap: when the
# invocation cannot see a local std/ or core/, the launcher re-anchors
# the invoking directory on the installed SDK. A std/ or core/ next
# to the invocation always wins — the compiler walks those first.
#
# Requirements: a C compiler (gcc or clang) and GNU make. Nothing
# else — no Python anywhere on the install path.
#
# Run from inside a checkout, the script builds that tree as-is —
# no download, same laws.
# ============================================================================

set -eu

REPO_URL="https://github.com/mhieuhonda/halis-lang"
API_URL="https://api.github.com/repos/mhieuhonda/halis-lang/releases/latest"
# The fallback when the API is unreachable or rate-limited: the
# current stable line, kept current by the release discipline.
DEFAULT_TAG="v1.0.0"

PREFIX_OPT=
REF_OPT=
USE_GIT=0
RUN_TESTS=0
NO_WRAPPER=0
KEEP_BUILD=0
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
install.sh — the Halis installer

Usage:
  ./install.sh [options]
  curl -fsSL <this file> | sh

Options:
  --prefix DIR    install under DIR (default: /usr/local when
                  writable, else ~/.local)
  --ref REF       the git ref to build — a tag (v1.0.0), a branch,
                  or a commit SHA. Default: the latest release tag.
                  A branch or SHA implies --git.
  --git           clone with git instead of the source tarball
                  (default for branches and SHAs; needs git)
  --run-tests     run `make test` after the bootstrap (needs
                  python3 — the differential suites are lab tooling)
  --no-wrapper    install the bare compiler on $PATH with no
                  launcher; std./core. imports then resolve only
                  against the entry file and the invoking directory
  --keep-build    keep the fetched source tree (path printed)
  --uninstall     remove exactly what an install recorded
  -h, --help      this text

Environment:
  CC              the C compiler make should use (gcc or clang)
  HALIS_HOME      overrides the SDK root the launcher anchors on

Run from inside a checkout, the script builds that tree as-is —
no download, same laws.
USAGE_EOF
    exit "${1:-0}"
}

# ------------------------------------------------------------------- args --

while [ $# -gt 0 ]; do
    case $1 in
        --prefix)     [ $# -ge 2 ] || die "--prefix expects a directory"; shift
                      PREFIX_OPT=$1 ;;
        --ref)        [ $# -ge 2 ] || die "--ref expects a tag, branch or SHA"; shift
                      REF_OPT=$1 ;;
        --git)        USE_GIT=1 ;;
        --run-tests)  RUN_TESTS=1 ;;
        --no-wrapper) NO_WRAPPER=1 ;;
        --keep-build) KEEP_BUILD=1 ;;
        --uninstall)  UNINSTALL=1 ;;
        -h|--help)    usage 0 ;;
        *)            die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

# Where this script lives — the in-tree check judges that directory,
# not the invoking one, so `sh /path/to/halis-lang/install.sh` works.
SELF_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

TMPDIR_BASE=
BUILD_DIR=
DOWNLOADED=0
STAGE_DIR=

cleanup() {
    if [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ]; then
        rm -rf "$STAGE_DIR"
    fi
    if [ "$DOWNLOADED" = 1 ] && [ -n "$BUILD_DIR" ] && [ -d "$BUILD_DIR" ]; then
        if [ "$KEEP_BUILD" = 1 ]; then
            printf '%s\n' "${C_DIM}build tree kept at $BUILD_DIR${C_OFF}"
        else
            rm -rf "$BUILD_DIR"
        fi
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
    # the previous install moved aside for the rename that never ran.
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

case $PREFIX in
    *'|'*) die "the prefix must not contain a '|': $PREFIX" ;;
esac

# Fail before the long steps, not after them.
mkdir -p "$PREFIX/bin" "$PREFIX/share" 2>/dev/null || true
if [ ! -d "$PREFIX/bin" ] || [ ! -w "$PREFIX/bin" ] \
   || [ ! -d "$PREFIX/share" ] || [ ! -w "$PREFIX/share" ]; then
    die "$PREFIX is not writable — re-run with sudo, or pass --prefix DIR"
fi

SDK_ROOT=$PREFIX/share/halis
EXE=
TMPDIR_BASE=$(mktemp -d "${TMPDIR:-/tmp}/halis-install.XXXXXX") || die "mktemp failed"

TOTAL=6
if [ "$RUN_TESTS" = 1 ]; then
    TOTAL=7
fi

# ------------------------------------------------------------ [1] probe --

step 1 "$TOTAL" "probing the environment"

UNAME_S=$(uname -s)
UNAME_M=$(uname -m)

case $UNAME_S in
    Linux*)     OS=linux ;;
    Darwin*)    OS=macos ;;
    FreeBSD*)   OS=freebsd ;;
    MSYS*|MINGW*|CYGWIN*)
                OS=windows-posix
                note "detected $UNAME_S — building the native (MinGW) toolchain" ;;
    *)          OS=unknown
                warn "unrecognised system $UNAME_S — proceeding best-effort" ;;
esac

case $UNAME_M in
    x86_64|amd64)   ARCH=x86_64 ;;
    aarch64|arm64)  ARCH=aarch64 ;;
    riscv64)        ARCH=riscv64 ;;
    *)              ARCH=$UNAME_M
                    warn "unusual architecture $UNAME_M — the C compiler decides" ;;
esac

# CC: honour the caller's choice, else prefer gcc, else clang. The
# Makefile defaults to gcc but honours the environment either way.
if [ -n "${CC:-}" ]; then
    command -v "$CC" >/dev/null 2>&1 || die "CC=$CC is not on this machine"
elif command -v gcc >/dev/null 2>&1; then
    CC=gcc
elif command -v clang >/dev/null 2>&1; then
    CC=clang
else
    case $OS in
        linux)  CC_HINT='apt-get install build-essential    (Debian/Ubuntu)
dnf install gcc make                   (Fedora)
pacman -S gcc make                     (Arch)
apk add build-base                     (Alpine)' ;;
        macos)  CC_HINT='xcode-select --install' ;;
        freebsd) CC_HINT='the base clang is enough — pkg install gmake' ;;
        *)      CC_HINT='install gcc or clang, plus GNU make' ;;
    esac
    die "no C compiler found (gcc or clang). On this system:
$CC_HINT"
fi
export CC

# GNU make. FreeBSD ships pmake as `make`, so gmake is tried too.
MAKE=
for m in make gmake; do
    if command -v "$m" >/dev/null 2>&1 \
       && "$m" --version 2>/dev/null | head -1 | grep -q 'GNU'; then
        MAKE=$m
        break
    fi
done
if [ -z "$MAKE" ]; then
    die "GNU make is required (FreeBSD: pkg install gmake)"
fi

# Downloader, unless the tree is already at hand.
if command -v curl >/dev/null 2>&1; then
    FETCHER=curl
elif command -v wget >/dev/null 2>&1; then
    FETCHER=wget
else
    FETCHER=
fi

GIT_BIN=
if command -v git >/dev/null 2>&1; then
    GIT_BIN=git
fi

ok "$OS $ARCH — cc: $CC, make: $MAKE"

# ----------------------------------------------------- [2] acquire sources --

step 2 "$TOTAL" "acquiring the sources"

IN_TREE=0
if [ -f "$SELF_DIR/Makefile" ] && [ -f "$SELF_DIR/snapshot/hlc.c" ] \
   && [ -d "$SELF_DIR/std" ]; then
    IN_TREE=1
fi

VERSION=

if [ "$IN_TREE" = 1 ]; then
    if [ -n "$REF_OPT" ]; then
        die "this script sits inside a checkout ($SELF_DIR) — it builds that tree; drop --ref, or run the script from elsewhere"
    fi
    BUILD_DIR=$SELF_DIR
    if [ -d "$BUILD_DIR/.git" ] && [ -n "$GIT_BIN" ]; then
        VERSION="in-tree $("$GIT_BIN" -C "$BUILD_DIR" rev-parse --short HEAD 2>/dev/null || echo snapshot)"
    else
        VERSION=in-tree
    fi
    note "building the tree at hand: $BUILD_DIR ($VERSION)"
else
    if [ -z "$FETCHER" ]; then
        die "neither curl nor wget is available — install one, or clone the repo and run ./install.sh from inside it"
    fi

    # Resolve the ref: explicit, then the latest release tag, then the
    # pinned fallback. A rate-limited or offline API must not stop an
    # install that can still fetch the tarball by name.
    if [ -n "$REF_OPT" ]; then
        REF=$REF_OPT
    else
        REF=
        if [ "$FETCHER" = curl ]; then
            REF=$(curl -fsSL "$API_URL" 2>/dev/null \
                  | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
        else
            REF=$(wget -qO- "$API_URL" 2>/dev/null \
                  | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
        fi
        if [ -z "$REF" ]; then
            REF=$DEFAULT_TAG
            warn "could not ask GitHub for the latest release — falling back to $REF"
        fi
    fi

    # Tags travel as tarballs; anything else (a branch, a SHA) is a
    # moving target and travels through git.
    case $REF in
        v[0-9]*.[0-9]*.[0-9]*) ;;
        *)  USE_GIT=1 ;;
    esac

    BUILD_DIR=$TMPDIR_BASE/src
    if [ "$USE_GIT" = 1 ]; then
        if [ -z "$GIT_BIN" ]; then
            die "--ref $REF needs git (or pass a release tag like v1.0.0)"
        fi
        note "cloning $REPO_URL at $REF"
        case $REF in
            [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*)
                "$GIT_BIN" clone -q "$REPO_URL" "$BUILD_DIR" \
                    || die "git clone failed"
                "$GIT_BIN" -C "$BUILD_DIR" checkout -q --detach "$REF" \
                    || die "no such commit: $REF"
                ;;
            *)
                "$GIT_BIN" clone -q --depth 1 --branch "$REF" "$REPO_URL" "$BUILD_DIR" \
                    || die "git clone failed (no such branch or tag: $REF?)"
                ;;
        esac
        VERSION="$REF $("$GIT_BIN" -C "$BUILD_DIR" rev-parse --short HEAD)"
    else
        note "downloading $REPO_URL at $REF (source tarball)"
        TARBALL=$TMPDIR_BASE/halis.tar.gz
        if [ "$FETCHER" = curl ]; then
            curl -fsSL "$REPO_URL/archive/refs/tags/$REF.tar.gz" -o "$TARBALL" \
                || die "download failed — is $REF a release tag?"
        else
            wget -q "$REPO_URL/archive/refs/tags/$REF.tar.gz" -O "$TARBALL" \
                || die "download failed — is $REF a release tag?"
        fi
        mkdir -p "$BUILD_DIR"
        tar -xzf "$TARBALL" -C "$BUILD_DIR" --strip-components=1 \
            || die "the tarball did not unpack cleanly"
        VERSION=$REF
    fi
    DOWNLOADED=1
fi

# The tree must look like the tree — a truncated download stops here.
if [ ! -f "$BUILD_DIR/snapshot/hlc.c" ] || [ ! -d "$BUILD_DIR/std" ]; then
    die "the source tree at $BUILD_DIR is incomplete (no snapshot, no std/)"
fi

# ---------------------------------------------------------- [3] bootstrap --

step 3 "$TOTAL" "bootstrap — the fixed-point law"

# make bootstrap: cc compiles the committed C snapshot, the native
# compiler re-derives itself from the HLS sources, and the derivation
# must equal the snapshot byte for byte. This step is the integrity
# gate — a tampered tree fails it loudly, before anything installs.
if ! ( cd "$BUILD_DIR" && "$MAKE" bootstrap ); then
    die "bootstrap failed — the tree failed to prove itself; re-fetch it, and report if it persists"
fi

# MinGW appends .exe behind make's back; meet the artifact where it is.
if [ ! -f "$BUILD_DIR/bin/hlc" ] && [ ! -f "$BUILD_DIR/bin/hlc.exe" ]; then
    die "bootstrap reported success but produced no compiler"
fi
if [ -f "$BUILD_DIR/bin/hlc.exe" ] && [ ! -f "$BUILD_DIR/bin/hlc" ]; then
    EXE=.exe
fi

# --------------------------------------------------------- [4] formatter --

step 4 "$TOTAL" "building the formatter"

if ! ( cd "$BUILD_DIR" && "$MAKE" bin/hlfmt ); then
    die "the formatter failed to build — the compiler built, so report this"
fi
if [ ! -f "$BUILD_DIR/bin/hlfmt$EXE" ]; then
    die "the formatter build produced no binary"
fi

# ---------------------------------------------------------- [tests, opt] --

if [ "$RUN_TESTS" = 1 ]; then
    step 5 "$TOTAL" "the test suite (this is the long one)"
    if ! command -v python3 >/dev/null 2>&1; then
        die "--run-tests needs python3 (the differential suites are lab tooling)"
    fi
    TEST_LOG=$TMPDIR_BASE/make-test.log
    if ( cd "$BUILD_DIR" && "$MAKE" test ) >"$TEST_LOG" 2>&1; then
        ok "the whole suite passed"
    else
        tail -40 "$TEST_LOG" >&2
        die "make test failed — the tail of the log is above"
    fi
fi

# ------------------------------------------------------------ [5] install --

CUR=5
if [ "$RUN_TESTS" = 1 ]; then
    CUR=6
fi
step "$CUR" "$TOTAL" "installing under $PREFIX"

STAGE_DIR=$PREFIX/share/halis.staging.$$
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR/bin" || die "could not stage into $PREFIX/share"

install_bin() { # install_bin SRC DST — copy, then 0755
    cp "$1" "$2" || die "could not place $2"
    chmod 0755 "$2" || die "could not set the mode of $2"
}

install_bin "$BUILD_DIR/bin/hlc$EXE"   "$STAGE_DIR/bin/hlc$EXE"
install_bin "$BUILD_DIR/bin/hlfmt$EXE" "$STAGE_DIR/bin/hlfmt$EXE"

# The library travels as source — the compiler reads it at the build
# of every user program, exactly as it does inside a checkout.
cp -R "$BUILD_DIR/std"  "$STAGE_DIR/std"  || die "could not stage std/"
cp -R "$BUILD_DIR/core" "$STAGE_DIR/core" || die "could not stage core/"

{
    printf 'version=%s\n' "$VERSION"
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
# sees one.

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
        if [ "$expect" = path ] && [ "$arg" != - ]; then
            out=$(abs "$arg")
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

CUR=$((CUR + 1))
step "$CUR" "$TOTAL" "acceptance — a real program, compiled and run"

# The installer's own gate, run the way a user would: from a neutral
# directory, through $PATH, with the standard library imported — the
# release workflow's smoke, plus the one thing it cannot test: the
# installed SDK resolving from anywhere. Without the launcher there
# is no SDK resolution to prove, so the smoke drops the import and
# the limitation is named instead.
SMOKE=$TMPDIR_BASE/acceptance
mkdir -p "$SMOKE"
if [ "$NO_WRAPPER" = 1 ]; then
    cat > "$SMOKE/smoke.hls" <<'SMOKE_EOF'
fn main() -> int uses IO {
    println("Hello, Halis!")
    return 0
}
SMOKE_EOF
else
    cat > "$SMOKE/smoke.hls" <<'SMOKE_EOF'
import "std.result"

# A std-library round trip and the CI smoke line in one program: if
# this compiles and prints, the binary, the launcher and the library
# all did their part.
fn main() -> int uses IO {
    let r: Result[int, str] = Result.Ok(42)
    println("Hello, Halis! " + result_unwrap_or(r, 0).to_str())
    return 0
}
SMOKE_EOF
fi

if ! ( cd "$SMOKE" && "$PREFIX/bin/hlc" smoke.hls smoke.c ); then
    die "the installed compiler did not compile the smoke program"
fi
if ! ( cd "$SMOKE" && "$CC" -O2 -o "smoke$EXE" smoke.c -lm -pthread ); then
    die "the smoke program's C did not build — odd; report this"
fi
OUT=$( cd "$SMOKE" && "./smoke$EXE" ) || die "the smoke program did not run"
case $OUT in
    *'Hello, Halis!'*)
        ok "smoke: $OUT"
        if [ "$NO_WRAPPER" = 1 ]; then
            warn "installed with --no-wrapper: std./core. imports resolve only against the entry file and the invoking directory — vendor std/ and core/ into a project, or run from a checkout, to import the library"
        fi
        ;;
    *) die "unexpected smoke output: $OUT" ;;
esac

# The formatter answers, quietly.
if ! "$PREFIX/bin/hlfmt" -h >/dev/null 2>&1; then
    die "the installed formatter does not run"
fi

# --------------------------------------------------------------- summary --

printf '\n'
printf '%s\n' "${C_BOLD}Halis $VERSION is installed.${C_OFF}"
printf '\n'
printf '  %s\n' "${C_DIM}what landed where${C_OFF}"
if [ "$NO_WRAPPER" = 1 ]; then
    printf '  %-32s the compiler (--no-wrapper — no launcher)\n' "$PREFIX/bin/hlc"
else
    printf '  %-32s the compiler launcher\n' "$PREFIX/bin/hlc"
fi
printf '  %-32s the formatter\n' "$PREFIX/bin/hlfmt"
printf '  %-32s the SDK the launcher anchors on:\n' "$SDK_ROOT"
printf '      %-28s %s\n' "bin/hlc, bin/hlfmt" "the native binaries"
printf '      %-28s %s\n' "std/, core/" "the standard library"
printf '      %-28s %s\n' "VERSION" "the record of this install"
printf '\n'
printf '  %s\n' "${C_DIM}first program${C_OFF}"
printf '    cat > hello.hls <<'"'"'EOF'"'"'\n'
printf '    fn main() -> int uses IO {\n'
printf '        println("Hello, Halis!")\n'
printf '        return 0\n'
printf '    }\n'
printf '    EOF\n'
if [ "$NO_WRAPPER" = 1 ]; then
    printf '    hlc hello.hls hello.c && %s -o hello hello.c -lm -pthread && ./hello\n' "$CC"
    printf '    %s\n' "${C_DIM}(import the library from a checkout, or vendor std/ and core/ next to the program)${C_OFF}"
else
    printf '    hlc hello.hls hello.c && %s -o hello hello.c -lm -pthread && ./hello\n' "$CC"
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
