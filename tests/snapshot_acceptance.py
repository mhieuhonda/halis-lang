#!/usr/bin/env python3
"""Stage 119 acceptance gate — hltest snapshot testing (`assert_snapshot`).

Run with `make snapshot-acceptance` (or
`python3 tests/snapshot_acceptance.py`).

Eight sections:

  1. the assertion  — std.test's assert_snapshot emits the exact wire
                      protocol (marker line, length-prefixed value,
                      frame separator); several records in one test;
                      interleaved println diagnostics stay OUTSIDE the
                      values; an embedded marker-lookalike line inside
                      a value is data, not framing; the empty value is
                      a legal record; a tainted value is a compile
                      error on BOTH front-ends (assert_snapshot's
                      value flows into println, a taint sink)
  2. the store      — the first verify run fails with "new snapshot"
                      and the recording hint; `-u` writes the store at
                      `__snapshots__/<stem>.snap` with the exact v1
                      framing (magic line, generator note, sorted
                      `@@@` records, length prefixes, separators); a
                      second `-u` changes nothing; verify then passes
  3. the verify     — a changed value fails with a unified diff
                      (stored vs received, byte lengths, the -u hint);
                      `-u` accepts it; the tampered-store matrix (bad
                      magic, non-canonical header, mid-record
                      truncation, missing final separator, length
                      overrun, duplicate record, invalid name) all
                      fail as `<snapshots>` corruption — and update
                      mode REFUSES to regenerate a corrupt store
  4. new/obsolete   — a new assertion is a verify failure until
                      recorded; a removed assertion is an obsolete
                      failure naming it, pruned by -u; a deleted test
                      function is a file-level `<snapshots>` failure,
                      pruned by -u; --grep never mislabels filtered
                      tests (verify stays silent, -u keeps their
                      records); a panicking test's partial records are
                      never recorded
  5. the protocol   — the stream parser rejects a duplicate name in
                      one test, a marker without a length field, a
                      declared length past EOF, a missing frame
                      separator, an invalid name charset, and a
                      leading-zero length; a marker buried mid-line by
                      preceding print() noise still parses (records
                      are never silently lost)
  6. the runner     — parallel -u across two directories writes both
                      stores without cross-contamination; exit codes
                      follow the suite contract (0 green, 1 any
                      failure); JUnit XML carries the diff in the
                      failure node; files without snapshots pay
                      nothing (no store is ever created for them)
  7. the differential—the stage's ok fixture prints byte-identical
                      protocol output from the interpreter and the
                      native compiler (println parity, one store for
                      both backends); the native run's parsed records
                      equal the committed store's values; -u on a copy
                      regenerates the committed store byte for byte
  8. the fixed point— the committed store verifies green in-tree; -u
                      is idempotent (store bytes unchanged, nothing
                      rewritten, no .tmp leftovers); the fixture is a
                      formatter fixed point

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

from boot.lexer import HLError  # noqa: E402
import tools.hltest as hltest  # noqa: E402
from tools.hltest import (  # noqa: E402
    parse_snapshot_records,
    load_store,
    serialize_store,
    store_path_for,
)

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s119_", dir=os.path.join(ROOT, "tests"))

HLTEST = [sys.executable, "tools/hltest.py"]
BOOT = [sys.executable, "boot/boot.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
HLS_CHECK = [sys.executable, "boot/boot.py", "--check"]
HLC_BIN = HLC if os.path.isfile(HLC) else None

FIXTURE = os.path.join(ROOT, "tests", "ok", "feat_stage119_snapshot.hls")
COMMITTED_STORE = os.path.join(ROOT, "tests", "ok", "__snapshots__",
                               "feat_stage119_snapshot.snap")
FAIL_TAINT = os.path.join(ROOT, "tests", "fail", "fail_snapshot_taint.hls")


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
    return subprocess.run(HLTEST + args, capture_output=True, text=True,
                          cwd=cwd)


# ---------------------------------------------------------------------------
print("Section 1 — the assertion emits the exact wire protocol")
# ---------------------------------------------------------------------------

# A probe file run as a plain program: the protocol bytes ARE the stdout.
probe = write("s1/probe.hls", """import "std.test"

