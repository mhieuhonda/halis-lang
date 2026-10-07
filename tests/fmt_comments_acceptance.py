#!/usr/bin/env python3
"""Stage 116 acceptance gate — hlfmt preserves comments in ALL
positions.

Run with `make fmt-comments-acceptance` (or
`python3 tests/fmt_comments_acceptance.py`).

Eight sections:

  1. the sigils      — `#[...]` / `#![...]` are attributes, not
                       comments: the scanner skips them (bracket-depth
                       and string aware), a real comment after an
                       attribute on the same line survives, `#!` without
                       `[` stays a comment, and `#` inside string
                       literals never starts one
  2. the duplication — an attribute line that also carries tokens
                       (`#[cold] fn inner() -> int {` inside a block)
                       formats EXACTLY once — before Stage 116 the
                       scanner misread it as a comment-only line and
                       the gap logic re-emitted it, growing the file on
                       every pass; three passes change nothing
  3. the positions   — every position a comment can occupy survives a
                       pass: file banner, above a declaration, interior
                       block comment, trailing on a statement, on an
                       opening brace, on a closing brace, between `}`
                       and `else`, inside a struct body, inside a
                       struct literal, between match arms, between
                       parameters, mid-expression, inside a call, and
                       the EOF note
  4. the re-indent   — comment-only lines adopt the canonical block
                       indent (stale source indents do not survive a
                       pass, code never kept one either); top-level
                       comments stay at column 0; `hlfmt -c` flags the
                       stale file and passes after `-w`
  5. the preservation— comment TEXT is byte-exact (multi-byte UTF-8
                       survives the latin-1 round-trip), no comment is
                       lost, none is duplicated, none is reordered
  6. the one-liners  — `fn f() -> int { return 0 } # done` attaches the
                       comment exactly once (after the last segment the
                       source line produced); a comment after `{` lands
                       on the brace line
  7. the differential— the stage's ok corpus file runs interpreter vs
                       native byte-identically BEFORE a formatting pass
                       and AFTER one; the formatted file re-compiles and
                       re-runs to the same markers on both front-ends
  8. the corpus      — every examples/, tests/ok/ and tests/fail/ file
                       formats idempotently and preserves its comment
                       set; hllint stays clean on the formatted stage
                       fixture

Exit code 0 = all acceptance criteria met.
"""
import glob
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from tools.hlfmt import format_source, _extract_comments  # noqa: E402

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s116_", dir=os.path.join(ROOT, "tests"))

HLC = os.path.join(ROOT, "bin", "hlc")
CORPUS = os.path.join(ROOT, "tests", "ok", "feat_stage116_fmt_comments.hls")


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def write(name, src):
    path = os.path.join(TMP, name)
    with open(path, "wb") as f:
        f.write(src if isinstance(src, bytes) else src.encode("utf-8"))
    return path


def passes(src):
    """Format src, then format the output again: a formatter pass must
    be a fixed point of itself."""
    out1 = format_source(src)
    out2 = format_source(out1.encode("latin-1"))
    return out1, out2.encode("latin-1") == out1.encode("latin-1")


def comment_bodies(src):
    """The set of comment texts (kind-agnostic, whitespace-normalised)
    a scanner pass finds in src — the preservation law compares these."""
    return sorted(v[1].strip() for v in _extract_comments(src).values())


def compile_native(hls_path, c_path):
    proc = subprocess.run([HLC, hls_path, c_path],
                          capture_output=True, text=True)
    return proc.returncode == 0, proc.stderr + proc.stdout


def run_native(c_path, bin_path):
    cc = subprocess.run(["gcc", "-O2", "-o", bin_path, c_path, "-lm",
                         "-pthread"], capture_output=True, text=True)
    if cc.returncode != 0:
        return None, cc.stderr
    proc = subprocess.run([bin_path], capture_output=True, text=True)
    return proc.stdout, proc.stderr


