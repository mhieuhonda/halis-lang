#!/usr/bin/env python3
"""hls-repro — Stage 105 (v0.124.0-alpha): reproducible-build
verification across distros.

Usage:
  python3 tools/hls-repro.py <entry.hls> [--builds N]
                            [--engine auto|boot|native]
                            [--cc CMD] [--cflags STR] [--epoch N]
                            [--out DIR] [--scratch DIR] [--json]
                            [--no-smoke]
  python3 tools/hls-repro.py --pkg [DIR] [the same options] [--release]
  python3 tools/hls-repro.py --verify FILE [--in DIR] [--json]

The claim. A build you cannot rebuild is a build you cannot trust:
`hls-pkg lock` pins the inputs (Stage 13), `hls-audit` explains them
(Stage 103), `hls-sbom` lists them for the parties who ask (Stage
104) — and none of that says the BYTES come back. This tool makes the
round trip: build the same tree N times in N isolated build roots
under N deliberately different environments, and require the
artifacts to come back byte-identical. Not "should reproduce" — did.

Across distros, honestly. A single machine cannot literally run five
distros, so the tool does what is TRUE on one machine: it varies every
environmental input a distro build daemon varies — the build-root path
(length and letters), the locale set, the timezone, the umask, the
Python hash seed, and the breadth of the environment itself (each
build starts from a MINIMAL env, not your shell's). The five profiles
are named for the archetypes (baseline, glibc-debian, musl-alpine,
bsd-sandbox, nix-long). The full cross-distro claim is then exactly
one command away ON the other distro: `hls-repro --verify
<buildinfo>` rebuilds there and compares the hashes — the buildinfo
file is the carrier of the claim between machines.

The recipe. Everything the toolchain could read from the environment
is pinned by the recipe, not by luck:

  - the tree is staged HERMETICALLY into each build root — exactly
    the modules hls-audit's own walkers name (one definition of the
    tree, shared with hls-audit and hls-sbom), never the working
    copy's leftovers;
  - the intermediate C file is ALWAYS named build/hl_repro.c. This
    is not cosmetic: gcc records the intermediate C's NAME in the
    ELF symbol table (the STT_FILE symbol), so two builds over
    byte-identical C differ in exactly one symbol-table byte when
    the intermediate name differs. Found the hard way in Stage 105's
    own first experiment; the recipe names the C so the artifact
    never sees your filesystem;
  - the cc invocation is fixed (recorded flags, `-lm -pthread`
    appended, cwd = the build root, relative paths only);
  - SOURCE_DATE_EPOCH is honored when the environment or --epoch
    names one, and DERIVED from the tree digest otherwise — the
    epoch is content, not clock: int(tree_digest[:8], 16) % 2**31.

The matrix. `--builds N` (default 2, max 5) picks the first N
profiles in rotation. The buildinfo records which profiles ran and
their exact deltas, so the claim is scoped: byte-identical under
THIS environment matrix — and re-verifiable anywhere.

The smoke. When every build produced a binary, each one is run once
(its own profile's env, stdin closed, a timeout); if all builds agree
on exit code and stdout, the buildinfo records the stdout hash — the
artifacts are not just the same bytes, they DO the same thing. A
program that needs arguments or a socket simply fails to agree
consistently or fails to run; the smoke is then `skipped` and never
fails the gate. Build reproducibility is the contract; runtime
determinism is recorded when it can be, for free.

The diagnosis. A mismatch is not just "failed": the diagnoser finds
the first differing byte, counts the differing bytes, and hunts the
classic leak classes — the build-root path embedded in the artifact,
the intermediate C's name in the symbol table, and the timestamp
macros (__DATE__, __TIME__, __TIMESTAMP__) in the generated C. When
the C stage is identical but the binary is not, the report says so —
the divergence is downstream of the compiler (assembler/linker
environment or a leaked intermediate name), with the bytes around
the first difference printed as evidence.

verify. `--verify FILE` re-runs the whole pipeline on the CURRENT
tree: the buildinfo's input hashes are re-checked FIRST (a tree that
is not what the buildinfo describes refuses before anything is
built), the engine class must match (a boot-built buildinfo is not
verified by a native build — fail-closed), the profile rotation is
shifted so the fresh builds run under a DIFFERENT slice of the
matrix than the original did, and the fresh artifact hashes must
equal the recorded ones. Exit 0 is the cross-distro answer: another
environment rebuilt your bytes.

--release (package mode only) is the shipping gate, hls-sbom's
contract carried over: a lockfile must exist (a release ships
pinned content), the audit's drift check runs first and a mismatch
refuses, an unauditable package refuses outright, and on success the
versioned buildinfo `hls-repro-<name>-<version>.buildinfo.json` is
written and ONE record (kind "repro" — the package hash, the C and
binary hashes, the buildinfo's own hash, the matrix size) is chained
into the same transparency ledger lock, publish and the SBOM append
to. Without --release the log is never touched.

Exit contract: 0 when the builds agree (or --verify reproduced); 1
on any mismatch, input drift, an unauditable package, a missing
lockfile under --release, an unwritable output location, or a
refused verify; 2 for usage errors. --json prints the report (schema
"hls-buildinfo/v1") instead of the human summary — the buildinfo IS
the report, and it is deterministic: two runs over an unchanged tree
with the same engine produce byte-identical buildinfo files, the
same content-addressing the SBOM documents already hold to.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TOOL_DIR)

# Load hls-audit by path (a hyphenated module name is not importable
# the normal way) — the SAME machine hls-sbom uses. The repro tool
# reuses its walkers so the three tools share ONE definition of "what
# the tree is"; an SBOM, an audit and a buildinfo can then never
# disagree about the inputs.
_AUDIT_PATH = os.path.join(TOOL_DIR, "hls-audit.py")
_spec = importlib.util.spec_from_file_location("hls_audit_mod", _AUDIT_PATH)
hls_audit = importlib.util.module_from_spec(_spec)
sys.modules["hls_audit_mod"] = hls_audit
_spec.loader.exec_module(hls_audit)

AuditError = hls_audit.AuditError

# The transparency log is hls-pkg's ledger; the --release record
# chains into the SAME chain (hpkg_parts is on sys.path once the
# hls-audit body has run).
from hpkg_log import transparency_log_append            # noqa: E402

TOOL = "hls-repro"
VERSION = "0.124.0-alpha"          # the toolchain release this run stamps
SCHEMA = "hls-buildinfo/v1"
C_NAME = "hl_repro.c"              # the CANONICAL intermediate name
BIN_NAME = "hl_repro"              # the canonical artifact name
BUILD_DIR = "build"
HLC_SRC = os.path.join(REPO_ROOT, "src", "hlc.hls")
NATIVE_HLC = os.path.join(REPO_ROOT, "bin", "hlc")
BOOT_PY = os.path.join(REPO_ROOT, "boot", "boot.py")
DEFAULT_CFLAGS = "-O2"
MAX_BUILDS = 5
SMOKE_TIMEOUT = 15

# Staging excludes: the package tree's own working-copy noise never
# enters a hermetic build root (the lockfile itself IS release
# content and stays).
_STAGE_SKIP = {".hls-pkg-deps", ".hls-pkg-build", ".hls-pkg-cache",
               "__pycache__"}
_STAGE_SKIP_SUFFIX = (".buildinfo.json",)


class ReproError(Exception):
    """A refused run (exit 1): drift, an unauditable package, a
    mismatch, a verify that did not reproduce. Usage errors exit 2
    before any of these can exist."""


# ---------------------------------------------------------------------------
# The distro matrix. Named archetypes; the deltas are the honest list
# of what a build daemon's environment varies. Root patterns are
# templates ({i} = the build index) so the buildinfo can record them
# without recording any machine's absolute paths.
# ---------------------------------------------------------------------------

PROFILES = (
    {"name": "baseline",
     "env": {},
     "umask": 0o022,
     "root": "b{i}/work"},
    {"name": "glibc-debian",
     "env": {"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8",
             "TZ": "Europe/Berlin", "PYTHONHASHSEED": "1"},
     "umask": 0o022,
     "root": "b{i}/debian-builder-with-a-deliberately-long-name"},
    {"name": "musl-alpine",
     "env": {"LANG": "C", "LC_ALL": "C", "TZ": "UTC",
             "PYTHONHASHSEED": "2"},
     "umask": 0o077,
     "root": "b{i}/a"},
    {"name": "bsd-sandbox",
     "env": {"LC_ALL": "C.UTF-8", "TZ": "Asia/Tokyo",
             "PYTHONHASHSEED": "3"},
     "umask": 0o002,
     "root": "b{i}/sandbox/deep/deeper/deepest"},
    {"name": "nix-long",
     "env": {"TZ": "America/New_York", "LC_ALL": "C",
             "PYTHONHASHSEED": "4"},
     "umask": 0o027,
     "root": "b{i}/" + "store" * 16},
)


def profile_for(i, offset=0):
    """The profile for build i. The rotation is deterministic; verify
    shifts the offset so a fresh run exercises a different slice of
    the matrix than the run that produced the buildinfo."""
    idx = (i + offset) % len(PROFILES)
    p = dict(PROFILES[idx])
    if i + offset >= len(PROFILES):
        # Cycling past the named set: the hash seed keeps moving so
        # the extra builds are still meaningfully different.
        p["env"] = dict(p["env"], PYTHONHASHSEED=str(100 + i + offset))
    return p


# ---------------------------------------------------------------------------
# Deterministic plumbing.
# ---------------------------------------------------------------------------

def _sha256_file(path):
    return hls_audit.sha256_file(path)


def _tree_digest(pairs):
    """One digest over the input set: every module's key and content
    hash, sorted. The repro twin of the SBOM's set digest — one
    flipped byte anywhere moves it, an unchanged tree never does."""
    h = hashlib.sha256()
    for key, sha in sorted(pairs):
        h.update(("%s|%s" % (key, sha)).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _epoch_for(tree_digest, explicit):
    """The epoch policy: explicit --epoch wins, then the environment
    (the reproducible-builds contract), then DERIVED from the tree
    digest — content, not clock, so a tree that has never heard of
    SOURCE_DATE_EPOCH still builds with a stable one."""
    if explicit is not None:
        return int(explicit), "--epoch"
    env_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if env_epoch is not None:
        try:
            return int(env_epoch), "SOURCE_DATE_EPOCH"
        except ValueError:
            raise ReproError("SOURCE_DATE_EPOCH is not an integer: %r"
                             % env_epoch)
    return int(tree_digest[:8], 16) % (2 ** 31), "derived"


def _key_to_path(key):
    """A module key (repo-relative inside the repo, absolute outside)
    back to a real filesystem path — the same round-trip hls-sbom
    draws."""
    if os.path.isabs(key):
        return os.path.realpath(key)
    return os.path.realpath(os.path.join(REPO_ROOT, key))


def _dump(doc):
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _cc_version(cc):
    """The first line of `cc --version` — recorded in the buildinfo
    so a buildinfo names the compiler that made it. A missing
    compiler is a refused run (nothing downstream can work)."""
    try:
        p = subprocess.run([cc, "--version"], capture_output=True,
                           text=True, timeout=30)
    except OSError as ex:
        raise ReproError("cannot run the C compiler %r: %s" % (cc, ex))
    if p.returncode != 0:
        raise ReproError("%r --version failed (exit %d)" % (cc, p.returncode))
    return (p.stdout or p.stderr).strip().splitlines()[0]


def _python_version():
    return "%d.%d.%d" % sys.version_info[:3]


def _engine_resolve(engine):
    """auto -> native when bin/hlc exists, boot otherwise. The
    RESOLVED class is what gets recorded and what verify pins."""
    if engine == "native":
        if not os.path.isfile(NATIVE_HLC):
            raise ReproError("--engine native: bin/hlc does not exist "
                             "(run make bootstrap)")
        return "native"
    if engine == "boot":
        return "boot"
    return "native" if os.path.isfile(NATIVE_HLC) else "boot"


def _hlc_fingerprint():
    return _sha256_file(HLC_SRC)


# ---------------------------------------------------------------------------
# The input tree — hls-audit's walkers, one definition of the tree.
# ---------------------------------------------------------------------------

def load_source_tree(entry_path):
    """The import tree of an entry file: the audit's own report, plus
    the (key, sha256) input set. Returns (report, pairs, layout) where
    layout names how the tree stages: 'repo' mirrors repo-relative
    keys, 'detached' mirrors paths relative to the entry's directory
    (every import boot's resolver accepts is confined there; the
    std./core. fallback modules are keyed repo-relative and stage at
    the root)."""
    entry_abs = os.path.realpath(os.path.abspath(entry_path))
    if not os.path.isfile(entry_abs):
        raise ReproError("no such file: %s" % entry_path)
    try:
        r = hls_audit.audit_source(entry_abs, None)
    except AuditError as ex:
        raise ReproError(str(ex))
    entry_dir = os.path.dirname(entry_abs)
    pairs, rel = [], {}
    for m in r["modules"]:
        key = m["key"]
        sha = _sha256_file(_key_to_path(key))
        pairs.append((key, sha))
        if os.path.isabs(key):
            rel[key] = os.path.relpath(_key_to_path(key), entry_dir)
        else:
            rel[key] = key
    for key, relpath in rel.items():
        if relpath.startswith(".."):
            raise ReproError("module %s stages outside the tree root"
                             % key)
    return {"report": r, "pairs": pairs, "rel": rel,
            "digest": _tree_digest(pairs),
            "entry_key": r["root"], "layout": "repo" if not os.path.isabs(
                r["root"]) else "detached"}


def load_package_tree(pkg_dir):
    """The manifest tree: the audit's package report (drift checked
    against the lockfile when one exists, every package audited
    fail-closed), the input set (the root's own files, every dep as
    the content unit the lockfile hashes), and the walk nodes for
    staging."""
    root_dir = os.path.realpath(os.path.abspath(pkg_dir or os.getcwd()))
    if not os.path.isdir(root_dir):
        raise ReproError("not a directory: %s" % pkg_dir)
    if not os.path.isfile(os.path.join(root_dir, "hls-pkg.toml")):
        raise ReproError("no hls-pkg.toml in %s "
                         "(pass --pkg DIR or run inside the package)"
                         % root_dir)
    try:
        r = hls_audit.audit_package(root_dir, None)
    except AuditError as ex:
        raise ReproError(str(ex))
    # The refusal order is the SBOM's documented order: drift first —
    # a tree the lock no longer names is refused before anything
    # else is classified; then unauditable, fail-closed.
    if r["drift"]:
        raise ReproError(
            "lockfile drift: %s — re-lock before repro"
            % "; ".join("%s: %s" % (d["package"], d["why"])
                        for d in r["drift"]))
    if r["unauditable"]:
        raise ReproError(
            "unauditable package(s): %s — no reproducible-build claim "
            "stamps a package nobody could check"
            % ", ".join(x["package"] for x in r["unauditable"]))
    try:
        root_node = hls_audit._walk_packages(root_dir, REPO_ROOT, [], {})
    except AuditError as ex:
        raise ReproError(str(ex))
    nodes = hls_audit._all_nodes(root_node)
    seen, dep_units = set(), []
    for n in nodes:
        if n is root_node or n.dir in seen:
            continue
        seen.add(n.dir)
        dep_units.append({"name": n.name,
                          "path": n.content_path,
                          "sha256": hls_audit._sha256_tree(n.content_path)})
    pairs = [("dep:" + d["name"], d["sha256"]) for d in dep_units]
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _STAGE_SKIP)
        for fn in sorted(filenames):
            if fn.endswith(_STAGE_SKIP_SUFFIX):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root_dir)
            pairs.append(("app/" + rel.replace(os.sep, "/"),
                          _sha256_file(full)))
    digest = _tree_digest(pairs)
    entry = str((root_node.manifest.get("package") or {}).get("entry")
                or "main.hls")
    return {"report": r, "pairs": pairs, "dep_units": dep_units,
            "root_dir": root_dir, "digest": digest,
            "name": r["root"]["name"], "version": r["root"]["version"],
            "entry": entry}


def source_digest(tree):
    return _tree_digest(tree["pairs"])


# ---------------------------------------------------------------------------
# Staging — the hermetic copy each build root receives.
# ---------------------------------------------------------------------------

def _copy_tree_contents(src, dst):
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in _STAGE_SKIP]
        for fn in filenames:
            if fn.endswith(_STAGE_SKIP_SUFFIX):
                continue
            s = os.path.join(dirpath, fn)
            d = os.path.join(dst, os.path.relpath(s, src))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copyfile(s, d)


def stage_source(root, tree):
    """Copy exactly the audit's module set into the build root, at the
    layout the import resolver will re-derive. Nothing else crosses —
    the build sees the tree the buildinfo lists, not the working
    copy."""
    for key, relpath in tree["rel"].items():
        dst = os.path.join(root, relpath)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(_key_to_path(key), dst)
    return tree["rel"][tree["entry_key"]]


def stage_package(root, tree):
    """Copy the package directory into <root>/app and lay the dep
    units out exactly the way `hls-pkg build` does — a flat
    .hls-pkg-deps/ with one name per top-level dep (a directory for a
    multi-file package, <name>.hls for the legacy single file) — so
    the compiler's HLS_PKG_DEPS fallback resolves the same graph the
    lockfile pins."""
    app = os.path.join(root, "app")
    os.makedirs(app)
    _copy_tree_contents(tree["root_dir"], app)
    deps = os.path.join(app, ".hls-pkg-deps")
    os.makedirs(deps)
    for d in tree["dep_units"]:
        dst = os.path.join(deps, d["name"])
        if os.path.isdir(d["path"]):
            _copy_tree_contents(d["path"], dst)
        else:
            shutil.copyfile(d["path"], dst + ".hls")
    return os.path.join("app", tree["entry"])


# ---------------------------------------------------------------------------
# The build.
# ---------------------------------------------------------------------------

def _base_env(epoch):
    """The MINIMAL environment every build starts from — a build that
    only sees PATH and the recipe cannot inherit your shell's mood."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SOURCE_DATE_EPOCH": str(epoch)}


