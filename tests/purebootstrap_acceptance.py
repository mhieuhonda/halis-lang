#!/usr/bin/env python3
"""Stage 142 acceptance gate — the pure-HLS bootstrap: the Python
seed leaves the build.

Run with `make purebootstrap-acceptance` (or
`python3 tests/purebootstrap_acceptance.py`).

Seven sections:

  1. the no-python law    — `make bootstrap` succeeds under a PATH
                            that cannot resolve python3 or python, on
                            a tree copy that does not even CARRY
                            boot/: the build path is cc + the
                            committed snapshot, nothing else
  2. the fixed-point law  — the compiler built from the snapshot
                            re-derives the snapshot byte for byte; a
                            hand-edited snapshot fails the build with
                            the named refusal; a SEMANTIC source
                            change fails it too; a comment-only
                            change does NOT (codegen is semantic, not
                            textual — the build never cries wolf over
                            a moved comment)
  3. the regen law        — the only legal mutation: regen re-derives
                            at the fixed point (1 round here), turns
                            the drifted tree green again, and moves
                            the changed bytes into the snapshot; a
                            regen on a matching tree is REFUSED (the
                            no-op law); a release-heading change
                            refreshes the manifest ONLY (emitted C
                            unchanged); the native path refuses to run
                            without a snapshot to build from
  4. the agreement law    — `prove`: the Python seed derives the
                            committed snapshot byte for byte — two
                            independent lineages, one result
  5. the manifest law     — the document agrees with itself and the
                            world: a broken digest is a named refusal,
                            a hand-edited snapshot is TAMPERED, a
                            missing snapshot is NO-SNAPSHOT
  6. the independence     — the purely-bootstrapped bin/hlc compiles
     smoke                  and runs a real program with output
                            byte-identical to the interpreter's
  7. the provenance law   — the gate re-derives the import closure
                            with its OWN walker and demands it equal
                            the manifest's, the version pins the
                            CHANGELOG head, the rounds are sane, and
                            the snapshot text carries no absolute
                            paths (the same C from any checkout dir)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_pboot_", dir=os.path.join(ROOT, "tests"))

NEEDED = ["gcc", "cc", "make", "cmp", "diff", "mkdir", "cp", "rm", "cat",
          "sh", "bash", "ld", "as"]

IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"')


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def copy_tree(tag):
    """A disposable build tree: Makefile + mk/ + src/ + snapshot/ + the
    kit + CHANGELOG for the version law. Deliberately WITHOUT boot/ —
    the pure build must not read it."""
    dst = os.path.join(TMP, tag)
    os.makedirs(dst)
    shutil.copy2(os.path.join(ROOT, "Makefile"), dst)
    shutil.copy2(os.path.join(ROOT, "CHANGELOG.md"), dst)
    shutil.copytree(os.path.join(ROOT, "mk"), os.path.join(dst, "mk"))
    shutil.copytree(os.path.join(ROOT, "src"), os.path.join(dst, "src"))
    shutil.copytree(os.path.join(ROOT, "snapshot"),
                    os.path.join(dst, "snapshot"))
    tools_dir = os.path.join(dst, "tools")
    os.makedirs(tools_dir)
    shutil.copy2(os.path.join(ROOT, "tools", "hls-snap.py"), tools_dir)
    return dst


def python_free_env():
    """A PATH holding the C toolchain and coreutils — and provably no
    python3 / python anywhere on it."""
    bin_dir = os.path.join(TMP, "nopybin")
    if not os.path.isdir(bin_dir):
        os.makedirs(bin_dir)
        for name in NEEDED:
            src = shutil.which(name)
            if src:
                os.symlink(src, os.path.join(bin_dir, name))
        # Silent stubs for the Makefile's parse-time probes.
        for name in ("curl-config", "pkg-config"):
            stub = os.path.join(bin_dir, name)
            with open(stub, "w") as fh:
                fh.write("#!/bin/sh\nexit 1\n")
            os.chmod(stub, 0o755)
    for name in ("python3", "python"):
        if shutil.which(name, path=bin_dir) is not None:
            bad("no-python PATH: %s unexpectedly resolvable — the gate "
                "cannot prove the law" % name)
            sys.exit(1)
    return {"PATH": bin_dir, "TMPDIR": TMP}


def make_bootstrap(tree, env=None):
    return run(["make", "bootstrap"], cwd=tree,
               env=env if env is not None else dict(os.environ))


def snap(tree, *args):
    return run([sys.executable, "tools/hls-snap.py"] + list(args),
               cwd=tree)


# ---------------------------------------------------------------------------
print("=== 1. the no-python law ===")
# ---------------------------------------------------------------------------
tree1 = copy_tree("t1")
if os.path.exists(os.path.join(tree1, "boot")):
    bad("the build tree carries boot/ — the gate cannot prove the "
        "build never reads it")
else:
    ok("the build tree carries no boot/ at all")
r = make_bootstrap(tree1, python_free_env())
if r.returncode == 0 and "pure-HLS fixed point holds" in r.stdout:
    ok("make bootstrap succeeds with no python3 and no python on PATH "
       "(cc + the committed snapshot only)")
else:
    bad("no-python bootstrap failed (rc=%d): %s"
        % (r.returncode, (r.stdout + r.stderr)[:400]))
if re.search(r"python[0-9]?\s+boot/boot\.py", r.stdout + r.stderr) \
        or "Stage-0:" in r.stdout:
    bad("the bootstrap chain invokes the interpreter — it is not pure")
else:
    ok("the bootstrap chain invokes no interpreter (the only 'python' "
       "in the output is the word the success message is proud of)")

# ---------------------------------------------------------------------------
print("=== 2. the fixed-point law ===")
# ---------------------------------------------------------------------------
# 2a. A hand-edited snapshot: flip a byte inside the header comment.
snap_path = os.path.join(tree1, "snapshot", "hlc.c")
orig_bytes = open(snap_path, "rb").read()
tampered = orig_bytes.replace(b"generated BY hlc", b"generated BY hlx", 1)
assert tampered != orig_bytes
with open(snap_path, "wb") as fh:
    fh.write(tampered)
r = make_bootstrap(tree1, python_free_env())
if r.returncode != 0 and "stale" in r.stdout and "hand-edited" in r.stdout:
    ok("a hand-edited snapshot fails the build with the named refusal")
else:
    bad("the tampered snapshot did not fail loudly (rc=%d)" % r.returncode)
with open(snap_path, "wb") as fh:
    fh.write(orig_bytes)

# 2b. A SEMANTIC source change: one byte inside a string literal.
main_hls = os.path.join(tree1, "src", "hlc", "main.hls")
src_orig = open(main_hls, "r", encoding="utf-8").read()
needle = "usage: hlc --audit <input.hls>"
if needle in src_orig:
    drifted = src_orig.replace(needle, needle + " ", 1)
    with open(main_hls, "w", encoding="utf-8") as fh:
        fh.write(drifted)
    r = make_bootstrap(tree1, python_free_env())
    if r.returncode != 0 and "differs from snapshot" in r.stdout:
        ok("a semantic source change fails the build (the snapshot is "
           "stale) — named, not guessed")
    else:
        bad("a semantic drift did not fail the build (rc=%d)" % r.returncode)
    with open(main_hls, "w", encoding="utf-8") as fh:
        fh.write(src_orig)
else:
    bad("the drift needle moved — update the gate to the current "
        "usage string in src/hlc/main.hls")

# 2c. A comment-only change: the build stays green (codegen is
# semantic, not textual) — the manifest names it instead (section 7).
types_hls = os.path.join(tree1, "src", "hlc", "types.hls")
types_orig = open(types_hls, "r", encoding="utf-8").read()
with open(types_hls, "w", encoding="utf-8") as fh:
    fh.write(types_orig + "\n# gate: comment-only drift\n")
r = make_bootstrap(tree1, python_free_env())
if r.returncode == 0:
    ok("a comment-only change does NOT fail the build (no false wolf)")
else:
    bad("a comment-only change failed the build — the fixed-point law "
        "cries wolf over text (rc=%d)" % r.returncode)
r = snap(tree1, "status")
if r.returncode != 0 and "STALE-SOURCES" in r.stdout and "types.hls" in r.stdout:
    ok("...and the manifest names it: STALE-SOURCES at src/hlc/types.hls")
else:
    bad("the manifest did not name the comment-only drift: %s"
        % (r.stdout + r.stderr)[:300])
with open(types_hls, "w", encoding="utf-8") as fh:
    fh.write(types_orig)
r = make_bootstrap(tree1, python_free_env())
if r.returncode == 0:
    ok("the restored tree bootstraps green again")
else:
    bad("the restored tree still fails (rc=%d)" % r.returncode)

# ---------------------------------------------------------------------------
print("=== 3. the regen law ===")
# ---------------------------------------------------------------------------
tree2 = copy_tree("t2")
main_hls = os.path.join(tree2, "src", "hlc", "main.hls")
src_orig = open(main_hls, "r", encoding="utf-8").read()
needle = "usage: hlc --audit <input.hls>"
with open(main_hls, "w", encoding="utf-8") as fh:
    fh.write(src_orig.replace(needle, needle + " ", 1))
r = snap(tree2, "regen")
if r.returncode == 0 and "REGENERATED" in r.stdout and "1 round" in r.stdout:
    ok("regen re-derives the drifted tree at the fixed point (1 round)")
else:
    bad("regen on a drifted tree failed: %s" % (r.stdout + r.stderr)[:400])
if (needle + " ") in open(os.path.join(tree2, "snapshot", "hlc.c")).read():
    ok("the changed bytes travelled into the snapshot")
else:
    bad("the snapshot did not absorb the change")
r = make_bootstrap(tree2, python_free_env())
if r.returncode == 0:
    ok("the regenerated tree bootstraps green")
else:
    bad("the regenerated tree still fails (rc=%d)" % r.returncode)
# The no-op law: the tree now matches — a second regen is noise.
r = snap(tree2, "regen")
if r.returncode == 1 and "no-op law" in r.stdout:
    ok("a regen on the matching tree is REFUSED (the no-op law)")
else:
    bad("the no-op regen was not refused (rc=%d)" % r.returncode)

# The version law: a new release heading refreshes the manifest ONLY.
tree2b = copy_tree("t2b")
sha_before = sha256_file(os.path.join(tree2b, "snapshot", "hlc.c"))
chlog = os.path.join(tree2b, "CHANGELOG.md")
head = open(chlog, "r", encoding="utf-8").read()
open(chlog, "w", encoding="utf-8").write(
    "## [v9.9.9-alpha] — gate: a heading move\n\n" + head)
r = snap(tree2b, "regen")
man = json.load(open(os.path.join(tree2b, "snapshot", "MANIFEST.json")))
if (r.returncode == 0 and "MANIFEST REFRESHED" in r.stdout
        and man["version"] == "v9.9.9-alpha"
        and sha256_file(os.path.join(tree2b, "snapshot", "hlc.c")) == sha_before):
    ok("a release-heading move refreshes the manifest ONLY (C unmoved)")
else:
    bad("the version refresh misbehaved: %s" % (r.stdout + r.stderr)[:300])

# The seed law: the native path refuses to run without a snapshot.
tree2c = copy_tree("t2c")
os.remove(os.path.join(tree2c, "snapshot", "hlc.c"))
r = snap(tree2c, "regen")
if r.returncode == 1 and "seed law" in r.stdout:
    ok("the native regen refuses when the snapshot is missing (it is "
       "the seed it builds from)")
else:
    bad("missing-snapshot regen not refused (rc=%d)" % r.returncode)

# ---------------------------------------------------------------------------
print("=== 4. the agreement law (the seed re-derives the snapshot) ===")
# ---------------------------------------------------------------------------
r = snap(ROOT, "prove")
if r.returncode == 0 and "PROVEN" in r.stdout:
    ok("prove: the Python seed derives the committed snapshot byte for "
       "byte (two independent lineages agree)")
else:
    bad("prove failed: %s" % (r.stdout + r.stderr)[:400])

# ---------------------------------------------------------------------------
print("=== 5. the manifest law ===")
# ---------------------------------------------------------------------------
r = snap(ROOT, "verify")
if r.returncode == 0 and "VERIFIED" in r.stdout:
    ok("verify: the document agrees with itself and with the world")
else:
    bad("verify failed on the real tree: %s" % (r.stdout + r.stderr)[:400])

tree5 = copy_tree("t5")
man_path = os.path.join(tree5, "snapshot", "MANIFEST.json")
man = json.load(open(man_path))
man["hlc_c_sha256"] = "0" * 64
json.dump(man, open(man_path, "w"), sort_keys=True, indent=2)
r = snap(tree5, "status")
if r.returncode == 1 and "TAMPERED" in r.stdout:
    ok("a snapshot that disagrees with its recorded digest is TAMPERED")
else:
    bad("the digest disagreement was not named (rc=%d)" % r.returncode)

tree5b = copy_tree("t5b")
c_path = os.path.join(tree5b, "snapshot", "hlc.c")
body = open(c_path, "rb").read()
open(c_path, "wb").write(body.replace(b"generated BY hlc", b"generated BY hlx", 1))
r = snap(tree5b, "status")
if r.returncode == 1 and "TAMPERED" in r.stdout:
    ok("a hand-edited snapshot is TAMPERED at status time (the fast "
       "path names it without compiling)")
else:
    bad("the hand edit was not named TAMPERED (rc=%d)" % r.returncode)

tree5c = copy_tree("t5c")
os.remove(os.path.join(tree5c, "snapshot", "hlc.c"))
r = snap(tree5c, "status")
if r.returncode == 1 and "NO-SNAPSHOT" in r.stdout:
    ok("a missing snapshot is NO-SNAPSHOT (with the --seed advice)")
else:
    bad("the missing snapshot was not named (rc=%d)" % r.returncode)

# ---------------------------------------------------------------------------
print("=== 6. the independence smoke ===")
# ---------------------------------------------------------------------------
r = run(["make", "bootstrap"])
if r.returncode != 0:
    bad("the real-tree bootstrap failed before the smoke: %s"
        % (r.stdout + r.stderr)[:300])
else:
    fib_c = os.path.join(TMP, "fib.c")
    fib_bin = os.path.join(TMP, "fib")
    r1 = run(["./bin/hlc", "examples/fibonacci.hls", fib_c])
    r2 = run(["gcc", "-O2", "-o", fib_bin, fib_c, "-lm", "-pthread"])
    nat = run([fib_bin])
    interp = run([sys.executable, "boot/boot.py",
                  "examples/fibonacci.hls"])
    if (r1.returncode == 0 and r2.returncode == 0
            and nat.stdout == interp.stdout
            and nat.returncode == interp.returncode):
        ok("the purely-bootstrapped hlc compiles + runs fibonacci with "
           "interpreter-identical output")
    else:
        bad("the smoke diverged (nat rc=%d interp rc=%d)"
            % (nat.returncode, interp.returncode))

# ---------------------------------------------------------------------------
print("=== 7. the provenance law ===")
# ---------------------------------------------------------------------------
# The gate's OWN closure walker — never the tool's reader.
entry = os.path.join(ROOT, "src", "hlc.hls")
queue = [entry]
seen = {}
while queue:
    cur = queue.pop(0)
    if cur in seen:
        continue
    seen[cur] = sha256_file(cur)
    for line in open(cur, "r", encoding="utf-8", errors="replace"):
        m = IMPORT_RE.match(line)
        if not m:
            continue
        imp = m.group(1)
        if imp.startswith("std.") or imp.startswith("core."):
            fam = "std" if imp.startswith("std.") else "core"
            cand = os.path.join(ROOT, fam, imp.split(".", 1)[1].replace(".", "/") + ".hls")
        else:
            cand = os.path.join(os.path.dirname(cur), imp)
        if not os.path.isfile(cand):
            bad("provenance: unresolvable import %s in %s" % (imp, cur))
            cand = None
        if cand and cand not in seen:
            queue.append(cand)
gate_closure = {os.path.relpath(p, ROOT).replace(os.sep, "/"): d
                for p, d in seen.items()}
man = json.load(open(os.path.join(ROOT, "snapshot", "MANIFEST.json")))
if man["closure"] == gate_closure:
    ok("the manifest closure equals the gate's own derivation "
       "(%d files)" % len(gate_closure))
else:
    bad("the manifest closure disagrees with the gate's walk "
        "(manifest %d vs gate %d files)"
        % (len(man["closure"]), len(gate_closure)))

head_ver = None
for line in open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8"):
    m = re.match(r"^## \[(v[0-9][^\]\s]*)\]", line)
    if m:
        head_ver = m.group(1)
        break
if head_ver and man["version"] == head_ver:
    ok("the manifest pins the CHANGELOG head release (%s)" % head_ver)
else:
    bad("the manifest version %s != CHANGELOG head %s"
        % (man.get("version"), head_ver))

if 1 <= int(man.get("rounds", 0)) <= 8:
    ok("the recorded rounds are sane (rounds=%s)" % man.get("rounds"))
else:
    bad("the manifest rounds are insane: %s" % man.get("rounds"))

snap_text = open(os.path.join(ROOT, "snapshot", "hlc.c"), "r",
                 encoding="utf-8", errors="replace").read()
leaks = [p for p in (ROOT, TMP, os.path.join(ROOT, "tests")) if p and p in snap_text]
if not leaks:
    ok("the snapshot text carries no absolute path (the same C from "
       "any checkout directory)")
else:
    bad("the snapshot leaks absolute paths: %s" % leaks)

# ---------------------------------------------------------------------------
print()
print("GATE RESULT: %d pass, %d fail" % (PASS, FAIL))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
