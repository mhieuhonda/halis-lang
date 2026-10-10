#!/usr/bin/env python3
"""Stage 127 acceptance gate — hls-det, the determinism verifier.

Run with `make det-acceptance` (or `python3 tests/det_acceptance.py`).

Eight sections over the REAL verifier CLI (subprocess, the same
interface a user drives), the REAL runtimes (the emitted C's det
region, the Stage-0 interpreter's DetCore), and the verifier's own
trace validator (imported for the protocol's unit checks):

  1. the convention  — usage gates are exit 2 (missing file, non-.hls,
     & the refusals    --runs < 2, --timeout < 1); the flags compose

  2. the policy      — the interleaving traces of three fixtures,
     hand-computed    HAND-COMPUTED from the section-67 policy and
                       asserted line-by-line on BOTH backends (the
                       rotation, the park/wake order, the bounded
                       backpressure with the re-park)

  3. the runtime     — the emitted hosted C carries the region and the
     wiring           arm call; a --no-libc emission carries the stub
                       macros and none of the mode; an unarmed binary
                       behaves exactly as before (the ok corpus's
                       showcase: unarmed native == unarmed interpreter)

  4. the             — every det_libs fixture certifies through the
     certification    REAL CLI (exit 0, parity checked, --json parses
                       and carries the config, the runs and the stats);
                       argv reaches the program; --runs 8 stays stable

  5. the fairness    — the recv_or poll loop terminates under the
     matrix           rotation with the exact poll count the policy
                       predicts; the hltest --det witness passes both
                       armed and unarmed

  6. the deadlock    — the all-blocked moment: both schedulers, both
     & panic parity   backends, both modes — exit 101, the canonical
                       Stage 16 message, empty stdout; the tool
                       certifies the deterministic death (exit 101,
                       said so)

  7. the honesty     — a clock-reading program is NOT DETERMINISTIC
     gates            (the comparison is real); a forged step line is
                       a loud protocol error; the validator's own unit
                       checks (malformed, non-advancing, unknown op,
                       look-alike, well-formed)

  8. the differential— every new .hls file is hlfmt-canonical,
     & the corpus     hllint-clean and boots through the real checker;
     hygiene          the interpreter-vs-native det trace equality over
                       the whole fixture set; no gate litter survives

Exit code 0 = all acceptance criteria met.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

DET = [sys.executable, "tools/hls-det.py"]
BOOT = [sys.executable, "boot/boot.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage127_det.hls")
PANIC_FIXTURE = os.path.join(ROOT, "tests", "ok", "panic_stage127_det.hls")
LIBS = os.path.join(ROOT, "tests", "det_libs")
ROTATION = os.path.join(LIBS, "rotation.hls")
PARK_WAKE = os.path.join(LIBS, "park_wake.hls")
BOUNDED = os.path.join(LIBS, "bounded_wake.hls")
SELECT_ROT = os.path.join(LIBS, "select_rotation.hls")
FAIRNESS = os.path.join(LIBS, "fairness_poll.hls")
DEADLOCK = os.path.join(LIBS, "deadlock.hls")
NONDET = os.path.join(LIBS, "nondet_source.hls")
LOOKALIKE = os.path.join(LIBS, "lookalike.hls")
ROTATION_TEST = os.path.join(LIBS, "rotation_test.hls")
ALL_LIBS = [ROTATION, PARK_WAKE, BOUNDED, SELECT_ROT, FAIRNESS,
            DEADLOCK, ROTATION_TEST, SHOWCASE]

# The section-67 policy, derived by hand. Each fixture's trace is the
# rotation made visible: the FIFO queue, the wakee ahead of the waker,
# the child's first turn before the parent's next.
EXPECTED_TRACES = {
    ROTATION: [
        "__HLDET_STEP__ 0 t0 spawn t1",
        "__HLDET_STEP__ 1 t1 recv ch0",
        "__HLDET_STEP__ 2 t0 send ch0",
        "__HLDET_STEP__ 3 t0 send ch0",
        "__HLDET_STEP__ 4 t1 send ch1",
        "__HLDET_STEP__ 5 t0 recv ch1",
        "__HLDET_STEP__ 6 t1 recv ch0",
        "__HLDET_STEP__ 7 t0 recv ch1",
        "__HLDET_STEP__ 8 t1 send ch1",
        "__HLDET_STEP__ 9 t1 finish",
        "__HLDET_STEP__ 10 t0 join t1",
    ],
    PARK_WAKE: [
        "__HLDET_STEP__ 0 t0 spawn t1",
        "__HLDET_STEP__ 1 t1 recv ch0",
        "__HLDET_STEP__ 2 t0 send ch0",
        "__HLDET_STEP__ 3 t0 join t1",
        "__HLDET_STEP__ 4 t1 finish",
    ],
    BOUNDED: [
        "__HLDET_STEP__ 0 t0 spawn t1",
        "__HLDET_STEP__ 1 t1 send ch0",
        "__HLDET_STEP__ 2 t0 spawn t2",
        "__HLDET_STEP__ 3 t1 send ch0",     # parks: the channel is full
        "__HLDET_STEP__ 4 t2 send ch0",     # parks: still full
        "__HLDET_STEP__ 5 t0 recv ch0",     # frees capacity: both woken
        "__HLDET_STEP__ 6 t0 recv ch0",     # (t1 re-parked: lost the race)
        "__HLDET_STEP__ 7 t1 send ch0",
        "__HLDET_STEP__ 8 t0 recv ch0",
        "__HLDET_STEP__ 9 t1 finish",
        "__HLDET_STEP__ 10 t0 recv ch0",
        "__HLDET_STEP__ 11 t2 send ch0",
        "__HLDET_STEP__ 12 t0 recv ch0",
        "__HLDET_STEP__ 13 t2 send ch0",
        "__HLDET_STEP__ 14 t0 recv ch0",
        "__HLDET_STEP__ 15 t2 finish",
        "__HLDET_STEP__ 16 t0 join t1",
        "__HLDET_STEP__ 17 t0 join t2",
    ],
}

TMP = tempfile.mkdtemp(prefix="_gate_s127_", dir=os.path.join(ROOT, "tests"))

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=kw.pop("timeout", 600), **kw)


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def det_trace_native(src, workdir, name, argv=()):
    """Compile + run natively under det with the trace; return (exit,
    stdout, steps, stderr)."""
    c_path = os.path.join(workdir, name + ".c")
    bin_path = os.path.join(workdir, name)
    p = run([HLC, src, c_path])
    if p.returncode != 0:
        raise RuntimeError("hlc failed on %s" % src)
    p = run([CC, "-O2", "-o", bin_path, c_path, "-lm", "-pthread"])
    if p.returncode != 0:
        raise RuntimeError("gcc failed on %s: %s" % (src, p.stderr))
    env = dict(os.environ)
    env["HL_DET_SCHED"] = "1"
    env["HL_DET_TRACE"] = "1"
    p = run([bin_path] + list(argv), env=env)
    steps = [l for l in p.stderr.splitlines()
             if l.startswith("__HLDET_STEP__ ")]
    return p.returncode, p.stdout, steps, p.stderr


def det_trace_interp(src, argv=()):
    env = dict(os.environ)
    env["HL_DET_SCHED"] = "1"
    env["HL_DET_TRACE"] = "1"
    p = run(BOOT + ["--det", src] + list(argv), env=env)
    steps = [l for l in p.stderr.splitlines()
             if l.startswith("__HLDET_STEP__ ")]
    return p.returncode, p.stdout, steps, p.stderr


# ===========================================================================
print("Section 1 — the convention and the refusal matrix")

p = run(DET + ["no-such-file.hls"])
check(p.returncode == 2 and "no such file" in p.stderr,
      "a missing workload is a usage failure (exit 2)")

p = run(DET + ["Makefile"])
check(p.returncode == 2 and "not a .hls workload" in p.stderr,
      "a non-.hls workload is a usage failure (exit 2)")

p = run(DET + ["--runs", "1", ROTATION])
check(p.returncode == 2 and "at least two runs" in p.stderr,
      "--runs 1 is refused (a determinism claim needs two runs)")

p = run(DET + ["--timeout", "0", ROTATION])
check(p.returncode == 2 and "--timeout must be >= 1" in p.stderr,
      "a non-positive timeout is refused")

if not NATIVE:
    print("  [SKIP] the remaining sections need bin/hlc + gcc")
    print()
    print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
    raise SystemExit(0 if FAIL == 0 else 1)

p = run(DET + ["--no-parity", "--runs", "2", PARK_WAKE])
check(p.returncode == 0 and "SKIPPED (--no-parity, explicit)" in p.stdout,
      "the flags compose and a skipped leg says so, loudly")

# ===========================================================================
print("Section 2 — the policy: hand-computed interleaving traces")

for fixture, expected in EXPECTED_TRACES.items():
    name = os.path.basename(fixture, )[:-4]
    ncode, nout, nsteps, _ = det_trace_native(fixture, TMP, name)
    icode, iout, isteps, _ = det_trace_interp(fixture)
    label = os.path.basename(fixture)
    check(ncode == 0 and nsteps == expected,
          "%s: the native trace is the policy, line by line" % label)
    check(icode == 0 and isteps == expected,
          "%s: the interpreter trace is the same policy" % label)

# The select fixture's trace is policy-derived but long; assert its
# SHAPE: the select parks on both channels (no ready scan hit before
# the feeders send) and the wake comes through the first fed channel.
ncode, nout, nsteps, _ = det_trace_native(SELECT_ROT, TMP, "select_rotation")
sel = [s for s in nsteps if s.split(" ")[3] == "select"]
check(ncode == 0 and len(sel) == 1,
      "select_rotation: exactly one select op (it parks, then returns)")
sel_idx = nsteps.index(sel[0])
feed_after = [s for s in nsteps[sel_idx + 1:]
              if s.split(" ")[3] == "send"
              and s.split(" ")[4] in ("ch0", "ch1")]
check(len(feed_after) == 2,
      "select_rotation: both select channels are fed after the park")
check(nout.startswith("idx=0 a=11 b=22"),
      "select_rotation: the scan returns list order (ch_a first)")
icode, iout, isteps, _ = det_trace_interp(SELECT_ROT)
check(isteps == nsteps,
      "select_rotation: interpreter trace identical to native")

# ===========================================================================
print("Section 3 — the runtime wiring (region, arm call, unarmed parity)")

# The hosted emission carries the region and main() arms it.
p = run([HLC, PARK_WAKE, os.path.join(TMP, "wiring.c")])
csrc = open(os.path.join(TMP, "wiring.c")).read()
check("Stage 127 (v0.145.0-alpha): the deterministic scheduler" in csrc,
      "the emitted C carries the det region")
check("hl_det_init();" in csrc,
      "main() arms the mode (the arm call is emitted)")
check("HL_DET_SCHED" in csrc and "__HLDET_STEP__" in csrc,
      "the region reads its env and speaks the trace protocol")

# A --no-libc emission carries the stub macros and none of the mode.
p = run([HLC, "--no-libc", PARK_WAKE, os.path.join(TMP, "wiring_nl.c")])
nlsrc = open(os.path.join(TMP, "wiring_nl.c")).read()
check("#define hl_det_yield() ((void)0)" in nlsrc,
      "a --no-libc emission stubs the hooks away")
check("hl_det_init" not in nlsrc.split("hl_rt_deadlock_check")[0]
      or "static void hl_det_init" not in nlsrc,
      "a --no-libc emission defines no scheduler")
check("hl_det_init();" not in nlsrc,
      "a --no-libc main() arms nothing")

# An unarmed binary behaves exactly as before: the showcase's unarmed
# native output equals its unarmed interpreter output (the differential
# suite's own discipline, checked here for the new corpus).
p = run(BOOT + [SHOWCASE])
interp_unarmed = (p.returncode, p.stdout)
c_path = os.path.join(TMP, "showcase.c")
b_path = os.path.join(TMP, "showcase")
run([HLC, SHOWCASE, c_path])
run([CC, "-O2", "-o", b_path, c_path, "-lm", "-pthread"])
p = run([b_path])
check((p.returncode, p.stdout) == interp_unarmed and p.returncode == 0,
      "unarmed native == unarmed interpreter on the showcase")

# ===========================================================================
print("Section 4 — the determinism certification (the REAL CLI)")

for fixture in ALL_LIBS:
    label = os.path.relpath(fixture, ROOT)
    p = run(DET + [fixture], timeout=900)
    check(p.returncode == 0 and "DETERMINISTIC" in p.stdout
          and "parity: interpreter agrees" in p.stdout,
          "%s certifies (det + parity)" % label)

# --runs 8 stays stable; --json parses and carries the story.
jp = os.path.join(TMP, "report.json")
p = run(DET + ["--runs", "8", "--json", jp, ROTATION], timeout=900)
check(p.returncode == 0, "--runs 8 certifies")
rep = json.load(open(jp))
check(rep["verdict"] == "DETERMINISTIC" and rep["config"]["runs"] == 8
      and rep["parity"]["checked"] is True
      and rep["parity"]["steps_match"] is True
      and rep["parity"]["stdout_match"] is True
      and rep["parity"]["exit_match"] is True
      and len(rep["runs"]) == 8
      and all(r["steps"] == rep["runs"][0]["steps"] for r in rep["runs"])
      and rep["stats"]["steps"] == 11 and rep["stats"]["tasks"] == 2,
      "the json report carries the config, the runs and the stats")

# argv reaches the program (the plan-driving convention).
argv_fixture = os.path.join(TMP, "argv_probe.hls")
with open(argv_fixture, "w") as fh:
    fh.write(
        '# argv probe: echoes its first PROGRAM argument back.\n'
        'fn main() -> int uses IO, Args {\n'
        '    let a: list[str] = args()\n'
        '    if a.len() > 1 {\n'
        '        println("argv1=" + a.get(1))\n'
        '    } else {\n'
        '        println("argv1=missing")\n'
        '    }\n'
        '    return 0\n'
        '}\n')
p = run(DET + [argv_fixture, "hello-det"], timeout=900)
probe_ok = p.returncode == 0
if probe_ok:
    # The tool's report does not echo program stdout; certify the
    # passthrough on both legs directly.
    ncode, nout, _nsteps, _ = det_trace_native(argv_fixture, TMP,
                                               "argv_probe",
                                               argv=("hello-det",))
    icode, iout, _isteps, _ = det_trace_interp(argv_fixture,
                                                argv=("hello-det",))
    probe_ok = ("argv1=hello-det" in nout and "argv1=hello-det" in iout)
check(probe_ok, "program argv passes through the verifier (both legs)")

# ===========================================================================
print("Section 5 — the fairness and the starvation matrix")

# The recv_or poll loop never parks — under the rotation every poll is
# a yield, so the producer always gets its turn: the count of empty
# polls is exactly what the policy predicts (zero: the child's first
# turn comes before the parent's next, so the first value is already
# there when main's first poll runs). The exact program output is
# asserted on the binary (the tool's report does not echo it); the
# tool's certification is asserted beside it.
ncode, nout, _nsteps, _ = det_trace_native(FAIRNESS, TMP, "fairness")
check(ncode == 0 and "empty_polls=0" in nout and "got=3" in nout,
      "the poll loop terminates with the rotation's exact answer")
p = run(DET + [FAIRNESS], timeout=900)
check(p.returncode == 0 and "DETERMINISTIC" in p.stdout,
      "and the tool certifies the poll loop (det + parity)")

# The hltest witness: armed and unarmed, the tests pass.
p = run([sys.executable, "tools/hltest.py", "--det", ROTATION_TEST])
check("2 pass, 0 fail" in p.stdout, "hltest --det runs the witness green")
p = run([sys.executable, "tools/hltest.py", ROTATION_TEST])
check("2 pass, 0 fail" in p.stdout, "hltest unarmed runs it green too")

# ===========================================================================
print("Section 6 — the deadlock and the panic parity")

for fixture, label in ((DEADLOCK, "det_libs/deadlock"),
                       (PANIC_FIXTURE, "ok/panic_stage127_det")):
    ncode, nout, nsteps, nerr = det_trace_native(fixture, TMP,
                                                 label.replace("/", "_"))
    icode, iout, isteps, ierr = det_trace_interp(fixture)
    check(ncode == 101 and icode == 101 and nout == "" and iout == "",
          "%s: exit 101, empty stdout, both backends, det mode" % label)
    check("deadlock: all tasks are blocked on channel operations"
          in nerr and "deadlock: all tasks are blocked on channel "
          "operations" in ierr,
          "%s: the canonical Stage 16 message, both backends" % label)
    # Unarmed: the same verdict through the preemptive scan.
    c_path = os.path.join(TMP, "dl_%s.c" % label.replace("/", "_"))
    b_path = os.path.join(TMP, "dl_%s" % label.replace("/", "_"))
    run([HLC, fixture, c_path])
    run([CC, "-O2", "-o", b_path, c_path, "-lm", "-pthread"])
    p = run([b_path])
    p2 = run(BOOT + [fixture])
    check(p.returncode == 101 and p2.returncode == 101,
          "%s: the preemptive verdict agrees (unarmed, both backends)"
          % label)
    # The tool certifies the deterministic death and says so.
    p = run(DET + [fixture], timeout=900)
    check(p.returncode == 0 and "exit 101" in p.stdout
          and "certified, not the health" in p.stdout,
          "%s: hls-det certifies the deterministic death, honestly"
          % label)

# ===========================================================================
print("Section 7 — the honesty gates")

# A clock-reading program is nondeterministic however it is scheduled:
# the tool must refuse (the comparison is real, not vacuous).
p = run(DET + [NONDET], timeout=900)
check(p.returncode == 1 and "NOT DETERMINISTIC" in p.stdout,
      "a clock-reading workload is refused (NOT DETERMINISTIC)")

# A forged step line is a loud protocol error, never a silent pass.
p = run(DET + [LOOKALIKE], timeout=900)
check(p.returncode == 1 and "protocol look-alike on stdout" in p.stdout,
      "a forged step line on stdout is a protocol error")

# The validator, unit-checked directly (imported, not subprocessed).
mod = load_mod("hls_det", os.path.join(ROOT, "tools", "hls-det.py"))


def expect_proto_err(stderr_text, why):
    try:
        mod.parse_trace(stderr_text.encode())
    except mod.ProtocolError:
        return True
    return False


check(expect_proto_err("__HLDET_STEP__ 0 t0 recv ch0\n"
                       "__HLDET_STEP__ 2 t0 recv ch0\n",
                       "gap"),
      "validator: a seq gap is refused")
check(expect_proto_err("__HLDET_STEP__ x t0 recv ch0\n", "malformed"),
      "validator: a malformed record is refused")
check(expect_proto_err("__HLDET_STEP__ 0 t0 teleport ch0\n", "op"),
      "validator: an unknown op is refused")
check(expect_proto_err("some __HLDET_ chatter\n", "look-alike"),
      "validator: a look-alike line is refused")
check(not expect_proto_err("__HLDET_STEP__ 0 t0 spawn t1\n"
                           "__HLDET_STEP__ 1 t1 finish\n", "ok"),
      "validator: a well-formed dense trace parses")
try:
    mod.check_stdout_protocol(b"__HLDET_STEP__ 9 t9 recv ch9\n")
    bad("validator: a stdout forgery is refused")
except mod.ProtocolError:
    ok("validator: a stdout forgery is refused")
steps, chatter = mod.parse_trace(
    b"__HLDET_STEP__ 0 t0 recv ch0\nprogram chatter\n")
check(steps == ["__HLDET_STEP__ 0 t0 recv ch0"] and chatter == ["program chatter"],
      "validator: chatter is separated from steps")

# ===========================================================================
print("Section 8 — the differential and the corpus hygiene")

# The full fixture set: interpreter det trace == native det trace.
allsame = True
for fixture in ALL_LIBS:
    name = os.path.basename(fixture)[:-4]
    ncode, nout, nsteps, _ = det_trace_native(fixture, TMP, "s8_" + name)
    icode, iout, isteps, _ = det_trace_interp(fixture)
    if nsteps != isteps or nout != iout or ncode != icode:
        allsame = False
        bad("  trace divergence on %s" % os.path.relpath(fixture, ROOT))
check(allsame,
      "every fixture: stdout, trace and exit identical across backends")

# The showcase is deterministic unarmed as well (it is in the ok corpus:
# the differential suite will run it; we check the property directly).
p = run(BOOT + [SHOWCASE])
check(p.returncode == 0 and "doubled=12 total=6" in p.stdout,
      "the showcase runs green through the interpreter")

# Corpus hygiene: canonical form, lints, checker.
hygiene = [SHOWCASE, PANIC_FIXTURE] + [
    os.path.join(LIBS, f) for f in sorted(os.listdir(LIBS))
    if f.endswith(".hls")]
for f in hygiene:
    label = os.path.relpath(f, ROOT)
    p = run([sys.executable, "tools/hlfmt.py", "-c", f])
    check("already formatted" in p.stdout, "%s is hlfmt-canonical" % label)
    p = run([sys.executable, "tools/hllint.py", f])
    check("no warnings" in p.stdout, "%s is hllint-clean" % label)
    p = run(BOOT + ["--check", f])
    check("OK: types and effects valid" in p.stdout,
          "%s boots through the real checker" % label)

# No gate litter survives.
litter = []
for d in (LIBS, TMP, os.path.join(ROOT, "tests")):
    for f in os.listdir(d):
        if f.startswith("_gate_s127_") and os.path.isdir(os.path.join(d, f)):
            continue
        if f.startswith(".hls-det-") or f.startswith("hls-det-"):
            litter.append(os.path.join(d, f))
check(not litter, "no verifier temp files survive anywhere the gate ran")

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
raise SystemExit(0 if FAIL == 0 else 1)
