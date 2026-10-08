#!/usr/bin/env python3
"""Stage 120 acceptance gate — hltest parameterised (table-driven) tests.

Run with `make cases-acceptance` (or
`python3 tests/cases_acceptance.py`).

Eight sections:

  1. the convention — a test fn with exactly one parameter + a
                      cases_* table runs ONCE PER ROW, each row its own
                      result named `test_<name>#<case>`; struct rows,
                      str rows and enum rows all drive the test; a
                      failing row fails alone and the table's other
                      rows still run and pass; mark_skip inside one row
                      skips THAT row only; a plain test's mark_skip is
                      a SKIP too (the Stage 18 bytes-panic fix); an
                      empty table is a SKIP, never a silent pass
  2. the static      — the convention errors fail before any row runs,
     contract          with the fix in the message: a missing table, a
                      table that takes parameters, a test with two
                      parameters, a statically-checkable row/test type
                      mismatch naming BOTH types, a stray table with no
                      matching parameterised test (typo or a test that
                      lost its parameter); exit code 1
  3. the table       — a panicking table fails under the base name
     runtime           (message decoded, no b'' repr); a table that
                      returns void names what it returned; duplicate
                      case names fail the table; case names outside
                      [A-Za-z0-9_.:-] fail with the charset rule; the
                      store-key grammar helpers accept/reject exactly
                      the Stage 120 shapes
  4. the store       — rows record under `test_x#case` keys with the
                      exact v1 framing; verify passes; a changed row
                      value fails with a unified diff and the -u hint;
                      a row dropped from the table is a base-name
                      failure naming it and -u prunes it; plain records
                      left behind after a test became parameterised
                      (and the reverse) are stale-shape failures and
                      -u prunes them
  5. the selection   — --grep matches the base name (whole table) or a
                      full row name; -u keeps unselected rows' records;
                      a broken table never fails a run that did not
                      select it; a grep matching nothing is a
                      <no-tests> skip, not an error
  6. the runner      — parallel -u across two directories writes both
                      row stores without cross-contamination; exit
                      codes follow the suite contract; JUnit XML
                      carries rows as testcases (skip node included)
  7. the differential— the stage's ok fixture compiles natively
                      (bin/hlc + gcc; the TestCase[T] generic goes
                      through the real compiler) and its output is
                      byte-identical to the interpreter's; the native
                      records equal the committed store's values; -u on
                      a copy regenerates the committed store byte for
                      byte
  8. the fixed point— the committed store verifies green in-tree; -u
                      is idempotent (no rewrite, no .tmp leftovers);
                      the fixture is an hlfmt fixed point; boot --check
                      is green

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

from boot.lexer import HLError  # noqa: E402  (kept for parity with 119)
import tools.hltest as hltest  # noqa: E402
from tools.hltest import (  # noqa: E402
    load_store,
    store_path_for,
    row_base,
    _table_row_type,
    _STORE_TEST_RE,
)

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="_gate_s120_", dir=os.path.join(ROOT, "tests"))

HLTEST = [sys.executable, "tools/hltest.py"]
BOOT = [sys.executable, "boot/boot.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
HLC_BIN = HLC if os.path.isfile(HLC) else None

FIXTURE = os.path.join(ROOT, "tests", "ok", "feat_stage120_cases.hls")
COMMITTED_STORE = os.path.join(ROOT, "tests", "ok", "__snapshots__",
                               "feat_stage120_cases.snap")


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
print("Section 1 — the convention: one row, one result, one fresh state")
# ---------------------------------------------------------------------------

rows = write("s1/rows.hls", """import "std.test"

struct Pt {
    x: int,
    y: int
}

fn cases_sum() -> list[TestCase[Pt]] {
    return [
        TestCase { name: "first", row: Pt { x: 1, y: 2 } },
        TestCase { name: "second", row: Pt { x: 10, y: 20 } }
    ]
}

fn test_sum(p: Pt) {
    assert_eq_int(p.x + p.y, p.y + p.x)
}

