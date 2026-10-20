#!/usr/bin/env python3
"""hls-perf — the Stage 136 per-commit performance regression dashboard.

Usage:
  python3 tools/hls-perf.py record    [--suite FILE] [--native] [--grep S]
                                      [--samples N] [--fixed-iters K]
                                      [--from-report R.json] [--commit SHA]
                                      [--note TEXT] [--allow-dirty]
                                      [--history PATH] [--json OUT]
  python3 tools/hls-perf.py sanction BENCH --reason TEXT [--commit SHA]
                                      [--author NAME] [--history PATH]
  python3 tools/hls-perf.py gate      [--baseline SHA] [--threshold PCT]
                                      [--history PATH] [--json OUT]
                                      [--quiet]
  python3 tools/hls-perf.py dashboard --out FILE.md [--limit N]
                                      [--history PATH] [--json OUT]
                                      [--stamp]
  python3 tools/hls-perf.py history   [--bench NAME] [--json]
                                      [--history PATH]

Roadmap Stage 136 is one phrase: "Performance regression dashboard
(per-commit)". Stage 124 built the instrument — hls-bench, the
criterion-style runner with Welch tests and seeded bootstrap CIs — but
an instrument is not a memory: every run starts from nothing, and a
regression has to be noticed by someone who remembers yesterday's
numbers. This stage adds the memory. hls-perf is a LEDGER over commits:

  1. a RECORD is one measurement pinned to one commit. `record` runs
     the REAL hls-bench over a suite (the tracked default is
     benchmarks/regression_watch.hls) and appends an entry keyed to
     `git rev-parse HEAD` — refused when the work tree carries tracked
     modifications, because a per-commit record that does not pin a
     clean commit is a rumour, not a measurement. `--from-report`
     imports a machine report instead of measuring (the CI path: the
     runner machine records, this machine files it), and then the
     commit must come from --commit or the ambient git;
  2. a SANCTION is a written excuse: one bench, one reason, one
     author. `sanction loop_sum --reason "switched to a correct
     algorithm; the constant factor was the price"` — a regression
     with a sanction is reported, not failed. A sanction without a
     reason is refused (an excuse nobody wrote down is not an excuse);
  3. the GATE is the law. `gate` classifies every bench in the newest
     record against the prior record of the SAME INSTRUMENT (same
     backend, byte-identical suite) with exactly Stage 124's law —
     Welch p < 0.05 for significance, the seeded bootstrap CI of the
     median change against the --threshold noise band (default 5%):
     no-change / within-noise / improved / regressed, plus new and
     vanished benches on either side of a rename. One unsanctioned
     regressed or vanished bench exits 1 naming it; sanctioned rows
     are loud but green;
  4. the DASHBOARD renders the ledger — PERFORMANCE.md with the
     verdict table, per-bench trend sparklines across the recorded
     commits, and the sanctions register. It is DETERMINISTIC: sorted
     walks, no wall-clock unless --stamp (and then only the Generated
     line), the sha256 ledger digest embedded — two renders of one
     ledger are byte-identical, so a hand-edited dashboard is a diff;

The history is a JSONL file (default .hlperf/history.jsonl, gitignored
— pass --history to point CI at a committed ledger). Append-only by
construction: `record` and `sanction` append, nothing rewrites, and a
line that does not parse is a named error, never a silent skip.

Exit codes:
  0  verified (gate pass, dashboard rendered, record filed)
  1  named violation (gate failure, dirty tree, corrupt line,
     instrument mismatch)
  2  usage / IO error (bad paths, empty ledger, unknown bench, a
     sanction without a reason)

Layering note: hls-bench (Stage 124) measures; hls-perf (this tool)
remembers and judges. The statistics are imported from hls-bench
itself — one implementation of Welch's test and the bootstrap, so the
runner's numbers and the ledger's verdicts can never drift apart.
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

TOOL_VERSION = "0.154.0"
TOOL = "hls-perf"
HISTORY_DEFAULT = os.path.join(".hlperf", "history.jsonl")
DEFAULT_SUITE = os.path.join("benchmarks", "regression_watch.hls")
DEFAULT_SAMPLES = 10
DEFAULT_FIXED_ITERS = 400
DEFAULT_NOISE_PCT = 5.0
DEFAULT_DASH_LIMIT = 12
MIN_SAMPLES = 2
BACKENDS = ("interpreter", "native")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The statistics are hls-bench's statistics — imported, not re-derived.
_spec = importlib.util.spec_from_file_location(
    "hls_bench", os.path.join(ROOT, "tools", "hls-bench.py"))
BENCH = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(BENCH)

_SPARK_BANDS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
_SPARK_MID = "\u2584"


class HlPerfError(Exception):
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


def classify(cur_est, base_est, bench_key, noise_pct):
    """Stage 124's classification law, verbatim: Welch p for
    significance, the seeded bootstrap CI of the median change against
    the noise band. The seed is derived from the bench key, so the
    SAME pair of sample sets always classifies the SAME way."""
    if len(cur_est) < MIN_SAMPLES or len(base_est) < MIN_SAMPLES:
        raise HlPerfError(
            "classification needs >= %d samples per side (bench %s)"
            % (MIN_SAMPLES, bench_key))
    p = BENCH.welch_p(cur_est, base_est)
    significant = p < BENCH._ALPHA
    cur_med = BENCH.median(cur_est)
    base_med = BENCH.median(base_est)
    if base_med <= 0:
        raise HlPerfError(
            "baseline median is not positive (bench %s)" % bench_key)
    point = (cur_med - base_med) / base_med * 100.0
    rng = random.Random(bench_key)
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
            "cur_median_ns": cur_med, "base_median_ns": base_med}


# ---------------------------------------------------------------------------
# The ledger — one JSON object per line, append-only.
# ---------------------------------------------------------------------------

def load_history(path):
    """Parse the ledger. A line that does not parse, an object without
    a known type, a record without a commit — every one is a NAMED
    error naming the line number; the ledger is never guessed around."""
    if not os.path.exists(path):
        raise HlPerfError("the history ledger %s does not exist yet "
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
                raise HlPerfError("history line %d is corrupt (%s) — "
                                  "the ledger is append-only; fix or "
                                  "remove the line by hand" % (i, exc))
            if not isinstance(obj, dict) or "type" not in obj:
                raise HlPerfError("history line %d carries no type" % i)
            if obj["type"] == "record":
                _validate_record(obj, "history line %d" % i)
            elif obj["type"] == "sanction":
                _validate_sanction(obj, "history line %d" % i)
            else:
                raise HlPerfError("history line %d has unknown type %r"
                                  % (i, obj["type"]))
            obj["_line"] = i
            entries.append(obj)
    return entries


def _validate_record(rec, where):
    if not rec.get("commit") or not is_sha1(rec["commit"]):
        raise HlPerfError("%s: a record without a 40-hex "
                          "commit pin is not a per-commit record" % where)
    if rec.get("backend") not in BACKENDS:
        raise HlPerfError("%s: backend %r is not one of %s"
                          % (where, rec.get("backend"), list(BACKENDS)))
    if not rec.get("suite_digest") or not is_sha256(rec["suite_digest"]):
        raise HlPerfError("%s: a record without a suite "
                          "digest cannot be compared" % where)
    benches = rec.get("benches")
    if not isinstance(benches, dict) or not benches:
        raise HlPerfError("%s: a record with no benches "
                          "measured nothing" % where)
    for name, b in sorted(benches.items()):
        est = b.get("estimates_ns")
        if not isinstance(est, list) or len(est) < MIN_SAMPLES:
            raise HlPerfError(
                "%s: bench %s carries fewer than %d "
                "samples — a median no sample supports is not a "
                "measurement" % (where, name, MIN_SAMPLES))
        if not all(isinstance(x, (int, float)) and x > 0 for x in est):
            raise HlPerfError(
                "%s: bench %s has non-positive estimates"
                % (where, name))


def _validate_sanction(s, where):
    if not s.get("bench"):
        raise HlPerfError("%s: a sanction without a bench" % where)
    if not s.get("reason"):
        raise HlPerfError("%s: a sanction without a "
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

def suite_digest(suite_path):
    try:
        with open(suite_path, "rb") as fh:
            return sha256_hex(fh.read())
    except OSError as exc:
        raise HlPerfError("cannot read the suite %s: %s"
                          % (suite_path, exc), exit=2)


def run_hls_bench(args):
    """The real instrument: hls-bench over the suite, --json into a
    temp file, raw samples included. Returns the parsed report."""
    tmp = tempfile.NamedTemporaryFile(
        prefix=".hlperf-report-", suffix=".json", delete=False)
    tmp.close()
    try:
        cmd = [sys.executable, os.path.join(ROOT, "tools", "hls-bench.py"),
               args.suite, "--json", tmp.name,
               "--samples", str(args.samples),
               "--fixed-iters", str(args.fixed_iters)]
        if args.native:
            cmd.append("--native")
        if args.grep:
            cmd += ["--grep", args.grep]
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, timeout=1800)
        if p.returncode != 0:
            tail = (p.stderr or p.stdout).strip().splitlines()
            detail = tail[-1] if tail else "(no output)"
            raise HlPerfError("the bench run failed (%d): %s"
                              % (p.returncode, detail))
        with open(tmp.name, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return report


def report_to_benches(report):
    """Extract the ledger's bench map from an hls-bench report: for
    every ok bench, the raw per-iteration estimates (elapsed/iters per
    sample — the distribution, not just the median) and the throughput
    units. Skipped benches are recorded by name; a FAILED bench fails
    the record (a dashboard built over half a suite is a lie)."""
    if report.get("tool") != "hls-bench":
        raise HlPerfError("the report is not an hls-bench report "
                          "(tool=%r)" % report.get("tool"), exit=2)
    backend = report.get("backend")
    if backend not in BACKENDS:
        raise HlPerfError("report backend %r is not one of %s"
                          % (backend, list(BACKENDS)), exit=2)
    benches = {}
    skipped = []
    failed = []
    for f in report.get("files", []):
        for b in f.get("benches", []):
            name = b.get("name", "?")
            if b.get("status") == "ok":
                raw = b.get("samples") or []
                est = [el / float(it) for (it, el) in raw
                       if it and el is not None]
                med = (b.get("estimates_ns_per_iter") or {}).get("median")
                if not est and med:
                    est = [float(med)]
                benches[name] = {"estimates_ns": est,
                                 "median_ns": med,
                                 "samples_count": len(est),
                                 "units": b.get("units")}
            elif b.get("status") == "skip":
                skipped.append(name)
            else:
                failed.append("%s (%s)"
                              % (name, (b.get("detail") or "").strip()
                                 .splitlines()[:1]))
    if failed:
        raise HlPerfError("the bench run carries failed benches: %s"
                          % ", ".join(failed))
    if not benches:
        raise HlPerfError("the report carries no measured benches",
                          exit=2)
    return backend, benches, skipped


def cmd_record(args):
    root = args.root or ROOT
    history = args.history or HISTORY_DEFAULT

    # The pinning law: every record names one clean commit. Explicit
    # --commit wins (the CI import path); otherwise the ambient git
    # must supply a HEAD, and a dirty tree is a refusal, not a note.
    if args.commit:
        commit = args.commit.strip().lower()
        if not is_sha1(commit):
            raise HlPerfError("--commit must be a 40-hex sha (got %r)"
                              % args.commit, exit=2)
    else:
        commit = git_head(root)
        if not commit:
            raise HlPerfError(
                "%s is not a git work tree — pass --commit to pin the "
                "record explicitly" % root, exit=2)
        dirty = git(["status", "--porcelain", "-uno"], root)
        if dirty and not args.allow_dirty:
            raise HlPerfError(
                "the work tree carries tracked modifications — a "
                "per-commit record pins a clean commit "
                "(--allow-dirty to record anyway)")

    digest = suite_digest(os.path.join(root, args.suite))

    if args.from_report:
        try:
            with open(args.from_report, "r", encoding="utf-8") as fh:
                report = json.load(fh)
        except OSError as exc:
            raise HlPerfError("cannot read the report: %s" % exc, exit=2)
        except ValueError as exc:
            raise HlPerfError("the report is not valid JSON: %s" % exc,
                              exit=2)
        source = "report %s" % args.from_report
    else:
        report = run_hls_bench(args)
        source = "hls-bench"

    backend, benches, skipped = report_to_benches(report)

    entry = {
        "type": "record",
        "tool": TOOL,
        "version": TOOL_VERSION,
        "commit": commit,
        "backend": backend,
        "suite": args.suite,
        "suite_digest": digest,
        "plan": {"mode": "fixed",
                 "samples": args.samples,
                 "iters": args.fixed_iters},
        "noise_pct": DEFAULT_NOISE_PCT,
        "env": _env_fingerprint(root),
        "benches": benches,
        "skipped": sorted(skipped),
        "note": args.note,
        "stamp": _utc_now() if args.stamp else None,
    }
    # The floor law bites HERE, at record time — a record that its
    # own ledger would refuse never enters the ledger.
    _validate_record(entry, "the new record")
    append_entry(history, entry)
    _write_report(args, report)
    print("recorded %s (%s) backend=%s suite=%s benches=%d source=%s"
          % (short(commit), commit, backend, args.suite, len(benches),
             source))
    for name in sorted(benches):
        b = benches[name]
        print("  %-24s median %s  (%d samples)"
              % (name, BENCH.fmt_time(b["median_ns"] or 0.0),
                 b["samples_count"]))
    if skipped:
        print("  skipped: %s" % ", ".join(sorted(skipped)))
    return 0


def _env_fingerprint(root):
    env = {"python": "%d.%d.%d" % sys.version_info[:3],
           "cpus": os.cpu_count() or 0}
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


# ---------------------------------------------------------------------------
# sanction — a written excuse, one bench at a time.
# ---------------------------------------------------------------------------

def cmd_sanction(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    known = sorted({n for r in records_of(entries)
                    for n in r["benches"]})
    if args.bench not in known:
        raise HlPerfError(
            "unknown bench %r — the ledger knows: %s"
            % (args.bench, ", ".join(known) if known else "(none)"),
            exit=2)
    if not args.reason:
        raise HlPerfError("a sanction without a written reason is not a "
                          "sanction (--reason TEXT)", exit=2)
    entry = {
        "type": "sanction",
        "tool": TOOL,
        "version": TOOL_VERSION,
        "bench": args.bench,
        "reason": args.reason,
        "commit": args.commit,
        "author": args.author or git_user(args.root or ROOT),
        "stamp": _utc_now() if args.stamp else None,
    }
    append_entry(history, entry)
    print("sanctioned %s (%s)" % (args.bench, args.author
                                  or git_user(args.root or ROOT)))
    return 0


# ---------------------------------------------------------------------------
# gate — the law: one unsanctioned regression fails.
# ---------------------------------------------------------------------------

def pick_baseline(new_rec, entries, want):
    """The instrument law: comparisons happen only within the same
    backend and byte-identical suite. Default baseline: the newest
    earlier record of that instrument."""
    recs = records_of(entries)
    idx = max(i for i, r in enumerate(recs) if r is new_rec)
    if want:
        cands = [r for r in recs if r["commit"].startswith(want)]
        if not cands:
            raise HlPerfError("no record carries commit prefix %r" % want,
                              exit=2)
        if len(cands) > 1:
            raise HlPerfError("commit prefix %r is ambiguous (%d "
                              "records)" % (want, len(cands)), exit=2)
        base = cands[0]
        if base is new_rec:
            raise HlPerfError("the baseline is the record itself — a "
                              "record does not regress against "
                              "itself", exit=2)
    else:
        earlier = [r for r in recs[:idx]
                   if r["backend"] == new_rec["backend"]
                   and r["suite_digest"] == new_rec["suite_digest"]]
        if not earlier:
            raise HlPerfError(
                "no earlier record of the same instrument to compare "
                "against (backend=%s suite_digest=%s) — record one first"
                % (new_rec["backend"], new_rec["suite_digest"][:12]),
                exit=2)
        base = earlier[-1]
    if base["backend"] != new_rec["backend"]:
        raise HlPerfError(
            "different instruments: the baseline is %s, the record is "
            "%s — interpreter and native timings are different "
            "instruments" % (base["backend"], new_rec["backend"]))
    if base["suite_digest"] != new_rec["suite_digest"]:
        raise HlPerfError(
            "the suite changed since the baseline (digest %s vs %s) — "
            "re-baseline, do not compare"
            % (base["suite_digest"][:12],
               new_rec["suite_digest"][:12]))
    return base


def verdict_of(entries, baseline=None, noise_pct=DEFAULT_NOISE_PCT):
    """Classify the newest record against the chosen baseline and
    apply the sanctions. Returns the full machine verdict."""
    recs = records_of(entries)
    if not recs:
        raise HlPerfError("the ledger carries no records — nothing to "
                          "gate", exit=2)
    new_rec = recs[-1]
    base_rec = pick_baseline(new_rec, entries, baseline)

    active = {}
    for s in sanctions_of(entries):
        active.setdefault(s["bench"], s)

    rows = []
    for name in sorted(set(new_rec["benches"])
                       | set(base_rec["benches"])):
        if name in new_rec["benches"] and name in base_rec["benches"]:
            c = classify(new_rec["benches"][name]["estimates_ns"],
                         base_rec["benches"][name]["estimates_ns"],
                         name, noise_pct)
            row = {"bench": name, "class": c["class"],
                   "p": c["p"], "point_pct": c["point_pct"],
                   "ci_pct": c["ci_pct"],
                   "cur_median_ns": c["cur_median_ns"],
                   "base_median_ns": c["base_median_ns"],
                   "samples": new_rec["benches"][name]["samples_count"]}
        elif name in new_rec["benches"]:
            row = {"bench": name, "class": "new",
                   "cur_median_ns":
                       new_rec["benches"][name]["median_ns"]}
        else:
            row = {"bench": name, "class": "vanished"}
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
        "backend": new_rec["backend"],
        "suite": new_rec["suite"],
        "noise_pct": noise_pct,
        "benches": rows,
        "sanctioned": sorted(r["bench"] for r in rows if "sanction" in r),
        "unsanctioned": sorted(r["bench"] for r in unsanctioned),
    }
    return verdict


def cmd_gate(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    v = verdict_of(entries, baseline=args.baseline,
                   noise_pct=args.threshold)
    if not args.quiet:
        print("gate %s: record %s vs baseline %s (backend %s, "
              "threshold %.1f%%)"
              % (v["verdict"].upper(), short(v["record"]),
                 short(v["baseline"]), v["backend"], v["noise_pct"]))
        for r in v["benches"]:
            line = "  %-24s" % r["bench"]
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
                    line += " (%s)" % BENCH.fmt_time(
                        r["cur_median_ns"] or 0.0)
            if "sanction" in r:
                line += ("  sanctioned: %s (%s)"
                         % (r["sanction"]["reason"],
                            r["sanction"].get("author")))
            print(line)
    if v["unsanctioned"]:
        print("GATE FAIL: %s regressed without a sanction: %s"
              % (len(v["unsanctioned"]), ", ".join(v["unsanctioned"])))
    elif not args.quiet:
        print("GATE PASS: no unsanctioned regression across %d benches"
              % len(v["benches"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(json_dumps_sorted(v))
    return 1 if v["unsanctioned"] else 0


# ---------------------------------------------------------------------------
# dashboard — the deterministic render of the ledger.
# ---------------------------------------------------------------------------

def sparkline(medians):
    """A per-bench trend across records, oldest to newest. The series
    is normalized to [min, max] over 8 bands; a constant series is a
    run of mid bands (a wall of max-height blocks would read as a
    regression that is not there). The band is INTEGER half-up math —
    ((v-lo)*14 + (hi-lo)) // (2*(hi-lo)) — the exact function the ok
    corpus fixture pins, so tool and fixture cannot drift."""
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
        raise HlPerfError("the ledger carries no records — nothing to "
                          "render", exit=2)
    try:
        with open(history_path, "rb") as fh:
            digest = sha256_hex(fh.read())
    except OSError as exc:
        raise HlPerfError("cannot read the ledger: %s" % exc, exit=2)

    new_rec = recs[-1]
    base = None
    verdict = None
    try:
        verdict = verdict_of(entries, noise_pct=DEFAULT_NOISE_PCT)
        base = pick_baseline(new_rec, entries, None)
    except HlPerfError:
        verdict = None
        base = None

    bench_names = sorted(set(new_rec["benches"]))

    shown = recs[-limit:] if limit and limit > 0 else recs

    lines = []
    a = lines.append
    a("# HLS performance dashboard")
    a("")
    a("- Tool: %s %s" % (TOOL, TOOL_VERSION))
    a("- Ledger: `%s` (sha256 `%s`)" % (history_path, digest))
    a("- Records: %d (showing %d)" % (len(recs), len(shown)))
    a("- Newest record: `%s` (%d benches)"
      % (new_rec["commit"], len(new_rec["benches"])))
    if base is not None:
        a("- Baseline: `%s`" % base["commit"])
    else:
        a("- Baseline: none (no earlier record of this instrument)")
    if verdict is not None:
        a("- Gate verdict: **%s**" % verdict["verdict"])
        if verdict["unsanctioned"]:
            a("- Unsanctioned: %s" % ", ".join(verdict["unsanctioned"]))
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
        a("| bench | base | now | change | 95% CI | p | class |")
        a("|---|---|---|---|---|---|---|")
        for r in verdict["benches"]:
            if r["class"] in ("no-change", "within-noise", "improved",
                              "regressed"):
                lo, hi = r["ci_pct"]
                a("| %s | %s | %s | %s | [%s %s] | %.4f | %s |"
                  % (r["bench"], BENCH.fmt_time(r["base_median_ns"]),
                     BENCH.fmt_time(r["cur_median_ns"]),
                     BENCH.fmt_pct(r["point_pct"]),
                     BENCH.fmt_pct(lo), BENCH.fmt_pct(hi), r["p"],
                     r["class"] + (" (sanctioned)"
                                   if "sanction" in r else "")))
            elif r["class"] == "new":
                a("| %s | — | %s | — | — | — | new |"
                  % (r["bench"],
                     BENCH.fmt_time(r["cur_median_ns"] or 0.0)))
            else:
                a("| %s | %s | — | — | — | — | vanished |"
                  % (r["bench"],
                     BENCH.fmt_time(r.get("base_median_ns") or 0.0)))
    a("")
    a("## Trends (median ns/iter across records, oldest to newest)")
    a("")
    a("| bench | trend | %s |" % " | ".join(
        short(r["commit"], 7) for r in shown))
    a("|---|---|%s|" % "---|" * len(shown))
    for name in bench_names:
        series = []
        for r in shown:
            b = r["benches"].get(name)
            series.append(b["median_ns"] if b else None)
        known = [m for m in series if m]
        spark = sparkline(known) if known else "-"
        cells = " | ".join(
            (BENCH.fmt_time(m) if m else "—") for m in series)
        a("| %s | %s | %s |" % (name, spark, cells))
    a("")
    a("## Sanctions")
    a("")
    sancs = sanctions_of(entries)
    if not sancs:
        a("None. Every regression the ledger has seen was answered, "
          "not excused.")
    else:
        a("| bench | author | commit | reason |")
        a("|---|---|---|---|")
        seen = set()
        for s in sorted(sancs, key=lambda s: (s["bench"],
                                              s.get("reason") or "")):
            key = (s["bench"], s.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            a("| %s | %s | %s | %s |"
              % (s["bench"], s.get("author") or "unknown",
                 ("`%s`" % short(s["commit"])) if s.get("commit")
                 else "—",
                 (s.get("reason") or "").replace("|", "\\|")))
    a("")
    a("## Reading this dashboard")
    a("")
    a("- A record is one hls-bench measurement pinned to one clean "
      "commit; the ledger is append-only and this document is a pure "
      "render of it (the sha256 above pins the bytes).")
    a("- Classes come from Stage 124's law: Welch p < 0.05 for "
      "significance, the seeded bootstrap CI of the median change "
      "against the %.0f%% noise band. Non-significant changes are "
      "no-change; significant changes inside the band are "
      "within-noise." % DEFAULT_NOISE_PCT)
    a("- regressed or vanished benches fail the gate unless a "
      "sanction names them. A sanction is a written excuse with an "
      "author — the register above is the audit trail.")
    a("- Interpreter and native timings are different instruments; "
      "the ledger never compares across backends or across suite "
      "digests.")
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


def _write_report(args, report):
    """--json on record: file the raw instrument report beside the
    ledger entry (what CI keeps when the full report matters)."""
    if not args.json:
        return
    with open(args.json, "w", encoding="utf-8") as fh:
        fh.write(json_dumps_sorted(report))


# ---------------------------------------------------------------------------
# history — read the ledger back.
# ---------------------------------------------------------------------------

def cmd_history(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    shown = entries
    if args.bench:
        shown = [e for e in entries
                 if (e["type"] == "record" and e["benches"].get(args.bench))
                 or (e["type"] == "sanction" and e["bench"] == args.bench)]
    if args.json:
        out = []
        for e in shown:
            e2 = {k: v for k, v in e.items() if not k.startswith("_")}
            out.append(e2)
        sys.stdout.write(json_dumps_sorted(out))
        return 0
    for e in shown:
        if e["type"] == "record":
            print("line %-4d record  %s  %-11s suite=%s benches=%d"
                  % (e["_line"], e["commit"], e["backend"],
                     os.path.basename(e["suite"]), len(e["benches"])))
        else:
            print("line %-4d sanction %s  %s"
                  % (e["_line"], e["bench"], e["reason"]))
    print("%d entries" % len(shown))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="hls-perf",
        description="Halis per-commit performance regression ledger "
                    "(Stage 136). hls-bench measures; hls-perf "
                    "remembers and judges.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("record", help="measure and pin one commit")
    p.add_argument("--root", default=None,
                   help="repository root the record is pinned from "
                        "(default: the tool's repo)")
    p.add_argument("--suite", default=DEFAULT_SUITE,
                   help="bench suite file (default: %s)" % DEFAULT_SUITE)
    p.add_argument("--native", action="store_true",
                   help="measure the native backend (bin/hlc + gcc)")
    p.add_argument("--grep", default=None,
                   help="only run benches matching this substring")
    p.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    p.add_argument("--fixed-iters", type=int, default=DEFAULT_FIXED_ITERS)
    p.add_argument("--from-report", default=None, metavar="R.json",
                   help="import an hls-bench --json report instead of "
                        "measuring")
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

    p = sub.add_parser("sanction", help="excuse one bench, in writing")
    p.add_argument("--root", default=None,
                   help="repository root the author name is read from "
                        "(default: the tool's repo)")
    p.add_argument("bench")
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
    p.add_argument("--bench", default=None)
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
    except HlPerfError as exc:
        sys.stderr.write("hls-perf: %s\n" % exc)
        return exc.exit


if __name__ == "__main__":
    sys.exit(main())
