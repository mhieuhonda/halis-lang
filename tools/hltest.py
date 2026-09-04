#!/usr/bin/env python3
"""hltest — the Stage 18 test runner for Halis (HLS).

Usage:
  python3 tools/hltest.py [OPTIONS] <file.hls>...
  python3 tools/hltest.py --dir tests/ok            # discover every .hls
  python3 tools/hltest.py --dir tests/ok --grep map # filter by substring
  python3 tools/hltest.py -j 8 file_a.hls file_b.hls # run 8 in parallel
  python3 tools/hltest.py --junit out.xml file.hls  # CI XML report

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

Property-based testing: see `std/quickcheck.hls` for `for_all_<type>`
helpers and `tools/hls-fuzz.py` for the AST-level differential fuzzer.

Coverage: see `tools/hlcov.py` for HLIR-level branch/edge coverage.

Exit code:
  0  all tests pass (or skip)
  1  one or more tests fail
  2  usage / IO error
"""
import argparse
import io
import multiprocessing
import os
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

# Reserved panic prefix used by std.test.test_skip — hltest recognises
# this and reports the test as SKIP instead of FAIL.
_SKIP_PREFIX = "__HLTEST_SKIP__:"

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
    """Parse JUST the entry file (not its imports) and return the names
    of every top-level fn whose name starts with `test_`, in source
    order. We parse the file in isolation so we only see the USER's
    functions, not the stdlib helpers (which are merged in by
    load_program)."""
    with open(filepath, "rb") as f:
        src = f.read()
    from boot.lexer import tokenize              # noqa: E402
    from boot.parser import Parser               # noqa: E402
    toks = tokenize(src)
    prog = Parser(toks).parse_program()
    return [name for name in prog["fns"].keys() if name.startswith("test_")]


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


def run_file(filepath, grep=None):
    """Type-check one file and run every `test_*` fn in it. Returns a
    list of TestResult (one per test). On a load/check error, returns a
    single FAIL result for a synthetic test named `<file>:load`."""
    results = []
    try:
        program = load_program(filepath)
    except HLError as ex:
        results.append(TestResult(filepath, "<load>", "fail",
                                  "compile error: %s" % ex, 0.0))
        return results
    except OSError as ex:
        results.append(TestResult(filepath, "<load>", "fail",
                                  "io: %s" % ex, 0.0))
        return results
    try:
        checker = check(program)
    except HLError as ex:
        results.append(TestResult(filepath, "<check>", "fail",
                                  "type error: %s" % ex, 0.0))
        return results

    tests = list_tests_in_file(filepath)
    if grep:
        tests = [t for t in tests if grep in t]
    if not tests:
        # No tests in this file — not a failure, just record a skip.
        results.append(TestResult(filepath, "<no-tests>", "skip",
                                  "no test_* functions", 0.0))
        return results

    # The interp's args() returns argv[1:] of the program. For tests we
    # pass an empty list (the test should not depend on argv).
    for tname in tests:
        t0 = time.perf_counter()
        buf = io.BytesIO()
        try:
            interp = Interp(program, [filepath.encode("utf-8")], buf,
                            contracts=False)
            interp.call_fn(tname, [])
            # If the test returned a value, ignore it (tests are `void`).
            status = "pass"
            detail = ""
        except HLPanic as ex:
            msg = str(ex)
            if msg.startswith(_SKIP_PREFIX):
                status = "skip"
                detail = msg[len(_SKIP_PREFIX):].strip()
            else:
                status = "fail"
                detail = msg
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
        results.append(TestResult(filepath, tname, status, detail, ms))
    return results


# ---------------------------------------------------------------------------
# Parallel worker — runs ONE file's full test list. Files are the unit
# of parallelism (rather than tests) so each worker only type-checks
# once. The pool is sized by `-j` (default: number of CPUs).
# ---------------------------------------------------------------------------

def _worker(args):
    filepath, grep = args
    try:
        return run_file(filepath, grep=grep)
    except Exception as ex:  # noqa: BLE001
        return [TestResult(filepath, "<worker>", "fail",
                           "worker crash: %r\n%s" % (
                               ex, traceback.format_exc(limit=4)), 0.0)]


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
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show skip reasons")
    args = ap.parse_args()

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

    if args.jobs <= 1 or len(files) == 1:
        all_results = []
        for f in files:
            all_results.extend(run_file(f, grep=args.grep))
    else:
        # multiprocessing pool — one task per file.
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(args.jobs) as pool:
            task_args = [(f, args.grep) for f in files]
            for batch in pool.imap_unordered(_worker, task_args):
                all_results.extend(batch)
    total_ms = (time.perf_counter() - t0) * 1000.0

    counts = print_report(all_results, total_ms, args.verbose)
    if args.junit:
        write_junit(all_results, args.junit, total_ms)
        print("== junit xml written to %s ==" % args.junit)
    return 0 if counts["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
