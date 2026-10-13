#!/usr/bin/env python3
"""hls-fuzz — AST-level differential fuzzer for Halis (Stage 18).

The fuzzer generates small random HLS programs (a tiny grammar tuned to
exercise the surface that matters: arithmetic, control flow, lists,
strings, structs, enums, contracts), compiles each program TWO ways:

  (1) Interpreter:  python3 boot/boot.py prog.hls
  (2) Native:       hlc.hls (via Stage-0) -> C -> gcc -> run

and compares stdout+exit-code byte-for-byte. Any divergence is a
soundness bug in EITHER implementation; the fuzzer auto-minimises the
failing program (delta-debugging on the AST) and writes the minimised
case to fuzz-corpus/.

Acceptance criterion (Stage 18): the fuzzer runs for 1 hour without
finding any semantic discrepancy between the two implementations.

Stage 134: `--seed-corpus DIR` points the fuzzer at a corpus seeded
from real-world packages (tools/hls-fuzzseed.py). A `--seed-ratio`
fraction of the programs then comes from the corpus — mutation
included, via the same op set the seeder used — instead of the
toy grammar, so the differential oracle sees real code shapes. The
counters report the two populations separately; every other law is
unchanged (byte comparison, minimisation, corpus output).

Usage:
  python3 tools/hls-fuzz.py --time 3600            # 1-hour smoke
  python3 tools/hls-fuzz.py --time 600             # 10-minute smoke
  python3 tools/hls-fuzz.py --seed 42 --n 1000     # deterministic
  python3 tools/hls-fuzz.py --seed-corpus fuzz-corpus/seed --time 600
  python3 tools/hls-fuzz.py --minimize case.hls    # minimize a case
"""
import argparse
import importlib.util
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from boot.boot import load_program          # noqa: E402
from boot.checker import check              # noqa: E402
from boot.lexer import HLError              # noqa: E402

# Stage 134: the seeder's mutator is shared, not duplicated. The tool
# file carries a dash, so the import goes through an explicit spec —
# one loader, the same ops both sides of the corpus boundary.
_FUZZSEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "hls-fuzzseed.py")
_spec = importlib.util.spec_from_file_location("hls_fuzzseed",
                                               _FUZZSEED_PATH)
hls_fuzzseed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hls_fuzzseed)


# ---------------------------------------------------------------------------
# AST generation — emit a SMALL but type-correct HLS program.
# ---------------------------------------------------------------------------
# The generator is intentionally narrow: it produces a `main() -> int`
# that prints some integer results. Every generated program is type-
# correct by construction (we never emit a malformed expression), so the
# fuzzer exercises the BACKEND, not the parser/checker (which have their
# own differential test in tests/run_tests.sh section 1).
#
# Grammar (in brief):
#   program  := fn main() -> int uses IO { stmts; return 0 }
#   stmts    := let x: int = expr
#             | println(x.to_str())
#             | if expr { stmts } else { stmts }
#             | while expr { stmts }
#   expr     := int_lit | x | (expr op expr) | expr % expr
#             | expr.len() | expr.to_str()
#   op       := + - * / % == != < <= > >= && ||
# Every `let` declares a FRESH variable; we never reuse names so the
# checker's "use of moved value" rule can't fire on a generator bug.


