#!/usr/bin/env python3
"""Stage 105 acceptance gate — hls-repro, the reproducible-build
verification across distros.

Run with `make repro-acceptance` (or
`python3 tests/repro_acceptance.py`).

Nine sections:

  1. the recipe     — the demo built twice in two isolated roots
                      under two profiles (locale, TZ, umask, hash
                      seed, root-path shape): byte-identical C,
                      byte-identical binary, the buildinfo pinned
                      (schema, engine, epoch policy, the matrix
                      recorded, the smoke consistent, every hash
                      over the real bytes)
  2. the diagnoser  — the STT_FILE lesson: the same C compiled under
                      two intermediate names differs in exactly one
                      byte, and diagnose() names the leak; a C with
                      a timestamp macro is named too
  3. the epoch      — --epoch wins, SOURCE_DATE_EPOCH next, the
                      tree digest derives otherwise (changed source,
                      changed epoch); the SBOM bridge: with the
                      repro epoch pinned, the Stage 104 documents
                      rebuild byte for byte
  4. the matrix     — three builds, three profiles, one verdict;
                      the staged root holds exactly the audit's
                      module set (hermetic, decoys excluded)
  5. the drift      — a detached fixture: a changed module moves the
                      tree digest and --verify refuses BEFORE
                      building; an engine mismatch refuses
  6. the verify     — a fresh verify returns the bytes (exit 0); a
                      tampered outputs hash refuses (exit 1)
  7. the package    — no lockfile, no release; a fresh lock releases
                      (versioned buildinfo, one "repro" record
                      chained, the chain still verifies); content
                      changed under the lock refuses WITHOUT
                      touching the log
  8. the demos      — the demo and the ok-test run; interpreter and
                      native agree byte for byte (native half only
                      when bin/hlc exists — the gate stays hermetic)
  9. the tools      — hlfmt stable, hllint clean on the new entry
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
sys.path.insert(0, os.path.join(ROOT, "tools"))

REPRO = [sys.executable, os.path.join(ROOT, "tools", "hls-repro.py")]
SBOM = [sys.executable, os.path.join(ROOT, "tools", "hls-sbom.py")]
PKG = [sys.executable, os.path.join(ROOT, "tools", "hls-pkg.py")]
DEMO = "examples/repro_demo.hls"
OK_TEST = "tests/ok/feat_stage105_repro.hls"

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s105_", dir=os.path.join(ROOT, "tests"))
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


def repro_json(*extra):
    p = run(REPRO + list(extra) + ["--json"])
    return p, (json.loads(p.stdout) if p.returncode == 0 and p.stdout
               else None)


# The engine the gate builds with: native hlc when it exists (fast),
# the boot chain otherwise — and every engine-conditional assertion
# below keys off this one variable.
sys.path.insert(0, os.path.join(ROOT, "tools"))
import importlib.util as _ilu                                     # noqa: E402
_spec = _ilu.spec_from_file_location(
    "hls_repro_mod", os.path.join(ROOT, "tools", "hls-repro.py"))
hls_repro = _ilu.module_from_spec(_spec)
sys.modules["hls_repro_mod"] = hls_repro
_spec.loader.exec_module(hls_repro)
ENGINE = hls_repro._engine_resolve("auto")

# ---------------------------------------------------------------------------
print("=== 1. the recipe (two profiles, one verdict) ===")
# ---------------------------------------------------------------------------

p, bi = repro_json(DEMO, "--builds", "2", "--engine", ENGINE)
expect(p.returncode == 0, "the demo reproduces across two profiles "
       "(exit 0)")
expect(bi["schema"] == "hls-buildinfo/v1" and bi["tool"] == "hls-repro"
       and bi["tool_version"] == "0.124.0-alpha",
       "the buildinfo names the schema, the tool and the release")
expect(bi["mode"] == "source" and bi["engine"] == ENGINE
       and bi["tree"]["entry"] == DEMO and bi["tree"]["layout"] == "repo",
       "source mode, repo layout, the entry named as the audit keys it")
expect(bi["epoch"]["source"] == "derived" and 0 <= bi["epoch"]["value"]
       < 2 ** 31,
       "the epoch is derived (content, not clock) and in the sane range")
expect(len(bi["matrix"]["profiles"]) == 2
       and bi["matrix"]["profiles"][0]["name"] == "baseline"
       and bi["matrix"]["profiles"][1]["name"] == "glibc-debian",
       "the matrix records the profiles it exercised")
expect(bi["matrix"]["profiles"][1]["env"]["TZ"] == "Europe/Berlin"
       and bi["matrix"]["profiles"][1]["env"]["PYTHONHASHSEED"] == "1",
       "the profile deltas ride along (the claim is scoped, not vague)")
c_set = {b["c_sha256"] for b in bi["builds"]}
bin_set = {b["binary_sha256"] for b in bi["builds"]}
expect(len(c_set) == 1 and len(bin_set) == 1,
       "byte-identical C and binary across the roots")
expect(bi["outputs"]["c_sha256"] in c_set
       and bi["outputs"]["binary_sha256"] in bin_set,
       "the outputs section repeats the per-build hashes")
expect(bi["smoke"]["status"] == "consistent",
       "the smoke is consistent: same exit, same stdout everywhere")
expect(bi["toolchain"]["hlc_fingerprint_sha256"]
       == sha256_of("src/hlc.hls")
       and "cc" in bi["toolchain"] and "python" in bi["toolchain"],
       "the toolchain section names hlc, cc and python")
expect(all(b["binary_size"] == bi["outputs"]["binary_size"]
           for b in bi["builds"]),
       "the binary size agrees with the hashes")

# Text mode writes the buildinfo next to the entry; --out moves it.
p = run(REPRO + [DEMO, "--builds", "2", "--engine", ENGINE,
                 "--out", os.path.join(TMP, "out")])
expect(p.returncode == 0
       and os.path.isfile(os.path.join(TMP, "out",
                                       "hls-repro.buildinfo.json")),
       "text mode writes the buildinfo (--out honoured)")
written = json.load(open(os.path.join(TMP, "out",
                                      "hls-repro.buildinfo.json")))
expect(written == bi, "the written buildinfo equals the --json report")

# ---------------------------------------------------------------------------
print("=== 2. the diagnoser (the STT_FILE lesson) ===")
# ---------------------------------------------------------------------------

diag_dir = os.path.join(TMP, "diag")
os.makedirs(os.path.join(diag_dir, "one"), exist_ok=True)
os.makedirs(os.path.join(diag_dir, "two"), exist_ok=True)
tiny_c = ("int hl_probe(void) { return 42; }\n"
          "int main(void) { return hl_probe(); }\n")
# Same length, one letter apart: the exact shape of the one-byte
# STT_FILE divergence — the symbol table records the input NAME.
for sub, cname in (("one", "a.c"), ("two", "b.c")):
    with open(os.path.join(diag_dir, sub, cname), "w") as f:
        f.write(tiny_c)
    cc = run(["cc", "-O2", "-o", "hl_repro", cname, "-lm", "-pthread"],
             cwd=os.path.join(diag_dir, sub))
    if cc.returncode != 0:
        bad("the diagnoser fixture failed to compile (%s)" % sub)
        break
else:
    # Byte-identical C, different intermediate NAMES of the same
    # length: the binaries differ in exactly one symbol-table byte
    # — the leak class the recipe's canonical name exists to kill.
    one = os.path.join(diag_dir, "one")
    two = os.path.join(diag_dir, "two")
    expect(sha256_of(os.path.join(one, "a.c"))
           == sha256_of(os.path.join(two, "b.c")),
           "the fixture's two C files are byte-identical")
    expect(sha256_of(os.path.join(one, "hl_repro"))
           != sha256_of(os.path.join(two, "hl_repro")),
           "the binaries differ because the intermediate NAME differs")
    findings = hls_repro.diagnose(
        [{"c_sha256": "x", "binary_sha256": "y"},
         {"c_sha256": "x", "binary_sha256": "z"}],
        [os.path.join(one, "a.c"), os.path.join(two, "b.c")],
        [os.path.join(one, "hl_repro"), os.path.join(two, "hl_repro")],
        [one, two])
    joined = " | ".join(findings)
    expect(any("STT_FILE" in f or "FILE NAME" in f for f in findings),
           "the diagnoser names the symbol-table signature: %s"
           % joined)
    expect(any("differ in 1 byte" in f for f in findings),
           "the diagnoser counts the one differing byte")

    # A C that reads the wall clock cannot reproduce: named.
    clock_c = write(os.path.join(diag_dir, "clocky.c"),
                    "const char *born = __DATE__ \" \" __TIME__;\n"
                    "int main(void) { return born[0] ? 0 : 1; }\n")
    findings2 = hls_repro.diagnose(
        [{"c_sha256": "a", "binary_sha256": "a"},
         {"c_sha256": "b", "binary_sha256": "b"}],
        [clock_c, clock_c], ["", ""], ["", ""])
    expect(any("__DATE__" in f for f in findings2),
           "the diagnoser names the timestamp macros in the C")

# ---------------------------------------------------------------------------
print("=== 3. the epoch (content, not clock) ===")
# ---------------------------------------------------------------------------

p, e1 = repro_json(DEMO, "--builds", "2", "--engine", ENGINE,
                   "--epoch", "1727800000")
expect(p.returncode == 0 and e1["epoch"]["source"] == "--epoch"
       and e1["epoch"]["value"] == 1727800000,
       "--epoch wins and is recorded as the source")
env = dict(os.environ, SOURCE_DATE_EPOCH="1234567890")
p = run(REPRO + [DEMO, "--builds", "2", "--engine", ENGINE, "--json"],
        env=env)
e2 = json.loads(p.stdout)
expect(e2["epoch"]["source"] == "SOURCE_DATE_EPOCH"
       and e2["epoch"]["value"] == 1234567890,
       "the environment's SOURCE_DATE_EPOCH is honoured next")
p, e3 = repro_json(DEMO, "--builds", "2", "--engine", ENGINE)
derived = e3["epoch"]["value"]
expect(e3["epoch"]["source"] == "derived" and 0 <= derived < 2 ** 31,
       "no epoch named — the tree digest derives one")

# One flipped source byte moves the tree digest and the derived
# epoch with it.
scratch = os.path.join(TMP, "epoch")
os.makedirs(scratch, exist_ok=True)
demo_copy = write(os.path.join(scratch, "repro_demo.hls"),
                  open(DEMO).read())
p, e4 = repro_json(demo_copy, "--builds", "2", "--engine", ENGINE,
                   "--scratch", os.path.join(TMP, "epoch_roots"))
with open(demo_copy, "a") as f:
    f.write("\n# one byte more\n")
p, e5 = repro_json(demo_copy, "--builds", "2", "--engine", ENGINE,
                   "--scratch", os.path.join(TMP, "epoch_roots"))
expect(e4["epoch"]["value"] != e5["epoch"]["value"]
       and e4["tree"]["digest"] != e5["tree"]["digest"],
       "one flipped byte moves the tree digest and the derived epoch")

# The Stage 104 bridge: with the repro epoch pinned, the SBOM
# documents rebuild byte for byte — a release's SBOM can be
# REBUILT, not just re-emitted.
senv = dict(os.environ, SOURCE_DATE_EPOCH=str(derived))
sa = run(SBOM + [DEMO, "--format", "cdx", "--stdout"], env=senv)
sb = run(SBOM + [DEMO, "--format", "cdx", "--stdout"], env=senv)
expect(sa.returncode == 0 and sa.stdout == sb.stdout
       and sb.stdout.strip().startswith("{"),
       "the SBOM bridge: byte-identical documents under the repro epoch")

# ---------------------------------------------------------------------------
print("=== 4. the matrix (three builds, one verdict) ===")
# ---------------------------------------------------------------------------

p, m3 = repro_json(DEMO, "--builds", "3", "--engine", ENGINE)
expect(p.returncode == 0, "three builds agree (exit 0)")
prof_names = [x["name"] for x in m3["matrix"]["profiles"]]
expect(prof_names == ["baseline", "glibc-debian", "musl-alpine"],
       "the rotation walks the profile list in order: %s" % prof_names)
expect(len({x["umask"] for x in m3["matrix"]["profiles"]}) >= 2,
       "the umask varies across the matrix (and never matters)")
expect(len({b["binary_sha256"] for b in m3["builds"]}) == 1,
       "three environments, one binary hash")

# The staging is hermetic: a build root holds exactly the audit's
# module set — the demo tree is the entry + std.str, nothing else.
stage_root = os.path.join(TMP, "stage")
os.makedirs(stage_root)
tree = hls_repro.load_source_tree(os.path.join(ROOT, DEMO))
staged_entry = hls_repro.stage_source(stage_root, tree)
staged_files = set()
for dirpath, _, filenames in os.walk(stage_root):
    for fn in filenames:
        staged_files.add(
            os.path.relpath(os.path.join(dirpath, fn), stage_root)
            .replace(os.sep, "/"))
audit_files = {tree["rel"][m["key"]] for m in tree["report"]["modules"]}
expect(staged_files == audit_files,
       "the staged root is exactly the audit's module set")
expect(staged_entry == DEMO,
       "the entry stages at its own key")

# ---------------------------------------------------------------------------
print("=== 5. the drift (refuses before it builds) ===")
# ---------------------------------------------------------------------------

# The detached fixture MUST live outside the repo — a tree inside
# tests/ would be keyed repo-relative and stage as 'repo' layout.
det = tempfile.mkdtemp(prefix="_gate_s105_det_")
os.makedirs(os.path.join(det, "lib"), exist_ok=True)
write(os.path.join(det, "lib", "helper.hls"),
      "fn twice(x: int) -> int {\n    return x * 2\n}\n")
det_entry = write(os.path.join(det, "myprog.hls"),
                  'import "lib/helper.hls"\n\n'
                  'fn main() -> int uses IO {\n'
                  '    println("detached " + twice(21).to_str())\n'
                  '    return 0\n}\n')
p, dbi = repro_json(det_entry, "--builds", "2", "--engine", ENGINE)
expect(p.returncode == 0 and dbi["tree"]["layout"] == "detached"
       and dbi["tree"]["entry"] == "myprog.hls",
       "a detached tree stages relative to the entry and says so")
det_bi = os.path.join(det, "hls-repro.buildinfo.json")
p = run(REPRO + [det_entry, "--builds", "2", "--engine", ENGINE])
expect(p.returncode == 0 and os.path.isfile(det_bi),
       "the detached run writes its buildinfo next to the entry")

with open(os.path.join(det, "lib", "helper.hls"), "a") as f:
    f.write("\nfn extra() -> int {\n    return 7\n}\n")
p = run(REPRO + ["--verify", det_bi, "--in", det])
out = p.stderr
expect(p.returncode == 1 and "input drift" in out
       and "digest" in out,
       "a changed module refuses the verify BEFORE anything is built")
expect("build roots are kept" not in out,
       "the refusal is a refusal, not a mismatch (no evidence dir)")

# Restore the fixture tree and verify the happy path in section 6.
with open(os.path.join(det, "lib", "helper.hls"), "w") as f:
    f.write("fn twice(x: int) -> int {\n    return x * 2\n}\n")

# ---------------------------------------------------------------------------
print("=== 6. the verify (the cross-environment answer) ===")
# ---------------------------------------------------------------------------

p = run(REPRO + ["--verify", det_bi, "--in", det])
expect(p.returncode == 0 and "verified" in p.stdout,
       "a fresh verify reproduces the bytes (exit 0)")

if ENGINE == "native":
    p = run(REPRO + ["--verify", det_bi, "--in", det, "--engine", "boot"])
    expect(p.returncode == 1 and "engine mismatch" in p.stderr,
           "an engine mismatch refuses (fail-closed)")
else:
    print("  [note] engine is boot — the native-mismatch refusal "
          "needs bin/hlc (skipped)")
good = json.load(open(det_bi))
tampered = write(os.path.join(TMP, "tampered.buildinfo.json"),
                 json.dumps(good).replace(
                     good["outputs"]["binary_sha256"], "0" * 64))
p = run(REPRO + ["--verify", tampered, "--in", det])
expect(p.returncode == 1 and "did not reproduce" in p.stderr,
       "a tampered outputs hash refuses (exit 1)")
badjson = write(os.path.join(TMP, "notabi.json"),
                '{"schema": "some-other/v9"}')
p = run(REPRO + ["--verify", badjson])
expect(p.returncode == 1 and "not a" in p.stderr,
       "a foreign document refuses outright")

# ---------------------------------------------------------------------------
print("=== 7. the package (the release gate) ===")
# ---------------------------------------------------------------------------

rel_root = os.path.join(TMP, "rel_app")
dep_dir = os.path.join(ROOT, "tests", "_gate_s105_dep")
write(os.path.join(dep_dir, "hls-pkg.toml"),
      '[package]\nname = "rel_dep"\nversion = "0.1.0"\n')
dep_file = write(os.path.join(dep_dir, "main.hls"),
                 'fn sz() -> int uses Fs {\n    return fs_size("a")\n}\n')
write(os.path.join(rel_root, "hls-pkg.toml"),
      '[package]\nname = "rel_app"\nversion = "2.3.4"\n\n'
      '[dependencies]\nrel_dep = { path = "tests/_gate_s105_dep/main.hls" }\n\n'
      "[effects]\nallowed = [\"IO\", \"Fs\"]\n")
write(os.path.join(rel_root, "main.hls"),
      'fn main() -> int uses IO {\n    println("x")\n    return 0\n}\n')

had_log = os.path.exists(LOG_MAIN)
had_lock_file = os.path.exists(LOG_LOCK)

# No lockfile, no release.
p = run(REPRO + ["--pkg", rel_root, "--release", "--engine", ENGINE])
expect(p.returncode == 1 and "no lockfile" in p.stderr
       and "hls-pkg lock" in p.stderr,
       "--release without a lockfile refuses (exit 1, says what to do)")
expect(not os.path.exists(os.path.join(
           rel_root, "hls-repro-rel_app-2.3.4.buildinfo.json")),
       "the refused release wrote nothing")

# A fresh lock releases: the versioned buildinfo + a chained record.
lp = run(PKG + ["lock"], cwd=rel_root)
expect(lp.returncode == 0, "hls-pkg lock pins the release fixture")
log_lines_before = 0
if os.path.exists(LOG_MAIN):
    with open(LOG_MAIN, "rb") as f:
        log_lines_before = len(f.readlines())

p = run(REPRO + ["--pkg", rel_root, "--release", "--engine", ENGINE])
expect(p.returncode == 0, "the locked tree releases (exit 0)")
bi_out = os.path.join(rel_root,
                      "hls-repro-rel_app-2.3.4.buildinfo.json")
expect(os.path.isfile(bi_out),
       "the release writes hls-repro-<name>-<version>.buildinfo.json")
rel_bi = json.load(open(bi_out))
expect(rel_bi["mode"] == "package" and rel_bi["tree"]["name"] == "rel_app"
       and rel_bi["tree"]["version"] == "2.3.4"
       and rel_bi["tree"]["entry"] == "main.hls",
       "the package buildinfo names the package and its entry")
expect(any(k.startswith("dep:") for k in
           [i["key"] for i in rel_bi["tree"]["inputs"]]),
       "the dep units are inputs, hashed as the lock hashes them")
expect(rel_bi["smoke"]["status"] == "consistent",
       "the packaged artifact smokes consistently too")
bi_sha = sha256_of(bi_out)

with open(LOG_MAIN, "rb") as f:
    lines = f.readlines()
expect(len(lines) == log_lines_before + 1,
       "one record appended to the transparency log")
rec = json.loads(lines[-1].decode("utf-8"))
expect(rec.get("kind") == "repro" and rec.get("name") == "rel_app"
       and rec.get("version") == "2.3.4"
       and rec.get("c_sha256") == rel_bi["outputs"]["c_sha256"]
       and rec.get("binary_sha256") == rel_bi["outputs"]["binary_sha256"]
       and rec.get("buildinfo_sha256") == bi_sha,
       "the record chains the tree, C, binary and buildinfo hashes")
vp = run(PKG + ["log", "--verify"])
expect(vp.returncode == 0, "the transparency chain still verifies")

# Content changed under the lock — drift refuses, the log is
# untouched.
with open(dep_file, "a") as f:
    f.write("\nfn helper() -> int uses Rand {\n"
            "    return rand_int(9)\n}\n")
p = run(REPRO + ["--pkg", rel_root, "--release", "--engine", ENGINE])
with open(LOG_MAIN, "rb") as f:
    lines_after = len(f.readlines())
expect(p.returncode == 1 and "drift" in p.stderr,
       "content changed under the lock — the release refuses (exit 1)")
expect(lines_after == log_lines_before + 1,
       "the refused release appended NOTHING to the log")

# Gate scratch hygiene: the log the lock wrote is not left behind
# for the commit.
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
shutil.rmtree(os.path.join(ROOT, "tests", "_gate_s105_dep"),
              ignore_errors=True)
shutil.rmtree(det, ignore_errors=True)

# ---------------------------------------------------------------------------
print("=== 8. the demos ===")
# ---------------------------------------------------------------------------

p1 = run([sys.executable, "boot/boot.py", DEMO])
expect(p1.returncode == 0 and "checks passed: 4" in p1.stdout,
       "the demo runs (interpreter)")
p2 = run([sys.executable, "boot/boot.py", OK_TEST])
expect(p2.returncode == 0 and "checks passed: 5" in p2.stdout,
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
    print("  [note] bin/hlc not built — the native half of section 8 "
          "is skipped (the gate stays hermetic)")

# ---------------------------------------------------------------------------
print("=== 9. the tools ===")
# ---------------------------------------------------------------------------

for f in (DEMO, OK_TEST):
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

lints = [run([sys.executable, "tools/hllint.py", f]) for f in (DEMO, OK_TEST)]
if all(l.returncode == 0 for l in lints):
    ok("hllint: no findings on the demo or the ok-test")
else:
    bad("hllint: findings on the new sources")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 105 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
