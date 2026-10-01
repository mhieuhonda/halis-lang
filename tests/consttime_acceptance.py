#!/usr/bin/env python3
"""Stage 102 acceptance gate — the constant-time verifier.

Run with `make ct-acceptance` (or
`python3 tests/consttime_acceptance.py`).

Eight sections:

  1. the engine          — the verdict battery: the VERIFIED shapes
                           (branch-free selection, the MAC with its
                           len() policy note, the transitive chain)
                           and the VIOLATED shapes (own-body branch,
                           the callee branch with the chain, the
                           secret division and loop bound)
  2. the universes       — the precision flips a global fixpoint gets
                           wrong: an unrelated #[secrets] leak cannot
                           violate a claim, two claims are judged
                           independently, the claim covers the secrets
                           it passes DOWN (propagation), and the
                           return-secreteness imprecision is NOT
                           charged without a flow
  3. the chains          — the three-level chain, the self-body chain,
                           recursion terminating the fixpoint
  4. the CLI             --consttime on the demo (3 verified, exit 0),
                           on a violated program (exit 1), on a
                           claim-free program (honest line, exit 0)
  5. the demo            — interpreter and native agree byte for byte
  6. the audit parity    — boot.py --audit and hlc --audit print the
                           same #[ct] claims block, word for word
  7. the fail tests      — both front-ends reject the malformed
                           claims with the same words
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
from boot.proof import analyze_consttime      # noqa: E402

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_ct_", dir=os.path.join(ROOT, "tests"))

DEMO = "examples/consttime_demo.hls"
OK_TEST = "tests/ok/feat_stage102_consttime.hls"

FAIL_PROGRAMS = [
    ("fail_stage102_ct_dup",
     "appears more than once on one function",
     "the claim is per-function, so the second #[ct] is refused"),
    ("fail_stage102_ct_nosecrets",
     "must name the parameters it covers",
     "a claim over nothing verifies nothing"),
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


def claims_of(source, name):
    """load + check + verify a scratch program; returns the claims."""
    path = os.path.join(TMP, name)
    with open(path, "w") as f:
        f.write(source)
    program = load_program(path)
    check(program)
    return analyze_consttime(program)


def claim_of(claims, fn_key):
    for c in claims:
        if c.fn_key == fn_key:
            return c
    return None


# ---------------------------------------------------------------------------
print("=== 1. the engine ===")
# ---------------------------------------------------------------------------

# -- the VERIFIED branch-free selection ------------------------------------
claims = claims_of("""
#[secrets(mask)]
#[ct]
fn pick(mask: int, a: int, b: int) -> int {
    return a * mask + b * (1 - mask)
}

fn main() -> int uses IO {
    println(pick(1, 10, 20).to_str())
    return 0
}
""", "pick.hls")
c = claim_of(claims, "pick")
if c is not None and c.verified and c.reach == ["pick"] \
        and c.returns_secret:
    ok("pick: VERIFIED (data-only flow), the reach is the claim alone, "
       "the return is still secret")
else:
    bad("pick: wrong verdict — %s" % (c.verified if c else "no claim"))

# -- the VIOLATED own-body branch ------------------------------------------
claims = claims_of("""
#[secrets(s)]
#[ct]
fn gate(s: int) -> int {
    if s > 10 {
        return 1
    }
    return 0
}

fn main() -> int uses IO {
    println(gate(3).to_str())
    return 0
}
""", "gate.hls")
c = claim_of(claims, "gate")
if c is not None and not c.verified and len(c.findings) == 1 \
        and c.findings[0][0] == "gate" \
        and c.findings[0][1].kind == "branch" \
        and c.chains.get(("gate", "branch", 5)) == []:
    ok("gate: VIOLATED on its own branch, the chain is the claim's "
       "own body")
else:
    bad("gate: wrong verdict — %s" % (c.findings if c else "no claim"))

# -- the VIOLATED transitive branch (the chain crosses two hops) -----------
claims = claims_of("""
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
#[ct]
fn top(master: int) -> int {
    return mid(master)
}

