#!/usr/bin/env python3
"""hls-bench — the Stage 124 criterion-style micro-benchmark runner.

Usage:
  python3 tools/hls-bench.py benches/                     # run every suite in a dir
  python3 tools/hls-bench.py benches/math.hls             # one suite file
  python3 tools/hls-bench.py --grep str benches/          # filter by substring
  python3 tools/hls-bench.py --native benches/math.hls    # measure bin/hlc + gcc output
  python3 tools/hls-bench.py --save main benches/         # record a baseline
  python3 tools/hls-bench.py --baseline main benches/     # compare against it
  python3 tools/hls-bench.py --fixed-iters 1000 benches/  # deterministic plan
  python3 tools/hls-bench.py --list benches/              # show what would run
  python3 tools/hls-bench.py --json report.json benches/  # machine-readable report

The convention (HLS has no attributes on functions and no closures, so —
like Stage 119's snapshot assertion and Stage 120's case tables — the
benchmark is expressed in DATA plus a naming convention the runner
drives, with zero checker or codegen edits):

  * a benchmark SUITE is a .hls file that defines NO `main` — the
    runner is its program (a suite that defines main is a file-level
    failure: the driver the runner synthesizes must be the entry);
  * a BENCHMARK is a top-level `fn bench_<name>()` taking no
    parameters. Its return value (if any) is ignored — the runner
    times the CALL, not the result; the synthesized loop binds the
    result into a type-shaped sink so no backend can dead-code-
    eliminate the work;
  * a THROUGHPUT UNIT is an optional companion
    `fn units_<name>() -> int` returning the work units one iteration
    consumes (elements hashed, bytes produced, requests shaped...).
    Present, the report gains a `thrpt:` line (units per second).
    Malformed (parameters, wrong declared return, extern) fails the
    bench; a stray `units_` with no matching bench is a file-level
    failure (the typo case, exactly like Stage 120's stray `cases_`);
  * `std.bench.mark_skip(reason)` skips the bench (the reserved
    `__HLBENCH_SKIP__:` panic prefix — the `std.test.mark_skip`
    discipline, Stage 18/120).

The driver: for every selected bench the runner synthesizes a small
Halis driver program NEXT TO the suite file (so the suite's own
relative imports resolve exactly where they resolve for any other
importer) and runs it — in-process on the Stage-0 interpreter (one
fresh load + check + Interp per bench), or, with --native, through
bin/hlc + gcc. ONE driver source, TWO backends: the measurement
protocol is ordinary stdout, and the plan constants are baked into the
driver as literals, so `--fixed-iters` makes the interpreter and the
native binary emit the SAME (bench, iters) plan — only the clock
readings differ, which is exactly what must differ.

The wire protocol (line-oriented, reserved-prefix discipline — the
same rules as Stage 119's `__SNAP__` stream):

  __HLBENCH_UNITS__ <bench> <units>          zero or one per run
  __HLBENCH__ <bench> <iters> <elapsed_ns>   exactly SAMPLES per run
  __HLBENCH_HARNESS__ <iters> <elapsed_ns>   exactly one per run

A line STARTING with one of the prefixes must parse exactly (else the
bench fails with a protocol error); a line merely CONTAINING
`__HLBENCH_` that is not a record is a protocol error too (a bench
that prints protocol look-alikes corrupts the stream — fail loudly,
never silently drop); any other output the bench prints is skipped
(benches may print; the measurement survives).

The measurement plan (criterion's routine, in-language): the driver
warms up with DOUBLING batches until the warmup budget is spent, takes
the last batch as the per-iteration cost estimate, then takes SAMPLES
samples, each sized to run about the target sample time (clamped to
[min_iters, max_iters]). `--fixed-iters K` replaces all of it with a
deterministic plan: SAMPLES samples of exactly K iterations, no
warmup. After the samples the driver times ONE batch of the empty
harness loop at the same size — the runner reports that harness cost
per iteration (it is included in every sample, criterion-style; we
surface it instead of hiding it, and we do NOT subtract it).

The statistics (runner-side, criterion-style): per bench, each sample
yields a per-iteration estimate elapsed/iters; the report shows the
MEDIAN with a bootstrap confidence interval (the `time:` bracket), the
mean with sample standard deviation, the median absolute deviation,
Tukey-fence outlier counts (low/high × mild/severe — criterion's
classification), and, when units are declared, the throughput with its
own bootstrap CI (slope-based: total units over total time). Bootstrap
resampling is seeded from the bench name, so the SAME data always
yields the SAME report.

Baselines: `--save NAME` writes one JSON file per measured bench under
`.hlbench/<suite-stem>/<bench>.json` beside the suite file — the raw
per-iteration estimates plus the backend and totals, byte-stable by
construction (sorted keys, fixed floats, no timestamps). `--baseline
NAME` compares the current run against it: the relative change of the
median with a bootstrap CI and a Welch t-test p-value, classified the
way criterion classifies it — "No change in performance detected.",
"Change within noise threshold.", "Performance has improved." /
"Performance has regressed." (significance p < 0.05, noise threshold
--noise, default 5%). Comparing across backends fails the bench —
interpreter and native timings are different instruments.

Exit codes:
  0  every selected bench measured (skips allowed)
  1  at least one bench (or file) failed
  2  usage / IO error (bad paths, no suites discovered, a --baseline
     name that has never been recorded)

Naming note: the Stage 32 zero-cost-abstractions audit gate — the
per-stdlib-function threshold harness — now lives in
`tools/hls-bench-stdlib.py` (`make bench-stdlib`); this tool is the
user-facing benchmark runner the roadmap's Stage 124 names.
"""

import argparse
import io
import json
import math
import multiprocessing
import os
import random
import subprocess
import sys
import tempfile
import time
import traceback
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boot.boot import load_program           # noqa: E402
from boot.checker import check               # noqa: E402
from boot.interp import Interp, HLPanic      # noqa: E402
from boot.lexer import tokenize, HLError     # noqa: E402
from boot.parser import Parser               # noqa: E402

TOOL_VERSION = "0.142.0-alpha"

# The reserved protocol prefixes (in longest-first match order —
# `__HLBENCH_UNITS__ ` and `__HLBENCH_HARNESS__ ` must be tested before
# `__HLBENCH__ `, which is a prefix of neither string but shares the
# stem; matching longest-first keeps one scan honest).
_PROTO_UNITS = b"__HLBENCH_UNITS__ "
_PROTO_HARNESS = b"__HLBENCH_HARNESS__ "
_PROTO_SAMPLE = b"__HLBENCH__ "
_PROTO_STEM = b"__HLBENCH_"

# The reserved panic prefix std.bench.mark_skip raises — a bench that
# panics with it is reported SKIP with the reason, never FAIL.
_SKIP_PREFIX = "__HLBENCH_SKIP__:"

# Baseline store layout: .hlbench/<suite-stem>/<bench>.json beside the
# suite file (dot-directory, machine-local by nature — timings are a
# property of the machine that measured them, so unlike snapshot stores
# baselines are NOT for committing).
_BASELINE_DIR = ".hlbench"

# The full effect universe a probe driver declares on its main. The
# probe's job is to run the REAL checker over the suite (a suite has no
# main of its own — the checker legitimately refuses a module) so the
# runner can read computed_effects instead of re-implementing effect
# inference. Declaring wider effects than computed is legal; the probe
# is the one program where that is exactly the point.
_PROBE_EFFECTS = ["IO", "Fs", "Clock", "Args", "Exit",
                  "Net", "Rand", "Proc", "Conc"]