fn cases_tag() -> list[TestCase[str]] {
    return [
        TestCase { name: "short", row: "a" },
        TestCase { name: "long", row: "abc" }
    ]
}

fn test_tag(s: str) {
    assert_true_msg(s.len() > 0, "non-empty")
}

enum Mood {
    Up,
    Down
}

fn cases_mood() -> list[TestCase[Mood]] {
    return [
        TestCase { name: "up", row: Mood.Up },
        TestCase { name: "down", row: Mood.Down }
    ]
}

fn test_mood(m: Mood) {
    let want: int = match m {
        Mood.Up => 1,
        Mood.Down => -1
    }
    assert_eq_int(mood_sign(m), want)
}

fn mood_sign(m: Mood) -> int {
    return match m {
        Mood.Up => 1,
        Mood.Down => -1
    }
}

fn main() -> int {
    return 0
}
""")
r = run_cli([rows])
check(r.returncode == 0 and "6 pass, 0 fail, 0 skip" in r.stdout
      and "test_sum#first" in r.stdout and "test_sum#second" in r.stdout
      and "test_tag#short" in r.stdout and "test_mood#up" in r.stdout,
      "a table test runs once per row; every row is its own result "
      "named test_<name>#<case> (struct, str and enum rows)")

# A failing row fails ALONE: the table's other rows still run.
midfail = write("s1/midfail.hls", """import "std.test"

struct C {
    v: int
}

fn cases_c() -> list[TestCase[C]] {
    return [
        TestCase { name: "good", row: C { v: 1 } },
        TestCase { name: "bad", row: C { v: 2 } },
        TestCase { name: "also-good", row: C { v: 3 } }
    ]
}

fn test_c(c: C) {
    assert_eq_int(c.v % 2, 1)
}

fn main() -> int {
    return 0
}
""")
r = run_cli([midfail])
check(r.returncode == 1 and "test_c#bad" in r.stdout
      and "assert_eq_int failed" in r.stdout
      and "PASS" in r.stdout and "test_c#good" in r.stdout
      and "test_c#also-good" in r.stdout
      and "2 pass, 1 fail, 0 skip" in r.stdout,
      "a failing row fails alone; the other rows still run and pass "
      "(exit 1)")

# mark_skip inside ONE row: that row is a SKIP, the rest pass.
rowskip = write("s1/rowskip.hls", """import "std.test"

struct C {
    v: int
}

fn cases_c() -> list[TestCase[C]] {
    return [
        TestCase { name: "kept", row: C { v: 1 } },
        TestCase { name: "parked", row: C { v: 0 } }
    ]
}

fn test_c(c: C) {
    if c.v == 0 {
        mark_skip("not interesting on this shard")
    }
    assert_eq_int(c.v, 1)
}

fn main() -> int {
    return 0
}
""")
r = run_cli(["-v", rowskip])
check(r.returncode == 0 and "test_c#parked" in r.stdout
      and "not interesting on this shard" in r.stdout
      and "1 pass, 0 fail, 1 skip" in r.stdout,
      "mark_skip inside one row reports THAT row SKIP; the rest run")

# The Stage 18 latent bug this stage fixes: a PLAIN test's mark_skip
# was classified against str(ex) (a b'...' repr) and never matched the
# reserved prefix — every skip was a FAIL.
plainskip = write("s1/plainskip.hls", """import "std.test"

fn test_plain_skip() {
    mark_skip("table-free skip")
}

fn main() -> int {
    return 0
}
""")
r = run_cli(["-v", plainskip])
check(r.returncode == 0 and "SKIP" in r.stdout
      and "table-free skip" in r.stdout
      and "0 pass, 0 fail, 1 skip" in r.stdout,
      "a plain test's mark_skip is a SKIP (Stage 18 bytes-panic fix), "
      "never a FAIL with a b'...' payload")

# An empty table is a SKIP with its reason, not a silent pass.
empty = write("s1/empty.hls", """import "std.test"

struct C {
    v: int
}

fn cases_c() -> list[TestCase[C]] {
    return []
}

fn test_c(c: C) {
    assert_eq_int(c.v, c.v)
}

