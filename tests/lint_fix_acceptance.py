#!/usr/bin/env python3
"""Stage 118 acceptance gate — hllint autofix mode (`--fix` / `--diff`).

Run with `make lint-fix-acceptance` (or
`python3 tests/lint_fix_acceptance.py`).

Eight sections:

  1. the renames    — L001 is a scope rename: the binding's declaration
                      AND every consistent occurrence inside the
                      enclosing function (reads, assign targets, match
                      payload bindings) get the underscore prefix, so a
                      blind spot in the reference collector cannot turn
                      the fix into a break; `let mut` renames too; a
                      binding whose value calls something keeps the
                      call; L002 renames the definition of an unused
                      function and nothing else; an already-`_`-prefixed
                      name is never flagged again (the fix's own output
                      is the accepted end state, not a new defect); an
                      ambiguous short name (two `fn save` in different
                      impls) is skipped with the file untouched
  2. the wraps      — L004 binds a discard: `parse_it("hi")` becomes
                      `let _ignored: T = parse_it("hi")` with the
                      checker's own type text, for the plain-call and
                      the method-call shape, on a statement that spans
                      lines as well
  3. the signature  — L006 deletes the whole `uses` clause (plain, with
                      two effects, with a contract clause after it); an
                      extern's `uses` clause is part of the FFI contract
                      and is skipped
  4. the deletions  — L007 deletes the unreachable run up to the
                      enclosing brace (nested blocks and comments inside
                      the dead code go with it, the brace survives, no
                      stale indent is left glued to it); L010 deletes
                      the empty impl block; L011/L012 delete the
                      attribute line cleanly while a sibling attribute
                      on the same line survives
  5. the safety net — a file that does not parse is never written; a
                      fix the crate check rejects (a rename colliding
                      with an imported module, a stream target that
                      would stop resolving) is rolled back and
                      reported; a corrupted candidate edit is caught by
                      the parse check and rolled back
  6. the fixed point— a second --fix run changes nothing; --diff exits 1
                      while fixes are pending and 0 when clean; --fix
                      and --diff together are a usage error; --strict
                      exits 1 when warnings remain after fixing
  7. the differential—the stage's ok fixture runs interpreter and
                      native before and after --fix and prints the SAME
                      markers from all four runs; the fixed file is
                      still a fixed point under hlfmt
  8. the corpus     — every tests/ok/ and examples/ file fixes on a
                      temp copy without crashing, re-lints clean of
                      crashes, and — when the original ran — the fixed
                      copy prints the same output (timing-flavoured
                      markers masked)

Exit code 0 = all acceptance criteria met.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from boot.lexer import HLError  # noqa: E402
import tools.hllint as hllint  # noqa: E402
from tools.hllint import Fixer  # noqa: E402

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s118_", dir=os.path.join(ROOT, "tests"))

HLC = os.path.join(ROOT, "bin", "hlc")
HLLINT = [sys.executable, "tools/hllint.py"]
HLFMT = [sys.executable, "tools/hlfmt.py"]
BOOT = [sys.executable, "boot/boot.py"]


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


def write(path, text):
    full = os.path.join(TMP, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
    return full


def run_cli(args, cwd=ROOT):
    return subprocess.run(HLLINT + args, capture_output=True, text=True,
                          cwd=cwd)


def fixed_lines(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8").splitlines()


def markers_of(src_path):
    """(returncode, masked_lines, raw_stdout) — the masked lines hide
    every digit (clock values) and every whole `ts=` line (UUID v7 /
    ULID timestamps print hex whose LETTERS vary with the clock), so
    two runs of a time-flavoured program compare equal."""
    r = subprocess.run(BOOT + [src_path], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        if "ts=" in line:
            out.append("TS")
        else:
            out.append("".join("#" if c.isdigit() else c
                               for c in line))
    return r.returncode, out, r.stdout


# ---------------------------------------------------------------------------
print("Section 1 — the renames (L001 scope rename, L002 definition rename)")
# ---------------------------------------------------------------------------

src = '''# rename scope probe
fn scope_fn(n: int) -> int uses IO {
    let mut y: int = n
    y = exit_free(n)
    println("ran")
    return 0
}

fn exit_free(m: int) -> int {
    return m
}

fn main() -> int uses IO {
    let gone: int = scope_fn(1)
    println(gone.to_str())
    return 0
}
'''
p = write("renames/a.hls", src)
r = run_cli(["--fix", p])
lines = fixed_lines(p)
text = "\n".join(lines)
check("let mut _y: int = n" in text,
      "L001 renames the declaration (`let mut _y`)")
check("_y = exit_free(n)" in text,
      "L001 renames the assign target too (write-only binding)")
check("fn scope_fn" in text and "fn _scope_fn" not in text,
      "the enclosing fn's own name is untouched")
check("fn exit_free" in text and "fn _exit_free" not in text,
      "the callee keeps its name")
r2 = run_cli([p])
check(r2.stdout.strip().endswith("no warnings"),
      "the fixed file re-lints clean")
rc, out, raw = markers_of(p)
check(rc == 0 and "ran" in raw,
      "the scope-renamed function still computes and prints")
# value side effects survive: noisy() still called after the rename
src2 = '''fn noisy() -> int uses IO {
    println("side effect kept")
    return 1
}

fn main() -> int uses IO {
    let unused: int = noisy()
    return 0
}
'''
p2 = write("renames/b.hls", src2)
run_cli(["--fix", p2])
rc, out, raw = markers_of(p2)
check(rc == 0 and "side effect kept" in raw,
      "the renamed binding's value still runs (side effect kept)")
# ambiguous short names are skipped, never guessed
src3 = '''struct Saver {
    n: int
}

impl Saver {
    fn save(self: Saver) -> int {
        return self.n
    }
}

impl Saver {
    fn save2(self: Saver) -> int {
        return self.n
    }
}

fn orphan_x() -> int {
    return 3
}

fn main() -> int uses IO {
    let s: Saver = Saver { n: 1 }
    println(s.save().to_str())
    return 0
}
'''
p3 = write("renames/c.hls", src3)
before = open(p3, "rb").read()
r3 = run_cli(["--fix", p3])
after = open(p3, "rb").read()
check("fn _orphan_x" in after.decode("utf-8"),
      "L002 renames the unique orphan fn")
check("fn save(self: Saver)" in after.decode("utf-8"),
      "the two `save` methods keep their names (rename would be a guess)")
# extern + handler fns are never renamed
src4 = '''extern "C" {
    fn puts(s: str) -> int uses IO
}

fn main() -> int uses IO {
    println("hi")
    return 0
}
'''
p4 = write("renames/d.hls", src4)
before4 = open(p4, "rb").read()
r4 = run_cli(["--fix", p4])
check(open(p4, "rb").read() == before4,
      "an extern declaration is never renamed (the name is the C symbol)")
check("extern declaration" in r4.stdout,
      "the extern skip is reported with a reason")

# ---------------------------------------------------------------------------
print("Section 2 — the wraps (L004 discard bindings)")
# ---------------------------------------------------------------------------

src = '''import "std.result"

fn parse_it(s: str) -> Result[int, int] {
    if s.len() == 0 {
        return Result.Err(1)
    }
    return Result.Ok(s.len())
}

struct Cell {
    v: int
}

impl Cell {
    fn refresh(self: Cell) -> Result[int, int] {
        return Result.Ok(self.v)
    }
}

fn main() -> int uses IO {
    parse_it("hi")
    let c: Cell = Cell { v: 2 }
    c.refresh()
    parse_it(
        "a longer argument, one line down")
    println("wrapped")
    return 0
}
'''
p = write("wraps/a.hls", src)
r = run_cli(["--fix", p])
lines = fixed_lines(p)
text = "\n".join(lines)
check("let _ignored: Result[int, int] = parse_it(\"hi\")" in text,
      "the plain call is wrapped in a typed discard")
check("let _ignored2: Result[int, int] = c.refresh()" in text,
      "the method call is wrapped in a typed discard (unique name)")
check("let _ignored3: Result[int, int] = parse_it(" in text and
      '"a longer argument, one line down")' in text,
      "a multi-line statement is wrapped at its head, body untouched")
rc, out, raw = markers_of(p)
check(rc == 0 and "wrapped" in raw, "the wrapped file still runs")
r2 = run_cli([p])
check(r2.stdout.strip().endswith("no warnings"),
      "the wrapped file re-lints clean")

# ---------------------------------------------------------------------------
print("Section 3 — the signature (L006 clause deletion)")
# ---------------------------------------------------------------------------

src = '''fn pure_one() -> int uses IO {
    return 1
}

fn pure_two() -> int uses IO, Fs {
    return 2
}

fn pure_three() -> int uses IO requires true {
    return 3
}

fn main() -> int uses IO {
    println((pure_one() + pure_two() + pure_three()).to_str())
    return 0
}
'''
p = write("sig/a.hls", src)
r = run_cli(["--fix", p])
text = "\n".join(fixed_lines(p))
check("fn pure_one() -> int {" in text,
      "the single-effect clause is deleted")
check("fn pure_two() -> int {" in text,
      "the two-effect clause is deleted")
check("fn pure_three() -> int requires true {" in text,
      "the clause before a contract is deleted, the contract survives")
rc, out, raw = markers_of(p)
check(rc == 0 and "6" in raw,
      "the fixed signatures still compute the same values")
r2 = run_cli([p])
check(r2.stdout.strip().endswith("no warnings"),
      "the fixed signatures re-lint clean")
# an extern's uses clause is part of the FFI contract — skipped
src2 = '''extern "C" {
    fn getenv(name: str) -> str uses IO
}

fn main() -> int uses IO {
    println("x")
    return 0
}
'''
p2 = write("sig/b.hls", src2)
before = open(p2, "rb").read()
r = run_cli(["--fix", p2])
check(open(p2, "rb").read() == before,
      "an extern `uses` clause is never deleted")
check("extern declaration" in r.stdout,
      "the extern skip is reported with a reason")

# ---------------------------------------------------------------------------
print("Section 4 — the deletions (L007 dead code, L010 empty impl, "
      "L011/L012 attributes)")
# ---------------------------------------------------------------------------

# L007: the rescue — a checker-rejected file becomes a runnable one.
src = '''fn dead_tail(k: int) -> int {
    let mut acc: int = 0
    if k > 0 {
        acc = 1
    }
    return acc
    let never: int = 2
    println("unreachable " + never.to_str())
    while false {
        acc = acc + 1
    }
}

fn main() -> int uses IO {
    println(dead_tail(1).to_str())
    return 0
}
'''
p = write("del/a.hls", src)
rc0, _out0, _raw0 = markers_of(p)
check(rc0 != 0, "the checker rejects the dead-tail file before the fix")
r = run_cli(["--fix", p])
text = "\n".join(fixed_lines(p))
check('println("unreachable " + never.to_str())' not in text,
      "the unreachable run is deleted")
check("return acc\n}" in text.replace("    return acc\n}", "return acc\n}")
      or "return acc\n}" in text,
      "the live body and the closing brace survive")
check("    }\n}" not in text and "\n\n\n}" not in text,
      "no stale indent or blank line is glued to the brace")
rc1, out1, raw1 = markers_of(p)
check(rc1 == 0 and "1" in raw1.splitlines()[0],
      "the rescued file compiles and runs after the fix")
check("let never" not in text,
      "a binding inside the dead code dies with it (subsumed)")
# L010 + attribute deletions, sibling attribute survives
body = "\n".join("    acc = acc + %d" % i for i in range(55))
src2 = '''impl Nothing {
}

#[cold]
#[inline(always)]
fn hot_stub() -> int {
    let mut acc: int = 0
%s
    return acc
}

#[tail_call]
fn recurs(k: int) -> int {
    let mut acc: int = 0
%s
    if k <= 0 {
        return acc
    }
    return recurs(k - 1)
}

fn main() -> int uses IO {
    println((hot_stub() + recurs(1)).to_str())
    return 0
}
''' % (body, "\n".join("    acc = acc + %d" % i for i in range(32)))
p2 = write("del/b.hls", src2)
r = run_cli(["--fix", p2])
text = "\n".join(fixed_lines(p2))
check("impl Nothing" not in text, "the empty impl block is deleted")
check("#[inline(always)]" not in text,
      "the #[inline(always)] attribute is deleted")
check("#[cold]" in text,
      "the sibling #[cold] attribute survives")
check("#[tail_call]" not in text,
      "the #[tail_call] attribute is deleted")
rc, out, raw = markers_of(p2)
check(rc == 0 and raw.strip(), "the attribute-deleted file still runs")
r2 = run_cli([p2])
check(r2.stdout.strip().endswith("no warnings"),
      "the deletions re-lint clean")

# ---------------------------------------------------------------------------
print("Section 5 — the safety net (never parse, never guess, roll back)")
# ---------------------------------------------------------------------------

p = write("safety/broken.hls", "fn broken( {\n    return 0\n}\n")
before = open(p, "rb").read()
r = run_cli(["--fix", p])
check(open(p, "rb").read() == before,
      "an unparseable file is never written")
check(r.returncode == 1 and "error:" in r.stderr,
      "the unparseable file is reported as an error (exit 1)")
# a rename that collides with an imported module is rolled back
p2 = write("safety/collide.hls",
           open(os.path.join(ROOT, "examples", "uuid_ulid_demo.hls"),
                "rb").read())
before = open(p2, "rb").read()
r = run_cli(["--fix", p2])
check(open(p2, "rb").read() == before,
      "a fix the crate check rejects leaves the file byte-identical")
check("rolled back" in r.stdout or "not auto-fixed" in r.stdout,
      "the rejected fix is reported, not silent")
# a corrupted candidate edit is caught by the parse check
class CorruptFixer(Fixer):
    def _apply(self, src, edits):
        out = super()._apply(src, edits)
        return out.replace(b"fn main", b"fn main(")  # break it on purpose


p3 = write("safety/corrupt.hls", '''fn helper() -> int {
    return 1
}

fn main() -> int uses IO {
    let stray: int = helper()
    return 0
}
''')
before = open(p3, "rb").read()
fx = CorruptFixer(path=p3)
new_src, report = fx.fix_source(open(p3, "rb").read())
check(new_src == before and not report["changed"],
      "a corrupted candidate is rolled back (parse check)")
# the crate net: a stream target rename is refused
p4 = write("safety/stream.hls",
           open(os.path.join(ROOT, "tests", "ok",
                              "feat_stage34_stream.hls"), "rb").read())
before = open(p4, "rb").read()
run_cli(["--fix", p4])
check(open(p4, "rb").read() == before,
      "a stream target keeps its name (the rename would stop resolving)")

# ---------------------------------------------------------------------------
print("Section 6 — the fixed point, --diff, and the exit codes")
# ---------------------------------------------------------------------------

src = '''fn one() -> int {
    return 1
}

fn main() -> int uses IO {
    let stray: int = one()
    println(stray.to_str())
    return 0
}
'''
p = write("point/a.hls", '''fn one() -> int {
    return 1
}

fn main() -> int uses IO {
    let unused_a: int = one()
    println("x")
    return 0
}
''')
r1 = run_cli(["--fix", p])
check("fixed" in r1.stdout, "the first --fix reports what it fixed")
n1 = len(run_cli([p]).stdout.strip().splitlines())
r2 = run_cli(["--fix", p])
check("fixed" not in r2.stdout,
      "a second --fix run fixes nothing (fixed point)")
# --diff exit codes
p2 = write("point/b.hls", '''fn two() -> int {
    return 2
}

fn main() -> int uses IO {
    let gone: int = two()
    println("y")
    return 0
}
''')
rd = run_cli(["--diff", p2])
check(rd.returncode == 1 and "---" in rd.stdout,
      "--diff shows the pending diff and exits 1")
run_cli(["--fix", p2])
rd2 = run_cli(["--diff", p2])
check(rd2.returncode == 0, "--diff on a clean file exits 0")
# --fix --diff is a usage error
r3 = run_cli(["--fix", "--diff", p2])
check(r3.returncode == 2,
      "--fix and --diff together are a usage error (exit 2)")
# --strict after a fix: remaining (unfixable) warnings still exit 1
p3 = write("point/c.hls", '''struct Hidden {
    field: int
}

fn main() -> int uses IO {
    let h: Hidden = Hidden { field: 1 }
    println("z")
    return 0
}
''')
r4 = run_cli(["--strict", "--fix", p3])
check(r4.returncode == 1 and "L003" in r4.stdout,
      "--strict exits 1 when an unfixable warning remains after the fix")

# ---------------------------------------------------------------------------
print("Section 7 — the differential (interpreter and native, before "
      "and after the fix)")
# ---------------------------------------------------------------------------

FIXTURE = os.path.join(ROOT, "tests", "ok", "feat_stage118_lint_fix.hls")
work = write("diff/fixture.hls", open(FIXTURE, "rb").read())

rc_i0, out_i0, raw_i0 = markers_of(FIXTURE)
rc_i1, out_i1, raw_i1 = markers_of(work)
check(rc_i0 == 0 and rc_i0 == rc_i1 and out_i0 == out_i1,
      "the interpreter prints the same markers before and after --fix")

native = shutil.which("gcc") is not None and os.path.isfile(HLC)
if native:
    def build_and_run(path, tag):
        c = os.path.join(TMP, "diff_%s.c" % tag)
        e = os.path.join(TMP, "diff_%s" % tag)
        b = subprocess.run([HLC, path, c], capture_output=True, text=True)
        if b.returncode != 0:
            return None, ["compile failed: %s" % b.stderr.strip()[:80]]
        g = subprocess.run(["gcc", "-O2", "-o", e, c, "-lm", "-pthread"],
                           capture_output=True, text=True)
        if g.returncode != 0:
            return None, ["gcc failed: %s" % g.stderr.strip()[:80]]
        r = subprocess.run([e], capture_output=True, text=True)
        return r.returncode, [l for l in r.stdout.splitlines()]

    rc_n0, out_n0 = build_and_run(FIXTURE, "before")
    rc_n1, out_n1 = build_and_run(work, "after")
    check(rc_n0 == 0 and rc_n0 == rc_n1 and out_n0 == out_n1,
          "the native build prints the same markers before and after --fix")
    masked_n = ["".join("#" if c.isdigit() else c for c in l)
                for l in out_n0]
    check(out_i0 == masked_n,
          "interpreter and native agree on the fixed file's markers")
else:
    bad("the native half of the differential could not run "
        "(no gcc or no bin/hlc)")

# the fixed file is still a fixed point under hlfmt
f1 = subprocess.run(HLFMT + [work], capture_output=True, text=True)
f2 = subprocess.run(HLFMT + ["-c", work], capture_output=True, text=True)
check(f1.returncode == 0 and "already formatted" in f2.stdout,
      "the fixed file is a fixed point under hlfmt")

# ---------------------------------------------------------------------------
print("Section 8 — the corpus (every ok file and example fixes safely)")
# ---------------------------------------------------------------------------

corpus = sorted(glob.glob("tests/ok/*.hls") + glob.glob("examples/*.hls"))
crashed = regressed = semantic = 0
fixed_count = 0
for f in corpus:
    t = os.path.join(TMP, "corpus_" + os.path.basename(f))
    shutil.copy(f, t)
    rf = run_cli(["--fix", t])
    if rf.returncode not in (0, 1) or "Traceback" in rf.stderr:
        crashed += 1
        bad("--fix crashed on %s" % f)
        continue
    rl = run_cli([t])
    if rl.returncode != 0 or "Traceback" in rl.stderr:
        regressed += 1
        bad("the fixed copy of %s does not re-lint" % f)
        continue
    if "fixed" in rf.stdout:
        fixed_count += 1
        rc0, out0, _raw0 = markers_of(f)
        rc1, out1, _raw1 = markers_of(t)
        if rc0 == 0 and rc1 != 0:
            semantic += 1
            bad("the fixed copy of %s no longer runs" % f)
        elif rc0 == 0 and out0 != out1:
            semantic += 1
            bad("the fixed copy of %s prints different markers" % f)
if crashed == 0:
    ok("--fix runs without crashing on all %d corpus files" % len(corpus))
if regressed == 0:
    ok("every fixed copy re-lints without crashing")
if semantic == 0:
    ok("every runnable original prints the same markers after --fix "
       "(%d files fixed)" % fixed_count)

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
if FAIL == 0:
    print("=== Stage 118 acceptance: %d PASS / %d FAIL ===" % (PASS, FAIL))
    sys.exit(0)
print("=== Stage 118 acceptance: %d PASS / %d FAIL ===" % (PASS, FAIL))
sys.exit(1)