fn main() -> int uses IO {
    assert_snapshot("greeting", "hello, snapshot")
    assert_snapshot("empty", "")
    assert_snapshot("multi", "line1\\nline2\\n__SNAP__ fake 2\\nzz")
    println("done")
    return 0
}
""")
r = subprocess.run(BOOT + [probe], capture_output=True)
want = (b"__SNAP__ greeting 15\nhello, snapshot\n"
        b"__SNAP__ empty 0\n\n"
        b"__SNAP__ multi 30\nline1\nline2\n__SNAP__ fake 2\nzz\n"
        b"done\n")
check(r.returncode == 0 and r.stdout == want,
      "assert_snapshot writes marker line + length-prefixed value + "
      "frame separator, byte for byte")
records, err = parse_snapshot_records(want)
check(err is None and records == [
        (b"greeting", b"hello, snapshot"),
        (b"empty", b""),
        (b"multi", b"line1\nline2\n__SNAP__ fake 2\nzz"),
      ], "the parser reads the records back (the embedded "
         "marker-lookalike line is data, not framing)")

# Interleaved diagnostics: println lines between records are noise.
probe2 = write("s1/interleave.hls", """import "std.test"

fn main() -> int uses IO {
    println("computing...")
    assert_snapshot("a", "first")
    println("middle")
    assert_snapshot("b", "second")
    println("done")
    return 0
}
""")
r2 = subprocess.run(BOOT + [probe2], capture_output=True)
recs2, err2 = parse_snapshot_records(r2.stdout)
check(r2.returncode == 0 and err2 is None and recs2 == [
        (b"a", b"first"), (b"b", b"second")],
      "diagnostic println lines between records never leak into values")

# A marker glued mid-line by a preceding print() (no newline) still
# parses: records are never silently lost to output interleaving.
glued = b"partial-no-newline__SNAP__ glued 5\nvalue\n"
recs3, err3 = parse_snapshot_records(glued)
check(err3 is None and recs3 == [(b"glued", b"value")],
      "a marker glued mid-line by preceding print() noise still parses")

# A tainted value cannot be snapshotted: the value parameter is str,
# so the taint is rejected at the call site on both front-ends.
rc = subprocess.run(HLS_CHECK + [FAIL_TAINT], capture_output=True, text=True)
check(rc.returncode == 1 and "tainted[str]" in rc.stderr
      and "assert_snapshot" in rc.stderr,
      "a tainted value is a compile error naming assert_snapshot "
      "(interpreter front-end)")
if HLC_BIN:
    rn = subprocess.run([HLC_BIN, FAIL_TAINT, os.path.join(TMP, "x.c")],
                        capture_output=True, text=True)
    check(rn.returncode != 0 and "tainted[str]" in rn.stderr
          and "assert_snapshot" in rn.stderr,
          "the native front-end rejects the tainted value with the "
          "same words")
else:
    bad("bin/hlc missing — the native front-end half of section 1 "
        "could not run")

# ---------------------------------------------------------------------------
print("Section 2 — the store: record, frame, fixed point")
# ---------------------------------------------------------------------------

work = write("s2/fixture.hls", open(FIXTURE, "rb").read())

# No store yet: verify fails with the recording hint.
r = run_cli([work])
check(r.returncode == 1 and "new snapshot 'greeting'" in r.stdout
      and "--update-snapshots" in r.stdout,
      "verify without a store fails with 'new snapshot' + the "
      "recording hint (exit 1)")

# Record it. The store lands at __snapshots__/<stem>.snap next to the
# test file, with the committed fixture's exact record set.
r = run_cli(["-u", work])
store = os.path.join(TMP, "s2", "__snapshots__", "fixture.snap")
check(r.returncode == 0 and os.path.isfile(store),
      "-u records the snapshots and writes the store at "
      "__snapshots__/<stem>.snap")
committed = open(COMMITTED_STORE, "rb").read()
recorded = open(store, "rb").read()
check(recorded == committed,
      "the recorded store is byte-identical to the committed one "
      "(deterministic serialization: magic line, generator note, "
      "sorted @@@ records)")

# The framing is pinned explicitly, not just by total equality.
check(recorded.startswith(b"# hltest-snapshots v1\n"
                          b"# generated by `hltest --update-snapshots` - "
                          b"do not edit by hand\n"),
      "the store header is the two fixed v1 lines")
check(b"@@@ test_snapshot_plain greeting 15\nhello, snapshot\n" in recorded
      and b"@@@ test_snapshot_empty empty 0\n\n" in recorded,
      "records are framed '@@@ <test> <name> <len>' + value + separator")
keys = list(load_store(store).keys())
check(keys == sorted(keys),
      "records are sorted by (test, name) so reruns do not churn the file")

# A second -u is a no-op; verify then passes.
r2 = run_cli(["-u", work])
check(r2.returncode == 0 and "store(s) updated" not in r2.stdout
      and open(store, "rb").read() == recorded,
      "a second -u changes nothing (no rewrite, no report line)")
r3 = run_cli([work])
check(r3.returncode == 0 and "6 pass, 0 fail" in r3.stdout,
      "verify passes against the recorded store (6 pass, 0 fail)")

# ---------------------------------------------------------------------------
print("Section 3 — the verify: diffs, and the corrupt-store matrix")
# ---------------------------------------------------------------------------

# A changed value fails with a unified diff.
mut = write("s3/fixture.hls",
            open(FIXTURE, "rb").read().replace(b"hello, snapshot",
                                               b"hello, CHANGED!"))
shutil.copytree(os.path.join(TMP, "s2", "__snapshots__"),
                os.path.join(TMP, "s3", "__snapshots__"))
r = run_cli([mut])
check(r.returncode == 1 and "snapshot 'greeting' differs" in r.stdout
      and "--- stored" in r.stdout and "+++ received" in r.stdout
      and "-hello, snapshot" in r.stdout
      and "+hello, CHANGED!" in r.stdout
      and "--update-snapshots to accept" in r.stdout,
      "a changed value fails with a unified diff and the accept hint")
r = run_cli(["-u", mut])
check(r.returncode == 0 and "~1 changed" in r.stdout
      and b"hello, CHANGED!" in open(
          os.path.join(TMP, "s3", "__snapshots__", "fixture.snap"),
          "rb").read(),
      "-u accepts the change and records the new bytes")

# The corrupt-store matrix: every tampering fails as <snapshots>
# corruption, and update mode REFUSES to regenerate over it.
def corrupt_and_run(name, mutate, expect):
    d = os.path.join(TMP, "s3c", name)
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "fixture.hls")
    shutil.copy(FIXTURE, f)
    sp = os.path.join(d, "__snapshots__", "fixture.snap")
    os.makedirs(os.path.dirname(sp), exist_ok=True)
    raw = open(COMMITTED_STORE, "rb").read()
    tampered = mutate(raw)
    with open(sp, "wb") as fh:
        fh.write(tampered)
    rv = run_cli([f])
    good = (rv.returncode == 1 and "<snapshots>" in rv.stdout
            and "corrupt" in rv.stdout and expect in rv.stdout)
    ru = run_cli(["-u", f])
    kept = open(sp, "rb").read()
    good = good and ru.returncode == 1 and kept == tampered
    check(good, "store tampered (%s) -> <snapshots> failure mentioning "
          "'%s', and -u refuses to regenerate over it" % (name, expect))


corrupt_and_run("bad-magic",
                lambda b: b"# not a store at all\n" + b, "magic line")
corrupt_and_run("bad-header",
                lambda b: b.split(b"\n", 1)[0] + b"\n# wrong note\n"
                + b.split(b"\n", 2)[2],
                "non-canonical store header")
# 2 bytes off the tail: the LAST record ('second 3') keeps its header
# but loses 1 value byte + its separator, so the declared length
# overruns what remains.
corrupt_and_run("truncated-mid-record",
                lambda b: b[:-2], "runs past EOF")
corrupt_and_run("missing-final-separator",
                lambda b: b[:-1], "frame separator")
# Overrun the LAST record's declared length (a mid-file overrun would
# swallow the following records' bytes and is position-dependent).
corrupt_and_run("length-overrun",
                lambda b: b.replace(b"@@@ test_snapshot_two_records second 3",
                                    b"@@@ test_snapshot_two_records second 99"),
                "runs past EOF")
corrupt_and_run("duplicate-record",
                lambda b: b + b"@@@ test_snapshot_plain greeting 15\n"
                b"hello, snapshot\n",
                "duplicate record")
# ';' is outside the name charset; same byte length so the framing
# (and the declared value length) is otherwise intact.
corrupt_and_run("invalid-name",
                lambda b: b.replace(b"greeting", b"gr;eting"),
                "invalid test or snapshot name")

# ---------------------------------------------------------------------------
print("Section 4 — new and obsolete snapshots, grep safety, panics")
# ---------------------------------------------------------------------------

# A new assertion is a verify failure until recorded.
newf = write("s4/fixture.hls",
             open(FIXTURE, "rb").read().replace(
                 b'assert_snapshot("first", "one")',
                 b'assert_snapshot("first", "one")\n'
                 b'    assert_snapshot("extra", "bonus")'))
r = run_cli([newf])
check(r.returncode == 1 and "new snapshot 'extra'" in r.stdout,
      "a new assertion fails verify until it is recorded")
shutil.copytree(os.path.join(TMP, "s2", "__snapshots__"),
                os.path.join(TMP, "s4", "__snapshots__"))
r = run_cli(["-u", newf])
check(r.returncode == 0 and "+1 new" in r.stdout and
      b'@@@ test_snapshot_two_records extra 5\nbonus\n' in open(
          os.path.join(TMP, "s4", "__snapshots__", "fixture.snap"),
          "rb").read(),
      "-u records the new snapshot")

# A removed assertion is an obsolete failure naming the record.
delf = write("s4b/fixture.hls",
             open(FIXTURE, "rb").read().replace(
                 b'    assert_snapshot("padded", "  keep my spaces  ")\n', b""))
shutil.copytree(os.path.join(TMP, "s2", "__snapshots__"),
                os.path.join(TMP, "s4b", "__snapshots__"))
r = run_cli([delf])
check(r.returncode == 1 and "obsolete snapshot 'padded'" in r.stdout
      and "--update-snapshots" in r.stdout,
      "a removed assertion fails verify naming the obsolete record")
r = run_cli(["-u", delf])
sp = os.path.join(TMP, "s4b", "__snapshots__", "fixture.snap")
check(r.returncode == 0 and b"padded" not in open(sp, "rb").read(),
      "-u prunes the obsolete record from the store")

# A deleted TEST FUNCTION is a file-level <snapshots> failure.
nofn = write("s4c/fixture.hls", b"""import "std.test"

