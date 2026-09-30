#!/usr/bin/env python3
"""Stage 99 acceptance gate — `hlprove --cvc5`, the CVC5 SMT backend.

Run with `make cvc5-acceptance` (or `python3 tests/cvc5_acceptance.py`).

Six sections:

  1. the backend         — the cvc5 binary and the cvc5 module answer a
                           trivial probe; the decider exists; a trivial
                           unsat is unsat through it
  2. the bridge          — the contract bridge runs under --cvc5: the
                           vacuity / ensures verdicts are the ones the
                           contracts were BUILT to force (implied, not
                           implied, vacuous), --cvc5 implies --smt, and
                           the verdict lines match --z3 word for word
  3. the inference       — the Stage 97 battery through the cvc5
                           decider: the strengthening, both soundness
                           flips, the congruence, the unmodeled and
                           escaping bodies claiming nothing
  4. cross-solver parity — every demo obligation re-decided by BOTH
                           backends agrees; the full CLI reports are
                           byte-identical modulo the solver's name
  5. the CLI             — the cvc5 labels, the refusal of --z3
                           together with --cvc5, and the honesty of a
                           missing backend (nothing is claimed)
  6. the dump            — the obligation files written under --cvc5
                           re-decide unsat from the file alone

The gate REQUIRES the cvc5 backend (a binary or the cvc5 python
module) and the z3 backend (the Stage 97 decider) — the whole point of
the stage is that the two agree.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from boot.boot import load_program            # noqa: E402
from boot.checker import check                # noqa: E402
from boot import proof as _proof              # noqa: E402
import tools.hlprove as hl                    # noqa: E402
from tools.hlprove import (gen_smt, make_cvc5_decider,  # noqa: E402
                           make_decider)

PASS = 0
FAIL = 0
# No "cvc5" in the name: the paths the bridge prints land in the CLI
# output, and the parity sections normalise the solver's name in that
# output — a path carrying the name would survive one side's replace.
TMP = tempfile.mkdtemp(prefix="_gate_s99_", dir=os.path.join(ROOT, "tests"))

DEMO = "examples/invariant_demo.hls"
HMAC = "examples/hmac_proven.hls"


def clean_demo_dumps():
    """Remove the obligation dumps the demo runs leave next to the
    source (the bridge convention: same directory as the .hls)."""
    for f in os.listdir("examples"):
        if f.endswith(".smt2") and "__L" in f:
            try:
                os.unlink(os.path.join("examples", f))
            except OSError:
                pass


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


def engine_on(source, name):
    """load + check on a scratch program; returns the loop summaries."""
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        f.write(source)
    program = load_program(path)
    check(program)
    return _proof.infer_loops(program)


def loop_by_fn(loops, fn_key):
    for lp in loops:
        if lp.fn_key == fn_key:
            return lp
    return None


def accepted_texts(loop):
    return {c.text for c in loop.accepted}


def rejected_texts(loop):
    return {c.text for c, _why in loop.rejected}


def segments_unsat(segments, decide, label):
    """Decide every segment; returns True when every verdict is unsat."""
    good = True
    for _label, lines in segments:
        if decide(lines) != "unsat":
            good = False
            bad("%s: an obligation decided non-unsat" % label)
    return good


# ---------------------------------------------------------------------------
print("=== 1. the backend ===")
# ---------------------------------------------------------------------------

BIN_V = hl._cvc5_binary_decide(["(check-sat)"])
MOD_V = hl._cvc5_module_decide(["(check-sat)"])
if BIN_V is not None:
    ok("the cvc5 binary is on PATH and answers: %s" % BIN_V)
else:
    print("  [note] no cvc5 binary on PATH — the module carries the gate")
if MOD_V is not None:
    ok("the cvc5 python module is importable and answers: %s" % MOD_V)
else:
    print("  [note] no cvc5 python module — the binary carries the gate")
if BIN_V is None and MOD_V is None:
    print("  [FAIL] no cvc5 binary and no cvc5 module — install one:"
          " put a cvc5 binary on PATH or pip install cvc5")
    sys.exit(1)
DECIDER = make_cvc5_decider()
if DECIDER is not None:
    ok("the cvc5 decider is available (binary first, module fallback)")
else:
    bad("make_cvc5_decider returned None with a backend present")
trivial = ["(declare-const x Int)", "(assert (> x 0))",
           "(assert (not (> x 0)))", "(check-sat)"]
if DECIDER(trivial) == "unsat":
    ok("a trivial contradiction decides unsat through the cvc5 decider")
else:
    bad("a trivial contradiction decided %s (expected unsat)"
        % DECIDER(trivial))

Z3 = make_decider()
if Z3 is not None:
    ok("the z3 decider is available (the parity sections need it)")
else:
    print("  [FAIL] no z3 binary and no z3-solver module — the parity"
          " sections need both backends (pip install z3-solver)")
    sys.exit(1)

# ---------------------------------------------------------------------------
print("=== 2. the bridge ===")
# ---------------------------------------------------------------------------
# Three contracts built to force three DIFFERENT verdicts: an ensures
# that IS implied by its requires, one that is NOT (a solver that
# rubber-stamps unsat fails here), and a vacuous contract (unsat
# vacuity). Both backends must read the bridge the same way.

probe_src = """fn implied(x: int, y: int) -> int
    requires x >= 2
    requires y >= 3
    ensures x + y >= 5
{
    return x + y
}
fn not_implied(x: int, y: int) -> int
    requires x >= 2
    requires y >= 3
    ensures x + y >= 6
{
    return x + y
}
fn vacuous(n: int) -> int
    requires n > 0
    requires n < 0
{
    return 0
}
fn main() -> int { return 0 }
"""
probe = os.path.join(TMP, "bridge_probe.hls")
with open(probe, "w") as f:
    f.write(probe_src)
out_cvc5 = run([sys.executable, "tools/hlprove.py", probe, "--cvc5"])
out_z3 = run([sys.executable, "tools/hlprove.py", probe, "--z3"])
if out_cvc5.returncode == 0:
    ok("hlprove --cvc5 runs the bridge clean on the probe")
else:
    bad("hlprove --cvc5 exited %d on the probe" % out_cvc5.returncode)
expect = [
    ("the implied ensures is certified unsat (requires |= ensures)",
     "implied" in out_cvc5.stdout
     and "cvc5: vacuity: sat, ensures: unsat" in out_cvc5.stdout),
    ("the NOT-implied ensures is certified sat (no rubber stamp)",
     "not_implied" in out_cvc5.stdout
     and "cvc5: vacuity: sat, ensures: sat" in out_cvc5.stdout),
    ("the vacuous contract is certified unsat (vacuity)",
     "cvc5: vacuity: unsat" in out_cvc5.stdout),
]
for name, cond in expect:
    if cond:
        ok("bridge: %s" % name)
    else:
        bad("bridge: %s" % name)
# The bridge header names the backend that will decide it.
if "SMT-LIB2 bridge (cvc5-ready" in out_cvc5.stdout:
    ok("bridge: the header says cvc5-ready under --cvc5")
else:
    bad("bridge: the header does not name the cvc5 backend")
# --cvc5 implies --smt: the .smt2 files land next to the source.
dumped = sorted(f for f in os.listdir(TMP) if f.endswith(".smt2"))
if {"implied.smt2", "not_implied.smt2", "vacuous.smt2"} <= set(dumped):
    ok("--cvc5 implies --smt (the obligation files are written)")
else:
    bad("--cvc5 did not write the obligation files — got %s" % dumped)
# The verdict lines agree word for word once the solver name is
# normalised.
if out_z3.returncode == 0 and \
        out_cvc5.stdout.replace("cvc5", "z3") == out_z3.stdout:
    ok("the cvc5 bridge verdicts match the z3 run word for word")
else:
    bad("the cvc5 and z3 bridge verdicts disagree")

# ---------------------------------------------------------------------------
print("=== 3. the inference battery ===")
# ---------------------------------------------------------------------------
# The Stage 97 battery, re-decided by cvc5: every acceptance and every
# refusal must be the SAME verdict z3 gave — the conclusions belong to
# the obligations, not to the solver.

loops = engine_on("""
fn sum_to(n: int) -> int
    requires n >= 0
{
    let mut s: int = 0
    let mut i: int = 0
    while i < n {
        s = s + i
        i = i + 1
    }
    return s
}
fn main() -> int { return 0 }
""", "sum_to.hls")
lp = loop_by_fn(loops, "sum_to")
_proof.verify_loop(lp, DECIDER)
acc = accepted_texts(lp)
if {"i <= n", "i >= 0", "s >= 0"} <= acc:
    ok("sum_to: i <= n, i >= 0 AND the strengthened s >= 0 are inferred")
else:
    bad("sum_to: strengthening set wrong — got %s" % sorted(acc))
if "i <= 0" in rejected_texts(lp):
    ok("sum_to: the stale entry bound i <= 0 is refused (preservation)")
else:
    bad("sum_to: the stale entry bound i <= 0 was not refused")

loops = engine_on("""
fn step_two(n: int) -> int
    requires n >= 0
{
    let mut i: int = 0
    while i < n {
        i = i + 2
    }
    return i
}
fn main() -> int { return 0 }
""", "step_two.hls")
lp = loop_by_fn(loops, "step_two")
_proof.verify_loop(lp, DECIDER)
acc = accepted_texts(lp)
if "i <= n" not in acc and "i <= n + 1" in acc and "i >= 0" in acc:
    ok("step_two: i <= n refused, the overshoot bound i <= n + 1 kept")
else:
    bad("step_two: wrong verdicts — accepted %s" % sorted(acc))

loops = engine_on("""
fn skipper(n: int) -> int
    requires n >= 0
{
    let mut i: int = n
    while i > 0 {
        i = i - 2
    }
    return i
}
fn main() -> int { return 0 }
""", "skipper.hls")
lp = loop_by_fn(loops, "skipper")
_proof.verify_loop(lp, DECIDER)
acc = accepted_texts(lp)
if "i >= 0" not in acc and "i >= -1" in acc:
    ok("skipper: i >= 0 refused, the true floor i >= -1 discovered")
else:
    bad("skipper: wrong verdicts — accepted %s" % sorted(acc))

loops = engine_on("""
fn mod4(n: int) -> int
    requires n >= 0
{
    let mut x: int = 0
    let mut k: int = 0
    while k < n {
        x = x + 4
        k = k + 1
    }
    return x
}
fn main() -> int { return 0 }
""", "mod4.hls")
lp = loop_by_fn(loops, "mod4")
_proof.verify_loop(lp, DECIDER)
if "x ≡ 0 (mod 4)" in accepted_texts(lp):
    ok("mod4: the congruence x ≡ 0 (mod 4) is inferred (non-interval)")
else:
    bad("mod4: the congruence invariant is missing — got %s"
        % sorted(accepted_texts(lp)))

loops = engine_on("""
fn times2(n: int) -> int
    requires n >= 0
{
    let mut i: int = 1
    while i < n {
        i = i * i
    }
    return i
}
fn main() -> int { return 0 }
""", "times2.hls")
lp = loop_by_fn(loops, "times2")
if lp is not None and "non-affine update" in lp.unmodeled.get("i", ""):
    ok("times2: the non-affine body is reported unmodeled")
else:
    bad("times2: expected an unmodeled report — got %s"
        % (lp.unmodeled if lp is not None else "no loop"))
if lp is not None:
    _proof.verify_loop(lp, DECIDER)
    if not lp.accepted:
        ok("times2: no invariant is claimed over an unmodeled variable")
    else:
        bad("times2: claims %s over an unmodeled variable — UNSOUND"
            % [c.text for c in lp.accepted])

loops = engine_on("""
fn escaper(n: int) -> int
    requires n >= 0
{
    let mut i: int = 0
    while i < n {
        if i == 3 {
            break
        }
        i = i + 1
    }
    return i
}
fn main() -> int { return 0 }
""", "escaper.hls")
lp = loop_by_fn(loops, "escaper")
if lp is not None:
    _proof.verify_loop(lp, DECIDER)
    if "i" in lp.unmodeled and not lp.accepted:
        ok("escaper: the break sends i unmodeled and nothing is claimed")
    else:
        bad("escaper: expected wholesale unmodeled — got %s / %s"
            % (lp.unmodeled, [c.text for c in lp.accepted]))
else:
    bad("escaper: no loop summary collected")

# ---------------------------------------------------------------------------
print("=== 4. cross-solver parity ===")
# ---------------------------------------------------------------------------
# The obligations decide the verdicts — so BOTH backends must certify
# every obligation of the demo, and the full CLI reports must agree
# byte for byte once the solver's name is normalised.

program = load_program(DEMO)
check(program)
loops = _proof.infer_loops(program)
seg_count = 0
cvc5_unsat = True
z3_agrees = True
for lp in loops:
    segs = _proof.verify_loop(lp, DECIDER)
    for _label, lines in segs:
        seg_count += 1
        v1 = DECIDER(lines)
        v2 = Z3(lines)
        if v1 != "unsat":
            cvc5_unsat = False
            bad("%s: cvc5 decided %s (expected unsat)" % (_label, v1))
        if v2 != v1:
            z3_agrees = False
            bad("%s: z3 decided %s against cvc5's %s" % (_label, v2, v1))
if cvc5_unsat and z3_agrees and seg_count > 0:
    ok("%d obligation segments across %d loops: cvc5-unsat, z3 agrees"
       % (seg_count, len(loops)))
elif seg_count == 0:
    bad("no obligation segments were generated for the demo")

# The contract bridge of the HMAC acceptance example: every segment
# decided independently by both backends must pair up.
program = load_program(HMAC)
check(program)
bridge = gen_smt(program, TMP)
pairs = 0
parity = True
for key, fpath in bridge:
    with open(fpath) as f:
        text = f.read()
    # The runners report every check-sat verdict in the file — a
    # trailing prelude-only segment (an ensures query skipped for a
    # result type with no QF_LIA encoding) carries no verdict, so the
    # parity compares exactly the verdict-bearing segments.
    for seg in [s.strip() for s in text.split("(reset)") if s.strip()]:
        if "(check-sat)" not in seg:
            continue
        lines = seg.splitlines()
        v1 = DECIDER(lines)
        v2 = Z3(lines)
        pairs += 1
        if v1 != v2 or v1 not in ("sat", "unsat"):
            parity = False
            bad("%s: backends disagree (%s vs %s)" % (key, v1, v2))
if parity and pairs > 0:
    ok("%d bridge segments of %s: cvc5 and z3 agree on every verdict"
       % (pairs, HMAC))
elif pairs == 0:
    bad("no bridge segments for the HMAC example")

# The full CLI reports are byte-identical modulo the solver's name.
cli_cvc5 = run([sys.executable, "tools/hlprove.py", DEMO,
                "--cvc5", "--infer-invariants"])
cli_z3 = run([sys.executable, "tools/hlprove.py", DEMO,
              "--z3", "--infer-invariants"])
if cli_cvc5.returncode == 0 and cli_z3.returncode == 0 and \
        cli_cvc5.stdout.replace("cvc5", "z3") == cli_z3.stdout:
    ok("the full CLI report is identical across backends (modulo the"
       " solver's name)")
else:
    bad("the cvc5 and z3 CLI reports differ beyond the solver's name")

# ---------------------------------------------------------------------------
print("=== 5. the CLI ===")
# ---------------------------------------------------------------------------
txt = cli_cvc5.stdout
checks = [
    ("the inference header names the backend",
     "cvc5-checked" in txt),
    ("the strengthened accumulator is reported as cvc5-verified",
     "inferred (cvc5-verified inductive):" in txt
     and "      s >= 0   (entry-bound)" in txt),
    ("the overshoot bound is reported",
     "i <= n + 1   (cond-bound)" in txt),
    ("the refusal names its obligation",
     "rejected: i >= 0 (preservation obligation not discharged)" in txt),
    ("the totals line closes the report",
     "INFERENCE TOTAL:" in txt and "invariants verified" in txt),
]
for name, cond in checks:
    if cond:
        ok("cli: %s" % name)
    else:
        bad("cli: %s" % name)

both = run([sys.executable, "tools/hlprove.py", DEMO, "--z3", "--cvc5"])
if both.returncode == 2 and "pick one" in (both.stderr or ""):
    ok("cli: --z3 together with --cvc5 is refused (rc=2, pick one)")
else:
    bad("cli: --z3 + --cvc5 not refused (rc=%d)" % both.returncode)

saved_bin = hl._cvc5_binary_decide
saved_mod = hl._cvc5_module_decide
try:
    hl._cvc5_binary_decide = lambda lines: None
    hl._cvc5_module_decide = lambda lines: None
    if hl.make_cvc5_decider() is None:
        ok("cli: with neither binary nor module the decider is None"
           " (nothing is ever claimed without verdicts)")
    else:
        bad("cli: a decider exists with no backend behind it — UNSOUND")
finally:
    hl._cvc5_binary_decide = saved_bin
    hl._cvc5_module_decide = saved_mod

# ---------------------------------------------------------------------------
print("=== 6. the dump ===")
# ---------------------------------------------------------------------------
clean_demo_dumps()
dump = run([sys.executable, "tools/hlprove.py", DEMO,
            "--cvc5", "--infer-invariants", "--smt"])
dumps = sorted(f for f in os.listdir("examples")
               if f.endswith(".smt2") and "__L" in f)
if dump.returncode != 0:
    bad("the --cvc5 --smt dump run failed (rc=%d)" % dump.returncode)
if not dumps:
    bad("the --cvc5 --smt dump wrote no obligation files")
else:
    ok("%d obligation files dumped next to the demo" % len(dumps))
    dumped_ok = True
    for fname in dumps:
        with open(os.path.join("examples", fname)) as f:
            text = f.read()
        for seg in [s.strip() for s in text.split("(reset)") if s.strip()]:
            if DECIDER(seg.splitlines()) != "unsat":
                dumped_ok = False
        os.unlink(os.path.join("examples", fname))
    if dumped_ok:
        ok("every dumped obligation re-decides unsat through cvc5")
    else:
        bad("a dumped obligation is not unsat — the file lies")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 99 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