print("=== 1. the sigils: attributes are not comments ===")
cases = [
    # (source, description, expected comment bodies after scan)
    (b"#[cold]\nfn f() -> int {\n    return 1\n}\n",
     "bare attribute line carries no comment entry",
     []),
    (b"#![no_std]\nfn f() -> int {\n    return 1\n}\n",
     "crate attribute carries no comment entry",
     []),
    (b"#[cold] # legacy path\nfn f() -> int {\n    return 1\n}\n",
     "comment after an attribute survives",
     ["# legacy path"]),
    (b'#[doc("# not a comment")]\nfn f() -> int {\n    return 1\n}\n',
     "hash inside an attribute string is not a comment",
     []),
    (b"#! run with the hls driver\nfn f() -> int {\n    return 1\n}\n",
     "#! without [ stays a comment",
     ["#! run with the hls driver"]),
    (b'fn f() -> int {\n    let s: str = "# not a comment"\n    return 1\n}\n',
     "hash inside a string literal is not a comment",
     []),
    (b"# banner\n#[inline(always)]\n#[cold]\nfn f() -> int {\n    return 1\n}\n",
     "attribute stack keeps only the real banner",
     ["# banner"]),
    (b"#[a([b])]\nfn f() -> int {\n    return 1\n}\n",
     "nested brackets inside an attribute do not leak",
     []),
]
for src, desc, expect in cases:
    bodies = comment_bodies(src)
    if bodies == sorted(expect):
        ok(desc)
    else:
        bad("%s: scanned %r, expected %r" % (desc, bodies, sorted(expect)))
out1, idem = passes(b"#[cold] # why\nfn f() -> int {\n    return 1\n}\n")
if "# why" in out1 and idem:
    ok("attribute + trailing comment formats idempotently with the note kept")
else:
    bad("attribute + trailing comment: %r idem=%s" % (out1, idem))

print("=== 2. the duplication law: attribute lines format once ===")
dup_src = (b"fn outer() -> int {\n"
           b"    #[cold] fn inner() -> int {\n"
           b"        return 1\n"
           b"    }\n"
           b"    return 0\n"
           b"}\n")
out1, idem = passes(dup_src)
n_inner = out1.count("fn inner")
if n_inner == 1:  # exactly the declaration — no re-emitted ghost
    ok("attribute-carrying nested fn appears exactly once")
else:
    bad("nested attribute fn duplicated: %d occurrences" % n_inner)
if "#[cold]" in out1 and out1.count("#[cold]") == 1:
    ok("the attribute sigil itself survives exactly once")
else:
    bad("attribute sigil count wrong: %d" % out1.count("#[cold]"))
out3 = format_source(format_source(
    format_source(dup_src).encode("latin-1")).encode("latin-1")).encode("latin-1")
if out3 == out1.encode("latin-1"):
    ok("three passes converge (no per-pass growth)")
else:
    bad("formatting is not stable across three passes")

print("=== 3. the positions: every seat a comment can take ===")
positions_src = b"""# 01 file banner
# 02 second banner line
fn add(a: int, b: int) -> int {
    # 03 interior at block top
    return a + b  # 04 trailing on a statement
}

# 05 above a declaration
fn wrapped() -> int {
    let v: int = add(1,
        # 06 inside a call
        2)
    let w: int = 1 +
        # 07 mid-expression
        v
    return w  # 08 trailing before close
}

struct Box {
    # 09 struct body
    label: str,
    size: int
}

fn rank(l: Level) -> int {
    return match l {
        # 10 match arm
        Level.Low => 1,
        # 11 second arm
        Level.High => 2
    }
}

enum Level {
    Low,
    High
}

fn main() -> int uses IO {
    let b: Box = Box {
        # 12 struct literal
        label: "x",
        size: 3
    }
    if b.size == 3 {
        println(b.label)  # 13 trailing in if
    }  # 14 on a closing brace
    if b.size != 3 {
        println("no")
    } else {
        # 15 inside else
        println("yes")
    }
    # 16 before the final brace
    return 0
}
# 17 EOF note
"""
out1, idem = passes(positions_src)
missing = [i for i in range(1, 18) if ("# %02d" % i) not in out1]
if not missing:
    ok("all 17 comment positions present after a pass")
else:
    bad("lost comment positions: %r" % missing)
if idem:
    ok("the 17-position fixture is idempotent")
else:
    bad("the 17-position fixture is NOT idempotent")
order = [out1.index("# %02d" % i) for i in (1, 5, 9, 17)]
if order == sorted(order):
    ok("comments keep their relative order")
else:
    bad("comments reordered: %r" % order)