fn main() -> int uses IO {
    println(top(7).to_str())
    return 0
}
""", "chain.hls")
c = claim_of(claims, "top")
chain = c.chains.get(("deep", "branch", 4)) if c else None
if c is not None and not c.verified \
        and c.findings[0][0] == "deep" \
        and chain == [("mid", "k", 19), ("deep", "x", 13)]:
    ok("chain: VIOLATED inside deep, the printed chain is "
       "top -> mid (k, line 19) -> deep (x, line 13)")
else:
    bad("chain: wrong verdict or chain — %s"
        % (c.chains if c else "no claim"))

# -- the VIOLATED division and loop bound inside a callee ------------------
claims = claims_of("""
fn workload(rounds: int) -> int {
    let mut acc: int = 0
    for i: int in range(0, rounds) {
        acc = acc + i
    }
    acc = acc % rounds
    return acc
}

#[secrets(n)]
#[ct]
fn drive(n: int) -> int {
    return workload(n)
}

fn main() -> int uses IO {
    println(drive(6).to_str())
    return 0
}
""", "workload.hls")
c = claim_of(claims, "drive")
kinds = sorted(f[1].kind for f in c.findings) if c else []
if kinds == ["division", "loop-bound"] and not c.verified:
    ok("workload: the claim dies on the callee's LOOP-BOUND and "
       "DIVISION sinks")
else:
    bad("workload: wrong findings — %s" % kinds)

# -- the policy note rides along on a VERIFIED claim ------------------------
claims = claims_of("""
#[secrets(acc, k)]
#[ct]
fn fold(mut acc: int, k: str) -> int {
    let mut i: int = 0
    while i < k.len() {
        acc = acc + k.byte_at(i) * 31
        i = i + 1
    }
    return acc
}

fn main() -> int uses IO {
    println(fold(0, "abc").to_str())
    return 0
}
""", "fold.hls")
c = claim_of(claims, "fold")
if c is not None and c.verified and c.length_uses == 1:
    ok("fold: VERIFIED with the len() policy note counted, never hidden")
else:
    bad("fold: wrong verdict — verified=%s uses=%s"
        % (c.verified if c else "?", c.length_uses if c else "?"))

# ---------------------------------------------------------------------------
print("=== 2. the universes ===")
# ---------------------------------------------------------------------------
# The cases a GLOBAL fixpoint gets wrong: the claim is judged in its
# OWN universe.

# an unrelated #[secrets] fn leaks — the claim is untouched.
claims = claims_of("""
#[secrets(s)]
fn leaky(s: int) -> int {
    if s > 10 {
        return 1
    }
    return 0
}

#[secrets(mask)]
#[ct]
fn pick(mask: int, a: int, b: int) -> int {
    return a * mask + b * (1 - mask)
}

fn main() -> int uses IO {
    println(pick(1, 10, 20).to_str())
    println(leaky(42).to_str())
    return 0
}
""", "universe.hls")
c = claim_of(claims, "pick")
if c is not None and c.verified:
    ok("universe: leaky's BRANCH sink cannot violate pick — different "
       "universe")
else:
    bad("universe: the unrelated leak bled into the claim")

# two claims, one program: judged independently.
claims = claims_of("""
#[secrets(s)]
#[ct]
fn good(s: int) -> int {
    return s * 2
}

#[secrets(t)]
#[ct]
fn bad(t: int) -> int {
    if t > 0 {
        return 1
    }
    return 0
}

