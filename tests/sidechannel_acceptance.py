#!/usr/bin/env python3
"""Stage 101 acceptance gate — the cryptographic side-channel analysis.

Run with `make sidechannel-acceptance` (or
`python3 tests/sidechannel_acceptance.py`).

Eight sections:

  1. the engine          — the sink battery: branch / index / loop-bound
                           / division findings pinned exactly, the
                           constant-time idioms pinned CLEAN
  2. the soundness flips — the cases a naive taint pass gets wrong
                           (public index into a secret list, secret
                           index into a public list, the data-only
                           flows, the flow-sensitive kill, the loop
                           widening, the push taint)
  3. the propagation     — the interprocedural chain (leak at the
                           bottom, incoming edges at every level) and
                           recursion terminating the fixpoint
  4. the CLI             — `hlprove --sidechannel` on the demo: every
                           sink kind, the policy note, the totals
  5. the demo            — interpreter and native agree byte for byte
  6. the audit parity    — boot.py --audit and hlc --audit print the
                           same #[secrets(...)] block, word for word
  7. the fail tests      — both front-ends reject the malformed
                           markings with the same words
  8. the tools           — hlfmt stable, hllint clean on the new sources
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from boot.boot import load_program            # noqa: E402
from boot.checker import check                # noqa: E402
from boot.proof import analyze_sidechannel    # noqa: E402

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_sc_", dir=os.path.join(ROOT, "tests"))

DEMO = "examples/sidechannel_demo.hls"
OK_TEST = "tests/ok/feat_stage101_sidechannel.hls"

FAIL_PROGRAMS = [
    ("fail_stage101_secret_unknown_param",
     "which is not a parameter of",
     "a secret name that marks nothing hides the hazard"),
    ("fail_stage101_secret_dup",
     "appears more than once on one function",
     "two markings merge into one list, so the attr is refused"),
    ("fail_stage101_secret_empty",
     "needs at least one parameter name",
     "an empty list marks nothing"),
]


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
    """load + check + analyze a scratch program; returns the reports."""
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        f.write(source)
    program = load_program(path)
    check(program)
    return analyze_sidechannel(program)


def findings_of(reports, fn_key):
    rep = reports.get(fn_key)
    if rep is None:
        return []
    return [(f.kind, f.line, f.text) for f in rep.findings]


def has_finding(reports, fn_key, kind, text_sub):
    for kind, _line, text in findings_of(reports, fn_key):
        if kind == kind and text_sub in text:
            return True
    return False


# ---------------------------------------------------------------------------
print("=== 1. the engine ===")
# ---------------------------------------------------------------------------

# -- the INDEX sink: a secret byte selecting a table entry --------------
reports = engine_on("""
#[secrets(b)]
fn lookup(sbox: list[int], b: int) -> int {
    return sbox.get(b)
}

fn main() -> int { return 0 }
""", "lookup.hls")
fs = findings_of(reports, "lookup")
if len(fs) == 1 and fs[0][0] == "index" and "sbox.get(b)" in fs[0][2]:
    ok("lookup: one INDEX finding naming the sbox.get call")
else:
    bad("lookup: wrong findings — %s" % fs)

# -- the BRANCH sink: the early-exit compare ------------------------------
reports = engine_on("""
#[secrets(key)]
fn cmp(key: str, probe: str) -> int {
    let mut i: int = 0
    while i < key.len() {
        if key.byte_at(i) != probe.byte_at(i) {
            return i
        }
        i = i + 1
    }
    return -1
}

fn main() -> int { return 0 }
""", "cmp.hls")
rep = reports.get("cmp")
fs = findings_of(reports, "cmp")
if len(fs) == 1 and fs[0][0] == "branch" and "key.byte_at(i)" in fs[0][2]:
    ok("cmp: one BRANCH finding on the byte compare (the while stays "
       "public: len is policy)")
else:
    bad("cmp: wrong findings — %s" % fs)
if rep is not None and rep.length_uses == 1:
    ok("cmp: the len() use is counted as a policy note")
else:
    bad("cmp: the len() policy note is missing — %s"
        % (rep.length_uses if rep else "?"))

# -- the LOOP-BOUND and DIVISION sinks -----------------------------------
reports = engine_on("""
#[secrets(rounds)]
fn workload(rounds: int) -> int {
    let mut acc: int = 0
    for i: int in range(0, rounds) {
        acc = acc + i
    }
    acc = acc % rounds
    return acc
}

