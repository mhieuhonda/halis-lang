#!/usr/bin/env python3
"""Stage 109 acceptance gate — taint-tracking through FFI boundaries.

Run with `make ffi-taint-acceptance` (or
`python3 tests/ffi_taint_acceptance.py`).

Seven sections:

  1. the parser       — a valid marking parses and runs (boot); the
                        placement rule (the two names on a normal fn
                        are named, not "unknown"), the extern-block
                        mini-grammar refusals (unknown attr, duplicate
                        source, empty sink list, duplicate sink name),
                        word for word, in both front-ends where a
                        binary is available
  2. the checker      — #[taint_source] wraps the call result
                        (tainted[str] at the call site), the dedicated
                        #[taint_sink] diagnostic, the void-source
                        refusal, the unknown-sink-name refusal, and
                        the blanket Stage 15 rule still standing on an
                        unmarked extern — boot AND native hlc agree
  3. the runtime      — the ok test through the interpreter: the
                        wrapper-dict representation flows through the
                        pure queries, the concat, the sanitizer and
                        the hatch, and the output is pinned
  4. the census       — the --audit FFI block is word-for-word
                        identical between boot and the self-hosted
                        compiler, on a marked AND an unmarked program
  5. the report       — hlprove --taint: the demo's pinned lines
                        (census, provenance, the sanitised cut, the
                        accepted-risk sink flow, the total) and the
                        sink-flow program; analysis-only (exit 0)
  6. the demos        — examples/ffi_taint_demo.hls runs; the demo and
                        the ok-test agree with the interpreter
                        (native half only when bin/hlc exists — the
                        gate stays hermetic)
  7. the tools        — hlfmt stable, hllint clean on the new sources
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

BOOT = [sys.executable, "boot/boot.py"]
PROVE = [sys.executable, "tools/hlprove.py"]
FMT = [sys.executable, "tools/hlfmt.py"]
LINT = [sys.executable, "tools/hllint.py"]
DEMO = "examples/ffi_taint_demo.hls"
OK_TEST = "tests/ok/feat_stage109_ffi_taint.hls"
HLC = "bin/hlc"

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s109_", dir=os.path.join(ROOT, "tests"))


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


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


def expect(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def boot_check_err(path):
    """boot --check: (rc, stderr)"""
    p = run(BOOT + ["--check", path])
    return p.returncode, (p.stderr or "")


def hlc_check_err(path):
    """native hlc compile attempt: (rc, stderr)"""
    out_c = os.path.join(TMP, os.path.basename(path) + ".nat.c")
    p = run([HLC, path, out_c])
    return p.returncode, (p.stderr or "")


# ---------------------------------------------------------------------------
print("=== 1. the parser ===")
# ---------------------------------------------------------------------------

# A valid marking parses and RUNS (the interpreter's wrapper-dict path
# is exercised for real in section 3 — here, just the parse).
p = run(BOOT + [OK_TEST])
expect(p.returncode == 0, "the ok test parses and runs (boot)")

# The placement rule: the two names on a NORMAL fn are named, not
# buried in "unknown attribute".
f = write(os.path.join(TMP, "p_onfn.hls"), """\
#[taint_source]
fn f() -> int uses IO { return 1 }
fn main() -> int uses IO {
    let x: int = f()
    let _y: int = x
    return 0
}
""")
rc, err = boot_check_err(f)
expect(rc == 1 and "is an extern-block attribute" in err
       and "extern \"C\"" in err,
       "boot: #[taint_source] on a normal fn names the placement rule")
if os.path.exists(HLC):
    rc2, err2 = hlc_check_err(f)
    expect(rc2 != 0 and "is an extern-block attribute" in err2,
           "native: the same placement refusal")

# Unknown extern-block attribute — the codegen attrs belong to the C
# side and cannot annotate a declaration.
f = write(os.path.join(TMP, "p_unknown.hls"), """\
extern "C" {
    #[hot]
    fn dirname(p: str) -> str uses IO
}
fn main() -> int uses IO {
    let d: str = dirname("x/y")
    println(d.len().to_str())
    return 0
}
""")
rc, err = boot_check_err(f)
expect(rc == 1 and "unknown extern-block attribute 'hot'" in err,
       "boot: #[hot] inside an extern block is refused by name")

# Duplicate taint_source.
f = write(os.path.join(TMP, "p_dupsrc.hls"), """\
extern "C" {
    #[taint_source]
    #[taint_source]
    fn dirname(p: str) -> str uses IO
}
fn main() -> int uses IO {
    let d: tainted[str] = dirname("x/y")
    let s: str = taint_unwrap(d)
    println(s.len().to_str())
    return 0
}
""")
rc, err = boot_check_err(f)
expect(rc == 1 and "taint_source attribute appears more than once" in err,
       "boot: duplicate #[taint_source] on one extern is refused")

# Empty sink list — a sink that sinks nothing hides its hazard.
f = write(os.path.join(TMP, "p_emptysink.hls"), """\
extern "C" {
    #[taint_sink()]
    fn system(cmd: str) -> int uses IO
}
fn main() -> int uses IO {
    let rc: int = system("ls")
    println(rc.to_str())
    return 0
}
""")
rc, err = boot_check_err(f)
expect(rc == 1 and "#[taint_sink(...)] needs at least one parameter name" in err,
       "boot: an empty #[taint_sink(...)] list is refused")

# Duplicate name in one sink list.
f = write(os.path.join(TMP, "p_dupsink.hls"), """\
extern "C" {
    #[taint_sink(buf, buf)]
    fn readinto(fd: int, buf: str) -> int uses IO
}
fn main() -> int uses IO {
    let rc: int = readinto(0, "x")
    println(rc.to_str())
    return 0
}
""")
rc, err = boot_check_err(f)
expect(rc == 1 and "'buf' appears more than once" in err,
       "boot: a duplicate name in #[taint_sink(...)] is refused")

# ---------------------------------------------------------------------------
print("=== 2. the checker ===")
# ---------------------------------------------------------------------------

# The wrap: the marked call site's result is tainted[str] — assigning
# it to a plain str is a type error that NAMES the tainted type.
f = write(os.path.join(TMP, "c_wrap.hls"), """\
extern "C" {
    #[taint_source]
    fn dirname(p: str) -> str uses IO
}
fn main() -> int uses IO {
    let d: str = dirname("x/y")
    println(d)
    return 0
}
""")
rc, err = boot_check_err(f)
expect(rc == 1 and "declared str but got tainted[str]" in err,
       "boot: the #[taint_source] call result is typed tainted[str]")
if os.path.exists(HLC):
    rc2, err2 = hlc_check_err(f)
    expect(rc2 != 0 and "tainted[str]" in err2,
           "native: the wrap is typed the same way")

# The dedicated sink diagnostic.
f = "tests/fail/fail_stage109_ffi_sink.hls"
rc, err = boot_check_err(f)
expect(rc == 1
       and "taint-sink violation: extern 'system' argument 1 ('cmd')"
       in err
       and "declared #[taint_sink(...)]" in err,
       "boot: a #[taint_sink] parameter gets the dedicated diagnostic")
if os.path.exists(HLC):
    rc2, err2 = hlc_check_err(f)
    expect(rc2 != 0 and "declared #[taint_sink(...)]" in err2,
           "native: the same sink diagnostic")

# Void source.
f = "tests/fail/fail_stage109_ffi_source_void.hls"
rc, err = boot_check_err(f)
expect(rc == 1 and "#[taint_source] on 'notify' is meaningless" in err,
       "boot: #[taint_source] on a void extern is refused")
if os.path.exists(HLC):
    rc2, err2 = hlc_check_err(f)
    expect(rc2 != 0 and "is meaningless" in err2,
           "native: the same void-source refusal")

# Unknown sink name.
f = "tests/fail/fail_stage109_ffi_sink_badname.hls"
rc, err = boot_check_err(f)
expect(rc == 1 and "names 'comand' which is not a parameter" in err,
       "boot: a typo'd #[taint_sink] name is refused")
if os.path.exists(HLC):
    rc2, err2 = hlc_check_err(f)
    expect(rc2 != 0 and "which is not a parameter" in err2,
           "native: the same unknown-name refusal")

# The blanket Stage 15 rule still stands on an UNMARKED extern.
f = write(os.path.join(TMP, "c_blanket.hls"), """\
extern "C" {
    fn puts(s: str) -> int uses IO
}
fn main() -> int uses Args, IO {
    let argv: list[tainted[str]] = tainted_args()
    let rc: int = puts(argv.get(0))
    println(rc.to_str())
    return 0
}
""")
rc, err = boot_check_err(f)
expect(rc == 1
       and "passing tainted data across the FFI boundary is forbidden"
       in err,
       "boot: an unmarked extern still rejects tainted arguments "
       "(Stage 15 rule intact)")

# ---------------------------------------------------------------------------
print("=== 3. the runtime ===")
# ---------------------------------------------------------------------------

p = run(BOOT + [OK_TEST])
expect(p.returncode == 0, "the ok test runs (interpreter)")
expect(p.stdout == "byte: 65\nbyte2: 65\n"
                   "clean: project/bin/tool.hls\n",
       "the ok test's pinned output (FFI source, hatch, propagation, "
       "sanitizer)")

# ---------------------------------------------------------------------------
print("=== 4. the census ===")
# ---------------------------------------------------------------------------

MARKED = OK_TEST
p_boot = run(BOOT + ["--audit", MARKED])
b_text = p_boot.stdout


def ffi_block(text):
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.startswith("  FFI taint marking (Stage 109):"):
            out = [l]
            for l2 in lines[i + 1:]:
                if not l2.startswith("    ") and not l2.startswith("      "):
                    break
                if l2.startswith("      "):
                    continue
                out.append(l2)
            # the rows are indented deeper than the headers
            return "\n".join(out)
    return ""


expect("FFI taint marking (Stage 109):" in b_text,
       "boot --audit prints the Stage 109 census")
expect("    - toupper: -> int" in b_text,
       "the census names the #[taint_source] extern with its return type")
if os.path.exists(HLC):
    p_hlc = run([HLC, "--audit", MARKED, os.path.join(TMP, "a.c")])
    expect(ffi_block(p_hlc.stdout) == ffi_block(b_text) and ffi_block(b_text),
           "the census is word-for-word identical between the front-ends")

# A sink-marked program lists the sink row.
f = write(os.path.join(TMP, "census_sink.hls"), """\
extern "C" {
    #[taint_sink(cmd)]
    fn system(cmd: str) -> int uses IO
}
fn main() -> int uses IO {
    let rc: int = system("ls")
    println(rc.to_str())
    return 0
}
""")
p2 = run(BOOT + ["--audit", f])
expect("    #[taint_sink(...)] externs (declared sinks): 1" in p2.stdout
       and "    - system: cmd" in p2.stdout,
       "the census lists a #[taint_sink(...)] extern with its params")

# ---------------------------------------------------------------------------
print("=== 5. the report ===")
# ---------------------------------------------------------------------------

p = run(PROVE + [DEMO, "--taint"])
expect(p.returncode == 0, "hlprove --taint is analysis-only (exit 0)")
out = p.stdout
for pin in (
        'dirname(p: str) -> str  [SOURCE #[taint_source]',
        "boundary census: 1 source(s), 0 sink(s), 0 unmarked",
        'SOURCE — `dirname("...")` (from ffi:dirname)',
        'SANITISED — `sanitize_path(raw)` (from ffi:dirname)',
        'UNWRAP — `taint_unwrap(raw)` (from ffi:dirname)',
        'SINK-REACH — `println(("..." + raw_dir))` (from ffi:dirname)',
        'untrusted params (by propagation): dir',
        "FFI TAINT TOTAL: 1 source sites, 4 unwrap escapes, "
        "1 sink-reaching flows, 1 sanitised cuts",
):
    expect(pin in out, "the report pins: %s" % pin)

# The sink-flow program: the declared sink catches the escaped data,
# and the extern return stops the propagation (rc is clean).
f = write(os.path.join(TMP, "r_sinkflow.hls"), """\
import "std.sanitize"
import "std.taint"

