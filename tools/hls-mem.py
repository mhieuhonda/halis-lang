#!/usr/bin/env python3
"""hls-mem — the Stage 137 memory regression dashboard (RSS over time).

Usage:
  python3 tools/hls-mem.py record    [--suite DIR] [--samples N]
                                      [--cycles C] [--warmup W]
                                      [--interval MS]
                                      [--from-bundle B.json] [--commit SHA]
                                      [--note TEXT] [--allow-dirty]
                                      [--history PATH] [--json OUT]
  python3 tools/hls-mem.py sanction WORKLOAD --reason TEXT [--commit SHA]
                                      [--author NAME] [--history PATH]
  python3 tools/hls-mem.py gate      [--baseline SHA] [--threshold PCT]
                                      [--floor PAGES] [--history PATH]
                                      [--json OUT] [--quiet]
  python3 tools/hls-mem.py dashboard --out FILE.md [--limit N]
                                      [--history PATH] [--json OUT]
                                      [--stamp]
  python3 tools/hls-mem.py history   [--workload NAME] [--json]
                                      [--history PATH]

Roadmap Stage 137 is one phrase: "Memory regression dashboard (RSS
over time)". Stage 125 built the instrument — hls-rss, the
two-instrument verifier that samples /proc/<pid>/statm, fits a
Theil-Sen slope against the cycle markers and counts the malloc
balance through the link-time interposer — but an instrument is not a
history: it certifies ONE workload at ONE moment, and "the runtime
got heavier" is a sentence about commit-to-commit drift that no
single run can pronounce. Stage 136 gave TIME its ledger; this stage
gives MEMORY its own. hls-mem is a LEDGER over commits:

  1. a RECORD is one measurement pinned to one commit. `record` runs
     the REAL hls-rss over every workload of the tracked suite (the
     tracked default is benchmarks/rss_watch/ — four deterministic,
     allocation-shaped canaries: the resident-set floor, string
     growth, heap churn, boxed objects) --samples times each, and
     appends an entry keyed to `git rev-parse HEAD` — refused when
     the work tree carries tracked modifications, because a
     per-commit record that does not pin a clean commit is a rumour,
     not a measurement. `--from-bundle` imports a machine bundle
     instead of measuring (the CI path: the runner machine records,
     this machine files it); the bundle may carry the measuring
     machine's env fingerprint, and when it does it wins — the record
     describes the machine that measured, not the machine that files;

  2. the FLOOR LAW bites at record time: every run of every workload
     must certify STABLE under hls-rss before it can enter the
     ledger — a canary that leaks (or that cannot certify) is a bug,
     not a record ("file a bug, not a record"); a workload with
     fewer than two samples is refused ("a median no sample supports
     is not a measurement"); a record without its page size is
     refused — a record without its unit is not a record;

  3. the MEASUREMENT LAW: the instrument runs with address-space
     randomization DISABLED (setarch -R, inherited through exec into
     the compiled workload) whenever the platform provides setarch
     — the resident set must reflect the code, not the loader's
     dice, and the env fingerprint records which mode ran (aslr:
     fixed / random). The metric is the MARKER MEDIAN — the median
     resident set over the measured phase's cycle markers — not a
     sampled maximum: a max is a lottery over transient spikes (the
     runtime's background threads touch stack pages that recede,
     and the kernel's VmHWM remembers them forever; neither is the
     workload's footprint), while the marker median is the steady
     level the workload actually stands on. The tracked suite is
     DESIGNED so its peak lives at its markers — each workload
     holds its working set live across the marker line — so the
     marker median IS the footprint;

  4. the CLASSIFICATION LAW is the page law. The marker-median
     samples are INTEGER PAGES and that distribution is often
     degenerate (with the layout pinned, every run of one binary
     lands on the same pages; Welch's t-test returns p=1 when both
     variances are zero and would call a deterministic thirty-page
     growth "no change"), so significance here is the SEEDED
     BOOTSTRAP CI itself: a change is significant when no resample
     of the two distributions can bring the medians together (the
     CI excludes zero), and the noise band is floored in PAGES —
     max(--threshold percent of baseline, --floor pages, default 16)
     — because a percent of a small footprint is a fraction of the
     page RSS moves in. Significant and beyond the band: regressed;
     beyond the band downward: improved; significant but inside the
     band: within-noise; not significant: no-change. A workload that
     appears is new (reported, never failed); one that disappears is
     vanished and fails the gate BY NAME — silently losing a canary
     would hide the leak it was watching for;

  5. the UNIT LAW: a page is a unit. Comparisons happen only within
     one byte-identical suite digest AND one page size — a baseline
     measured in 8192-byte pages is a different instrument from a
     record in 4096-byte pages, exactly as interpreter timings are a
     different instrument from native ones;

  6. a SANCTION is a written excuse: one workload, one reason, one
     author. A regression with a sanction is reported, not failed. A
     sanction without a reason is refused (an excuse nobody wrote
     down is not an excuse);

  7. the GATE is the law. `gate` classifies every workload in the
     newest record against the prior record of the same instrument
     and exits 1 naming any unsanctioned regressed or vanished
     workload; sanctioned rows are loud but green;

  8. the DASHBOARD renders the ledger — MEMORY.md with the verdict
     table, the resident-set-at-markers trend (sparklines across
     the recorded commits — "RSS over time" is literally this
     table), the slope watch (pages per 1000 cycles against
     hls-rss's 0.25 page/cycle leak threshold) and the sanctions
     register. It is DETERMINISTIC: sorted walks, no wall-clock
     unless --stamp (and then only the Generated line), the sha256
     ledger digest embedded — two renders of one ledger are
     byte-identical, so a hand-edited dashboard is a diff.

The history is a JSONL file (default .hlmem/history.jsonl, gitignored
— pass --history to point CI at a committed ledger). Append-only by
construction: `record` and `sanction` append, nothing rewrites, and a
line that does not parse is a named error, never a silent skip.

Exit codes:
  0  verified (gate pass, dashboard rendered, record filed)
  1  named violation (gate failure, dirty tree, corrupt line, a
     workload that did not certify stable, a unit mismatch)
  2  usage / IO error (bad paths, empty ledger, unknown workload, a
     sanction without a reason, an unusable instrument)

Layering note: hls-rss (Stage 125) measures; hls-mem (this tool)
remembers and judges. The page size, the quantile estimator and the
median are imported from hls-rss itself — one definition of the unit
and its statistics — and the bootstrap width is hls-bench's
_BOOTSTRAP_B (2000), the same width every dashboard in this tree
uses, so the instrument's pages and the ledger's verdicts can never
drift apart.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

TOOL_VERSION = "0.155.0"
TOOL = "hls-mem"
HISTORY_DEFAULT = os.path.join(".hlmem", "history.jsonl")
DEFAULT_SUITE = os.path.join("benchmarks", "rss_watch")
DEFAULT_SAMPLES = 5
DEFAULT_CYCLES = 200
DEFAULT_INTERVAL = 10.0
DEFAULT_NOISE_PCT = 5.0
DEFAULT_FLOOR_PAGES = 16
DEFAULT_DASH_LIMIT = 12
MIN_SAMPLES = 2
# hls-rss's own leak threshold (pages/cycle), restated for the
# dashboard's slope watch table — the instrument's law, not a new one.
LEAK_SLOPE_THRESHOLD = 0.25
BUNDLE_TOOL = "hls-mem-bundle"
VERDICTS = ("stable",)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The unit and its statistics are hls-rss's — imported, not re-derived.
_spec = importlib.util.spec_from_file_location(
    "hls_rss", os.path.join(ROOT, "tools", "hls-rss.py"))
RSS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RSS)

# The bootstrap width is hls-bench's — the same 2000 resamples every
# dashboard in this tree draws, so a CI here means what a CI means
# over in PERFORMANCE.md.
_spec2 = importlib.util.spec_from_file_location(
    "hls_bench", os.path.join(ROOT, "tools", "hls-bench.py"))
BENCH = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(BENCH)

PAGE_SIZE = RSS._PAGE_SIZE
_BOOTSTRAP_B = BENCH._BOOTSTRAP_B

_SPARK_BANDS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
_SPARK_MID = "\u2584"


class HlMemError(Exception):
    """A named refusal. .exit is the process exit code it deserves."""

    def __init__(self, msg, exit=1):
        super().__init__(msg)
        self.exit = exit


# ---------------------------------------------------------------------------
# Small helpers — bytes, digests, json, the git surface, page formatting.
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


def median_of(xs):
    return RSS._median(sorted(xs))


def _is_pos_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ---------------------------------------------------------------------------
# Formatting — pages first, percents second.
# ---------------------------------------------------------------------------

def fmt_bytes(nbytes):
    if nbytes >= 1048576:
        return "%.1f MiB" % (nbytes / 1048576.0)
    return "%d KiB" % (nbytes // 1024)


def fmt_pg(n):
    return "%d pg" % n


def fmt_pg_human(n):
    """Pages and bytes side by side — RSS is page-granular; the KiB is
    what a human compares against top(1)."""
    return "%d pg (%s)" % (n, fmt_bytes(n * PAGE_SIZE))


def fmt_change(point_pages, point_pct):
    return "%+d pg (%s)" % (point_pages, BENCH.fmt_pct(point_pct))


def fmt_ci_pages(lo, hi):
    return "[%+.1f %+.1f] pg" % (lo, hi)


def fmt_band(band):
    return "%.1f pg" % band


def fmt_slope_kc(v):
    """A slope in pages per 1000 cycles — the scale a human reads
    (0.0134 page/cycle is 13.4 pg/kc, not a number anyone can feel)."""
    s = "%.1f" % (v * 1000.0)
    return ("+" + s) if v > 0 else s


# ---------------------------------------------------------------------------
# The classification law — the page law.
# ---------------------------------------------------------------------------

def classify(cur_est, base_est, workload_key, noise_pct, floor_pages):
    """The memory classification: the seeded bootstrap CI of the median
    marker-RSS change against the floored band. Significance is the CI
    ITSELF excluding zero — no Welch, deliberately: RSS samples are
    integer pages and the run-to-run distribution is often degenerate
    (every run of one binary lands on the same pages), and Welch's
    t-test answers p=1.0 when both variances are zero — it would call
    a deterministic thirty-page growth "no change". The page law says
    what the data says: when no resample can bring the medians
    together, the change is real. The seed is derived from the
    workload key, so the SAME pair of sample sets always classifies
    the SAME way."""
    if len(cur_est) < MIN_SAMPLES or len(base_est) < MIN_SAMPLES:
        raise HlMemError(
            "classification needs >= %d samples per side (workload %s)"
            % (MIN_SAMPLES, workload_key))
    cur_med = median_of(cur_est)
    base_med = median_of(base_est)
    if base_med <= 0:
        raise HlMemError(
            "baseline median is not positive (workload %s)" % workload_key)
    band = max(float(floor_pages), base_med * noise_pct / 100.0)
    point_pages = cur_med - base_med
    point_pct = point_pages / base_med * 100.0
    rng = random.Random(workload_key)
    nb = len(base_est)
    diffs = []
    for _ in range(_BOOTSTRAP_B):
        cs = [cur_est[rng.randrange(len(cur_est))]
              for _ in range(len(cur_est))]
        bs = [base_est[rng.randrange(nb)] for _ in range(nb)]
        diffs.append(median_of(cs) - median_of(bs))
    diffs.sort()
    lo = RSS.quantile(diffs, 0.025)
    hi = RSS.quantile(diffs, 0.975)
    significant = lo > 0.0 or hi < 0.0
    if not significant:
        klass = "no-change"
    elif lo > band:
        klass = "regressed"
    elif hi < -band:
        klass = "improved"
    else:
        klass = "within-noise"
    return {"class": klass,
            "point_pages": point_pages, "point_pct": point_pct,
            "ci_pages": [lo, hi], "band_pages": band,
            "cur_median_pages": cur_med, "base_median_pages": base_med}


# ---------------------------------------------------------------------------
# The ledger — one JSON object per line, append-only.
# ---------------------------------------------------------------------------

def load_history(path):
    """Parse the ledger. A line that does not parse, an object without
    a known type, a record without a commit — every one is a NAMED
    error naming the line number; the ledger is never guessed around."""
    if not os.path.exists(path):
        raise HlMemError("the history ledger %s does not exist yet "
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
                raise HlMemError("history line %d is corrupt (%s) — "
                                  "the ledger is append-only; fix or "
                                  "remove the line by hand" % (i, exc))
            if not isinstance(obj, dict) or "type" not in obj:
                raise HlMemError("history line %d carries no type" % i)
            if obj["type"] == "record":
                _validate_record(obj, "history line %d" % i)
            elif obj["type"] == "sanction":
                _validate_sanction(obj, "history line %d" % i)
            else:
                raise HlMemError("history line %d has unknown type %r"
                                  % (i, obj["type"]))
            obj["_line"] = i
            entries.append(obj)
    return entries


def _validate_record(rec, where):
    if not rec.get("commit") or not is_sha1(rec["commit"]):
        raise HlMemError("%s: a record without a 40-hex "
                          "commit pin is not a per-commit record" % where)
    if not rec.get("suite") or not isinstance(rec["suite"], str):
        raise HlMemError("%s: a record without its suite path cannot be "
                          "re-baselined honestly" % where)
    if not rec.get("suite_digest") or not is_sha256(rec["suite_digest"]):
        raise HlMemError("%s: a record without a suite "
                          "digest cannot be compared" % where)
    plan = rec.get("plan")
    if not isinstance(plan, dict) \
            or not all(k in plan for k in
                       ("cycles", "warmup", "interval", "samples")):
        raise HlMemError("%s: a record without its pinned measurement "
                          "plan cannot be judged" % where)
    env = rec.get("env")
    if not isinstance(env, dict) or not _is_pos_int(env.get("page_size")):
        raise HlMemError("%s: a record without its page size is a "
                          "record without its unit" % where)
    if not _is_num(rec.get("noise_pct")) or not _is_num(rec.get("floor_pages")) \
            or rec["floor_pages"] < 0:
        raise HlMemError("%s: a record without its noise band (percent "
                          "and page floor) cannot classify anything" % where)
    workloads = rec.get("workloads")
    if not isinstance(workloads, dict) or not workloads:
        raise HlMemError("%s: a record with no workloads "
                          "measured nothing" % where)
    for name, w in sorted(workloads.items()):
        mr = w.get("marker_rss_pages")
        if not isinstance(mr, list) or len(mr) < MIN_SAMPLES:
            raise HlMemError(
                "%s: workload %s carries fewer than %d samples — a "
                "median no sample supports is not a measurement"
                % (where, name, MIN_SAMPLES))
        if not all(_is_pos_int(x) for x in mr):
            raise HlMemError(
                "%s: workload %s has a non-positive marker reading"
                % (where, name))
        for key in ("end_rss_pages", "warmup_end_rss_pages"):
            xs = w.get(key)
            if not isinstance(xs, list) or len(xs) != len(mr) \
                    or not all(_is_pos_int(x) for x in xs):
                raise HlMemError(
                    "%s: workload %s carries %s readings that do not "
                    "match its %d marker samples"
                    % (where, name, key.replace("_pages", ""), len(mr)))
        slopes = w.get("slope_pages_per_cycle")
        cis = w.get("slope_ci_pages")
        if not isinstance(slopes, list) or len(slopes) != len(mr) \
                or not all(_is_num(x) for x in slopes):
            raise HlMemError(
                "%s: workload %s carries slopes that do not match its "
                "samples" % (where, name))
        if not isinstance(cis, list) or len(cis) != len(mr) \
                or not all(isinstance(c, list) and len(c) == 2
                           and _is_num(c[0]) and _is_num(c[1])
                           and c[0] <= c[1] for c in cis):
            raise HlMemError(
                "%s: workload %s carries slope intervals that do not "
                "match its samples" % (where, name))
        if w.get("hls_rss_verdict") != "stable":
            raise HlMemError(
                "%s: workload %s did not certify stable under hls-rss — "
                "the floor law refuses it (file a bug, not a record)"
                % (where, name))


def _validate_sanction(s, where):
    if not s.get("workload"):
        raise HlMemError("%s: a sanction without a workload" % where)
    if not s.get("reason"):
        raise HlMemError("%s: a sanction without a "
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


def page_size_of(rec):
    return (rec.get("env") or {}).get("page_size")


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
        raise HlMemError("the suite directory %s does not exist" % suite,
                          exit=2)
    out = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".hls"):
            out.append((fn[:-len(".hls")], os.path.join(d, fn)))
    if not out:
        raise HlMemError("the suite directory %s carries no .hls "
                          "workloads" % suite, exit=2)
    return out


def members_digest(members):
    """sha256 over the sorted (name, bytes) pairs — names included, so
    a rename is a change, not a coincidence."""
    h = hashlib.sha256()
    for name, path in members:
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError as exc:
            raise HlMemError("cannot read the workload %s: %s"
                              % (path, exc), exit=2)
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(blob)
        h.update(b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# The instrument — run the REAL hls-rss, read its REAL report.
# ---------------------------------------------------------------------------

def run_hls_rss(workload_path, plan):
    """The real instrument: hls-rss over one workload, --json into a
    temp file. The invocation runs under `setarch -R` when the
    platform provides it — ADDR_NO_RANDOMIZE is inherited through
    exec into the compiled workload, pinning the address-space
    layout so the resident set reflects the code, not the loader's
    dice (the env fingerprint records which mode ran). Exit 2 from
    the instrument is an environment refusal (no hlc, no /proc) and
    propagates as usage; exit 1 is a measurement refusal — the
    workload did not certify STABLE — and propagates as a named
    violation: file a bug, not a record."""
    tmp = tempfile.NamedTemporaryFile(
        prefix=".hlmem-report-", suffix=".json", delete=False)
    tmp.close()
    try:
        cmd = [sys.executable, os.path.join(ROOT, "tools", "hls-rss.py"),
               os.path.abspath(workload_path), "--json", tmp.name,
               "--cycles", str(plan["cycles"]),
               "--warmup", str(plan["warmup"]),
               "--interval", str(plan["interval"])]
        if shutil.which("setarch"):
            cmd = ["setarch", "-R"] + cmd
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True,
                           cwd=ROOT, timeout=1800)
        if p.returncode != 0:
            tail = (p.stderr or p.stdout).strip().splitlines()
            detail = tail[-1] if tail else "(no output)"
            if p.returncode == 2:
                raise HlMemError(
                    "the instrument refused to run %s: %s"
                    % (os.path.basename(workload_path), detail), exit=2)
            raise HlMemError(
                "the workload %s did not certify STABLE (hls-rss exit "
                "%d): %s — file a bug, not a record"
                % (os.path.basename(workload_path), p.returncode, detail))
        with open(tmp.name, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return report


def report_readings(report, where):
    """Extract the ledger's per-run readings from one hls-rss --json
    report: the MARKER MEDIAN — the median resident pages over the
    measured phase's cycle markers (the steady level the workload
    stands on; the tracked suite holds its peak live at its markers,
    so this IS the footprint) — with the warmup-end RSS, the end RSS
    and the timeline instrument's slope with its CI alongside. The
    floor law bites here: a report whose verdict is not stable never
    becomes readings."""
    if not isinstance(report, dict) or report.get("tool") != "hls-rss":
        raise HlMemError("%s: not an hls-rss report (tool=%r)"
                          % (where, (report or {}).get("tool")
                             if isinstance(report, dict) else report),
                          exit=2)
    if report.get("verdict") != "stable":
        raise HlMemError(
            "%s: the run certified %r, not stable — the floor law "
            "refuses it (file a bug, not a record)"
            % (where, report.get("verdict")))
    run_a = report.get("run_a")
    if not isinstance(run_a, dict):
        raise HlMemError("%s: the report carries no run" % where, exit=2)
    timeline = run_a.get("timeline") or []
    cps = run_a.get("cycle_points") or []
    if not isinstance(timeline, list) or not isinstance(cps, list):
        raise HlMemError("%s: the report's traces are malformed" % where,
                          exit=2)
    for pt in timeline:
        if not isinstance(pt, list) or len(pt) != 2 \
                or not _is_pos_int(pt[1]):
            raise HlMemError("%s: a timeline point is malformed" % where,
                              exit=2)
    for pt in cps:
        if not isinstance(pt, list) or len(pt) != 3 \
                or not _is_pos_int(pt[2]):
            raise HlMemError("%s: a cycle point is malformed" % where,
                              exit=2)
    warmup = (report.get("config") or {}).get("warmup")
    if not _is_pos_int(warmup):
        raise HlMemError("%s: the report carries no warmup plan" % where,
                          exit=2)
    if not cps and not timeline:
        raise HlMemError("%s: the report carries no RSS readings" % where,
                          exit=2)
    # The marker median: cycle-aligned pages over the MEASURED phase
    # (the warmup split the instrument itself defines); a report in
    # time mode (no markers) falls back to the timeline's pages —
    # honestly weaker, and labelled so by the instrument.
    if cps:
        marker_pages = [pt[2] for pt in cps if pt[0] >= warmup]
        if not marker_pages:
            marker_pages = [pt[2] for pt in cps]
    else:
        marker_pages = [pt[1] for pt in timeline]
    marker_rss = int(median_of(marker_pages))
    if marker_rss <= 0:
        raise HlMemError("%s: the marker median is not positive" % where,
                          exit=2)
    warm_pts = [pt[2] for pt in cps if pt[0] < warmup]
    if warm_pts:
        warmup_end = warm_pts[-1]
    elif timeline:
        warmup_end = timeline[0][1]
    else:
        warmup_end = marker_rss
    end_rss = cps[-1][2] if cps else \
        (timeline[-1][1] if timeline else marker_rss)
    slope_blk = report.get("slope")
    slope = slope_blk.get("slope") if isinstance(slope_blk, dict) else None
    ci = slope_blk.get("ci") if isinstance(slope_blk, dict) else None
    if not _is_num(slope) \
            or not (isinstance(ci, list) and len(ci) == 2
                    and _is_num(ci[0]) and _is_num(ci[1])):
        raise HlMemError("%s: a stable report without its slope is not "
                          "a reading" % where, exit=2)
    return {"marker_rss_pages": marker_rss,
            "end_rss_pages": end_rss,
            "warmup_end_rss_pages": warmup_end,
            "slope_pages_per_cycle": float(slope),
            "slope_ci_pages": [float(ci[0]), float(ci[1])]}


def measure_bundle(members, plan):
    """Run the real instrument over every workload, --samples times
    each. Returns the measurement bundle: the artifact another
    machine could re-file with --from-bundle."""
    bundle = {"tool": BUNDLE_TOOL, "version": TOOL_VERSION,
              "workloads": {}, "env": None}
    for name, path in members:
        reports = []
        for i in range(plan["samples"]):
            reports.append(run_hls_rss(path, plan))
        bundle["workloads"][name] = reports
        # live feedback — the default plan takes a minute, and silence
        # is not feedback. The line mirrors the dashboard's own units.
        readings = [report_readings(r, "workload %s run %d"
                                    % (name, j + 1))
                    for j, r in enumerate(reports)]
        print("  measured %-16s marker-rss %s  slope %s pg/kc  "
              "(%d samples)"
              % (name, fmt_pg(median_of([r["marker_rss_pages"]
                                         for r in readings])),
                 fmt_slope_kc(median_of([r["slope_pages_per_cycle"]
                                         for r in readings])),
                 plan["samples"]))
    return bundle


# ---------------------------------------------------------------------------
# record — measure now, or import a bundle, and pin it to a commit.
# ---------------------------------------------------------------------------

def _env_fingerprint(root):
    env = {"python": "%d.%d.%d" % sys.version_info[:3],
           "cpus": os.cpu_count() or 0,
           "page_size": PAGE_SIZE,
           # The measurement law: setarch -R pins the address-space
           # layout when the platform provides it — the fingerprint
           # records which mode the instrument ran in.
           "aslr": "fixed" if shutil.which("setarch") else "random"}
    cc = os.environ.get("CC", "gcc")
    try:
        cc_ver = subprocess.run([cc, "--version"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                timeout=30)
        if cc_ver.returncode == 0 and cc_ver.stdout:
            env["cc"] = cc_ver.stdout.strip().splitlines()[0]
        else:
            env["cc"] = cc
    except (OSError, subprocess.TimeoutExpired):
        env["cc"] = cc
    kernel = None
    try:
        with open("/proc/sys/kernel/osrelease", "r",
                  encoding="utf-8") as fh:
            kernel = fh.read().strip() or None
    except OSError:
        kernel = None
    env["kernel"] = kernel
    return env


def _merge_env(local, bundle_env, where):
    """The bundle may carry the measuring machine's env fingerprint;
    when it does it wins — the record describes the machine that
    measured, not the machine that files. The page size is not
    optional in either voice: a record without its unit is not a
    record."""
    if bundle_env is None:
        return local
    if not isinstance(bundle_env, dict):
        raise HlMemError("%s: the bundle env is not an object" % where,
                          exit=2)
    env = dict(local)
    env.update(bundle_env)
    if not _is_pos_int(env.get("page_size")):
        raise HlMemError("%s: the bundle env carries no positive "
                          "page_size — a record without its unit is not "
                          "a record" % where, exit=2)
    return env


def cmd_record(args):
    root = args.root or ROOT
    history = args.history or HISTORY_DEFAULT

    # The pinning law: every record names one clean commit. Explicit
    # --commit wins (the CI import path); otherwise the ambient git
    # must supply a HEAD, and a dirty tree is a refusal, not a note.
    if args.commit:
        commit = args.commit.strip().lower()
        if not is_sha1(commit):
            raise HlMemError("--commit must be a 40-hex sha (got %r)"
                              % args.commit, exit=2)
    else:
        commit = git_head(root)
        if not commit:
            raise HlMemError(
                "%s is not a git work tree — pass --commit to pin the "
                "record explicitly" % root, exit=2)
        dirty = git(["status", "--porcelain", "-uno"], root)
        if dirty and not args.allow_dirty:
            raise HlMemError(
                "the work tree carries tracked modifications — a "
                "per-commit record pins a clean commit "
                "(--allow-dirty to record anyway)")

    if args.samples < MIN_SAMPLES:
        raise HlMemError("--samples must be at least %d — a median no "
                          "sample supports is not a measurement"
                          % MIN_SAMPLES, exit=2)
    if args.cycles < 3:
        raise HlMemError("--cycles must be at least 3 (a slope needs "
                          "points)", exit=2)
    warmup = args.warmup if args.warmup is not None \
        else max(1, args.cycles // 4)
    if warmup < 1:
        raise HlMemError("--warmup must be at least 1", exit=2)
    if args.interval < 1.0:
        raise HlMemError("--interval must be >= 1 ms", exit=2)
    plan = {"cycles": args.cycles, "warmup": warmup,
            "interval": args.interval, "samples": args.samples}

    members = suite_members(root, args.suite)
    digest = members_digest(members)

    if args.from_bundle:
        try:
            with open(args.from_bundle, "r", encoding="utf-8") as fh:
                bundle = json.load(fh)
        except OSError as exc:
            raise HlMemError("cannot read the bundle: %s" % exc, exit=2)
        except ValueError as exc:
            raise HlMemError("the bundle is not valid JSON: %s" % exc,
                              exit=2)
        if not isinstance(bundle, dict) \
                or bundle.get("tool") != BUNDLE_TOOL:
            raise HlMemError("the bundle is not an %s bundle (tool=%r)"
                              % (BUNDLE_TOOL,
                                 (bundle or {}).get("tool")
                                 if isinstance(bundle, dict) else bundle),
                              exit=2)
        if not isinstance(bundle.get("workloads"), dict) \
                or not bundle["workloads"]:
            raise HlMemError("the bundle carries no workloads", exit=2)
        source = "bundle %s" % args.from_bundle
    else:
        bundle = measure_bundle(members, plan)
        source = "hls-rss"

    member_by_name = dict(members)
    workloads = {}
    for name in sorted(bundle["workloads"]):
        reports = bundle["workloads"][name]
        if not isinstance(reports, list) or not reports:
            raise HlMemError(
                "workload %s carries no runs in the bundle" % name,
                exit=2)
        if len(reports) < MIN_SAMPLES:
            raise HlMemError(
                "workload %s carries fewer than %d samples — a median "
                "no sample supports is not a measurement"
                % (name, MIN_SAMPLES))
        readings = [report_readings(r, "workload %s run %d"
                                    % (name, i + 1))
                    for i, r in enumerate(reports)]
        wfile = None
        wdigest = None
        if name in member_by_name:
            wfile = os.path.relpath(member_by_name[name],
                                    root).replace(os.sep, "/")
            try:
                with open(member_by_name[name], "rb") as fh:
                    wdigest = sha256_hex(fh.read())
            except OSError:
                wdigest = None
        workloads[name] = {
            "file": wfile or name,
            "digest": wdigest,
            "samples": len(readings),
            "marker_rss_pages": [r["marker_rss_pages"]
                                 for r in readings],
            "end_rss_pages": [r["end_rss_pages"] for r in readings],
            "warmup_end_rss_pages": [r["warmup_end_rss_pages"]
                                     for r in readings],
            "slope_pages_per_cycle": [r["slope_pages_per_cycle"]
                                      for r in readings],
            "slope_ci_pages": [r["slope_ci_pages"] for r in readings],
            "hls_rss_verdict": "stable",
        }

    env = _merge_env(_env_fingerprint(root),
                     bundle.get("env") if args.from_bundle else None,
                     "the bundle env")

    entry = {
        "type": "record",
        "tool": TOOL,
        "version": TOOL_VERSION,
        "commit": commit,
        "suite": args.suite,
        "suite_digest": digest,
        "plan": plan,
        "noise_pct": DEFAULT_NOISE_PCT,
        "floor_pages": DEFAULT_FLOOR_PAGES,
        "env": env,
        "workloads": workloads,
        "note": args.note,
        "stamp": _utc_now() if args.stamp else None,
    }
    # The floor law bites HERE, at record time — a record that its
    # own ledger would refuse never enters the ledger.
    _validate_record(entry, "the new record")
    append_entry(history, entry)
    _write_report(args, bundle)
    print("recorded %s (%s) suite=%s workloads=%d source=%s"
          % (short(commit), commit, args.suite, len(workloads), source))
    return 0


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# sanction — a written excuse, one workload at a time.
# ---------------------------------------------------------------------------

def cmd_sanction(args):
    history = args.history or HISTORY_DEFAULT
    entries = load_history(history)
    known = sorted({n for r in records_of(entries)
                    for n in r["workloads"]})
    if args.workload not in known:
        raise HlMemError(
            "unknown workload %r — the ledger knows: %s"
            % (args.workload, ", ".join(known) if known else "(none)"),
            exit=2)
    if not args.reason:
        raise HlMemError("a sanction without a written reason is not a "
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
    byte-identical suite digest and the same page size. Default
    baseline: the newest earlier record of that instrument."""
    recs = records_of(entries)
    idx = max(i for i, r in enumerate(recs) if r is new_rec)
    if want:
        cands = [r for r in recs if r["commit"].startswith(want)]
        if not cands:
            raise HlMemError("no record carries commit prefix %r" % want,
                              exit=2)
        if len(cands) > 1:
            raise HlMemError("commit prefix %r is ambiguous (%d "
                              "records)" % (want, len(cands)), exit=2)
        base = cands[0]
        if base is new_rec:
            raise HlMemError("the baseline is the record itself — a "
                              "record does not regress against "
                              "itself", exit=2)
    else:
        earlier = [r for r in recs[:idx]
                   if r["suite_digest"] == new_rec["suite_digest"]
                   and page_size_of(r) == page_size_of(new_rec)]
        if not earlier:
            raise HlMemError(
                "no earlier record of the same instrument to compare "
                "against (suite_digest=%s, page_size=%s) — record one "
                "first" % (new_rec["suite_digest"][:12],
                           page_size_of(new_rec)), exit=2)
        base = earlier[-1]
    if base["suite_digest"] != new_rec["suite_digest"]:
        raise HlMemError(
            "the suite changed since the baseline (digest %s vs %s) — "
            "re-baseline, do not compare"
            % (base["suite_digest"][:12],
               new_rec["suite_digest"][:12]))
    bps = page_size_of(base)
    nps = page_size_of(new_rec)
    if bps != nps:
        raise HlMemError(
            "different units: the baseline's page is %s bytes, the "
            "record's is %s — a page is the unit RSS moves in; "
            "re-baseline on this machine" % (bps, nps))
    return base