def _run(cmd, cwd, env, umask, what):
    """One subprocess of the recipe. The umask is part of the recipe
    (a distro's build daemon sets its own); stdin is closed; output
    is captured for the report, never inherited."""
    p = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       umask=umask, text=True, timeout=600)
    if p.returncode != 0:
        raise ReproError("%s failed (exit %d)\n%s"
                         % (what, p.returncode,
                            (p.stderr or p.stdout).strip()[-800:]))
    return p


def build_one(i, scratch, tree, mode, epoch, engine, cc, cflags,
              offset=0, smoke_on=True):
    """Build index i: one profile, one isolated root, one staged copy,
    one recipe. Returns the build's record — hashes and smoke only,
    no machine paths."""
    prof = profile_for(i, offset)
    root = os.path.join(scratch, prof["root"].format(i=i))
    os.makedirs(os.path.join(root, BUILD_DIR), exist_ok=True)
    if mode == "source":
        staged_entry = stage_source(root, tree)
    else:
        staged_entry = stage_package(root, tree)

    env = dict(_base_env(epoch))
    env.update(prof["env"])
    umask = prof["umask"]

    out_c = os.path.join(BUILD_DIR, C_NAME)
    if engine == "native":
        cmd = [NATIVE_HLC, staged_entry, out_c]
    else:
        cmd = [sys.executable, BOOT_PY, HLC_SRC, staged_entry, out_c]
    if mode == "package":
        env["HLS_PKG_DEPS"] = os.path.join(root, "app", ".hls-pkg-deps")
    _run(cmd, root, env, umask, "the %s compile (%s engine)"
         % (mode, engine))
    c_path = os.path.join(root, out_c)
    c_sha = _sha256_file(c_path)

    cc_argv = [cc] + cflags.split() + ["-o", os.path.join(BUILD_DIR,
                                                         BIN_NAME),
                                       out_c, "-lm", "-pthread"]
    _run(cc_argv, root, env, umask, "the C compile (%s)" % cc)
    bin_path = os.path.join(root, BUILD_DIR, BIN_NAME)
    bin_sha = _sha256_file(bin_path)

    smoke = _smoke(bin_path, root, env, umask) if smoke_on else {
        "status": "skipped", "why": "--no-smoke"}
    return {"index": i, "profile": prof["name"],
            "root_pattern": prof["root"],
            "umask": format(umask, "03o"),
            "env": prof["env"],
            "c_sha256": c_sha, "binary_sha256": bin_sha,
            "binary_size": os.path.getsize(bin_path),
            "smoke": smoke}


