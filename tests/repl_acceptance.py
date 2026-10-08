#!/usr/bin/env python3
"""Stage 123 acceptance gate — hls-repl, the interactive REPL.

Run with `make repl-acceptance` (or `python3 tests/repl_acceptance.py`).

Eight sections over the REAL REPL subprocess (piped stdin, the same
interface a user's terminal drives):

  1. the session     — expressions evaluate and print `= <value>` (ints,
     is a program      floats, bools, strings, lists, structs, enums,
                       match-as-expression); lets and assignments persist;
                       the checker's rejections never touch the session
                       (type mismatch, shadowing, unknown variable); a
                       redefinition is refused with the fix; one line can
                       carry several statements
  2. definitions     — multi-line fn/struct/impl/enum/generic at the prompt
     at the prompt     (bracket-honest continuation), std./core. imports,
                       forward reference honestly refused, method chains
                       and generic calls evaluate
  3. nothing is      — a committed println prints ONCE (the next input
     re-run             does not replay it); a loop's state advances
                       across inputs; rand_int's sequence marches forward;
                       a write_file once is visible to a later read_file
                       and written exactly once
  4. :type           — the checker's own annotation: primitives, list[T],
                       generic instantiation, method returns, struct
                       fields, map values, `: void` for void calls, and
                       the error path (unknown variable, garbage)
  5. :effects        — `(none - pure)`; IO; Fs in a fresh session; the
                       `(new: ...)` delta when the expression needs more
                       than the session holds; `none beyond the session's`
                       when it does not; the session block's grown uses
                       clause visible in :audit while a USER fn missing an
                       effect is reported, never repaired
  6. the session     — :audit names every session fn with OK statuses and
     commands           the same table shape boot.py prints; :env lists
                       declared types with values and marks moved
                       bindings; :load loads a module-style file (callable),
                       refuses a file with main and a missing file;
                       :reset clears everything; unknown commands are
                       refused by name
  7. panics and      — `1/0` at the prompt reports input-relative; a
     refusals           panicking statement input commits nothing (its
                       binding stays unknown); a panic inside a called fn
                       reports the fn-relative line; the session survives
                       all of it; exit/return refused with the reason;
                       the reserved __repl_ prefix cannot be bound
  8. the corpus      — the canonical session's `=` lines are byte-identical
     and parity         with the fixture program's output (interpreter and,
                       when bin/hlc + gcc exist, native); the fixture is
                       hlfmt-canonical and hllint-clean; boot --check green;
                       a piped session is deterministic (same input, same
                       transcript bytes)

Exit code 0 = all acceptance criteria met.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

REPL = [sys.executable, "tools/hls-repl.py"]
BOOT = [sys.executable, "boot/boot.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
HLC_BIN = HLC if os.path.isfile(HLC) else None

FIXTURE = os.path.join(ROOT, "tests", "ok", "feat_stage123_repl.hls")
LIB = os.path.join(ROOT, "tests", "repl_libs", "session_lib.hls")

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s123_", dir=os.path.join(ROOT, "tests"))


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


def run_session(lines):
    """Feed lines to the real REPL; return (stdout, stderr, returncode)."""
    data = "\n".join(lines) + "\n"
    p = subprocess.run(REPL, input=data, capture_output=True, text=True,
                       cwd=ROOT, timeout=120)
    return p.stdout, p.stderr, p.returncode


def value_lines(out):
    """The transcript's result lines (`= ...` and `: ...` and `effects: ...`)."""
    return [l for l in out.splitlines()
            if l.startswith("= ") or l.startswith(": ")
            or l.startswith("effects: ")]


# ---------------------------------------------------------------------------
print("Section 1 — the session is a program: state, rejections, red lines")
out, err, rc = run_session([
    "1 + 2",
    "2.5 * 4.0",
    "1 < 2",
    '"halis" + " " + "repl"',
    "[10, 20, 30]",
    "let mut x: int = 6",
    "x * 7",
    "x = x + 1",
    "x",
])
check(rc == 0, "clean session exits 0")
check("= 3" in out.splitlines(), "integer expression prints = 3")
check("= 10.0" in out.splitlines(), "float expression prints = 10.0")
check("= true" in out.splitlines(), "bool prints the HLS literal")
check('= "halis repl"' in out.splitlines(), "strings print quoted")
check("= [10, 20, 30] (len=3)" in out.splitlines(),
      "lists print with length")