fn test_snapshot_plain() uses IO {
    assert_snapshot("greeting", "hello, snapshot")
}

fn main() -> int uses IO {
    test_snapshot_plain()
    return 0
}
""")
shutil.copytree(os.path.join(TMP, "s2", "__snapshots__"),
                os.path.join(TMP, "s4c", "__snapshots__"))
r = run_cli([nofn])
check(r.returncode == 1 and "<snapshots>" in r.stdout
      and "not in the file" in r.stdout
      and "test_snapshot_multiline" in r.stdout,
      "a deleted test function fails with the file-level <snapshots> "
      "entry naming it")
r = run_cli(["-u", nofn])
sp = os.path.join(TMP, "s4c", "__snapshots__", "fixture.snap")
recs = load_store(sp) if os.path.isfile(sp) else {}
check(r.returncode == 0
      and set(t.decode() for (t, _n) in recs) == {"test_snapshot_plain"},
      "-u prunes the deleted function's records and keeps the rest")

# --grep never mislabels filtered tests: verify stays silent, -u keeps
# their records untouched.
r = run_cli(["--grep", "test_snapshot_plain", nofn])
check(r.returncode == 0 and "not in the file" not in r.stdout
      and "obsolete" not in r.stdout,
      "--grep runs stay silent about tests that did not run (verify)")
sp_before = open(sp, "rb").read()
r = run_cli(["-u", "--grep", "test_snapshot_plain", nofn])
check(r.returncode == 0 and open(sp, "rb").read() == sp_before,
      "--grep updates never touch the filtered tests' records")

# A panicking test's partial records are never recorded.
pan = write("s4d/panic.hls", b"""import "std.test"