fn main() -> int {
    return 0
}
""")
r = run_cli(["-v", empty])
check(r.returncode == 0 and "0 pass, 0 fail, 1 skip" in r.stdout
      and "empty case table 'cases_c' (0 rows)" in r.stdout,
      "an empty table is a SKIP naming the table, never a silent pass")

# ---------------------------------------------------------------------------
print("Section 2 — the static contract: convention errors fail before rows run")
# ---------------------------------------------------------------------------

static = write("s2/static.hls", """import "std.test"

struct C {
    v: int
}

fn test_no_table(c: C) {
    assert_eq_int(c.v, c.v)
}

fn test_two(a: int, b: int) {
    assert_eq_int(a, b)
}

fn cases_takes_params(x: int) -> list[TestCase[C]] {
    return [TestCase { name: "c", row: C { v: x } }]
}

fn test_takes_params(c: C) {
    assert_eq_int(c.v, c.v)
}

fn cases_mismatch() -> list[TestCase[str]] {
    return [TestCase { name: "s", row: "x" }]
}

fn test_mismatch(c: C) {
    assert_eq_int(c.v, c.v)
}

fn cases_stray() -> list[TestCase[C]] {
    return [TestCase { name: "s", row: C { v: 1 } }]
}

fn cases_plainname() -> list[TestCase[C]] {
    return [TestCase { name: "s", row: C { v: 1 } }]
}

fn test_plainname() {
    assert_true(true)
}

fn main() -> int {
    return 0
}
""")
r = run_cli([static])
check(r.returncode == 1
      and "parameterised test 'test_no_table' has no case table" in r.stdout
      and "cases_no_table" in r.stdout,
      "a parameterised test without its table fails, naming the "
      "expected companion fn")
check("parameterised test 'test_two' must take exactly one parameter"
      in r.stdout,
      "a test with two parameters fails: the row value is the only "
      "parameter")
check("case table 'cases_takes_params' must take no parameters"
      in r.stdout,
      "a table that takes parameters fails (the runner calls it with "
      "none)")
check("case table 'cases_mismatch' returns rows of type 'str' but "
      "'test_mismatch' takes 'C'" in r.stdout,
      "a statically-checkable row/test type mismatch fails naming "
      "BOTH types")
check("cases_stray" in r.stdout
      and "no matching parameterised test 'test_stray'" in r.stdout,
      "a stray table with no matching test fails (the typo case)")
check("case table 'cases_plainname' has a matching test 'test_plainname', "
      "but the test takes no parameters" in r.stdout,
      "a table whose matching test lost its parameter fails")

# ---------------------------------------------------------------------------
print("Section 3 — the table runtime: panics, shapes, names")
# ---------------------------------------------------------------------------

tablepanic = write("s3/tablepanic.hls", """import "std.test"

struct C {
    v: int
}

fn cases_boom() -> list[TestCase[C]] {
    panic("table construction exploded")
}

fn test_boom(c: C) {
    assert_eq_int(c.v, c.v)
}

fn main() -> int {
    return 0
}
""")
r = run_cli([tablepanic])
check(r.returncode == 1
      and "case table 'cases_boom' panicked: table construction exploded"
      in r.stdout and "b'" not in r.stdout,
      "a panicking table fails under the base name, message decoded "
      "(no Python repr)")

voidtable = write("s3/voidtable.hls", """import "std.test"

struct C {
    v: int
}

fn cases_void() {
    let unused: int = 1
}

fn test_void(c: C) {
    assert_eq_int(c.v, c.v)
}

fn main() -> int {
    return 0
}
""")
r = run_cli([voidtable])
check(r.returncode == 1 and "case table 'cases_void' returned void, "
      "expected a list[std.test.TestCase[...]]" in r.stdout,
      "a table that returns nothing names what it returned (void) and "
      "the expected shape")

dup = write("s3/dup.hls", """import "std.test"

struct C {
    v: int
}

fn cases_c() -> list[TestCase[C]] {
    return [
        TestCase { name: "same", row: C { v: 1 } },
        TestCase { name: "same", row: C { v: 2 } }
    ]
}