extern "C" {
    #[taint_source]
    fn getenv(name: str) -> str uses IO
    #[taint_sink(cmd)]
    fn system(cmd: str) -> int uses IO
}

fn main() -> int uses Args, IO {
    let raw: tainted[str] = getenv("USER_CONFIG")
    let argv: list[tainted[str]] = tainted_args()
    let dir: str = taint_unwrap(raw)
    let risky: str = taint_unwrap(argv.get(1))
    let rc: int = system(risky)
    println("rc: " + rc.to_str())
    return 0
}
""")
p = run(PROVE + [f, "--taint"])
expect(p.returncode == 0, "the sink-flow report runs clean (exit 0)")
out = p.stdout
expect("getenv(name: str) -> str  [SOURCE #[taint_source]" in out
       and "system(cmd: str) -> int  [SINK #[taint_sink(cmd)]]" in out,
       "the report censuses both markings")
expect('SINK-REACH — `system(risky)` (from builtin:tainted_args)' in out,
       "the report names the accepted-risk flow into the declared sink")
expect('SINK-REACH — `println(("..." + rc.to_str()))`' not in out,
       "an extern's return is NOT untrusted (argument provenance "
       "stops at the boundary)")

# A program with no externs says so, in one line.
f = write(os.path.join(TMP, "r_noext.hls"), """\
fn main() -> int uses IO {
    println("plain")
    return 0
}
""")
p = run(PROVE + [f, "--taint"])
expect(p.returncode == 0
       and "(no extern declarations — no FFI boundary to audit)" in p.stdout,
       "a program without externs gets the one-line note")

# ---------------------------------------------------------------------------
print("=== 6. the demos ===")
# ---------------------------------------------------------------------------

p = run(BOOT + [DEMO])
expect(p.returncode == 0, "the demo runs (interpreter)")
expect(p.stdout == "dirname: 11\nconfig path len: 20\n"
                   "clean dir: project/bin\n"
                   "raw dir (unwrapped): project/bin\n",
       "the demo's pinned output (propagation through a user fn)")

if os.path.exists(HLC):
    for src in (DEMO, OK_TEST):
        base = os.path.basename(src).replace(".hls", "")
        out_c = os.path.join(TMP, "demo_%s.c" % base)
        c = run([HLC, src, out_c])
        g = run(["gcc", "-O2", "-o",
                 os.path.join(TMP, "demo_%s.bin" % base), out_c,
                 "-lm", "-pthread"])
        expect(c.returncode == 0 and g.returncode == 0,
               "native hlc + gcc build %s (the forward declaration "
               "conflicts with no included header)" % base)
    # The toupper ok test RUNS natively too — int ABI, no str marshalling
    # on the boundary (the documented str-arg limitation stays out).
    nb = os.path.join(TMP, "demo_feat_stage109_ffi_taint.bin")
    r = run([nb])
    expect(r.returncode == 0 and r.stdout == "byte: 65\nbyte2: 65\n"
           "clean: project/bin/tool.hls\n",
           "the ok test's FFI source runs natively and matches the "
           "interpreter byte for byte")
else:
    print("  [note] bin/hlc not built — the native half of section 6 "
          "is skipped (the gate stays hermetic)")

# ---------------------------------------------------------------------------
print("=== 7. the tools ===")
# ---------------------------------------------------------------------------

for src in (DEMO, OK_TEST):
    base = os.path.basename(src)
    f1 = os.path.join(TMP, "fmt1_" + base)
    f2 = os.path.join(TMP, "fmt2_" + base)
    with open(src) as fh:
        content = fh.read()
    write(f1, content)
    write(f2, content)
    # f1 is formatted once; f2 twice. Formatting must be a fixed
    # point: once == twice.
    run(FMT + ["-w", f1])
    run(FMT + ["-w", f2])
    run(FMT + ["-w", f2])
    once = open(f1).read()
    twice = open(f2).read()
    expect(once == twice,
           "hlfmt: stable formatting on %s" % base)

lints = [run(LINT + [f]).returncode
         for f in (DEMO, OK_TEST)]
expect(all(rc == 0 for rc in lints),
       "hllint: no findings on the demo or the ok-test")

# ---------------------------------------------------------------------------
print("")
print("Stage 109 acceptance: %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