fn main() -> int { return 0 }
""", "workload.hls")
fs = findings_of(reports, "workload")
kinds = sorted(k for k, _l, _t in fs)
if kinds == ["division", "loop-bound"]:
    ok("workload: LOOP-BOUND on range(0, rounds) and DIVISION on the "
       "modulo")
else:
    bad("workload: wrong findings — %s" % fs)

# -- the constant-time idiom stays CLEAN ----------------------------------
reports = engine_on("""
#[secrets(mask)]
fn pick(mask: int, a: int, b: int) -> int {
    return a * mask + b * (1 - mask)
}

fn main() -> int { return 0 }
""", "pick.hls")
rep = reports.get("pick")
if rep is not None and not rep.findings and rep.returns_secret:
    ok("pick: CLEAN (data-only flow), the return is still marked secret")
else:
    bad("pick: wrong verdict — %s / %s"
        % (findings_of(reports, "pick"),
           rep.returns_secret if rep else "no report"))

# -- the match sink --------------------------------------------------------
reports = engine_on("""
enum Op {
    Add,
    Mul,
}

fn pick_op(s: int) -> Op {
    if s > 10 {
        return Op.Add
    }
    return Op.Mul
}

#[secrets(s)]
fn apply(s: int) -> int {
    let op: Op = pick_op(s)
    return match op {
        Op.Add => s + 1,
        Op.Mul => s * 2
    }
}

fn main() -> int { return 0 }
""", "matchsink.hls")
fs = findings_of(reports, "apply")
if len(fs) == 1 and fs[0][0] == "branch" and fs[0][2] == "op":
    ok("match: a match on a secret-derived scrutinee (in return "
       "position) is a BRANCH sink")
else:
    bad("match: wrong findings — %s" % fs)
if has_finding(reports, "pick_op", "branch", "s > 10"):
    ok("match: the helper that derived the scrutinee leaks too")
else:
    bad("match: the deriving helper's leak is missing — %s"
        % findings_of(reports, "pick_op"))

# ---------------------------------------------------------------------------
print("=== 2. the soundness flips ===")
# ---------------------------------------------------------------------------
# Every case below is one a naive source-taint pass would get WRONG.

# a PUBLIC index into a SECRET list: the address is the constant, only
# the VALUE is secret — no INDEX finding (and no branch).
reports = engine_on("""
#[secrets(xs)]
fn read0(xs: list[int]) -> int {
    return xs.get(0)
}

fn main() -> int { return 0 }
""", "read0.hls")
program = load_program(os.path.join(TMP, "read0.hls"))
check(program)
rep = analyze_sidechannel(program).get("read0")
if rep is not None and not rep.findings and rep.returns_secret:
    ok("read0: a public index into a secret list is data flow only")
else:
    bad("read0: wrong verdict — %s" % findings_of(reports, "read0"))

# a SECRET index into a PUBLIC list: the address follows the secret —
# the INDEX finding fires even though the list is public.
reports = engine_on("""
#[secrets(idx)]
fn sneaky(idx: int, pub_table: list[int]) -> int {
    return pub_table.get(idx)
}

fn main() -> int { return 0 }
""", "sneaky.hls")
if has_finding(reports, "sneaky", "index", "pub_table.get(idx)"):
    ok("sneaky: a secret index into a public table is an INDEX leak")
else:
    bad("sneaky: the INDEX leak is missing — %s"
        % findings_of(reports, "sneaky"))

# the flow-sensitive kill: a variable that CARRIED a secret and was
# reassigned a public value before the branch — no leak.
reports = engine_on("""
#[secrets(s)]
fn killed(s: int) -> int {
    let mut x: int = s
    x = 0
    if x > 0 {
        return 1
    }
    return 0
}