fn main() -> int uses IO {
    println(good(3).to_str())
    println(bad(3).to_str())
    return 0
}
""", "independent.hls")
g = claim_of(claims, "good")
b = claim_of(claims, "bad")
if g is not None and b is not None and g.verified and not b.verified:
    ok("independent: good VERIFIED and bad VIOLATED in the same run")
else:
    bad("independent: verdicts leaked across claims "
        "(good=%s bad=%s)" % (g.verified if g else "?",
                              b.verified if b else "?"))

# the claim covers what it passes DOWN: the secret flows into a plain
# callee, so the callee is inside the claim's reach.
claims = claims_of("""
fn helper(k: int) -> int {
    return k * 3 + 1
}

#[secrets(m)]
#[ct]
fn feed(m: int) -> int {
    return helper(m)
}

fn main() -> int uses IO {
    println(feed(4).to_str())
    return 0
}
""", "feed.hls")
c = claim_of(claims, "feed")
if c is not None and c.verified and c.reach == ["feed", "helper"]:
    ok("feed: the propagated callee is inside the reach — and clean")
else:
    bad("feed: wrong reach — %s" % (c.reach if c else "no claim"))

# ...and the same shape with a leaky callee dies BY the chain.
claims = claims_of("""
fn helper(k: int) -> int {
    if k > 100 {
        return 1
    }
    return 0
}

#[secrets(m)]
#[ct]
fn feed(m: int) -> int {
    return helper(m)
}

fn main() -> int uses IO {
    println(feed(4).to_str())
    return 0
}
""", "feed_leak.hls")
c = claim_of(claims, "feed")
chain = c.chains.get(("helper", "branch", 3)) if c else None
if c is not None and not c.verified and chain == [("helper", "k", 12)]:
    ok("feed_leak: the secret passed down is the claim's own "
       "responsibility")
else:
    bad("feed_leak: wrong verdict — %s" % (c.chains if c else "no claim"))

# the return-secreteness imprecision is NOT charged without a flow:
# g branches on h(5) — public arg, secret-marked return — and f (the
# claim) is not g's supplier.
claims = claims_of("""
fn h(k: int) -> int {
    return k * 3
}

#[secrets(m)]
#[ct]
fn f(m: int) -> int {
    return h(m)
}

fn g(z: int) -> int {
    let r: int = h(5)
    if r > 10 {
        return 1
    }
    return 0
}

fn main() -> int uses IO {
    println(f(4).to_str())
    println(g(0).to_str())
    return 0
}
""", "imprecise.hls")
c = claim_of(claims, "f")
if c is not None and c.verified:
    ok("imprecise: g's sink has no param-flow from f — not charged")
else:
    bad("imprecise: an unchained sink was charged to the claim — %s"
        % (c.findings if c else "no claim"))

# ---------------------------------------------------------------------------
print("=== 3. the chains ===")
# ---------------------------------------------------------------------------

# recursion terminates the fixpoint and the clean recursive claim
# verifies.
claims = claims_of("""
#[secrets(acc)]
#[ct]
fn rec(n: int, acc: int) -> int {
    if n <= 0 {
        return acc
    }
    return rec(n - 1, acc + 2)
}

fn main() -> int uses IO {
    println(rec(3, 0).to_str())
    return 0
}
""", "rec.hls")
c = claim_of(claims, "rec")
if c is None:
    bad("recursion: no verdict for the recursive claim")
elif c.verified:
    ok("recursion: the fixpoint terminates, the clean claim verifies")
else:
    bad("recursion: false violation — %s" % c.findings)

# a VIOLATED recursive claim: the sink fires in the claim's own body.
claims = claims_of("""
#[secrets(acc)]
#[ct]
fn rec(mut acc: int, n: int) -> int {
    if n <= 0 {
        return acc
    }
    if acc > 100 {
        acc = 100
    }
    return rec(acc + 2, n - 1)
}