check("= 42" in out.splitlines(), "let persists into the next expression")
check("= 7" in out.splitlines(), "assignment rewrites the binding")
check(err == "", "no stderr noise")

out, _, _ = run_session([
    "let mut n: int = 5",
    'n = "five"',
    "n + 1",
])
check("error: type mismatch" in out, "type mismatch is refused")
check("= 6" in out.splitlines(), "the rejected input left n untouched")

out, _, _ = run_session([
    "let x: int = 6",
    "x = 7",
])
check("cannot reassign immutable variable" in out,
      "immutable bindings are enforced across inputs")

out, _, _ = run_session([
    "let v: int = 1",
    "let v: int = 2",
])
check("error:" in out and "v" in out,
      "shadowing a session binding is refused")

out, _, _ = run_session([
    "ghost",
])
check("error: variable does not exist: ghost" in out,
      "unknown variables are refused by name")

out, _, _ = run_session([
    "let a: int = 1 let b: int = 2",
    "a + b",
])
check("= 3" in out.splitlines(), "one line can carry several statements")

out, _, _ = run_session([
    "fn f() -> int { return 1 }",
    "fn f() -> int { return 2 }",
])
check("duplicate function name: f" in out,
      "redefining a fn is refused (the session is one program)")

# ---------------------------------------------------------------------------
print("Section 2 — definitions at the prompt: multi-line, imports, generics")
out, _, _ = run_session([
    "fn fact(n: int) -> int {",
    "  if n <= 1 { return 1 }",
    "  return n * fact(n - 1)",
    "}",
    "fact(10)",
])
check("= 3628800" in out.splitlines(),
      "a multi-line fn defines silently and calls")

out, _, _ = run_session([
    'import "std.str"',
    "str(42) + \"!\"",
])
check('= "42!"' in out.splitlines(),
      "std imports resolve at the prompt")

out, _, _ = run_session([
    'import "core.option"',
    "Option.Some(9)",
])
check("= Option.Some(9)" in out.splitlines(), "core imports and enum display")

out, _, _ = run_session([
    "struct P { x: int, y: int }",
    "impl P {",
    "  fn norm2(self: P) -> int { return self.x * self.x + self.y * self.y }",
    "}",
    "let p: P = P { x: 2, y: 3 }",
    "p.norm2()",
    "p.y",
])
check("= 13" in out.splitlines(), "struct + impl method evaluate")
check("= 3" in out.splitlines(), "field access evaluates")

out, _, _ = run_session([
    "fn last_of[T](xs: list[T]) -> T {",
    "  return xs[xs.len() - 1]",
    "}",
    "last_of([3, 1, 4])",
    'last_of(["a", "b", "c"])',
])
check("= 4" in out.splitlines(), "generic fn, int instantiation")
check('= "c"' in out.splitlines(), "generic fn, str instantiation")

out, _, _ = run_session([
    "later(1)",
    "fn later(n: int) -> int { return n }",
])
check("error: function does not exist: later" in out,
      "forward reference is honestly refused (definitions bind, not time-travel)")
out, _, _ = run_session([
    "fn later(n: int) -> int { return n }",
    "later(1)",
])
check("= 1" in out.splitlines(), "the same fn works once defined")

# ---------------------------------------------------------------------------
print("Section 3 — nothing is ever re-run behind your back")
out, _, _ = run_session([
    'println("once")',
    "1 + 1",
    "2 + 2",
])
lines = out.splitlines()
check(lines.count("once") == 1, "a committed println is NOT replayed")

out, _, _ = run_session([
    "let mut c: int = 0",
    "c = c + 1",
    "c = c + 1",
    "c",
])
check("= 2" in out.splitlines(), "state advances across inputs")

out, _, _ = run_session([
    "rand_seed(7)",
    "rand_int(1000)",
    "rand_int(1000)",
])
vals = [l for l in out.splitlines() if l.startswith("= ") and l[2:].isdigit()]
check(len(vals) == 2 and vals[0] != vals[1],
      "the RNG marches forward (two draws differ, same session)")

sandbox = os.path.join(TMP, "fs")
os.makedirs(sandbox, exist_ok=True)
probe = os.path.join(sandbox, "probe.txt")
out, _, _ = run_session([
    'write_file("%s", "written once")' % probe,
    'read_file("%s")' % probe,
    'read_file("%s")' % probe,
])
check('= "written once"' in out.splitlines(),
      "a write_file once is visible to later reads")
