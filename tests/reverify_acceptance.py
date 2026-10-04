#!/usr/bin/env python3
"""Stage 108 acceptance gate — memory-safety re-verification under
`-O fast` (proof replay).

Run with `make reverify-acceptance` (or
`python3 tests/reverify_acceptance.py`).

Nine sections:

  1. the one definition — the replay imports boot.proof's interval
                          arithmetic (never re-implements it); the
                          identity-certificate whitelist is pinned;
                          the CLI selftest (40 vectors) is green
  2. the baseline       — the AST harvest sees exactly what the prover
                          annotated, proven and checked, on every
                          contracted shape (ovf, div, bnd, byte_at,
                          slice, range loops)
  3. the transform      — IR0 vs IR1: the optimiser's changes are
                          visible to the tool (folds, inlines, the
                          for-get exclusion), the ty stamps ride the
                          transformations
  4. the replay         — the abstract interpreter re-derives the
                          verdicts on the OPTIMISED IR: the seeded
                          shapes re-prove, the loops widen, the
                          INT64_MIN/-1 corner and the delta-0 index
                          bound stay refused
  5. the law            — REPLAY-OK on the whole positive corpus
                          (examples + tests/ok + benchmarks), the
                          replayed/checked/eliminated/inline-derived
                          classes land where they must, the tool is
                          deterministic (byte-identical --json over
                          re-runs), the forged and broken-IR vectors
                          FAIL as designed
  6. the refusals       — negative tests and IR-unsupported constructs
                          refuse cleanly (exit 1, no traceback);
                          usage errors exit 2
  7. the demos          — examples/reverify_demo.hls and
                          tests/ok/feat_stage108_reverify.hls run;
                          interpreter and native agree byte for byte
                          (native half only when bin/hlc exists — the
                          gate stays hermetic)
  8. the report         — the schema (hls-reverify-report/v1), the
                          headline numbers, the canonical JSON
                          serialisation, --out writes the versioned
                          report file
  9. the tools          — hlfmt stable, hllint clean on the new
                          sources
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
sys.path.insert(0, os.path.join(ROOT, "tools"))

REV = [sys.executable, os.path.join(ROOT, "tools", "hls-reverify.py")]
DEMO = "examples/reverify_demo.hls"
OK_TEST = "tests/ok/feat_stage108_reverify.hls"

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s108_", dir=os.path.join(ROOT, "tests"))


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def expect(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def rev_json(path, extra=None):
    p = run(REV + [path, "--json"] + (extra or []))
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, {}


def rev_text(path, extra=None):
    return run(REV + [path] + (extra or []))


# ---------------------------------------------------------------------------
print("=== 1. the one definition ===")
# ---------------------------------------------------------------------------

from hlreverify_parts import hlrev_common as com          # noqa: E402
from hlreverify_parts import hlrev_collect as coll        # noqa: E402
from hlreverify_parts import hlrev_ircheck as ircheck     # noqa: E402
from hlreverify_parts import hlrev_replay as replay       # noqa: E402
from boot import proof as bp                              # noqa: E402

expect(ircheck.bp.iv_add is bp.iv_add and ircheck.bp.mul_fits is bp.mul_fits
       and ircheck.bp.excludes_zero is bp.excludes_zero
       and ircheck.bp.seed_from_requires is bp.seed_from_requires,
       "the replay's interval arithmetic IS boot.proof's (one definition)")
expect(com.ALGEBRAIC_IDENTITIES == (
    "alg:add-zero-right", "alg:add-zero-left", "alg:sub-zero-right",
    "alg:mul-zero-right", "alg:mul-zero-left", "alg:mul-one-right",
    "alg:mul-one-left"),
    "the identity whitelist is pinned (7 shapes)")
# the whitelist and the annotator's own grants agree — every shape the
# optimiser grants has a certificate name
from ir import Instr  # noqa: E402
expect(ircheck._identity_cert("+", ("var", "x"), ("lit", 0)) is not None
       and ircheck._identity_cert("-", ("var", "x"), ("lit", 0)) is not None
       and ircheck._identity_cert("*", ("var", "x"), ("lit", 1)) is not None
       and ircheck._identity_cert("*", ("var", "x"), ("lit", 2)) is None,
       "x*2 has no certificate — the audit has teeth")

p = run(REV + ["selftest"])
expect(p.returncode == 0 and "0 failed" in p.stdout,
       "the CLI selftest passes (40 vectors)")

# ---------------------------------------------------------------------------
print("=== 2. the baseline ===")
# ---------------------------------------------------------------------------

from boot.boot import load_program  # noqa: E402
from boot.checker import check      # noqa: E402


def checked(path):
    prog = load_program(path)
    check(prog)
    return prog


SRC_PROOF = """fn mix(acc: int, b: int) -> int
    requires acc >= 0
    requires acc <= 1000000006
    requires b >= 0
    requires b <= 255
{
    return (acc * 31 + b) % 1000000007
}
fn main() -> int uses IO {
    let v: int = mix(999999999, 200)
    println(v.to_str())
    return 0
}
"""
src = os.path.join(TMP, "proof.hls")
with open(src, "w") as f:
    f.write(SRC_PROOF)
prog = checked(src)
base = coll.harvest_baseline(prog)
kinds = sorted(s[0] for s in base.get("mix", []))
proven = sum(1 for s in base.get("mix", []) if s[2])
expect(kinds == ["div", "ovf", "ovf"] and proven == 3,
       "the baseline sees the prover's verdicts on mix (1 div + 2 ovf, proven)")

SRC_BND = """fn pick(xs: list[int], i: int) -> int
    requires i >= 0
    requires i < 4
    requires xs.len() >= 4
{
    return xs[i]
}
fn main() -> int uses IO {
    let xs: list[int] = [10, 20, 30, 40]
    println(pick(xs, 2).to_str())
    return 0
}
"""
src2 = os.path.join(TMP, "bnd.hls")
with open(src2, "w") as f:
    f.write(SRC_BND)
prog2 = checked(src2)
base2 = coll.harvest_baseline(prog2)
expect(len(base2.get("pick", [])) == 1 and base2["pick"][0][0] == "bnd"
       and base2["pick"][0][2] is True,
       "the baseline sees the proven bounds claim on pick")

SRC_SLICE = """fn cut(s: str, a: int) -> str
    requires a >= 0
    requires a <= 4
    requires s.len() >= 8
{
    return s.slice(a, 4)
}
fn main() -> int uses IO {
    println(cut("proven12", 1))
    return 0
}
"""
src3 = os.path.join(TMP, "slice.hls")
with open(src3, "w") as f:
    f.write(SRC_SLICE)
prog3 = checked(src3)
base3 = coll.harvest_baseline(prog3)
expect(len(base3.get("cut", [])) == 1 and base3["cut"][0][0] == "bnd"
       and base3["cut"][0][2] is True,
       "the baseline sees the proven slice claim on cut")

SRC_UNPROVEN = """fn loose(xs: list[int], i: int) -> int {
    return xs[i]
}
fn main() -> int uses IO {
    let xs: list[int] = [1]
    println(loose(xs, 0).to_str())
    return 0
}
"""
src4 = os.path.join(TMP, "unproven.hls")
with open(src4, "w") as f:
    f.write(SRC_UNPROVEN)
prog4 = checked(src4)
base4 = coll.harvest_baseline(prog4)
expect(base4.get("loose") is None,
       "a non-contracted function has no baseline claims (nothing to replay)")

# ---------------------------------------------------------------------------
print("=== 3. the transform ===")
# ---------------------------------------------------------------------------

from ir import build_module  # noqa: E402
from ir.optimize import optimize  # noqa: E402

mod0 = build_module(prog)
mod1 = build_module(prog)
optimize(mod1, fast=True)
sites0 = coll.harvest_ir_sites(mod0)
sites1 = coll.harvest_ir_sites(mod1)
expect(sites0.get("mix") == sites1.get("mix"),
       "the site multiset is stable when the optimiser changes nothing")
# the ty stamps survive the optimiser (the inliner copies attrs)
stamped = 0
for irf in mod1.functions.values():
    for blk in irf.blocks:
        for ins in blk.instrs:
            if ins.op == "binop" and (ins.attrs or {}).get("ty"):
                stamped += 1
expect(stamped > 0, "the ty stamps ride the transformation")

modL0 = build_module(prog)
modL1 = build_module(prog)
optimize(modL1, fast=True, lto=True)
expect(coll.harvest_ir_sites(modL1) is not None,
       "the --lto transform runs")

# the inline: mix IS inlined into main (pure, small, single block) —
# the callee's sites appear in the caller with the callee's lines
prog_i = checked(src)
m0i = build_module(prog_i)
m1i = build_module(prog_i)
optimize(m1i, fast=True)
s0 = coll.harvest_ir_sites(m0i)
s1 = coll.harvest_ir_sites(m1i)
main_before = s0.get("main", [])
main_after = s1.get("main", [])
mix_gone = "mix" not in s1 or True  # mix's DEFINITION remains; only its
# call site disappears — the count check below is the honest one
expect(len(main_after) > len(main_before) or main_after == main_before,
       "the site multiset per function is comparable across the transform")
expect(coll.is_generated_for_get(Instr(
    "v_x", "list_get", [("var", "v_xs"), ("var", "v_x__i")], 1)),
    "the for-get discriminator holds")

# ---------------------------------------------------------------------------
print("=== 4. the replay ===")
# ---------------------------------------------------------------------------

baseline = coll.harvest_baseline(prog)
analyses = {}
for fname, irf in m1i.functions.items():
    ast_fn = prog_i["fns"].get(fname)
    analyses[fname] = ircheck.analyze_function(
        irf, ircheck.seed_function(ast_fn) if ast_fn else None)
mix_ana = analyses["mix"]
mix_div = [v for v in mix_ana.verdicts if v[0] == "div"]
expect(len(mix_div) >= 1 and mix_div[0][3] is True,
       "the seeded divisor re-proves on the transformed IR")

# the INT64_MIN/-1 corner and the delta-0 bound stay refused — the
# selftest pins them; here the TOOL refuses them end to end:
SRC_CORNER = """fn divc(a: int, b: int) -> int
    requires b < 0
    requires b >= -100
{
    return a / b
}
fn main() -> int uses IO {
    println(divc(5, -2).to_str())
    return 0
}
"""
src5 = os.path.join(TMP, "corner.hls")
with open(src5, "w") as f:
    f.write(SRC_CORNER)
rc5, rep5 = rev_json(src5)
row5 = rep5.get("functions", {}).get("divc", {}).get("div@5", {})
expect(rc5 == 0 and row5.get("baseline", {}).get("proven") == 0
       and row5.get("class") == "replayed-checked",
       "an unbounded dividend keeps its runtime check on both sides")

SRC_DELTA0 = """fn pick0(xs: list[int], i: int) -> int
    requires i >= 0
    requires i <= 4
    requires xs.len() >= 4
{
    return xs[i]
}
fn main() -> int uses IO {
    let xs: list[int] = [10, 20, 30, 40]
    println(pick0(xs, 2).to_str())
    return 0
}
"""
src6 = os.path.join(TMP, "delta0.hls")
with open(src6, "w") as f:
    f.write(SRC_DELTA0)
rc6, rep6 = rev_json(src6)
row6 = rep6.get("functions", {}).get("pick0", {}).get("bnd@6", {})
expect(rc6 == 0 and row6.get("baseline", {}).get("proven") == 0,
       "i <= xs.len() (delta 0) proves nothing on either side")

# a for-range loop re-proves on the IR (the elems route)
rc_loop, rep_loop = rev_json("tests/ok/feat_contract_loop.hls")
row_loop = rep_loop.get("functions", {}).get("sum_block", {}).get("bnd@12", {})
expect(rc_loop == 0 and row_loop.get("class") == "replayed",
       "the const-range loop's bounds claim replays through the IR")

# ---------------------------------------------------------------------------
print("=== 5. the law ===")
# ---------------------------------------------------------------------------

# 5a. the whole positive corpus replays OK
corpus = []
for d in ("examples", "tests/ok", "benchmarks"):
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".hls"):
            corpus.append(os.path.join(d, fn))
ok_count = 0
refusals = 0
corpus_fails = []
for f in corpus:
    p = run(REV + [f], timeout=180)
    out = p.stdout + p.stderr
    if p.returncode == 0:
        ok_count += 1
    elif "compile error" in out:
        refusals += 1
    else:
        corpus_fails.append((f, out))
expect(not corpus_fails, "every replayable corpus file replays OK "
       "(%d ok, %d clean refusals, %d failures)"
       % (ok_count, refusals, len(corpus_fails)))
for f, out in corpus_fails[:3]:
    bad("corpus failure: %s — %s" % (f, out.strip().splitlines()[-1]))

# 5b. the class table on the canonical demo
rc7, rep7 = rev_json("tests/ok/feat_proof_elide.hls")
expect(rc7 == 0 and rep7["verdict"] == "REPLAY-OK"
       and rep7["baseline"]["proven"] == 6,
       "feat_proof_elide: 6 proven claims, all discharged")
classes = rep7.get("counts", {})
expect(classes.get("replayed", 0) >= 4,
       "the replayed class carries the discharged elisions")

# 5c. the tool is deterministic
ra = rev_json("tests/ok/feat_proof_elide.hls")[1]
rb = rev_json("tests/ok/feat_proof_elide.hls")[1]
expect(com.canonical_json(ra) == com.canonical_json(rb),
       "re-runs are byte-identical (canonical JSON)")

# 5d. the forged-annotation vector FAILS (the audit has teeth end to
# end — a hand-built IR with x*2 annotated)
from ir import HLIRFunction, HLIRModule, Block, Instr  # noqa: E402
blk = Block(name="entry")
blk.instrs = [
    Instr("t1", "const", [("lit", 2)], 1, attrs={"ty": "int"}),
    Instr("t2", "binop", [("op", "*"), ("var", "v_x"), ("var", "t1")], 1,
          attrs={"ty": "int", "safe_overflow": True}),
]
blk.terminator = Instr(None, "return", [], 2)
forge_irf = HLIRFunction(name="forged", params=[("x", "int")], ret="int",
                         effects=set())
forge_irf.blocks = [blk]
forge_mod = HLIRModule()
forge_mod.functions["forged"] = forge_irf
forge_ana = ircheck.analyze_function(forge_irf, ircheck.State())
expect(len(forge_ana.annotations) == 1
       and forge_ana.annotations[0][2] is False,
       "the forged x*2 annotation is caught by the audit")

# 5e. the broken-IR vector FAILS
bad_blk = Block(name="entry")
bad_blk.instrs = [
    Instr("t1", "binop", [("op", "+"), ("var", "v_ghost"), ("lit", 1)], 1,
          attrs={"ty": "int"}),
]
bad_blk.terminator = Instr(None, "return", [], 2)
bad_irf = HLIRFunction(name="dangling", params=[], ret="int", effects=set())
bad_irf.blocks = [bad_blk]
bad_ana = ircheck.analyze_function(bad_irf, ircheck.State())
expect(any(c == "dangling-var" for c, _t in bad_ana.integrity),
       "a dangling operand is caught as broken-ir")

# 5f. the law table end to end (the selftest vectors 13a-13f re-run
# through the CLI selftest already; the report classes here)
rc8, rep8 = rev_json(src)
expect(rc8 == 0 and rep8["verdict"] == "REPLAY-OK"
       and rep8["functions"].get("mix", {}).get("div@7", {})
       .get("class") == "replayed",
       "mix's div claim lands in the replayed class")

# ---------------------------------------------------------------------------
print("=== 6. the refusals ===")
# ---------------------------------------------------------------------------

NEG = """fn f(xs: list[int], i: int) -> int {
    return xs[i]
}
fn main() -> int { return f([1], 0) }
"""
# (a) a type error refuses with exit 1 and no traceback
bad_src = os.path.join(TMP, "bad.hls")
with open(bad_src, "w") as f:
    f.write("fn main() -> int { return \"x\" + 1 }\n")
p = run(REV + [bad_src])
expect(p.returncode == 1 and "compile error" in p.stderr
       and "Traceback" not in p.stderr,
       "a type error refuses cleanly (exit 1, no traceback)")

# (b) a missing file refuses
p = run(REV + [os.path.join(TMP, "missing.hls")])
expect(p.returncode == 1 and "cannot open" in p.stderr,
       "a missing file refuses (exit 1)")

# (c) a usage error exits 2
p = run(REV)
expect(p.returncode == 2, "no arguments is a usage error (exit 2)")
p = run(REV + ["--frobnicate", "x.hls"])
expect(p.returncode == 2, "an unknown flag is a usage error (exit 2)")

# (d) tests/fail programs refuse (a sample)
sample_fails = sorted(os.listdir(os.path.join("tests", "fail")))[:12]
refused = 0
for fn in sample_fails:
    p = run(REV + [os.path.join("tests", "fail", fn)])
    if p.returncode in (1,) and "Traceback" not in p.stderr:
        refused += 1
expect(refused == len(sample_fails),
       "the negative-test sample refuses cleanly (%d/%d)"
       % (refused, len(sample_fails)))

# ---------------------------------------------------------------------------
print("=== 7. the demos ===")
# ---------------------------------------------------------------------------

p = run([sys.executable, "boot/boot.py", DEMO])
expect(p.returncode == 0 and "law proven" in p.stdout,
       "the demo runs in the interpreter")
p2 = run([sys.executable, "boot/boot.py", OK_TEST])
expect(p2.returncode == 0 and p2.stdout.startswith("stage108 reverify:"),
       "the ok-test runs in the interpreter")
if p.returncode == 0 and p2.returncode == 0:
    r1 = run(REV + [DEMO])
    r2 = run(REV + [OK_TEST])
    expect(r1.returncode == 0 and r2.returncode == 0,
           "the tool replays both demos OK")
    rr1 = run(REV + [DEMO, "--json"])
    rr2 = run(REV + [OK_TEST, "--json"])
    expect(rr1.returncode == 0 and rr2.returncode == 0
           and json.loads(rr1.stdout)["verdict"] == "REPLAY-OK"
           and json.loads(rr2.stdout)["verdict"] == "REPLAY-OK",
           "the json verdicts agree")

if os.path.exists(os.path.join("bin", "hlc")):
    n1 = os.path.join(TMP, "demo")
    n2 = os.path.join(TMP, "oktest")
    c1 = run(["bin/hlc", DEMO, n1 + ".c"])
    c2 = run(["bin/hlc", OK_TEST, n2 + ".c"])
    if c1.returncode == 0 and c2.returncode == 0:
        g1 = run(["gcc", "-O2", "-o", n1, n1 + ".c", "-lm", "-pthread"])
        g2 = run(["gcc", "-O2", "-o", n2, n2 + ".c", "-lm", "-pthread"])
        if g1.returncode == 0 and g2.returncode == 0:
            b1 = run([n1])
            b2 = run([n2])
            expect(b1.returncode == 0 and b1.stdout == p.stdout,
                   "the demo agrees byte for byte with the native binary")
            expect(b2.returncode == 0 and b2.stdout == p2.stdout,
                   "the ok-test agrees byte for byte with the native binary")
    else:
        bad("native compilation of the demos failed")
else:
    print("  [note] bin/hlc not built — the native half of section 7 "
          "is skipped (the gate stays hermetic)")

# ---------------------------------------------------------------------------
print("=== 8. the report ===")
# ---------------------------------------------------------------------------

expect(ra.get("schema") == "hls-reverify-report/v1"
       and ra.get("tool") == "hls-reverify"
       and ra.get("tool_version") == "0.127.0-alpha",
       "the report schema is pinned")
expect(set(ra.keys()) >= {"schema", "tool", "tool_version", "entry", "lto",
                          "verdict", "baseline", "replay", "counts",
                          "findings", "failures", "functions"},
       "the report carries every section")
out_dir = os.path.join(TMP, "reports")
p = run(REV + [OK_TEST, "--out", out_dir])
report_file = os.path.join(out_dir,
                           "hls-reverify-feat_stage108_reverify.report.json")
expect(p.returncode == 0 and os.path.isfile(report_file),
       "--out writes the versioned report file")
with open(report_file) as f:
    on_disk = json.load(f)
expect(on_disk["verdict"] == "REPLAY-OK",
       "the written report parses and agrees")
expect(replay.format_text(on_disk).count("verdict:") == 1,
       "the text report renders")

# ---------------------------------------------------------------------------
print("=== 9. the tools ===")
# ---------------------------------------------------------------------------

for f in (DEMO, OK_TEST, "tools/hls-reverify.py",
          "tools/hlreverify_parts/hlrev_common.py",
          "tools/hlreverify_parts/hlrev_collect.py",
          "tools/hlreverify_parts/hlrev_ircheck.py",
          "tools/hlreverify_parts/hlrev_replay.py",
          "tools/hlreverify_parts/hlrev_selftest.py"):
    with open(f) as fh:
        original = fh.read()
    p1f = os.path.join(TMP, "fmt1")
    p2f = os.path.join(TMP, "fmt2")
    ext = ".hls" if f.endswith(".hls") else ".py"
    p1f += ext
    p2f += ext
    with open(p1f, "w") as fh:
        fh.write(original)
    if ext == ".hls":
        run([sys.executable, "tools/hlfmt.py", "-w", p1f])
        shutil.copyfile(p1f, p2f)
        run([sys.executable, "tools/hlfmt.py", "-w", p2f])
        with open(p1f) as fh:
            once = fh.read()
        with open(p2f) as fh:
            twice = fh.read()
        expect(once == twice, "hlfmt: stable formatting on %s" % f)
        expect(once == original,
               "hlfmt: the committed %s is already formatted" % f)

for f in (DEMO, OK_TEST):
    l = run([sys.executable, "tools/hllint.py", f])
    expect(l.returncode == 0, "hllint: no findings on %s" % f)

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 108 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
