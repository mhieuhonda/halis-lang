#!/usr/bin/env python3
"""Stage 103 acceptance gate — hls-audit, the supply-chain effect
report (transitive).

Run with `make supply-acceptance` (or
`python3 tests/audit_acceptance.py`).

Eight sections:

  1. the engine        — the source-mode report on the demo, pinned
                         line for line: the module census (4 modules —
                         1 toolchain, 3 workspace), the per-module
                         intrinsic vs reachable split, the attribution
                         (Net at netcheck, Clock at geo through the
                         middle hop, IO at TWO introduction points),
                         the extern surface, the totals, exit 0
  2. the policy        — --allow gates the chain, not the root: the
                         violated run (exit 1, chain drawn), the clean
                         run (exit 0), the root-exemption flip (the
                         entry's own IO is never a violation), the
                         unknown-effect refusal (exit 2)
  3. fail-closed       — a broken tree is a broken build (source mode
                         exits 1 and says so); a broken PACKAGE is a
                         fail-closed node (full effect set, UNAUDITABLE
                         line, exit 1); the cycle refusal with the
                         chain that closes it; usage errors exit 2
  4. the package tree  — the in-tree demo: the manifest walk reaches
                         audit_lib_b through audit_lib_a's OWN manifest
                         (the hop hls-pkg audit never takes), the
                         chains, the manifest policy violated, exit 1;
                         --allow clean, exit 0; --json machine-exact
  5. the drift         — a fresh lockfile audits clean; content that
                         changed under the lock is DRIFT and fails the
                         run; a dep in the manifest but not in the
                         lockfile is drift too
  6. the parity        — the two modes agree on the root: the root
                         package's surface from --pkg equals the
                         source-mode surface of its main.hls
  7. the demos         — the demo and the ok-test run; interpreter and
                         native agree byte for byte (native half only
                         when bin/hlc exists — the gate stays hermetic)
  8. the tools         — hlfmt stable, hllint clean on the new sources
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

AUDIT = [sys.executable, os.path.join(ROOT, "tools", "hls-audit.py")]
PKG = [sys.executable, os.path.join(ROOT, "tools", "hls-pkg.py")]
DEMO = "examples/audit_demo.hls"
PKG_DEMO = "examples/pkg_audit_demo"
OK_TEST = "tests/ok/feat_stage103_supplychain.hls"

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s103_", dir=os.path.join(ROOT, "tests"))
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


# ---------------------------------------------------------------------------
print("=== 1. the engine (source mode) ===")
# ---------------------------------------------------------------------------

p = run(AUDIT + [DEMO])
expect(p.returncode == 0, "the demo audits clean (exit 0)")
out = p.stdout
jsonp = run(AUDIT + [DEMO, "--json"])
r = json.loads(jsonp.stdout)

mods = {m["key"]: m for m in r["modules"]}
expect(len(r["modules"]) == 4, "four modules in the tree (entry, two libs, std.str)")
expect(sum(1 for m in r["modules"] if m["kind"] == "toolchain") == 1,
       "std.str classified toolchain")
expect(sum(1 for m in r["modules"] if m["kind"] == "workspace") == 3,
       "entry + netcheck + geo classified workspace")
expect(mods["std/str.hls"]["intrinsic"] == [], "std.str is pure (no intrinsic effects)")
expect(mods["examples/audit_libs/geo.hls"]["intrinsic"] == ["Clock"],
       "geo introduces Clock intrinsically")
expect(mods["examples/audit_libs/netcheck.hls"]["intrinsic"] == ["IO", "Net"],
       "netcheck introduces IO (println) and Net (net_lookup)")
expect(mods["examples/audit_demo.hls"]["intrinsic"] == ["IO"],
       "the entry introduces IO (println)")
expect(mods["examples/audit_libs/netcheck.hls"]["surface"] == ["Clock", "IO", "Net"],
       "netcheck's surface carries geo's Clock as reachable")
expect(r["program_surface"] == ["Clock", "IO", "Net"],
       "program surface is Clock + IO + Net")
expect(len(r["extern_blocks"]) == 1
       and r["extern_blocks"][0]["fns"] == ["labs"]
       and r["extern_blocks"][0]["effects"] == [],
       "the C boundary is named: one extern (labs), declared pure")

att = {(a["effect"], a["module"]): a for a in r["attribution"]}
expect(set(att) == {("Clock", "examples/audit_libs/geo.hls"),
                    ("IO", "examples/audit_demo.hls"),
                    ("IO", "examples/audit_libs/netcheck.hls"),
                    ("Net", "examples/audit_libs/netcheck.hls")},
       "attribution covers every introduction point (IO twice)")
expect(att[("Clock", "examples/audit_libs/geo.hls")]["chain"] ==
       ["examples/audit_demo.hls", "examples/audit_libs/netcheck.hls",
        "examples/audit_libs/geo.hls"],
       "the Clock chain is drawn through the middle hop")
expect(att[("IO", "examples/audit_demo.hls")]["root_introduced"],
       "the entry's IO is marked root-introduced")

for needle, why in [
    ("modules: 4 (1 toolchain, 0 dependency, 3 workspace) — edges: 4 — "
     "extern decls: 1", "the module census line"),
    ("Clock — introduced by examples/audit_libs/geo.hls [workspace]",
     "the Clock attribution line"),
    ("IO — introduced by examples/audit_libs/netcheck.hls [workspace]",
     "the second IO introduction point"),
    ("via: examples/audit_demo.hls -> examples/audit_libs/netcheck.hls"
     " -> examples/audit_libs/geo.hls", "the three-hop chain line"),
    ("extern \"C\" { labs } declared (none)", "the extern surface line"),
    ("program effect surface: Clock, IO, Net", "the totals line"),
    ("verdict: report only", "the honest report-only verdict"),
]:
    expect(needle in out, why)

# ---------------------------------------------------------------------------
print("=== 2. the policy ===")
# ---------------------------------------------------------------------------

p = run(AUDIT + [DEMO, "--allow", "IO,Net"])
expect(p.returncode == 1, "Clock outside the allow list fails the run (exit 1)")
expect("Clock (introduced by examples/audit_libs/geo.hls)" in p.stdout
       and "via: examples/audit_demo.hls -> examples/audit_libs/netcheck.hls"
           " -> examples/audit_libs/geo.hls" in p.stdout,
       "the violation names the introducer and the chain")
expect("verdict: VIOLATED — 1 effect(s) outside the allow list" in p.stdout,
       "the violated verdict is printed")

p = run(AUDIT + [DEMO, "--allow", "Clock,IO,Net"])
expect(p.returncode == 0
       and "verdict: OK — the chain fits the allow list" in p.stdout,
       "the full surface allowed — clean (exit 0)")

# The gate covers the CHAIN, not the root: allow Clock+Net only. The
# entry's own IO is exempt; netcheck's IO is not.
p = run(AUDIT + [DEMO, "--allow", "Clock,Net"])
expect(p.returncode == 1
       and "IO (introduced by examples/audit_libs/netcheck.hls)" in p.stdout
       and "IO (introduced by examples/audit_demo.hls)" not in p.stdout,
       "the root's own IO is exempt; the chain's IO is not")

r2 = json.loads(run(AUDIT + [DEMO, "--allow", "IO,Net", "--json"]).stdout)
expect(r2["policy"]["allowed"] == ["IO", "Net"]
       and len(r2["violations"]) == 1
       and r2["violations"][0]["effect"] == "Clock",
       "--json violations are machine-exact")

p = run(AUDIT + [DEMO, "--allow", "IO,Bogus"])
expect(p.returncode == 2 and "unknown effect" in p.stderr,
       "an unknown effect in --allow is a usage error (exit 2)")

# ---------------------------------------------------------------------------
print("=== 3. fail-closed ===")
# ---------------------------------------------------------------------------

# Source mode: a broken tree is a broken build, not a finding.
scratch = os.path.join(TMP, "broken")
os.makedirs(scratch, exist_ok=True)
write(os.path.join(scratch, "lib.hls"),
      "fn helper( -> int {\n    return 1\n}\n")
write(os.path.join(scratch, "main.hls"),
      'import "lib.hls"\n\nfn main() -> int uses IO {\n'
      "    println(helper().to_str())\n    return 0\n}\n")
p = run(AUDIT + [os.path.join(scratch, "main.hls")])
expect(p.returncode == 1 and "parse error" in p.stderr,
       "a broken source tree exits 1 and names the failure")

# A missing import is a tree the tool cannot see.
write(os.path.join(scratch, "ghost.hls"),
      'import "no_such_module.hls"\n\nfn main() -> int { return 0 }\n')
p = run(AUDIT + [os.path.join(scratch, "ghost.hls")])
expect(p.returncode == 1 and "module not found" in p.stderr,
       "a missing module exits 1 (a tree we cannot see is a tree we "
       "cannot report)")

# Package mode: a broken PACKAGE is a fail-closed node, and the run
# still fails after the full report.
pkg = os.path.join(TMP, "fc_root", "dep")
write(os.path.join(pkg, "hls-pkg.toml"),
      '[package]\nname = "fc_dep"\nversion = "0.1.0"\n')
write(os.path.join(pkg, "main.hls"),
      "fn read_cfg( -> str {\n    return \"x\"\n}\n")
write(os.path.join(TMP, "fc_root", "hls-pkg.toml"),
      '[package]\nname = "fc_app"\nversion = "1.0.0"\n\n[dependencies]\n'
      'fc_dep = { path = "%s" }\n'
      % os.path.relpath(pkg, ROOT))
write(os.path.join(TMP, "fc_root", "main.hls"),
      'fn main() -> int uses IO {\n    println("x")\n    return 0\n}\n')
p = run(AUDIT + ["--pkg", os.path.join(TMP, "fc_root")])
expect(p.returncode == 1, "a broken package fails the run (exit 1)")
expect("UNAUDITABLE — fail closed" in p.stdout
       and "unauditable (fail closed):" in p.stdout,
       "the report names the unauditable package")
r3 = json.loads(run(AUDIT + ["--pkg", os.path.join(TMP, "fc_root"),
                             "--json"]).stdout)
fc = next(x for x in r3["packages"] if x["name"] == "fc_dep")
expect(fc["surface"] == ["Args", "Clock", "Conc", "Exit", "Fs", "IO",
                         "Net", "Proc", "Rand"],
       "the unauditable package's surface is the FULL effect set "
       "(fail closed, never pure)")

# A cycle is refused with the chain that closes it.
cyc = os.path.join(TMP, "cyc")
write(os.path.join(cyc, "a", "hls-pkg.toml"),
      '[package]\nname = "cyc_a"\nversion = "0.1.0"\n\n[dependencies]\n'
      'cyc_b = { path = "%s" }\n'
      % os.path.relpath(os.path.join(cyc, "b"), ROOT))
write(os.path.join(cyc, "a", "main.hls"), "fn fa() -> int { return 1 }\n")
write(os.path.join(cyc, "b", "hls-pkg.toml"),
      '[package]\nname = "cyc_b"\nversion = "0.1.0"\n\n[dependencies]\n'
      'cyc_a = { path = "%s" }\n'
      % os.path.relpath(os.path.join(cyc, "a"), ROOT))
write(os.path.join(cyc, "b", "main.hls"), "fn fb() -> int { return 2 }\n")
p = run(AUDIT + ["--pkg", os.path.join(cyc, "a")])
expect(p.returncode == 1
       and "circular package dependency: cyc_a -> cyc_b -> cyc_a" in p.stderr,
       "a cycle is refused with the closing chain")

# Usage errors: exit 2, never a traceback.
expect(run(AUDIT).returncode == 2, "no arguments — usage error (exit 2)")
expect(run(AUDIT + [DEMO, "--pkg", PKG_DEMO]).returncode == 2,
       "<entry> and --pkg together — usage error (exit 2)")
expect(run(AUDIT + ["--pkg", os.path.join(TMP, "no_such_dir")]).returncode == 1,
       "a missing package dir is an audit failure (exit 1)")

# ---------------------------------------------------------------------------
print("=== 4. the package tree ===")
# ---------------------------------------------------------------------------

p = run(AUDIT + ["--pkg", PKG_DEMO])
expect(p.returncode == 1, "the package demo violates its own policy (exit 1)")
out = p.stdout
for needle, why in [
    ("packages: 3 (root included) — deepest chain: 2 dep(s)",
     "the walk reached through audit_lib_a's OWN manifest"),
    ("pkg_audit_demo 0.1.0  [root] (this package)", "the tree root line"),
    ("audit_lib_a 0.1.0  [path]", "the middle package line"),
    ("audit_lib_b 0.1.0  [path]", "the DEEP package line"),
    ("Clock — introduced by audit_lib_b (depth 2)", "Clock at depth 2"),
    ("Fs — introduced by audit_lib_b (depth 2)", "Fs at depth 2"),
    ("via: pkg_audit_demo -> audit_lib_a -> audit_lib_b",
     "the two-hop chain"),
    ("policy: allowed = IO (from manifest)", "the manifest policy line"),
    ("verdict: VIOLATED — 2 effect(s) outside the allowed set",
     "the violated verdict"),
]:
    expect(needle in out, why)

p = run(AUDIT + ["--pkg", PKG_DEMO, "--allow", "IO,Fs,Clock"])
expect(p.returncode == 0
       and "verdict: OK — every package fits the allowed set" in p.stdout,
       "--allow widens the gate — clean (exit 0)")

rp = json.loads(run(AUDIT + ["--pkg", PKG_DEMO, "--json"]).stdout)
expect(rp["schema"] == "hls-audit/v1" and rp["mode"] == "package",
       "the JSON carries the schema and the mode")
expect(rp["policy"] == {"allowed": ["IO"], "source": "manifest"},
       "the manifest policy is parsed, not guessed")
expect([x["name"] for x in rp["packages"]] ==
       ["pkg_audit_demo", "audit_lib_a", "audit_lib_b"],
       "packages are listed root-first in walk order")
expect(rp["packages"][0]["depth"] == 0
       and rp["packages"][2]["depth"] == 2,
       "depths are the chain lengths from the root")
expect(len(rp["violations"]) == 2
       and all(v["package"] == "audit_lib_b" for v in rp["violations"]),
       "both violations come from the deep package")

# ---------------------------------------------------------------------------
print("=== 5. the drift ===")
# ---------------------------------------------------------------------------

drift_root = os.path.join(TMP, "drift_root")
dep_dir = os.path.join(TMP, "drift_dep")
write(os.path.join(dep_dir, "hls-pkg.toml"),
      '[package]\nname = "drift_dep"\nversion = "0.1.0"\n')
write(os.path.join(dep_dir, "main.hls"),
      'fn sz() -> int uses Fs {\n    return fs_size("a")\n}\n')
# A single-FILE path dep — the hls-pkg lock contract hashes the file,
# so the lock and the audit compare the same bytes.
write(os.path.join(drift_root, "hls-pkg.toml"),
      '[package]\nname = "drift_app"\nversion = "1.0.0"\n\n'
      '[dependencies]\ndrift_dep = { path = "%s" }\n\n'
      "[effects]\nallowed = [\"IO\", \"Fs\"]\n"
      % os.path.relpath(os.path.join(dep_dir, "main.hls"), ROOT))
write(os.path.join(drift_root, "main.hls"),
      'fn main() -> int uses IO {\n    println("x")\n    return 0\n}\n')
had_log = os.path.exists(LOG_MAIN)
had_lock_file = os.path.exists(LOG_LOCK)
lp = run(PKG + ["lock"], cwd=drift_root)
expect(lp.returncode == 0, "hls-pkg lock pins the drift fixture")

p = run(AUDIT + ["--pkg", drift_root])
expect(p.returncode == 0 and "drift:\n  (none)" in p.stdout,
       "a fresh lock audits clean (exit 0)")
expect("lockfile: present" in p.stdout, "the lockfile is reported present")

# The dep's content changes under the lock — DRIFT. The lock hashes
# the dep's main.hls (a single-file dep), so that is the file that
# must change.
with open(os.path.join(dep_dir, "main.hls"), "a") as f:
    f.write("\nfn helper() -> int uses Rand {\n"
            "    return rand_int(0, 9)\n}\n")
p = run(AUDIT + ["--pkg", drift_root])
expect(p.returncode == 1 and "locked sha256" in p.stdout
       and "re-lock" in p.stdout,
       "content changed under the lock — DRIFT fails the run")
r4 = json.loads(run(AUDIT + ["--pkg", drift_root, "--json"]).stdout)
expect(len(r4["drift"]) == 1 and r4["drift"][0]["package"] == "drift_dep",
       "the drift record names the package")

# A dep in the manifest but not in the lockfile is drift too. The
# manifest is REWRITTEN (appending after [effects] would bind the new
# dep to the wrong TOML section) with the same locked dep plus one
# the lock has never seen.
write(os.path.join(TMP, "drift_dep2", "hls-pkg.toml"),
      '[package]\nname = "drift_dep2"\nversion = "0.1.0"\n')
write(os.path.join(TMP, "drift_dep2", "main.hls"),
      "fn g() -> int { return 5 }\n")
write(os.path.join(drift_root, "hls-pkg.toml"),
      '[package]\nname = "drift_app"\nversion = "1.0.0"\n\n'
      '[dependencies]\n'
      'drift_dep = { path = "%s" }\n'
      'drift_dep2 = { path = "%s" }\n\n'
      "[effects]\nallowed = [\"IO\", \"Fs\"]\n"
      % (os.path.relpath(os.path.join(dep_dir, "main.hls"), ROOT),
         os.path.relpath(os.path.join(TMP, "drift_dep2"), ROOT)))
p = run(AUDIT + ["--pkg", drift_root])
expect(p.returncode == 1
       and "drift_dep2: in the manifest but not in the lockfile"
       in p.stdout,
       "an unlocked dep is drift")

# Gate scratch hygiene: the transparency log the lock wrote is not
# left behind for the commit.
for path, had in ((LOG_MAIN, had_log), (LOG_LOCK, had_lock_file)):
    if os.path.exists(path) and not had:
        try:
            os.unlink(path)
        except OSError:
            pass
cache_dir = os.path.join(ROOT, ".hls-pkg-cache")
if os.path.isdir(cache_dir) and not os.listdir(cache_dir):
    os.rmdir(cache_dir)

# ---------------------------------------------------------------------------
print("=== 6. the parity (the two modes agree on the root) ===")
# ---------------------------------------------------------------------------

src_root = json.loads(run(AUDIT + [os.path.join(PKG_DEMO, "main.hls"),
                                   "--json"]).stdout)
pkg_root = json.loads(run(AUDIT + ["--pkg", PKG_DEMO, "--json"]).stdout)
expect(src_root["mode"] == "source" and pkg_root["mode"] == "package",
       "both modes ran")
expect(src_root["modules"][0]["surface"] ==
       pkg_root["root"]["surface"] == ["IO"],
       "the root's surface agrees across the modes (IO)")

# ---------------------------------------------------------------------------
print("=== 7. the demos ===")
# ---------------------------------------------------------------------------

p1 = run([sys.executable, "boot/boot.py", DEMO])
expect(p1.returncode == 0 and "surface: IO declared here" in p1.stdout,
       "the demo runs (interpreter)")
p2 = run([sys.executable, "boot/boot.py", OK_TEST])
expect(p2.returncode == 0 and "checks passed: 4" in p2.stdout,
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

for f in (DEMO, OK_TEST, "examples/audit_libs/geo.hls",
          "examples/audit_libs/netcheck.hls"):
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

# The audit tool speaks the hls-pkg manifest dialect: parse_manifest
# round-trips the demo manifests.
sys.path.insert(0, os.path.join(ROOT, "tools", "hpkg_parts"))
from hpkg_manifest import parse_manifest                     # noqa: E402
mf = parse_manifest(os.path.join(PKG_DEMO, "hls-pkg.toml"))
expect(mf["package"]["name"] == "pkg_audit_demo"
       and mf["effects"]["allowed"] == ["IO"]
       and mf["dependencies"]["audit_lib_a"]["path"]
       == "examples/pkg_audit_libs/audit_lib_a",
       "the root manifest parses through hls-pkg's own reader")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 103 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