fn test_panics_after() uses IO {
    assert_snapshot("before", "emitted before the panic")
    panic("boom")
}

fn main() -> int uses IO {
    test_panics_after()
    return 0
}
""")
r = run_cli(["-u", pan])
check(r.returncode == 1 and "boom" in r.stdout
      and not os.path.exists(os.path.join(TMP, "s4d", "__snapshots__")),
      "a panicking test fails with its panic, and its partial records "
      "are never written to a store")

# ---------------------------------------------------------------------------
print("Section 5 — the protocol errors")
# ---------------------------------------------------------------------------

# Duplicate name in one test (HLS level).
dup = write("s5/dup.hls", b"""import "std.test"

fn test_dup() uses IO {
    assert_snapshot("a", "one")
    assert_snapshot("a", "two")
}

fn main() -> int uses IO {
    test_dup()
    return 0
}
""")
r = run_cli([dup])
check(r.returncode == 1 and "duplicate snapshot name 'a'" in r.stdout,
      "a duplicate snapshot name in one test fails verify")

# Malformed / adversarial stream bytes (parser level, the same parser
# the runner drives).
cases = [
    (b"__SNAP__ bogus no-length-here\n", "missing length field",
     "length"),
    (b"__SNAP__ big 999\n", "declared length past EOF", "999 bytes"),
    (b"__SNAP__ sep 5\nvalue", "missing frame separator", "separator"),
    (b"__SNAP__ bad name 5\nvalue\n", "invalid name charset", "name"),
    (b"__SNAP__ z 007\nvalue\n", "leading-zero length", "length"),
    (b"__SNAP__ end 3", "unterminated record header", "unterminated"),
]
for stream, label, needle in cases:
    _recs, e = parse_snapshot_records(stream)
    check(e is not None and needle in e,
          "the parser rejects %s (%r)" % (label, stream[:28]))

# ---------------------------------------------------------------------------
print("Section 6 — the runner integration")
# ---------------------------------------------------------------------------

# Parallel -u across two directories: both stores written, no
# cross-contamination.
os.makedirs(os.path.join(TMP, "s6/a"), exist_ok=True)
os.makedirs(os.path.join(TMP, "s6/b"), exist_ok=True)
fa = write("s6/a/alpha.hls", open(FIXTURE, "rb").read())
fb = write("s6/b/beta.hls",
           open(FIXTURE, "rb").read().replace(b"hello, snapshot",
                                              b"other value"))
r = run_cli(["-j", "4", "-u", "-r", os.path.join(TMP, "s6")])
sa = os.path.join(TMP, "s6", "a", "__snapshots__", "alpha.snap")
sb = os.path.join(TMP, "s6", "b", "__snapshots__", "beta.snap")
recs_a = load_store(sa) if os.path.isfile(sa) else {}
recs_b = load_store(sb) if os.path.isfile(sb) else {}
check(r.returncode == 0 and os.path.isfile(sa) and os.path.isfile(sb)
      and recs_a.get((b"test_snapshot_plain", b"greeting"))
      == b"hello, snapshot"
      and recs_b.get((b"test_snapshot_plain", b"greeting"))
      == b"other value",
      "parallel -u writes each file's own store, values never cross "
      "files")

# Exit codes follow the suite contract (a FRESH mismatch: `mut` was
# already accepted by -u in section 3, so it verifies green now).
mut2_dir = os.path.join(TMP, "s6c")
os.makedirs(mut2_dir, exist_ok=True)
mut2 = os.path.join(mut2_dir, "fixture.hls")
shutil.copy(FIXTURE, mut2)
os.makedirs(os.path.join(mut2_dir, "__snapshots__"), exist_ok=True)
shutil.copy(COMMITTED_STORE,
            os.path.join(mut2_dir, "__snapshots__", "fixture.snap"))
with open(mut2, "rb") as f:
    mb = f.read()
with open(mut2, "wb") as f:
    f.write(mb.replace(b"hello, snapshot", b"hello, MISMATCH!"))
check(run_cli([fa]).returncode == 0
      and run_cli([mut2]).returncode == 1,
      "exit codes: 0 when snapshots verify, 1 when any fails")

# JUnit carries the diff in the failure node.
jr = run_cli(["--junit", os.path.join(TMP, "s6", "j.xml"), mut2])
xml = open(os.path.join(TMP, "s6", "j.xml"), encoding="utf-8").read()
check(jr.returncode == 1 and "<failure" in xml
      and "'greeting' differs" in xml
      and "+hello, MISMATCH!" in xml,
      "the JUnit XML failure node carries the unified diff")

# Files without snapshots pay nothing: same counts, and that file
# never grows a store of its own (the committed fixture's store is a
# DIFFERENT stem in the same directory, which is fine).
no_snap = os.path.join(ROOT, "tests", "ok", "feat_stage18_hltest.hls")
r = run_cli([no_snap])
no_store = not os.path.exists(os.path.join(
    ROOT, "tests", "ok", "__snapshots__", "feat_stage18_hltest.snap"))
check(r.returncode == 0 and "12 pass, 0 fail" in r.stdout and no_store,
      "a file without snapshots runs unchanged and never grows a store")

# Store path convention.
check(store_path_for("/x/y/feat.hls") == "/x/y/__snapshots__/feat.snap"
      and store_path_for("/x/y/feat.hls").endswith(".snap"),
      "store_path_for: __snapshots__/<stem>.snap next to the test file")

# ---------------------------------------------------------------------------
print("Section 7 — the differential: one store, both backends")
# ---------------------------------------------------------------------------

if HLC_BIN and shutil.which("gcc"):
    c = os.path.join(TMP, "s7.c")
    e = os.path.join(TMP, "s7")
    b = subprocess.run([HLC_BIN, FIXTURE, c], capture_output=True, text=True)
    g = subprocess.run(["gcc", "-O2", "-o", e, c, "-lm", "-pthread"],
                       capture_output=True, text=True)
    if b.returncode == 0 and g.returncode == 0:
        rn = subprocess.run([e], capture_output=True)
        ri = subprocess.run(BOOT + [FIXTURE], capture_output=True)
        check(rn.returncode == 0 and rn.stdout == ri.stdout,
              "the fixture's protocol output is byte-identical: "
              "interpreter vs native (println parity)")
        nrecs, nerr = parse_snapshot_records(rn.stdout)
        stored = load_store(COMMITTED_STORE)
        by_name = {n: v for ((_t, n), v) in stored.items()}
        check(nerr is None and len(nrecs) == len(by_name)
              and all(by_name.get(n) == v for (n, v) in nrecs),
              "the native run's parsed records equal the committed "
              "store's values (one store serves both backends)")
    else:
        bad("the native build of the fixture failed: %s / %s"
            % (b.stderr.strip()[:60], g.stderr.strip()[:60]))
else:
    bad("the native half of the differential could not run "
        "(no gcc or no bin/hlc)")

# -u on a copy regenerates the committed store byte for byte (already
# proven in section 2, restated here as the differential contract).
check(recorded == committed,
      "-u on a fresh copy reproduces the committed store byte for byte")

# ---------------------------------------------------------------------------
print("Section 8 — the fixed point and tree hygiene")
# ---------------------------------------------------------------------------

# The committed store verifies green in-tree.
r = run_cli([FIXTURE])
check(r.returncode == 0 and "6 pass, 0 fail" in r.stdout,
      "the committed store verifies the committed fixture green")

# -u is idempotent: no rewrite, no report line, no .tmp leftovers.
before = open(COMMITTED_STORE, "rb").read()
r = run_cli(["-u", FIXTURE])
after = open(COMMITTED_STORE, "rb").read()
leftovers = [p for p in os.listdir(os.path.dirname(COMMITTED_STORE))
             if p.endswith(".tmp")]
check(r.returncode == 0 and before == after
      and "store(s) updated" not in r.stdout and not leftovers,
      "-u on the committed fixture is a no-op (bytes, report, no .tmp)")

# The fixture is a formatter fixed point.
fr = subprocess.run([sys.executable, "tools/hlfmt.py", "-c", FIXTURE],
                    capture_output=True, text=True)
check(fr.returncode == 0 and "already formatted" in fr.stdout,
      "the fixture is an hlfmt fixed point")

# The fail probe is rejected by the full suite contract (suite_01 loops
# every tests/fail/*.hls expecting exit 1 + a message).
rc = subprocess.run(HLS_CHECK + [FAIL_TAINT], capture_output=True, text=True)
check(rc.returncode == 1 and "tainted" in rc.stderr,
      "the taint fail file rejects under --check with exit 1")

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
if FAIL == 0:
    print("=== Stage 119 acceptance: %d PASS / %d FAIL ===" % (PASS, FAIL))
    sys.exit(0)
print("=== Stage 119 acceptance: %d PASS / %d FAIL ===" % (PASS, FAIL))
sys.exit(1)