def _smoke(bin_path, root, env, umask):
    """Run the artifact once under its own profile's env. The verdict
    compares builds against builds, never against an expectation —
    a program that legitimately fails without arguments still
    reproduces (the same failure everywhere)."""
    try:
        p = subprocess.run(["./" + os.path.join(BUILD_DIR, BIN_NAME)],
                           cwd=root, env=env, umask=umask,
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=SMOKE_TIMEOUT)
        return {"status": "ran", "exit": p.returncode,
                "stdout_sha256": hashlib.sha256(
                    p.stdout).hexdigest(),
                "stdout_size": len(p.stdout)}
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}
    except OSError as ex:
        return {"status": "skipped", "why": str(ex)}


# ---------------------------------------------------------------------------
# The diagnosis — WHY did the bytes differ.
# ---------------------------------------------------------------------------

_TIMESTAMP_MACROS = (b"__DATE__", b"__TIME__", b"__TIMESTAMP__")


def _strings_around(data, offset, span=64):
    lo = max(0, offset - span)
    hi = min(len(data), offset + span)
    chunk = data[lo:hi]
    out, cur = [], []
    for b in chunk:
        if 32 <= b < 127:
            cur.append(chr(b))
        else:
            if len(cur) >= 4:
                out.append("".join(cur))
            cur = []
    if len(cur) >= 4:
        out.append("".join(cur))
    return out