fn main() -> int { return 0 }
""", "killed.hls")
if not findings_of(reports, "killed"):
    ok("killed: the reassignment kills the marking before the branch")
else:
    bad("killed: false positive on a killed flow — %s"
        % findings_of(reports, "killed"))

# the loop widening: the variable becomes secret INSIDE the loop; the
# branch after the loop must still see it.
reports = engine_on("""
#[secrets(s)]
fn widened(s: int) -> int {
    let mut t: int = 0
    let mut j: int = 0
    while j < 10 {
        t = s
        j = j + 1
    }
    if t > 0 {
        return 1
    }
    return 0
}

fn main() -> int { return 0 }
""", "widened.hls")
if has_finding(reports, "widened", "branch", "t > 0"):
    ok("widened: a secret assigned inside the loop reaches the branch "
       "after it")
else:
    bad("widened: the loop widening missed the branch — %s"
        % findings_of(reports, "widened"))

# the push taint: pushing a secret element taints the container, and a
# later branch on its content is a leak.
reports = engine_on("""
#[secrets(s)]
fn pushed(s: int) -> int {
    let xs: list[int] = []
    xs.push(s)
    if xs.get(0) > 0 {
        return 1
    }
    return 0
}

fn main() -> int { return 0 }
""", "pushed.hls")
if has_finding(reports, "pushed", "branch", "xs.get(0) > 0"):
    ok("pushed: the push taints the list; the content branch is a leak")
else:
    bad("pushed: the push taint was missed — %s"
        % findings_of(reports, "pushed"))

# a join: secret on one arm only — the joined variable stays secret.
reports = engine_on("""
#[secrets(s)]
fn joined(s: int, pub_flag: bool) -> int {
    let mut x: int = 0
    if pub_flag {
        x = s
    } else {
        x = 7
    }
    if x > 3 {
        return 1
    }
    return 0
}

fn main() -> int { return 0 }
""", "joined.hls")
if has_finding(reports, "joined", "branch", "x > 3"):
    ok("joined: the if/else join keeps the secret arm secret")
else:
    bad("joined: the join missed the secret arm — %s"
        % findings_of(reports, "joined"))

# ---------------------------------------------------------------------------
print("=== 3. the propagation ===")
# ---------------------------------------------------------------------------

# three helper levels; the leak is found INSIDE the deepest fn, with
# the incoming chain printed at every level.
reports = engine_on("""
fn deep(x: int) -> int {
    let mut y: int = 0
    if x > 64 {
        y = x - 64
    } else {
        y = x + 1
    }
    return y
}

fn mid(k: int) -> int {
    return deep(k * 3)
}

#[secrets(master)]
fn top(master: int) -> int {
    return mid(master)
}

fn main() -> int { return 0 }
""", "chain.hls")
if has_finding(reports, "deep", "branch", "x > 64"):
    ok("chain: the leak is reported at the deepest fn's own line")
else:
    bad("chain: the deep leak is missing — %s"
        % findings_of(reports, "deep"))
rep = reports.get("mid")
if rep is not None and any(c == "top" and p == "k"
                           for c, p, _l in rep.incoming):
    ok("chain: mid reports the incoming secret 'k' from top")
else:
    bad("chain: mid's incoming edge is missing — %s"
        % (rep.incoming if rep else "no report"))
rep = reports.get("top")
if rep is not None and rep.secret_params == ["master"]:
    ok("chain: top's parameter is a secret root (#[secrets])")
else:
    bad("chain: top's roots are wrong — %s"
        % (rep.secret_params if rep else "no report"))

# recursion: the fixpoint terminates and the self-flow is handled.
reports = engine_on("""
#[secrets(acc)]
fn rec(n: int, acc: int) -> int {
    if n <= 0 {
        return acc
    }
    return rec(n - 1, acc + 2)
}