with open(probe, "rb") as f:
    body = f.read()
check(body == b"written once", "and the file holds exactly one write")

# ---------------------------------------------------------------------------
print("Section 4 — :type reads the checker's annotation")
out, _, _ = run_session([
    ":type 1 + 2",
    ":type 2.5",
    ":type \"s\"",
    ":type true",
    ":type [1, 2]",
    ":type 1.0 > 0.5",
])
check(": int" in out.splitlines(), ":type int")
check(": float" in out.splitlines(), ":type float")
check(": str" in out.splitlines(), ":type str")
check(": bool" in out.splitlines(), ":type bool")
check(": list[int]" in out.splitlines(), ":type list[int]")

out, _, _ = run_session([
    "let m: map[str, int] = map_new()",
    ":type m",
    "fn id[T](x: T) -> T { return x }",
    ":type id([1])",
    ":type id(\"s\")",
])
check(": map[str, int]" in out.splitlines(), ":type map[str, int]")
check(": list[int]" in out.splitlines(), ":type pins a generic instantiation")
check(": str" in out.splitlines(), ":type re-instantiates per call site")

out, _, _ = run_session([
    "struct P { x: int, y: int }",
    "impl P { fn n2(self: P) -> int { return 4 } }",
    "let p: P = P { x: 1, y: 1 }",
    ":type p.x",
    ":type p.n2()",
    ":type println(\"x\")",
])
check(": int" in out.splitlines(), ":type through a field and a method")

out, _, _ = run_session([
    ":type nope + 1",
])
check("error: variable does not exist: nope" in out,
      ":type surfaces the checker's refusal")

out, _, _ = run_session([
    ":type",
])
check("error: :type expects an expression" in out,
      ":type without an argument is refused")

# ---------------------------------------------------------------------------
print("Section 5 — :effects reads the fixpoint, relative to the session")
out, _, _ = run_session([
    ":effects 1 + 2",
])
check("effects: (none - pure)" in out.splitlines(),
      "a pure expression is named pure")

out, _, _ = run_session([
    ':effects println("x")',
])
check("effects: IO" in out.splitlines(), "println needs IO")

out, _, _ = run_session([
    ':effects read_file("f")',
])
check("effects: Fs" in out.splitlines(), "read_file needs Fs (fresh session)")

out, _, _ = run_session([
    'println("committed")',
    ':effects read_file("f")',
])
check("effects: Fs, IO (new: Fs)" in out.splitlines(),
      "the delta names what the expression ADDS")

out, _, _ = run_session([
    'println("committed")',
    ':effects print("y")',
])
check("effects: none beyond the session's (IO)" in out.splitlines(),
      "an expression needing nothing new says so honestly")

out, _, _ = run_session([
    ":effects rand_int(3)",
])
check("effects: Rand" in out.splitlines(), "Rand is its own family")

out, _, _ = run_session([
    "fn leaky() -> int uses IO {",
    '  println("leak")',
    "  return 1",
    "}",
    ":audit",
])
check("main" in out and "leaky" in out and "OK" in out,
      ":audit shows the session block and the user fn")
check("uses IO" in out or "IO" in out, "the declared capability is visible")

out, _, _ = run_session([
    "fn leaky() -> int {",
    '  println("leak")',
    "  return 1",
    "}",
])
check("error: function 'leaky' calls 'println' which requires effect 'IO'"
      in out,
      "a USER fn missing an effect is reported, never auto-repaired")

# ---------------------------------------------------------------------------
print("Section 6 — the session commands")
out, _, _ = run_session([
    "fn twice(n: int) -> int { return n * 2 }",
    "fn noisy() -> int uses IO {",
    '  println("n")',
    "  return 1",
    "}",
    ":audit",
])
check("function" in out and "declared" in out and "computed" in out
      and "status" in out,
      ":audit prints boot.py's exact table shape")
check("twice" in out and "noisy" in out and "main" in out,
      ":audit names every session fn")
check("3 functions: 0 declared pure, 1 declared with effects" in out,
      ":audit's summary counts the session program's fns")

out, _, _ = run_session([
    "let a: int = 7",
    "let mut names: list[str] = [\"ada\"]",
    ":env",
])
check("a: int = 7" in out, ":env shows declared type and value")
check("names: mut list[str] = [\"ada\"] (len=1)" in out,
      ":env marks mut and renders lists")

