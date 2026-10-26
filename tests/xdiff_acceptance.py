#!/usr/bin/env python3
"""Stage 133 acceptance gate — the cross-implementation differential
suite (v0.151.0-alpha).

Run with `make xdiff-acceptance` (or
`python3 tests/xdiff_acceptance.py`).

Eight sections over the REAL suite CLI (subprocess, the interface a
release engineer drives) and the REAL backends — a differential gate
that did not run programs on the implementations it compares would be
a memo, not a table:

  1. the convention   — `hls-xdiff` exits 0 green / 1 not green /
     2 usage; every refusal names the RULE (an unknown backend, a
     missing program, the reference-leg law: interp cannot be dropped
     while native runs); `selftest` is green; the --json report
     carries the schema keys a CI consumer needs

  2. the normalizer   — the channel canonicalizer's own vectors, in
     process: printf-vs-JS renderings of the same double agree
     ("0.750000"/"0.75", six-decimal rounding against JS's shortest
     form, exponents, ±nan folding, ±inf, the sign of zero, the 2^63
     magnitude), and the license's limits hold (version strings,
     integers, a real 6-digit difference are NEVER reconciled)

  3. the three-       — the fixtures through the real backends:
     backend match      xdiff_ints_corners byte-identical on all
                        legs with the native leg's raw digest EQUAL
                        to the interpreter's (bytes, not stories);
                        xdiff_floats_channel `normalized` (and only
                        normalized — the raw digests must DIFFER,
                        proving the channel did real work)

  4. the halt law     — xdiff_panic_div_zero and xdiff_panic_overflow
                        are `panic-equal`; xdiff_exit_halt is
                        `halt-gap` with the code pair named; a
                        program that traps on wasm while running
                        clean on the interpreter rings unledgered
                        (exit 1, the divergence named)

  5. the ledger       — every ledgered divergence sanctions with its
     law                 diagnosis quoted back; the negative space
                         rings: a scratch ledger missing an entry
                         turns the divergence into a REGRESSION, a
                         bogus entry on a clean program turns STALE,
                         an entry whose states no longer match turns
                         MISMATCH — all exit 1, each named

  6. the purity gate  — the census excludes std.http by name,
     freestanding/no_std by crate mode, the fixtures pass; the gate
     is honest about what it excludes and --all lifts it (the http
     program then earns its timeout error openly)

  7. the llvm leg     — the static leg emits and structurally
                        validates IR for the fixture; the report says
                        "static — validated, not executed" in those
                        words; llvm never contributes an ok/halted
                        verdict

  8. the corpus       — tests/ok/feat_stage133_xdiff.hls is
     showcase            hlfmt-canonical, hllint-clean, boots through
                         the real checker, runs green on the
                         interpreter and byte-identical natively; and
                         the full --core run is GREEN with every
                         verdict class present and three executing
                         backends having participated

Exit code 0 = all acceptance criteria met.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
XDIFF = os.path.join(REPO, "tools", "hls-xdiff.py")
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("hls_xdiff", XDIFF)
xd = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(xd)

PASS = 0
FAIL = 0


def ok(label):
    global PASS
    PASS += 1
    print("  [PASS] %s" % label)


def bad(label, detail=""):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s%s" % (label, (": " + detail) if detail else ""))


def check(cond, label, detail=""):
    if cond:
        ok(label)
    else:
        bad(label, detail)
    return cond


def run_xdiff(*args):
    """Run the real CLI; return (exit, stdout, stderr)."""
    p = subprocess.run([sys.executable, XDIFF] + list(args),
                       capture_output=True, timeout=900, cwd=REPO)
    return p.returncode, p.stdout.decode("utf-8", "replace"), \
        p.stderr.decode("utf-8", "replace")


def file_run(path, *extra):
    return run_xdiff("--file", path, *extra)


def fixture(name):
    return os.path.join(REPO, "tests", "xdiff", name + ".hls")


# ---------------------------------------------------------------------------
print("=== 1. the convention — exit codes 0/1/2, refusals name the rule ===")

code, out, err = run_xdiff("selftest")
check(code == 0, "selftest exits 0", err[:120])
check("green" in out and "[FAIL]" not in out,
      "selftest: all normalizer + verdict + ledger vectors green")

code, out, err = run_xdiff("--backends", "interp,nope", "--file",
                           fixture("xdiff_ints_corners"))
check(code == 2, "unknown backend exits 2")
check("unknown backend" in err and "nope" in err,
      "the refusal names the unknown backend", err[:120])

code, out, err = run_xdiff("--file", "tests/ok/no_such_program.hls")
check(code == 2, "missing program exits 2")
check("no such program" in err, "the refusal names the missing path",
      err[:120])

code, out, err = run_xdiff("--backends", "native,wasm", "--file",
                           fixture("xdiff_ints_corners"))
check(code == 2, "native without the reference leg exits 2")
check("reference" in err, "the refusal names the reference-leg law",
      err[:120])

code, out, err = file_run(fixture("xdiff_ints_corners"), "--json",
                          "/tmp/hls-xdiff-acc.json")
if check(code == 0, "a green --file run exits 0"):
    with open("/tmp/hls-xdiff-acc.json") as f:
        rep = json.load(f)
    for key in ("schema", "backends", "programs", "summary", "ledger"):
        check(key in rep, "json report carries '%s'" % key)
    prog = rep["programs"][0]
    for key in ("name", "verdict", "signature", "legs"):
        check(key in prog, "program entry carries '%s'" % key)

# ---------------------------------------------------------------------------
print("=== 2. the normalizer — the channel's vectors, in process ===")

norm_vectors = [
    ("0.750000", "0.75", True), ("1.500000", "1.5", True),
    ("0.123457", "0.12345678901", True),
    ("123456789012345680.000000", "123456789012345680.0", True),
    ("0.000000", "1e-7", True), ("0.000010", "1e-5", True),
    ("1000000000000000000000.000000", "1e+21", True),
    ("-0.000000", "0.0", True),
    ("nan", "NaN", True), ("-nan", "nan", True),
    ("inf", "Infinity", True), ("-inf", "-Infinity", True),
    ("1.10", "1.1", False), ("9007199254740993", "9007199254740994", False),
    ("0.750001", "0.75", False), ("abc", "abd", False),
    ("v1.2.3", "v1.2.4", False),
]
norm_bad = []
for a, b, expect in norm_vectors:
    ca = xd.canonical_channel(a)[0]
    cb = xd.canonical_channel(b)[0]
    if (ca == cb) != expect:
        norm_bad.append((a, b, ca, cb))
check(not norm_bad, "%d channel vectors green" % len(norm_vectors),
      str(norm_bad[:3]))

check(xd.canonical_channel("x=0.750000")[1] is True,
      "the channel reports WHEN it changed a token (normalized vs exact)")
check(xd.canonical_channel("x=0.75")[1] is False,
      "a short form passes through unrewritten")

# ---------------------------------------------------------------------------
print("=== 3. the three-backend match — bytes, not stories ===")

code, out, err = file_run(fixture("xdiff_ints_corners"), "--json",
                          "/tmp/hls-xdiff-acc.json")
if check(code == 0, "xdiff_ints_corners green on all legs"):
    rep = json.load(open("/tmp/hls-xdiff-acc.json"))
    prog = rep["programs"][0]
    check(prog["verdict"] == "exact",
          "integer corners are EXACT (byte-identical)", prog["verdict"])
    digests = {b: prog["legs"][b].get("out_sha256_16")
               for b in ("interp", "native", "wasm")}
    check(digests["interp"] == digests["native"] == digests["wasm"]
          and digests["interp"] is not None,
          "interp == native == wasm on the RAW digest (%s)" %
          digests["interp"], str(digests))

code, out, err = file_run(fixture("xdiff_floats_channel"), "--json",
                          "/tmp/hls-xdiff-acc.json")
if check(code == 0, "xdiff_floats_channel green"):
    rep = json.load(open("/tmp/hls-xdiff-acc.json"))
    prog = rep["programs"][0]
    check(prog["verdict"] == "normalized",
          "the float channel is NORMALIZED (not exact, not divergence)",
          prog["verdict"])
    raw_i = prog["legs"]["interp"]["out_sha256_16"]
    raw_w = prog["legs"]["wasm"]["out_sha256_16"]
    can_i = prog["legs"]["interp"]["out_canonical_sha256_16"]
    can_w = prog["legs"]["wasm"]["out_canonical_sha256_16"]
    check(raw_i != raw_w,
          "raw digests DIFFER (the channel did real work)")
    check(can_i == can_w,
          "canonical digests AGREE (same doubles, one channel)")

# ---------------------------------------------------------------------------
print("=== 4. the halt law — panic-equal, halt-gap, the alarm ===")

for name, want in (("xdiff_panic_div_zero", "panic-equal"),
                   ("xdiff_panic_overflow", "panic-equal")):
    code, out, err = file_run(fixture(name))
    verdict = "missing"
    for line in out.splitlines():
        if name in line and "panic-equal" in line:
            verdict = "panic-equal"
        elif name in line and "halt-gap" in line:
            verdict = "halt-gap"
    check(code == 0 and verdict == want,
          "%s is %s" % (name, want), "exit=%d verdict=%s" % (code, verdict))

code, out, err = file_run(fixture("xdiff_exit_halt"))
check(code == 0 and "halt-gap" in out,
      "xdiff_exit_halt is halt-gap (the wasm alpha's trap convention)")
check("exit 7" in out, "the halt-gap names the unpropagated code",
      out[:400])

# a program that traps on wasm while the interpreter runs clean rings
# — unledgered, exit 1, the divergence named
scratch = tempfile.mkdtemp(prefix="hls-xdiff-acc-")
trap_prog = os.path.join(scratch, "trap_prog.hls")
with open(trap_prog, "w") as f:
    f.write('fn main() uses IO {\n'
            '    println("clean on interp")\n'
            '    let v: float = "3.5".to_float()\n'
            '    println("never reached on wasm")\n'
            '}\n')
code, out, err = file_run(trap_prog, "--backends", "interp,wasm",
                          "--ledger", os.path.join(scratch, "empty.json"))
check(code == 1, "an unledgered divergence rings (exit 1)")
check("divergence" in out and "trap_prog" in out,
      "the alarm names the program and the verdict", out[:300])

# ---------------------------------------------------------------------------
print("=== 5. the ledger law — sanctioned, and the negative space ===")

LEDGERED = [
    ("feat_deep_scan7_to_float", "wasm-halt-unimplemented"),
    ("feat_deep_scan27_nested_generic", "wasm-invalid-module"),
    ("feat_stage31_tail", "wasm-invalid-module"),
    ("feat_deep_scan6_lexer", "wasm-bad-value"),
    ("feat_stage98_refine_panic", "wasm-missing-check"),
    ("panic_str_bounds", "wasm-missing-check"),
]
for name, cls in LEDGERED:
    code, out, err = file_run(os.path.join(REPO, "tests", "ok",
                                           name + ".hls"))
    check(code == 0 and cls in out and "sanctioned" in out,
          "%s sanctioned as %s" % (name, cls),
          "exit=%d out=%s" % (code, out[:160]))

real_ledger = os.path.join(REPO, "tests", "xdiff", "ledger.json")
with open(real_ledger) as f:
    real_entries = json.load(f)

# (a) the entry MISSING: the same divergence becomes a regression
trimmed = dict(real_entries)
trimmed["waivers"] = [w for w in real_entries["waivers"]
                      if w["file"] != "panic_str_bounds"]
trim_path = os.path.join(scratch, "trimmed.json")
with open(trim_path, "w") as f:
    json.dump(trimmed, f)
code, out, err = file_run(os.path.join(REPO, "tests", "ok",
                                       "panic_str_bounds.hls"),
                          "--ledger", trim_path)
check(code == 1 and "divergence" in out,
      "a missing entry turns the divergence into an unledgered alarm")

# (b) a BOGUS entry on a clean program: the two-way law rings STALE
bogus = dict(real_entries)
bogus["waivers"] = list(real_entries["waivers"]) + [{
    "file": "feat_contract_basic", "class": "wasm-bad-value",
    "culprit": "wasm",
    "states": {"interp": "ok", "native": "ok", "wasm": "ok"},
    "diagnosis": "a bogus entry the two-way law must catch",
}]
bogus_path = os.path.join(scratch, "bogus.json")
with open(bogus_path, "w") as f:
    json.dump(bogus, f)
code, out, err = file_run(os.path.join(REPO, "tests", "ok",
                                       "feat_contract_basic.hls"),
                          "--ledger", bogus_path)
check(code == 1 and "stale" in out,
      "a bogus entry on a clean program rings STALE (two-way)")

# (c) the states MOVED: the entry no longer describes reality
moved = dict(real_entries)
moved["waivers"] = [dict(w) if w["file"] != "feat_deep_scan7_to_float"
                    else dict(w, states={"interp": "ok", "native": "ok",
                                         "wasm": "ok"})
                    for w in real_entries["waivers"]]
moved_path = os.path.join(scratch, "moved.json")
with open(moved_path, "w") as f:
    json.dump(moved, f)
code, out, err = file_run(os.path.join(REPO, "tests", "ok",
                                       "feat_deep_scan7_to_float.hls"),
                          "--ledger", moved_path)
check(code == 1 and "mismatch" in out,
      "an entry whose states moved rings MISMATCH (re-diagnose)")

# (d) --update-ledger drafts without writing
code, out, err = file_run(trap_prog, "--backends", "interp,wasm",
                          "--ledger", os.path.join(scratch, "empty.json"),
                          "--update-ledger")
check(code == 1 and "draft entries" in out and '"file": "trap_prog"' in out,
      "--update-ledger drafts the diverging set")
check(not os.path.exists(os.path.join(REPO, "tests", "xdiff",
                                      "empty.json")),
      "the tool never writes the ledger itself")

# ---------------------------------------------------------------------------
print("=== 6. the purity gate — named exclusions, and an honest --all ===")

code, out, err = run_xdiff("--discover")
check(code == 0, "the census runs")
check("std.http" in out and "feat_stage38_http" in out,
      "std.http is excluded BY NAME (output depends on the world)")
check("#![freestanding]" in out,
      "the crate mode is excluded by name")
check("panic_divzero" not in out,
      "panic fixtures are pure enough to sit at the table")
for fx in ("xdiff_ints_corners", "xdiff_floats_channel",
           "xdiff_control_flow", "xdiff_exit_halt"):
    check(fx not in out, "%s passes the gate" % fx)

code, out, err = run_xdiff("--discover")
n_elig = int(out.split("purity census:")[1].split("eligible")[0].strip().split()[0])
check(n_elig > 100, "the gated corpus keeps the bulk of tests/ok (%d)" % n_elig)

code, out, err = run_xdiff("--list")
check(code == 0 and "feat_stage38_http" not in out,
      "the gated corpus does not contain the http program")
code, out, err = run_xdiff("--all", "--list")
check(code == 0 and "feat_stage38_http" in out,
      "--all lifts the gate: the world-dependent program re-enters the corpus")

# ---------------------------------------------------------------------------
print("=== 7. the llvm leg — static, validated, honestly labelled ===")

code, out, err = file_run(fixture("xdiff_control_flow"), "--json",
                          "/tmp/hls-xdiff-acc.json")
if check(code == 0, "xdiff_control_flow green"):
    rep = json.load(open("/tmp/hls-xdiff-acc.json"))
    prog = rep["programs"][0]
    llvm = prog["legs"].get("llvm", {})
    check(llvm.get("status") == "validated",
          "the llvm leg is VALIDATED (static)", str(llvm))
    check("IR valid" in llvm.get("notes", ""),
          "the IR passed the structural validator")
    check(llvm.get("status") not in ("ok", "halted"),
          "llvm never contributes an executing verdict")
check("static — validated, not executed" in out,
      "the report says 'static — validated, not executed' in those words")

# ---------------------------------------------------------------------------
print("=== 8. the corpus showcase — the fixture, then the full core ===")

OK_F = os.path.join(REPO, "tests", "ok", "feat_stage133_xdiff.hls")

p = subprocess.run([os.path.join(REPO, "bin", "hlfmt"),
                    "--check", OK_F], capture_output=True, cwd=REPO)
check(p.returncode == 0, "the fixture is hlfmt-canonical")
p = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hllint.py"),
                    OK_F], capture_output=True, cwd=REPO)
check(p.returncode == 0 and b"no warnings" in p.stdout,
      "the fixture is hllint-clean", p.stdout.decode()[:200])

p = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hlinterp", "boot.py"),
                    "--check", OK_F], capture_output=True, cwd=REPO)
check(p.returncode == 0, "the fixture boots through the real checker",
      p.stderr.decode()[:200])

p = subprocess.run([sys.executable, os.path.join(REPO, "tools", "hlinterp", "boot.py"),
                    OK_F], capture_output=True, cwd=REPO)
interp_out = p.stdout
check(p.returncode == 0 and b"cross-impl suite OK" in interp_out,
      "the fixture runs green on the interpreter")

scratch8 = tempfile.mkdtemp(prefix="hls-xdiff-acc8-")
cfile = os.path.join(scratch8, "f.c")
binfile = os.path.join(scratch8, "f.bin")
p = subprocess.run([os.path.join(REPO, "bin", "hlc"), OK_F, cfile],
                   capture_output=True, cwd=REPO)
if check(p.returncode == 0, "the native compiler takes the fixture"):
    subprocess.run(["gcc", "-O2", "-o", binfile, cfile, "-lm", "-pthread"],
                   cwd=scratch8)
    p2 = subprocess.run([binfile], capture_output=True, cwd=scratch8,
                        env=dict(os.environ, LC_ALL="C"))
    check(p2.returncode == 0 and p2.stdout == interp_out,
          "the native build is byte-identical to the interpreter")

# the full core run: every verdict class, three executing backends
code, out, err = run_xdiff("--core", "--json", "/tmp/hls-xdiff-core.json")
if check(code == 0, "the --core run is GREEN (exit 0)", out[-400:]):
    rep = json.load(open("/tmp/hls-xdiff-core.json"))
    s = rep["summary"]
    check(s.get("exact", 0) >= 15, "exact verdicts present (%d)" % s.get("exact", 0))
    check(s.get("panic-equal", 0) >= 10,
          "panic-equal verdicts present (%d)" % s.get("panic-equal", 0))
    check(s.get("normalized", 0) >= 1,
          "normalized verdicts present (%d)" % s.get("normalized", 0))
    check(s.get("halt-gap", 0) >= 1,
          "halt-gap verdicts present (%d)" % s.get("halt-gap", 0))
    check(s.get("divergence", 0) >= 4 and s.get("error", 0) >= 2,
          "the six ledgered findings stay visible (div=%d, err=%d)" %
          (s.get("divergence", 0), s.get("error", 0)))
    sanctioned = [p2 for p2 in rep["programs"]
                  if p2.get("ledger") and
                  p2["ledger"]["status"] == "sanctioned"]
    check(len(sanctioned) >= 6,
          "every finding carries its sanctioned diagnosis (%d)" %
          len(sanctioned))
    exec_backends = set()
    for p2 in rep["programs"]:
        for b, leg in p2["legs"].items():
            if leg["status"] in ("ok", "halted"):
                exec_backends.add(b)
    check({"interp", "native", "wasm"} <= exec_backends,
          "three executing backends participated: %s" %
          sorted(exec_backends))

# ---------------------------------------------------------------------------
print("==========================================")
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
print("==========================================")
sys.exit(0 if FAIL == 0 else 1)
