#!/usr/bin/env python3
"""hls-ctime — the Stage 138 per-commit compile-time regression
dashboard.

Usage:
  python3 tools/hls-ctime.py record    [--suite DIR] [--instrument S]
                                      [--grep S] [--samples N]
                                      [--warmup K] [--from-report R.json]
                                      [--commit SHA] [--note TEXT]
                                      [--allow-dirty]
                                      [--history PATH] [--json OUT]
  python3 tools/hls-ctime.py sanction WORKLOAD --reason TEXT
                                      [--commit SHA] [--author NAME]
                                      [--history PATH]
  python3 tools/hls-ctime.py gate      [--baseline SHA] [--threshold PCT]
                                      [--history PATH] [--json OUT]
                                      [--quiet]
  python3 tools/hls-ctime.py dashboard --out FILE.md [--limit N]
                                      [--history PATH] [--json OUT]
                                      [--stamp]
  python3 tools/hls-ctime.py history   [--workload NAME] [--json]
                                      [--history PATH]

Roadmap Stage 138 is one phrase: "Compile-time regression dashboard
(per-commit)". Stage 124 built the runtime instrument (hls-bench),
Stage 136 gave RUNTIME its ledger (hls-perf), Stage 137 gave MEMORY
its own (hls-mem) — this stage gives the COMPILER's own wall time
its ledger, the axis a language release can regress that no program
benchmark sees: the tool gets slower to feed.

The instrument is the compiler itself, and Halis ships two of them,
so they are two INSTRUMENTS, never compared:

  stage0  python3 tools/hlinterp/boot.py src/hlc.hls <workload> <out.c>
         (the bootstrap chain — the front door every contributor's
         `make` walks through)
  native  bin/hlc <workload> <out.c>
         (the self-hosted compiler — the artifact users run)

Both time the SAME quantity — one full HLS-to-C compile of one
tracked workload, wall time, milliseconds — but through different
machines, so a record names its instrument and the gate refuses to
compare across them, exactly as interpreter and native timings are
different instruments in hls-perf.

hls-ctime is a LEDGER over commits:

  1. a RECORD is one measurement pinned to one commit. `record` runs
     the REAL compiler over the tracked suite (the tracked default
     is benchmarks/ctime_watch/ — five deterministic, compile-shaped
     workloads: the token stream, the parse tree, the type checker,
     the effect graph, the code generator) and appends an entry keyed
     to `git rev-parse HEAD` — refused when the work tree carries
     tracked modifications, because a per-commit record that does
     not pin a clean commit is a rumour, not a measurement. Every
     record also pins the compiler's OWN identity: the sha256 over
     the compiler source (src/hlc.hls + src/hlc/*.hls — the bytes
     of the thing being timed) and the sha256 of the compiler
     BINARY actually timed (bin/hlc on the native instrument;
     tools/hlinterp/boot.py on stage0) — and the STALE-BINARY LAW bites at
     record time: a native record whose compiler digest was already
     filed against a DIFFERENT source digest is refused, because
     bin/hlc was not rebuilt for the commit it claims to measure
     ("run make bootstrap first"; a doc-only commit leaves the
     source digest unchanged, so the identical binary it produces
     is expected, not stale). `--from-report` imports a machine
     report instead of measuring (the CI path: the runner machine
     records, this machine files it), and then the commit must come
     from --commit or the ambient git;

  2. a SANCTION is a written excuse: one workload, one reason, one
     author. `sanction parse_deep --reason "the checker grew a
     second pass; the compile paid for it"` — a regression with a
     sanction is reported, not failed. A sanction without a reason
     is refused (an excuse nobody wrote down is not an excuse);

  3. the GATE is the law. `gate` classifies every workload in the
     newest record against the prior record of the SAME INSTRUMENT
     (byte-identical suite) with Stage 124's law — Welch p < 0.05
     for significance, the seeded bootstrap CI of the median change
     against the --threshold noise band (default 10%, honest about
     wall time on a shared machine): no-change / within-noise /
     improved / regressed, plus new and vanished workloads on
     either side of a rename. One unsanctioned regressed or
     vanished workload exits 1 naming it; sanctioned rows are loud
     but green;

  4. the DASHBOARD renders the ledger — COMPILETIME.md with the
     verdict table, per-workload trend sparklines across the
     recorded commits, and the sanctions register. It is
     DETERMINISTIC: sorted walks, no wall-clock unless --stamp (and
     then only the Generated line), the sha256 ledger digest
     embedded — two renders of one ledger are byte-identical, so a
     hand-edited dashboard is a diff.

The history is a JSONL file (default .hlctime/history.jsonl,
gitignored — pass --history to point CI at a committed ledger).
Append-only by construction: `record` and `sanction` append, nothing
rewrites, and a line that does not parse is a named error, never a
silent skip.

Exit codes:
  0  verified (gate pass, dashboard rendered, record filed)
  1  named violation (gate failure, dirty tree, corrupt line,
     instrument mismatch, stale compiler binary)
  2  usage / IO error (bad paths, empty ledger, unknown workload,
     a sanction without a reason, no compiler to time)

Layering note: hls-bench (Stage 124) measures programs; hls-perf
(Stage 136) remembers program time; hls-mem (Stage 137) remembers
program memory; hls-ctime (this tool) remembers the time of the
COMPILER itself. The statistics are imported from hls-bench — one
implementation of Welch's test and the bootstrap, so the runner's
numbers, the perf ledger's verdicts and this ledger's verdicts can
never drift apart.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
import time

TOOL_VERSION = "0.156.0"
TOOL = "hls-ctime"
HISTORY_DEFAULT = os.path.join(".hlctime", "history.jsonl")
DEFAULT_SUITE = os.path.join("benchmarks", "ctime_watch")
DEFAULT_SAMPLES = 6
DEFAULT_WARMUP = 1
DEFAULT_NOISE_PCT = 10.0
DEFAULT_DASH_LIMIT = 12
MIN_SAMPLES = 2
INSTRUMENTS = ("stage0", "native")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HLC_SOURCE_ENTRY = os.path.join("src", "hlc.hls")
HLC_SOURCE_DIR = os.path.join("src", "hlc")
BOOT_DRIVER = os.path.join("tools", "hlinterp", "boot.py")
NATIVE_COMPILER = os.path.join("bin", "hlc")

# The statistics are hls-bench's statistics — imported, not
# re-derived, so every ledger in this tree speaks the same law.
_spec = importlib.util.spec_from_file_location(
    "hls_bench", os.path.join(ROOT, "tools", "hls-bench.py"))
BENCH = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(BENCH)

_SPARK_BANDS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
_SPARK_MID = "\u2584"


class HlCtimeError(Exception):
    """A named refusal. .exit is the process exit code it deserves."""

    def __init__(self, msg, exit=1):
        super().__init__(msg)
        self.exit = exit


# ---------------------------------------------------------------------------
# Small helpers — bytes, digests, json, the git surface.
# ---------------------------------------------------------------------------

def json_dumps_sorted(obj):
    return json.dumps(obj, sort_keys=True, indent=1) + "\n"


def json_line(obj):
    """One ledger line. COMPACT — the history is JSONL, one self-
    contained object per line; pretty-printing here would shatter an
    entry across lines and corrupt every later parse."""
    return json.dumps(obj, sort_keys=True) + "\n"


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def is_sha1(s):
    return (len(s) == 40
            and all(c in "0123456789abcdef" for c in s))


def is_sha256(s):
    return (len(s) == 64
            and all(c in "0123456789abcdef" for c in s))


def short(s, n=10):
    return s[:n]


def fmt_ms(ms):
    """A millisecond count fit for a table cell — hls-bench's own
    time formatter, seeded from ns so the two dashboards agree."""
    return BENCH.fmt_time((ms or 0.0) * 1e6)


def git(args, root):
    """Run git in root; None when git is absent or the tree is not a
    work tree — callers decide whether that is fatal."""
    try:
        p = subprocess.run(["git", "-C", root] + args,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def git_head(root):
    return git(["rev-parse", "HEAD"], root)


def git_user(root):
    name = git(["config", "user.name"], root)
    return name if name else "unknown"


def median_of(estimates):
    return BENCH.median(estimates)


# ---------------------------------------------------------------------------
# The compiler identity — the two digests every record pins.
# ---------------------------------------------------------------------------

def compiler_source_digest(root):
    """sha256 over the sorted (relpath, bytes) walk of the compiler
    source — src/hlc.hls and every src/hlc/*.hls module. This is the
    identity of the CODE BEING TIMED: the tracked suite changes what
    the compiler compiles, the source digest says which compiler
    compiled it."""
    d = root
    members = []
    if not os.path.isfile(os.path.join(d, HLC_SOURCE_ENTRY)):
        raise HlCtimeError("the compiler entry %s does not exist under %s"
                           % (HLC_SOURCE_ENTRY, root), exit=2)
    members.append((HLC_SOURCE_ENTRY,
                    os.path.join(d, HLC_SOURCE_ENTRY)))
    srcdir = os.path.join(d, HLC_SOURCE_DIR)
    if not os.path.isdir(srcdir):
        raise HlCtimeError("the compiler module directory %s does not "
                           "exist under %s" % (HLC_SOURCE_DIR, root), exit=2)
    for fn in sorted(os.listdir(srcdir)):
        if fn.endswith(".hls"):
            rel = os.path.join(HLC_SOURCE_DIR, fn)
            members.append((rel, os.path.join(srcdir, fn)))
    h = hashlib.sha256()
    for rel, path in members:
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            raise HlCtimeError("cannot read the compiler source %s: %s"
                              % (rel, exc), exit=2)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(blob)
        h.update(b"\0")
    return h.hexdigest()


def compiler_binary_digest(root, instrument):
    """sha256 of the compiler BINARY actually timed — bin/hlc for the
    native instrument (an untracked build artifact, so the record
    pins its bytes itself: the clean-tree check cannot vouch for it),
    tools/hlinterp/boot.py for stage0 (the driver the chain runs through; the
    interpreter's word is the Python fingerprint in the env)."""
    path = os.path.join(root, NATIVE_COMPILER if instrument == "native"
                        else BOOT_DRIVER)
    if not os.path.isfile(path):
        raise HlCtimeError(
            "the %s instrument needs %s — run `make bootstrap` first"
            % (instrument, NATIVE_COMPILER if instrument == "native"
               else BOOT_DRIVER), exit=2)
    try:
        with open(path, "rb") as fh:
            return sha256_hex(fh.read())
    except OSError as exc:
        raise HlCtimeError("cannot read %s: %s" % (path, exc), exit=2)


# ---------------------------------------------------------------------------
# The suite — the tracked workload directory and its digest.
# ---------------------------------------------------------------------------

def suite_members(root, suite):
    """The tracked workloads: every *.hls file directly in the suite
    directory, sorted by stem — the suite IS the set; the ledger's
    suite digest covers names AND bytes, so a renamed or edited
    workload re-baselines, exactly as an edited bench suite does."""
    d = os.path.join(root, suite)
    if not os.path.isdir(d):
        raise HlCtimeError("the suite directory %s does not exist" % suite,
                           exit=2)
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".hls"):
            out.append((fn[:-len(".hls")], os.path.join(d, fn)))
    if not out:
        raise HlCtimeError("the suite directory %s carries no .hls "
                          "workloads" % suite, exit=2)
    return out


def members_digest(root, members):
    """sha256 over the sorted (name, bytes) pairs — names included, so
    a rename is a change, not a coincidence."""
    h = hashlib.sha256()
    for name, path in members:
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            raise HlCtimeError("cannot read the workload %s: %s"
                              % (path, exc), exit=2)
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(blob)
        h.update(b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# The instrument — compile one workload once, timed.
# ---------------------------------------------------------------------------

def compile_once(instrument, workload, out_c, root):
    """One full HLS-to-C compile of the workload through the chosen
    instrument. Returns wall milliseconds. A compile that does not
    exit 0 is a named refusal — the suite compiles green or the
    record does not exist (a dashboard over half a suite is a lie).
    The C output is thrown away: the artifact of a compile-time
    record is the time, not the code."""
    if instrument == "native":
        cmd = [os.path.join(root, NATIVE_COMPILER),
               os.path.abspath(workload), out_c]
    else:
        cmd = [sys.executable,
               os.path.join(root, BOOT_DRIVER),
               os.path.join(root, HLC_SOURCE_ENTRY),
               os.path.abspath(workload), out_c]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True,
                           cwd=root, timeout=600)
    except subprocess.TimeoutExpired:
        raise HlCtimeError("the compile of %s timed out after 600s "
                           "(instrument %s)"
                           % (os.path.basename(workload), instrument))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()
        detail = tail[-1] if tail else "(no output)"
        raise HlCtimeError(
            "the compile of %s failed (%s, exit %d): %s — file a "
            "bug, not a record"
            % (os.path.basename(workload), instrument, p.returncode,
               detail))
    return round(elapsed_ms, 3)


def measure_suite(root, instrument, members, samples, warmup, grep):
    """The real measurement: for every workload, `warmup` untimed
    compiles (page cache, dynamic loader, interpreter warm path —
    the first run of a binary is not its steady state), then
    `samples` timed compiles whose milliseconds are the ledger's
    distribution."""
    workloads = {}
    for name, path in members:
        if grep and grep not in name:
            continue
        tmp = tempfile.NamedTemporaryFile(
            prefix=".hlctime-out-", suffix=".c", delete=False)
        tmp.close()
        try:
            for _ in range(max(0, warmup)):
                compile_once(instrument, path, tmp.name, root)
            ms = [compile_once(instrument, path, tmp.name, root)
                  for _ in range(samples)]
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        workloads[name] = {
            "samples_ms": ms,
            "median_ms": median_of(ms),
            "samples_count": len(ms),
        }
    if not workloads:
        raise HlCtimeError("--grep %r matched no workload of the suite"
                          % grep, exit=2)
    return workloads


# ---------------------------------------------------------------------------
# The ledger — one JSON object per line, append-only.
# ---------------------------------------------------------------------------

def load_history(path):
    """Parse the ledger. A line that does not parse, an object without
    a known type, a record without a commit — every one is a NAMED
    error naming the line number; the ledger is never guessed
    around."""
    if not os.path.exists(path):
        raise HlCtimeError("the history ledger %s does not exist yet "
                          "(record something first)" % path, exit=2)
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise HlCtimeError("history line %d is corrupt (%s) — "
                                  "the ledger is append-only; fix or "
                                  "remove the line by hand" % (i, exc))
            if not isinstance(obj, dict) or "type" not in obj:
                raise HlCtimeError("history line %d carries no type" % i)
            if obj["type"] == "record":
                _validate_record(obj, "history line %d" % i)
            elif obj["type"] == "sanction":
                _validate_sanction(obj, "history line %d" % i)
            else:
                raise HlCtimeError("history line %d has unknown type %r"
                                  % (i, obj["type"]))
            obj["_line"] = i
            entries.append(obj)
    return entries


def _validate_record(rec, where):
    if not rec.get("commit") or not is_sha1(rec["commit"]):
        raise HlCtimeError("%s: a record without a 40-hex "
                          "commit pin is not a per-commit record" % where)
    if rec.get("instrument") not in INSTRUMENTS:
        raise HlCtimeError("%s: instrument %r is not one of %s"
                          % (where, rec.get("instrument"),
                             list(INSTRUMENTS)))
    if not rec.get("suite_digest") or not is_sha256(rec["suite_digest"]):
        raise HlCtimeError("%s: a record without a suite "
                          "digest cannot be compared" % where)
    if not rec.get("source_digest") or not is_sha256(rec["source_digest"]):
        raise HlCtimeError("%s: a record without a compiler source "
                          "digest does not name the compiler it "
                          "timed" % where)
    if not rec.get("compiler_digest") \
            or not is_sha256(rec["compiler_digest"]):
        raise HlCtimeError("%s: a record without a compiler binary "
                          "digest does not name the binary it "
                          "timed" % where)
    workloads = rec.get("workloads")
    if not isinstance(workloads, dict) or not workloads:
        raise HlCtimeError("%s: a record with no workloads "
                          "measured nothing" % where)
    for name, w in sorted(workloads.items()):
        ms = w.get("samples_ms")
        if not isinstance(ms, list) or len(ms) < MIN_SAMPLES:
            raise HlCtimeError(
                "%s: workload %s carries fewer than %d "
                "samples — a median no sample supports is not a "
                "measurement" % (where, name, MIN_SAMPLES))
        if not all(isinstance(x, (int, float)) and x > 0 for x in ms):
            raise HlCtimeError(
                "%s: workload %s has non-positive compile times"
                % (where, name))


def _validate_sanction(s, where):
    if not s.get("workload"):
        raise HlCtimeError("%s: a sanction without a workload" % where)
    if not s.get("reason"):
        raise HlCtimeError("%s: a sanction without a "
                          "written reason is not a sanction" % where)


def append_entry(path, entry):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json_line(entry))


def records_of(entries):
    return [e for e in entries if e["type"] == "record"]


def sanctions_of(entries):
    return [e for e in entries if e["type"] == "sanction"]


# ---------------------------------------------------------------------------
# record — measure now, or import a report, and pin it to a commit.
# ---------------------------------------------------------------------------

def _env_fingerprint(root, instrument):
    env = {"python": "%d.%d.%d" % sys.version_info[:3],
           "cpus": os.cpu_count() or 0}
    if instrument == "stage0":
        cc = os.environ.get("CC", "gcc")
        cc_ver = subprocess.run([cc, "--version"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        if cc_ver.returncode == 0 and cc_ver.stdout:
            env["cc"] = cc_ver.stdout.strip().splitlines()[0]
        else:
            env["cc"] = cc
    return env


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_report(path, where):
    """The CI import path: a machine report is the measuring
    machine's word — it carries the samples AND the three digests
    (suite, compiler source, compiler binary), because the machine
    that measured is the machine that can pin what it measured."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except OSError as exc:
        raise HlCtimeError("cannot read the report: %s" % exc, exit=2)
    except ValueError as exc:
        raise HlCtimeError("the report is not valid JSON: %s" % exc, exit=2)
    if not isinstance(report, dict) or report.get("tool") != TOOL:
        raise HlCtimeError("%s: the report is not an hls-ctime report "
                          "(tool=%r)" % (where, report.get("tool")
                                         if isinstance(report, dict)
                                         else type(report).__name__), exit=2)
    if report.get("instrument") not in INSTRUMENTS:
        raise HlCtimeError("%s: report instrument %r is not one of %s"
                          % (where, report.get("instrument"),
                             list(INSTRUMENTS)), exit=2)
    for key in ("suite_digest", "source_digest", "compiler_digest"):
        v = report.get(key)
        if not v or not is_sha256(v):
            raise HlCtimeError("%s: the report carries no valid %s — a "
                              "machine report must pin what it measured"
                              % (where, key), exit=2)
    return report


def stale_binary_check(entries, new_rec):
    """THE STALE-BINARY LAW — the compile-time-specific floor. A
    native record whose compiler digest was filed earlier against a
    DIFFERENT source digest is a lie: the same bin/hlc bytes cannot
    be the build of two different compiler sources, so the binary
    was not rebuilt for the commit it claims to measure. Doc-only
    commits leave the source digest unchanged, so an identical
    binary is expected there — the law refuses only the
    contradiction."""
    if new_rec["instrument"] != "native":
        return
    for r in records_of(entries):
        if r is new_rec:
            continue
        if r["instrument"] != "native":
            continue
        if r["compiler_digest"] == new_rec["compiler_digest"] \
                and r["source_digest"] != new_rec["source_digest"]:
            raise HlCtimeError(
                "the stale-binary law: bin/hlc (sha256 %s) was already "
                "filed at commit %s against a different compiler source "
                "(%s vs %s) — the binary was not rebuilt for this commit; "
                "run `make bootstrap`, then record"
                % (short(new_rec["compiler_digest"]),
                   short(r["commit"]),
                   short(r["source_digest"]),
                   short(new_rec["source_digest"])))


def cmd_record(args):
    root = args.root or ROOT
    history = args.history or HISTORY_DEFAULT

    # The pinning law: every record names one clean commit. Explicit
    # --commit wins (the CI import path); otherwise the ambient git
    # must supply a HEAD, and a dirty tree is a refusal, not a note.
    if args.commit:
        commit = args.commit.strip().lower()
        if not is_sha1(commit):
            raise HlCtimeError("--commit must be a 40-hex sha (got %r)"
                              % args.commit, exit=2)
    else:
        commit = git_head(root)
        if not commit:
            raise HlCtimeError(
                "%s is not a git work tree — pass --commit to pin the "
                "record explicitly" % root, exit=2)
        dirty = git(["status", "--porcelain", "-uno"], root)
        if dirty and not args.allow_dirty:
            raise HlCtimeError(
                "the work tree carries tracked modifications — a "
                "per-commit record pins a clean commit "
                "(--allow-dirty to record anyway)")

    members = suite_members(root, args.suite)
    digest = members_digest(root, members)

    if args.from_report:
        report = read_report(args.from_report, "the report")
        instrument = report["instrument"]
        if report["suite_digest"] != digest:
            raise HlCtimeError(
                "the report's suite digest (%s) does not match the "
                "local suite (%s) — the machine measured a different "
                "suite; re-baseline, do not file"
                % (short(report["suite_digest"]), short(digest)))
        source_digest = report["source_digest"]
        compiler_digest = report["compiler_digest"]
        workloads = report.get("workloads")
        if not isinstance(workloads, dict) or not workloads:
            raise HlCtimeError("the report carries no measured "
                              "workloads", exit=2)
        for name in sorted(workloads):
            w = workloads[name]
            if not isinstance(w, dict) or "samples_ms" not in w:
                raise HlCtimeError(
                    "the report's workload %s carries no samples_ms"
                    % name, exit=2)
            workloads[name] = {
                "samples_ms": w["samples_ms"],
                "median_ms": median_of(w["samples_ms"]),
                "samples_count": len(w["samples_ms"]),
            }
        plan = report.get("plan") or {"samples": args.samples,
                                      "warmup": args.warmup}
        env = report.get("env") or _env_fingerprint(root, instrument)
        source = "report %s" % args.from_report
    else:
        instrument = args.instrument
        if instrument == "native" \
                and not os.path.isfile(os.path.join(root, NATIVE_COMPILER)):
            raise HlCtimeError(
                "the native instrument needs %s — run `make bootstrap` "
                "first (or record with --instrument stage0)"
                % NATIVE_COMPILER, exit=2)
        workloads = measure_suite(root, instrument, members,
                                  args.samples, args.warmup, args.grep)
        source_digest = compiler_source_digest(root)
        compiler_digest = compiler_binary_digest(root, instrument)
        plan = {"samples": args.samples, "warmup": args.warmup}
        env = _env_fingerprint(root, instrument)
        source = instrument

    entry = {
        "type": "record",
        "tool": TOOL,
        "version": TOOL_VERSION,
        "commit": commit,
        "instrument": instrument,
        "suite": args.suite,
        "suite_digest": digest,
        "source_digest": source_digest,
        "compiler_digest": compiler_digest,
        "plan": plan,
        "noise_pct": DEFAULT_NOISE_PCT,
        "env": env,
        "workloads": workloads,
        "note": args.note,
        "stamp": _utc_now() if args.stamp else None,
    }
    # The floor laws bite HERE, at record time — a record that its
    # own ledger would refuse never enters the ledger, and a stale
    # binary never gets the chance to pretend it was rebuilt.
    _validate_record(entry, "the new record")
    stale_binary_check(load_history(history) if os.path.exists(history)
                       else [], entry)
    append_entry(history, entry)
    _write_report(args, entry)
    print("recorded %s (%s) instrument=%s suite=%s workloads=%d "
          "source=%s" % (short(commit), commit, instrument, args.suite,
                         len(workloads), source))
    for name in sorted(workloads):
        w = workloads[name]
        print("  %-16s median %s  (%d samples)" % (name,
                                                   fmt_ms(w["median_ms"]),
                                                   w["samples_count"]))
    return 0


def _write_report(args, entry):
    """--json on record: file the filed entry beside the ledger (what
    CI keeps when the raw record matters)."""
    if not args.json:
        return
    with open(args.json, "w", encoding="utf-8") as fh:
        fh.write(json_dumps_sorted(entry))


# ---------------------------------------------------------------------------
# sanction — a written excuse, one workload at a time.
# ---------------------------------------------------------------------------

def cmd_sanction(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    known = sorted({n for r in records_of(entries)
                    for n in r["workloads"]})
    if args.workload not in known:
        raise HlCtimeError(
            "unknown workload %r — the ledger knows: %s"
            % (args.workload, ", ".join(known) if known else "(none)"),
            exit=2)
    if not args.reason:
        raise HlCtimeError("a sanction without a written reason is not a "
                          "sanction (--reason TEXT)", exit=2)
    entry = {
        "type": "sanction",
        "tool": TOOL,
        "version": TOOL_VERSION,
        "workload": args.workload,
        "reason": args.reason,
        "commit": args.commit,
        "author": args.author or git_user(args.root or ROOT),
        "stamp": _utc_now() if args.stamp else None,
    }
    append_entry(history, entry)
    print("sanctioned %s (%s)" % (args.workload, args.author
                                  or git_user(args.root or ROOT)))
    return 0


# ---------------------------------------------------------------------------
# gate — the law: one unsanctioned regression fails.
# ---------------------------------------------------------------------------

def pick_baseline(new_rec, entries, want):
    """The instrument law: comparisons happen only within the same
    instrument and byte-identical suite. Default baseline: the newest
    earlier record of that instrument."""
    recs = records_of(entries)
    idx = max(i for i, r in enumerate(recs) if r is new_rec)
    if want:
        cands = [r for r in recs if r["commit"].startswith(want)]
        if not cands:
            raise HlCtimeError("no record carries commit prefix %r" % want,
                              exit=2)
        if len(cands) > 1:
            raise HlCtimeError("commit prefix %r is ambiguous (%d "
                              "records)" % (want, len(cands)), exit=2)
        base = cands[0]
        if base is new_rec:
            raise HlCtimeError("the baseline is the record itself — a "
                              "record does not regress against "
                              "itself", exit=2)
    else:
        earlier = [r for r in recs[:idx]
                   if r["instrument"] == new_rec["instrument"]
                   and r["suite_digest"] == new_rec["suite_digest"]]
        if not earlier:
            raise HlCtimeError(
                "no earlier record of the same instrument to compare "
                "against (instrument=%s suite_digest=%s) — record one "
                "first" % (new_rec["instrument"],
                           new_rec["suite_digest"][:12]), exit=2)
        base = earlier[-1]
    if base["instrument"] != new_rec["instrument"]:
        raise HlCtimeError(
            "different instruments: the baseline is %s, the record is "
            "%s — the bootstrap chain and the self-hosted compiler are "
            "different machines" % (base["instrument"],
                                    new_rec["instrument"]))
    if base["suite_digest"] != new_rec["suite_digest"]:
        raise HlCtimeError(
            "the suite changed since the baseline (digest %s vs %s) — "
            "re-baseline, do not compare"
            % (base["suite_digest"][:12],
               new_rec["suite_digest"][:12]))
    return base


def classify(cur_est, base_est, workload_key, noise_pct):
    """Stage 124's classification law, verbatim: Welch p for
    significance, the seeded bootstrap CI of the median change against
    the noise band. The seed is derived from the workload key, so the
    SAME pair of sample sets always classifies the SAME way."""
    if len(cur_est) < MIN_SAMPLES or len(base_est) < MIN_SAMPLES:
        raise HlCtimeError(
            "classification needs >= %d samples per side (workload %s)"
            % (MIN_SAMPLES, workload_key))
    p = BENCH.welch_p(cur_est, base_est)
    significant = p < BENCH._ALPHA
    cur_med = BENCH.median(cur_est)
    base_med = BENCH.median(base_est)
    if base_med <= 0:
        raise HlCtimeError(
            "baseline median is not positive (workload %s)" % workload_key)
    point = (cur_med - base_med) / base_med * 100.0
    rng = random.Random(workload_key)
    nb = len(base_est)
    diffs = []
    for _ in range(BENCH._BOOTSTRAP_B):
        cs = [cur_est[rng.randrange(len(cur_est))]
              for _ in range(len(cur_est))]
        bs = [base_est[rng.randrange(nb)] for _ in range(nb)]
        diffs.append((BENCH.median(cs) - BENCH.median(bs))
                     / base_med * 100.0)
    diffs.sort()
    lo = BENCH.quantile(diffs, 0.025)
    hi = BENCH.quantile(diffs, 0.975)
    if not significant:
        klass = "no-change"
    elif lo > noise_pct:
        klass = "regressed"
    elif hi < -noise_pct:
        klass = "improved"
    else:
        klass = "within-noise"
    return {"class": klass, "p": p, "point_pct": point,
            "ci_pct": [lo, hi],
            "cur_median_ms": cur_med, "base_median_ms": base_med}


def verdict_of(entries, baseline=None, noise_pct=DEFAULT_NOISE_PCT):
    """Classify the newest record against the chosen baseline and
    apply the sanctions. Returns the full machine verdict."""
    recs = records_of(entries)
    if not recs:
        raise HlCtimeError("the ledger carries no records — nothing to "
                          "gate", exit=2)
    new_rec = recs[-1]
    base_rec = pick_baseline(new_rec, entries, baseline)

    active = {}
    for s in sanctions_of(entries):
        active.setdefault(s["workload"], s)

    rows = []
    for name in sorted(set(new_rec["workloads"])
                       | set(base_rec["workloads"])):
        if name in new_rec["workloads"] \
                and name in base_rec["workloads"]:
            c = classify(new_rec["workloads"][name]["samples_ms"],
                         base_rec["workloads"][name]["samples_ms"],
                         name, noise_pct)
            row = {"workload": name, "class": c["class"],
                   "p": c["p"], "point_pct": c["point_pct"],
                   "ci_pct": c["ci_pct"],
                   "cur_median_ms": c["cur_median_ms"],
                   "base_median_ms": c["base_median_ms"],
                   "samples": new_rec["workloads"][name]["samples_count"]}
        elif name in new_rec["workloads"]:
            row = {"workload": name, "class": "new",
                   "cur_median_ms":
                       new_rec["workloads"][name]["median_ms"]}
        else:
            row = {"workload": name, "class": "vanished"}
        if row["class"] in ("regressed", "vanished") and name in active:
            row["sanction"] = {"reason": active[name]["reason"],
                               "author": active[name].get("author"),
                               "commit": active[name].get("commit")}
        rows.append(row)

    unsanctioned = [r for r in rows
                    if r["class"] in ("regressed", "vanished")
                    and "sanction" not in r]
    verdict = {
        "tool": TOOL,
        "version": TOOL_VERSION,
        "verdict": "fail" if unsanctioned else "pass",
        "record": new_rec["commit"],
        "baseline": base_rec["commit"],
        "instrument": new_rec["instrument"],
        "suite": new_rec["suite"],
        "noise_pct": noise_pct,
        "workloads": rows,
        "sanctioned": sorted(r["workload"] for r in rows
                             if "sanction" in r),
        "unsanctioned": sorted(r["workload"] for r in unsanctioned),
    }
    return verdict


def cmd_gate(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    v = verdict_of(entries, baseline=args.baseline,
                   noise_pct=args.threshold)
    if not args.quiet:
        print("gate %s: record %s vs baseline %s (instrument %s, "
              "threshold %.1f%%)"
              % (v["verdict"].upper(), short(v["record"]),
                 short(v["baseline"]), v["instrument"], v["noise_pct"]))
        for r in v["workloads"]:
            line = "  %-16s" % r["workload"]
            if r["class"] in ("no-change", "within-noise", "improved",
                              "regressed"):
                lo, hi = r["ci_pct"]
                line += (" %-12s %s  [%s %s]  p=%.4f"
                         % (r["class"],
                            BENCH.fmt_pct(r["point_pct"]),
                            BENCH.fmt_pct(lo), BENCH.fmt_pct(hi),
                            r["p"]))
            else:
                line += " %s" % r["class"]
                if r["class"] == "new":
                    line += " (%s)" % fmt_ms(r["cur_median_ms"] or 0.0)
            if "sanction" in r:
                line += ("  sanctioned: %s (%s)"
                         % (r["sanction"]["reason"],
                            r["sanction"].get("author")))
            print(line)
    if v["unsanctioned"]:
        print("GATE FAIL: %s regressed without a sanction: %s"
              % (len(v["unsanctioned"]), ", ".join(v["unsanctioned"])))
    elif not args.quiet:
        print("GATE PASS: no unsanctioned regression across %d workloads"
              % len(v["workloads"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(json_dumps_sorted(v))
    return 1 if v["unsanctioned"] else 0


# ---------------------------------------------------------------------------
# dashboard — the deterministic render of the ledger.
# ---------------------------------------------------------------------------

def sparkline(medians):
    """A per-workload trend across records, oldest to newest. The
    series is normalized to [min, max] over 8 bands; a constant series
    is a run of mid bands (a wall of max-height blocks would read as
    a regression that is not there). The band is INTEGER half-up
    math — ((v-lo)*14 + (hi-lo)) // (2*(hi-lo)) — the exact function
    the ok corpus fixture pins, so tool and fixture cannot drift."""
    if not medians:
        return ""
    lo = min(medians)
    hi = max(medians)
    if hi <= lo:
        return _SPARK_MID * len(medians)
    span = hi - lo
    out = []
    for v in medians:
        band = ((v - lo) * 14 + span) // (2 * span)
        out.append(_SPARK_BANDS[int(band)])
    return "".join(out)


def render_dashboard(entries, history_path, limit, stamp=None):
    """The whole document is a pure function of the ledger bytes and
    nothing else (stamp excepted). Sorted walks everywhere; the sha256
    of the ledger file pins what was rendered."""
    recs = records_of(entries)
    if not recs:
        raise HlCtimeError("the ledger carries no records — nothing to "
                          "render", exit=2)
    try:
        with open(history_path, "rb") as fh:
            digest = sha256_hex(fh.read())
    except OSError as exc:
        raise HlCtimeError("cannot read the ledger: %s" % exc, exit=2)

    new_rec = recs[-1]
    base = None
    verdict = None
    try:
        verdict = verdict_of(entries, noise_pct=DEFAULT_NOISE_PCT)
        base = pick_baseline(new_rec, entries, None)
    except HlCtimeError:
        verdict = None
        base = None

    workload_names = sorted(set(new_rec["workloads"]))

    shown = recs[-limit:] if limit and limit > 0 else recs

    lines = []
    a = lines.append
    a("# HLS compile-time dashboard")
    a("")
    a("- Tool: %s %s" % (TOOL, TOOL_VERSION))
    a("- Ledger: `%s` (sha256 `%s`)" % (history_path, digest))
    a("- Records: %d (showing %d)" % (len(recs), len(shown)))
    a("- Newest record: `%s` (%d workloads, instrument %s)"
      % (new_rec["commit"], len(new_rec["workloads"]),
         new_rec["instrument"]))
    a("- Compiler source: `%s`" % new_rec["source_digest"])
    a("- Compiler binary: `%s`" % new_rec["compiler_digest"])
    if base is not None:
        a("- Baseline: `%s`" % base["commit"])
    else:
        a("- Baseline: none (no earlier record of this instrument)")
    if verdict is not None:
        a("- Gate verdict: **%s**" % verdict["verdict"])
        if verdict["unsanctioned"]:
            a("- Unsanctioned: %s"
              % ", ".join(verdict["unsanctioned"]))
    else:
        a("- Gate verdict: n/a (no earlier record of this "
          "instrument to compare against)")
    if stamp:
        a("- Generated: %s" % stamp)
    a("")
    a("## Verdicts (newest record vs baseline)")
    a("")
    if verdict is None:
        a("No comparison yet: the newest record is the first of its "
          "instrument. Record twice and the verdict table fills.")
    else:
        a("| workload | base | now | change | 95% CI | p | class |")
        a("|---|---|---|---|---|---|---|")
        for r in verdict["workloads"]:
            if r["class"] in ("no-change", "within-noise", "improved",
                              "regressed"):
                lo, hi = r["ci_pct"]
                a("| %s | %s | %s | %s | [%s %s] | %.4f | %s |"
                  % (r["workload"], fmt_ms(r["base_median_ms"]),
                     fmt_ms(r["cur_median_ms"]),
                     BENCH.fmt_pct(r["point_pct"]),
                     BENCH.fmt_pct(lo), BENCH.fmt_pct(hi), r["p"],
                     r["class"] + (" (sanctioned)"
                                   if "sanction" in r else "")))
            elif r["class"] == "new":
                a("| %s | — | %s | — | — | — | new |"
                  % (r["workload"], fmt_ms(r["cur_median_ms"] or 0.0)))
            else:
                a("| %s | %s | — | — | — | — | vanished |"
                  % (r["workload"],
                     fmt_ms(r.get("base_median_ms") or 0.0)))
    a("")
    a("## Trends (median ms/compile across records, oldest to newest)")
    a("")
    a("| workload | trend | %s |" % " | ".join(
        short(r["commit"], 7) for r in shown))
    a("|---|---|" + "---|" * len(shown))
    for name in workload_names:
        series = []
        for r in shown:
            w = r["workloads"].get(name)
            series.append(w["median_ms"] if w else None)
        known = [m for m in series if m]
        spark = sparkline(known) if known else "-"
        cells = " | ".join(
            (fmt_ms(m) if m else "—") for m in series)
        a("| %s | %s | %s |" % (name, spark, cells))
    a("")
    a("## Sanctions")
    a("")
    sancs = sanctions_of(entries)
    if not sancs:
        a("None. Every compile-time regression the ledger has seen "
          "was answered, not excused.")
    else:
        a("| workload | author | commit | reason |")
        a("|---|---|---|---|")
        seen = set()
        for s in sorted(sancs, key=lambda s: (s["workload"],
                                              s.get("reason") or "")):
            key = (s["workload"], s.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            a("| %s | %s | %s | %s |"
              % (s["workload"], s.get("author") or "unknown",
                 ("`%s`" % short(s["commit"])) if s.get("commit")
                 else "—",
                 (s.get("reason") or "").replace("|", "\\|")))
    a("")
    a("## Reading this dashboard")
    a("")
    a("- A record is one compile-time measurement pinned to one "
      "clean commit: the tool compiled every workload of the tracked "
      "suite through one instrument and kept the samples. The ledger "
      "is append-only and this document is a pure render of it (the "
      "sha256 above pins the bytes).")
    a("- The instrument is the compiler itself. `stage0` is the "
      "bootstrap chain (tools/hlinterp/boot.py running src/hlc.hls); `native` "
      "is bin/hlc. They time the same quantity through different "
      "machines and are never compared to each other.")
    a("- Every record pins the compiler's identity: the sha256 of the "
      "compiler source and of the binary timed. A native record "
      "whose binary was already filed against a different source is "
      "refused at record time (the stale-binary law) — bin/hlc must "
      "be rebuilt for the commit it measures.")
    a("- Classes come from Stage 124's law: Welch p < 0.05 for "
      "significance, the seeded bootstrap CI of the median change "
      "against the %.0f%% noise band. Non-significant changes are "
      "no-change; significant changes inside the band are "
      "within-noise." % DEFAULT_NOISE_PCT)
    a("- regressed or vanished workloads fail the gate unless a "
      "sanction names them. A sanction is a written excuse with an "
      "author — the register above is the audit trail.")
    a("")
    return "\n".join(lines)


def cmd_dashboard(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    doc = render_dashboard(entries, history, args.limit,
                           stamp=_utc_now() if args.stamp else None)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(doc)
    if args.json:
        v = verdict_of(entries, noise_pct=DEFAULT_NOISE_PCT)
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(json_dumps_sorted(v))
    print("dashboard written to %s" % args.out)
    return 0


# ---------------------------------------------------------------------------
# history — read the ledger back.
# ---------------------------------------------------------------------------

def cmd_history(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    shown = entries
    if args.workload:
        shown = [e for e in entries
                 if (e["type"] == "record"
                     and e["workloads"].get(args.workload))
                 or (e["type"] == "sanction"
                     and e["workload"] == args.workload)]
    if args.json:
        out = []
        for e in shown:
            e2 = {k: v for k, v in e.items() if not k.startswith("_")}
            out.append(e2)
        sys.stdout.write(json_dumps_sorted(out))
        return 0
    for e in shown:
        if e["type"] == "record":
            print("line %-4d record  %s  %-7s suite=%s workloads=%d"
                  % (e["_line"], e["commit"], e["instrument"],
                     os.path.basename(e["suite"].rstrip("/")),
                     len(e["workloads"])))
        else:
            print("line %-4d sanction %s  %s"
                  % (e["_line"], e["workload"], e["reason"]))
    print("%d entries" % len(shown))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="hls-ctime",
        description="Halis per-commit compile-time regression ledger "
                    "(Stage 138). The compiler is the instrument; "
                    "this tool remembers and judges.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("record", help="measure and pin one commit")
    p.add_argument("--root", default=None,
                   help="repository root the record is pinned from "
                        "(default: the tool's repo)")
    p.add_argument("--suite", default=DEFAULT_SUITE,
                   help="workload suite directory (default: %s)"
                        % DEFAULT_SUITE)
    p.add_argument("--instrument", default="stage0",
                   choices=list(INSTRUMENTS),
                   help="which compiler to time (default: stage0, the "
                        "bootstrap chain; native needs bin/hlc)")
    p.add_argument("--grep", default=None,
                   help="only compile workloads matching this substring")
    p.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    p.add_argument("--from-report", default=None, metavar="R.json",
                   help="import a machine report instead of measuring")
    p.add_argument("--commit", default=None,
                   help="pin this commit explicitly (the CI path)")
    p.add_argument("--note", default=None)
    p.add_argument("--allow-dirty", action="store_true",
                   help="record despite tracked modifications")
    p.add_argument("--stamp", action="store_true",
                   help="embed the wall-clock stamp in the entry")
    p.add_argument("--history", default=None)
    p.add_argument("--json", default=None)
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("sanction", help="excuse one workload, in writing")
    p.add_argument("--root", default=None,
                   help="repository root the author name is read from "
                        "(default: the tool's repo)")
    p.add_argument("workload")
    p.add_argument("--reason", default=None)
    p.add_argument("--commit", default=None)
    p.add_argument("--author", default=None)
    p.add_argument("--stamp", action="store_true")
    p.add_argument("--history", default=None)
    p.set_defaults(fn=cmd_sanction)

    p = sub.add_parser("gate", help="classify newest vs baseline; "
                                    "exit 1 on unsanctioned regression")
    p.add_argument("--baseline", default=None, metavar="SHA",
                   help="compare against this commit's record")
    p.add_argument("--threshold", type=float, default=DEFAULT_NOISE_PCT,
                   help="noise band in %% (default %g)"
                        % DEFAULT_NOISE_PCT)
    p.add_argument("--history", default=None)
    p.add_argument("--json", default=None)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("dashboard", help="render the ledger to "
                                         "markdown")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=DEFAULT_DASH_LIMIT)
    p.add_argument("--stamp", action="store_true")
    p.add_argument("--history", default=None)
    p.add_argument("--json", default=None)
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("history", help="list the ledger entries")
    p.add_argument("--workload", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--history", default=None)
    p.set_defaults(fn=cmd_history)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_usage(sys.stderr)
        return 2
    try:
        return args.fn(args)
    except HlCtimeError as exc:
        sys.stderr.write("hls-ctime: %s\n" % exc)
        return exc.exit


if __name__ == "__main__":
    sys.exit(main())
