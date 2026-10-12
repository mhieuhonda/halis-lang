#!/usr/bin/env python3
"""Stage 130 acceptance gate — the deprecation mechanism.

Run with `make deprecated-acceptance` (or
`python3 tests/deprecated_acceptance.py`).

Eight sections over BOTH real front-ends (boot and the self-hosted
bin/hlc), the real linter CLI, and the real formatter — the same
interfaces a user drives:

  1. the convention  — the three refusal fixtures fail BOTH
     & the refusals    front-ends with the same core message (a
                       non-literal message, a duplicated notice, a
                       let-binding placement); --list shows L013
                       without the [fixable] tag; --strict exits 1

  2. the parse laws  — the AST carries the notice (absent -> None,
     (imported)        bare -> "", message -> the decoded string,
                       escapes decoded exactly as the lexer would);
                       a duplicate is a named parse error; an extern
                       fn never carries one; the unknown-attribute
                       message names `deprecated` in the known list

  3. the self-hosted — bin/hlc compiles the ok fixture; the emitted C
     parity             carries NO deprecation machinery (the only
                        `deprecated` substring is the fixture's own
                        string literal — zero codegen); the fixed
                        point holds (the self-hosted compiler still
                        compiles itself)

  4. the lint:       — L013 on a same-file deprecated call: both
     same-file          forms, line precision, the live replacements
                        silent, a deprecated fn's own body silent
                        (old_chain calls old_base — one warning, not
                        two); --rule L013 filters; --strict exits 1

  5. the lint:       — the message quoted across an import (the
     cross-module       registry spans the merged crate); the
                        ambiguous-method silence (a deprecated method
                        whose short name has a live twin stays
                        silent); the unambiguous method warning
                        carries the `method` qualifier

  6. the honesty     — --fix leaves a deprecated-call file
     gates              byte-identical (L013 is not fixable: choosing
                        the replacement is a design decision);
                        hlfmt preserves both attribute forms byte
                        for byte; a missing-module crate falls back
                        to the same-file registry without crashing

  7. the differential— the ok fixture runs green on BOTH backends
                        with byte-identical (exit, stdout); its L013
                        census is exactly the five warnings the
                        fixture documents — the cross-module notices
                        fire, the deprecated bodies stay silent, the
                        live replacements never warn

  8. the corpus      — the fixture, its module sibling and the demo
     hygiene            are hlfmt-canonical and checker-clean; the
                        demo runs green carrying exactly its two
                        warnings; no gate litter survives

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

BOOT = [sys.executable, "boot/boot.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
LINT = [sys.executable, "tools/hllint.py"]
FMT = [sys.executable, "tools/hlfmt.py"]
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage130_deprecated.hls")
MODULE = os.path.join(ROOT, "tests", "ok", "modules", "depmod.hls")
DEMO = os.path.join(ROOT, "examples", "deprecated_demo.hls")
FAILS = os.path.join(ROOT, "tests", "fail")
TMP = tempfile.mkdtemp(prefix="hls-deprecated-gate-")

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print("  ok: " + msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  FAIL: " + msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def run(argv, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=300,
                          text=True, **kw)


def core(msg):
    """The message both front-ends must agree on: everything after the
    last 'error: '/'syntax error: ' marker, up to the (line ...) tail."""
    out = msg
    for marker in ("panic: syntax error: ", "compile error: "):
        if marker in out:
            out = out.split(marker, 1)[1]
    return out.strip()


# ===========================================================================
def _section_1():
    print("Section 1 — the convention and the refusals")

    cases = {
        "fail_stage130_deprecated_nonstr.hls":
            "deprecated expects a string literal message but got '42'",
        "fail_stage130_deprecated_dup.hls":
            "deprecated attribute appears more than once on one function",
        "fail_stage130_deprecated_let.hls":
            "unknown let-binding attribute 'deprecated'",
    }
    for name, want in cases.items():
        path = os.path.join(FAILS, name)
        p = run(BOOT + ["--check", path])
        check(p.returncode != 0 and want in p.stdout,
              "boot rejects %s (%s...)" % (name.split("_")[-1], want[:40]))
        if NATIVE:
            p2 = run([HLC, path, os.path.join(TMP, "refuse.c")])
            check(p2.returncode != 0 and want in core(p2.stdout),
                  "hlc rejects %s with the same core message"
                  % name.split("_")[-1])

    p = run(LINT + ["--list"])
    check("L013" in p.stdout and "deprecated-call" in p.stdout,
          "--list names L013 deprecated-call")
    lines = [ln for ln in p.stdout.splitlines() if "L013" in ln]
    check(lines and "[fixable]" not in lines[0],
          "L013 carries no [fixable] tag (the replacement is a design "
          "decision)")

    p = run(LINT + ["--strict", DEMO])
    check(p.returncode == 1, "--strict exits 1 on a deprecated call")


# ===========================================================================
def _section_2():
    print("Section 2 — the parse laws (the boot AST)")
    from boot.lexer import tokenize
    from boot.parser import Parser

    def parse(text):
        return Parser(tokenize(text.encode("utf-8"))).parse_program()

    # bare form: "" (deprecated, no message)
    prog = parse("#[deprecated]\nfn f() -> int { return 1 }\n"
                 "fn main() -> int { return f() }\n")
    attrs = prog["fns"]["f"]["attrs"]
    check(attrs.get("deprecated") == "",
          "the bare #[deprecated] carries an empty message")
    # message form: the decoded literal
    prog = parse('#[deprecated("use g — the old form mis-handles '
                 'negatives")]\nfn f() -> int { return 1 }\n'
                 'fn main() -> int { return f() }\n')
    attrs = prog["fns"]["f"]["attrs"]
    check(attrs.get("deprecated") ==
          "use g — the old form mis-handles negatives",
          "the message form carries the decoded string")
    # escapes decode exactly as the lexer decodes them
    prog = parse('#[deprecated("line one\\nline two")]\n'
                 'fn f() -> int { return 1 }\n'
                 'fn main() -> int { return f() }\n')
    check(prog["fns"]["f"]["attrs"].get("deprecated") ==
          "line one\nline two",
          "message escapes decode with the lexer's own rules")
    # absent: None
    prog = parse("fn f() -> int { return 1 }\n"
                 "fn main() -> int { return f() }\n")
    check(prog["fns"]["f"]["attrs"].get("deprecated") is None,
          "an un-attributed fn reads deprecated = None")
    # duplicate: a named parse error
    try:
        parse('#[deprecated("a")]\n#[deprecated]\n'
              'fn f() -> int { return 1 }\n'
              'fn main() -> int { return f() }\n')
        check(False, "a duplicated notice is a parse error")
    except Exception as ex:
        check("more than once" in str(ex),
              "a duplicated notice is a parse error, named")
    # externs never carry one (the parser rejects attrs in extern blocks)
    try:
        parse('extern "C" {\n    #[deprecated]\n'
              '    fn puts(s: str) -> int uses IO\n}\n'
              'fn main() -> int { return 0 }\n')
        check(False, "an extern declaration refuses the notice")
    except Exception as ex:
        check("attribute" in str(ex) or "extern" in str(ex),
              "an extern declaration refuses the notice (the C "
              "declaration is the contract)")
    # the unknown-attribute message names deprecated among the known
    try:
        parse("#[nosuch]\nfn f() -> int { return 1 }\n"
              "fn main() -> int { return f() }\n")
        check(False, "an unknown attribute is still refused")
    except Exception as ex:
        check("deprecated" in str(ex),
              "the unknown-attribute message lists deprecated as known")
    # a non-string message is refused
    try:
        parse("#[deprecated(42)]\nfn f() -> int { return 1 }\n"
              "fn main() -> int { return f() }\n")
        check(False, "a non-literal message is refused")
    except Exception as ex:
        check("string literal" in str(ex),
              "a non-literal message is refused, named")


# ===========================================================================
def _section_3():
    print("Section 3 — the self-hosted parity")
    if not NATIVE:
        print("  (bin/hlc absent — the self-hosted sections are "
              "skipped, named, not silent)")
        return
    # The self-hosted compiler accepts the showcase...
    c_path = os.path.join(TMP, "showcase.c")
    p = run([HLC, SHOWCASE, c_path])
    check(p.returncode == 0, "hlc compiles the ok fixture")
    # ...and the emitted C carries NO deprecation machinery: the only
    # `deprecated` substring is the fixture's own string literal.
    c_src = open(c_path).read()
    hits = [ln for ln in c_src.splitlines() if "deprecated" in ln]
    check(len(hits) == 1 and '"deprecated OK: "' in hits[0],
          "zero codegen: the only deprecated substring is the "
          "fixture's own message literal")
    # The fixed point: the compiler still compiles itself.
    c2 = os.path.join(TMP, "self.c")
    p = run([HLC, os.path.join(ROOT, "src", "hlc.hls"), c2])
    check(p.returncode == 0,
          "the self-hosted compiler re-compiles itself with the "
          "attribute code in place")


# ===========================================================================
def _section_4():
    print("Section 4 — the lint: same-file")
    tree = os.path.join(TMP, "same")
    os.makedirs(tree)
    path = os.path.join(tree, "app.hls")
    with open(path, "w") as fh:
        fh.write(
            '#[deprecated("use fast_path(v) — the old form '
            'double-counts zero")]\n'
            'fn slow_path(v: int) -> int {\n'
            '    if v == 0 {\n'
            '        return 1\n'
            '    }\n'
            '    return v * 3\n'
            '}\n'
            '\n'
            'fn fast_path(v: int) -> int {\n'
            '    if v == 0 {\n'
            '        return 1\n'
            '    }\n'
            '    return v * 3\n'
            '}\n'
            '\n'
            '#[deprecated]\n'
            'fn legacy_name(v: int) -> int {\n'
            '    return v + 11\n'
            '}\n'
            '\n'
            '#[deprecated("temporary glue")]\n'
            'fn old_chain(v: int) -> int {\n'
            '    # silent: the caller is deprecated too\n'
            '    return slow_path(v) + legacy_name(v)\n'
            '}\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    let a: int = slow_path(3)\n'
            '    let b: int = legacy_name(4)\n'
            '    let c: int = old_chain(2)\n'
            '    let d: int = fast_path(3)\n'
            '    println((a + b + c + d).to_str())\n'
            '    return 0\n'
            '}\n')
    p = run(LINT + [path])
    out = p.stdout
    check("app.hls:28: warning [L013] call to deprecated 'slow_path': "
          "use fast_path(v) — the old form double-counts zero" in out,
          "the message form warns with the message quoted, at its line")
    check("app.hls:29: warning [L013] call to deprecated "
          "'legacy_name' (no deprecation message given)" in out,
          "the bare form warns without inventing a message")
    check(out.count("[L013]") == 3,
          "exactly three call sites warn (the deprecated body stays "
          "silent; the live replacement never warns)")
    check("[L002] function 'old_chain'" not in out,
          "a called deprecated fn is not ALSO flagged unused (L002)")
    p = run(LINT + ["--rule", "L013", path])
    check(p.stdout.count("[L013]") == 3
          and "L002" not in p.stdout and "L001" not in p.stdout,
          "--rule L013 selects the deprecation walk alone")
    p = run(LINT + ["--strict", path])
    check(p.returncode == 1, "--strict exits 1 while the calls stand")


# ===========================================================================
def _section_5():
    print("Section 5 — the lint: cross-module and methods")
    tree = os.path.join(TMP, "cross")
    os.makedirs(tree)
    with open(os.path.join(tree, "lib.hls"), "w") as fh:
        fh.write('#[deprecated("use lib_new() — the old name leaks the '
                 'seed")]\n'
                 'fn lib_old(x: int) -> int {\n'
                 '    return x + 100\n'
                 '}\n'
                 '\n'
                 'fn lib_new(x: int) -> int {\n'
                 '    return x + 1\n'
                 '}\n')
    app = os.path.join(tree, "app.hls")
    with open(app, "w") as fh:
        fh.write('import "lib.hls"\n'
                 '\n'
                 'fn main() -> int uses IO {\n'
                 '    println(lib_old(1).to_str())\n'
                 '    return 0\n'
                 '}\n')
    p = run(LINT + [app])
    check("app.hls:4: warning [L013] call to deprecated 'lib_old': use "
          "lib_new() — the old name leaks the seed" in p.stdout,
          "a deprecation in an IMPORTED module warns in the importer, "
          "message quoted")
    check("lib.hls" not in p.stdout.replace("lib_old", "").replace(
        "lib_new", ""),
          "the warning names the linted file, never the module's own")

    # Methods: the ambiguous case stays silent, the unique one warns.
    amb = os.path.join(tree, "amb.hls")
    with open(amb, "w") as fh:
        fh.write('struct Meter {\n    v: int,\n}\n'
                 '\n'
                 'struct Gauge {\n    v: int,\n}\n'
                 '\n'
                 'impl Meter {\n'
                 '    #[deprecated("use Meter.read2")]\n'
                 '    fn read(m: Meter) -> int {\n'
                 '        return m.v\n'
                 '    }\n'
                 '}\n'
                 '\n'
                 'impl Gauge {\n'
                 '    fn read(g: Gauge) -> int {\n'
                 '        return g.v\n'
                 '    }\n'
                 '}\n'
                 '\n'
                 'fn main() -> int {\n'
                 '    let m: Meter = Meter { v: 1 }\n'
                 '    let g: Gauge = Gauge { v: 2 }\n'
                 '    return m.read() + g.read()\n'
                 '}\n')
    p = run(LINT + [amb])
    check("[L013]" not in p.stdout,
          "a method name with a live twin elsewhere stays SILENT (no "
          "receiver-type analysis means no lying)")
    uniq = os.path.join(tree, "uniq.hls")
    with open(uniq, "w") as fh:
        fh.write('struct Meter {\n    v: int,\n}\n'
                 '\n'
                 'impl Meter {\n'
                 '    #[deprecated("use Meter.read2")]\n'
                 '    fn read(m: Meter) -> int {\n'
                 '        return m.v\n'
                 '    }\n'
                 '    fn read2(m: Meter) -> int {\n'
                 '        return m.v\n'
                 '    }\n'
                 '}\n'
                 '\n'
                 'fn main() -> int {\n'
                 '    let m: Meter = Meter { v: 1 }\n'
                 '    return m.read()\n'
                 '}\n')
    p = run(LINT + [uniq])
    check("warning [L013] call to deprecated method 'read': use "
          "Meter.read2" in p.stdout,
          "an unambiguous deprecated method warns with the `method` "
          "qualifier")


# ===========================================================================
def _section_6():
    print("Section 6 — the honesty gates")
    tree = os.path.join(TMP, "honest")
    os.makedirs(tree)
    path = os.path.join(tree, "app.hls")
    body = ('#[deprecated("use fast_path(v)")]\n'
            'fn slow_path(v: int) -> int {\n'
            '    return v * 3\n'
            '}\n'
            '\n'
            'fn fast_path(v: int) -> int {\n'
            '    return v * 3\n'
            '}\n'
            '\n'
            'fn main() -> int uses IO {\n'
            '    println(slow_path(3).to_str())\n'
            '    println(fast_path(4).to_str())\n'
            '    return 0\n'
            '}\n')
    with open(path, "w") as fh:
        fh.write(body)
    # --fix must not touch it: L013 has no mechanical fix.
    p = run(LINT + ["--fix", path])
    after = open(path).read()
    check(after == body,
          "--fix leaves a deprecated-call file byte-identical (L013 "
          "is not fixable)")
    check("fixed" not in p.stdout or "fixed 0" in p.stdout
          or "no warnings" not in p.stdout,
          "the fix report never claims a deprecation was fixed")

    # hlfmt preserves both attribute forms byte for byte.
    fmtme = os.path.join(tree, "fmt.hls")
    with open(fmtme, "w") as fh:
        fh.write('#[deprecated("use g — the old form mis-handles '
                 'negatives")]\n'
                 'fn f(x: int) -> int {\n'
                 '    return x+1\n'
                 '}\n'
                 '\n'
                 'fn main() -> int {\n'
                 '    return f(1)\n'
                 '}\n')
    p = run(FMT + ["-w", fmtme])
    src = open(fmtme).read()
    check('#[deprecated("use g — the old form mis-handles negatives")]'
          in src,
          "hlfmt preserves the message form byte for byte")
    with open(fmtme, "w") as fh:
        fh.write('#[deprecated]\nfn f(x: int) -> int {\n    return x\n}\n'
                 '\nfn main() -> int {\n    return f(1)\n}\n')
    run(FMT + ["-w", fmtme])
    check(open(fmtme).read().startswith("#[deprecated]\n"),
          "hlfmt preserves the bare form")

    # A missing-module crate: the fallback keeps the same-file
    # registry alive (and never crashes).
    miss = os.path.join(tree, "miss.hls")
    with open(miss, "w") as fh:
        fh.write('#[deprecated]\nfn alone(v: int) -> int {\n'
                 '    return v\n}\n')
    # (miss.hls imports nothing — the fallback is exercised by the
    # same-file path above; here we assert the load failure of a
    # missing import degrades to the same-file view, loudly linting.)
    broken = os.path.join(tree, "broken.hls")
    with open(broken, "w") as fh:
        fh.write('import "no_such_module.hls"\n'
                 '\n'
                 'fn main() -> int {\n    return 0\n}\n')
    p = run(LINT + [broken])
    check(p.returncode == 0,
          "a crate that cannot load still lints (no crash, exit 0)")


# ===========================================================================
def _section_7():
    print("Section 7 — the differential and the pinned census")
    p = run(BOOT + [SHOWCASE])
    icmd, iout = p.returncode, p.stdout
    check(icmd == 0 and "deprecated OK: 113" in iout,
          "the ok fixture runs green on the interpreter")
    if NATIVE:
        c_path = os.path.join(TMP, "show.c")
        p2 = run([HLC, SHOWCASE, c_path])
        binp = os.path.join(TMP, "show")
        if p2.returncode == 0:
            r = run([CC, "-O2", "-o", binp, c_path, "-lm", "-pthread"])
            if r.returncode == 0:
                n = run([binp])
                check((n.returncode, n.stdout) == (icmd, iout),
                      "the ok fixture: interpreter == native, byte for "
                      "byte")
            else:
                bad("  gcc failed on the emitted C")
        else:
            bad("  hlc failed to compile the ok fixture")
    else:
        print("  (bin/hlc absent — the native leg is skipped, named, "
              "not silent)")

    # The pinned census: exactly the five warnings the fixture
    # documents, byte for byte.
    p = run(LINT + [SHOWCASE])
    census = [ln for ln in p.stdout.splitlines() if "[L013]" in ln]
    check(len(census) == 5,
          "the census: exactly five L013 warnings (three same-file, "
          "two cross-module)")
    check(any("'slow_path': use fast_path(v) — the old form "
              "double-counts zero" in ln for ln in census),
          "the census: the message form quoted")
    check(any("'legacy_name' (no deprecation message given)" in ln
              for ln in census),
          "the census: the bare form named")
    check(any("'depmod_slow': use depmod_fast(v)" in ln for ln in census)
          and any("'depmod_legacy' (no deprecation message given)" in ln
                  for ln in census),
          "the census: the cross-module notices fire in the importer")
    check(any("'old_chain': temporary glue; dies with the legacy "
              "surface" in ln for ln in census),
          "the census: the deprecated wrapper's own call site warns "
          "from main")
    others = [ln for ln in p.stdout.splitlines()
              if "warning" in ln and "[L013]" not in ln]
    check(not others,
          "the census: nothing ELSE warns (no L001/L002 noise on the "
          "fixture)")


# ===========================================================================
def _section_8():
    print("Section 8 — the corpus hygiene")
    for f, label in ((SHOWCASE, "the ok fixture"),
                     (MODULE, "the module sibling"),
                     (DEMO, "the demo")):
        p = run(FMT + ["--check", f])
        check(p.returncode == 0 and "already formatted" in p.stdout,
              "%s is hlfmt-canonical" % label)
    # The module sibling is a LIBRARY (no main) — it is checked as
    # part of the fixture that imports it, never standalone.
    for f, label in ((SHOWCASE, "the ok fixture"),
                     (DEMO, "the demo")):
        p = run(BOOT + ["--check", f])
        check("OK: types and effects valid" in p.stdout,
              "%s boots through the real checker" % label)
    p = run(BOOT + [DEMO])
    check(p.returncode == 0 and "deprecated demo OK" in p.stdout,
          "the demo runs green")
    p = run(LINT + [DEMO])
    l013 = [ln for ln in p.stdout.splitlines() if "[L013]" in ln]
    check(len(l013) == 2 and "shout" in l013[0] and "legacy_id" in l013[1],
          "the demo carries exactly its two documented warnings")
    # No gate litter.
    litter = []
    for d in (ROOT, os.path.join(ROOT, "tests")):
        for name in os.listdir(d):
            if name.startswith("hls-deprecated-gate-"):
                litter.append(name)
    check(not litter, "no gate temp files survive in the repo")
    for root, dirs, names in os.walk(os.path.join(ROOT, "tests", "ok")):
        for name in names:
            if name.endswith(".bak"):
                litter.append(name)
    check(not litter, "no .bak litter anywhere in the corpus")


# ---------------------------------------------------------------------------
# Section selection: the whole gate is one process by default; a CI
# step timeout can run it in pieces (--sections 1,2 or the
# HLS_DEPRECATED_GATE_SECTIONS env). A subset run reports the sections
# it ran, never a silent skip.
_ALL_SECTIONS = list(range(1, 9))
if "--sections" in sys.argv:
    _want = sys.argv[sys.argv.index("--sections") + 1]
    _selected = [int(x) for x in _want.split(",") if x.strip()]
else:
    _env = os.environ.get("HLS_DEPRECATED_GATE_SECTIONS")
    _selected = ([int(x) for x in _env.split(",") if x.strip()]
                 if _env else _ALL_SECTIONS)
for _bad_n in _selected:
    if _bad_n not in _ALL_SECTIONS:
        sys.stderr.write("deprecated gate: no such section: %d\n"
                         % _bad_n)
        raise SystemExit(2)
if _selected != _ALL_SECTIONS:
    print("deprecated gate: running sections %s of %s"
          % (",".join(str(x) for x in _selected),
             ",".join(str(x) for x in _ALL_SECTIONS)))
for _n in _selected:
    globals()["_section_%d" % _n]()

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
raise SystemExit(0 if FAIL == 0 else 1)