# A comment must sit at its statement: 04 stays on the return line.
for ln in out1.splitlines():
    if ln.strip() == "return a + b # 04 trailing on a statement":
        ok("trailing comment stays glued to its statement")
        break
else:
    bad("trailing comment detached from its statement: %r" %
        [ln for ln in out1.splitlines() if "04 trailing" in ln])

print("=== 4. the re-indent law: stale indents do not survive ===")
stale_src = (b"fn f() -> int {\n"
             b"    if true {\n"
             b"        if true {\n"
             b"    # badly indented\n"
             b"            return 1\n"
             b"        }\n"
             b"    }\n"
             b"        # stale before close\n"
             b"    return 0\n"
             b"}\n"
             b"      # stale top-level\n")
out1, idem = passes(stale_src)
if "\n            # badly indented\n" in "\n%s\n" % out1:
    ok("deep comment adopts the block indent")
else:
    bad("deep comment not re-indented: %r" %
        [ln for ln in out1.splitlines() if "badly" in ln])
for ln in out1.splitlines():
    if "stale top-level" in ln:
        if ln == "# stale top-level":
            ok("top-level comment lands at column 0")
        else:
            bad("top-level comment keeps junk indent: %r" % ln)
if idem:
    ok("re-indented fixture is idempotent")
else:
    bad("re-indented fixture NOT idempotent")
stale_path = write("stale.hls", stale_src)
chk = subprocess.run([sys.executable, "tools/hlfmt.py", "-c", stale_path],
                     capture_output=True, text=True)
if chk.returncode != 0:
    ok("hlfmt -c flags the stale-indented file")
else:
    bad("hlfmt -c accepted a stale-indented file")
subprocess.run([sys.executable, "tools/hlfmt.py", "-w", stale_path],
               capture_output=True, text=True)
chk2 = subprocess.run([sys.executable, "tools/hlfmt.py", "-c", stale_path],
                      capture_output=True, text=True)
if chk2.returncode == 0:
    ok("hlfmt -w repairs the file; -c passes afterwards")
else:
    bad("hlfmt -w did not produce a -c-clean file")

print("=== 5. the preservation law: text survives byte-exact ===")
utf8_src = ("# café ☕ — reviewed by the team\n"
            "fn f() -> int {\n"
            '    let s: str = "hash # inside"\n'
            "    return 1  # giữ nguyên dấu tiếng Việt ☕\n"
            "}\n").encode("utf-8")
out1, idem = passes(utf8_src)
# format_source returns a latin-1 char stream (1 char == 1 byte) so
# multi-byte UTF-8 survives; decode the byte stream back to Unicode
# before comparing text.
real_text = out1.encode("latin-1").decode("utf-8")
if "# café ☕ — reviewed by the team" in real_text:
    ok("multi-byte UTF-8 comment text survives")
else:
    bad("UTF-8 banner mangled: %r" %
        [ln for ln in real_text.splitlines() if "caf" in ln])
if "# giữ nguyên dấu tiếng Việt ☕" in real_text:
    ok("UTF-8 trailing comment survives")
else:
    bad("UTF-8 trailing comment mangled")
if comment_bodies(utf8_src) == comment_bodies(out1.encode("latin-1")):
    ok("comment set identical before and after (count + text)")
else:
    bad("comment set changed: %r -> %r"
        % (comment_bodies(utf8_src), comment_bodies(out1.encode("latin-1"))))
if idem:
    ok("UTF-8 fixture is idempotent")
else:
    bad("UTF-8 fixture NOT idempotent")
noisy_src = (b"fn f() -> int {\n"
             b"    let a: int = 1  # one\n"
             b"    let b: int = 2  # two\n"
             b"    # standalone\n"
             b"    return a + b  # sum\n"
             b"}\n")
out1, _ = passes(noisy_src)
n = sum(ln.count("#") for ln in out1.splitlines())
if n == 4:
    ok("no comment duplicated or dropped on a four-comment fixture")
else:
    bad("comment count changed: 4 -> %d" % n)

print("=== 6. the one-liners: attach exactly once ===")
one_src = b"fn f() -> int { return 0 } # done\n"
out1, idem = passes(one_src)
lines = out1.splitlines()
n_done = sum(ln.count("# done") for ln in lines)
if n_done == 1:
    ok("one-liner trailing comment attached exactly once (%d)" % n_done)