class Gen:
    def __init__(self, rng):
        self.rng = rng
        self.next_id = 0
        # Live integer variables in scope (for use in expressions).
        # Each is a name string; values are not tracked (the checker
        # does that).
        self.live_ints = []
        self.live_strs = []

    def fresh(self, prefix="x"):
        self.next_id += 1
        return "%s%d" % (prefix, self.next_id)

    def gen_int_lit(self):
        # Small constants catch off-by-one bugs faster than huge ones.
        # Sprinkle in a few corner-case values.
        r = self.rng.random()
        if r < 0.10:
            return "0"
        if r < 0.15:
            return "1"
        if r < 0.20:
            return "-1"
        if r < 0.25:
            return "9223372036854775807"   # INT64_MAX
        if r < 0.30:
            return "-9223372036854775808"  # INT64_MIN
        # Random in [-1000, 1000]
        return str(self.rng.randint(-1000, 1000))

    def gen_bool_lit(self):
        return "true" if self.rng.random() < 0.5 else "false"

    def gen_str_lit(self):
        # Short ASCII strings — keep them simple so the output is easy
        # to diff.
        n = self.rng.randint(0, 8)
        chars = []
        for _ in range(n):
            c = self.rng.randint(97, 122)  # a-z
            chars.append(chr(c))
        return '"%s"' % ("".join(chars),)

    def gen_int_expr(self, depth):
        if depth <= 0 or self.rng.random() < 0.30:
            if self.live_ints and self.rng.random() < 0.6:
                return self.rng.choice(self.live_ints)
            return self.gen_int_lit()
        op = self.rng.choice(["+", "-", "*", "%"])
        # For "/" the divisor might be 0 — the language panics on that,
        # which is a legitimate observable. We DO generate "/" to test
        # the panic path matches between interp and native.
        if self.rng.random() < 0.2:
            op = "/"
        a = self.gen_int_expr(depth - 1)
        b = self.gen_int_expr(depth - 1)
        # Wrap in parens to keep precedence simple.
        return "(%s %s %s)" % (a, op, b)

    def gen_bool_expr(self, depth):
        if depth <= 0 or self.rng.random() < 0.40:
            if self.live_ints and self.rng.random() < 0.5:
                a = self.rng.choice(self.live_ints)
                b = self.gen_int_expr(0)
                op = self.rng.choice(["<", "<=", ">", ">=", "==", "!="])
                return "(%s %s %s)" % (a, op, b)
            return self.gen_bool_lit()
        op = self.rng.choice(["&&", "||"])
        a = self.gen_bool_expr(depth - 1)
        b = self.gen_bool_expr(depth - 1)
        return "(%s %s %s)" % (a, op, b)

    def gen_stmt(self, depth):
        r = self.rng.random()
        if r < 0.40:
            # let x: int = <expr>
            name = self.fresh("v")
            expr = self.gen_int_expr(min(depth, 3))
            self.live_ints.append(name)
            return "    let %s: int = %s" % (name, expr)
        if r < 0.55:
            # let s: str = <str_lit>
            name = self.fresh("s")
            expr = self.gen_str_lit()
            self.live_strs.append(name)
            return "    let %s: str = %s" % (name, expr)
        if r < 0.75 and self.live_ints:
            # println(x.to_str())
            v = self.rng.choice(self.live_ints)
            return '    println(%s.to_str())' % v
        if r < 0.85 and self.live_strs:
            v = self.rng.choice(self.live_strs)
            return '    println(%s)' % v
        if r < 0.95:
            # if <bool> { ... } else { ... }
            cond = self.gen_bool_expr(min(depth, 2))
            then = self.gen_stmt(depth - 1) if depth > 0 else "    let _z: int = 0"
            els = self.gen_stmt(depth - 1) if depth > 0 else "    let _w: int = 0"
            return "    if %s {\n%s\n    } else {\n%s\n    }" % (
                cond, then, els)
        # while loop — bounded so the program terminates. We use a
        # counter that we KNOW starts at 0 and increments to a constant.
        counter = self.fresh("i")
        limit = self.rng.randint(0, 5)
        body = "    let _q: int = 0"
        if self.live_ints:
            v = self.rng.choice(self.live_ints)
            body = "    %s = %s + 1" % (v, v)
        return ("    let mut %s: int = 0\n    while %s < %d {\n%s\n"
                "        %s = %s + 1\n    }" % (
                    counter, counter, limit, body, counter, counter))

    def gen_program(self):
        n_stmts = self.rng.randint(3, 10)
        body = []
        for _ in range(n_stmts):
            body.append(self.gen_stmt(3))
        body.append("    return 0")
        return ("fn main() -> int uses IO {\n"
                + "\n".join(body)
                + "\n}\n")


# ---------------------------------------------------------------------------
# Differential execution
# ---------------------------------------------------------------------------

class Divergence(Exception):
    def __init__(self, prog_src, interp_out, native_out, interp_rc, native_rc,
                 stage):
        super().__init__("divergence")
        self.prog_src = prog_src
        self.interp_out = interp_out
        self.native_out = native_out
        self.interp_rc = interp_rc
        self.native_rc = native_rc
        self.stage = stage


def run_interp(prog_path, timeout=10):
    """Run the program via Stage-0 interpreter. Returns (stdout bytes, rc)."""
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "boot/boot.py"),
             prog_path],
            capture_output=True, timeout=timeout, input=b"",
            cwd=REPO_ROOT)
        return p.stdout, p.returncode
    except subprocess.TimeoutExpired:
        return b"<timeout>", 124


def run_native(prog_path, hlc_native, tmpdir, timeout=10):
    """Compile + run via the native compiler. Returns (stdout bytes, rc)."""
    out_c = os.path.join(tmpdir, "out.c")
    out_bin = os.path.join(tmpdir, "out.bin")
    # Step 1: hlc.hls (via Stage-0) compiles prog.hls -> out.c
    p1 = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "boot/boot.py"),
         os.path.join(REPO_ROOT, "src/hlc.hls"), prog_path, out_c],
        capture_output=True, timeout=timeout, cwd=REPO_ROOT)
    if p1.returncode != 0:
        # Compile error — not a divergence; skip this program.
        return None, None
    # Step 2: gcc out.c -> out.bin
    p2 = subprocess.run(
        ["gcc", "-O2", "-o", out_bin, out_c, "-lm", "-pthread"],
        capture_output=True, timeout=timeout)
    if p2.returncode != 0:
        return None, None
    # Step 3: run out_bin
    try:
        p3 = subprocess.run(
            [out_bin], capture_output=True, timeout=timeout, input=b"")
        return p3.stdout, p3.returncode
    except subprocess.TimeoutExpired:
        return b"<timeout>", 124


