#!/usr/bin/env python3
"""Stage 98 acceptance gate — refinement types (lightweight, opt-in).

Run with `make refine-acceptance` (or
`python3 tests/refine_acceptance.py`).

Seven sections:

  1. the parser        — every lightweight position accepts `where`,
                         every non-lightweight position rejects it
  2. the checker       — predicate validation: bool-typed, `self`-only,
                         pure; constant violations are compile errors at
                         lets / args / returns / fields / defaults
  3. the guards        — the generated C guards every runtime boundary
                         (fn entry, return, let, assign, constructor)
  4. the proof         — proven sites are elided (constant value,
                         same-pred flow); the unprovable sites keep
                         their guards; `where self != 0` + a bounded
                         dividend elides the division check under
                         -O fast
  5. the differential  — interpreter vs native (default AND -O fast)
                         byte-identical, including the runtime-violation
                         panics (exit 101, nothing printed)
  6. the demo          — examples/refine_demo.hls agrees everywhere
  7. the tools         — hlfmt stable, hllint clean on the new sources
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from boot.boot import load_program            # noqa: E402
from boot.checker import check                # noqa: E402

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_ref_", dir=os.path.join(ROOT, "tests"))

DEMO = "examples/refine_demo.hls"


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def check_deny(name, src, expect):
    """The source must be rejected with the expected message."""
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        f.write(src)
    proc = subprocess.run(
        [sys.executable, "boot/boot.py", "--check", path],
        capture_output=True, text=True)
    err = proc.stderr + proc.stdout
    if proc.returncode == 1 and expect in err:
        ok("%s rejected (%s)" % (name, expect))
    else:
        bad("%s: rc=%d expected '%s' in: %s"
            % (name, proc.returncode, expect, err[:220]))


def compile_native(hlc, src, out_c, flags=None):
    cmd = [hlc] + (flags or []) + [src, out_c]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0


def run_native(binary):
    proc = subprocess.run([binary], capture_output=True, text=True, timeout=120)
    return proc.stdout, proc.returncode


def run_interp(src):
    proc = subprocess.run(
        [sys.executable, "boot/boot.py", src],
        capture_output=True, text=True, timeout=120)
    return proc.stdout, proc.returncode


print("=== 1. the parser: lightweight positions accept, others reject ===")
good = """fn f(a: int where self > 0) -> int where self >= 0 {
    let b: int where self >= 0 = a
    return b
}
fn main() -> int { return f(1) }
"""
good_path = os.path.join(TMP, "good.hls")
with open(good_path, "w") as gf:
    gf.write(good)
try:
    prog = load_program(good_path)
    check(prog)
    ok("param/ret/let refinements parse and check")
except Exception as e:  # noqa: BLE001
    bad("param/ret/let refinements rejected: %s" % e)

check_deny("pos_container.hls",
           "fn s(xs: list[int where self > 0]) -> int { return 0 }\n"
           "fn main() -> int { return s([1]) }\n",
           "container element types")
check_deny("pos_typearg.hls",
           "struct B[T] { v: T }\n"
           "fn main() -> int { let b: B[int where self > 0] = B{ v: 1 } return 0 }\n",
           "type arguments")
check_deny("pos_forvar.hls",
           "fn main() -> int {\n"
           "    for x: int where self > 0 in [1, 2] { }\n"
           "    return 0\n}\n",
           "loop variables")
check_deny("pos_extern.hls",
           'extern "C" {\n    fn puts(s: str where self.len() > 0) -> int uses IO\n}\n'
           "fn main() -> int { return 0 }\n",
           "extern declarations")
check_deny("pos_nonprim.hls",
           "struct P { x: int }\n"
           "fn f(p: P where self.x > 0) -> int { return 0 }\n"
           "fn main() -> int { return f(P{ x: 1 }) }\n",
           "only supported on primitive types")

print("=== 2. the checker: predicate validation + constant violations ===")
check_deny("chk_scope.hls",
           "fn f(x: int where self > y) -> int { return x }\n"
           "fn main() -> int { return f(1) }\n",
           "may only reference 'self'")
check_deny("chk_pure.hls",
           "fn g(x: int) -> int { return x }\n"
           "fn f(x: int where g(1) > 0) -> int { return x }\n"
           "fn main() -> int { return f(1) }\n",
           "predicates must be pure")
check_deny("chk_bool.hls",
           "fn f(x: int where self + 1) -> int { return x }\n"
           "fn main() -> int { return f(1) }\n",
           "must be bool")
check_deny("chk_let.hls",
           "fn main() -> int {\n"
           "    let x: int where self > 0 = 0 - 5\n"
           "    return 0\n}\n",
           "value -5 does not satisfy")
check_deny("chk_arg.hls",
           "fn h(x: int where self > 0) -> int { return x }\n"
           "fn main() -> int { return h(0) }\n",
           "value 0 does not satisfy")
check_deny("chk_ret.hls",
           "fn f() -> int where self >= 0 { return 0 - 1 }\n"
           "fn main() -> int { return f() }\n",
           "value -1 does not satisfy")
check_deny("chk_field.hls",
           "struct B { v: int where self >= 0 }\n"
           "fn main() -> int { let b: B = B{ v: 0 - 1 } return 0 }\n",
           "field 'B.v'")
check_deny("chk_default.hls",
           "struct B { v: int where self >= 0 = 0 - 1 }\n"
           "fn main() -> int { let b: B = B{ v: 1 } return 0 }\n",
           "default of field 'B.v'")
check_deny("chk_assign.hls",
           "fn main() -> int {\n"
           "    let mut x: int where self >= 0 = 1\n"
           "    x = 0 - 2\n"
           "    return 0\n}\n",
           "value -2 does not satisfy")

print("=== 3. the guards: every runtime boundary emits its check ===")
ok_src = "tests/ok/feat_stage98_refine.hls"
nat_c = os.path.join(TMP, "refine.c")
hlc = os.path.join(TMP, "hlc_gate")
subprocess.run([sys.executable, "boot/boot.py", "src/hlc.hls", "src/hlc.hls",
                nat_c], capture_output=True)
subprocess.run(["gcc", "-O2", "-o", hlc, nat_c, "-lm", "-pthread"],
               capture_output=True)
if not os.path.exists(hlc):
    bad("gate hlc build failed")
else:
    ok("gate hlc built")
    out_c = os.path.join(TMP, "refine_gen.c")
    if compile_native(hlc, ok_src, out_c):
        c = open(out_c).read()
        guards = c.count("hl_die(\"refinement violation:")
        if guards >= 7:
            ok("runtime guards emitted (%d sites)" % guards)
        else:
            bad("too few runtime guards emitted (%d)" % guards)
        if "parameter 'amt' of 'withdraw' must satisfy 'self > 0'" in c:
            ok("fn-entry guard present")
        else:
            bad("fn-entry guard missing")
        if "field 'Account.balance' must satisfy 'self >= 0'" in c and \
           "usf_new_Account" in c:
            ok("constructor guard present")
        else:
            bad("constructor guard missing")
        if "return " in c and "refinement violation: return value of 'abs_diff'" in c:
            ok("return guard present")
        else:
            bad("return guard missing")
    else:
        bad("native compile of %s failed" % ok_src)

print("=== 4. the proof: proven sites elided, unproven kept ===")
# Elisions are PROOF-based, not mode-based: a site the checker/interval
# pass proves (constant value, same-pred flow, interval facts) carries
# no guard in EITHER build; unprovable sites keep their guard in both.
fast_src = "tests/ok/feat_stage98_refine_fast.hls"
if os.path.exists(hlc):
    fast_c = os.path.join(TMP, "refine_fast.c")
    if compile_native(hlc, fast_src, fast_c):
        fc = open(fast_c).read()
        for var in ("a", "b", "c"):
            if ("variable '%s' must satisfy" % var) not in fc:
                ok("proven let '%s' guard elided" % var)
            else:
                bad("proven let '%s' guard NOT elided" % var)
        for var in ("d", "e"):
            if ("variable '%s' must satisfy" % var) in fc:
                ok("unprovable let '%s' guard kept" % var)
            else:
                bad("unprovable let '%s' guard wrongly elided" % var)
    else:
        bad("fast-source compile failed")
    fast2_c = os.path.join(TMP, "refine_fast2.c")
    if compile_native(hlc, ok_src, fast2_c, ["--fast"]):
        n_def = open(out_c).read().count("hl_die(\"refinement violation:")
        n_fast = open(fast2_c).read().count("hl_die(\"refinement violation:")
        if n_def == n_fast:
            ok("guard count mode-independent (%d == %d)" % (n_def, n_fast))
        else:
            bad("guard count differs by mode (%d vs %d)" % (n_def, n_fast))
    else:
        bad("fast compile failed")

    demo_c = os.path.join(TMP, "demo_fast.c")
    if compile_native(hlc, DEMO, demo_c, ["--fast"]):
        dc = open(demo_c).read()
        if "return (u_num / u_den);" in dc \
                and "refinement violation: parameter 'amount' of 'apply'" in dc:
            ok("refinements compose into a proof (division elided, guards kept)")
        else:
            bad("demo proof/elision pattern wrong")
    else:
        bad("demo fast compile failed")

print("=== 5. the differential: interp vs native (default + fast) ===")
for f in ["tests/ok/feat_stage98_refine.hls",
          "tests/ok/feat_stage98_refine_fast.hls",
          "tests/ok/feat_stage98_refine_panic.hls",
          "tests/ok/feat_stage98_refine_sound.hls"]:
    if not os.path.exists(hlc):
        break
    iout, irc = run_interp(f)
    nc = os.path.join(TMP, "d.c")
    nb = os.path.join(TMP, "d.bin")
    name = os.path.basename(f)
    if compile_native(hlc, f, nc):
        subprocess.run(["gcc", "-O2", "-o", nb, nc, "-lm", "-pthread"],
                       capture_output=True)
        nout, nrc = run_native(nb)
        if (iout, irc) == (nout, nrc):
            ok("%s: interp == native (rc=%d)" % (name, irc))
        else:
            bad("%s diverges: interp=%d/%r native=%d/%r"
                % (name, irc, iout[-40:], nrc, nout[-40:]))
        fc2 = os.path.join(TMP, "d_fast.c")
        fb = os.path.join(TMP, "d_fast.bin")
        if compile_native(hlc, f, fc2, ["--fast"]):
            subprocess.run(["gcc", "-O2", "-o", fb, fc2, "-lm", "-pthread"],
                           capture_output=True)
            fout, frc = run_native(fb)
            if (iout, irc) == (fout, frc):
                ok("%s: interp == -O fast (rc=%d)" % (name, frc))
            else:
                bad("%s -O fast diverges: interp=%d/%r fast=%d/%r"
                    % (name, irc, iout[-40:], frc, fout[-40:]))
        else:
            bad("%s fast compile failed" % name)
    else:
        bad("%s native compile failed" % name)

print("=== 6. the demo ===")
try:
    prog = load_program(DEMO)
    check(prog)
    ok("refine_demo checks clean")
except Exception as e:  # noqa: BLE001
    bad("refine_demo rejected: %s" % e)
if os.path.exists(hlc):
    iout, irc = run_interp(DEMO)
    dc2 = os.path.join(TMP, "demo.c")
    db = os.path.join(TMP, "demo.bin")
    if compile_native(hlc, DEMO, dc2):
        subprocess.run(["gcc", "-O2", "-o", db, dc2, "-lm", "-pthread"],
                       capture_output=True)
        nout, nrc = run_native(db)
        if (iout, irc) == (nout, nrc) and "gift:    main (545)" in nout:
            ok("refine_demo: interp == native, output pinned")
        else:
            bad("refine_demo diverges or output drifted")
    else:
        bad("refine_demo native compile failed")

print("=== 7. the tools ===")
fmt = subprocess.run([sys.executable, "tools/hlfmt.py", DEMO],
                     capture_output=True, text=True)
fmt2 = subprocess.run([sys.executable, "tools/hlfmt.py", DEMO],
                      capture_output=True, text=True)
if fmt.stdout == fmt2.stdout and fmt.returncode == 0:
    ok("hlfmt stable on the demo")
else:
    bad("hlfmt unstable on the demo")
lint = subprocess.run([sys.executable, "tools/hllint.py", DEMO],
                      capture_output=True, text=True)
if lint.returncode == 0:
    ok("hllint clean on the demo")
else:
    bad("hllint failed on the demo: %s" % lint.stderr[:160])

print("==========================================")
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
print("==========================================")
sys.exit(1 if FAIL else 0)