def diagnose(builds, c_paths, bin_paths, roots):
    """Classify a mismatch. Leak classes, in the order they bite:
    the build-root path embedded in an artifact, the intermediate C's
    name reaching the symbol table (the STT_FILE lesson), timestamp
    macros in the generated C, and the residual class — reported with
    the printable strings around the first differing byte as
    evidence. `builds` carries the per-build hashes; the path lists
    point at the artifacts behind them. Returns the findings."""
    findings = []
    names = [os.path.basename(r.rstrip("/")) for r in roots]
    if len({b["c_sha256"] for b in builds}) > 1:
        for c_path in c_paths:
            try:
                with open(c_path, "rb") as f:
                    c_text = f.read()
            except OSError:
                continue
            hits = [m.decode() for m in _TIMESTAMP_MACROS if m in c_text]
            if hits:
                findings.append(
                    "timestamp macro in the generated C: %s (%s) — "
                    "the C stage cannot be deterministic while it "
                    "reads the wall clock" % (c_path, ", ".join(hits)))
            for nm in names:
                if nm.encode() in c_text:
                    findings.append(
                        "the build-root name %r appears in the "
                        "generated C — path leakage at the compiler "
                        "stage" % nm)
    if len({b["binary_sha256"] for b in builds}) > 1:
        blobs = []
        for p in bin_paths:
            try:
                with open(p, "rb") as f:
                    blobs.append(f.read())
            except OSError:
                blobs.append(b"")
        if blobs and all(blobs):
            diffs = sum(1 for k in range(min(len(b) for b in blobs))
                        if len({b[k] for b in blobs}) > 1)
            offset = next(k for k in range(min(len(b) for b in blobs))
                          if len({b[k] for b in blobs}) > 1)
            near = sorted({s for b in blobs
                           for s in _strings_around(b, offset)})
            findings.append(
                "the binaries differ in %d byte(s); first at offset %d "
                "— printable strings around it: %s"
                % (diffs, offset, ", ".join(near[:6]) or "(none)"))
            if len({len(b) for b in blobs}) == 1 and diffs == 1:
                findings.append(
                    "a single differing byte in an otherwise identical "
                    "binary is the symbol-table signature of a leaked "
                    "intermediate FILE NAME (STT_FILE) — the recipe "
                    "names the C %s canonically; a non-canonical name "
                    "reached this build's compiler" % C_NAME)
            for nm in names:
                if any(nm.encode() in b for b in blobs):
                    findings.append(
                        "the build-root name %r appears inside a "
                        "binary — path leakage at the assembler or "
                        "linker stage" % nm)
    if not findings:
        findings.append("no known leak class matched — compare the "
                        "artifact hashes and the toolchain versions "
                        "recorded in the buildinfo")
    return findings