out, _, _ = run_session([
    "let a: list[int] = [1]",
    "let b: list[int] = take(a)",
    ":env",
    "a",
])
check("b: list[int] = [1] (len=1)" in out,
      ":env shows the take'd binding")
check("use of moved value: a" in out,
      "while the checker holds a as moved (static ownership)")

out, _, _ = run_session([
    ":env",
])
check("session bindings: (none)" in out.splitlines(),
      ":env on a fresh session says none")

out, _, _ = run_session([
    ":load " + LIB,
    "square(9)",
    'shout("hls")',
    "let mtr: Meter = Meter { reading: 100 }",
    "mtr.advance(42).reading",
])
check("loaded" in out and "session_lib.hls" in out, ":load reports the load")
check("= 81" in out.splitlines(), "the loaded fn is callable")
check('= "HLS!"' in out.splitlines(), "the loaded std-using fn works")
check("= 142" in out.splitlines(), "the loaded struct + method work")

with open(os.path.join(TMP, "with_main.hls"), "w") as f:
    f.write("fn main() -> int { return 0 }\n")
out, _, _ = run_session([
    ":load %s" % os.path.join(TMP, "with_main.hls"),
])
check("refuses files that define main" in out,
      ":load refuses files that define main")

out, _, _ = run_session([
    ":load /nonexistent/never.hls",
])
check("error: cannot read" in out, ":load reports a missing file")

out, _, _ = run_session([
    "let k: int = 5",
    ":reset",
    ":env",
    "k",
])
check("session cleared" in out.splitlines(), ":reset announces itself")
check("session bindings: (none)" in out.splitlines(), ":reset clears bindings")
check("error: variable does not exist: k" in out,
      ":reset really cleared (k is unknown)")

out, _, _ = run_session([
    ":frobnicate",
])
check("error: unknown command ':frobnicate' (try :help)" in out,
      "unknown commands are refused by name")

out, _, _ = run_session([
    ":help",
])
check(":type <expr>" in out and ":effects <expr>" in out
      and ":audit" in out and ":env" in out and ":load <file.hls>" in out
      and ":reset" in out and ":quit" in out,
      ":help lists every command")

# ---------------------------------------------------------------------------
print("Section 7 — panics and refusals, and the session survives them")
out, _, _ = run_session([
    "1 / 0",
    "let survivor: int = 1",
    "survivor",
])
check("panic: division by zero (at line 1)" in out.splitlines(),
      "an expression panic reports input-relative")
check("= 1" in out.splitlines(), "the session continues after a panic")

out, _, _ = run_session([
    "let dead: int = 1 / 0",
    "dead",
])
check("panic: division by zero (at line 1)" in out.splitlines(),
      "a statement panic reports and commits nothing")
check("error: variable does not exist: dead" in out,
      "the panicked binding stays unknown (rollback is real)")

out, _, _ = run_session([
    "fn boom(n: int) -> int {",
    "  return n / 0",
    "}",
    "boom(3)",
])
check("panic: division by zero (at line 2)" in out.splitlines(),
      "a panic inside a called fn reports the fn-relative line")

out, _, _ = run_session([
    "exit(0)",
])
check("exit(0) is refused at the prompt" in out,
      "exit is refused (the session is not a process)")

out, _, _ = run_session([
    "fn fine() -> int { return 1 }",
    "return 5",
    "fine()",
])
check("error:" in out and "return" in out, "return is refused with the reason")
check("= 1" in out.splitlines(), "and the session carries on")

out, _, _ = run_session([
    "__repl_value",
])
check("reserved by the REPL" in out, "the __repl_ prefix is reserved")

out, _, _ = run_session([
    "let __repl_x: int = 1",
])
check("reserved by the REPL" in out, "binding __repl_* is refused too")

out, _, _ = run_session([
    "fn main() -> int { return 0 }",
])
check("duplicate function name: main" in out
      and "session block" in out,
      "redefining main names the session block")

# ---------------------------------------------------------------------------
print("Section 8 — the corpus, transcript parity and determinism")
with open(FIXTURE, "rb") as f:
    fixture_src = f.read().decode("utf-8")
check("fn main() uses IO" in fixture_src,
      "the fixture's main carries the grown session capability")

p = subprocess.run(BOOT + [FIXTURE], capture_output=True, text=True,
                   cwd=ROOT, timeout=120)