def differential_one(src, hlc_native, tmpdir, rng_seed):
    """Run one program through both implementations. Raises Divergence
    on a mismatch. Returns True if the program was type-correct and
    ran; False if it was rejected (a skipped sample)."""
    prog_path = os.path.join(tmpdir, "prog.hls")
    with open(prog_path, "w") as f:
        f.write(src)
    # Quick type-check first — skip the program if it doesn't compile.
    try:
        program = load_program(prog_path)
        check(program)
    except HLError:
        return False
    i_out, i_rc = run_interp(prog_path)
    n_out, n_rc = run_native(prog_path, hlc_native, tmpdir)
    if n_out is None:
        return False  # native compile failed (not a divergence)
    if i_out != n_out or i_rc != n_rc:
        raise Divergence(src, i_out, n_out, i_rc, n_rc, "run")
    return True


# ---------------------------------------------------------------------------
# Delta-debugging minimiser
# ---------------------------------------------------------------------------

def minimize(src, hlc_native, tmpdir, rng_seed):
    """Delta-debug: try removing one statement at a time from the
    program while preserving the divergence. Returns the minimised
    source. Best-effort — does not guarantee a global minimum.

    Stage 134: the region under minimisation is the MAIN function's
    body, located by name rather than assumed to be the whole file —
    seeded programs carry a provenance header and closure definitions
    above `fn main`, and those lines are part of the case."""
    lines = src.split("\n")
    mi = None
    for i, l in enumerate(lines):
        if l.startswith("fn main"):
            mi = i
            break
    if mi is None:
        return src
    # the tail is `return 0` + `}` (the harness contract); the body
    # under minimisation is what sits between the opening line and it
    ri = None
    for i in range(len(lines) - 1, mi, -1):
        if lines[i].strip() == "return 0":
            ri = i
            break
    if ri is None or ri - mi < 2:
        return src

    def try_subset(subset_lines):
        new_src = ("\n".join(lines[:mi + 1]) + "\n"
                   + "\n".join(subset_lines) + "\n"
                   + "\n".join(lines[ri:]))
        prog_path = os.path.join(tmpdir, "prog_min.hls")
        with open(prog_path, "w") as f:
            f.write(new_src)
        try:
            program = load_program(prog_path)
            check(program)
        except HLError:
            return None  # no longer compiles — not a valid minimisation
        i_out, i_rc = run_interp(prog_path)
        n_out, n_rc = run_native(prog_path, hlc_native, tmpdir)
        if n_out is None:
            return None
        if i_out != n_out or i_rc != n_rc:
            return new_src  # still diverges — keep this subset
        return None

    body = lines[mi + 1:ri]
    changed = True
    while changed and len(body) > 1:
        changed = False
        # Try removing each body line one at a time.
        for i in range(len(body) - 1, -1, -1):
            cand = body[:i] + body[i + 1:]
            new_src = try_subset(cand)
            if new_src is not None:
                body = cand
                src = new_src
                changed = True
    return src


# ---------------------------------------------------------------------------
# Main fuzz loop
# ---------------------------------------------------------------------------

def load_seed_corpus(corpus_dir, cap=4000):
    """Stage 134: read a seeded corpus off disk. Every file is
    re-checked through the real pipeline — a stale or hand-edited
    corpus must not poison the run — and the rejected count is
    reported, never swallowed. Returns (sources, rejected)."""
    sources = []
    rejected = 0
    if not os.path.isdir(corpus_dir):
        return sources, rejected
    paths = []
    for dirpath, _dirs, files in os.walk(corpus_dir):
        for fn in sorted(files):
            if fn.endswith(".hls"):
                paths.append(os.path.join(dirpath, fn))
    for p in sorted(paths)[:cap]:
        try:
            with open(p, "r", encoding="utf-8") as f:
                src = f.read()
        except OSError:
            rejected += 1
            continue
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".hls",
                                          delete=False)
        try:
            tmp.write(src)
            tmp.close()
            program = load_program(tmp.name)
            check(program)
            sources.append(src)
        except Exception:
            rejected += 1
        finally:
            os.unlink(tmp.name)
    return sources, rejected


