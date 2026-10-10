#!/usr/bin/env python3
"""hltest — the Stage 18 test runner for Halis (HLS).

Usage:
  python3 tools/hltest.py [OPTIONS] <file.hls>...
  python3 tools/hltest.py --dir tests/ok            # discover every .hls
  python3 tools/hltest.py --dir tests/ok --grep map # filter by substring
  python3 tools/hltest.py -j 8 file_a.hls file_b.hls # run 8 in parallel
  python3 tools/hltest.py --junit out.xml file.hls  # CI XML report
  python3 tools/hltest.py -u file.hls              # record snapshots
  python3 tools/hltest.py --grep "#zero-left" f.hls # run one table row

A "test" is any top-level function in the program whose name begins with
`test_`. Each test is executed by the Stage-0 interpreter in the SAME
process (one Interp per test, sharing the type-checked program) so the
type-checker is run once per file, not once per test. Tests are run in
PARALLEL across files (a process pool sized by `-j`).

A test PASSES when it returns normally (exit 0). A test FAILS when it
panics (HLPanic) — the panic message is the failure detail. A test is
SKIP'd when the panic message starts with the reserved prefix
`__HLTEST_SKIP__:` (set by the `std.test.test_skip` helper).

Assertion helpers (assert_eq, assert_ne, assert_true, assert_false,
assert_int_range, assert_len) are in `std/test.hls`; tests import them
with `import "std.test"`.

Snapshot testing (Stage 119, v0.138.0-alpha): a test calls
`std.test.assert_snapshot(name, value)`; the assertion writes a
length-framed record to stdout (`__SNAP__ <name> <len>\n<value>\n`),
the runner parses the records out of the test's captured stdout, and
each is compared against the SNAPSHOT STORE — the file
`__snapshots__/<file>.snap` next to the test file. Verify mode (the
default) fails on new, changed, or obsolete snapshots (the failure
carries a unified diff and the --update-snapshots hint); `-u` records
new and changed values, prunes obsolete ones, and never rewrites a
store whose bytes did not change. Corrupt stores are refused, never
silently regenerated. See the Stage 119 SPEC section for the wire and
store formats.

Parameterised (table-driven) testing (Stage 120, v0.139.0-alpha): a
test_ fn that takes EXACTLY ONE parameter is a table-driven test; its
cases come from a companion `fn cases_<name>() -> list[TestCase[R]]`
(std.test.TestCase — `{ name: str, row: T }`) in the same file. The
runner executes the table, validates every row (shape, name charset,
uniqueness), then runs the test ONCE PER ROW in a fresh Interp with
the row's `row` value as the sole argument. Each row is reported as
its own test named `test_<name>#<case-name>` — its own PASS/FAIL/SKIP,
its own timing, its own JUnit testcase, its own snapshot-store key.
Table-level problems (missing or mismatched table, a table that
panics, a non-list or malformed row, a duplicate or invalid case
name, a statically-checkable row/test type mismatch) fail the test
UNDER ITS BASE NAME and no row runs. An empty table is a SKIP, not a
silent pass. See the Stage 120 SPEC section for the full contract.

Property-based testing: see `std/quickcheck.hls` for `for_all_<type>`
helpers and `tools/hls-fuzz.py` for the AST-level differential fuzzer.

Coverage: see `tools/hlcov.py` for HLIR-level branch/edge coverage.

Exit code:
  0  all tests pass (or skip)
  1  one or more tests fail
  2  usage / IO error
"""
import argparse
import difflib
import io
import multiprocessing
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
import xml.sax.saxutils as saxutils

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boot.boot import load_program           # noqa: E402
from boot.checker import check               # noqa: E402
from boot.interp import Interp, HLPanic      # noqa: E402
from boot.lexer import HLError               # noqa: E402

# Reserved panic prefix used by std.test.mark_skip — hltest recognises
# this and reports the test as SKIP instead of FAIL.
_SKIP_PREFIX = "__HLTEST_SKIP__:"
_SKIP_PREFIX_B = _SKIP_PREFIX.encode("ascii")

# ---------------------------------------------------------------------------
# Stage 119 (v0.138.0-alpha): snapshot testing — assert_snapshot records.
# ---------------------------------------------------------------------------

# The reserved stdout prefix std.test.assert_snapshot writes. Framing:
#   __SNAP__ <name> <byte-len>\n<value bytes>\n
# The value is length-prefixed so ANY content is safe inside it (embedded
# marker-lookalike lines included); the trailing \n is the record
# separator, so a record never glues onto following output. The parser
# searches for the prefix anywhere OUTSIDE consumed values — a marker
# buried mid-line by preceding print() noise still parses, and a stray
# `__SNAP__ ` in diagnostic output fails loudly as a protocol error
# rather than losing a record silently.
_SNAP_PREFIX = b"__SNAP__ "

# Snapshot-store constants. The store is a generated artifact OWNED by
# `hltest -u`; both header lines are fixed bytes so the file stays
# position-independent (no absolute paths -> byte-identical across
# machines and clones, which is what makes committed stores diffable).
_SNAP_MAGIC = b"# hltest-snapshots v1"
_SNAP_HOWTO = (b"# generated by `hltest --update-snapshots` - "
               b"do not edit by hand")

# Snapshot names are 1..96 bytes of [A-Za-z0-9_.:-]. No spaces or
# newlines: the record header is space-separated, so a name that could
# contain the framing bytes would make the format ambiguous.
_SNAP_NAME_RE = re.compile(rb"^[A-Za-z0-9_.:\-]{1,96}$")

# Stage 120: the TEST field of a store record widens additively from
# `<test>` to `<test>[#<case>]` — a parameterised test's records are
# keyed per row (`test_parse#empty`), so one store continues to serve
# plain and table tests alike. `#` cannot appear in an HLS fn name, so
# the row suffix is unambiguous, and stores written before Stage 120
# verify byte-for-byte unchanged (the grammar only gained a form, it
# did not change any existing one). Snapshot NAMES keep the Stage 119
# charset exactly.
_STORE_TEST_RE = re.compile(
    rb"^[A-Za-z0-9_.:\-]{1,96}(#[A-Za-z0-9_.:\-]{1,96})?$")


