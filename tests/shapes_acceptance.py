#!/usr/bin/env python3
"""Stage 100 acceptance gate — the separation-logic fragment (heap
shapes).

Run with `make shapes-acceptance` (or `python3 tests/shapes_acceptance.py`).

Six sections:

  1. the engine          — alias classes, separation verdicts and shape
                           triples on a battery of functions: every
                           PROVEN shape, every refusal and every access
                           count pinned exactly
  2. the soundness flips — the loops a naive shape prover would get
                           WRONG (descending steps, the conditional bump
                           that lands the cursor at len, the non-strict
                           condition, off-cursor and through-alias
                           writes, the push inside the loop, the
                           captured length): none of them may be
                           claimed
  3. the obligations     — every verified segment is z3-unsat, the
                           .shape.smt2 dump re-decides clean from the
                           file alone
  4. the CLI             — `hlprove --shapes` reports the demo's
                           shapes, refusals and totals; the honest
                           no-solver path claims nothing; --z3 --cvc5
                           is still a caller error
  5. the demo            — heap_demo.hls: interpreter and native output
                           agree; the fragment changes nothing
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
import tools.hlprove as hl                    # noqa: E402

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s100_", dir=os.path.join(ROOT, "tests"))

DEMO = "examples/heap_demo.hls"
OK_TEST = "tests/ok/feat_stage100_heaps.hls"


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


def shapes_on(source, name):
    """load + check + collect + verify on a scratch program."""
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        f.write(source)
    program = load_program(path)
    check(program)
    fs_list = _proof.collect_shapes(program)
    _proof.verify_shapes(fs_list, DECIDER)
    return fs_list


def fn_shapes(fs_list, fn_key):
    for fs in fs_list:
        if fs.fn_key == fn_key:
            return fs
    return None


def loop_of(fs, line):
    for ls in fs.loops:
        if ls.line == line:
            return ls
    return None


def sep_pair(fs, a, b):
    for ra, rb, proven, why in fs.separation:
        if {ra, rb} == {a, b}:
            return proven, why
    return None, None


# ---------------------------------------------------------------------------
print("=== 1. the engine ===")
# ---------------------------------------------------------------------------

DECIDER = make_decider()
if DECIDER is None:
    print("  [FAIL] no z3 binary and no z3-solver module — install one:"
          " pip install z3-solver (or put a z3 binary on PATH)")
    sys.exit(1)
ok("a decider is available (z3 binary or z3-solver module)")

# -- the flagship for-range walk is proven, accesses counted --------------
fs_list = shapes_on("""
fn fill(xs: list[int]) -> int {
    for i: int in range(0, xs.len()) {
        xs[i] = i * 2
    }
    return 0
}
fn main() -> int { return 0 }
""", "fill.hls")
ls = loop_of(fn_shapes(fs_list, "fill"), 3)
if ls is not None and ls.status == "proven" \
        and ls.shape_text == "lseg(xs, 0, i) * cell(xs, i) * lseg(xs, i + 1, xs.len())" \
        and (ls.access_safe, ls.access_total) == (1, 1):
    ok("fill: for-range walk proven, 1/1 accesses in bounds by shape")
else:
    bad("fill: wrong verdict (%s)" % (ls.status if ls else "no loop"))

# -- the while-anchored walk with set/get at the cursor --------------------
fs_list = shapes_on("""
fn bump(xs: list[int]) -> int {
    let mut i: int = 0
    while i < len(xs) {
        xs.set(i, xs.get(i) + 1)
        i = i + 1
    }
    return 0
}
fn main() -> int { return 0 }
""", "bump.hls")
ls = loop_of(fn_shapes(fs_list, "bump"), 4)
if ls is not None and ls.status == "proven" \
        and (ls.access_safe, ls.access_total) == (2, 2):
    ok("bump: while walk via len()/set()/get() proven, 2/2 accesses")
else:
    bad("bump: wrong verdict (%s)" % (ls.status if ls else "no loop"))

# -- a conditional bump keeps the shape, loses the post-bump access -------
fs_list = shapes_on("""
fn condbump(xs: list[int], flag: bool) -> int {
    let mut i: int = 0
    while i < xs.len() {
        if flag {
            i = i + 1
        }
        xs[i] = 5
    }
    return 0
}
fn main() -> int { return 0 }
""", "condbump.hls")
ls = loop_of(fn_shapes(fs_list, "condbump"), 4)
if ls is not None and ls.status == "proven" \
        and (ls.access_safe, ls.access_total, ls.access_blocked) == (0, 1, 1):
    ok("condbump: shape proven, the post-bump access honestly blocked")
else:
    bad("condbump: wrong verdict (%s)" % (ls.status if ls else "no loop"))

# -- two walks over two roots in one function ------------------------------
fs_list = shapes_on("""
fn two(dst: list[int], src: list[int]) -> int {
    for i: int in range(0, dst.len()) {
        dst[i] = 1
    }
    let mut j: int = 0
    while j < src.len() {
        src[j] = 2
        j = j + 1
    }
    return 0
}
fn main() -> int { return 0 }
""", "two.hls")
l1 = loop_of(fn_shapes(fs_list, "two"), 3)
l2 = loop_of(fn_shapes(fs_list, "two"), 7)
if l1 is not None and l2 is not None and l1.status == "proven" \
        and l2.status == "proven":
    ok("two: both walks proven (one for-range, one while)")
else:
    bad("two: wrong verdicts (%s / %s)"
        % (l1.status if l1 else "?", l2.status if l2 else "?"))

# -- a var range start proven under its requires ----------------------------
fs_list = shapes_on("""
fn tail(xs: list[int], start: int) -> int
    requires start >= 0
{
    for i: int in range(start, xs.len()) {
        xs[i] = 7
    }
    return 0
}
fn main() -> int { return 0 }
""", "tail.hls")
ls = loop_of(fn_shapes(fs_list, "tail"), 5)
if ls is not None and ls.status == "proven" \
        and (ls.access_safe, ls.access_total) == (1, 1):
    ok("tail: var range start pinned by requires start >= 0")
else:
    bad("tail: wrong verdict (%s)" % (ls.status if ls else "no loop"))

# -- separation: fresh evidence proves, caller-owned refuses ----------------
fs_list = shapes_on("""
fn freshpair(a: list[int]) -> int {
    let buf: list[int] = [0, 0, 0]
    a[0] = 1
    buf[0] = 2
    return 0
}
fn clonepair(a: list[int]) -> int {
    let buf: list[int] = clone(a)
    a[0] = 1
    buf[0] = 2
    return 0
}
fn twoparams(a: list[int], b: list[int]) -> int {
    a[0] = 1
    b[0] = 2
    return 0
}
fn aliasmerge(a: list[int]) -> int {
    let b2: list[int] = a
    a[0] = 1
    b2[0] = 2
    return 0
}
fn main() -> int { return 0 }
""", "separation.hls")
fs = fn_shapes(fs_list, "freshpair")
proven, why = sep_pair(fs, "a", "buf")
if proven is True:
    ok("freshpair: own(a) * own(buf) PROVEN (a fresh allocation)")
else:
    bad("freshpair: separation not proven (%s)" % why)
fs = fn_shapes(fs_list, "clonepair")
proven, why = sep_pair(fs, "a", "buf")
if proven is True:
    ok("clonepair: own(a) * own(buf) PROVEN (clone() is a fresh heap)")
else:
    bad("clonepair: separation not proven (%s)" % why)
fs = fn_shapes(fs_list, "twoparams")
proven, why = sep_pair(fs, "a", "b")
if proven is False and "caller" in (why or ""):
    ok("twoparams: own(a) * own(b) REFUSED — caller-owned, f(xs, xs) "
       "aliases")
else:
    bad("twoparams: separation wrongly reported (proven=%s)" % proven)
fs = fn_shapes(fs_list, "aliasmerge")
if not fs.separation:
    ok("aliasmerge: one alias class — single writer, trivially separated")
else:
    bad("aliasmerge: aliasing not merged (%d verdicts)" % len(fs.separation))

# ---------------------------------------------------------------------------
print("=== 2. the soundness flips ===")
# ---------------------------------------------------------------------------

FLIPS = [
    # (name, source, loop line, pinned refusal substring)
    ("descend: i' = i - 1 lands at -1 from i == 0", """