fn test_c(c: C) {
    assert_eq_int(c.v, c.v)
}

fn main() -> int {
    return 0
}
""")
r = run_cli([dup])
check(r.returncode == 1 and "duplicate case name 'same' in 'cases_c'"
      in r.stdout and "race for one report line" in r.stdout,
      "duplicate case names fail the table (one report line, one "
      "store slot)")

badname = write("s3/badname.hls", """import "std.test"

struct C {
    v: int
}

fn cases_c() -> list[TestCase[C]] {
    return [
        TestCase { name: "has space", row: C { v: 1 } }
    ]
}

fn test_c(c: C) {
    assert_eq_int(c.v, c.v)
}

fn main() -> int {
    return 0
}
""")
r = run_cli([badname])
check(r.returncode == 1 and "invalid case name 'has space' in 'cases_c'"
      in r.stdout and "[A-Za-z0-9_.:-]" in r.stdout,
      "case names outside [A-Za-z0-9_.:-] fail with the charset rule "
      "(a row name is also a store key)")

# Store-key grammar: the helpers behind the report and the store.
check(row_base(b"test_parse#empty") == b"test_parse"
      and row_base(b"test_plain") == b"test_plain",
      "row_base: the first # starts the row suffix; a plain key is "
      "its own base")
check(_table_row_type("list[TestCase[AddCase]]") == "AddCase"
      and _table_row_type("list[TestCase[list[int]]]") == "list[int]"
      and _table_row_type("list[Pt]") is None
      and _table_row_type("int") is None
      and _table_row_type(None) is None,
      "_table_row_type: parses list[TestCase[X]] (nested generics "
      "included), refuses everything else silently")
check(bool(_STORE_TEST_RE.match(b"test_add#zero-left"))
      and bool(_STORE_TEST_RE.match(b"test_plain"))
      and not _STORE_TEST_RE.match(b"test_add#two#hashes")
      and not _STORE_TEST_RE.match(b"has space"),
      "the store test-name grammar: <test>[#<case>], one optional "
      "row suffix")

# ---------------------------------------------------------------------------
print("Section 4 — the store: rows are first-class store keys")
# ---------------------------------------------------------------------------

work = write("s4/fixture.hls", open(FIXTURE, "rb").read())

# Record the fixture: row keys, exact framing.
r = run_cli(["-u", work])
store = os.path.join(TMP, "s4", "__snapshots__", "fixture.snap")
recs = load_store(store) if os.path.isfile(store) else {}
check(r.returncode == 0 and os.path.isfile(store)
      and set(recs.keys()) == {(b"test_bound#boundary", b"range-line"),
                               (b"test_bound#inside", b"range-line")}
      and recs[(b"test_bound#inside", b"range-line")] == b"5 in [0, 10]",
      "-u records each row under its test_x#case store key with its "
      "own value")
raw = open(store, "rb").read()
check(raw.startswith(b"# hltest-snapshots v1\n"
                     b"# generated by `hltest --update-snapshots` - "
                     b"do not edit by hand\n")
      and b"@@@ test_bound#boundary range-line 13\n10 in [0, 10]\n"
      in raw,
      "the store framing is unchanged: v1 header, '@@@ <test#case> "
      "<name> <len>' records, sorted")
r = run_cli([work])
check(r.returncode == 0 and "11 pass, 0 fail, 1 skip" in r.stdout,
      "verify passes against the recorded row store (11 pass, 1 "
      "table-level skip)")

# A changed row value fails THAT row with the diff and the hint. The
# rendered value is computed from the " in [" literal, so the source
# edit swaps that literal (the STORE holds the rendered bytes; the
# source never contains them).
mut_src = open(FIXTURE, "rb").read().replace(b'" in ["', b'" within ["')
mut = write("s4m/mut.hls", mut_src)
shutil.copytree(os.path.join(TMP, "s4", "__snapshots__"),
                os.path.join(TMP, "s4m", "__snapshots__"))
shutil.move(os.path.join(TMP, "s4m", "__snapshots__", "fixture.snap"),
            os.path.join(TMP, "s4m", "__snapshots__", "mut.snap"))
r = run_cli([mut])
check(r.returncode == 1 and "test_bound#inside" in r.stdout
      and "snapshot 'range-line' differs" in r.stdout
      and "-5 in [0, 10]" in r.stdout and "+5 within [0, 10]" in r.stdout
      and "--update-snapshots" in r.stdout,
      "a changed row value fails that row with a unified diff and the "
      "accept hint (the other row still passes)")
r = run_cli(["-u", mut])
check(r.returncode == 0
      and load_store(os.path.join(TMP, "s4m", "__snapshots__",
                                  "mut.snap"))[
          (b"test_bound#inside", b"range-line")] == b"5 within [0, 10]",
      "-u accepts the changed row value")

# A row dropped from the table: verify fails under the base name
# naming the dead row; -u prunes exactly it. The probe keeps the
# fixture's NAME (the copied store is <stem>.snap next to it).
drop = write("s4d/fixture.hls",
             open(FIXTURE, "rb").read().replace(
                 b'''    TestCase {
        name: "inside", row: BoundCase {
            v: 5, lo: 0, hi: 10
        }
    },\n''', b""))
shutil.copytree(os.path.join(TMP, "s4", "__snapshots__"),
                os.path.join(TMP, "s4d", "__snapshots__"))
r = run_cli([drop])
check(r.returncode == 1 and "FAIL" in r.stdout
      and "stored snapshot records for case row(s) the table no longer "
      "contains: test_bound#inside" in r.stdout
      and "--update-snapshots" in r.stdout,
      "a row dropped from the table is a verify failure naming the "
      "dead row key")
r = run_cli(["-u", drop])
kept = load_store(os.path.join(TMP, "s4d", "__snapshots__",
                               "fixture.snap"))
check(r.returncode == 0
      and set(kept.keys()) == {(b"test_bound#boundary", b"range-line")},
      "-u prunes exactly the dead row's records")

# Stale shape, both directions: plain records after a test became
# parameterised, and row records after it became plain. Every step
# keeps main() consistent with the current shape (the file must
# type-check for the runner to reach the store logic).
plain_src = """import "std.test"