fn main() -> int uses IO {
    println(rec(0, 3).to_str())
    return 0
}
""", "rec_leak.hls")
c = claim_of(claims, "rec")
if c is not None and not c.verified \
        and c.chains.get(("rec", "branch", 8)) == []:
    ok("recursion: the violated claim pins its own branch")
else:
    bad("recursion: the violated self-branch is missing — %s"
        % (c.chains if c else "no claim"))

# ---------------------------------------------------------------------------
print("=== 4. the CLI ===")
# ---------------------------------------------------------------------------

out = run([sys.executable, "tools/hlprove.py", DEMO, "--consttime"])
if out.returncode != 0:
    bad("hlprove --consttime exited %d on the demo" % out.returncode)
else:
    ok("hlprove --consttime runs clean on the demo (exit 0)")
    txt = out.stdout
    checks = [
        ("the selection claim is VERIFIED",
         "ct_pick:" in txt and "VERIFIED: no secret-derived branch"
         in txt),
        ("the MAC claim carries its policy note",
         "policy: .len() on a secret value — 1 use(s), public by policy"
         in txt),
        ("the transitive claim names its reach",
         "secret reach: ct_expand, schedule_step (2 fns)" in txt),
        ("the totals line closes the report",
         "CT TOTAL: 3 verified, 0 violated of 3 claims" in txt),
    ]
    for name, cond in checks:
        if cond:
            ok("cli: %s" % name)
        else:
            bad("cli: %s" % name)

okt = run([sys.executable, "tools/hlprove.py", OK_TEST, "--consttime"])
if okt.returncode == 0 and "CT TOTAL: 3 verified, 0 violated of 3 " \
        "claims" in okt.stdout:
    ok("cli: the ok-test's claimed shapes all verify")
else:
    bad("cli: the ok-test verification is wrong (rc=%d)" % okt.returncode)

violated = os.path.join(TMP, "cli_violated.hls")
with open(violated, "w") as f:
    f.write("""
#[secrets(s)]
#[ct]
fn gate(s: int) -> int {
    if s > 10 {
        return 1
    }
    return 0
}

fn main() -> int uses IO {
    println(gate(3).to_str())
    return 0
}
""")
vout = run([sys.executable, "tools/hlprove.py", violated,
            "--consttime"])
if vout.returncode == 1 and "VIOLATED" in vout.stdout \
        and "chain: gate (the claim's own body)" in vout.stdout \
        and "CT TOTAL: 0 verified, 1 violated of 1 claims" \
        in vout.stdout:
    ok("cli: a violated claim exits 1 with the chain printed")
else:
    bad("cli: the violated run is wrong (rc=%d)" % vout.returncode)

none = run([sys.executable, "tools/hlprove.py", "examples/hello.hls",
            "--consttime"])
if none.returncode == 0 and "no #[ct] claims" in none.stdout:
    ok("cli: a claim-free program says so honestly (exit 0)")
else:
    bad("cli: the claim-free report is wrong (rc=%d)" % none.returncode)

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
    c_out = os.path.join(TMP, "consttime_demo.c")
    comp = run([BIN, DEMO, c_out])
    if comp.returncode == 0:
        exe = os.path.join(TMP, "consttime_demo")
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
# boot.py --audit and hlc --audit must print the SAME #[ct] claims
# block, word for word (the claim is a language-level fact, so the
# two front-ends must state it identically).

def audit_ct_block(cmd):
    r = run(cmd)
    lines = (r.stdout or "").splitlines()
    block = []
    grabbing = False
    for ln in lines:
        if ln.startswith("  #[ct] claims:"):
            grabbing = True
            block.append(ln)
            continue
        if grabbing:
            if ln.startswith("    "):
                block.append(ln)
            else:
                break
    return block


b_block = audit_ct_block(["python3", "boot/boot.py", "--audit", DEMO])
h_block = audit_ct_block([BIN, "--audit", DEMO])
if b_block and b_block == h_block:
    ok("audit parity: %d #[ct] claims lines identical from both "
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
        bad("%s: boot accepted the malformed claim (%s)" % (stem, why))
        continue
    if h.returncode == 0:
        bad("%s: hlc accepted the malformed claim (%s)" % (stem, why))
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
print("Stage 102 gate: %d passed / %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