fn descend(xs: list[int]) -> int {
    let mut i: int = 0
    while i < xs.len() {
        xs[i] = 1
        i = i - 1
    }
    return 0
}
fn main() -> int { return 0 }
""", 4, "preservation not discharged"),
    ("offcursor: xs[0] is not the cursor cell", """
fn offcursor(xs: list[int]) -> int {
    let mut i: int = 0
    while i < xs.len() {
        xs[0] = 1
        i = i + 1
    }
    return 0
}
fn main() -> int { return 0 }
""", 4, "writes off the cursor cell"),
    ("grower: push changes len inside the loop", """
fn grower(xs: list[int]) -> int {
    let mut i: int = 0
    while i < xs.len() {
        xs.push(i)
        i = i + 1
    }
    return 0
}
fn main() -> int { return 0 }
""", 4, "changes len"),
    ("captured: let n = xs.len() has no heap link", """
fn captured(xs: list[int]) -> int {
    let n: int = xs.len()
    let mut i: int = 0
    while i < n {
        xs[i] = 1
        i = i + 1
    }
    return 0
}
fn main() -> int { return 0 }
""", 5, "no heap anchor"),
    ("aliaswrite: a through-alias write is a wildcard write", """
fn aliaswrite(xs: list[int]) -> int {
    let mut i: int = 0
    while i < xs.len() {
        let ys: list[int] = xs
        ys[3] = 1
        i = i + 1
    }
    return 0
}
fn main() -> int { return 0 }
""", 4, "unknown provenance"),
    ("escaper: break ends the owned shape", """
