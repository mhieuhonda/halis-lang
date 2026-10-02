#!/usr/bin/env python3
"""Stage 104 acceptance gate — hls-sbom, the CycloneDX + SPDX bill
of materials.

Run with `make sbom-acceptance` (or
`python3 tests/sbom_acceptance.py`).

Eight sections:

  1. the engine        — the CycloneDX document for the demo tree,
                         pinned: bomFormat, specVersion 1.5, the
                         content-derived serialNumber, the metadata
                         component (the entry, application, hashed),
                         the three file components (toolchain versioned
                         with the compiler, workspace not), every
                         SHA-256 over the real bytes, the effect split
                         as properties, and the dependency graph
  2. the SPDX          — the same tree in SPDX-2.3: CC0-1.0, DESCRIBES
                         + DEPENDS_ON from the SAME edges, the same
                         hashes (the two documents cannot disagree),
                         APPLICATION/LIBRARY purposes, NOASSERTION
                         where the tool genuinely does not know
  3. determinism       — same tree, same SOURCE_DATE_EPOCH →
                         byte-identical documents; the serial and the
                         namespace are content-addressed: one flipped
                         byte in a component moves both
  4. the package tree  — package mode on the in-tree demo: the root
                         application with a purl, the two libraries
                         (a diamond is ONE component, two edges), the
                         directory hashes hls-pkg itself computes, the
                         deep package's effect surface in its
                         properties
  5. the lock          — the release gate: no lockfile, no release;
                         a fresh lock releases (versioned documents
                         written, the SBOM's own hashes chained into
                         the transparency log, the chain still
                         verifies); content changed under the lock is
                         drift and refuses WITHOUT touching the log
  6. the parity        — the SBOM's effect properties equal
                         hls-audit's per-module surfaces on the same
                         tree (the two tools share one walker; the
                         gate proves it end to end)
  7. the demos         — the demo and the ok-test run; interpreter and
                         native agree byte for byte (native half only
                         when bin/hlc exists — the gate stays hermetic)
  8. the tools         — hlfmt stable, hllint clean on the new entry
                         sources
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

SBOM = [sys.executable, os.path.join(ROOT, "tools", "hls-sbom.py")]
AUDIT = [sys.executable, os.path.join(ROOT, "tools", "hls-audit.py")]
PKG = [sys.executable, os.path.join(ROOT, "tools", "hls-pkg.py")]
DEMO = "examples/sbom_demo.hls"
PKG_DEMO = "examples/pkg_audit_demo"
OK_TEST = "tests/ok/feat_stage104_sbom.hls"

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s104_", dir=os.path.join(ROOT, "tests"))
LOG_LOCK = os.path.join(ROOT, ".hls-pkg-transparency.log.lock")
LOG_MAIN = os.path.join(ROOT, ".hls-pkg-transparency.log")


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


def expect(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def props_of(comp):
    return {p["name"]: p["value"] for p in comp.get("properties", [])}


def cdx_doc(*extra):
    p = run(SBOM + list(extra) + ["--format", "cdx", "--stdout"])
    return p, json.loads(p.stdout)


def spdx_doc(*extra):
    p = run(SBOM + list(extra) + ["--format", "spdx", "--stdout"])
    return p, json.loads(p.stdout)


# ---------------------------------------------------------------------------
print("=== 1. the engine (CycloneDX, source mode) ===")
# ---------------------------------------------------------------------------

p, cdx = cdx_doc(DEMO)
expect(p.returncode == 0, "the demo produces a CycloneDX document (exit 0)")
expect(cdx["bomFormat"] == "CycloneDX" and cdx["specVersion"] == "1.5"
       and cdx["version"] == 1,
       "bomFormat CycloneDX, specVersion 1.5, version 1")
expect(cdx["serialNumber"].startswith("urn:uuid:")
       and len(cdx["serialNumber"]) == len("urn:uuid:") + 36,
       "the serialNumber is a content-derived UUID")
expect(cdx["metadata"]["tools"][0]["name"] == "hls-sbom"
       and cdx["metadata"]["tools"][0]["vendor"] == "halis-lang",
       "the tool is named (who made this document)")

root = cdx["metadata"]["component"]
expect(root["type"] == "application" and root["name"] == DEMO,
       "the metadata component is the entry (application)")
expect(root["hashes"][0]["content"] == sha256_of(DEMO),
       "the root's hash is over the entry's real bytes")
rprops = props_of(root)
expect(rprops["hls:effects:intrinsic"] == "IO"
       and rprops["hls:effects:surface"] == "Clock, Fs, IO, Net",
       "the root's properties carry the audit's intrinsic/surface split")

comps = {c["name"]: c for c in cdx["components"]}
expect(len(cdx["components"]) == 3, "three components underneath the root")
expect(all(c["type"] == "file" for c in cdx["components"]),
       "every module is a file component")
expect(comps["std/str.hls"]["version"] == "0.123.0-alpha"
       and "version" not in comps["examples/sbom_libs/dep_b.hls"],
       "toolchain components are versioned with the compiler; workspace "
       "modules carry their own bytes")
expect(props_of(comps["std/str.hls"])["hls:kind"] == "toolchain"
       and props_of(comps["examples/sbom_libs/dep_b.hls"])["hls:kind"]
       == "workspace",
       "the classification rides along as hls:kind")
expect(props_of(comps["examples/sbom_libs/dep_b.hls"])[
           "hls:effects:intrinsic"] == "Clock, Fs",
       "dep_b's properties name Clock + Fs as intrinsic")
expect(props_of(comps["examples/sbom_libs/dep_a.hls"])[
           "hls:effects:intrinsic"] == "Net",
       "dep_a introduces Net (the localhost lookup)")
for name, c in comps.items():
    if c["hashes"][0]["content"] != sha256_of(name):
        bad("component hash over the real bytes: %s" % name)
        break
else:
    ok("every component hash is over the component's real bytes")

deps = {d["ref"]: d["dependsOn"] for d in cdx["dependencies"]}
expect(deps["hls:module:" + DEMO] ==
       ["hls:module:examples/sbom_libs/dep_a.hls",
        "hls:module:std/str.hls"],
       "the root depends on dep_a and the toolchain module")
expect(deps["hls:module:examples/sbom_libs/dep_a.hls"] ==
       ["hls:module:examples/sbom_libs/dep_b.hls",
        "hls:module:std/str.hls"],
       "dep_a depends on dep_b and the toolchain module")
expect(deps["hls:module:examples/sbom_libs/dep_b.hls"] ==
       ["hls:module:std/str.hls"]
       and deps["hls:module:std/str.hls"] == [],
       "the graph is the import tree, leaf to root")

# ---------------------------------------------------------------------------
print("=== 2. the SPDX document (same tree, same truth) ===")
# ---------------------------------------------------------------------------

p, spdx = spdx_doc(DEMO)
expect(p.returncode == 0, "the demo produces an SPDX document (exit 0)")
expect(spdx["spdxVersion"] == "SPDX-2.3"
       and spdx["dataLicense"] == "CC0-1.0",
       "SPDX-2.3 under CC0-1.0 (the document itself is open)")
expect(spdx["SPDXID"] == "SPDXRef-DOCUMENT",
       "the document element is SPDXRef-DOCUMENT")
expect("Tool: hls-sbom-0.123.0-alpha" in spdx["creationInfo"]["creators"],
       "the creator names the tool AND its version")

pkgs = {x["name"]: x for x in spdx["packages"]}
expect(len(spdx["packages"]) == 4, "four packages: the root + three parts")
expect(pkgs[DEMO]["primaryPackagePurpose"] == "APPLICATION"
       and pkgs["std/str.hls"]["primaryPackagePurpose"] == "LIBRARY",
       "the root is APPLICATION, the parts are LIBRARY")
expect(all(x["downloadLocation"] == "NOASSERTION"
           for x in spdx["packages"]),
       "downloadLocation is NOASSERTION (the tool does not invent URLs)")
expect(all(x["licenseConcluded"] == "NOASSERTION"
           for x in spdx["packages"]),
       "licenseConcluded is NOASSERTION (no guesses in a legal field)")
expect(pkgs["examples/sbom_libs/dep_b.hls"]["checksums"][0][
           "checksumValue"] == sha256_of("examples/sbom_libs/dep_b.hls"),
       "SPDX checksums are over the same real bytes")

rel = [(r["relationshipType"], r["spdxElementId"], r["relatedSpdxElement"])
       for r in spdx["relationships"]]
describes = [r for r in rel if r[0] == "DESCRIBES"]
depends = [r for r in rel if r[0] == "DEPENDS_ON"]
expect(len(describes) == 1
       and describes[0][2] == pkgs[DEMO]["SPDXID"],
       "the document DESCRIBES the root")
expect(len(depends) == sum(len(v) for v in
                           {d["ref"]: d["dependsOn"]
                            for d in cdx["dependencies"]}.values()),
       "the DEPENDS_ON count matches the CycloneDX edge count")
cdx_deps = {d["ref"]: d["dependsOn"] for d in cdx["dependencies"]}
expect(len(depends) == sum(len(v) for v in cdx_deps.values()),
       "the two documents render the SAME edge set")


def spdx_id_of(name):
    return pkgs[name]["SPDXID"]


edge_set = {(r[1], r[2]) for r in depends}
want_edges = set()
for src, kids in cdx_deps.items():
    src_name = src.split(":", 2)[-1]
    for kid in kids:
        kid_name = kid.split(":", 2)[-1]
        want_edges.add((spdx_id_of(src_name), spdx_id_of(kid_name)))
expect(edge_set == want_edges,
       "every CycloneDX edge has its SPDX twin (by element id)")

# ---------------------------------------------------------------------------
print("=== 3. determinism (content-addressed documents) ===")
# ---------------------------------------------------------------------------

scratch = os.path.join(TMP, "det")
os.makedirs(os.path.join(scratch, "sbom_libs"), exist_ok=True)
shutil.copyfile(DEMO, os.path.join(scratch, "sbom_demo.hls"))
shutil.copyfile("examples/sbom_libs/dep_a.hls",
                os.path.join(scratch, "sbom_libs", "dep_a.hls"))
shutil.copyfile("examples/sbom_libs/dep_b.hls",
                os.path.join(scratch, "sbom_libs", "dep_b.hls"))
scratch_entry = os.path.join(scratch, "sbom_demo.hls")

env = dict(os.environ, SOURCE_DATE_EPOCH="1727800000")
a = run(SBOM + [scratch_entry, "--format", "cdx", "--stdout"], env=env)
b = run(SBOM + [scratch_entry, "--format", "cdx", "--stdout"], env=env)
expect(a.returncode == 0 and b.returncode == 0
       and a.stdout == b.stdout,
       "same tree, same SOURCE_DATE_EPOCH — byte-identical CycloneDX")
a2 = run(SBOM + [scratch_entry, "--format", "spdx", "--stdout"], env=env)
b2 = run(SBOM + [scratch_entry, "--format", "spdx", "--stdout"], env=env)
expect(a2.stdout == b2.stdout,
       "same tree, same SOURCE_DATE_EPOCH — byte-identical SPDX")

r1 = json.loads(run(SBOM + [scratch_entry, "--json"], env=env).stdout)
serial = r1["documents"]["serial_number"]
expect(serial.startswith("urn:uuid:"), "the report names the serial")
hexd = serial.replace("urn:uuid:", "").replace("-", "")
digest = r1["digest"]
expect(all(hexd[i] == digest[i]
           for i in range(32) if i not in (12, 16))
       and hexd[12] == "4" and hexd[16] in "89ab",
       "the serial is the digest, shaped (version + variant nibbles)")

# One flipped byte in one component moves the digest — the document
# is addressed by what it lists.
with open(os.path.join(scratch, "sbom_libs", "dep_b.hls"), "a") as f:
    f.write("\n# one byte more\n")
r2 = json.loads(run(SBOM + [scratch_entry, "--json"], env=env).stdout)
expect(r2["digest"] != r1["digest"]
       and r2["documents"]["serial_number"] != serial,
       "one changed component moves the serial (content-addressed)")

# Without SOURCE_DATE_EPOCH the timestamp is honest wall time and the
# serial still holds (it never depended on the clock).
r3 = json.loads(run(SBOM + [scratch_entry, "--json"]).stdout)
expect(r3["documents"]["serial_number"] == r2["documents"]["serial_number"],
       "the serial survives a changing clock")

# ---------------------------------------------------------------------------
print("=== 4. the package tree (package mode) ===")
# ---------------------------------------------------------------------------

p, pcdx = cdx_doc("--pkg", PKG_DEMO)
expect(p.returncode == 0, "the package demo produces a document (exit 0)")
proot = pcdx["metadata"]["component"]
expect(proot["type"] == "application"
       and proot["name"] == "pkg_audit_demo"
       and proot["version"] == "0.1.0",
       "the root is the manifest's package, versioned from the manifest")
expect(proot.get("purl") == "pkg:generic/halis/pkg_audit_demo@0.1.0",
       "the root carries a purl (generic type, halis namespace)")
pcomps = {c["name"]: c for c in pcdx["components"]}
expect(set(pcomps) == {"audit_lib_a", "audit_lib_b"},
       "the diamond is ONE component per package (two paths, one part)")
expect(all(c["type"] == "library" for c in pcdx["components"]),
       "dependencies are library components")
expect(props_of(pcomps["audit_lib_b"])["hls:depth"] == "2"
       and props_of(pcomps["audit_lib_b"])["hls:effects:surface"]
       == "Clock, Fs",
       "the deep package's depth and effect surface ride along")
expect(props_of(pcomps["audit_lib_a"])["hls:depth"] == "1",
       "the middle package sits at depth 1")

pdeps = {d["ref"]: d["dependsOn"] for d in pcdx["dependencies"]}
expect(pdeps["hls:pkg:pkg_audit_demo"] == ["hls:pkg:audit_lib_a"]
       and pdeps["hls:pkg:audit_lib_a"] == ["hls:pkg:audit_lib_b"]
       and pdeps["hls:pkg:audit_lib_b"] == [],
       "the package graph is the manifest tree")

# The hashes are hls-pkg's own directory hashes — the same bytes the
# lockfile would pin.
sys.path.insert(0, os.path.join(ROOT, "tools", "hpkg_parts"))
from hpkg_cmds import _sha256_directory                    # noqa: E402
pkg_dirs = {"audit_lib_a": "examples/pkg_audit_libs/audit_lib_a",
            "audit_lib_b": "examples/pkg_audit_libs/audit_lib_b"}
hash_ok = True
for name, d in pkg_dirs.items():
    real = _sha256_directory(os.path.join(ROOT, pkg_dirs[name]))
    if pcomps[name]["hashes"][0]["content"] != real:
        hash_ok = False
if hash_ok:
    ok("package component hashes are hls-pkg's own directory hashes")
else:
    bad("package component hash is not hls-pkg's directory hash")

p, pspdx = spdx_doc("--pkg", PKG_DEMO)
spkgs = {x["name"]: x for x in pspdx["packages"]}
expect(spkgs["audit_lib_b"]["checksums"][0]["checksumValue"]
       == pcomps["audit_lib_b"]["hashes"][0]["content"],
       "SPDX and CycloneDX agree on the package hash byte for byte")
expect(len(pspdx["packages"]) == 3,
       "three SPDX packages (root + the two libraries)")

# ---------------------------------------------------------------------------
print("=== 5. the lock (the release gate) ===")
# ---------------------------------------------------------------------------

rel_root = os.path.join(TMP, "rel_app")
dep_dir = os.path.join(TMP, "rel_dep")
write(os.path.join(dep_dir, "hls-pkg.toml"),
      '[package]\nname = "rel_dep"\nversion = "0.1.0"\n')
dep_file = write(os.path.join(dep_dir, "main.hls"),
                 'fn sz() -> int uses Fs {\n    return fs_size("a")\n}\n')
write(os.path.join(rel_root, "hls-pkg.toml"),
      '[package]\nname = "rel_app"\nversion = "2.3.4"\n\n'
      '[dependencies]\nrel_dep = { path = "%s" }\n\n'
      "[effects]\nallowed = [\"IO\", \"Fs\"]\n"
      % os.path.relpath(dep_file, ROOT))
write(os.path.join(rel_root, "main.hls"),
      'fn main() -> int uses IO {\n    println("x")\n    return 0\n}\n')

had_log = os.path.exists(LOG_MAIN)
had_lock_file = os.path.exists(LOG_LOCK)

# No lockfile, no release.
p = run(SBOM + ["--pkg", rel_root, "--release"])
expect(p.returncode == 1
       and "no lockfile" in p.stderr and "hls-pkg lock" in p.stderr,
       "--release without a lockfile refuses (exit 1, says what to do)")
expect(not os.path.exists(os.path.join(
           rel_root, "hls-sbom-rel_app-2.3.4.cdx.json")),
       "the refused release wrote nothing")

# A fresh lock releases: versioned documents + a chained log record.
lp = run(PKG + ["lock"], cwd=rel_root)
expect(lp.returncode == 0, "hls-pkg lock pins the release fixture")
lock = json.load(open(os.path.join(rel_root, "hls-pkg.lock")))
locked_sha = {x["name"]: x["sha256"]
              for x in lock["packages"]}[  "rel_dep"]
log_lines_before = 0
if os.path.exists(LOG_MAIN):
    with open(LOG_MAIN, "rb") as f:
        log_lines_before = len(f.readlines())

p = run(SBOM + ["--pkg", rel_root, "--release"])
expect(p.returncode == 0, "the locked tree releases (exit 0)")
cdx_out = os.path.join(rel_root, "hls-sbom-rel_app-2.3.4.cdx.json")
spdx_out = os.path.join(rel_root, "hls-sbom-rel_app-2.3.4.spdx.json")
expect(os.path.isfile(cdx_out) and os.path.isfile(spdx_out),
       "the release writes hls-sbom-<name>-<version>.{cdx,spdx}.json")
rel_cdx = json.load(open(cdx_out))
rel_comp = {c["name"]: c for c in rel_cdx["components"]}["rel_dep"]
expect(rel_comp["hashes"][0]["content"] == locked_sha,
       "the released hash IS the lockfile's hash (byte for byte)")
doc_sha = hashlib.sha256(open(cdx_out, "rb").read()).hexdigest()

with open(LOG_MAIN, "rb") as f:
    lines = f.readlines()
expect(len(lines) == log_lines_before + 1,
       "one record appended to the transparency log")
rec = json.loads(lines[-1].decode("utf-8"))
expect(rec.get("kind") == "sbom" and rec.get("name") == "rel_app"
       and rec.get("version") == "2.3.4"
       and rec.get("cdx_sha256") == doc_sha
       and rec.get("spdx_sha256")
       == hashlib.sha256(open(spdx_out, "rb").read()).hexdigest(),
       "the record chains the SBOM documents' own hashes")
vp = run(PKG + ["log", "--verify"])
expect(vp.returncode == 0, "the transparency chain still verifies")

# Content changed under the lock — drift refuses, the log is untouched.
with open(dep_file, "a") as f:
    f.write("\nfn helper() -> int uses Rand {\n"
            "    return rand_int(0, 9)\n}\n")
p = run(SBOM + ["--pkg", rel_root, "--release"])
with open(LOG_MAIN, "rb") as f:
    lines_after = len(f.readlines())
expect(p.returncode == 1 and "drift" in p.stderr,
       "content changed under the lock — the release refuses (exit 1)")
expect(lines_after == log_lines_before + 1,
       "the refused release appended NOTHING to the log")

# Gate scratch hygiene: the log the lock wrote is not left behind for
# the commit.
if not had_log and os.path.exists(LOG_MAIN):
    try:
        os.unlink(LOG_MAIN)
    except OSError:
        pass
if not had_lock_file and os.path.exists(LOG_LOCK):
    try:
        os.unlink(LOG_LOCK)
    except OSError:
        pass
cache_dir = os.path.join(ROOT, ".hls-pkg-cache")
if os.path.isdir(cache_dir) and not os.listdir(cache_dir):
    os.rmdir(cache_dir)

# ---------------------------------------------------------------------------
print("=== 6. the parity (one walker, two reports) ===")
# ---------------------------------------------------------------------------

sb = json.loads(run(SBOM + [DEMO, "--json"]).stdout)
au = json.loads(run(AUDIT + [DEMO, "--json"]).stdout)
rc = run(SBOM + [DEMO, "--format", "cdx", "--stdout"])
cdx_once = json.loads(rc.stdout)
sb_surf = {c["name"]: c["properties"].get("hls:effects:surface", "")
           for c in sb["components"]}
root_props = props_of(cdx_once["metadata"]["component"])
sb_surf[sb["root"]["name"]] = root_props["hls:effects:surface"]
au_surf = {m["key"]: ", ".join(m["surface"]) for m in au["modules"]}
mismatch = [k for k in au_surf
            if sb_surf.get(k, "") != au_surf[k]]
if not mismatch:
    ok("every module's surface property equals hls-audit's surface")
else:
    bad("surface mismatch between the tools: %s" % mismatch)
expect(sb["dependencies"] and len(sb["dependencies"]) == len(au["edges"]),
       "the SBOM renders the audit's edge count")

expect(root_props["hls:effects:surface"] == ", ".join(au["program_surface"]),
       "the root's surface property is the audit's program surface")

psb = json.loads(run(SBOM + ["--pkg", PKG_DEMO, "--json"]).stdout)
pau = json.loads(run(AUDIT + ["--pkg", PKG_DEMO, "--json"]).stdout)
lib_b_au = ", ".join(next(x["surface"] for x in pau["packages"]
                          if x["name"] == "audit_lib_b"))
expect(psb["root"]["name"] == pau["root"]["name"]
       and next(c for c in psb["components"]
                if c["name"] == "audit_lib_b")["properties"][
                    "hls:effects:surface"] == lib_b_au,
       "package mode: the deep surface matches the audit's own report")

# ---------------------------------------------------------------------------
print("=== 7. the demos ===")
# ---------------------------------------------------------------------------

p1 = run([sys.executable, "boot/boot.py", DEMO])
expect(p1.returncode == 0 and "bill of materials for one small release"
       in p1.stdout,
       "the demo runs (interpreter)")
p2 = run([sys.executable, "boot/boot.py", OK_TEST])
expect(p2.returncode == 0 and "checks passed: 6" in p2.stdout,
       "the ok-test runs (interpreter)")

hlc = os.path.join(ROOT, "bin", "hlc")
if os.path.exists(hlc):
    c1 = os.path.join(TMP, "demo_native.c")
    c2 = os.path.join(TMP, "ok_native.c")
    b1 = os.path.join(TMP, "demo_native")
    b2 = os.path.join(TMP, "ok_native")
    ok1 = run([hlc, DEMO, c1]).returncode == 0 and \
        run(["cc", "-O2", "-o", b1, c1, "-lm", "-pthread"]).returncode == 0
    ok2 = run([hlc, OK_TEST, c2]).returncode == 0 and \
        run(["cc", "-O2", "-o", b2, c2, "-lm", "-pthread"]).returncode == 0
    if ok1 and ok2:
        n1 = run([b1])
        expect(n1.returncode == 0 and n1.stdout == p1.stdout,
               "the demo agrees byte for byte with the native binary")
        n2 = run([b2])
        expect(n2.returncode == 0 and n2.stdout == p2.stdout,
               "the ok-test agrees byte for byte with the native binary")
    else:
        bad("native compilation of the demos failed")
else:
    print("  [note] bin/hlc not built — the native half of section 7 "
          "is skipped (the gate stays hermetic)")

# ---------------------------------------------------------------------------
print("=== 8. the tools ===")
# ---------------------------------------------------------------------------

for f in (DEMO, OK_TEST, "examples/sbom_libs/dep_a.hls",
          "examples/sbom_libs/dep_b.hls"):
    with open(f) as fh:
        original = fh.read()
    p1f = os.path.join(TMP, "fmt1.hls")
    p2f = os.path.join(TMP, "fmt2.hls")
    with open(p1f, "w") as fh:
        fh.write(original)
    run([sys.executable, "tools/hlfmt.py", "-w", p1f])
    shutil.copyfile(p1f, p2f)
    run([sys.executable, "tools/hlfmt.py", "-w", p2f])
    with open(p1f) as fh:
        once = fh.read()
    with open(p2f) as fh:
        twice = fh.read()
    expect(once == twice, "hlfmt: stable formatting on %s" % f)

lints = [run([sys.executable, "tools/hllint.py", f])
         for f in (DEMO, OK_TEST)]
if all(l.returncode == 0 for l in lints):
    ok("hllint: no findings on the demo or the ok-test")
else:
    bad("hllint: findings on the new sources")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 104 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