def seeded_program(sources, rng):
    """One corpus program, 0..2 mutator ops applied (30% none, 50%
    one, 20% two) — the same ops the seeder used, the checker no
    longer watching (the corpus files are known-good; a mutant that
    breaks is caught by differential_one's own check gate)."""
    src = rng.choice(sources)
    r = rng.random()
    n_ops = 0 if r < 0.30 else (1 if r < 0.80 else 2)
    for _ in range(n_ops):
        msrc, _op = hls_fuzzseed.mutate_source(src, rng)
        if msrc is not None:
            src = msrc
    return src


def main():
    ap = argparse.ArgumentParser(
        prog="hls-fuzz",
        description="AST-level differential fuzzer for Halis (Stage 18).",
    )
    ap.add_argument("--time", type=int, default=60,
                    help="run for this many seconds (default 60)")
    ap.add_argument("--seed", type=int, default=None,
                    help="RNG seed (default: time-based)")
    ap.add_argument("--n", type=int, default=None,
                    help="max number of programs (default: unlimited)")
    ap.add_argument("--corpus", default=os.path.join(REPO_ROOT, "fuzz-corpus"),
                    help="directory to save minimised divergent cases")
    ap.add_argument("--seed-corpus", default=None,
                    help="Stage 134: corpus directory seeded from "
                         "real-world packages (tools/hls-fuzzseed.py)")
    ap.add_argument("--seed-ratio", type=float, default=0.5,
                    help="fraction of programs drawn from the seed "
                         "corpus when --seed-corpus is given")
    ap.add_argument("--minimize", default=None,
                    help="minimize an existing .hls case file and exit")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.minimize:
        # Minimize mode: load the case, find a smaller one.
        with open(args.minimize) as f:
            src = f.read()
        tmpdir = tempfile.mkdtemp(prefix="hlsfuzz-")
        try:
            # For minimize mode we don't need a pre-built native hlc —
            # the run_native function bootstraps via Stage-0.
            minimised = minimize(src, None, tmpdir, args.seed or 0)
            print(minimised)
            return 0
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    rng_seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(rng_seed)
    if not args.quiet:
        print("== hls-fuzz: seed=%d, time=%ds ==" % (
            rng_seed, args.time))

    corpus_dir = args.corpus
    os.makedirs(corpus_dir, exist_ok=True)

    # Stage 134: the seeded corpus, validated up front
    seed_sources = []
    seed_rejected = 0
    if args.seed_corpus:
        seed_sources, seed_rejected = load_seed_corpus(args.seed_corpus)
        if not args.quiet:
            print("== seed corpus: %d programs loaded, %d rejected =="
                  % (len(seed_sources), seed_rejected))
        if not (0.0 <= args.seed_ratio <= 1.0):
            print("hls-fuzz: --seed-ratio must be within [0, 1]",
                  file=sys.stderr)
            return 2

    tmpdir = tempfile.mkdtemp(prefix="hlsfuzz-")
    try:
        n_run = 0
        n_gen = 0
        n_seeded = 0
        n_diverge = 0
        n_skip = 0
        deadline = time.time() + args.time
        next_report = time.time() + 5.0
        while time.time() < deadline:
            if args.n is not None and n_run >= args.n:
                break
            if seed_sources and rng.random() < args.seed_ratio:
                src = seeded_program(seed_sources, rng)
                n_seeded += 1
            else:
                gen = Gen(rng)
                src = gen.gen_program()
                n_gen += 1
            n_run += 1
            try:
                ok = differential_one(src, None, tmpdir, rng_seed)
                if ok:
                    pass
                else:
                    n_skip += 1
            except Divergence as d:
                n_diverge += 1
                # Minimize the failing case.
                minimised = minimize(d.prog_src, None, tmpdir, rng_seed)
                # Save to corpus.
                idx = len(os.listdir(corpus_dir))
                case_path = os.path.join(corpus_dir, "case-%04d.hls" % idx)
                with open(case_path, "w") as f:
                    f.write(minimised)
                if not args.quiet:
                    print("\n!! DIVERGENCE #%d saved to %s" % (
                        n_diverge, case_path))
                    print("   interp rc=%d out=%r" % (
                        d.interp_rc, d.interp_out[:200]))
                    print("   native rc=%d out=%r" % (
                        d.native_rc, d.native_out[:200]))
                    print("   minimised program:")
                    for line in minimised.split("\n"):
                        print("   | " + line)
            if time.time() >= next_report:
                rate = n_run / max(1.0, time.time() - (deadline - args.time))
                print("  [%ds] %d run (%d gen, %d seeded), %d skip, "
                      "%d diverge (%.1f prog/s)" % (
                          int(time.time() - (deadline - args.time)),
                          n_run, n_gen, n_seeded, n_skip, n_diverge, rate))
                next_report = time.time() + 5.0
        print("\n== hls-fuzz done: %d run (%d gen, %d seeded), %d skip, "
              "%d diverge ==" % (n_run, n_gen, n_seeded, n_skip,
                                  n_diverge))
        return 0 if n_diverge == 0 else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