# ---------------------------------------------------------------------------
# The orchestration — N builds, one verdict, one buildinfo.
# ---------------------------------------------------------------------------

def run_builds(tree, mode, opts, offset=0):
    """The pipeline: epoch -> N isolated builds -> verdict -> (on
    agreement) the buildinfo dict. `offset` shifts the profile
    rotation (verify uses a shifted slice)."""
    engine = _engine_resolve(opts.engine)
    digest = tree["digest"] if "digest" in tree else source_digest(tree)
    epoch, epoch_src = _epoch_for(digest, opts.epoch)
    cc_ver = _cc_version(opts.cc)
    cflags = opts.cflags.strip() or DEFAULT_CFLAGS

    builds, roots, c_paths, bin_paths = [], [], [], []
    smoke_on = not getattr(opts, "no_smoke", False)
    for i in range(opts.builds):
        rec = build_one(i, opts.scratch, tree, mode, epoch, engine,
                        opts.cc, cflags, offset, smoke_on)
        builds.append(rec)
        roots.append(os.path.join(opts.scratch,
                                  rec["root_pattern"].format(i=i)))
        c_paths.append(os.path.join(roots[-1], BUILD_DIR, C_NAME))
        bin_paths.append(os.path.join(roots[-1], BUILD_DIR, BIN_NAME))

    c_ok = len({b["c_sha256"] for b in builds}) == 1
    bin_ok = len({b["binary_sha256"] for b in builds}) == 1
    smokes = [b["smoke"] for b in builds]
    if all(s["status"] == "ran" for s in smokes):
        smoke = {"status": "consistent",
                 "exit": smokes[0]["exit"],
                 "stdout_sha256": smokes[0]["stdout_sha256"]}
        if len({s["stdout_sha256"] for s in smokes}) > 1 or \
                len({s["exit"] for s in smokes}) > 1:
            smoke = {"status": "divergent"}
    elif all(s["status"] == "timeout" for s in smokes):
        smoke = {"status": "skipped", "why": "every run timed out"}
    else:
        smoke = {"status": "skipped",
                 "why": "not every build produced a runnable artifact"}

    if not (c_ok and bin_ok):
        findings = diagnose(builds, c_paths, bin_paths, roots)
        raise Mismatch(builds, findings,
                       "the artifacts are NOT byte-identical across "
                       "%d builds" % len(builds))

    inputs = [{"key": k, "sha256": s} for k, s in sorted(tree["pairs"])]
    info = {
        "schema": SCHEMA,
        "tool": TOOL,
        "tool_version": VERSION,
        "mode": mode,
        "engine": engine,
        "epoch": {"value": epoch, "source": epoch_src},
        "tree": {
            "digest": digest,
            "inputs": inputs,
        },
        "toolchain": {
            "hlc_fingerprint_sha256": _hlc_fingerprint(),
            "cc": cc_ver,
            "cc_command": opts.cc,
            "python": _python_version(),
        },
        "recipe": {
            "c_name": BUILD_DIR + "/" + C_NAME,
            "binary_name": BUILD_DIR + "/" + BIN_NAME,
            "cflags": cflags,
            "link_libs": ["-lm", "-pthread"],
            "hermetic_staging": True,
        },
        "matrix": {
            "builds": len(builds),
            "profiles": [{"name": b["profile"],
                          "root_pattern": b["root_pattern"],
                          "umask": b["umask"],
                          "env": b["env"]} for b in builds],
        },
        "smoke": smoke,
        "builds": [{"index": b["index"], "profile": b["profile"],
                    "c_sha256": b["c_sha256"],
                    "binary_sha256": b["binary_sha256"],
                    "binary_size": b["binary_size"],
                    "smoke": b["smoke"]} for b in builds],
        "outputs": {"c_sha256": builds[0]["c_sha256"],
                    "binary_sha256": builds[0]["binary_sha256"],
                    "binary_size": builds[0]["binary_size"]},
    }
    if mode == "source":
        info["tree"]["layout"] = tree["layout"]
        info["tree"]["entry"] = (
            tree["entry_key"] if tree["layout"] == "repo"
            else os.path.basename(tree["entry_key"]))
    else:
        info["tree"]["name"] = tree["name"]
        info["tree"]["version"] = tree["version"]
        info["tree"]["entry"] = tree["entry"]
    return info


