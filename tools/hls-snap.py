#!/usr/bin/env python3
"""hls-snap — the Stage 142 pure-bootstrap kit: the committed C snapshot.

Usage:
  python3 tools/hls-snap.py status  [--root PATH] [--json]
  python3 tools/hls-snap.py verify  [--root PATH] [--deep] [--json]
  python3 tools/hls-snap.py regen   [--root PATH] [--seed] [--cc CC]
  python3 tools/hls-snap.py prove   [--root PATH] [--cc CC]
                                       [--python PYTHON]

Roadmap Stage 142 is one phrase: "Pure-HLS bootstrap (remove `boot/`
Python seed)". Until now the self-hosting cycle had a Python-shaped
ignition key: every fresh checkout, every CI runner, every release
build first paid the interpreter — `boot/boot.py` walking the HLS
compiler source statement by statement (~80 s) — before gcc ever saw
a byte. The seed was honest, but it made the one language whose
compiler is written in itself depend on a different language to turn
it on, and it put Python on the build path of every artifact this
tree ships. This stage moves the ignition: the repository now
carries `snapshot/hlc.c` — the C that the Halis compiler emits for
its own source, ~37k lines, committed like any other file — and the
default build compiles THAT:

    cc -O2 -o bin/hlc snapshot/hlc.c -lm -pthread
    bin/hlc src/hlc.hls bin/hlc_regen.c
    cmp snapshot/hlc.c bin/hlc_regen.c        # the fixed-point law

The chain needs gcc and nothing else. `boot/` stays in the tree —
the interpreter still drives the differential suites and remains the
oracle that re-derives the snapshot when the language itself grows
new syntax — but it is OFF the build path: `make bootstrap` no
longer contains the string "python" in any command it runs.

  1. the FIXED-POINT law — the snapshot is derived, never written:
     the compiler built FROM snapshot/hlc.c must re-emit
     snapshot/hlc.c byte-for-byte when it compiles src/hlc.hls.
     `make bootstrap` proves this on every build. A hand edit to
     the C breaks the law loudly (the edited bytes will not
     re-derive themselves), and so does a compiler-source change
     that was not followed by a regen — the failure names both
     possibilities because the law cannot tell them apart, the
     manifest can (see 3);

  2. the SEED law — `boot/` is demoted from ignition to oracle: the
     interpreter must derive the SAME bytes the snapshot carries
     (`make snapshot-prove`), so the committed C is provably the
     child of the HLS sources through TWO independent lineages —
     the seed reading the sources, and the compiled snapshot
     reading them. A snapshot neither lineage can reproduce is a
     snapshot nobody wrote;

  3. the MANIFEST law — snapshot/MANIFEST.json records what the
     snapshot was cut from: the exact import closure of
     src/hlc.hls (every file, content-addressed), the closure
     digest, the snapshot's own sha256, the release it belongs to,
     the number of fixed-point rounds the regen took. No wall-
     clock, no paths outside the tree, no machine specifics — two
     regens of one state write byte-identical manifests. The
     manifest is the fast path that NAMES the drift (which file
     moved, REMOVED / ADDED / CHANGED) without compiling anything,
     and the document must agree with itself: its closure digest
     is the digest of the closure it carries;

  4. the REGEN law — the only legal mutation of the snapshot is
     `make snapshot-regen`, which iterates to the fixed point (a
     codegen change re-derives the compiler, which re-emits its
     own source, until the bytes stabilise — normally 1 round,
     refused after 8) and rewrites the snapshot AND the manifest
     TOGETHER. A regen on a tree that already matches its
     snapshot is refused: a no-op regen is noise, and noise is
     how real regens stop being read. A regen that would need the
     seed (new syntax the current compiler cannot parse) is named,
     not guessed at — rerun with --seed. The native path refuses
     to run when the snapshot is missing (it is the seed it
     builds from);

  5. the AGREEMENT law — prove never runs from a drifted tree: a
     proof that the seed matches a snapshot the manifest no
     longer describes is not a proof, it is a coincidence.

Exit codes, the house contract: 0 the law held; 1 a named refusal
or detected drift (fail-closed); 2 usage error. Every refusal
names the rule that fired, not just the fact.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SNAPSHOT_DIR = "snapshot"
SNAPSHOT_C = "snapshot/hlc.c"
MANIFEST_PATH = "snapshot/MANIFEST.json"
ENTRY = "src/hlc.hls"
CC_DEFAULT = "gcc"
CFLAGS = ["-O2"]
LINK_FLAGS = ["-lm", "-pthread"]
MAX_ROUNDS = 8

LAW_TEXT = (
    "snapshot/hlc.c == the C that a Halis compiler built from "
    "snapshot/hlc.c emits for src/hlc.hls, byte for byte — proved "
    "at every make bootstrap"
)
PROVENANCE_TEXT = (
    "cut from the src/hlc.hls import closure by hlc itself "
    "(make snapshot-regen); the Python seed derives the same bytes "
    "(make snapshot-prove)"
)

IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"')

# Classification verdicts (closed vocabulary).
FRESH = "FRESH"
STALE = "STALE-SOURCES"
TAMPERED = "TAMPERED"
NO_SNAPSHOT = "NO-SNAPSHOT"
NO_MANIFEST = "NO-MANIFEST"


def _refuse(rule, detail):
    print("snapshot: REFUSED (%s) — %s" % (rule, detail))
    sys.exit(1)


def _usage_error(detail):
    print("hls-snap: usage error — %s" % detail)
    sys.exit(2)


def _tail(stderr_text, limit=20):
    lines = (stderr_text or "").strip().splitlines()
    return "\n".join(lines[-limit:])


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def canonical_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_digest(obj):
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


# ---------------------------------------------------------------------------
# The import closure of the entry file.
# ---------------------------------------------------------------------------

def _resolve_import(import_path, importing_file, root):
    """Resolve one import the way boot/boot.py does, or return None."""
    for prefix, family in (("std.", "std"), ("core.", "core")):
        if import_path.startswith(prefix):
            rest = import_path[len(prefix):].replace(".", "/")
            candidate = os.path.join(root, family, rest + ".hls")
            if os.path.isfile(candidate):
                return candidate
            return None
    candidate = os.path.join(os.path.dirname(importing_file), import_path)
    if os.path.isfile(candidate):
        return candidate
    return None


def closure_map(root):
    """Walk the transitive import closure of src/hlc.hls.

    Returns {relative-path: sha256} over every file the compiler
    reads when it compiles itself — the exact inputs the snapshot
    is a function of. Sorted for determinism.
    """
    entry = os.path.join(root, ENTRY)
    if not os.path.isfile(entry):
        _refuse("the entry law",
                "src/hlc.hls is missing under %s — nothing to close over" % root)
    seen = {}
    queue = [entry]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        if not os.path.isfile(current):
            _refuse("the closure law",
                    "imported file does not exist: %s" % os.path.relpath(current, root))
        seen[current] = sha256_file(current)
        with open(current, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = IMPORT_RE.match(line)
                if not m:
                    continue
                resolved = _resolve_import(m.group(1), current, root)
                if resolved is None:
                    _refuse("the closure law",
                            "unresolvable import \"%s\" in %s"
                            % (m.group(1), os.path.relpath(current, root)))
                if resolved not in seen:
                    queue.append(resolved)
    return {os.path.relpath(p, root).replace(os.sep, "/"): d
            for p, d in seen.items()}


def live_closure(root):
    return dict(sorted(closure_map(root).items()))


# ---------------------------------------------------------------------------
# The manifest.
# ---------------------------------------------------------------------------

def read_manifest(root):
    path = os.path.join(root, MANIFEST_PATH)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError) as exc:
        _refuse("the manifest law",
                "snapshot/MANIFEST.json does not parse: %s" % exc)


def write_manifest(root, manifest):
    path = os.path.join(root, MANIFEST_PATH)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(manifest, sort_keys=True, indent=2))
        fh.write("\n")
    os.replace(tmp, path)


def build_manifest(root, snapshot_path, rounds):
    live = live_closure(root)
    return {
        "closure": live,
        "closure_digest": canonical_digest(live),
        "hlc_c_bytes": os.path.getsize(snapshot_path),
        "hlc_c_lines": _count_lines(snapshot_path),
        "hlc_c_sha256": sha256_file(snapshot_path),
        "law": LAW_TEXT,
        "provenance": PROVENANCE_TEXT,
        "rounds": rounds,
        "version": changelog_version(root),
    }


def _count_lines(path):
    n = 0
    with open(path, "rb") as fh:
        for _ in fh:
            n += 1
    return n


def changelog_version(root):
    path = os.path.join(root, "CHANGELOG.md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^## \[(v[0-9][^\]\s]*)\]", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    _refuse("the version law",
            "CHANGELOG.md carries no '## [v...]' release heading — the "
            "manifest records the release the snapshot belongs to")


# ---------------------------------------------------------------------------
# Classification.
# ---------------------------------------------------------------------------

def classify(root):
    """(state, drift) — drift names REMOVED / ADDED / CHANGED files."""
    snap_path = os.path.join(root, SNAPSHOT_C)
    if not os.path.isfile(snap_path):
        return NO_SNAPSHOT, {}
    manifest = read_manifest(root)
    if manifest is None:
        return NO_MANIFEST, {}
    drift = {}
    live = live_closure(root)
    frozen = manifest.get("closure", {})
    removed = sorted(set(frozen) - set(live))
    added = sorted(set(live) - set(frozen))
    changed = sorted(p for p in set(live) & set(frozen)
                     if live[p] != frozen[p])
    if removed:
        drift["REMOVED"] = removed
    if added:
        drift["ADDED"] = added
    if changed:
        drift["CHANGED"] = changed
    if drift:
        return STALE, drift
    if sha256_file(snap_path) != manifest.get("hlc_c_sha256"):
        return TAMPERED, {}
    if canonical_digest(frozen) != manifest.get("closure_digest"):
        return TAMPERED, {}
    return FRESH, {}


# ---------------------------------------------------------------------------
# Compilers: the snapshot C, the seed interpreter.
# ---------------------------------------------------------------------------

def cc_compile(root, cc, c_path, out_bin):
    cmd = [cc] + CFLAGS + ["-o", out_bin, c_path] + LINK_FLAGS
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        _refuse("the toolchain law",
                "%s could not compile %s:\n%s"
                % (cc, os.path.relpath(c_path, root), _tail(proc.stderr)))
    return out_bin


def hlc_emit(root, hlc_bin, out_c):
    cmd = [hlc_bin, ENTRY, out_c]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        return False, proc.stderr
    return True, ""


def seed_emit(root, python, out_c):
    cmd = [python, "boot/boot.py", ENTRY, ENTRY, out_c]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        return False, proc.stderr
    return True, ""


def _same_bytes(a, b):
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca = fa.read(1 << 20)
            cb = fb.read(1 << 20)
            if ca != cb:
                return False
            if not ca:
                return True


# ---------------------------------------------------------------------------
# Commands.
# ---------------------------------------------------------------------------

def cmd_status(args):
    root = args.root
    state, drift = classify(root)
    if args.json:
        print(json.dumps({"state": state, "drift": drift}, sort_keys=True))
        return 0 if state == FRESH else 1
    if state == FRESH:
        manifest = read_manifest(root)
        print("snapshot: FRESH — the tree matches the snapshot "
              "(%s, %s lines, rounds=%s)"
              % (manifest.get("version"), manifest.get("hlc_c_lines"),
                 manifest.get("rounds")))
        return 0
    if state == NO_SNAPSHOT:
        print("snapshot: NO-SNAPSHOT — snapshot/hlc.c is missing; "
              "re-cut it with `make snapshot-regen --seed` "
              "(the native path builds FROM the snapshot)")
        return 1
    if state == NO_MANIFEST:
        print("snapshot: NO-MANIFEST — snapshot/hlc.c exists but "
              "snapshot/MANIFEST.json does not; run `make snapshot-regen`")
        return 1
    if state == TAMPERED:
        print("snapshot: TAMPERED — snapshot/hlc.c (or the manifest's own "
              "arithmetic) does not agree with snapshot/MANIFEST.json; a "
              "hand-edited snapshot is not a snapshot. Run "
              "`make snapshot-regen` to re-derive it from the sources")
        return 1
    print("snapshot: STALE-SOURCES — the sources moved after the snapshot "
          "was cut:")
    for verdict in ("REMOVED", "ADDED", "CHANGED"):
        for path in drift.get(verdict, []):
            print("  %-8s %s" % (verdict, path))
    print("run `make snapshot-regen` and commit snapshot/hlc.c together "
          "with snapshot/MANIFEST.json")
    return 1


def cmd_verify(args):
    root = args.root
    state, drift = classify(root)
    if state != FRESH:
        # Let status do the naming; re-run it for the message.
        cmd_status(arg_defaults(args))
        _refuse("the verify law",
                "verify only passes on a FRESH snapshot (state=%s)" % state)
    manifest = read_manifest(root)
    if manifest.get("version") != changelog_version(root):
        _refuse("the version law",
                "the manifest records release %s but CHANGELOG.md heads "
                "with %s — run `make snapshot-regen` so the manifest names "
                "the release the snapshot ships in"
                % (manifest.get("version"), changelog_version(root)))

    if args.deep:
        work = tempfile.mkdtemp(prefix="hls-snap-verify-")
        try:
            hlc_bin = cc_compile(root, args.cc,
                                 os.path.join(root, SNAPSHOT_C),
                                 os.path.join(work, "hlc"))
            out_c = os.path.join(work, "hlc_regen.c")
            ok, err = hlc_emit(root, hlc_bin, out_c)
            if not ok:
                _refuse("the fixed-point law",
                        "the compiler built from the snapshot could not "
                        "re-compile src/hlc.hls:\n%s" % _tail(err))
            if not _same_bytes(out_c, os.path.join(root, SNAPSHOT_C)):
                _refuse("the fixed-point law",
                        "the compiler built from snapshot/hlc.c emits "
                        "DIFFERENT bytes for src/hlc.hls than the snapshot "
                        "carries — the snapshot is stale or hand-edited; "
                        "run `make snapshot-regen`")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print("snapshot: VERIFIED (deep) — manifest agrees with itself and "
              "the world; the fixed point holds (%s)"
              % manifest.get("version"))
        return 0

    print("snapshot: VERIFIED (shallow) — manifest self-agreement and "
          "closure agreement hold (%s); the fixed point itself is "
          "`make bootstrap`'s law" % manifest.get("version"))
    return 0


def _regen_iteration(root, cc, first_c, work):
    """Iterate: compile candidate, re-emit, compare. Returns (c_path, rounds)."""
    current = first_c
    rounds = 1
    while True:
        hlc_bin = os.path.join(work, "hlc_r%d" % rounds)
        cc_compile(root, cc, current, hlc_bin)
        nxt = os.path.join(work, "regen_r%d.c" % rounds)
        ok, err = hlc_emit(root, hlc_bin, nxt)
        if not ok:
            _refuse("the fixed-point law",
                    "round %d: the derived compiler could not re-compile "
                    "src/hlc.hls:\n%s" % (rounds, _tail(err)))
        if _same_bytes(current, nxt):
            return current, rounds
        current = nxt
        rounds += 1
        if rounds > MAX_ROUNDS:
            _refuse("the convergence law",
                    "the regeneration did not reach a fixed point within "
                    "%d rounds — a codegen change that keeps changing its "
                    "own output is a bug, not a regen" % MAX_ROUNDS)


def cmd_regen(args):
    root = args.root
    state, _drift = classify(root)
    manifest = read_manifest(root)
    version = changelog_version(root)

    if manifest is not None and state == FRESH:
        if manifest.get("version") == version:
            _refuse("the no-op law",
                    "the tree already matches the snapshot at release %s; "
                    "a regeneration that changes nothing is noise, and "
                    "noise is how real regens stop being read. The two "
                    "legal triggers are: a source in the import closure "
                    "moved, or the release heading in CHANGELOG.md did"
                    % version)
        # Only the release heading moved: a manifest-only refresh.
        snap_path = os.path.join(root, SNAPSHOT_C)
        write_manifest(root, build_manifest(root, snap_path,
                                             manifest.get("rounds", 1)))
        print("snapshot: MANIFEST REFRESHED — emitted C unchanged; the "
              "manifest now records release %s (was %s)"
              % (version, manifest.get("version")))
        print("commit snapshot/MANIFEST.json (snapshot/hlc.c did not move)")
        return 0

    snap_path = os.path.join(root, SNAPSHOT_C)
    if not os.path.isfile(snap_path) and not args.seed:
        _refuse("the seed law",
                "the native path builds its compiler FROM snapshot/hlc.c, "
                "which is missing — rerun with --seed (the Python "
                "interpreter re-derives the C from the HLS sources)")

    work = tempfile.mkdtemp(prefix="hls-snap-regen-")
    try:
        if args.seed:
            first_c = os.path.join(work, "seed_pass1.c")
            ok, err = seed_emit(root, args.python, first_c)
            if not ok:
                _refuse("the seed law",
                        "boot/boot.py could not compile src/hlc.hls:\n%s"
                        % _tail(err))
            origin = "the Python seed (boot/boot.py pass 1)"
        else:
            # The OLD snapshot's compiler must accept the NEW sources.
            cur_bin = cc_compile(root, args.cc, snap_path,
                                 os.path.join(work, "hlc_cur"))
            first_c = os.path.join(work, "regen_native.c")
            ok, err = hlc_emit(root, cur_bin, first_c)
            if not ok:
                _refuse("the seed law",
                        "the current snapshot's compiler could not compile "
                        "the new sources (new syntax?):\n%s\nrerun with "
                        "--seed to derive through the Python interpreter"
                        % _tail(err))
            origin = "the compiler built from the previous snapshot"

        fixed_c, rounds = _regen_iteration(root, args.cc, first_c, work)

        moved = True
        if os.path.isfile(snap_path):
            moved = not _same_bytes(fixed_c, snap_path)

        # Atomic rewrite of BOTH files — they travel together.
        tmp_c = snap_path + ".tmp"
        shutil.copyfile(fixed_c, tmp_c)
        os.replace(tmp_c, snap_path)
        new_manifest = build_manifest(root, snap_path, rounds)
        write_manifest(root, new_manifest)

        print("snapshot: REGENERATED via %s" % origin)
        print("  fixed point reached in %d round%s"
              % (rounds, "s" if rounds != 1 else ""))
        if moved:
            print("  snapshot/hlc.c: %d lines, %d bytes (moved)"
                  % (new_manifest["hlc_c_lines"], new_manifest["hlc_c_bytes"]))
        else:
            print("  snapshot/hlc.c: %d lines, %d bytes (emitted C "
                  "unchanged — manifest refreshed for the moved sources)"
                  % (new_manifest["hlc_c_lines"], new_manifest["hlc_c_bytes"]))
        print("  manifest: %s, %d closure files, digest %s"
              % (new_manifest["version"], len(new_manifest["closure"]),
                 new_manifest["closure_digest"][:16] + "..."))
        print("commit snapshot/hlc.c and snapshot/MANIFEST.json TOGETHER "
              "(or neither)")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cmd_prove(args):
    root = args.root
    state, _drift = classify(root)
    if state != FRESH:
        _refuse("the drifted-tree law",
                "prove never runs from a tree the manifest does not "
                "describe (state=%s); run `make snapshot-regen` first"
                % state)
    manifest = read_manifest(root)
    work = tempfile.mkdtemp(prefix="hls-snap-prove-")
    try:
        pass1 = os.path.join(work, "seed_pass1.c")
        ok, err = seed_emit(root, args.python, pass1)
        if not ok:
            _refuse("the seed law",
                    "boot/boot.py could not compile src/hlc.hls:\n%s"
                    % _tail(err))
        if not _same_bytes(pass1, os.path.join(root, SNAPSHOT_C)):
            _refuse("the agreement law",
                    "the Python seed derives DIFFERENT bytes than the "
                    "committed snapshot carries — the snapshot's second "
                    "lineage is broken; run `make snapshot-regen --seed`")
        print("snapshot: PROVEN — the Python seed derives the committed "
              "snapshot byte for byte (%d lines, sha256 %s...)"
              % (manifest.get("hlc_c_lines"),
                 manifest.get("hlc_c_sha256")[:16]))
        print("  two independent lineages agree: the seed reading the "
              "sources, and the compiled snapshot reading them")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def arg_defaults(args):
    """A shim so verify can reuse status's printer."""
    ns = argparse.Namespace()
    ns.root = args.root
    ns.json = getattr(args, "json", False)
    return ns