fn test_evolve() uses IO {
    assert_snapshot("era", "plain")
}

fn main() -> int uses IO {
    test_evolve()
    return 0
}
"""
evolve = write("s4e/evolve.hls", plain_src)
run_cli(["-u", evolve])
para_src = '''import "std.test"

struct C {
    v: int
}

fn cases_evolve() -> list[TestCase[C]] {
    return [TestCase { name: "a", row: C { v: 1 } }]
}

fn test_evolve(c: C) {
    assert_eq_int(c.v, 1)
}

fn main() -> int {
    let rows: list[TestCase[C]] = cases_evolve()
    let mut i: int = 0
    while i < rows.len() {
        test_evolve(rows[i].row)
        i = i + 1
    }
    return 0
}
'''
with open(evolve, "wb") as f:
    f.write(para_src.encode("utf-8"))
r = run_cli([evolve])
check(r.returncode == 1 and "<snapshots>" in r.stdout
      and "shape no longer matches" in r.stdout
      and "test_evolve" in r.stdout,
      "plain records left behind after a test became parameterised "
      "are a stale-shape failure")
r = run_cli(["-u", evolve])
kept = load_store(os.path.join(TMP, "s4e", "__snapshots__",
                               "evolve.snap"))
check(r.returncode == 0
      and set(kept.keys()) == set(),
      "-u prunes the stale plain records (the row emits no snapshots)")
row_src = para_src.replace(
    '''fn test_evolve(c: C) {
    assert_eq_int(c.v, 1)
}''',
    '''fn test_evolve(c: C) uses IO {
    assert_eq_int(c.v, 1)
    assert_snapshot("era", "row")
}''').replace(
    "fn main() -> int {",
    "fn main() -> int uses IO {")
with open(evolve, "wb") as f:
    f.write(row_src.encode("utf-8"))
run_cli(["-u", evolve])
with open(evolve, "wb") as f:
    f.write(plain_src.encode("utf-8"))
r = run_cli([evolve])
check(r.returncode == 1 and "<snapshots>" in r.stdout
      and "test_evolve#a" in r.stdout,
      "row records left behind after a test became plain are a "
      "stale-shape failure naming the row key")

# ---------------------------------------------------------------------------
print("Section 5 — the selection: --grep knows rows")
# ---------------------------------------------------------------------------

# grep by base name: the whole table runs.
r = run_cli(["--grep", "test_bound", work])
check(r.returncode == 0 and "test_bound#boundary" in r.stdout
      and "test_bound#inside" in r.stdout
      and "test_add#positives" not in r.stdout,
      "--grep by base name runs the whole table and nothing else")
# grep by row name: exactly one row.
r = run_cli(["--grep", "#boundary", work])
check(r.returncode == 0 and "test_bound#boundary" in r.stdout
      and "test_bound#inside" not in r.stdout
      and "1 pass, 0 fail, 0 skip" in r.stdout,
      "--grep '#case' selects one row")
# -u with a row filter keeps the unselected row's records.
sp = os.path.join(TMP, "s4", "__snapshots__", "fixture.snap")
before = open(sp, "rb").read()
r = run_cli(["-u", "--grep", "#boundary", work])
after = open(sp, "rb").read()
check(r.returncode == 0 and before == after,
      "-u with a row filter never touches the unselected rows' "
      "records")
# A broken table never fails a run that did not select it.
r = run_cli(["--grep", "test_boom", tablepanic])
check(r.returncode == 1 and "case table 'cases_boom' panicked"
      in r.stdout,
      "a selected broken table still fails its run")
r = run_cli(["--grep", "nothing-matches-this", tablepanic])
check("case table 'cases_boom' panicked" not in r.stdout,
      "an unselected broken table never fails a targeted run "
      "(--grep is a selection tool)")
# grep matching nothing at all: a skip, not an error (-v shows the
# skip's reason line).
r = run_cli(["-v", "--grep", "zzz-no-match", work])
check(r.returncode == 0 and "<no-tests>" in r.stdout
      and "no tests match --grep" in r.stdout,
      "a grep matching nothing is a <no-tests> skip with its reason")

# ---------------------------------------------------------------------------
print("Section 6 — the runner integration")
# ---------------------------------------------------------------------------

os.makedirs(os.path.join(TMP, "s6/a"), exist_ok=True)
os.makedirs(os.path.join(TMP, "s6/b"), exist_ok=True)
fa = write("s6/a/alpha.hls", open(FIXTURE, "rb").read())
fb = write("s6/b/beta.hls",
           open(FIXTURE, "rb").read().replace(b'" in ["',
                                              b'" between ["'))
r = run_cli(["-j", "4", "-u", "-r", os.path.join(TMP, "s6")])
sa = os.path.join(TMP, "s6", "a", "__snapshots__", "alpha.snap")
sb = os.path.join(TMP, "s6", "b", "__snapshots__", "beta.snap")
ra = load_store(sa) if os.path.isfile(sa) else {}
rb = load_store(sb) if os.path.isfile(sb) else {}
check(r.returncode == 0 and os.path.isfile(sa) and os.path.isfile(sb)
      and ra.get((b"test_bound#inside", b"range-line")) == b"5 in [0, 10]"
      and rb.get((b"test_bound#inside", b"range-line"))
      == b"5 between [0, 10]",
      "parallel -u writes each file's own row store; values never "
      "cross files")

r = run_cli([fa])
check(r.returncode == 0 and "11 pass, 0 fail, 1 skip" in r.stdout,
      "exit codes: 0 when every row passes (a table-level skip is "
      "not a failure)")

jr = run_cli(["--junit", os.path.join(TMP, "s6", "j.xml"), fa])
xml = open(os.path.join(TMP, "s6", "j.xml"), encoding="utf-8").read()
check(jr.returncode == 0 and 'name="test_add#positives"' in xml
      and 'name="test_mod#trivial"' in xml and "<skipped" in xml,
      "the JUnit XML carries rows as testcases, skip node included")

# ---------------------------------------------------------------------------
print("Section 7 — the differential: TestCase[T] goes through the "
      "native compiler")
# ---------------------------------------------------------------------------

if HLC_BIN and shutil.which("gcc"):
    fresh_dir = os.path.join(TMP, "s7")
    os.makedirs(fresh_dir, exist_ok=True)
    c = os.path.join(fresh_dir, "fixture.c")
    e = os.path.join(fresh_dir, "native")
    b = subprocess.run([HLC_BIN, FIXTURE, c], capture_output=True,
                       text=True)
    g = subprocess.run(["gcc", "-O2", "-o", e, c, "-lm", "-pthread"],
                       capture_output=True, text=True)
    if b.returncode == 0 and g.returncode == 0:
        rn = subprocess.run([e], capture_output=True)
        ri = subprocess.run(BOOT + [FIXTURE], capture_output=True)
        check(rn.returncode == 0 and rn.stdout == ri.stdout,
              "the fixture's output is byte-identical: interpreter vs "
              "native (the generic TestCase rows and the runner-mirror "
              "main compile and agree)")
        stored = load_store(COMMITTED_STORE)
        by_value = sorted(v for (_k, v) in stored.items())
        nrecs, nerr = hltest.parse_snapshot_records(rn.stdout)
        check(nerr is None
              and sorted(v for (_n, v) in nrecs) == by_value,
              "the native run's snapshot records equal the committed "
              "store's values (one store serves both backends)")
    else:
        bad("the native build of the fixture failed: %s / %s"
            % (b.stderr.strip()[:60], g.stderr.strip()[:60]))
else:
    bad("the native half of the differential could not run "
        "(no gcc or no bin/hlc)")

fresh = write("s7/fresh.hls", open(FIXTURE, "rb").read())
r = run_cli(["-u", fresh])
committed = open(COMMITTED_STORE, "rb").read()
recorded = open(os.path.join(TMP, "s7", "__snapshots__", "fresh.snap"),
                "rb").read()
check(r.returncode == 0 and recorded == committed,
      "-u on a fresh copy reproduces the committed store byte for "
      "byte (deterministic serialization, row keys sorted)")

# ---------------------------------------------------------------------------
print("Section 8 — the fixed point and tree hygiene")
# ---------------------------------------------------------------------------

r = run_cli([FIXTURE])
check(r.returncode == 0 and "11 pass, 0 fail, 1 skip" in r.stdout,
      "the committed store verifies the committed fixture green "
      "in-tree")

before = open(COMMITTED_STORE, "rb").read()
r = run_cli(["-u", FIXTURE])
after = open(COMMITTED_STORE, "rb").read()
leftovers = [p for p in os.listdir(os.path.dirname(COMMITTED_STORE))
             if p.endswith(".tmp")]
check(r.returncode == 0 and before == after
      and "store(s) updated" not in r.stdout and not leftovers,
      "-u on the committed fixture is a no-op (bytes, report, no "
      ".tmp)")

fr = subprocess.run([sys.executable, "tools/hlfmt.py", "-c", FIXTURE],
                    capture_output=True, text=True)
check(fr.returncode == 0 and "already formatted" in fr.stdout,
      "the fixture is an hlfmt fixed point")

rc = subprocess.run(BOOT + ["--check", FIXTURE], capture_output=True,
                    text=True)
check(rc.returncode == 0 and "OK" in rc.stdout,
      "the fixture type-checks as a plain program (boot --check)")

check(store_path_for("/x/y/feat.hls") == "/x/y/__snapshots__/feat.snap",
      "store_path_for: one store per file, next to it")

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
if FAIL == 0:
    print("=== Stage 120 acceptance: %d PASS / %d FAIL ===" % (PASS, FAIL))
    sys.exit(0)
print("=== Stage 120 acceptance: %d PASS / %d FAIL ===" % (PASS, FAIL))
sys.exit(1)