fn main() -> int { return 0 }
""", "rec.hls")
rep = reports.get("rec")
if rep is not None:
    ok("recursion: the fixpoint terminates on a self-recursive fn")
else:
    bad("recursion: no report for the recursive fn")

# a program with no markings: the CLI says so honestly (CLI section
# re-checks the exact words; here the engine returns nothing).
reports = engine_on("""
fn plain(x: int) -> int {
    if x > 0 {
        return 1
    }
    return 0
}

fn main() -> int { return 0 }
""", "plain.hls")
if not reports:
    ok("plain: no markings, no propagation — nothing is reported")
else:
    bad("plain: unexpected reports — %s" % sorted(reports))

# ---------------------------------------------------------------------------
print("=== 4. the CLI ===")
# ---------------------------------------------------------------------------

out = run([sys.executable, "tools/hlprove.py", DEMO, "--sidechannel"])
if out.returncode != 0:
    bad("hlprove --sidechannel exited %d" % out.returncode)
else:
    ok("hlprove --sidechannel runs clean on the demo")
    txt = out.stdout
    checks = [
        ("the INDEX sink names the sbox call",
         "line 40: INDEX — `sbox.get(b)`" in txt),
        ("the BRANCH sink names the byte compare",
         "line 47: BRANCH — `(key.byte_at(i) != probe.byte_at(i))`"
         in txt),
        ("the LOOP-BOUND sink names the range",
         "line 58: LOOP-BOUND — `range(0, rounds)`" in txt),
        ("the DIVISION sink names the modulo",
         "line 61: DIVISION — `(acc % rounds)`" in txt),
        ("the deep branch carries its incoming chain",
         "incoming: 'x' passes a secret 'x' from schedule_step"
         in txt),
        ("the policy note is printed where it applies",
         ".len() on a secret value: 1 use(s) — public by policy"
         in txt),
        ("the constant-time idiom is CLEAN",
         "CLEAN: no secret-dependent control flow" in txt),
        ("the totals line closes the report",
         "SIDECHANNEL TOTAL: 5 leaks — 2 branch, 1 index, 1 loop-bound,"
         " 1 division — in 4 of 8 secret-handling fns" in txt),
    ]
    for name, cond in checks:
        if cond:
            ok("cli: %s" % name)
        else:
            bad("cli: %s" % name)

plain = run([sys.executable, "tools/hlprove.py", OK_TEST,
             "--sidechannel"])
if plain.returncode == 0 and "SIDECHANNEL TOTAL: 0 leaks" in plain.stdout:
    ok("cli: the ok-test's constant-time shapes audit CLEAN")
else:
    bad("cli: the ok-test audit is wrong (rc=%d)" % plain.returncode)

none = run([sys.executable, "tools/hlprove.py", "examples/hello.hls",
            "--sidechannel"])
if none.returncode == 0 and "nothing to audit" in none.stdout:
    ok("cli: a program with no markings says so honestly")
else:
    bad("cli: the no-marking report is wrong (rc=%d)" % none.returncode)

# ---------------------------------------------------------------------------
print("=== 5. the demo ===")
# ---------------------------------------------------------------------------

interp = run([sys.executable, "boot/boot.py", DEMO])
if interp.returncode == 0:
    ok("the demo runs on the interpreter (rc=0)")
else:
    bad("the demo failed on the interpreter (rc=%d)" % interp.returncode)

BIN = os.path.join(ROOT, "bin", "hlc")
if os.path.exists(BIN):
    c_out = os.path.join(TMP, "sidechannel_demo.c")
    comp = run([BIN, DEMO, c_out])
    if comp.returncode == 0:
        exe = os.path.join(TMP, "sidechannel_demo")
        cc = run(["cc", "-O2", "-Werror", "-o", exe, c_out,
                  "-lm", "-pthread"])
        if cc.returncode == 0:
            native = run([exe])
            if native.returncode == 0 and native.stdout == interp.stdout:
                ok("native (-O2 -Werror) output identical to the "
                   "interpreter")
            else:
                bad("native output diverges (rc=%d)" % native.returncode)
        else:
            bad("cc refused the generated C (rc=%d)" % cc.returncode)
    else:
        bad("hlc refused the demo (rc=%d)" % comp.returncode)
else:
    bad("bin/hlc missing — run make bootstrap first")

# ---------------------------------------------------------------------------
print("=== 6. the audit parity ===")
# ---------------------------------------------------------------------------
# boot.py --audit and hlc --audit must print the SAME #[secrets(...)]
# block, word for word (the marking is a language-level fact, so the
# two front-ends must state it identically).

def audit_secrets_block(cmd):
    r = run(cmd)
    lines = (r.stdout or "").splitlines()
    block = []
    grabbing = False
    for ln in lines:
        if ln.startswith("  #[secrets(...)] annotations:"):
            grabbing = True
            block.append(ln)
            continue
        if grabbing:
            if ln.startswith("    "):
                block.append(ln)
            else:
                break
    return block


b_block = audit_secrets_block(["python3", "boot/boot.py", "--audit",
                               DEMO])
h_block = audit_secrets_block([BIN, "--audit", DEMO])
if b_block and b_block == h_block:
    ok("audit parity: %d #[secrets(...)] lines identical from both "
       "front-ends" % len(b_block))
    for ln in b_block:
        ok("    %s" % ln.strip())
else:
    bad("audit parity: blocks differ\n    boot: %s\n    hlc:  %s"
        % (b_block, h_block))

# ---------------------------------------------------------------------------
print("=== 7. the fail tests ===")
# ---------------------------------------------------------------------------


def normalise(text):
    """Strip the front-end-specific prefix and the line suffix (the
    Stage 86/91/93 gate convention)."""
    out = re.sub(r"^panic: ", "", text.strip())
    out = re.sub(r"^[a-z ]*error: ", "", out)
    out = re.sub(r" \(line \d+(?::\d+)?\)\s*$", "", out)
    return out


for stem, needle, why in FAIL_PROGRAMS:
    src = "tests/fail/%s.hls" % stem
    b = run(["python3", "boot/boot.py", "--check", src])
    h = run([BIN, src, os.path.join(TMP, stem + ".c")])
    if b.returncode == 0:
        bad("%s: boot accepted the malformed marking (%s)" % (stem, why))
        continue
    if h.returncode == 0:
        bad("%s: hlc accepted the malformed marking (%s)" % (stem, why))
        continue
    bmsg = normalise((b.stdout + b.stderr).splitlines()[0])
    hmsg = normalise((h.stdout + h.stderr).splitlines()[0])
    if bmsg != hmsg:
        bad("%s: boot and hlc disagree on the words:\n    boot: %s"
            "\n    hlc:  %s" % (stem, bmsg, hmsg))
        continue
    if needle not in bmsg:
        bad("%s: diagnostic does not name the rule ('%s'): %s"
            % (stem, needle, bmsg))
        continue
    ok("%s rejected identically by both front-ends (%s)" % (stem, why))

# ---------------------------------------------------------------------------
print("=== 8. the tools ===")
# ---------------------------------------------------------------------------

for f in (DEMO, OK_TEST):
    with open(f) as fh:
        original = fh.read()
    p1 = os.path.join(TMP, "fmt1.hls")
    p2 = os.path.join(TMP, "fmt2.hls")
    with open(p1, "w") as fh:
        fh.write(original)
    run([sys.executable, "tools/hlfmt.py", "-w", p1])
    shutil.copyfile(p1, p2)
    run([sys.executable, "tools/hlfmt.py", "-w", p2])
    with open(p1) as fh:
        once = fh.read()
    with open(p2) as fh:
        twice = fh.read()
    if once == twice:
        ok("hlfmt: stable formatting on %s" % f)
    else:
        bad("hlfmt: unstable formatting on %s" % f)

lints = [run([sys.executable, "tools/hllint.py", f])
         for f in (DEMO, OK_TEST)]
if all(l.returncode == 0 for l in lints):
    ok("hllint: no findings on the demo or the ok-test")
else:
    bad("hllint: findings on the new sources")

# ---------------------------------------------------------------------------
print()
shutil.rmtree(TMP, ignore_errors=True)
print("Stage 101 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