# Effect clause of the synthesized measurement driver: the timing
# helpers need Clock (instant_now_ns), the protocol printing needs IO,
# and the bench's own computed effects ride along so a benchmark that
# uses Rand / Net / Fs type-checks in the driver exactly as it does in
# the suite.
_DRIVER_BASE_EFFECTS = {"IO", "Clock"}

# Defaults of the measurement plan (criterion's shape, sized for a
# self-hosted toolchain: the Stage-0 interpreter is ~4 orders of
# magnitude slower than the native backend, so the per-bench budget
# must stay small while remaining large enough for stable statistics).
DEFAULT_SAMPLES = 30
DEFAULT_WARMUP_MS = 30
DEFAULT_SAMPLE_TIME_MS = 15
DEFAULT_MIN_ITERS = 1
DEFAULT_MAX_ITERS = 1 << 30
DEFAULT_NOISE_PCT = 5.0

# Bootstrap resamples for the confidence intervals. Seeded from the
# bench name (zlib.crc32), so the same data yields the same report.
_BOOTSTRAP_B = 2000

# Welch t-test significance level (criterion's change detection).
_ALPHA = 0.05


class BenchFileError(Exception):
    """A file-level problem that fails the whole suite (convention
    violation, compile error, protocol corruption discovered before any
    bench can be trusted)."""


# ---------------------------------------------------------------------------
# The protocol parser — line-oriented, reserved-prefix discipline.
# ---------------------------------------------------------------------------

def parse_protocol(stdout):
    """Parse one driver run's captured stdout into (records, error).

    records is a list of (kind, bench_or_None, a, b) tuples:
      ("units",   bench, units:int, None)
      ("sample",  bench, iters:int, elapsed_ns:int)
      ("harness", None,  iters:int, elapsed_ns:int)
    in emission order. error is None or a protocol-error string.

    The scan is line-wise over the RAW bytes (benches may print noise,
    including partial lines — we split on \\n and never decode): a line
    starting with a record prefix must parse EXACTLY (integers are
    decimal, no sign, no leading zeros); a line containing the reserved
    stem `__HLBENCH_` that is not a record is a protocol error (the
    reserved-prefix discipline: look-alikes fail loudly, records are
    never silently lost); every other line is skipped.
    """
    records = []
    for line in stdout.split(b"\n"):
        if not line:
            continue
        if line.startswith(_PROTO_UNITS):
            rest = line[len(_PROTO_UNITS):]
            parts = rest.split(b" ")
            if len(parts) != 2:
                return records, ("protocol error: %r is not "
                                 "'__HLBENCH_UNITS__ <bench> <units>'"
                                 % line[:64])
            bench, ub = parts
            if not _is_uint(ub):
                return records, ("protocol error: malformed units count "
                                 "%r" % ub[:32])
            records.append(("units", bench, int(ub), None))
        elif line.startswith(_PROTO_HARNESS):
            rest = line[len(_PROTO_HARNESS):]
            parts = rest.split(b" ")
            if len(parts) != 2:
                return records, ("protocol error: %r is not "
                                 "'__HLBENCH_HARNESS__ <iters> <elapsed>'"
                                 % line[:64])
            it, el = parts
            if not _is_uint(it) or not _is_uint(el):
                return records, ("protocol error: malformed harness "
                                 "record %r" % line[:64])
            records.append(("harness", None, int(it), int(el)))
        elif line.startswith(_PROTO_SAMPLE):
            rest = line[len(_PROTO_SAMPLE):]
            parts = rest.split(b" ")
            if len(parts) != 3:
                return records, ("protocol error: %r is not "
                                 "'__HLBENCH__ <bench> <iters> <elapsed>'"
                                 % line[:64])
            bench, it, el = parts
            if not _is_uint(it) or not _is_uint(el):
                return records, ("protocol error: malformed sample record "
                                 "for %r" % bench[:32])
            records.append(("sample", bench, int(it), int(el)))
        elif _PROTO_STEM in line:
            return records, ("protocol error: output contains the "
                             "reserved prefix '__HLBENCH_' outside a "
                             "record: %r" % line[:64])
    return records, None


def _is_uint(b):
    """A decimal unsigned int, no sign, no leading zeros, not empty."""
    if not b or not b.isdigit():
        return False
    if len(b) > 1 and b[0:1] == b"0":
        return False
    return True


# ---------------------------------------------------------------------------
# Statistics — the criterion-style numbers the report prints.
# ---------------------------------------------------------------------------

def _mean(xs):
    return sum(xs) / float(len(xs))