def main(argv):
    parser = argparse.ArgumentParser(
        prog="hls-snap",
        description="The Stage 142 pure-bootstrap snapshot kit.")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="is the snapshot fresh? names the drift")
    p.add_argument("--root", default=_default_root())
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("verify",
                       help="manifest laws; --deep adds the fixed point")
    p.add_argument("--root", default=_default_root())
    p.add_argument("--deep", action="store_true")
    p.add_argument("--cc", default=os.environ.get("CC", CC_DEFAULT))
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("regen",
                       help="re-cut the snapshot at the fixed point")
    p.add_argument("--root", default=_default_root())
    p.add_argument("--seed", action="store_true",
                   help="derive pass 1 through the Python interpreter "
                        "(new syntax); default is the native path")
    p.add_argument("--cc", default=os.environ.get("CC", CC_DEFAULT))
    p.add_argument("--python",
                   default=os.environ.get("HALS_PYTHON", sys.executable))

    p = sub.add_parser("prove",
                       help="the seed derives the snapshot, byte for byte")
    p.add_argument("--root", default=_default_root())
    p.add_argument("--cc", default=os.environ.get("CC", CC_DEFAULT))
    p.add_argument("--python",
                   default=os.environ.get("HALS_PYTHON", sys.executable))

    args = parser.parse_args(argv)
    if not args.cmd:
        _usage_error("no command; try status | verify | regen | prove")
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "regen":
        return cmd_regen(args)
    if args.cmd == "prove":
        return cmd_prove(args)
    _usage_error("unknown command %s" % args.cmd)


def _default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