class SnapshotStoreError(Exception):
    """A snapshot store exists but violates the format. Corrupt stores
    are reported, never silently regenerated (git checkout is the
    remedy — stores are committed generated artifacts)."""


def store_path_for(filepath):
    """The store path for a test file: `__snapshots__/<stem>.snap` in
    the file's own directory (one store per file -> the process pool
    can never have two workers contending for one store)."""
    d = os.path.dirname(filepath)
    stem = os.path.basename(filepath)
    if stem.endswith(".hls"):
        stem = stem[: -len(".hls")]
    return os.path.join(d, "__snapshots__", stem + ".snap")


def _parse_header_rest(rest):
    """Split the bytes after `__SNAP__ ` into (name, length) or return a
    human-readable protocol error. Shared by the stream parser and the
    store parser (the record header grammar is identical)."""
    sp = rest.find(b" ")
    if sp < 0:
        return None, None, "record header is missing its length field"
    name, lenb = rest[:sp], rest[sp + 1:]
    if not _SNAP_NAME_RE.match(name):
        return (None, None,
                "invalid snapshot name %r (names are 1-96 bytes of "
                "[A-Za-z0-9_.:-])" % name)
    if not lenb.isdigit() or (len(lenb) > 1 and lenb[0:1] == b"0"):
        return None, None, "malformed record length %r" % lenb
    return name, int(lenb), None


def parse_snapshot_records(stdout):
    """Parse the `__SNAP__` record stream from one test's captured
    stdout. Returns (records, error): records is the ordered list of
    (name: bytes, value: bytes); error is a protocol error string or
    None. Non-record output between records is skipped verbatim; the
    scan resumes AFTER each consumed value, so value content is never
    re-scanned."""
    records = []
    out = stdout
    n = len(out)
    i = 0
    while i < n:
        k = out.find(_SNAP_PREFIX, i)
        if k < 0:
            break
        j = out.find(b"\n", k)
        if j < 0:
            return records, "snapshot protocol error: unterminated record header at byte %d" % k
        name, ln, err = _parse_header_rest(out[k + len(_SNAP_PREFIX):j])
        if err is not None:
            return records, "snapshot protocol error: %s" % err
        vstart = j + 1
        vend = vstart + ln
        if vend > n:
            return records, ("snapshot protocol error: record '%s' declares "
                             "%d bytes but only %d remain"
                             % (name.decode("ascii", "replace"), ln, n - vstart))
        if out[vend:vend + 1] != b"\n":
            return records, ("snapshot protocol error: record '%s' is missing "
                             "its frame separator (the %d value bytes must be "
                             "followed by a newline)"
                             % (name.decode("ascii", "replace"), ln))
        records.append((name, out[vstart:vend]))
        i = vend + 1
    return records, None


def serialize_store(records):
    """Serialize a snapshot store: {(test: bytes, name: bytes): value} ->
    deterministic file bytes. Records are sorted by (test, name) so the
    file is a byte-stable function of its record set — refactors and
    reruns do not churn it, and `git diff` shows only real value
    changes. The header is the two fixed lines; every record is
    `@@@ <test> <name> <len>\\n<value>\\n` (the record grammar mirrors
    the stream grammar exactly)."""
    out = bytearray()
    out += _SNAP_MAGIC + b"\n"
    out += _SNAP_HOWTO + b"\n"
    for key in sorted(records.keys()):
        tname, sname = key
        v = records[key]
        out += b"@@@ " + tname + b" " + sname + b" " \
            + str(len(v)).encode("ascii") + b"\n"
        out += v
        out += b"\n"
    return bytes(out)