fixture_out = p.stdout
check(p.returncode == 0, "the fixture runs on the interpreter")
check("= Acc { total: 42, count: 2 }" in fixture_out.splitlines(),
      "the fixture prints the transcript's struct line")

p = subprocess.run(BOOT + ["--check", FIXTURE], capture_output=True,
                   text=True, cwd=ROOT, timeout=120)
check(p.returncode == 0 and "OK: types and effects valid" in p.stdout,
      "boot --check is green on the fixture")

p = subprocess.run([sys.executable, "tools/hlfmt.py", "-c", FIXTURE],
                   capture_output=True, text=True, cwd=ROOT, timeout=60)
check("already formatted" in p.stdout, "the fixture is an hlfmt fixed point")

p = subprocess.run([sys.executable, "tools/hllint.py", FIXTURE],
                   capture_output=True, text=True, cwd=ROOT, timeout=60)
check("no warnings" in p.stdout, "the fixture is hllint-clean")

if HLC_BIN:
    out_c = os.path.join(TMP, "s123.c")
    out_bin = os.path.join(TMP, "s123")
    p = subprocess.run([HLC_BIN, FIXTURE, out_c], capture_output=True,
                       text=True, cwd=ROOT, timeout=300)
    if p.returncode != 0:
        bad("native compile failed: %s" % p.stderr[:200])
    else:
        p = subprocess.run(["gcc", "-O2", "-o", out_bin, out_c, "-lm",
                            "-pthread"], capture_output=True, text=True,
                           timeout=120)
        if p.returncode != 0:
            bad("gcc failed: %s" % p.stderr[:200])
        else:
            p = subprocess.run([out_bin], capture_output=True, text=True,
                               timeout=60)
            check(p.stdout == fixture_out,
                  "native output is byte-identical with the interpreter's")
else:
    print("  [SKIP] native parity (bin/hlc not built)")

# the canonical session's transcript equals the fixture's output, and a
# second run is byte-identical (deterministic piped transcripts)
CANONICAL = [
    'import "std.str"',
    "struct Acc { total: int, count: int }",
    "enum Op { Add(int), Sub(int), Mul(int) }",
    "impl Acc {",
    "    fn push(self: Acc, v: int) -> Acc {",
    "        return Acc { total: self.total + v, count: self.count + 1 }",
    "    }",
    "    fn mean(self: Acc) -> int {",
    "        return self.total / self.count",
    "    }",
    "}",
    "fn apply(acc: Acc, op: Op) -> Acc {",
    "    return match op {",
    "        Op.Add(v) => acc.push(v),",
    "        Op.Sub(v) => acc.push(0 - v),",
    "        Op.Mul(v) => acc.push(acc.total * v)",
    "    }",
    "}",
    "fn last_of[T](xs: list[T]) -> T {",
    "    return xs[xs.len() - 1]",
    "}",
    "fn op_name(op: Op) -> str {",
    "    return match op {",
    '        Op.Add(_) => "add",',
    '        Op.Sub(_) => "sub",',
    '        Op.Mul(_) => "mul"',
    "    }",
    "}",
    "let a: Acc = Acc { total: 0, count: 0 }",
    "let a2: Acc = apply(a, Op.Add(10))",
    "let a3: Acc = apply(a2, Op.Add(32))",
    "a3",
    "a3.mean()",
    "apply(a3, Op.Sub(2)).total",
    "op_name(Op.Add(1))",
    "op_name(Op.Mul(5))",
    "op_name(Op.Sub(9))",
    '"repl" + "." + "hls"',
    "1 < 2",
    "[7, 8]",
    "last_of([3, 1, 4, 1, 5])",
    'last_of(["x", "y", "z"])',
    ":quit",
]
out1, _, rc1 = run_session(CANONICAL)
out2, _, rc2 = run_session(CANONICAL)
check(rc1 == 0 and rc2 == 0, "the canonical session exits clean, twice")
check(out1 == out2, "the piped transcript is deterministic")
check(out1 == fixture_out,
      "the canonical session's transcript IS the fixture's output")

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
if FAIL == 0:
    print("ACCEPTANCE OK: Stage 123 — hls-repl (%d checks)" % PASS)
    sys.exit(0)
print("ACCEPTANCE FAILED: %d of %d checks failed" % (FAIL, PASS + FAIL))
sys.exit(1)