class Mismatch(ReproError):
    """The builds disagreed. Carries the per-build records and the
    diagnoser's findings so the caller can render both."""

    def __init__(self, builds, findings, message):
        super().__init__(message)
        self.builds = builds
        self.findings = findings


# ---------------------------------------------------------------------------
# verify — the cross-environment answer.
# ---------------------------------------------------------------------------

def verify_buildinfo(path, opts):
    """Re-run the recipe on the CURRENT tree and compare with the
    buildinfo. Order is the contract: shape first, the TREE second
    (a tree that is not what the buildinfo describes refuses before
    anything is built), the ENGINE third (fail-closed on a class
    mismatch), then the fresh builds under a shifted profile slice."""
    try:
        with open(path, "r") as f:
            bi = json.load(f)
    except (OSError, ValueError) as ex:
        raise ReproError("cannot read the buildinfo %s: %s" % (path, ex))
    if not isinstance(bi, dict) or bi.get("schema") != SCHEMA:
        raise ReproError("%s is not a %s buildinfo" % (path, SCHEMA))
    if bi.get("tool") != TOOL:
        raise ReproError("%s was produced by %r, not %s"
                         % (path, bi.get("tool"), TOOL))
    mode = bi.get("mode")
    if mode not in ("source", "package"):
        raise ReproError("buildinfo names an unknown mode: %r" % mode)
    tree_sec = bi.get("tree") or {}
    if mode == "source":
        entry_rel = str(tree_sec.get("entry") or "")
        if not entry_rel:
            raise ReproError("the buildinfo names no entry")
        if tree_sec.get("layout") == "detached":
            if not opts.in_dir:
                raise ReproError(
                    "this buildinfo describes a detached tree — pass "
                    "--in DIR (the directory holding %s)" % entry_rel)
            entry_path = os.path.join(opts.in_dir, entry_rel)
        else:
            entry_path = os.path.join(REPO_ROOT, entry_rel)
        if not os.path.isfile(entry_path):
            raise ReproError("no such file: %s" % entry_path)
        tree = load_source_tree(entry_path)
    else:
        pkg_root = opts.in_dir or os.getcwd()
        if not os.path.isfile(os.path.join(pkg_root, "hls-pkg.toml")):
            raise ReproError(
                "no hls-pkg.toml in %s — pass --in DIR" % pkg_root)
        tree = load_package_tree(pkg_root)

    if tree["digest"] != tree_sec.get("digest"):
        raise ReproError(
            "the tree is not what the buildinfo describes: digest %s "
            "but the buildinfo records %s — input drift"
            % (tree["digest"][:12], str(tree_sec.get("digest"))[:12]))

    engine = bi.get("engine")
    if engine not in ("boot", "native"):
        raise ReproError("buildinfo names an unknown engine: %r" % engine)
    if engine != _engine_resolve(opts.engine):
        raise ReproError(
            "engine mismatch: the buildinfo was built with %r and this "
            "run would use %r — a buildinfo is verified in the engine "
            "class it names" % (engine, _engine_resolve(opts.engine)))

    n = bi.get("matrix", {}).get("builds")
    if not isinstance(n, int) or n < 2 or n > MAX_BUILDS:
        raise ReproError("buildinfo names an unusable build count: %r" % n)
    opts.builds = n
    opts.cflags = (bi.get("recipe") or {}).get("cflags") or DEFAULT_CFLAGS
    epoch = (bi.get("epoch") or {}).get("value")
    if not isinstance(epoch, int):
        raise ReproError("buildinfo names no usable epoch")
    opts.epoch = epoch
    # A shifted rotation: the fresh builds exercise a DIFFERENT slice
    # of the matrix than the original run did — that is the whole
    # point of verifying.
    fresh = run_builds(tree, mode, opts, offset=1)
    fresh["verified"] = True
    fresh["verified_against_sha256"] = _sha256_file(path)
    out_want = (bi.get("outputs") or {})
    if fresh["outputs"].get("c_sha256") != out_want.get("c_sha256"):
        raise VerifyFail(fresh,
                         "the C stage did not reproduce: fresh %s, "
                         "buildinfo %s" % (fresh["outputs"]["c_sha256"][:12],
                                           str(out_want.get("c_sha256"))[:12]))
    if fresh["outputs"].get("binary_sha256") != out_want.get("binary_sha256"):
        raise VerifyFail(fresh,
                         "the binary did not reproduce: fresh %s, "
                         "buildinfo %s"
                         % (fresh["outputs"]["binary_sha256"][:12],
                            str(out_want.get("binary_sha256"))[:12]))
    old_smoke = (bi.get("smoke") or {}).get("status")
    new_smoke = fresh["smoke"]
    fresh["smoke_agreement"] = (
        old_smoke == "consistent" and new_smoke.get("status") == "consistent"
        and (bi["smoke"].get("stdout_sha256")
             == new_smoke.get("stdout_sha256")))
    return fresh, bi