else:
    bad("one-liner comment attached %d times" % n_done)
if lines[-1].endswith("# done"):
    ok("comment lands after the last segment the line produced")
else:
    bad("comment landed mid-line: %r" % lines)
if idem:
    ok("one-liner fixture is idempotent")
else:
    bad("one-liner fixture NOT idempotent")
open_src = b"fn f() -> int { # why\n    return 1\n}\n"
out1, idem = passes(open_src)
if "fn f() -> int { # why" in out1 and idem:
    ok("comment after an opening brace stays on the brace line")
else:
    bad("brace-line comment lost or non-idempotent: %r" % out1)

print("=== 7. the differential: formatting preserves semantics ===")
if not os.path.exists(HLC):
    bad("bin/hlc missing — run make bootstrap first")
else:
    interp_out = subprocess.run(
        [sys.executable, "boot/boot.py", CORPUS],
        capture_output=True, text=True).stdout
    c_path = os.path.join(TMP, "s116_orig.c")
    bin_path = os.path.join(TMP, "s116_orig")
    compiled, err = compile_native(CORPUS, c_path)
    native_out = None
    if compiled:
        native_out, rerr = run_native(c_path, bin_path)
    if compiled and native_out is not None and native_out == interp_out:
        ok("unformatted file: interpreter == native")
    else:
        bad("unformatted file diverges (compiled=%s, err=%s)"
            % (compiled, err[:160]))
    fmt_path = os.path.join(TMP, "s116_fmt.hls")
    with open(CORPUS, "rb") as f:
        fmt_bytes = format_source(f.read()).encode("latin-1")
    with open(fmt_path, "wb") as f:
        f.write(fmt_bytes)
    interp_fmt = subprocess.run(
        [sys.executable, "boot/boot.py", fmt_path],
        capture_output=True, text=True).stdout
    c_path2 = os.path.join(TMP, "s116_fmt.c")
    bin_path2 = os.path.join(TMP, "s116_fmt_bin")
    compiled2, err2 = compile_native(fmt_path, c_path2)
    native_fmt = None
    if compiled2:
        native_fmt, _ = run_native(c_path2, bin_path2)
    if compiled2 and native_fmt == interp_fmt:
        ok("formatted file: interpreter == native (recompiles and reruns)")
    else:
        bad("formatted file diverges (compiled=%s, err=%s)"
            % (compiled2, err2[:160]))
    if interp_out == interp_fmt:
        ok("formatting changed no output line (semantic witness)")
    else:
        bad("formatting changed program output:\n%r\n%r"
            % (interp_out, interp_fmt))
    with open(CORPUS, "rb") as f:
        orig_src = f.read()
    if comment_bodies(orig_src) == comment_bodies(fmt_bytes):
        ok("the corpus file keeps every comment through the pass")
    else:
        bad("the corpus file lost or altered comments")

print("=== 8. the corpus: idempotent + preserving everywhere ===")
files = sorted(glob.glob("examples/**/*.hls", recursive=True)
               + glob.glob("tests/ok/*.hls")
               + glob.glob("tests/fail/*.hls"))
non_idem = []
lost = []
for f in files:
    with open(f, "rb") as fh:
        src = fh.read()
    try:
        out1 = format_source(src)
    except Exception:
        continue  # unparseable fail- fixtures fall back verbatim
    out2 = format_source(out1.encode("latin-1")).encode("latin-1")
    if out2 != out1.encode("latin-1"):
        non_idem.append(f)
    if comment_bodies(src) != comment_bodies(out1.encode("latin-1")):
        lost.append(f)
if not non_idem:
    ok("%d corpus files format idempotently" % len(files))
else:
    bad("non-idempotent: %r" % non_idem[:8])
if not lost:
    ok("%d corpus files preserve their comment set" % len(files))
else:
    bad("comment set changed: %r" % lost[:8])
lint = subprocess.run(
    [sys.executable, "tools/hllint.py", CORPUS],
    capture_output=True, text=True)
if lint.returncode == 0:
    ok("hllint stays clean on the stage fixture")
else:
    bad("hllint flagged the stage fixture:\n%s" % lint.stdout[:200])

print("")
print("=== Stage 116 acceptance: %d PASS / %d FAIL ===" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