def _var_sample(xs):
    """Sample variance (n-1 denominator); 0.0 for fewer than 2 points."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / float(n - 1)


def quantile(sorted_xs, q):
    """Linear-interpolation quantile of a SORTED list (numpy's default
    'linear' method): q in [0, 1]."""
    n = len(sorted_xs)
    if n == 0:
        raise ValueError("quantile of empty data")
    if n == 1:
        return float(sorted_xs[0])
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_xs[lo])
    frac = pos - lo
    return sorted_xs[lo] * (1.0 - frac) + sorted_xs[hi] * frac


def median(xs):
    return quantile(sorted(xs), 0.5)


def mad(xs):
    """Median absolute deviation about the median — the robust spread
    the report prints next to it."""
    m = median(xs)
    return median([abs(x - m) for x in xs])


def classify_outliers(xs):
    """Tukey-fence outlier classification (criterion's): outside
    [q1-3iqr, q3+3iqr] is SEVERE, outside [q1-1.5iqr, q3+1.5iqr] is
    MILD; both split low/high. Returns a dict of counts."""
    s = sorted(xs)
    q1 = quantile(s, 0.25)
    q3 = quantile(s, 0.75)
    iqr = q3 - q1
    lo_mild, hi_mild = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    lo_sev, hi_sev = q1 - 3.0 * iqr, q3 + 3.0 * iqr
    out = {"low_severe": 0, "low_mild": 0,
           "high_mild": 0, "high_severe": 0}
    for x in xs:
        if x < lo_sev:
            out["low_severe"] += 1
        elif x < lo_mild:
            out["low_mild"] += 1
        elif x > hi_sev:
            out["high_severe"] += 1
        elif x > hi_mild:
            out["high_mild"] += 1
    return out


def bootstrap_ci(xs, stat, b=_BOOTSTRAP_B, seed=0):
    """Percentile bootstrap CI (2.5, 97.5) of stat(xs) under resampling
    with replacement. Seeded, so the same data gives the same interval
    byte for byte."""
    rng = random.Random(seed)
    n = len(xs)
    if n == 0:
        raise ValueError("bootstrap of empty data")
    if n == 1:
        v = stat(xs)
        return (v, v)
    stats = []
    for _ in range(b):
        rs = [xs[rng.randrange(n)] for _ in range(n)]
        stats.append(stat(rs))
    stats.sort()
    return (quantile(stats, 0.025), quantile(stats, 0.975))


def bootstrap_ci_slope(samples, units, b=_BOOTSTRAP_B, seed=0):
    """Bootstrap CI of the THROUGHPUT (units per second) under
    resampling whole (iters, elapsed) sample pairs: slope* = sum(el*) /
    sum(it*), thrpt* = units * it* / el*. units may be None (returns
    None) — samples are (iters, elapsed_ns) pairs."""
    if units is None:
        return None
    rng = random.Random(seed)
    n = len(samples)
    if n == 0:
        return None
    if n == 1:
        it, el = samples[0]
        return (units * it / el if el > 0 else 0.0,) * 2
    vals = []
    for _ in range(b):
        its = 0
        els = 0
        for _ in range(n):
            it, el = samples[rng.randrange(n)]
            its += it
            els += el
        if els > 0:
            vals.append(units * its / els * 1e9)
    if not vals:
        return None
    vals.sort()
    return (quantile(vals, 0.025), quantile(vals, 0.975))


# --- Welch's t-test (the change-detection p-value) ------------------------

def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function (Lentz's
    method, modified — the standard Numerical Recipes betacf)."""
    maxit = 300
    eps = 3e-16
    fpmin = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        dele = d * c
        h *= dele
        if abs(dele - 1.0) < eps:
            break
    return h


def _betainc(a, b, x):
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def welch_p(a, b):
    """Two-tailed Welch t-test p-value that the per-iteration
    distributions a and b share a mean. Returns 1.0 when either side
    has fewer than 2 samples or zero total variance (nothing to
    detect)."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 1.0
    va = _var_sample(a)
    vb = _var_sample(b)
    denom = va / na + vb / nb
    if denom <= 0.0:
        return 1.0
    t = (_mean(a) - _mean(b)) / math.sqrt(denom)
    num = denom * denom
    den = ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    if den <= 0.0:
        return 1.0
    df = num / den
    return _betainc(df / 2.0, 0.5, df / (df + t * t))


# --- formatting (criterion's units and significant digits) -----------------

_TIME_UNITS = [
    (1e9, "s"), (1e6, "ms"), (1e3, "us"), (1.0, "ns"),
]


def fmt_time(ns):
    """A duration in nanoseconds as criterion prints it: the largest
    unit that keeps the value >= 1, 4 significant digits."""
    for factor, name in _TIME_UNITS:
        if abs(ns) >= factor:
            v = ns / factor
            return _sig4(v) + " " + _unit_disp(name)
    return _sig4(ns) + " " + _unit_disp("ns")


def _unit_disp(name):
    # 'us' renders as the micro sign the report (and every terminal
    # the tool targets) can show.
    return {"us": "\u00b5s"}.get(name, name)


def _sig4(v):
    s = "%.4g" % v
    return s


def fmt_pct(x, signed=True):
    return ("%s%.2f%%" % (("+" if x >= 0 else ""), x)) if signed \
        else "%.2f%%" % x


def fmt_rate(units_per_s):
    return _sig4(units_per_s) + " units/s"


# ---------------------------------------------------------------------------
# Baseline store — .hlbench/<stem>/<bench>.json beside the suite file.
# ---------------------------------------------------------------------------

def baseline_path_for(filepath, bench_name, baseline_name):
    d = os.path.dirname(filepath)
    stem = os.path.basename(filepath)
    if stem.endswith(".hls"):
        stem = stem[: -len(".hls")]
    return os.path.join(d, _BASELINE_DIR, stem, bench_name + "."
                        + baseline_name + ".json")


def save_baseline(path, backend, bench_name, units, samples, estimates):
    """Write one baseline record. Byte-stable by construction: sorted
    keys, fixed float repr (Python's shortest-roundtrip repr is
    deterministic), no timestamps — the same measurements always write
    the same bytes."""
    obj = {
        "backend": backend,
        "bench": bench_name,
        "estimates_ns": list(estimates),
        "iters_total": sum(it for (it, _el) in samples),
        "elapsed_total_ns": sum(el for (_it, el) in samples),
        "samples": [[it, el] for (it, el) in samples],
        "tool": "hls-bench",
        "units": units,
        "version": TOOL_VERSION,
    }
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json_dumps_sorted(obj))
    os.replace(tmp, path)


def load_baseline(path):
    """Load one baseline record. A missing file is a usage error (the
    caller turns it into exit 2); anything that is not exactly what
    save_baseline writes is corruption — reported, never guessed
    around (the snapshot-store discipline, Stage 119)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise BenchFileError("baseline file not found: %s "
                             "(record one first: hls-bench --save ...)"
                             % path)
    except OSError as ex:
        raise BenchFileError("cannot read baseline %s: %s" % (path, ex))
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as ex:  # noqa: BLE001 — corrupt is corrupt
        raise BenchFileError("baseline %s is corrupt (not JSON: %s)"
                             % (path, ex))
    if not isinstance(obj, dict):
        raise BenchFileError("baseline %s is corrupt (not an object)"
                             % path)
    for key in ("backend", "bench", "estimates_ns", "samples"):
        if key not in obj:
            raise BenchFileError("baseline %s is corrupt (missing '%s')"
                                 % (path, key))
    est = obj["estimates_ns"]
    if (not isinstance(est, list) or not est
            or not all(isinstance(x, (int, float)) and x > 0 for x in est)):
        raise BenchFileError("baseline %s is corrupt ('estimates_ns' must "
                             "be a non-empty list of positive numbers)"
                             % path)
    return obj


def json_dumps_sorted(obj):
    return json.dumps(obj, sort_keys=True, indent=1) + "\n"


def compare_to_baseline(bench_name, cur_est, base_obj, noise_pct, seed):
    """The criterion-style change report for one bench: relative change
    of the median with a bootstrap CI, Welch p, and the classification
    line. cur_est is the current run's per-iteration estimates;
    base_obj the loaded baseline record. Returns the change dict (also
    embedded in --json output)."""
    base_est = [float(x) for x in base_obj["estimates_ns"]]
    cur_med = median(cur_est)
    base_med = median(base_est)
    if base_med <= 0:
        return None
    point = (cur_med - base_med) / base_med * 100.0
    # CI of the change: bootstrap the difference of medians (resample
    # each side independently), relative to the baseline median.
    rng = random.Random(seed)
    nb = len(base_est)
    diffs = []
    for _ in range(_BOOTSTRAP_B):
        cs = [cur_est[rng.randrange(len(cur_est))]
              for _ in range(len(cur_est))]
        bs = [base_est[rng.randrange(nb)] for _ in range(nb)]
        diffs.append((median(cs) - median(bs)) / base_med * 100.0)
    diffs.sort()
    lo = quantile(diffs, 0.025)
    hi = quantile(diffs, 0.975)
    p = welch_p(cur_est, base_est)
    significant = p < _ALPHA
    if not significant:
        klass = "no-change"
        note = "No change in performance detected."
    elif lo > noise_pct:
        klass = "regressed"
        note = "Performance has regressed."
    elif hi < -noise_pct:
        klass = "improved"
        note = "Performance has improved."
    else:
        klass = "within-noise"
        note = "Change within noise threshold."
    return {
        "baseline": base_obj.get("bench", bench_name),
        "classification": klass,
        "note": note,
        "p": p,
        "point_pct": point,
        "ci_pct": [lo, hi],
    }


# ---------------------------------------------------------------------------
# Driver synthesis — one small Halis program per benchmark.
# ---------------------------------------------------------------------------

_SINK_ESCAPE = -4611686018427387904  # int64 min / 2 — unreachable by the sink


def _call_and_sink(call, ret):
    """The two statement lines that call the bench inside the measured
    loop and keep its work observable: the result feeds a type-shaped
    sink (the Stage 32 driver's discipline — without it a backend free
    to dead-code-eliminate a pure unused call would measure nothing),
    and the sink itself escapes through an unreachable branch so the
    accumulator cannot be folded away."""
    ret = (ret or "void").strip()
    expr = call if call.endswith(")") else call + "()"
    if ret in ("", "void"):
        return ["        %s()" % call,
                "        sink = sink + 1"]
    if ret == "int":
        return ["        let v: int = %s" % expr,
                "        sink = sink + (v - (v / 100) * 100)"]
    if ret == "bool":
        return ["        let v: bool = %s" % expr,
                "        if v { sink = sink + 1 }"]
    if ret == "str":
        return ["        let v: str = %s" % expr,
                "        sink = sink + v.len()"]
    if ret == "float":
        return ["        let v: float = %s" % expr,
                "        if v > 0.0 { sink = sink + 1 }"]
    return ["        let v: %s = %s" % (ret, expr),
            "        sink = sink + 1"]


def _eff_clause(effects):
    effects = sorted(set(effects))
    if not effects:
        return ""
    return " uses " + ", ".join(effects)


def synthesize_driver(bench_name, suite_basename, plan, bench_ret,
                      bench_effects, units_name, units_effects):
    """The synthesized measurement driver for one bench. Plan constants
    are baked in as literals (the driver is regenerated per run — there
    is nothing to configure at runtime); the wire records are ordinary
    println output so the protocol rides the standing stdout
    byte-parity invariant between the two backends."""
    run_eff = _eff_clause(set(bench_effects) | {"IO"})
    main_eff = _eff_clause(set(bench_effects)
                           | (set(units_effects) if units_effects
                              else set())
                           | _DRIVER_BASE_EFFECTS)
    ret = (bench_ret or "void").strip()
    lines = []
    a = lines.append
    a("# generated by hls-bench (Stage 124, %s) - do not edit" % TOOL_VERSION)
    a("# suite: %s" % suite_basename)
    a("# bench: %s" % bench_name)
    a('import "%s"' % suite_basename)
    a("")
    a("fn __hlb_run(n: int)%s {" % run_eff)
    a("    let mut sink: int = 0")
    a("    let mut i: int = 0")
    a("    while i < n {")
    for stmt in _call_and_sink(bench_name, ret):
        a(stmt)
    a("        i = i + 1")
    a("    }")
    a("    if sink < %d {" % _SINK_ESCAPE)
    a('        println("__HLBENCH_SINK__")')
    a("    }")
    a("}")
    a("")
    a("fn __hlb_harness(n: int) {")
    a("    let mut i: int = 0")
    a("    while i < n {")
    a("        i = i + 1")
    a("    }")
    a("}")
    a("")
    a("fn main()%s {" % main_eff)
    if units_name:
        a("    let u: int = %s()" % units_name)
        a('    println("__HLBENCH_UNITS__ %s " + u.to_str())' % bench_name)
    if plan["mode"] == "fixed":
        a("    let iters: int = %d" % plan["iters"])
    else:
        a("    # warmup: doubling batches until the warmup budget is")
        a("    # spent; the last (largest) batch estimates the cost of")
        a("    # one iteration and sizes the measurement samples.")
        a("    let mut n: int = 1")
        a("    let mut dt: int = 0")
        a("    let mut warming: bool = true")
        a("    while warming {")
        a("        let t0: int = instant_now_ns()")
        a("        __hlb_run(n)")
        a("        dt = instant_now_ns() - t0")
        a("        if dt >= %d {" % plan["warmup_ns"])
        a("            warming = false")
        a("        } else if n >= %d {" % plan["max_iters"])
        a("            warming = false")
        a("        } else {")
        a("            n = n * 2")
        a("        }")
        a("    }")
        a("    let mut per_iter: int = dt / n")
        a("    if per_iter < 1 {")
        a("        per_iter = 1")
        a("    }")
        a("    let mut iters: int = %d / per_iter" % plan["sample_time_ns"])
        a("    if iters < %d {" % plan["min_iters"])
        a("        iters = %d" % plan["min_iters"])
        a("    }")
        a("    if iters > %d {" % plan["max_iters"])
        a("        iters = %d" % plan["max_iters"])
        a("    }")
    a("    let mut s: int = 0")
    a("    while s < %d {" % plan["samples"])
    a("        let t0: int = instant_now_ns()")
    a("        __hlb_run(iters)")
    a("        let d: int = instant_now_ns() - t0")
    a('        println("__HLBENCH__ %s " + iters.to_str() + " " + d.to_str())'
      % bench_name)
    a("        s = s + 1")
    a("    }")
    a("    # harness calibration: one batch of the empty loop at the")
    a("    # same size - the loop cost every sample includes.")
    a("    let th: int = instant_now_ns()")
    a("    __hlb_harness(iters)")
    a("    let dh: int = instant_now_ns() - th")
    a('    println("__HLBENCH_HARNESS__ " + iters.to_str() + " " + dh.to_str())')
    a("}")
    return "\n".join(lines) + "\n"


def synthesize_probe(suite_basename):
    """The probe driver: imports the suite and declares the full effect
    universe on an empty main. Its ONLY job is to give the real checker
    a program to accept (a suite has no main — the checker refuses a
    module), so the runner can read the checker's computed_effects for
    the suite's functions instead of re-implementing effect inference."""
    lines = [
        "# generated by hls-bench (Stage 124, %s) - do not edit" % TOOL_VERSION,
        "# probe: %s" % suite_basename,
        'import "%s"' % suite_basename,
        "",
        "fn main() uses %s {" % ", ".join(_PROBE_EFFECTS),
        "}",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Discovery + the suite convention.
# ---------------------------------------------------------------------------

def discover_files(paths, recurse):
    """Expand files/directories into a deduplicated .hls list. Dot-files
    are skipped: the runner's own temp drivers are dot-prefixed and
    must never be discovered as suites (a crashed run must not poison
    the next one)."""
    out = []
    seen = set()
    for p in paths:
        if os.path.isdir(p):
            entries = (os.walk(p) if recurse
                       else [(p, [], sorted(os.listdir(p)))])
            for root, _dirs, files in entries:
                for f in sorted(files):
                    if not f.endswith(".hls") or f.startswith("."):
                        continue
                    absf = os.path.abspath(os.path.join(root, f))
                    if absf not in seen:
                        seen.add(absf)
                        out.append(absf)
        elif p.endswith(".hls") and os.path.isfile(p):
            absf = os.path.abspath(p)
            if absf not in seen:
                seen.add(absf)
                out.append(absf)
        else:
            sys.stderr.write("hls-bench: skip (not .hls or not found): %s\n"
                             % p)
    return out


def _parse_entry(filepath):
    """Parse JUST the suite file (not its imports) and return its
    top-level fns in source order — the runner sees the USER's
    functions, not the stdlib helpers an import would merge in (the
    same discipline as hltest's list_tests_in_file)."""
    with open(filepath, "rb") as f:
        src = f.read()
    toks = tokenize(src)
    return Parser(toks, src).parse_program()["fns"]


def _display(ex):
    """A panic / HLError message as display text (Stage-0 str panics
    arrive as bytes; decode instead of leaking the b'...' repr)."""
    if isinstance(ex, (bytes, bytearray)):
        return bytes(ex).decode("utf-8", "replace")
    return str(ex)


class BenchResult:
    __slots__ = ("file", "name", "status", "detail", "report", "json_obj")

    def __init__(self, file, name, status, detail="", report=None,
                 json_obj=None):
        self.file = file
        self.name = name          # bench fn name, or <bench:stem>-style
        self.status = status      # "ok" / "skip" / "fail"
        self.detail = detail
        self.report = report      # list of rendered report lines
        self.json_obj = json_obj  # per-bench payload for --json


class FileOutcome:
    __slots__ = ("file", "results", "benches")

    def __init__(self, file, results, benches):
        self.file = file
        self.results = results    # list[BenchResult] (skip/fail included)
        self.benches = benches    # list[BenchResult] with status "ok"


# ---------------------------------------------------------------------------
# One suite file, end to end.
# ---------------------------------------------------------------------------

def run_file(filepath, cfg):
    """Discover, validate, and measure every bench in one suite file.
    Returns a FileOutcome. Never raises for bench-level problems — a
    problem becomes a failing (or skipping) result; only truly
    unexpected runner bugs raise (the pool worker catches those into a
    <worker> failure, hltest-style)."""
    results = []
    measured = []
    rel = filepath

    # ---- parse the entry file alone (source order, user fns only).
    try:
        fns = _parse_entry(filepath)
    except HLError as ex:
        results.append(BenchResult(rel, "<parse>", "fail",
                                   "compile error: %s" % ex))
        return FileOutcome(rel, results, measured)
    except OSError as ex:
        results.append(BenchResult(rel, "<parse>", "fail", "io: %s" % ex))
        return FileOutcome(rel, results, measured)

    bench_names = [n for n in fns if n.startswith("bench_")]
    units_names = [n for n in fns if n.startswith("units_")]

    def file_fail(name, detail):
        results.append(BenchResult(rel, name, "fail", detail))

    # ---- the convention, enforced statically where it can be.
    if "main" in fns:
        file_fail("<bench:%s>" % os.path.basename(filepath),
                  "bench suite defines main - bench files are libraries "
                  "(the runner is their main; move the demo into "
                  "examples/ or a test)")
        return FileOutcome(rel, results, measured)
    if not bench_names and not units_names:
        results.append(BenchResult(rel, "<no-benches>", "skip",
                                   "no bench_* functions in this file"))
        return FileOutcome(rel, results, measured)
    bench_bases = {n[len("bench_"):] for n in bench_names}
    for u in units_names:
        if u[len("units_"):] not in bench_bases:
            file_fail("<bench:%s>" % os.path.basename(filepath),
                      "stray units fn '%s' - no bench_%s beside it "
                      "(the convention is bench_<name> <-> units_<name>, "
                      "same file)" % (u, u[len("units_"):]))
    for b in bench_names:
        d = fns[b]
        if d["params"]:
            file_fail(b, "benchmark '%s' takes %d parameter(s) - "
                         "benchmarks take none (the runner calls them "
                         "with no arguments)" % (b, len(d["params"])))
        elif d.get("extern", False):
            file_fail(b, "benchmark '%s' is extern - benchmarks are HLS "
                         "code the runner measures, not a declaration" % b)
        else:
            # the companion units fn, when present, must be exactly what
            # the driver calls: no parameters, HLS code, declared int
            # (the malformed-units failure lands on the BENCH, the way
            # Stage 120 lands a malformed case table on its test).
            u = "units_" + b[len("bench_"):]
            if u in fns:
                ud = fns[u]
                if ud["params"]:
                    file_fail(b, "units fn '%s' must take no parameters "
                                 "(the runner calls it with none), got %d"
                                 % (u, len(ud["params"])))
                elif ud.get("extern", False):
                    file_fail(b, "units fn '%s' is extern - the units "
                                 "count is HLS code the runner executes, "
                                 "not a declaration" % u)
                elif ud.get("ret") not in (None, "int"):
                    file_fail(b, "units fn '%s' must return int (the "
                                 "work units of ONE iteration), declares "
                                 "'%s'" % (u, ud.get("ret")))
    static_fails = {r.name for r in results if r.status == "fail"}

    # ---- selection. --grep is a substring over the fn name, the same
    # rule hltest applies to tests. A grep that selects nothing is a
    # <no-benches> skip for this file, never an error.
    selected = [b for b in bench_names
                if cfg["grep"] is None or cfg["grep"] in b]
    if not selected:
        results.append(BenchResult(rel, "<no-benches>", "skip",
                                   "no bench matches --grep %r"
                                   % cfg["grep"]))
        return FileOutcome(rel, results, measured)

    # ---- probe: run the REAL checker over the suite (a suite has no
    # main, so the checker must be given one — the probe's) and read
    # the computed effects it wrote for the suite's functions. A suite
    # with a type error fails HERE, once, as a file-level failure.
    suite_basename = os.path.basename(filepath)
    probe_src = synthesize_probe(suite_basename)
    probe_path = _temp_sibling(filepath, "probe")
    computed = {}
    try:
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write(probe_src)
        try:
            program = load_program(probe_path)
            checker = check(program)
        except HLError as ex:
            file_fail("<check>", "type error: %s" % ex)
            return FileOutcome(rel, results, measured)
        except OSError as ex:
            file_fail("<check>", "io: %s" % ex)
            return FileOutcome(rel, results, measured)
        computed = checker.computed_effects
    finally:
        _unlink(probe_path)

    # ---- per-bench measurement.
    for bench in selected:
        if bench in static_fails:
            continue
        base = bench[len("bench_"):]
        units_fn = "units_" + base
        has_units = (units_fn in fns and units_fn not in static_fails
                     and not fns[bench]["params"]
                     and not fns[bench].get("extern", False))
        try:
            res = _measure_bench(filepath, bench, fns, computed, cfg,
                                 has_units)
        except BenchFileError as ex:
            res = BenchResult(rel, bench, "fail", str(ex))
        except Exception as ex:  # noqa: BLE001 — runner bug, not bench's
            res = BenchResult(rel, bench, "fail",
                              "internal error: %r\n%s"
                              % (ex, traceback.format_exc(limit=4)))
        results.append(res)
        if res.status == "ok":
            measured.append(res)
    return FileOutcome(rel, results, measured)


def _temp_sibling(filepath, kind):
    """A dot-prefixed temp driver path beside the suite file (same dir,
    so the suite's relative imports resolve exactly where they resolve
    for any other importer)."""
    d = os.path.dirname(filepath)
    stem = os.path.basename(filepath)
    if stem.endswith(".hls"):
        stem = stem[: -len(".hls")]
    return os.path.join(d, ".hlbench-%s-%s-%d.hls"
                        % (stem, kind, os.getpid()))


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _measure_bench(filepath, bench, fns, computed, cfg, has_units):
    """Synthesize, run, and parse one bench's driver; compute the
    statistics; compare against the baseline when configured. Returns
    the bench's BenchResult (status ok/skip/fail)."""
    rel = filepath
    base = bench[len("bench_"):]
    units_fn = "units_" + base if has_units else None
    plan = cfg["plan"]
    bench_effects = computed.get(bench, set())
    units_effects = computed.get(units_fn, set()) if units_fn else set()
    src = synthesize_driver(
        bench, os.path.basename(filepath), plan,
        fns[bench].get("ret") or "void", bench_effects,
        units_fn, units_effects)

    driver_path = _temp_sibling(filepath, bench)
    backend = cfg["backend"]
    try:
        with open(driver_path, "w", encoding="utf-8") as f:
            f.write(src)
        if backend == "native":
            rc, out, err = _run_native(driver_path, cfg)
        else:
            rc, out, err = _run_interpreter(driver_path, cfg)
    finally:
        _unlink(driver_path)

    # ---- classify the run itself before touching the records.
    if rc is not None and rc != 0:
        text = err.decode("utf-8", "replace").strip()
        if rc == 101 and _SKIP_PREFIX in text:
            # native panic path: the binary prints
            # "panic: __HLBENCH_SKIP__: reason (at line N)"
            idx = text.index(_SKIP_PREFIX) + len(_SKIP_PREFIX)
            reason = text[idx:].split(" (at line ")[0].strip()
            return BenchResult(rel, bench, "skip", reason)
        tail = "\n".join(text.splitlines()[-6:])
        return BenchResult(rel, bench, "fail",
                           "driver exited %d%s"
                           % (rc, (":\n" + tail) if tail else ""))

    records, perr = parse_protocol(out)
    if perr is not None:
        return BenchResult(rel, bench, "fail", perr)

    # ---- split the records; every kind has an exact expected shape.
    units_val = None
    harness = None
    samples = []
    for (kind, rbench, a, b) in records:
        if kind == "units":
            if rbench.decode("utf-8", "replace") != bench:
                return BenchResult(
                    rel, bench, "fail",
                    "protocol error: units record for %r in the driver "
                    "of '%s'" % (rbench.decode("utf-8", "replace"), bench))
            units_val = a
        elif kind == "harness":
            harness = (a, b)
        else:
            if rbench.decode("utf-8", "replace") != bench:
                return BenchResult(
                    rel, bench, "fail",
                    "protocol error: sample record for %r in the driver "
                    "of '%s'" % (rbench.decode("utf-8", "replace"), bench))
            samples.append((a, b))

    if len(samples) != plan["samples"]:
        return BenchResult(
            rel, bench, "fail",
            "protocol error: expected %d sample record(s), got %d - the "
            "driver's plan and the parser must agree"
            % (plan["samples"], len(samples)))
    if harness is None:
        return BenchResult(rel, bench, "fail",
                           "protocol error: no __HLBENCH_HARNESS__ record "
                           "(the driver always emits exactly one)")
    if any(el <= 0 for (_it, el) in samples):
        return BenchResult(
            rel, bench, "fail",
            "protocol error: a sample reports elapsed <= 0 ns - the "
            "monotonic clock must advance across a measured batch")
    if has_units:
        if units_val is None:
            return BenchResult(
                rel, bench, "fail",
                "protocol error: no __HLBENCH_UNITS__ record (the driver "
                "emits it before sampling)")
        if units_val <= 0:
            return BenchResult(
                rel, bench, "fail",
                "units fn '%s' returned %d - declare at least 1 unit "
                "per iteration" % (units_fn, units_val))
    else:
        units_val = None

    # ---- the statistics.
    est = [el / float(it) for (it, el) in samples]
    med = median(est)
    mean = _mean(est)
    std = math.sqrt(_var_sample(est))
    m = mad(est)
    med_lo, med_hi = bootstrap_ci(est, median, seed=_seed(bench))
    mean_lo, mean_hi = bootstrap_ci(est, _mean, seed=_seed(bench) + 1)
    outliers = classify_outliers(est)
    slope = sum(el for (_it, el) in samples) \
        / float(sum(it for (it, _el) in samples))
    thrpt_pt = None
    if units_val:
        it_total = sum(it for (it, _el) in samples)
        el_total = sum(el for (_it, el) in samples)
        thrpt_pt = units_val * it_total / (el_total / 1e9)
    thrpt_ci = bootstrap_ci_slope(samples, units_val, seed=_seed(bench) + 2)
    harness_per_iter = harness[1] / float(harness[0]) if harness[0] else 0.0

    # ---- baseline comparison (--baseline) and recording (--save).
    change = None
    bname = cfg["baseline"]
    if bname:
        bpath = baseline_path_for(filepath, bench, bname)
        try:
            base_obj = load_baseline(bpath)
        except BenchFileError as ex:
            return BenchResult(rel, bench, "fail", str(ex))
        if base_obj["backend"] != backend:
            return BenchResult(
                rel, bench, "fail",
                "baseline '%s' was recorded with backend '%s', this run "
                "is '%s' - interpreter and native timings are different "
                "instruments; compare like with like"
                % (bname, base_obj["backend"], backend))
        change = compare_to_baseline(bench, est, base_obj,
                                     cfg["noise_pct"], _seed(bench) + 3)
    if cfg["save"]:
        save_baseline(baseline_path_for(filepath, bench, cfg["save"]),
                      backend, bench, units_val, samples, est)

    # ---- render + carry. The raw samples ride in the json payload
    # (--json includes them; baselines record them via --save).
    report_lines, json_obj = _render_bench(
        bench, med, (med_lo, med_hi), mean, (mean_lo, mean_hi), std, m,
        outliers, len(est), slope, units_val, thrpt_pt, thrpt_ci,
        harness_per_iter, change, cfg)
    json_obj["raw_samples"] = [[it, el] for (it, el) in samples]
    return BenchResult(rel, bench, "ok", report=report_lines,
                       json_obj=json_obj)


def _seed(name):
    return zlib.crc32(name.encode("utf-8"))


def _run_interpreter(driver_path, cfg):
    """Run the driver on the Stage-0 interpreter, in-process: the real
    loader, the real checker, one fresh Interp — hltest's execution
    model. Returns (rc_or_None, stdout_bytes, stderr_bytes)."""
    buf = io.BytesIO()
    err = io.BytesIO()
    saved_err = sys.stderr
    sys.stderr = err
    try:
        program = load_program(driver_path)
        check(program)
        interp = Interp(program, [driver_path.encode("utf-8")], buf,
                        contracts=False)
        interp.call_fn("main", [])
        rc = 0
    except HLError as ex:
        sys.stderr = saved_err
        return 1, b"", ("compile error: %s" % ex).encode("utf-8")
    except HLPanic as ex:
        rc = 101
        line = "panic: %s (at line %d)\n" % (_display(ex.msg), ex.line)
        err.write(line.encode("utf-8"))
    except SystemExit as ex:
        rc = ex.code if isinstance(ex.code, int) else 0
        if rc == 0:
            rc = 0
    except RecursionError:
        rc = 101
        err.write(b"panic: stack overflow (recursion too deep)\n")
    except Exception as ex:  # noqa: BLE001 — interpreter bug
        rc = 1
        err.write(("internal error: %r\n%s"
                   % (ex, traceback.format_exc(limit=4))).encode("utf-8"))
    finally:
        sys.stderr = saved_err
    return rc, buf.getvalue(), err.getvalue()


def _run_native(driver_path, cfg):
    """Compile the driver with bin/hlc + gcc and run the binary.
    Build artifacts live in a private temp dir; the driver .hls (which
    must sit beside the suite for import resolution) is removed by the
    caller. Returns (rc, stdout_bytes, stderr_bytes)."""
    hlc = cfg["hlc"]
    cc = cfg["cc"]
    root = cfg["root"]
    if not os.path.exists(hlc):
        raise BenchFileError(
            "native backend needs the native compiler at %s - run "
            "`make bootstrap` first (or run without --native)"
            % hlc)
    with tempfile.TemporaryDirectory(prefix="hlbench-") as tmp:
        c_path = os.path.join(tmp, "driver.c")
        bin_path = os.path.join(tmp, "driver.bin")
        try:
            # The self-hosted compiler anchors a std./core. import on
            # the entry file's parent, then on the INVOKING cwd (the
            # boot compiler anchors on its own installation directory;
            # Deep-scan-25). A suite outside the repo tree compiles
            # with cwd = the repo root so `import "std.bench"` resolves
            # exactly where it resolves for the interpreter backend.
            r = subprocess.run([hlc, driver_path, c_path],
                               cwd=root, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=300)
        except subprocess.TimeoutExpired:
            return 1, b"", b"hlc: compile timed out after 300s"
        if r.returncode != 0:
            return 1, b"", r.stderr
        try:
            r = subprocess.run([cc, "-O2", "-o", bin_path, c_path,
                                "-lm", "-pthread"],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=300)
        except subprocess.TimeoutExpired:
            return 1, b"", b"gcc: compile timed out after 300s"
        if r.returncode != 0:
            return 1, b"", r.stderr
        try:
            r = subprocess.run([bin_path], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=600)
        except subprocess.TimeoutExpired:
            return 1, b"", b"driver: run timed out after 600s"
        return r.returncode, r.stdout, r.stderr


# ---------------------------------------------------------------------------
# Rendering — the criterion-style report and the --json payload.
# ---------------------------------------------------------------------------

_BRACKET = "[%s %s %s]"


def _bracket(lo, mid, hi):
    return _BRACKET % (fmt_time(lo), fmt_time(mid), fmt_time(hi))


def _render_bench(name, med, med_ci, mean, mean_ci, std, m, outliers,
                  n_samples, slope, units, thrpt_pt, thrpt_ci,
                  harness_per_iter, change, cfg):
    """The per-bench report lines (criterion's layout) and the --json
    payload. Returns (report_lines, json_obj)."""
    lines = []
    lines.append("time:   %s" % _bracket(med_ci[0], med, med_ci[1]))
    lines.append("mean:   %s  std dev: %s"
                 % (fmt_time(mean), fmt_time(std)))
    lines.append("median: %s  med abs dev: %s"
                 % (fmt_time(med), fmt_time(m)))
    if units:
        lo = "%.2f" % thrpt_ci[0] if thrpt_ci else _sig4(thrpt_pt)
        hi = "%.2f" % thrpt_ci[1] if thrpt_ci else _sig4(thrpt_pt)
        lines.append("thrpt:  [%s %s %s]  (%s)"
                     % (lo, _sig4(thrpt_pt), hi, fmt_rate(thrpt_pt)))
    if change:
        lo, hi = change["ci_pct"]
        rel = "%s %s %s" % (fmt_pct(lo), fmt_pct(change["point_pct"]),
                            fmt_pct(hi))
        cmp_op = "<" if change["p"] < _ALPHA else ">"
        lines.append("change: [%s] (p = %.2f %s %.2f%%)"
                     % (rel, change["p"], cmp_op, _ALPHA * 100.0))
        lines.append(change["note"])
    total_out = (outliers["low_severe"] + outliers["low_mild"]
                 + outliers["high_mild"] + outliers["high_severe"])
    if total_out:
        pct = total_out / float(n_samples) * 100.0
        lines.append("Found %d outliers among %d samples (%.2f%%)"
                     % (total_out, n_samples, pct))
        for key, label in (("low_severe", "low severe"),
                           ("low_mild", "low mild"),
                           ("high_mild", "high mild"),
                           ("high_severe", "high severe")):
            if outliers[key]:
                lines.append("  %d (%.2f%%) %s"
                             % (outliers[key],
                                outliers[key] / float(n_samples) * 100.0,
                                label))
    lines.append("harness: %s/iter (the loop every sample includes)"
                 % fmt_time(harness_per_iter))

    json_obj = {
        "name": name,
        "status": "ok",
        "samples": None,          # filled by the caller if cfg wants it
        "estimates_ns_per_iter": {
            "median": med,
            "mean": mean,
            "stddev": std,
            "mad": m,
            "slope": slope,
            "median_ci": [med_ci[0], med_ci[1]],
            "mean_ci": [mean_ci[0], mean_ci[1]],
        },
        "outliers": dict(outliers),
        "samples_count": n_samples,
        "units": units,
        "throughput_units_per_s": thrpt_pt,
        "throughput_ci": list(thrpt_ci) if thrpt_ci else None,
        "harness_ns_per_iter": harness_per_iter,
        "change": change,
    }
    return lines, json_obj


def _color(s, code):
    if not sys.stdout.isatty():
        return s
    return "\033[%sm%s\033" % (code, s)


def _green(s):  return _color(s, "32")
def _red(s):    return _color(s, "31")
def _yellow(s): return _color(s, "33")
def _dim(s):    return _color(s, "2")


def print_report(outcomes, cfg, total_s):
    """The full human report: a block per file, criterion's layout per
    bench, hltest's summary at the end. Returns the counts dict."""
    n_samples = cfg["plan"]["samples"]
    print("== hls-bench: %d file(s), backend=%s, %s plan, %d sample(s) =="
          % (len(outcomes), cfg["backend"], cfg["plan"]["mode"], n_samples))
    counts = {"ok": 0, "skip": 0, "fail": 0}
    for oc in outcomes:
        if not oc.results:
            continue
        print("")
        rel = os.path.relpath(oc.file) if os.path.isabs(oc.file) \
            else oc.file
        print("%s" % rel)
        width = max(len(r.name) for r in oc.results) + 2
        for r in oc.results:
            if r.status == "ok":
                counts["ok"] += 1
                pad = " " * (width + 2)
                first = True
                for line in r.report:
                    if first:
                        print("  %s%s" % (_green(r.name.ljust(width)),
                                          line))
                        first = False
                    else:
                        print("%s%s" % (pad, line))
            elif r.status == "skip":
                counts["skip"] += 1
                print("  %s  %s" % (_yellow(r.name.ljust(width)),
                                    _yellow(r.detail)))
            else:
                counts["fail"] += 1
                print("  %s  %s" % (_red(r.name.ljust(width)),
                                    _red("FAIL")))
                for dl in r.detail.splitlines():
                    print("        %s" % dl)
    print("")
    print("== hls-bench: %d bench measured, %d skip, %d fail in %.1f s =="
          % (counts["ok"], counts["skip"], counts["fail"], total_s))
    return counts


def write_json(outcomes, path, cfg):
    """The machine-readable report: the run's config, every file, every
    bench (skip/fail included as statuses), the raw samples for the
    measured ones, and the baseline change blocks."""
    files = []
    for oc in outcomes:
        benches = []
        for r in oc.results:
            if r.status == "ok" and r.json_obj is not None:
                obj = dict(r.json_obj)
                if cfg["json_samples"]:
                    obj["samples"] = _samples_of(r)
                benches.append(obj)
            elif r.status == "skip":
                benches.append({"name": r.name, "status": "skip",
                                "detail": r.detail})
            else:
                benches.append({"name": r.name, "status": "fail",
                                "detail": r.detail})
        rel = os.path.relpath(oc.file) if os.path.isabs(oc.file) \
            else oc.file
        files.append({"file": rel, "benches": benches})
    doc = {
        "tool": "hls-bench",
        "version": TOOL_VERSION,
        "backend": cfg["backend"],
        "plan": dict(cfg["plan"]),
        "noise_pct": cfg["noise_pct"],
        "files": files,
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(json_dumps_sorted(doc))


def _samples_of(_result):
    """Raw (iters, elapsed) sample pairs for --json. The runner keeps
    them in the json_obj under 'raw_samples' when collected."""
    raw = _result.json_obj.get("raw_samples") if _result.json_obj else None
    return raw


def _worker(task):
    filepath, cfg = task
    try:
        return run_file(filepath, cfg)
    except Exception as ex:  # noqa: BLE001
        r = BenchResult(filepath, "<worker>", "fail",
                        "worker crash: %r\n%s"
                        % (ex, traceback.format_exc(limit=4)))
        return FileOutcome(filepath, [r], [])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_plan(args):
    if args.fixed_iters is not None:
        if args.fixed_iters < 1:
            sys.stderr.write("hls-bench: --fixed-iters must be >= 1\n")
            sys.exit(2)
        return {"mode": "fixed", "samples": args.samples,
                "iters": args.fixed_iters}
    return {"mode": "adaptive",
            "samples": args.samples,
            "warmup_ns": int(args.warmup_ms * 1e6),
            "sample_time_ns": int(args.sample_time_ms * 1e6),
            "min_iters": args.min_iters,
            "max_iters": args.max_iters}


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(
        prog="hls-bench",
        description="Halis benchmark runner (Stage 124). Discovers "
                    "bench_* functions in .hls suite files and measures "
                    "them criterion-style.")
    ap.add_argument("files", nargs="*",
                    help=".hls suite files or directories containing "
                         "them (default: benches/ when it exists)")
    ap.add_argument("--dir", action="append", default=[],
                    help="directory to discover suites in (recursive)")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="recurse into positional directories")
    ap.add_argument("-j", "--jobs", type=int, default=1,
                    help="parallel worker count over suite files "
                         "(default: 1 — benches are timing-sensitive; "
                         "parallel runs are for throughput, not for "
                         "quiet numbers)")
    ap.add_argument("--grep", default=None,
                    help="only run benches whose name contains this "
                         "substring")
    ap.add_argument("--native", action="store_true",
                    help="measure the NATIVE backend (bin/hlc + gcc) "
                         "instead of the Stage-0 interpreter")
    ap.add_argument("--hlc", default=os.path.join(root, "bin", "hlc"),
                    help="path to the native compiler (--native)")
    ap.add_argument("--cc", default=os.environ.get("CC", "gcc"),
                    help="C compiler for --native (default: $CC or gcc)")
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                    help="samples per bench (default: %d)"
                         % DEFAULT_SAMPLES)
    ap.add_argument("--warmup-ms", type=float, default=DEFAULT_WARMUP_MS,
                    help="adaptive warmup budget in ms (default: %g)"
                         % DEFAULT_WARMUP_MS)
    ap.add_argument("--sample-time-ms", type=float,
                    default=DEFAULT_SAMPLE_TIME_MS,
                    help="target per-sample measurement time in ms "
                         "(default: %g)" % DEFAULT_SAMPLE_TIME_MS)
    ap.add_argument("--min-iters", type=int, default=DEFAULT_MIN_ITERS,
                    help="lower clamp on per-sample iterations "
                         "(default: %d)" % DEFAULT_MIN_ITERS)
    ap.add_argument("--max-iters", type=int, default=DEFAULT_MAX_ITERS,
                    help="upper clamp on per-sample iterations")
    ap.add_argument("--fixed-iters", type=int, default=None,
                    help="deterministic plan: every sample runs exactly "
                         "this many iterations, no warmup (the "
                         "differential mode — interpreter and native "
                         "emit the same plan)")
    ap.add_argument("--save", default=None,
                    help="record the run as baseline NAME under "
                         ".hlbench/ beside each suite file")
    ap.add_argument("--baseline", default=None,
                    help="compare the run against baseline NAME")
    ap.add_argument("--noise", dest="noise_pct", type=float,
                    default=DEFAULT_NOISE_PCT,
                    help="noise threshold for change classification in "
                         "%% (default: %g)" % DEFAULT_NOISE_PCT)
    ap.add_argument("--json", default=None,
                    help="write the machine-readable report to this file")
    ap.add_argument("--list", action="store_true",
                    help="list the benches each suite declares and exit")
    args = ap.parse_args()

    # ---- discovery.
    inputs = list(args.files) + list(args.dir)
    if not inputs:
        default_dir = os.path.join(os.getcwd(), "benches")
        if os.path.isdir(default_dir):
            inputs = [default_dir]
        else:
            ap.error("no input files (pass .hls suite paths, or use "
                     "--dir DIR; the default location is benches/)")
    files = discover_files(inputs, recurse=args.recursive)
    if not files:
        sys.stderr.write("hls-bench: no .hls suite files found\n")
        return 2

    if args.samples < 1:
        sys.stderr.write("hls-bench: --samples must be >= 1\n")
        return 2

    # ---- --list: what would run, without measuring.
    if args.list:
        for f in files:
            try:
                fns = _parse_entry(f)
            except (HLError, OSError) as ex:
                print("%s: error: %s" % (f, ex))
                continue
            benches = [n for n in fns if n.startswith("bench_")]
            units = {n[len("units_"):] for n in fns
                     if n.startswith("units_")}
            marks = ["%s%s" % (b, "*" if b[len("bench_"):] in units else "")
                     for b in benches]
            print("%s: %s" % (f, ", ".join(marks) if marks
                              else "(no bench_* functions)"))
        print("\n(* = units_<name> declared — the bench reports "
              "throughput)")
        return 0

    cfg = {
        "backend": "native" if args.native else "interpreter",
        "hlc": args.hlc,
        "cc": args.cc,
        "root": root,
        "grep": args.grep,
        "plan": _build_plan(args),
        "save": args.save,
        "baseline": args.baseline,
        "noise_pct": args.noise_pct,
        "json_samples": True,
    }

    # A baseline NAME that is also being --save'd is read BEFORE the
    # run overwrites it (compare-then-save, criterion's order).
    if args.baseline and args.save == args.baseline:
        sys.stderr.write(
            "hls-bench: note: comparing against and overwriting the "
            "same baseline %r in one run (compare happens first)\n"
            % args.baseline)

    # Pre-flight the baseline NAME before burning minutes of
    # measurement: a baseline that does not exist is a usage error
    # (exit 2, before the first bench runs). A baseline that EXISTS but
    # is corrupt stays a per-bench failing result (exit 1) — the
    # snapshot-store discipline: corruption is discovered where it
    # bites, named, and never guessed around.
    if args.baseline:
        missing = []
        for f in files:
            try:
                fns = _parse_entry(f)
            except (HLError, OSError):
                continue
            for b in [n for n in fns if n.startswith("bench_")]:
                if cfg["grep"] is not None and cfg["grep"] not in b:
                    continue
                if not os.path.exists(
                        baseline_path_for(f, b, args.baseline)):
                    missing.append((f, b))
        if missing:
            f0, b0 = missing[0]
            sys.stderr.write(
                "hls-bench: baseline %r does not exist (e.g. expected "
                "%s) - record one first: hls-bench --save %s ...\n"
                % (args.baseline,
                   baseline_path_for(f0, b0, args.baseline),
                   args.baseline))
            return 2

    print("== hls-bench: %d suite file(s) ==" % len(files),
          file=sys.stderr)
    t0 = time.perf_counter()
    outcomes = []
    if args.jobs <= 1 or len(files) == 1:
        for f in files:
            outcomes.append(run_file(f, cfg))
            _progress(files.index(f) + 1, len(files))
    else:
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(args.jobs) as pool:
            tasks = [(f, cfg) for f in files]
            for oc in pool.imap_unordered(_worker, tasks):
                outcomes.append(oc)
                _progress(len(outcomes), len(files))
    total_s = time.perf_counter() - t0

    print("")
    counts = print_report(outcomes, cfg, total_s)
    if args.json:
        write_json(outcomes, args.json, cfg)
        print("== json report written to %s ==" % args.json)
    return 0 if counts["fail"] == 0 else 1


def _progress(done, total):
    print("  [%d/%d] suites measured" % (done, total), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