class VerifyFail(ReproError):
    """The fresh build disagreed with the buildinfo. Carries the fresh
    buildinfo for the report."""

    def __init__(self, fresh, message):
        super().__init__(message)
        self.fresh = fresh


# ---------------------------------------------------------------------------
# main — modes, files, exits.
# ---------------------------------------------------------------------------

def _default_buildinfo_path(mode, tree, opts):
    name = "hls-repro.buildinfo.json"
    if getattr(opts, "release", False):
        name = "hls-repro-%s-%s.buildinfo.json" % (tree["name"],
                                                   tree["version"])
    base = opts.out
    if base is None:
        if mode == "package":
            base = tree["root_dir"]
        else:
            base = os.path.dirname(_key_to_path(tree["entry_key"]))
    return os.path.join(base, name)


def _write_buildinfo(path, info):
    """Write the buildinfo. The output directory is created when
    missing; a directory that cannot be created or written is a
    refused run, not a half-written claim."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(_dump(info))
    except OSError as ex:
        raise ReproError("cannot write the buildinfo %s: %s"
                         % (path, ex))
    return path


def _render_summary(info):
    print("reproducible: %d builds, %d profiles, engine %s"
          % (info["matrix"]["builds"], len(info["matrix"]["profiles"]),
             info["engine"]))
    print("  tree      = %s (%d inputs)"
          % (info["tree"]["digest"][:16], len(info["tree"]["inputs"])))
    print("  epoch     = %d (%s)" % (info["epoch"]["value"],
                                    info["epoch"]["source"]))
    print("  c         = %s" % info["outputs"]["c_sha256"])
    print("  binary    = %s (%d bytes)"
          % (info["outputs"]["binary_sha256"],
             info["outputs"]["binary_size"]))
    smoke = info["smoke"]
    if smoke["status"] == "consistent":
        print("  smoke     = consistent (exit %d, stdout %s)"
              % (smoke["exit"], smoke["stdout_sha256"][:16]))
    else:
        print("  smoke     = %s" % smoke["status"])
    print("  profiles  = %s"
          % ", ".join(p["name"] for p in info["matrix"]["profiles"]))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hls-repro",
        description="reproducible-build verification across distro "
                    "profiles (Stage 105)")
    ap.add_argument("entry", nargs="?", help="the entry .hls file")
    ap.add_argument("--pkg", nargs="?", const=".", default=None,
                    help="package mode (optional DIR, default cwd)")
    ap.add_argument("--verify", metavar="FILE",
                    help="verify a buildinfo against the current tree")
    ap.add_argument("--in", dest="in_dir", default=None,
                    help="the tree root for --verify (detached trees)")
    ap.add_argument("--builds", type=int, default=2,
                    help="builds across the profile matrix (2..%d, "
                         "default 2)" % MAX_BUILDS)
    ap.add_argument("--engine", choices=("auto", "boot", "native"),
                    default="auto")
    ap.add_argument("--cc", default=os.environ.get("CC", "cc"))
    ap.add_argument("--cflags", default=DEFAULT_CFLAGS)
    ap.add_argument("--epoch", type=int, default=None)
    ap.add_argument("--out", default=None,
                    help="directory for the buildinfo file")
    ap.add_argument("--scratch", default=None,
                    help="parent directory for the build roots")
    ap.add_argument("--keep-scratch", action="store_true")
    ap.add_argument("--no-smoke", action="store_true",
                    help="skip the runtime smoke of the artifacts")
    ap.add_argument("--release", action="store_true",
                    help="package mode: the shipping gate (lockfile "
                         "required, buildinfo + transparency record)")
    ap.add_argument("--json", action="store_true",
                    help="print the report instead of the summary")
    opts = ap.parse_args(argv)

    if opts.entry is None and opts.pkg is None and not opts.verify:
        sys.stderr.write(
            "error: choose exactly one mode: an entry file, --pkg, or "
            "--verify FILE\n")
        return 2
    if sum(1 for x in (opts.entry is not None, opts.pkg is not None,
                       bool(opts.verify)) if x) > 1:
        sys.stderr.write(
            "error: choose exactly one mode: an entry file, --pkg, or "
            "--verify FILE\n")
        return 2
    if opts.verify and (opts.release or opts.epoch is not None
                        or opts.out or opts.cflags != DEFAULT_CFLAGS):
        sys.stderr.write(
            "error: --verify reproduces the recorded recipe — --release, "
            "--epoch, --out and --cflags do not apply\n")
        return 2
    if opts.release and opts.pkg is None:
        sys.stderr.write("error: --release is a package-mode gate\n")
        return 2
    if opts.builds < 2 or opts.builds > MAX_BUILDS:
        sys.stderr.write("error: --builds must be between 2 and %d\n"
                         % MAX_BUILDS)
        return 2

    opts.scratch = opts.scratch or tempfile.mkdtemp(prefix="hls-repro-")
    keep_evidence = False
    try:
        if opts.verify:
            fresh, old = verify_buildinfo(opts.verify, opts)
            if opts.json:
                print(_dump(fresh))
            else:
                print("verified: %d fresh builds under a shifted profile "
                      "slice reproduce the buildinfo's bytes"
                      % fresh["matrix"]["builds"])
                print("  tree      = %s" % fresh["tree"]["digest"][:16])
                print("  c         = %s" % fresh["outputs"]["c_sha256"])
                print("  binary    = %s" % fresh["outputs"]["binary_sha256"])
                if fresh["smoke_agreement"]:
                    print("  smoke     = consistent in both runs")
                elif old.get("smoke", {}).get("status") != "consistent" \
                        or fresh["smoke"].get("status") != "consistent":
                    print("  smoke     = skipped (either run could not "
                          "smoke consistently)")
                else:
                    print("  smoke     = DIVERGED between the runs")
            return 0

        if opts.pkg is not None:
            tree = load_package_tree(opts.pkg)
            if opts.release and tree["report"]["lockfile"] != "present":
                raise ReproError(
                    "no lockfile next to the manifest — a release ships "
                    "pinned content: run hls-pkg lock first")
            mode = "package"
        else:
            tree = load_source_tree(opts.entry)
            mode = "source"

        info = run_builds(tree, mode, opts, offset=0)
        if opts.release:
            bi_path = _default_buildinfo_path(mode, tree, opts)
            _write_buildinfo(bi_path, info)
            rec = transparency_log_append({
                "kind": "repro",
                "name": tree["name"],
                "version": tree["version"],
                "tree_sha256": tree["digest"],
                "c_sha256": info["outputs"]["c_sha256"],
                "binary_sha256": info["outputs"]["binary_sha256"],
                "buildinfo_sha256": _sha256_file(bi_path),
                "builds": info["matrix"]["builds"],
                "tool_version": VERSION,
            })
            print("wrote %s" % bi_path)
            print("chained into the transparency log (seq %s)"
                  % rec.get("seq"))
            _render_summary(info)
        elif opts.json:
            print(_dump(info))
        else:
            bi_path = _default_buildinfo_path(mode, tree, opts)
            _write_buildinfo(bi_path, info)
            print("wrote %s" % bi_path)
            _render_summary(info)
        return 0
    except Mismatch as ex:
        keep_evidence = True
        sys.stderr.write("NOT reproducible: %s\n" % ex)
        for b in ex.builds:
            sys.stderr.write("  build %d (%s): c=%s bin=%s\n"
                             % (b["index"], b["profile"],
                                b["c_sha256"][:12], b["binary_sha256"][:12]))
        for f in ex.findings:
            sys.stderr.write("  finding: %s\n" % f)
        sys.stderr.write("the build roots are kept for evidence: %s\n"
                         % opts.scratch)
        return 1
    except VerifyFail as ex:
        sys.stderr.write("verify failed: %s\n" % ex)
        if getattr(ex, "fresh", None):
            sys.stderr.write("  fresh c     = %s\n"
                             % ex.fresh["outputs"]["c_sha256"])
            sys.stderr.write("  fresh bin   = %s\n"
                             % ex.fresh["outputs"]["binary_sha256"])
        return 1
    except ReproError as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 1
    finally:
        # The scratch is evidence on a mismatch (kept) and disposable
        # on success — unless the caller asked to keep it.
        if not opts.keep_scratch and not keep_evidence:
            shutil.rmtree(opts.scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