fn escaper(xs: list[int]) -> int {
    let mut i: int = 0
    while i < xs.len() {
        if xs[i] == 0 {
            break
        }
        i = i + 1
    }
    return 0
}
fn main() -> int { return 0 }
""", 4, "escapes"),
    ("varstart: no requires, a negative start is possible", """
fn varstart(xs: list[int], start: int) -> int {
    for i: int in range(start, xs.len()) {
        xs[i] = 1
    }
    return 0
}
fn main() -> int { return 0 }
""", 3, "initiation not discharged"),
]

flip_ok = True
for name, src, line, needle in FLIPS:
    fs_list = shapes_on(src, "_flip_%d.hls" % len(FLIPS))
    ls = None
    for cand_fs in fs_list:
        cand = loop_of(cand_fs, line)
        if cand is not None:
            ls = cand
            break
    if ls is None:
        bad("%s: no loop found at line %d" % (name, line))
        flip_ok = False
        continue
    if ls.status == "proven":
        bad("%s: CLAIMED — unsound" % name)
        flip_ok = False
        continue
    reasons = ls.frame_reasons + [ls.solver_reason or "",
                                  ls.cond_reason or ""]
    if ls.status == "refused" and any(needle in r for r in reasons):
        ok("%s: refused (%s)" % (name, needle))
    else:
        bad("%s: refused but for the wrong reason (%s)"
            % (name, reasons))
        flip_ok = False
if flip_ok:
    ok("all %d soundness flips refused with pinned reasons" % len(FLIPS))

# the non-strict condition: the shape is provable, the accesses are not
fs_list = shapes_on("""
fn nonstrict(xs: list[int]) -> int {
    let mut i: int = 0
    while i <= xs.len() {
        xs[i] = 1
        i = i + 1
    }
    return 0
}
fn main() -> int { return 0 }
""", "nonstrict.hls")
ls = loop_of(fn_shapes(fs_list, "nonstrict"), 4)
if ls is not None and ls.status == "proven" \
        and ls.access_safe == 0 and ls.access_total == 1:
    ok("nonstrict: shape proven, accesses refused (i may sit at len)")
else:
    bad("nonstrict: wrong verdict (%s)" % (ls.status if ls else "no loop"))

# ---------------------------------------------------------------------------
print("=== 3. the obligations ===")
# ---------------------------------------------------------------------------

seg_count = 0
all_unsat = True
fs_list = shapes_on(open(DEMO).read(), "_demo_copy.hls")
verified = _proof.verify_shapes(fs_list, DECIDER)
for _key, pairs in verified.items():
    for _ls, segs in pairs:
        for _label, lines in segs:
            seg_count += 1
            if DECIDER(lines) != "unsat":
                all_unsat = False
if seg_count > 0 and all_unsat:
    ok("%d shape obligation segments on the demo, every one z3-unsat"
       % seg_count)
else:
    bad("obligations wrong (segments=%d, all_unsat=%s)"
        % (seg_count, all_unsat))

dump = run([sys.executable, "tools/hlprove.py", DEMO, "--shapes", "--smt"])
dumps = sorted(f for f in os.listdir("examples")
               if f.endswith(".shape.smt2"))
if dump.returncode == 0 and dumps:
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
        ok("every dumped shape obligation re-decides unsat from its file")
    else:
        bad("a dumped obligation is not unsat — the file lies")
else:
    bad("the --shapes --smt dump failed (rc=%d, files=%d)"
        % (dump.returncode, len(dumps)))

# ---------------------------------------------------------------------------
print("=== 4. the CLI ===")
# ---------------------------------------------------------------------------

out = run([sys.executable, "tools/hlprove.py", DEMO, "--shapes"])
if out.returncode != 0:
    bad("hlprove --shapes exited %d" % out.returncode)
else:
    ok("hlprove --shapes runs clean on the demo")
    txt = out.stdout
    checks = [
        ("the fragment's header is present",
         "Heap shapes (the separation-logic fragment):" in txt),
        ("the for-range walk is proven with its triple",
         "shape PROVEN (z3): lseg(xs, 0, i) * cell(xs, i)"
         in txt),
        ("the frame refusal names the offending line",
         "frame REFUSED: xs[..] = .. at line 65 writes off the cursor"
         in txt),
        ("the separation refusal names the f(xs, xs) aliasing",
         "own(a) * own(b) REFUSED" in txt
         and "f(xs, xs) aliases the parameters" in txt),
        ("the fresh-buffer separation is proven",
         "own(a) * own(buf) PROVEN" in txt),
        ("the blocked access is explained",
         "accesses NOT claimed" in txt),
        ("the totals line closes the report",
         "SHAPES TOTAL: 4 loops proven, 1 refused, 0 unverified;"
         in txt),
    ]
    for name, cond in checks:
        if cond:
            ok("cli: %s" % name)
        else:
            bad("cli: %s" % name)

plain = run([sys.executable, "tools/hlprove.py", OK_TEST, "--shapes"])
if plain.returncode == 0 and "SHAPES TOTAL:" in plain.stdout:
    ok("cli: the ok-test's shapes report cleanly (totals line present)")
else:
    bad("cli: shapes on the ok-test failed (rc=%d)" % plain.returncode)

# the honest no-solver path: shapes_report with decider=None claims nothing
from contextlib import redirect_stdout
import io
prog = load_program(DEMO)
check(prog)
buf = io.StringIO()
with redirect_stdout(buf):
    hl.shapes_report(prog, False, TMP, None, "z3")
txt = buf.getvalue()
if "nothing claimed" in txt and "0 loops proven" in txt \
        and "PROVEN (z3)" not in txt:
    ok("cli: with no decider, candidates are reported and nothing "
       "is claimed")
else:
    bad("cli: the no-solver report claims something (%r)" % txt[:200])

both = run([sys.executable, "tools/hlprove.py", DEMO, "--z3", "--cvc5"])
if both.returncode == 2:
    ok("cli: --z3 and --cvc5 together is a caller error (exit 2)")
else:
    bad("cli: --z3 --cvc5 should exit 2, got %d" % both.returncode)

# ---------------------------------------------------------------------------
print("=== 5. the demo ===")
# ---------------------------------------------------------------------------

interp = run([sys.executable, "boot/boot.py", DEMO])
if interp.returncode == 0:
    ok("the demo runs on the interpreter (rc=0)")
else:
    bad("the demo failed on the interpreter (rc=%d)" % interp.returncode)

ok_test_interp = run([sys.executable, "boot/boot.py", OK_TEST])
if ok_test_interp.returncode == 0:
    ok("the ok-test runs on the interpreter (rc=0)")
else:
    bad("the ok-test failed on the interpreter (rc=%d)"
        % ok_test_interp.returncode)

BIN = os.path.join(ROOT, "bin", "hlc")
if os.path.exists(BIN):
    for src, tag in ((DEMO, "demo"), (OK_TEST, "ok-test")):
        c_out = os.path.join(TMP, "stage100_%s.c" % tag)
        comp = run([BIN, src, c_out])
        if comp.returncode != 0:
            bad("hlc refused the %s (rc=%d)" % (tag, comp.returncode))
            continue
        exe = os.path.join(TMP, "stage100_%s" % tag)
        cc = run(["cc", "-O2", "-Werror", "-o", exe, c_out,
                  "-lm", "-pthread"])
        if cc.returncode != 0:
            bad("cc refused the generated C for the %s (rc=%d)"
                % (tag, cc.returncode))
            continue
        native = run([exe])
        expected = interp.stdout if tag == "demo" else ok_test_interp.stdout
        if native.returncode == 0 and native.stdout == expected:
            ok("the %s: native output identical to the interpreter" % tag)
        else:
            bad("the %s: native output diverges (rc=%d)"
                % (tag, native.returncode))
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
print("Stage 100 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