def load_store(path):
    """Parse a snapshot store into {(test: bytes, name: bytes): value}.
    A missing file is an empty store ({}). Any framing deviation raises
    SnapshotStoreError naming the file and the byte offset — the store
    is a generated artifact, so anything that is not exactly what the
    writer would have produced is corruption, not a variant to tolerate."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    n = len(raw)
    magic_ln = _SNAP_MAGIC + b"\n"
    if not raw.startswith(magic_ln):
        raise SnapshotStoreError(
            "%s is not a hltest snapshot store (missing v1 magic line)" % path)
    i = len(magic_ln)
    j = raw.find(b"\n", i)
    if j < 0 or raw[i:j] != _SNAP_HOWTO:
        raise SnapshotStoreError(
            "%s has a non-canonical store header (line 2 must be the "
            "generator note; regenerate with --update-snapshots or "
            "git-checkout the file)" % path)
    i = j + 1
    recs = {}
    while i < n:
        j = raw.find(b"\n", i)
        if j < 0:
            raise SnapshotStoreError(
                "%s: truncated record header at byte %d" % (path, i))
        line = raw[i:j]
        if not line.startswith(b"@@@ "):
            raise SnapshotStoreError(
                "%s: expected a record at byte %d, found %r"
                % (path, i, line[:32]))
        parts = line[4:].split(b" ")
        if len(parts) != 3:
            raise SnapshotStoreError(
                "%s: malformed record header %r (want '@@@ <test> <name> "
                "<len>')" % (path, line[:48]))
        tname, sname, lenb = parts
        if not _STORE_TEST_RE.match(tname) or not _SNAP_NAME_RE.match(sname):
            raise SnapshotStoreError(
                "%s: invalid test or snapshot name in record header %r"
                % (path, line[:48]))
        if not lenb.isdigit() or (len(lenb) > 1 and lenb[0:1] == b"0"):
            raise SnapshotStoreError(
                "%s: malformed record length %r at byte %d"
                % (path, lenb, i))
        ln = int(lenb)
        vstart = j + 1
        vend = vstart + ln
        if vend > n - 1:
            raise SnapshotStoreError(
                "%s: record value at byte %d runs past EOF (declares %d "
                "bytes, %d remain, no room for the frame separator)"
                % (path, vstart, ln, n - vstart))
        if (tname, sname) in recs:
            raise SnapshotStoreError(
                "%s: duplicate record for '%s' / '%s' at byte %d"
                % (path, tname.decode("ascii", "replace"),
                   sname.decode("ascii", "replace"), i))
        recs[(tname, sname)] = raw[vstart:vend]
        if raw[vend:vend + 1] != b"\n":
            raise SnapshotStoreError(
                "%s: record at byte %d is missing its frame separator "
                "(value bytes must be followed by a newline)" % (path, i))
        i = vend + 1
    return recs


def _snap_diff(sname, stored, got):
    """A human-readable unified diff between the stored and the received
    snapshot value. Diffed line-wise for readability (decode errors are
    replaced), with the byte lengths alongside — the comparison itself
    is always byte-exact; the diff is only the report. Capped at 60
    diff lines per record so a huge value cannot bury the report."""
    a = stored.decode("utf-8", "replace").splitlines()
    b = got.decode("utf-8", "replace").splitlines()
    dl = list(difflib.unified_diff(
        a, b, fromfile="stored", tofile="received", lineterm="", n=2))
    if len(dl) > 60:
        dl = dl[:60] + ["... (%d more diff lines)"
                        % (len(dl) - 60)]
    return ("snapshot '%s' differs from the stored value "
            "(stored %d bytes, received %d bytes):\n%s"
            % (sname.decode("ascii", "replace"), len(stored), len(got),
               "\n".join(dl)))


# ---------------------------------------------------------------------------
# Stage 120 (v0.139.0-alpha): parameterised (table-driven) tests.
# ---------------------------------------------------------------------------

def row_base(test_key):
    """The base fn name of a store test key: `test_parse#empty` ->
    `test_parse`. `#` cannot appear in an HLS identifier, so the first
    `#` (if any) always starts the row suffix; a plain test's key is
    its own base."""
    return test_key.split(b"#", 1)[0]


def _norm_type(t):
    """A comparison form of a declared type string: all whitespace
    removed. Type grammar has no free-form text inside it, so this is
    a lossless normalisation for equality checking only."""
    return re.sub(r"\s+", "", t) if isinstance(t, str) else t


def _table_row_type(ret):
    """The declared row payload type of a case table: parse the return
    annotation `list[TestCase[X]]` and return X (whitespace-normalised),
    or None when the shape is not recognisably a TestCase table. A None
    here DISABLES the static cross-check (the runtime row validation is
    the backstop) — never a diagnostic on its own: the parser must not
    invent errors about types it does not fully understand (type vars,
    module-qualified names, future syntax)."""
    if not isinstance(ret, str) or "[" not in ret:
        return None
    from boot.checking.helpers import type_base, type_args  # noqa: E402
    if type_base(ret) != "list":
        return None
    args = type_args(ret)
    if len(args) != 1:
        return None
    elem = args[0]
    if type_base(elem) != "TestCase":
        return None
    targs = type_args(elem)
    if len(targs) != 1:
        return None
    return _norm_type(targs[0])


# Functions in the standard library whose names start with `test_` are
# helpers (like `mark_skip`), NOT tests. They live in `std/test.hls` and
# are imported into user test files; the user's own test_* functions are
# the real tests. We exclude any function whose SOURCE FILE is in the
# std/ directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STD_DIR = os.path.join(_REPO_ROOT, "std")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_files(paths, recurse):
    """Expand a list of file/directory paths into a deduplicated .hls list."""
    out = []
    seen = set()
    for p in paths:
        if os.path.isdir(p):
            if recurse:
                for root, _dirs, files in os.walk(p):
                    for f in sorted(files):
                        if f.endswith(".hls"):
                            absf = os.path.abspath(os.path.join(root, f))
                            if absf not in seen:
                                seen.add(absf)
                                out.append(absf)
            else:
                for f in sorted(os.listdir(p)):
                    if f.endswith(".hls"):
                        absf = os.path.abspath(os.path.join(p, f))
                        if absf not in seen:
                            seen.add(absf)
                            out.append(absf)
        elif p.endswith(".hls") and os.path.isfile(p):
            absf = os.path.abspath(p)
            if absf not in seen:
                seen.add(absf)
                out.append(absf)
        else:
            sys.stderr.write("hltest: skip (not .hls or not found): %s\n" % p)
    return out


def list_tests_in_file(filepath):
    """Parse JUST the entry file (not its imports) and return the
    top-level fns the runner drives, in source order: (tests, tables) —
    tests is every fn whose name starts with `test_`, tables every fn
    whose name starts with `cases_` (the Stage 120 case-table
    convention; `cases_` is a RESERVED prefix for parameterised tests
    in the entry file — a stray table with no matching test is a
    file-level failure, not dead code). We parse the file in isolation
    so we only see the USER's functions, not the stdlib helpers (which
    are merged in by load_program)."""
    with open(filepath, "rb") as f:
        src = f.read()
    from boot.lexer import tokenize              # noqa: E402
    from boot.parser import Parser               # noqa: E402
    toks = tokenize(src)
    prog = Parser(toks).parse_program()
    tests = [name for name in prog["fns"].keys() if name.startswith("test_")]
    tables = [name for name in prog["fns"].keys() if name.startswith("cases_")]
    return tests, tables


# ---------------------------------------------------------------------------
# Per-file test execution (runs in the main process — type-check once,
# then run each test in its own Interp so they cannot leak state).
# ---------------------------------------------------------------------------

class TestResult:
    __slots__ = ("file", "name", "status", "detail", "ms")

    def __init__(self, file, name, status, detail, ms):
        self.file = file
        self.name = name
        self.status = status      # "pass" / "fail" / "skip"
        self.detail = detail
        self.ms = ms


def _panic_text(msg):
    """A panic message as display text. Stage-0 str panics arrive as
    bytes (the runtime value type); decode instead of leaking the
    Python repr (b'...') into a failure report."""
    if isinstance(msg, (bytes, bytearray)):
        return bytes(msg).decode("utf-8", "replace")
    return str(msg)


def _value_kind(v):
    """A human-readable kind of an interpreter value, for diagnostics:
    'a str', 'an int', 'a Direction.South enum value', 'a struct value
    {a, b}'. Struct values do not carry their type name at runtime, so
    the struct form is described by its field names."""
    if isinstance(v, bytes):
        return "a str"
    if isinstance(v, bool):
        return "a bool"
    if isinstance(v, int):
        return "an int"
    if isinstance(v, float):
        return "a float"
    if isinstance(v, list):
        return "a list"
    if isinstance(v, dict):
        if "enum" in v and "var" in v:
            return "a %s.%s enum value" % (v.get("enum"), v.get("var"))
        if "name" in v and "row" in v:
            return "a TestCase value"
        keys = ", ".join(str(k) for k in list(v.keys())[:4])
        return "a struct value {%s}" % keys
    if v is None:
        return "void"
    return type(v).__name__


def _invoke(program, filepath, tname, args, tkey, snap_store, snap_err,
            update_snaps, produced, passed_keys, table_setup=None):
    """Execute ONE test invocation and do its Stage 119 snapshot
    bookkeeping. A plain test calls this with args=[] and no setup; one
    ROW of a Stage 120 parameterised test calls it with the row's store
    key (tkey = `test_x#case`) and a table_setup that re-runs the case
    table INSIDE this invocation's fresh Interp and returns the argument
    list — so the row value the test consumes is born in the same
    process state that consumes it, and no value ever crosses Interps.

    Returns (status, detail, ms). produced and passed_keys are mutated
    in place for the update-mode commit, and ONLY on a pass: a test
    that panicked or exited non-zero may have emitted partial records,
    and pinning those would freeze garbage as the expected output."""
    t0 = time.perf_counter()
    buf = io.BytesIO()
    try:
        interp = Interp(program, [filepath.encode("utf-8")], buf,
                        contracts=False)
        if table_setup is not None:
            args = table_setup(interp)
        interp.call_fn(tname, args)
        # If the test returned a value, ignore it (tests are `void`).
        status = "pass"
        detail = ""
    except HLPanic as ex:
        # Stage 120 fix (a Stage 18 latent bug): a panic message is the
        # VALUE passed to panic() — at the Stage-0 runtime a str panic
        # arrives as bytes, so str(ex) (repr) never matched the SKIP
        # prefix and every mark_skip test was reported FAIL with a
        # b'...' payload. Classify on the raw message, bytes or str.
        msg = ex.msg
        if isinstance(msg, (bytes, bytearray)):
            raw = bytes(msg)
        else:
            raw = str(msg).encode("utf-8", "replace")
        if raw.startswith(_SKIP_PREFIX_B):
            status = "skip"
            detail = raw[len(_SKIP_PREFIX_B):].decode(
                "utf-8", "replace").strip()
        else:
            status = "fail"
            detail = raw.decode("utf-8", "replace")
    except SystemExit as ex:
        # exit(n) inside a test — treat 0 as pass, anything else as
        # a failure with the exit code as the detail.
        code = ex.code if isinstance(ex.code, int) else 0
        status = "pass" if code == 0 else "fail"
        detail = "exit(%d)" % code if code else ""
    except Exception as ex:  # noqa: BLE001 — interpreter bug
        status = "fail"
        detail = "internal error: %r\n%s" % (
            ex, traceback.format_exc(limit=4))
    ms = (time.perf_counter() - t0) * 1000.0

    # ---- Stage 119: snapshot bookkeeping. Only a PASSING test's
    # records count (see the docstring); the logic is identical for
    # plain tests and rows — the tkey carries the difference.
    if status == "pass":
        records, perr = parse_snapshot_records(buf.getvalue())
        if perr is not None:
            status = "fail"
            detail = perr
        else:
            seen_names = set()
            for (nm, _v) in records:
                if nm in seen_names:
                    status = "fail"
                    detail = ("duplicate snapshot name '%s' in one "
                              "test - names must be unique within a "
                              "test (duplicates mean the two records "
                              "race for one store slot)"
                              % nm.decode("ascii", "replace"))
                    break
                seen_names.add(nm)
        if status == "pass":
            old = ({nm: v for ((t, nm), v) in snap_store.items()
                    if t == tkey} if snap_err is None else None)
            problems = []
            ann = []
            ann_new = ann_ch = 0
            for (nm, v) in records:
                k = (tkey, nm)
                disp = nm.decode("ascii", "replace")
                if update_snaps:
                    if snap_err is not None:
                        problems.append(
                            "snapshot '%s' not recorded: the store "
                            "is corrupt (see the <snapshots> entry)"
                            % disp)
                    else:
                        if k not in snap_store:
                            ann_new += 1
                        elif snap_store[k] != v:
                            ann_ch += 1
                        produced[k] = v
                else:
                    if snap_err is not None:
                        problems.append(
                            "snapshot '%s' not verified: the store "
                            "is corrupt (see the <snapshots> entry)"
                            % disp)
                    elif k not in snap_store:
                        problems.append(
                            "new snapshot '%s' - no stored value "
                            "(record it: run hltest "
                            "--update-snapshots)" % disp)
                    elif snap_store[k] != v:
                        problems.append(
                            _snap_diff(nm, snap_store[k], v)
                            + "\n(run hltest --update-snapshots to "
                              "accept the new value)")
            if not update_snaps and old is not None:
                # verify mode: stored names this invocation no longer
                # produces are obsolete and fail it.
                have = seen_names
                for nm in sorted(nm for nm in old if nm not in have):
                    problems.append(
                        "obsolete snapshot '%s' - the test no "
                        "longer produces it (prune: run hltest "
                        "--update-snapshots)"
                        % nm.decode("ascii", "replace"))
            if problems:
                status = "fail"
                detail = "\n".join(problems)
            elif update_snaps:
                # per-test annotation for the report line
                pruned_here = (0 if old is None
                               else sum(1 for nm in old
                                        if (tkey, nm) not in produced))
                if ann_new:
                    ann.append("+%d new" % ann_new)
                if ann_ch:
                    ann.append("~%d changed" % ann_ch)
                if pruned_here:
                    ann.append("-%d pruned" % pruned_here)
                if ann:
                    detail = "snapshots: " + ", ".join(ann)
            if status == "pass":
                passed_keys.add(tkey)
    return status, detail, ms


def _run_table(program, filepath, tname, grep, snap_store, snap_err,
               update_snaps, produced, passed_keys, dead_keys, results):
    """Stage 120 (v0.139.0-alpha): run one parameterised (table-driven)
    test — every SELECTED row of it, plus its table-level diagnostics.

    Selection: --grep matches the base fn name (the whole table runs)
    or a full row name (`test_x#case`). A base-unmatched fn still has
    its table executed — selection needs the row universe — but a
    table-level problem in an unselected fn is SUPPRESSED: --grep is a
    selection tool, and a filtered fn's broken table must not fail a
    targeted run (the full, unfiltered run reports it).

    The table is the store's source of truth for the row universe
    regardless of selection: when it parses, rows the table no longer
    contains are dead — pruned by -u, and reported in verify mode (a
    table-level failure under the base name). Rows that exist but were
    not selected keep their records everywhere.

    Each selected row runs in its OWN fresh Interp, the table re-runs
    inside it, and the row's `row` value becomes the test fn's sole
    argument. Results are appended under the full row name
    `test_x#case`; the update-mode merge inputs (produced, passed_keys)
    and the dead-key set are fed in place."""
    base = tname.encode("utf-8")
    base_matched = grep is None or grep in tname
    cases_fn = "cases_" + tname[len("test_"):]
    fn_def = program["fns"][tname]
    param_type = fn_def["params"][0][1]

    def fail_base(detail):
        if base_matched:
            results.append(TestResult(filepath, tname, "fail", detail, 0.0))

    # ---- the table must exist beside the test, take no parameters,
    # and (when both declared types are readable) promise exactly the
    # row type the test takes. Static checks first: they fail before
    # any row runs, with the fix in the message.
    if cases_fn not in program["fns"]:
        fail_base(
            "parameterised test '%s' has no case table: add "
            "`fn %s() -> list[std.test.TestCase[...]]` beside it (the "
            "convention is test_<name> <-> cases_<name>, same file; "
            "each row is TestCase { name, row })" % (tname, cases_fn))
        return
    cases_def = program["fns"][cases_fn]
    if cases_def.get("extern", False):
        fail_base("case table '%s' is extern - the table is HLS code "
                  "the runner executes, not a declaration" % cases_fn)
        return
    if cases_def["params"]:
        fail_base("case table '%s' must take no parameters (the runner "
                  "calls it with none), got %d"
                  % (cases_fn, len(cases_def["params"])))
        return
    if not cases_def.get("typeparams") and not fn_def.get("typeparams"):
        row_t = _table_row_type(cases_def.get("ret"))
        if row_t is not None and row_t != _norm_type(param_type):
            fail_base(
                "case table '%s' returns rows of type '%s' but '%s' "
                "takes '%s' - the TestCase row payload type and the "
                "test's parameter type must be the same type"
                % (cases_fn, row_t, tname, _norm_type(param_type)))
            return

    # ---- execute the table once (in a throwaway Interp; a table is
    # data, its stdout is not test output and is discarded).
    sink = io.BytesIO()
    try:
        interp = Interp(program, [filepath.encode("utf-8")], sink,
                        contracts=False)
        table = interp.call_fn(cases_fn, [])
    except HLPanic as ex:
        fail_base("case table '%s' panicked: %s"
                  % (cases_fn, _panic_text(ex.msg)))
        return
    except SystemExit as ex:
        code = ex.code if isinstance(ex.code, int) else 0
        fail_base("case table '%s' called exit(%d)" % (cases_fn, code))
        return
    except Exception as ex:  # noqa: BLE001 — interpreter bug
        fail_base("internal error in case table '%s': %r\n%s"
                  % (cases_fn, ex, traceback.format_exc(limit=4)))
        return

    # ---- validate the row shape: a list of TestCase { name, row }
    # values, names in the snapshot-name charset, names unique. The
    # first bad row fails the TABLE (no row runs) — a malformed table
    # is a bug in the test file, not a failure of any particular case.
    if not isinstance(table, list):
        fail_base("case table '%s' returned %s, expected a "
                  "list[std.test.TestCase[...]]"
                  % (cases_fn, _value_kind(table)))
        return
    rows = []                       # (name bytes, index, full name)
    seen = set()
    for idx, item in enumerate(table):
        if not isinstance(item, dict) or set(item.keys()) != {"name", "row"}:
            fail_base("row %d of case table '%s' is not a TestCase "
                      "{ name, row } value (got %s)"
                      % (idx, cases_fn, _value_kind(item)))
            return
        nm = item["name"]
        if not isinstance(nm, (bytes, bytearray)):
            fail_base("row %d of case table '%s' has a non-str name "
                      "(got %s)" % (idx, cases_fn, _value_kind(nm)))
            return
        nm = bytes(nm)
        if not _SNAP_NAME_RE.match(nm):
            fail_base("invalid case name %r in '%s' - case names are "
                      "1..96 bytes of [A-Za-z0-9_.:-] (the same charset "
                      "as snapshot names: a row's report line is also "
                      "its snapshot-store test key)"
                      % (nm.decode("utf-8", "replace"), cases_fn))
            return
        if nm in seen:
            fail_base("duplicate case name '%s' in '%s' - two rows "
                      "would race for one report line and one "
                      "snapshot-store slot"
                      % (nm.decode("ascii", "replace"), cases_fn))
            return
        seen.add(nm)
        rows.append((nm, idx, "%s#%s"
                     % (tname, nm.decode("utf-8", "replace"))))

    # ---- the row universe vs the store: rows the table dropped are
    # dead keys (-u prunes them) and, in verify mode, a table-level
    # failure. Unselected-but-alive rows are never touched.
    universe = {base + b"#" + nm for (nm, _i, _f) in rows}
    if snap_err is None:
        for k in snap_store:
            if b"#" in k[0] and row_base(k[0]) == base \
                    and k[0] not in universe:
                dead_keys.add(k[0])
    obsolete = []
    if snap_err is None and not update_snaps and base_matched:
        obsolete = sorted(k[0] for k in snap_store
                          if b"#" in k[0] and row_base(k[0]) == base
                          and k[0] not in universe)

    if not rows:
        if obsolete:
            fail_base(
                "stored snapshot records for case row(s) the table no "
                "longer contains: %s (prune them: run hltest "
                "--update-snapshots)"
                % ", ".join(t.decode("ascii", "replace") for t in obsolete))
        elif base_matched:
            results.append(TestResult(
                filepath, tname, "skip",
                "empty case table '%s' (0 rows)" % cases_fn, 0.0))
        return
    if obsolete:
        results.append(TestResult(
            filepath, tname, "fail",
            "stored snapshot records for case row(s) the table no "
            "longer contains: %s (prune them: run hltest "
            "--update-snapshots)"
            % ", ".join(t.decode("ascii", "replace") for t in obsolete),
            0.0))

    # ---- run the selected rows, each in its own fresh Interp with the
    # table re-executed inside it (same-interp row values; the runner
    # re-checks the positional shape in case the table is
    # nondeterministic across runs — that failure names the table).
    def make_setup(idx):
        def setup(ip):
            tab = ip.call_fn(cases_fn, [])
            if (not isinstance(tab, list) or idx >= len(tab)
                    or not isinstance(tab[idx], dict)
                    or "row" not in tab[idx]):
                raise HLPanic(
                    "case table '%s' did not yield row %d on re-run - "
                    "a case table must be deterministic" % (cases_fn, idx),
                    0)
            return [tab[idx]["row"]]
        return setup

    for (nm, idx, full_name) in rows:
        if grep is not None and not base_matched and grep not in full_name:
            continue
        status, detail, ms = _invoke(
            program, filepath, tname, [], base + b"#" + nm, snap_store,
            snap_err, update_snaps, produced, passed_keys,
            table_setup=make_setup(idx))
        results.append(TestResult(filepath, full_name, status, detail, ms))


def run_file(filepath, grep=None, update_snaps=False):
    """Type-check one file and run every `test_*` fn in it. Returns a
    (results, stats) pair: results is a list of TestResult (one per
    test — or per ROW of a Stage 120 parameterised test, under its
    `test_x#case` name — plus synthetic `<load>`/`<check>`/`<snapshots>`
    entries for file-level problems); stats carries the Stage 119 store
    bookkeeping (path, written/removed, new/changed/pruned counts) for
    the update-mode summary. On a load/check error, returns a single
    FAIL result for a synthetic test named `<load>` — and leaves any
    snapshot store untouched: a file that does not run cannot tell us
    what its snapshots should say."""
    results = []
    stats = {"store": None, "written": False, "removed": False,
             "new": 0, "changed": 0, "pruned": 0}
    try:
        program = load_program(filepath)
    except HLError as ex:
        results.append(TestResult(filepath, "<load>", "fail",
                                  "compile error: %s" % ex, 0.0))
        return results, stats
    except OSError as ex:
        results.append(TestResult(filepath, "<load>", "fail",
                                  "io: %s" % ex, 0.0))
        return results, stats
    try:
        # Validate (the call's side effect): the result object itself is
        # not needed here.
        check(program)
    except HLError as ex:
        # Deep-scan-23 fix: a MODULE (no `fn main`) is not a standalone
        # program — the checker legitimately rejects it with "missing
        # main function", but that is not a test failure. Recursive
        # discovery (--dir) walks into helper subdirectories such as
        # tests/ok/modules/ (imported by feat_import.hls), which the
        # non-recursive run_tests.sh glob never touches. Record a SKIP
        # for module-shaped files instead of a FAIL; any OTHER type
        # error in the module still fails the run.
        if "missing main function" in str(ex):
            results.append(TestResult(filepath, "<module>", "skip",
                                      "module without fn main", 0.0))
            return results, stats
        results.append(TestResult(filepath, "<check>", "fail",
                                  "type error: %s" % ex, 0.0))
        return results, stats

    # ---- Stage 119: resolve the snapshot store (one stat for the
    # corpus without snapshots; a parse only when a store exists).
    snap_path = store_path_for(filepath)
    store_existed = os.path.exists(snap_path)
    snap_store = {}
    snap_err = None
    if store_existed:
        try:
            snap_store = load_store(snap_path)
        except SnapshotStoreError as ex:
            snap_err = str(ex)
        except OSError as ex:
            snap_err = "%s (unreadable: %s)" % (snap_path, ex)
    if snap_err is not None:
        # A corrupt store is NEVER silently regenerated (with --grep
        # active, regeneration would drop the filtered tests' records).
        # The tests still run; every snapshot-producing test fails with
        # the pointer below, so the corruption cannot hide.
        results.append(TestResult(
            filepath, "<snapshots>", "fail",
            "snapshot store corrupt: %s - the store is a generated "
            "artifact; restore it with `git checkout` (or delete it and "
            "re-record with --update-snapshots, without --grep)" % snap_err,
            0.0))

    tests, entry_tables = list_tests_in_file(filepath)
    all_tests = list(tests)
    file_keys = {t.encode("utf-8") for t in all_tests}
    # Stage 120: fn arity separates plain tests (0 params) from
    # parameterised ones (exactly 1); >=2 is a protocol error reported
    # when the fn is selected by grep (or always, unfiltered).
    arity = {t: len(program["fns"][t]["params"]) for t in all_tests}

    # Stored records for test functions that are not in the file AT ALL
    # (deleted or renamed), or whose Stage 120 SHAPE no longer matches
    # it — plain records left behind after a test became parameterised
    # (its records are keyed per row now), or row records left behind
    # after a test became plain. Both are knowable independently of
    # --grep (all_tests is the pre-filter list), so the check is
    # grep-safe; in update mode the stale keys are pruned.
    stale_keys = set()
    if snap_err is None and snap_store:
        unknown = []
        for t in sorted({k[0] for k in snap_store}):
            rbase = row_base(t)
            if rbase not in file_keys:
                unknown.append(t)
                stale_keys.add(t)
            elif b"#" in t and arity.get(rbase.decode("utf-8"), 0) == 0:
                unknown.append(t)
                stale_keys.add(t)
            elif b"#" not in t and arity.get(rbase.decode("utf-8"), 0) >= 1:
                unknown.append(t)
                stale_keys.add(t)
        if unknown:
            if update_snaps:
                pass  # pruned silently by the commit below (counted there)
            else:
                names = ", ".join(t.decode("ascii", "replace") for t in unknown)
                results.append(TestResult(
                    filepath, "<snapshots>", "fail",
                    "stored snapshots for test function(s) not in the "
                    "file, or whose record shape no longer matches it "
                    "(plain vs parameterised): %s (prune them: run hltest "
                    "--update-snapshots)" % names, 0.0))

    # Stage 120: a case table with no parameterised test is almost
    # always a naming typo (`cases_pares` for `test_parse`) or a test
    # that lost its parameter — both fail loudly. Entry-file tables
    # only: imported tables are other modules' business.
    for cname in entry_tables:
        tmatch = "test_" + cname[len("cases_"):]
        if tmatch in arity and arity[tmatch] == 1:
            continue
        if tmatch in arity:
            results.append(TestResult(
                filepath, cname, "fail",
                "case table '%s' has a matching test '%s', but the test "
                "takes no parameters - a case table drives a "
                "parameterised test, one whose fn takes exactly one row "
                "parameter" % (cname, tmatch), 0.0))
        else:
            results.append(TestResult(
                filepath, cname, "fail",
                "case table '%s' has no matching parameterised test "
                "'%s' (the convention is test_<name> <-> cases_<name>, "
                "same file)" % (cname, tmatch), 0.0))

    if not all_tests:
        # No tests in this file — not a failure, just record a skip.
        results.append(TestResult(filepath, "<no-tests>", "skip",
                                  "no test_* functions", 0.0))
    else:
        produced = {}          # (test-or-row key, name) -> value, PASSED only
        passed_keys = set()    # test names + row keys that passed
        dead_keys = set()      # Stage 120: rows the tables no longer contain
        ran_any = False
        for tname in all_tests:
            ar = arity[tname]
            if ar >= 2:
                # A parameterised test takes EXACTLY one row parameter;
                # more is a convention error, reported only when the fn
                # is selected (--grep is a selection tool).
                if not grep or grep in tname:
                    ran_any = True
                    results.append(TestResult(
                        filepath, tname, "fail",
                        "parameterised test '%s' must take exactly one "
                        "parameter - the row value - but takes %d; put "
                        "the case data in a struct and make the table "
                        "rows TestCase[ThatStruct]" % (tname, ar), 0.0))
                continue
            if ar == 1:
                # Stage 120: parameterised (table-driven) test.
                before = len(results)
                _run_table(program, filepath, tname, grep, snap_store,
                           snap_err, update_snaps, produced, passed_keys,
                           dead_keys, results)
                if len(results) > before:
                    ran_any = True
                continue
            # Plain test: the base-name grep filter of Stage 18.
            if grep and grep not in tname:
                continue
            ran_any = True
            status, detail, ms = _invoke(
                program, filepath, tname, [], tname.encode("utf-8"),
                snap_store, snap_err, update_snaps, produced, passed_keys)
            results.append(TestResult(filepath, tname, status, detail, ms))
        if not ran_any:
            results.append(TestResult(
                filepath, "<no-tests>", "skip",
                "no tests match --grep" if grep else "no test_* functions",
                0.0))

        # ---- Stage 119 + 120: update-mode commit. new_store = kept
        # old records (tests/rows that did not run — filtered by --grep
        # — or ran without passing; a broken run must not destroy data)
        # + this run's produced records. Records whose test fn is gone,
        # whose shape no longer matches the file (plain vs
        # parameterised), or whose table row no longer exists are
        # pruned. Written only when the bytes differ, so a fixed point
        # leaves the file (and its mtime) untouched.
        if update_snaps and snap_err is None:
            kept = {}
            for k, v in snap_store.items():
                t = k[0]
                if row_base(t) not in file_keys:
                    continue        # the fn no longer exists: prune
                if t in stale_keys or t in dead_keys:
                    continue        # Stage 120: shape/row gone: prune
                if t in passed_keys:
                    continue        # replaced by this run's records
                kept[k] = v
            new_store = dict(kept)
            new_store.update(produced)
            stats["store"] = snap_path
            stats["new"] = sum(1 for k in produced if k not in snap_store)
            stats["changed"] = sum(1 for k in produced
                                   if k in snap_store
                                   and snap_store[k] != produced[k])
            stats["pruned"] = sum(1 for k in snap_store if k not in new_store)
            try:
                if not new_store:
                    if store_existed:
                        os.remove(snap_path)
                        stats["removed"] = True
                else:
                    blob = serialize_store(new_store)
                    cur = None
                    if store_existed:
                        try:
                            with open(snap_path, "rb") as f:
                                cur = f.read()
                        except OSError:
                            cur = None
                    if blob != cur:
                        tmp = snap_path + ".tmp"
                        try:
                            os.makedirs(os.path.dirname(snap_path),
                                        exist_ok=True)
                            with open(tmp, "wb") as f:
                                f.write(blob)
                            os.replace(tmp, snap_path)
                            stats["written"] = True
                        except OSError:
                            if os.path.exists(tmp):
                                try:
                                    os.remove(tmp)
                                except OSError:
                                    pass
                            raise
            except OSError as ex:
                results.append(TestResult(
                    filepath, "<snapshots>", "fail",
                    "cannot write snapshot store %s: %s" % (snap_path, ex),
                    0.0))
    return results, stats


# ---------------------------------------------------------------------------
# Parallel worker — runs ONE file's full test list. Files are the unit
# of parallelism (rather than tests) so each worker only type-checks
# once. The pool is sized by `-j` (default: number of CPUs).
# ---------------------------------------------------------------------------

def _worker(args):
    filepath, grep, update_snaps = args
    try:
        return run_file(filepath, grep=grep, update_snaps=update_snaps)
    except Exception as ex:  # noqa: BLE001
        return ([TestResult(filepath, "<worker>", "fail",
                            "worker crash: %r\n%s" % (
                                ex, traceback.format_exc(limit=4)), 0.0)],
                {"store": None, "written": False, "removed": False,
                 "new": 0, "changed": 0, "pruned": 0})


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _color(s, code):
    if not sys.stdout.isatty():
        return s
    return "\033[%sm%s\033[0m" % (code, s)


def _green(s):  return _color(s, "32")
def _red(s):    return _color(s, "31")
def _yellow(s): return _color(s, "33")
def _dim(s):    return _color(s, "2")


def print_report(results, total_ms, verbose):
    """Print a TAP-ish human report. Returns the counts dict."""
    counts = {"pass": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    # Sort by file then test name (stable, readable).
    results_sorted = sorted(results, key=lambda r: (r.file, r.name))
    for r in results_sorted:
        rel = os.path.relpath(r.file) if os.path.isabs(r.file) else r.file
        if r.status == "pass":
            tag = _green("PASS")
        elif r.status == "skip":
            tag = _yellow("SKIP")
        else:
            tag = _red("FAIL")
        line = "%s  %s::%s  %s" % (tag, rel, r.name, _dim("(%.1f ms)" % r.ms))
        if r.status == "fail":
            line += "\n        " + r.detail.replace("\n", "\n        ")
        elif r.status == "skip" and verbose:
            line += "  " + _yellow(r.detail)
        elif r.status == "pass" and r.detail:
            # Stage 119: update-mode annotations ("snapshots: +1 new,
            # ~2 changed, -1 pruned") ride on passing lines so an
            # update run reports exactly what changed, where.
            line += "  " + _dim(r.detail)
        print(line)
    print("")
    print("== hltest: %d pass, %d fail, %d skip in %.1f ms ==" % (
        counts["pass"], counts["fail"], counts["skip"], total_ms))
    return counts


def write_junit(results, path, total_ms):
    """Write a JUnit-compatible XML report. One testsuite per file."""
    by_file = {}
    for r in results:
        by_file.setdefault(r.file, []).append(r)
    root = ET.Element("testsuites", {
        "time": "%.3f" % (total_ms / 1000.0),
    })
    total_fail = sum(1 for r in results if r.status == "fail")
    total_skip = sum(1 for r in results if r.status == "skip")
    total_tests = sum(1 for r in results if r.status != "skip")
    suite_attrs = {
        "name": "hltest",
        "tests": str(total_tests),
        "failures": str(total_fail),
        "skipped": str(total_skip),
        "time": "%.3f" % (total_ms / 1000.0),
    }
    suite = ET.SubElement(root, "testsuite", suite_attrs)
    for filepath, rs in sorted(by_file.items()):
        for r in rs:
            rel = os.path.relpath(filepath) if os.path.isabs(filepath) \
                else filepath
            tc = ET.SubElement(suite, "testcase", {
                "classname": rel,
                "name": r.name,
                "time": "%.3f" % (r.ms / 1000.0),
            })
            if r.status == "fail":
                ET.SubElement(tc, "failure", {
                    "message": saxutils.escape(r.detail[:200]),
                }).text = saxutils.escape(r.detail)
            elif r.status == "skip":
                ET.SubElement(tc, "skipped", {
                    "message": saxutils.escape(r.detail[:200]),
                })
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="hltest",
        description="Halis test runner (Stage 18). Discovers test_* "
                    "functions in .hls files and runs them in parallel.",
    )
    ap.add_argument("files", nargs="*",
                    help=".hls files or directories containing them")
    ap.add_argument("--dir", action="append", default=[],
                    help="directory to discover .hls files in (recursive)")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="recurse into directories (default for --dir)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    help="parallel worker count (default: cpu count)")
    ap.add_argument("--grep", default=None,
                    help="only run tests whose name contains this substring")
    ap.add_argument("--junit", default=None,
                    help="write a JUnit XML report to this file")
    ap.add_argument("-u", "--update-snapshots", action="store_true",
                    help="record new and changed snapshots, prune obsolete "
                         "ones (stores are rewritten only when their bytes "
                         "change; verify mode never writes)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show skip reasons")
    ap.add_argument("--det", action="store_true",
                    help="run every test under the Stage 127 deterministic "
                         "scheduler (the FIFO baton rotation; SPEC section "
                         "67) — concurrent tests become reproducible")
    args = ap.parse_args()

    # Stage 127: --det arms the deterministic scheduler through the
    # HL_DET_SCHED environment variable BEFORE the pool spawns — every
    # worker (and every Interp it builds) inherits it, and boot.py's
    # env fallback does the arming. The mode is an execution property,
    # so the flag composes with every other one.
    if args.det:
        os.environ["HL_DET_SCHED"] = "1"

    # Combine positional files and --dir entries, then discover.
    inputs = list(args.files) + list(args.dir)
    if not inputs:
        ap.error("no input files (pass .hls paths or use --dir DIR)")
    files = discover_files(inputs, recurse=args.recursive)
    if not files:
        sys.stderr.write("hltest: no .hls files found\n")
        return 2

    # Always include the repo's std/ dir so test files can `import "std.test"`
    # (and any other stdlib module) — load_program walks up from the file's
    # directory, but a test file inside tests/ok/ would not find std/ without
    # this. The bootstrap script boot.py has the same walk-up logic, but
    # running hltest from any cwd should still work.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    std_dir = os.path.join(repo_root, "std")
    if os.path.isdir(std_dir):
        os.environ.setdefault("HLTEST_REPO_ROOT", repo_root)

    print("== hltest: %d file(s), -j %d ==" % (len(files), args.jobs))
    t0 = time.perf_counter()

    # BUG FIX: `all_results` was only initialised inside the single-process
    # branch. The multiprocessing branch (args.jobs > 1) used `all_results.extend(...)`
    # without first creating the list, raising UnboundLocalError on every
    # parallel run (the default -j is os.cpu_count(), so this fired on any
    # multi-core host unless the user passed -j 1).
    all_results = []
    all_stats = []
    if args.jobs <= 1 or len(files) == 1:
        for f in files:
            rs, st = run_file(f, grep=args.grep,
                              update_snaps=args.update_snapshots)
            all_results.extend(rs)
            all_stats.append(st)
    else:
        # multiprocessing pool — one task per file.
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(args.jobs) as pool:
            task_args = [(f, args.grep, args.update_snapshots)
                         for f in files]
            for batch, st in pool.imap_unordered(_worker, task_args):
                all_results.extend(batch)
                all_stats.append(st)
    total_ms = (time.perf_counter() - t0) * 1000.0

    counts = print_report(all_results, total_ms, args.verbose)
    if args.update_snapshots:
        # The stores this run touched (written or removed). Verbose path
        # prints are per-test; this is the commit-level summary.
        touched = [st for st in all_stats
                   if st.get("written") or st.get("removed")]
        if touched:
            rel = [os.path.relpath(st["store"]) if os.path.isabs(st["store"])
                   else st["store"] for st in touched]
            print("== snapshots: %d store(s) updated: %s =="
                  % (len(rel), ", ".join(rel)))
    if args.junit:
        write_junit(all_results, args.junit, total_ms)
        print("== junit xml written to %s ==" % args.junit)
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