def verdict_of(entries, baseline=None, noise_pct=DEFAULT_NOISE_PCT,
               floor_pages=DEFAULT_FLOOR_PAGES):
    """Classify the newest record against the chosen baseline and
    apply the sanctions. Returns the full machine verdict."""
    recs = records_of(entries)
    if not recs:
        raise HlMemError("the ledger carries no records — nothing to "
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
            c = classify(new_rec["workloads"][name]["marker_rss_pages"],
                         base_rec["workloads"][name]["marker_rss_pages"],
                         name, noise_pct, floor_pages)
            row = {"workload": name, "class": c["class"],
                   "point_pages": c["point_pages"],
                   "point_pct": c["point_pct"],
                   "ci_pages": c["ci_pages"],
                   "band_pages": c["band_pages"],
                   "cur_median_pages": c["cur_median_pages"],
                   "base_median_pages": c["base_median_pages"],
                   "samples": new_rec["workloads"][name]["samples"]}
        elif name in new_rec["workloads"]:
            row = {"workload": name, "class": "new",
                   "cur_median_pages": median_of(
                       new_rec["workloads"][name]["marker_rss_pages"])}
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
        "suite": new_rec["suite"],
        "page_size": page_size_of(new_rec),
        "noise_pct": noise_pct,
        "floor_pages": floor_pages,
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
                   noise_pct=args.threshold, floor_pages=args.floor)
    if not args.quiet:
        print("gate %s: record %s vs baseline %s (suite %s, threshold "
              "%.1f%%, floor %s pg)"
              % (v["verdict"].upper(), short(v["record"]),
                 short(v["baseline"]), v["suite"], v["noise_pct"],
                 v["floor_pages"]))
        for r in v["workloads"]:
            line = "  %-24s" % r["workload"]
            if r["class"] in ("no-change", "within-noise", "improved",
                              "regressed"):
                lo, hi = r["ci_pages"]
                line += (" %-12s %s  %s  band %s"
                         % (r["class"],
                            fmt_change(r["point_pages"], r["point_pct"]),
                            fmt_ci_pages(lo, hi),
                            fmt_band(r["band_pages"])))
            else:
                line += " %s" % r["class"]
                if r["class"] == "new":
                    line += " (%s)" % fmt_pg(r["cur_median_pages"])
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
    is a run of mid bands (a wall of max-height blocks would read as a
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
        raise HlMemError("the ledger carries no records — nothing to "
                          "render", exit=2)
    try:
        with open(history_path, "rb") as fh:
            digest = sha256_hex(fh.read())
    except OSError as exc:
        raise HlMemError("cannot read the ledger: %s" % exc, exit=2)

    new_rec = recs[-1]
    base = None
    verdict = None
    try:
        verdict = verdict_of(entries, noise_pct=DEFAULT_NOISE_PCT,
                             floor_pages=DEFAULT_FLOOR_PAGES)
        base = pick_baseline(new_rec, entries, None)
    except HlMemError:
        verdict = None
        base = None

    workload_names = sorted(set(new_rec["workloads"]))

    shown = recs[-limit:] if limit and limit > 0 else recs

    lines = []
    a = lines.append
    a("# HLS memory dashboard")
    a("")
    a("- Tool: %s %s" % (TOOL, TOOL_VERSION))
    a("- Ledger: `%s` (sha256 `%s`)" % (history_path, digest))
    a("- Records: %d (showing %d)" % (len(recs), len(shown)))
    a("- Newest record: `%s` (%d workloads)"
      % (new_rec["commit"], len(new_rec["workloads"])))
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
        a("| workload | base | now | change | band | 95% CI | class |")
        a("|---|---|---|---|---|---|---|")
        for r in verdict["workloads"]:
            if r["class"] in ("no-change", "within-noise", "improved",
                              "regressed"):
                lo, hi = r["ci_pages"]
                a("| %s | %s | %s | %s | %s | %s | %s |"
                  % (r["workload"], fmt_pg(r["base_median_pages"]),
                     fmt_pg(r["cur_median_pages"]),
                     fmt_change(r["point_pages"], r["point_pct"]),
                     fmt_band(r["band_pages"]),
                     fmt_ci_pages(lo, hi),
                     r["class"] + (" (sanctioned)"
                                   if "sanction" in r else "")))
            elif r["class"] == "new":
                a("| %s | — | %s | — | — | — | new |"
                  % (r["workload"], fmt_pg(r["cur_median_pages"])))
            else:
                a("| %s | (gone) | — | — | — | — | vanished |"
                  % r["workload"])
    a("")
    a("## Trends — resident set at markers (pages, oldest to newest)")
    a("")
    a("| workload | trend | %s |" % " | ".join(
        short(r["commit"], 7) for r in shown))
    a("|---|---|" + "---|" * len(shown))
    for name in workload_names:
        series = []
        for r in shown:
            w = r["workloads"].get(name)
            series.append(median_of(w["marker_rss_pages"])
                          if w else None)
        known = [m for m in series if m is not None]
        spark = sparkline(known) if known else "-"
        cells = " | ".join((fmt_pg(m) if m is not None else "—")
                           for m in series)
        a("| %s | %s | %s |" % (name, spark, cells))
    a("")
    a("## Slope watch (newest record — pages per 1000 cycles, leak "
      "threshold %.1f)" % (LEAK_SLOPE_THRESHOLD * 1000.0))
    a("")
    a("| workload | slope | 95% CI |")
    a("|---|---|---|")
    for name in workload_names:
        w = new_rec["workloads"][name]
        slopes = w["slope_pages_per_cycle"]
        cis = w["slope_ci_pages"]
        lo = min(c[0] for c in cis)
        hi = max(c[1] for c in cis)
        a("| %s | %s | [%s %s] |"
          % (name, fmt_slope_kc(median_of(slopes)),
             fmt_slope_kc(lo), fmt_slope_kc(hi)))
    a("")
    a("## Sanctions")
    a("")
    sancs = sanctions_of(entries)
    if not sancs:
        a("None. Every regression the ledger has seen was answered, "
          "not excused.")
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
    a("- A record is one hls-rss measurement of the tracked suite "
      "pinned to one clean commit; every workload must certify "
      "STABLE before it can be filed (the floor law — a canary that "
      "leaks is a bug, not a record), and the ledger is append-only. "
      "This document is a pure render of it (the sha256 above pins "
      "the bytes).")
    a("- The metric is the marker median — the resident set at the "
      "cycle markers, the steady level the workload stands on (the "
      "tracked suite holds its peak live at its markers, so this IS "
      "the footprint; a sampled maximum would be a lottery over "
      "transient spikes that recede). Classes follow the page law: "
      "significance is the seeded bootstrap CI of the median change "
      "excluding zero (RSS is integer pages, often with zero "
      "variance — a t-test would call a deterministic growth "
      "no-change), and the noise band is floored at %d pages because "
      "a percent of a small footprint is a fraction of the page RSS "
      "moves in." % DEFAULT_FLOOR_PAGES)
    a("- The instrument runs under setarch -R when the platform "
      "provides it (address-space randomization off — the env "
      "fingerprint records the mode): the resident set reflects the "
      "code, not the loader's dice.")
    a("- regressed or vanished workloads fail the gate unless a "
      "sanction names them. A sanction is a written excuse with an "
      "author — the register above is the audit trail.")
    a("- The unit law: the ledger never compares across page sizes or "
      "across suite digests — a page is a unit, and a changed suite "
      "is re-baselined, not compared.")
    a("- The slope watch restates each workload's hls-rss timeline "
      "slope (pages per 1000 cycles) against the instrument's own "
      "%s page/cycle leak threshold — the floor law's other half, "
      "trended so a slow creep cannot hide behind a green gate."
      % LEAK_SLOPE_THRESHOLD)
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
        v = verdict_of(entries, noise_pct=DEFAULT_NOISE_PCT,
                       floor_pages=DEFAULT_FLOOR_PAGES)
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(json_dumps_sorted(v))
    print("dashboard written to %s" % args.out)
    return 0


def _write_report(args, bundle):
    """--json on record: file the measurement bundle beside the
    ledger entry (what CI keeps when the raw instrument reports
    matter — and the artifact another machine can re-file with
    --from-bundle)."""
    if not args.json:
        return
    with open(args.json, "w", encoding="utf-8") as fh:
        fh.write(json_dumps_sorted(bundle))


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
            print("line %-4d record  %s  suite=%s workloads=%d"
                  % (e["_line"], e["commit"],
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
        prog="hls-mem",
        description="Halis memory regression ledger (Stage 137): RSS "
                    "over time. hls-rss measures; hls-mem remembers "
                    "and judges.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("record", help="measure and pin one commit")
    p.add_argument("--root", default=None,
                   help="repository root the record is pinned from "
                        "(default: the tool's repo)")
    p.add_argument("--suite", default=DEFAULT_SUITE,
                   help="workload suite directory (default: %s)"
                        % DEFAULT_SUITE)
    p.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                   help="hls-rss runs per workload (default %d)"
                        % DEFAULT_SAMPLES)
    p.add_argument("--cycles", type=int, default=DEFAULT_CYCLES,
                   help="measured steady-state cycles per run "
                        "(default %d)" % DEFAULT_CYCLES)
    p.add_argument("--warmup", type=int, default=None,
                   help="warmup cycles before the measured phase "
                        "(default cycles//4, at least 1)")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help="RSS sampling interval in ms (default %.0f)"
                        % DEFAULT_INTERVAL)
    p.add_argument("--from-bundle", default=None, metavar="B.json",
                   help="import a measurement bundle (the CI path) "
                        "instead of measuring")
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

    p = sub.add_parser("sanction", help="excuse one workload, in "
                                        "writing")
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
                   help="noise band in %% of baseline (default %g)"
                        % DEFAULT_NOISE_PCT)
    p.add_argument("--floor", type=float, default=DEFAULT_FLOOR_PAGES,
                   dest="floor",
                   help="noise band page floor (default %g pages)"
                        % DEFAULT_FLOOR_PAGES)
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
    except HlMemError as exc:
        sys.stderr.write("hls-mem: %s\n" % exc)
        return exc.exit


if __name__ == "__main__":
    sys.exit(main())
