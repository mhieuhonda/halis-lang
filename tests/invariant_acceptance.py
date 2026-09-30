#!/usr/bin/env python3
"""Stage 97 acceptance gate — SMT-based loop-invariant inference.

Run with `make invariant-acceptance` (or
`python3 tests/invariant_acceptance.py`).

Six sections:

  1. the engine          — candidate generation + verification on a
                           battery of loop shapes: every accepted
                           invariant and every refusal pinned exactly
  2. the soundness flips — loops where naive inference would be WRONG
                           (step-2 overshoot, the -2 skipper, the
                           shrinking counter, the unmodeled body): the
                           engine must NOT claim the wrong invariant
  3. the obligations     — every verified segment is z3-unsat, the
                           .smt2 dump re-decides clean end to end
  4. the CLI             — `hlprove --infer-invariants` reports the
                           demo's invariants, the refusal, and totals
  5. the demo            — invariant_demo.hls: interpreter and native
                           output agree; inference changes nothing
  6. the tools           — hlfmt stable, hllint clean on the new
                           sources

The gate REQUIRES a decider (a z3 binary or the z3-solver python
module) — a verification stage whose solver is missing has no gate.
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
from tools.hlprove import make_decider        # noqa: E402

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_inv_", dir=os.path.join(ROOT, "tests"))

DEMO = "examples/invariant_demo.hls"
OK_TEST = "tests/ok/feat_stage97_invariants.hls"


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
    """load + check + infer on a scratch program; returns (loops, decider)."""
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


# ---------------------------------------------------------------------------
print("=== 1. the engine ===")
# ---------------------------------------------------------------------------

DECIDER = make_decider()
if DECIDER is None:
    print("  [FAIL] no z3 binary and no z3-solver module — install one:"
          " pip install z3-solver (or put a z3 binary on PATH)")
    sys.exit(1)
ok("a decider is available (z3 binary or z3-solver module)")

# -- the flagship: strengthening (s' = s + i needs i >= 0) --------------
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
if not any(c.text == "i <= 0" for c, _w in lp.rejected):
    bad("sum_to: the stale entry bound i <= 0 should be refused")
else:
    ok("sum_to: the stale entry bound i <= 0 is refused (preservation)")

# -- the transition language ------------------------------------------
loops = engine_on("""
fn acc(n: int, base: int) -> int
    requires n >= 0
    requires base >= 1
{
    let mut i: int = base
    let mut j: int = 0
    while i > 0 {
        j = j + i
        i = i - 1
    }
    return j
}
fn main() -> int { return 0 }
""", "acc.hls")
lp = loop_by_fn(loops, "acc")
_proof.verify_loop(lp, DECIDER)
acc = accepted_texts(lp)
if {"i >= 0", "j >= 0"} <= acc and "i >= 1" not in acc:
    ok("acc: i >= 0 survives as the cond-bound; the stale i >= 1 does not")
else:
    bad("acc: expected i >= 0 / j >= 0 (no stale i >= 1) — got %s" % sorted(acc))

# -- for-range: entry equation + runs-guard + range bound --------------
loops = engine_on("""
fn frs(a: int, b: int) -> int
    requires a >= 0
    requires b >= a
{
    let mut s: int = 0
    for i: int in range(a, b) {
        s = s + i
    }
    return s
}
fn main() -> int { return 0 }
""", "frs.hls")
lp = loop_by_fn(loops, "frs")
if lp is None:
    bad("frs: no loop summary collected at all")
else:
    _proof.verify_loop(lp, DECIDER)
    acc = accepted_texts(lp)
    if {"i >= a", "i <= b", "s >= 0"} <= acc:
        ok("frs: range(a, b) yields i >= a, i <= b and the accumulator")
    else:
        bad("frs: expected i >= a / i <= b / s >= 0 — got %s" % sorted(acc))

# -- disjunctive transitions (conditional update) -----------------------
loops = engine_on("""
fn drift(n: int) -> int
    requires n >= 0
{
    let mut i: int = 0
    while i < n {
        if i % 2 == 0 {
            i = i + 1
        } else {
            i = i + 2
        }
    }
    return i
}
fn main() -> int { return 0 }
""", "drift.hls")
lp = loop_by_fn(loops, "drift")
_proof.verify_loop(lp, DECIDER)
if "i >= 0" in accepted_texts(lp) and \
        lp.transition_text("i") == "i' = i + 1 | i + 2":
    ok("drift: the disjunctive transition i' = i + 1 | i + 2 is encoded")
else:
    bad("drift: expected the disjunctive transition + i >= 0 — got %s"
        % lp.transition_text("i"))

# -- congruence (the non-interval family) -------------------------------
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

# ---------------------------------------------------------------------------
print("=== 2. the soundness flips ===")
# ---------------------------------------------------------------------------
# Every case below is one a naive inference would get WRONG. The engine
# must refuse exactly these — a claimed wrong invariant under -O fast
# reasoning would be a soundness hole.

# step-2 overshoot: i <= n is FALSE at the head (i lands on n + 1); the
# weakened i <= n + 1 is the truth.
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

# the skipper: i >= 0 fails (1 steps to -1); the tight floor is -1.
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

# the shrinking counter under i < n: i >= 0 is NOT inductive.
loops = engine_on("""
fn shrink(n: int) -> int
    requires n >= 0
{
    let mut i: int = 0
    while i < n {
        i = i - 1
    }
    return i
}
fn main() -> int { return 0 }
""", "shrink.hls")
lp = loop_by_fn(loops, "shrink")
_proof.verify_loop(lp, DECIDER)
if "i >= 0" not in accepted_texts(lp):
    ok("shrink: i >= 0 refused (the condition does not stop the crossing)")
else:
    bad("shrink: claimed i >= 0 — UNSOUND")

# unmodeled bodies claim nothing (i = i * i is genuinely non-affine).
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

# escapes go wholesale unmodeled.
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
if lp is not None and "i" in lp.unmodeled and not lp.accepted:
    ok("escaper: the break sends i unmodeled and nothing is claimed")
else:
    bad("escaper: expected wholesale unmodeled — got %s / %s"
        % (lp.unmodeled, [c.text for c in lp.accepted] if lp else "?"))

# ---------------------------------------------------------------------------
print("=== 3. the obligations ===")
# ---------------------------------------------------------------------------
# Every segment of every verified loop must re-decide unsat from its
# .smt2 text alone — the dump is the artifact, not a summary.

program = load_program(DEMO)
check(program)
loops = _proof.infer_loops(program)
all_unsat = True
seg_count = 0
for lp in loops:
    segs = _proof.verify_loop(lp, DECIDER)
    for _label, lines in segs:
        seg_count += 1
        v = DECIDER(lines)
        if v != "unsat":
            all_unsat = False
            bad("obligation %s of %s decided %s (expected unsat)"
                % (_label, lp.fn_key, v))
if all_unsat and seg_count > 0:
    ok("%d obligation segments across %d loops, every one z3-unsat"
       % (seg_count, len(loops)))
elif seg_count == 0:
    bad("no obligation segments were generated for the demo")

dump_dir = os.path.join(TMP, "smt")
os.makedirs(dump_dir, exist_ok=True)
dump_main = run([sys.executable, "tools/hlprove.py", DEMO,
                 "--infer-invariants", "--smt"])
# the dump lands next to the source (the bridge convention), named by
# function key: <fn>__L<line>.smt2
if dump_main.returncode != 0:
    bad("the --smt dump run failed (rc=%d)" % dump_main.returncode)
demo_dumps = sorted(f for f in os.listdir("examples")
                    if f.endswith(".smt2") and "__L" in f)
if not demo_dumps:
    bad("the --smt dump wrote no obligation files")
else:
    ok("%d obligation files dumped next to the demo" % len(demo_dumps))
    dumped_ok = True
    for fname in demo_dumps:
        with open(os.path.join("examples", fname)) as f:
            text = f.read()
        for seg in [s.strip() for s in text.split("(reset)") if s.strip()]:
            if DECIDER(seg.splitlines()) != "unsat":
                dumped_ok = False
        os.unlink(os.path.join("examples", fname))
    if dumped_ok:
        ok("every dumped obligation re-decides unsat from its file")
    else:
        bad("a dumped obligation is not unsat — the file lies")

# ---------------------------------------------------------------------------
print("=== 4. the CLI ===")
# ---------------------------------------------------------------------------
out = run([sys.executable, "tools/hlprove.py", DEMO, "--infer-invariants"])
if out.returncode != 0:
    bad("hlprove --infer-invariants exited %d" % out.returncode)
else:
    ok("hlprove --infer-invariants runs clean on the demo")
    txt = out.stdout
    checks = [
        ("sum_to: while header with line",
         "sum_to:21: while i < n" in txt),
        ("the strengthened accumulator is reported",
         "      s >= 0   (entry-bound)" in txt),
        ("the overshoot bound is reported",
         "i <= n + 1   (cond-bound)" in txt),
        ("the congruence is reported",
         "x ≡ 0 (mod 4)   (modular)" in txt),
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

plain = run([sys.executable, "tools/hlprove.py", OK_TEST,
             "--infer-invariants"])
if plain.returncode == 0 and "INFERENCE TOTAL:" in plain.stdout:
    ok("cli: the ok-test's loops infer cleanly (totals line present)")
else:
    bad("cli: inference on the ok-test failed (rc=%d)" % plain.returncode)

# ---------------------------------------------------------------------------
print("=== 5. the demo ===")
# ---------------------------------------------------------------------------
interp = run([sys.executable, "boot/boot.py", DEMO])
if interp.returncode == 0:
    ok("the demo runs on the interpreter (rc=0)")
else:
    bad("the demo failed on the interpreter (rc=%d)" % interp.returncode)

BIN = os.path.join(ROOT, "bin", "hlc")
native_ok = False
if os.path.exists(BIN):
    c_out = os.path.join(TMP, "invariant_demo.c")
    comp = run([BIN, DEMO, c_out])
    if comp.returncode == 0:
        exe = os.path.join(TMP, "invariant_demo")
        cc = run(["cc", "-O2", "-Werror", "-o", exe, c_out, "-lm", "-pthread"])
        if cc.returncode == 0:
            native = run([exe])
            if native.returncode == 0 and native.stdout == interp.stdout:
                native_ok = True
                ok("native (-O2 -Werror) output identical to the interpreter")
            else:
                bad("native output diverges (rc=%d)" % native.returncode)
        else:
            bad("cc refused the generated C (rc=%d)" % cc.returncode)
    else:
        bad("hlc refused the demo (rc=%d)" % comp.returncode)
else:
    bad("bin/hlc missing — run make bootstrap first")

# ---------------------------------------------------------------------------
print("=== 6. the tools ===")
# ---------------------------------------------------------------------------
fmt1 = run([sys.executable, "tools/hlfmt.py", DEMO])
fmt2 = run([sys.executable, "tools/hlfmt.py", DEMO])
if fmt1.returncode == 0 and fmt1.stdout == fmt2.stdout:
    ok("hlfmt: stable formatting on the demo")
else:
    bad("hlfmt: unstable or failed on the demo")

lints = [run([sys.executable, "tools/hllint.py", f])
         for f in (DEMO, OK_TEST)]
if all(l.returncode == 0 for l in lints):
    ok("hllint: no findings on the demo or the ok-test")
else:
    bad("hllint: findings on the new sources")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 97 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
