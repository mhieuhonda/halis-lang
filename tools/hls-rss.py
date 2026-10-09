#!/usr/bin/env python3
"""hls-rss — the Stage 125 RSS-stability verifier.

Usage:
  python3 tools/hls-rss.py workload.hls                  # verify: plan, sample, verdict
  python3 tools/hls-rss.py --cycles 400 workload.hls     # a bigger measured plan
  python3 tools/hls-rss.py --json r.json workload.hls    # machine-readable report
  python3 tools/hls-rss.py --no-malloc-count w.hls       # timeline instrument only
  python3 tools/hls-rss.py w.hls data.txt                # extra args reach the program

Stage 8 made the runtime garbage-collector-free: every heap object is
reference-counted and freed EXACTLY at scope exit. Stage 125 makes
that claim VERIFIED instead of believed: under a steady-state
workload, resident memory must not grow with the amount of work
performed. The before/after page reading that tests/memcheck/
stress_leak.hls prints (Stage 8-beta, suite section 3b) is a
two-point check; this tool is the two-instrument, statistical version
of it, drivable over ANY Halis program:

  1. the timeline  — the OS-level truth. The workload is compiled
     natively (bin/hlc + gcc -O2, the exact recipe every other gate
     uses) and launched with its stdout on a pty (glibc line-buffers
     a tty, so every line the program prints reaches the parent the
     moment it is written — the alignment the instrument depends on).
     A ticker thread samples /proc/<pid>/statm every --interval ms,
     giving the continuous resident-pages trace (start, end,
     high-water — the peaks BETWEEN markers included, which is the
     point of the ticker); a reader thread parses the program's
     output, and the instant it sees a cycle marker it takes its own
     reading — cycle-aligned (cycle, t, rss) points with no timer
     skew in the x coordinate.

  2. the retained set — the deterministic truth. When the workload
     cooperates with the plan (below), the tool runs it a SECOND time
     at the warmup-only cycle count and compares what each run's
     process still holds at exit, through the link-time interposer
     tools/rss_parts/malloc_balance_wrap.c (-Wl,--wrap=malloc/...):
     malloc/calloc/realloc/free events are counted (C11 atomics —
     the runtime spawns real pthreads) and the LIVE balance
     (allocations minus frees) is printed at exit. A leak retains an
     object per cycle, so live grows with the work volume; a clean
     exact-free runtime ends both runs at the same small constant.
     RSS page granularity cannot hide a single retained object from
     this instrument — and the interposer, in turn, cannot see
     address-space effects (fragmentation, arena pre-sizing), which
     is what the RSS high-water comparison alongside it is for.

The convention (the established one — no attributes, no closures:
naming plus data, zero checker or codegen edits): the workload is an
ordinary Halis program; a CYCLE MARKER is a line it prints once per
steady-state cycle,

    __HLRSS_CYCLE__ <i>

with i strictly increasing from 0. The first --warmup cycles are
warmup (the working set fills: allocator bins, stdio buffers, caches)
and are excluded from the slope fit; the measured phase must be flat.
The tool drives its plan through argv[1]: a COOPERATIVE workload reads
its cycle count from args().get(1) (falling back to an in-code default
when absent); a workload that ignores argv still verifies through the
timeline — the retained instrument simply reports itself skipped, with
the reason, the way every instrument here reports what it could not
do. A program that prints no markers at all is analysed against time
instead of cycles (a weaker, honestly-labelled mode: pages/second).

The wire protocol (line-oriented, the reserved-prefix discipline of
Stage 119's snapshots and Stage 124's bench stream): a line STARTING
with `__HLRSS_CYCLE__ ` must parse exactly (an integer, one greater
than the previous marker); a line merely CONTAINING `__HLRSS_` that is
not a record is a protocol error — a program that prints protocol
look-alikes corrupts the stream and fails loudly, never silently.
Any other output the program prints is captured and ignored: a
workload may talk.

The statistics: the slope of resident pages against cycle index is
Theil-Sen — the median of pairwise slopes, robust to the page-step
jumps an allocator's heap-top growth inserts into an otherwise flat
trace — with a percentile-bootstrap confidence interval seeded from
the workload's name (the same data always yields the same interval,
so a --json report is diffable; re-running the WORKLOAD of course
yields new samples, only the analysis is deterministic). Traces are
stride-decimated to 500 points before the pairwise fit — the O(n^2)
median over a multi-thousand-marker run buys no robustness.

The verdicts (per instrument, then composed): STABLE when the slope
CI lies entirely under --threshold pages/cycle (default 0.25 — one
leaked KiB per cycle, a real leak, comfortably above RSS page noise
after the Theil-Sen median); LEAK when the CI lies entirely above it;
INCONCLUSIVE when the CI straddles it (a verifier must be able to say
"cannot certify", and saying it is a failure for CI). The retained
instrument is STABLE when |live delta| <= --live-slack (default 0 —
the balance is exact) and the high-water delta <= --rss-slack pages
(default 256); a live delta beyond the slack is a LEAK (retained
objects grow with work volume), a high-water-only breach is
INCONCLUSIVE (page-level growth with a balanced ledger — not a
proven leak, and not a certification). The composition is the
strictest voice: any LEAK -> LEAK; else any INCONCLUSIVE ->
INCONCLUSIVE; else STABLE.

Native only, on purpose: the GC-free claim is a claim about the
runtime the compiler EMITS. The Stage-0 interpreter's RSS is the
Python process's RSS — measuring it would certify Python's
allocator, so the tool does not have an interpreter mode at all.
Likewise the sampler is /proc/<pid>/statm (Linux, or FreeBSD with
procfs): elsewhere the tool refuses with exit 2 rather than measure
nothing honestly, and the interposer needs GNU ld's --wrap (probed
once per invocation; unsupported -> the retained instrument skips
itself, honestly, unless --malloc-count forces it).

Exit codes:
  0  certified STABLE
  1  not certified: LEAK, INCONCLUSIVE, a program failure (non-zero
     exit, timeout, protocol error) or an instrument failure
  2  usage / environment: missing file, bad option values, no
     bin/hlc, no gcc, no /proc sampler
"""

import argparse
import json
import math
import os
import pty
import random
import subprocess
import sys
import tempfile
import threading
import time
import zlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOOL_VERSION = "0.143.0-alpha"

# The reserved protocol prefixes (the Stage 119/124 discipline: a
# record line must parse exactly; a stray stem is a loud error).
_PROTO_CYCLE = b"__HLRSS_CYCLE__ "
_PROTO_STEM = b"__HLRSS_"

# The interposer's stderr report prefixes (tools/rss_parts/
# malloc_balance_wrap.c) — every counter is HL_RSS_<NAME>=<int>.
_INTERPOSER_KEYS = ("MALLOC", "CALLOC", "REALLOC", "REALLOC_NULL",
                    "REALLOC_ZERO", "FREE", "FOREIGN_FREE", "LIVE",
                    "OVERFLOW")

# Stride-decimation bound for the pairwise slope fit (O(n^2) medians
# over traces larger than this buy no robustness).
_FIT_POINT_CAP = 500

_WRAP_C = os.path.join(REPO_ROOT, "tools", "rss_parts",
                       "malloc_balance_wrap.c")
_WRAP_FLAGS = ("-Wl,--wrap=malloc", "-Wl,--wrap=calloc",
               "-Wl,--wrap=realloc", "-Wl,--wrap=free")


class UsageError(Exception):
    """Exit 2: bad invocation or unusable environment."""


class VerifyError(Exception):
    """Exit 1: the run happened, the certification did not."""


# ---------------------------------------------------------------------------
# The protocol: marker lines in the workload's stdout.
# ---------------------------------------------------------------------------

def parse_marker_line(line):
    """Return the cycle index of a marker line, or None when the line
    is not a marker. Raises VerifyError on a malformed record (a line
    that starts with the record prefix but does not parse) — the
    reserved-prefix discipline."""
    if not line.startswith(_PROTO_CYCLE):
        return None
    payload = line[len(_PROTO_CYCLE):].strip()
    try:
        idx = int(payload)
    except ValueError:
        raise VerifyError(
            "protocol error: malformed cycle marker %r (expected "
            "`__HLRSS_CYCLE__ <int>`)" % line[:120].decode("utf-8",
                                                            "replace"))
    return idx


def scan_protocol_violation(line):
    """A line that merely CONTAINS the reserved stem but is not a
    record corrupts the stream: fail loudly, never silently drop."""
    if _PROTO_STEM in line and not line.startswith(_PROTO_CYCLE):
        raise VerifyError(
            "protocol error: reserved stem in a non-record line %r"
            % line[:120].decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Statistics — Theil-Sen slope, seeded bootstrap CI, quantiles.
# ---------------------------------------------------------------------------

def _median(sorted_xs):
    n = len(sorted_xs)
    if n == 0:
        raise VerifyError("median of empty data")
    mid = n // 2
    if n % 2:
        return sorted_xs[mid]
    return (sorted_xs[mid - 1] + sorted_xs[mid]) / 2.0


def quantile(sorted_xs, q):
    """Linear-interpolation quantile on ALREADY-SORTED data (the same
    estimator hls-bench uses, so the two tools agree by construction)."""
    n = len(sorted_xs)
    if n == 0:
        raise VerifyError("quantile of empty data")
    if n == 1:
        return sorted_xs[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_xs[lo] * (1.0 - frac) + sorted_xs[hi] * frac


def theil_sen(points):
    """The Theil-Sen slope: the median of pairwise slopes between all
    (x_i, y_i), x_i != x_j. Robust to the page-step jumps an
    allocator's heap-top growth inserts into an otherwise flat trace
    (a step moves half the pairs' slopes by a bounded amount; the
    median barely moves). Returns (slope, pair_count)."""
    pts = sorted(points)
    slopes = []
    n = len(pts)
    for i in range(n):
        xi, yi = pts[i]
        for j in range(i + 1, n):
            xj, yj = pts[j]
            dx = xj - xi
            if dx <= 0:
                continue
            slopes.append((yj - yi) / float(dx))
    if not slopes:
        raise VerifyError("theil-sen: no distinct-x pairs in data")
    slopes.sort()
    return _median(slopes), len(slopes)


# Each bootstrap resample recomputes a pairwise median; over a
# multi-hundred-point trace that is O(n^2) per resample and the CI
# would cost minutes. A seeded random subsample of pairs is the
# standard speedup: the median over a random pair subsample is a
# consistent estimator of the pair-median, and the seed keeps the
# whole interval byte-stable for the same data.
_BOOTSTRAP_PAIR_CAP = 3000


def bootstrap_ci_slope(points, b, seed):
    """Percentile-bootstrap CI for the Theil-Sen slope: resample the
    points with replacement, recompute the slope (over a seeded
    subsample of pairs when the trace is large), take the 2.5% and
    97.5% quantiles. Seeded from the workload name — the same data
    always yields the same interval (report bytes are diffable)."""
    rng = random.Random(seed)
    n = len(points)
    all_pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    stats = []
    for _ in range(b):
        sample = [points[rng.randrange(n)] for _ in range(n)]
        pairs = (rng.sample(all_pairs, _BOOTSTRAP_PAIR_CAP)
                 if len(all_pairs) > _BOOTSTRAP_PAIR_CAP else all_pairs)
        slopes = []
        for i, j in pairs:
            xi, yi = sample[i]
            xj, yj = sample[j]
            dx = xj - xi
            if dx > 0:
                slopes.append((yj - yi) / float(dx))
        if slopes:
            slopes.sort()
            stats.append(_median(slopes))
    if len(stats) < max(10, b // 10):
        raise VerifyError("bootstrap: too few valid resamples")
    stats.sort()
    return quantile(stats, 0.025), quantile(stats, 0.975)


def decimate(points, cap=_FIT_POINT_CAP):
    """Deterministic stride decimation to at most `cap` points (the
    first and the last point are always kept: the span is the span;
    the stride is computed against cap-1 so the re-appended endpoint
    cannot push the result past the cap)."""
    if len(points) <= cap:
        return list(points)
    step = math.ceil(len(points) / float(cap - 1))
    out = points[::step]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def _seed(name):
    return zlib.crc32(name.encode("utf-8"))


# ---------------------------------------------------------------------------
# RSS sampling — /proc/<pid>/statm, field 2 (resident), in pages.
# ---------------------------------------------------------------------------

_PAGE_SIZE = os.sysconf("SC_PAGESIZE") if hasattr(os, "sysconf") else 4096


def sampler_available():
    """The instrument needs /proc/<pid>/statm. Refuse honestly where
    it does not exist instead of measuring nothing."""
    return os.path.isdir("/proc/self") and \
        os.path.exists("/proc/self/statm")


def rss_pages(pid):
    """Resident pages of `pid`, or None when the process is gone (the
    zombie window after exit keeps its /proc entry with every field
    zeroed — a live process never has 0 resident pages, so a zero
    reading IS the "gone" signal and must not enter the trace; the
    reaped pid has no entry at all)."""
    try:
        with open("/proc/%d/statm" % pid, "rb") as f:
            parts = f.read().split()
        pages = int(parts[1])
        return pages if pages > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def fmt_pages(pages):
    """Human form: pages and KiB side by side (RSS is page-granular;
    the KiB is what a human compares against top(1))."""
    return "%d pages (%s KiB)" % (pages, fmt_kib(pages * _PAGE_SIZE))


def fmt_kib(nbytes):
    return "%d" % (nbytes // 1024)


def fmt_slope(v, per):
    """A slope with its unit; significant digits only — a slope of
    0.0134 page/cycle does not need seven decimals."""
    if abs(v) >= 100:
        s = "%.1f" % abs(v)
    elif abs(v) >= 1:
        s = "%.3f" % abs(v)
    else:
        s = ("%.4f" % abs(v)).rstrip("0").rstrip(".")
        if s in ("", "0"):
            s = "0"
    sign = "+" if v > 0 else ("" if v == 0 else "-")
    return "%s%s%s" % (sign, s, (" " + per) if per else "")


# ---------------------------------------------------------------------------
# The interposer — probe once, compile once, parse its report.
# ---------------------------------------------------------------------------

def probe_wrap(cc):
    """Compile the balance interposer and probe whether the linker
    understands -Wl,--wrap (GNU ld / BFD do; ld64 does not). Returns
    the .o path inside a caller-owned temp dir, or None."""
    try:
        with tempfile.TemporaryDirectory(prefix="hlrss-probe-") as tmp:
            obj = os.path.join(tmp, "balance.o")
            r = subprocess.run([cc, "-O2", "-c", _WRAP_C, "-o", obj],
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=120)
            if r.returncode != 0:
                return None
            probe_c = os.path.join(tmp, "probe.c")
            with open(probe_c, "w", encoding="utf-8") as f:
                f.write('#include <stdlib.h>\n'
                        'int main(void) { void* p = malloc(8);'
                        ' free(p); return p ? 0 : 1; }\n')
            probe_bin = os.path.join(tmp, "probe.bin")
            r = subprocess.run([cc, "-O2", "-o", probe_bin, probe_c, obj,
                                "-lm", "-pthread"] + list(_WRAP_FLAGS),
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=120)
            if r.returncode != 0:
                return None
            # The probe must not only link but RUN: a wrap that broke
            # the allocation path would poison every measurement.
            try:
                subprocess.run([probe_bin], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return None
            return obj
    except (OSError, subprocess.TimeoutExpired):
        return None


def build_interposer(cc, tmpdir):
    """Compile the interposer .o into `tmpdir` (probe_wrap already
    proved the toolchain can). Returns the .o path."""
    obj = os.path.join(tmpdir, "malloc_balance.o")
    r = subprocess.run([cc, "-O2", "-c", _WRAP_C, "-o", obj],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=120)
    if r.returncode != 0:
        raise UsageError("cannot compile the balance interposer:\n%s"
                         % r.stderr.decode("utf-8", "replace"))
    return obj


def parse_interposer(stderr_bytes):
    """Extract the HL_RSS_*=<n> counters from the program's stderr.
    Returns the counter dict, or None when no report is present (the
    interposer was not linked, or the process died by a signal — which
    the caller distinguishes by its own knowledge of the run)."""
    if not stderr_bytes:
        return None
    counters = {}
    for raw in stderr_bytes.splitlines():
        if not raw.startswith(b"HL_RSS_"):
            continue
        try:
            key, _, val = raw.partition(b"=")
            counters[key[len(b"HL_RSS_"):].decode("ascii")] = int(val)
        except (ValueError, UnicodeDecodeError):
            return None
    if any(k not in counters for k in _INTERPOSER_KEYS):
        return None
    # The LIVE identity is recomputed from the components: the
    # interposer prints its own arithmetic, and the verifier trusts
    # neither side's alone. (FREE counts only tracked pointers — a
    # FOREIGN_FREE is libc's own buffer handed back to free(), not a
    # balance event.)
    live = (counters["MALLOC"] + counters["CALLOC"]
            + counters["REALLOC_NULL"] - counters["FREE"]
            - counters["REALLOC_ZERO"])
    if live != counters["LIVE"]:
        return None
    return counters


# ---------------------------------------------------------------------------
# Native build — the exact recipe every other gate uses.
# ---------------------------------------------------------------------------

def compile_workload(workload, cfg, tmpdir, wrap_obj):
    """bin/hlc -> C -> gcc -O2 binary. hlc runs with cwd = the repo
    root so `import "std.***"` resolves exactly where it resolves for
    every other gate (the self-hosted compiler anchors std./core.
    imports on the entry file's parent, then on the invoking cwd —
    Deep-scan-25). Returns the binary path; raises UsageError on a
    compile failure (exit 2: a workload that does not build is an
    environment problem, not a verdict)."""
    hlc = cfg["hlc"]
    cc = cfg["cc"]
    if not os.path.isfile(hlc):
        raise UsageError("native verification needs the native compiler "
                         "at %s — run `make bootstrap` first" % hlc)
    c_path = os.path.join(tmpdir, "workload.c")
    bin_path = os.path.join(tmpdir, "workload.bin")
    try:
        r = subprocess.run([hlc, os.path.abspath(workload), c_path],
                           cwd=REPO_ROOT, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=300)
    except subprocess.TimeoutExpired:
        raise UsageError("hlc: compile timed out after 300s")
    if r.returncode != 0:
        raise UsageError("hlc refused the workload:\n%s"
                         % r.stderr.decode("utf-8", "replace"))
    cmd = [cc, "-O2", "-o", bin_path, c_path, "-lm", "-pthread"]
    if wrap_obj:
        cmd += [wrap_obj] + list(_WRAP_FLAGS)
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=300)
    except subprocess.TimeoutExpired:
        raise UsageError("gcc: compile timed out after 300s")
    if r.returncode != 0:
        raise UsageError("gcc refused the workload:\n%s"
                         % r.stderr.decode("utf-8", "replace"))
    return bin_path


# ---------------------------------------------------------------------------
# The run — pty stdout, ticker + reader threads, the trace they build.
# ---------------------------------------------------------------------------

class RunTrace(object):
    """Everything one execution of the workload yielded."""

    def __init__(self):
        self.rc = None
        self.timed_out = False
        self.timeline = []        # [(t_ms, pages)] — the ticker's trace
        self.cycle_points = []    # [(cycle, t_ms, pages)] — marker-aligned
        self.markers = []         # observed cycle indices, in order
        self.protocol_error = None
        self.interposer = None    # counter dict or None
        self.stderr_tail = b""    # last 4 KiB (panic messages)
        self.elapsed_s = 0.0

    # -- derived views ----------------------------------------------------

    def high_water(self):
        """The peak resident size over BOTH views: a fast run may
        finish between ticker ticks, and the cycle-aligned readings
        then hold the true peak the ticker never saw."""
        pages = [p for _, p in self.timeline]
        pages += [p for (_c, _t, p) in self.cycle_points]
        return max(pages) if pages else None

    def first_pages(self):
        return self.timeline[0][1] if self.timeline else None

    def last_pages(self):
        return self.timeline[-1][1] if self.timeline else None


def _ticker(pid, stop, interval_ms, trace, t0, bin_path=None):
    """Sample /proc/<pid>/statm every interval until stopped. Samples
    of a process that is not (or no longer) the workload binary are
    skipped: between Popen's fork() and exec() the child is a copy of
    the PYTHON interpreter's image (a ~30 MiB resident set that has
    nothing to do with the workload), and after exit the /proc entry
    is a zombie or gone. /proc/<pid>/exe is the witness: sample only
    while it still resolves to the binary we launched."""
    exe = "/proc/%d/exe" % pid
    while True:
        if stop.is_set():
            return
        if bin_path is None or _exe_is(exe, bin_path):
            pages = rss_pages(pid)
            if pages is not None:
                trace.timeline.append(((time.monotonic() - t0) * 1000.0,
                                       pages))
        stop.wait(interval_ms / 1000.0)


def _exe_is(proc_exe, bin_path):
    """True when /proc/<pid>/exe still resolves to the workload
    binary (post-exec, pre-exit). A pre-exec child resolves to the
    Python interpreter; a reaped pid raises."""
    try:
        return os.path.realpath(proc_exe) == bin_path
    except OSError:
        return False


def run_workload(bin_path, argv, cfg):
    """Run the binary once under the sampler. stdout is a pty (glibc
    line-buffers a tty, so each println reaches the parent as it is
    written — the marker alignment depends on it; ONLCR maps \\n to
    \\r\\n, which the reader strips). stderr is a pipe (the
    interposer reports at exit; it needs no alignment). Returns a
    RunTrace; raises VerifyError on a timeout."""
    trace = RunTrace()
    mfd, sfd = pty.openpty()
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        try:
            proc = subprocess.Popen(
                [bin_path] + list(argv),
                stdout=sfd, stderr=subprocess.PIPE, stdin=devnull,
                close_fds=True)
        except OSError as e:
            raise UsageError("cannot launch the workload: %s" % e)
        finally:
            os.close(sfd)
            os.close(devnull)
    except Exception:
        os.close(mfd)
        raise

    t0 = time.monotonic()
    stop = threading.Event()

    def reader():
        """Line reader: markers -> cycle-aligned RSS snapshots; any
        other line: protocol-scan then ignore."""
        last_cycle = None
        try:
            while True:
                raw = os.read(mfd, 65536)
                if not raw:
                    break
                for line in raw.split(b"\n"):
                    line = line.rstrip(b"\r")
                    if not line:
                        continue
                    try:
                        scan_protocol_violation(line)
                        idx = parse_marker_line(line)
                    except VerifyError as e:
                        trace.protocol_error = str(e)
                        # The stream is corrupt: kill the producer or a
                        # chatty workload blocks on the full pty buffer
                        # while communicate() waits on stderr — a
                        # deadlock the timeout would only paper over.
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        return
                    if idx is None:
                        continue
                    if last_cycle is not None and idx <= last_cycle:
                        trace.protocol_error = (
                            "protocol error: cycle marker %d does not "
                            "advance (previous %d)" % (idx, last_cycle))
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        return
                    last_cycle = idx
                    pages = rss_pages(proc.pid)
                    trace.markers.append(idx)
                    if pages is not None:
                        trace.cycle_points.append(
                            (idx, (time.monotonic() - t0) * 1000.0,
                             pages))
        except OSError:
            pass  # EIO on Linux once the slave side closed: EOF
        finally:
            try:
                os.close(mfd)
            except OSError:
                pass

    rt = threading.Thread(target=reader, daemon=True)
    tt = threading.Thread(target=_ticker,
                          args=(proc.pid, stop, cfg["interval"], trace,
                                t0, os.path.realpath(bin_path)),
                          daemon=True)
    rt.start()
    tt.start()
    try:
        out_err = None
        try:
            _, out_err = proc.communicate(timeout=cfg["timeout"])
        except subprocess.TimeoutExpired:
            trace.timed_out = True
            proc.kill()
            proc.communicate()
            raise VerifyError("workload timed out after %.0fs"
                              % cfg["timeout"])
    finally:
        stop.set()
        tt.join(timeout=5)
        rt.join(timeout=5)
        trace.rc = proc.returncode
        trace.elapsed_s = time.monotonic() - t0
        if out_err is not None:
            trace.stderr_tail = out_err[-4096:]
    trace.interposer = parse_interposer(trace.stderr_tail)
    return trace


# ---------------------------------------------------------------------------
# Analysis — the two instruments, their verdicts, the composition.
# ---------------------------------------------------------------------------

STABLE = "stable"
LEAK = "leak"
INCONCLUSIVE = "inconclusive"


def analyse_slope(trace, warmup, cfg, seed_name):
    """The timeline instrument: Theil-Sen slope of resident pages
    against cycle index (or against time when the program printed no
    markers — the weaker, honestly-labelled mode). Returns a dict with
    the verdict, or an INCONCLUSIVE dict naming what was missing."""
    out = {"instrument": "timeline", "mode": None, "verdict": None,
           "slope": None, "ci": None, "threshold": None, "unit": None,
           "note": None}
    if trace.protocol_error:
        out["verdict"] = INCONCLUSIVE
        out["note"] = trace.protocol_error
        return out
    pts = [(c, p) for (c, _t, p) in trace.cycle_points
           if c >= warmup]
    mode = "cycle"
    threshold = cfg["threshold"]
    unit = "page/cycle"
    if len(trace.cycle_points) < 8 and len(trace.timeline) >= 8:
        # No (or almost no) markers: analyse against wall-clock time.
        # The warmup split is unknown without markers, so the same
        # warmup/(warmup+cycles) FRACTION of the trace is excluded by
        # analogy with the cycle plan — a steady-state workload spends
        # that fraction filling its working set either way. A
        # pages/second threshold is machine-dependent by nature; the
        # report says so, and the default errs on the lenient side.
        mode = "time"
        frac = warmup / float(warmup + cfg["cycles"])
        if trace.timeline:
            t_cut = trace.timeline[-1][0] * frac
            pts = [(int(t), p) for (t, p) in trace.timeline if t >= t_cut]
        threshold = cfg["threshold_time"]
        unit = "page/s"
    out["mode"] = mode
    out["unit"] = unit
    out["threshold"] = threshold
    if len(pts) < 8:
        out["verdict"] = INCONCLUSIVE
        out["note"] = ("only %d sampled points after the warmup split — "
                       "the plan is too small to certify anything"
                       % len(pts))
        return out
    fit = decimate(pts)
    slope, pairs = theil_sen(fit)
    lo, hi = bootstrap_ci_slope(fit, cfg["bootstrap"], _seed(seed_name))
    out["slope"] = slope
    out["ci"] = (lo, hi)
    out["pairs"] = pairs
    out["points"] = len(fit)
    if hi < threshold:
        out["verdict"] = STABLE
        out["note"] = ("slope CI entirely within the threshold — "
                       "resident memory does not grow with work")
    elif lo > threshold:
        out["verdict"] = LEAK
        out["note"] = ("slope CI entirely above the threshold — resident "
                       "memory grows with the work performed")
    else:
        out["verdict"] = INCONCLUSIVE
        out["note"] = ("slope CI straddles the threshold — cannot "
                       "certify; widen the plan or raise the threshold")
    return out


def analyse_retained(trace_full, trace_warm, cfg):
    """The retained-set instrument: the same binary run at the full
    plan (warmup + measured) and at warmup-only, compared through the
    interposer's LIVE balance and the two RSS high-water marks. Only
    meaningful when both runs cooperated (observed == requested) and
    the interposer reported; the caller checks and this function
    receives honest inputs."""
    out = {"instrument": "retained", "verdict": None,
           "live_delta": None, "live_full": None, "live_warm": None,
           "hw_delta": None, "hw_full": None, "hw_warm": None,
           "note": None}
    a = trace_full.interposer
    b = trace_warm.interposer
    out["live_full"] = a["LIVE"] if a else None
    out["live_warm"] = b["LIVE"] if b else None
    out["hw_full"] = trace_full.high_water()
    out["hw_warm"] = trace_warm.high_water()
    if a is None or b is None:
        out["verdict"] = INCONCLUSIVE
        out["note"] = "the interposer report is missing from a clean run"
        return out
    if a["OVERFLOW"] or b["OVERFLOW"]:
        out["verdict"] = INCONCLUSIVE
        out["note"] = ("the tracking table overflowed — the live balance "
                       "degraded to event counting; refuse to certify")
        return out
    out["live_delta"] = a["LIVE"] - b["LIVE"]
    live_bad = abs(out["live_delta"]) > cfg["live_slack"]
    # The page-level leg needs BOTH high-water marks measured: a
    # workload faster than the sampling interval leaves no ticker
    # readings, and an instrument that measured nothing must say so,
    # not report a zero delta it never saw.
    if out["hw_full"] is None or out["hw_warm"] is None:
        out["hw_delta"] = None
        if live_bad and out["live_delta"] > 0:
            out["verdict"] = LEAK
            out["note"] = ("the full plan's process holds %d more live "
                           "objects at exit — retained memory grows with "
                           "the work volume (no page-level readings: the "
                           "workload outruns the sampler)"
                           % out["live_delta"])
        elif live_bad:
            out["verdict"] = INCONCLUSIVE
            out["note"] = ("live balance moved the wrong way (%d) — not "
                           "a leak, not a certification"
                           % out["live_delta"])
        else:
            out["verdict"] = STABLE
            out["note"] = ("the retained set is independent of the work "
                           "volume — the exact-free balance, observed "
                           "(no page-level readings: the workload outruns "
                           "the sampler)")
        return out
    out["hw_delta"] = out["hw_full"] - out["hw_warm"]
    hw_bad = out["hw_delta"] > cfg["rss_slack"]
    if live_bad and out["live_delta"] > 0:
        out["verdict"] = LEAK
        out["note"] = ("the full plan's process holds %d more live "
                       "objects at exit — retained memory grows with "
                       "the work volume" % out["live_delta"])
    elif live_bad:
        out["verdict"] = INCONCLUSIVE
        out["note"] = ("live balance moved the wrong way (%d) — not a "
                       "leak, not a certification" % out["live_delta"])
    elif hw_bad:
        out["verdict"] = INCONCLUSIVE
        out["note"] = ("live balances match but the high-water mark grew "
                       "%d pages — page-level growth without retained "
                       "objects (fragmentation?), not a certification"
                       % out["hw_delta"])
    else:
        out["verdict"] = STABLE
        out["note"] = ("the retained set is independent of the work "
                       "volume — the exact-free property, observed")
    return out


def compose_verdict(instruments):
    """The strictest voice wins: any leak is a leak; otherwise any
    unresolved question is an unresolved question; only a clean sheet
    certifies."""
    verdicts = [i["verdict"] for i in instruments if i["verdict"] is not None]
    if LEAK in verdicts:
        return LEAK
    if INCONCLUSIVE in verdicts:
        return INCONCLUSIVE
    if not verdicts:
        return INCONCLUSIVE
    return STABLE


# ---------------------------------------------------------------------------
# Rendering — the report and the --json payload.
# ---------------------------------------------------------------------------

_VERDICT_WORD = {STABLE: "STABLE", LEAK: "LEAK",
                 INCONCLUSIVE: "INCONCLUSIVE"}


def render_report(workload, cfg, plan, trace_a, trace_b, slope, retained,
                  verdict):
    """The human report — the numbers an engineer would quote in a
    bug report, each labelled with the instrument that produced it."""
    lines = []
    add = lines.append
    add("hls-rss %s — RSS-stability verifier (Stage 125)" % TOOL_VERSION)
    add("workload: %s (native, gcc -O2)" % os.path.basename(workload))
    add("plan: warmup %d + measured %d cycles, sampler %.0f ms, "
        "timeout %.0fs" % (plan["warmup"], plan["cycles"], cfg["interval"],
                           cfg["timeout"]))
    add("")
    # -- the timeline ----------------------------------------------------
    add("timeline (external sampler, /proc/<pid>/statm)")
    if trace_a.timeline:
        add("  samples:           %d over %.2f s"
            % (len(trace_a.timeline), trace_a.elapsed_s))
        hw = trace_a.high_water()
        add("  high-water:        %s" % fmt_pages(hw))
        if trace_a.cycle_points:
            add("  cycle markers:     %d (0..%d)"
                % (len(trace_a.markers), trace_a.markers[-1]))
            warm_pts = [(c, p) for (c, _t, p) in trace_a.cycle_points
                        if c < plan["warmup"]]
            meas_pts = [(c, p) for (c, _t, p) in trace_a.cycle_points
                        if c >= plan["warmup"]]
            if warm_pts:
                add("  rss at warmup end: %s" % fmt_pages(warm_pts[-1][1]))
            if meas_pts:
                add("  rss at end:        %s" % fmt_pages(meas_pts[-1][1]))
        else:
            add("  cycle markers:     none (time-based analysis)")
            add("  rss at start:      %s" % fmt_pages(trace_a.first_pages()))
            add("  rss at end:        %s" % fmt_pages(trace_a.last_pages()))
    else:
        add("  no samples (sampler produced no readings)")
    if slope:
        if slope.get("slope") is not None:
            add("  slope:             %s [95%% CI %s .. %s]"
                % (fmt_slope(slope["slope"], slope["unit"]),
                   fmt_slope(slope["ci"][0], ""),
                   fmt_slope(slope["ci"][1], "")))
            add("  threshold:         %s" % fmt_slope(slope["threshold"],
                                                      slope["unit"]))
        add("  verdict:           %s%s"
            % (_VERDICT_WORD[slope["verdict"]],
               (" — " + slope["note"]) if slope.get("note") else ""))
    add("")
    # -- the retained set ------------------------------------------------
    add("retained (two-run, malloc interposer)")
    if retained is None:
        add("  skipped — %s" % plan["retained_skip_reason"])
    else:
        add("  run A: %d cycles -> live %s, high-water %s"
            % (plan["warmup"] + plan["cycles"],
               retained["live_full"],
               fmt_pages(retained["hw_full"])
               if retained["hw_full"] is not None else "no readings"))
        add("  run B: %d cycles  -> live %s, high-water %s"
            % (plan["warmup"], retained["live_warm"],
               fmt_pages(retained["hw_warm"])
               if retained["hw_warm"] is not None else "no readings"))
        if retained["live_delta"] is not None:
            add("  live delta:        %d objects (slack %d)"
                % (retained["live_delta"], cfg["live_slack"]))
            if (trace_a.interposer or {}).get("FOREIGN_FREE") \
                    or (trace_b.interposer or {}).get("FOREIGN_FREE"):
                add("  foreign frees:    %d + %d (libc buffers handed back "
                    "to free — not balance events)"
                    % ((trace_a.interposer or {}).get("FOREIGN_FREE", 0),
                       (trace_b.interposer or {}).get("FOREIGN_FREE", 0)))
        if retained["hw_delta"] is not None:
            add("  high-water delta:  %+d pages (slack %d)"
                % (retained["hw_delta"], cfg["rss_slack"]))
        add("  verdict:           %s%s"
            % (_VERDICT_WORD[retained["verdict"]],
               (" — " + retained["note"]) if retained.get("note") else ""))
    add("")
    if verdict == STABLE:
        add("RSS STABLE — the runtime is leak-free under this workload.")
    elif verdict == LEAK:
        add("RSS LEAK — resident memory grows with the work performed.")
    else:
        add("RSS INCONCLUSIVE — the instruments could not certify this "
            "run; their notes above say why.")
    return "\n".join(lines)


def build_json(workload, cfg, plan, trace_a, trace_b, slope, retained,
               verdict):
    """The machine payload: config, the raw traces, both instruments'
    numbers and the composed verdict. Byte-stable for the same input
    data (sorted keys, fixed separators, no timestamps) — an analysis
    is diffable; a re-RUN of the workload of course produces new
    samples, which is the honest boundary."""
    payload = {
        "tool": "hls-rss",
        "version": TOOL_VERSION,
        "workload": os.path.basename(workload),
        "config": {k: cfg[k] for k in
                   ("cycles", "warmup", "interval", "timeout",
                    "threshold", "threshold_time", "rss_slack",
                    "live_slack", "bootstrap")},
        "plan": {k: plan[k] for k in ("warmup", "cycles",
                                      "retained_skip_reason")
                 if plan.get(k) is not None},
        "run_a": {
            "requested_cycles": plan["warmup"] + plan["cycles"],
            "observed_markers": len(trace_a.markers),
            "rc": trace_a.rc,
            "elapsed_s": round(trace_a.elapsed_s, 3),
            "timeline": [[round(t, 3), p] for (t, p) in trace_a.timeline],
            "cycle_points": [[c, round(t, 3), p]
                             for (c, t, p) in trace_a.cycle_points],
            "interposer": trace_a.interposer,
        },
        "slope": {k: (list(v) if isinstance(v, tuple) else v)
                  for k, v in slope.items()} if slope else None,
        "retained": ({k: v for k, v in retained.items()
                      if k != "instrument"} if retained else None),
        "verdict": verdict,
    }
    if trace_b is not None:
        payload["run_b"] = {
            "requested_cycles": plan["warmup"],
            "observed_markers": len(trace_b.markers),
            "rc": trace_b.rc,
            "elapsed_s": round(trace_b.elapsed_s, 3),
            "high_water_pages": trace_b.high_water(),
            "interposer": trace_b.interposer,
        }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

def verify(workload, cfg):
    """The full pipeline: probe, compile, run A, analyse, run B when
    cooperative, compose, render. Returns (report_text, json_text,
    verdict) — never an exception for a MEASUREMENT outcome (those are
    verdicts); UsageError still escapes for environment problems."""
    if not os.path.isfile(workload):
        raise UsageError("no such workload: %s" % workload)
    if not sampler_available():
        raise UsageError("this platform has no /proc/<pid>/statm sampler "
                         "— hls-rss cannot measure RSS here (refusing "
                         "instead of certifying nothing)")
    cc = cfg["cc"]
    wrap_obj = None
    if cfg["malloc_count"] is not False:
        wrap_obj = probe_wrap(cc)
        if wrap_obj is None and cfg["malloc_count"] is True:
            raise UsageError("--malloc-count forced but %s cannot link "
                             "-Wl,--wrap (GNU ld only)" % cc)

    warmup = cfg["warmup"]
    cycles = cfg["cycles"]
    with tempfile.TemporaryDirectory(prefix="hlrss-") as tmp:
        if wrap_obj:
            wrap_obj = build_interposer(cc, tmp)
        bin_path = compile_workload(workload, cfg, tmp, wrap_obj)
        # Run A: the full plan. The binary is the program's own; the
        # tool only drives the plan through argv[1].
        argv_a = [str(warmup + cycles)] + list(cfg["prog_args"])
        trace_a = run_workload(bin_path, argv_a, cfg)
        if trace_a.protocol_error:
            raise VerifyError(trace_a.protocol_error)
        if trace_a.timed_out:
            raise VerifyError("the workload timed out")
        if trace_a.rc != 0:
            tail = trace_a.stderr_tail.decode("utf-8", "replace").strip()
            raise VerifyError("the workload exited %d%s"
                              % (trace_a.rc,
                                 (": " + tail.splitlines()[-1])
                                 if tail else ""))
        requested = warmup + cycles
        observed = len(trace_a.markers)
        cooperative = observed == requested and trace_a.markers == \
            list(range(requested))

        plan = {"warmup": warmup, "cycles": cycles,
                "retained_skip_reason": None}
        trace_b = None
        retained = None
        if not cooperative:
            plan["retained_skip_reason"] = (
                "the workload printed %d markers, the plan asked for %d "
                "— it does not follow the argv[1] cycle convention, so "
                "the two-run comparison cannot isolate warmup from work"
                % (observed, requested))
        elif wrap_obj is None:
            plan["retained_skip_reason"] = (
                "%s cannot link -Wl,--wrap — the balance instrument is "
                "unavailable (the timeline instrument stands alone)"
                % cc)
        else:
            argv_b = [str(warmup)] + list(cfg["prog_args"])
            trace_b = run_workload(bin_path, argv_b, cfg)
            if trace_b.protocol_error or trace_b.rc != 0 or \
                    len(trace_b.markers) != warmup or \
                    trace_b.interposer is None:
                plan["retained_skip_reason"] = (
                    "run B (warmup-only) did not complete cleanly — "
                    "the comparison is off")
                trace_b = None
            else:
                retained = analyse_retained(trace_a, trace_b, cfg)

        slope = analyse_slope(trace_a, warmup, cfg,
                              os.path.splitext(os.path.basename(workload))[0])
        instruments = [i for i in (slope, retained) if i is not None]
        verdict = compose_verdict(instruments)
        report = render_report(workload, cfg, plan, trace_a, trace_b,
                               slope, retained, verdict)
        blob = build_json(workload, cfg, plan, trace_a, trace_b, slope,
                          retained, verdict)
    return report, blob, verdict


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hls-rss",
        description="Stage 125 RSS-stability verifier: run a Halis "
                    "workload natively, sample its resident memory, and "
                    "certify (or refuse to certify) that the GC-free "
                    "runtime keeps RSS flat as work volume grows.")
    ap.add_argument("workload", help="a .hls program (see the marker "
                    "and argv[1] cycle conventions in --help of the "
                    "SPEC)")
    ap.add_argument("prog_args", nargs="*", help="extra arguments passed "
                    "to the program after the tool's cycle count")
    ap.add_argument("--cycles", type=int, default=200, metavar="N",
                    help="measured steady-state cycles (default 200)")
    ap.add_argument("--warmup", type=int, default=None, metavar="W",
                    help="warmup cycles before the measured phase "
                         "(default cycles//4, at least 1)")
    ap.add_argument("--interval", type=float, default=10.0, metavar="MS",
                    help="RSS sampling interval in ms (default 10)")
    ap.add_argument("--timeout", type=float, default=120.0, metavar="S",
                    help="hard per-run timeout in seconds (default 120)")
    ap.add_argument("--threshold", type=float, default=0.25, metavar="P",
                    help="leak threshold in pages/cycle (default 0.25)")
    ap.add_argument("--threshold-time", type=float, default=2.0,
                    dest="threshold_time", metavar="P",
                    help="threshold in pages/s when no markers (default 2)")
    ap.add_argument("--rss-slack", type=int, default=256,
                    dest="rss_slack", metavar="P",
                    help="high-water slack across the two runs, pages "
                         "(default 256)")
    ap.add_argument("--live-slack", type=int, default=0,
                    dest="live_slack", metavar="N",
                    help="live-object slack across the two runs "
                         "(default 0 — the balance is exact)")
    ap.add_argument("--bootstrap", type=int, default=1000, metavar="B",
                    help="bootstrap resamples for the slope CI "
                         "(default 1000)")
    mt = ap.add_mutually_exclusive_group()
    mt.add_argument("--malloc-count", dest="malloc_count",
                    action="store_true", default=None,
                    help="force the interposer (exit 2 if unsupported)")
    mt.add_argument("--no-malloc-count", dest="malloc_count",
                    action="store_false",
                    help="disable the interposer (timeline only)")
    ap.add_argument("--json", metavar="FILE",
                    help="write the machine-readable report")
    ap.add_argument("--hlc", default=os.path.join(REPO_ROOT, "bin", "hlc"),
                    help="native compiler (default bin/hlc)")
    ap.add_argument("--cc", default=os.environ.get("CC", "gcc"),
                    help="C compiler (default $CC or gcc)")
    args = ap.parse_args(argv)

    cfg = {
        "cycles": args.cycles,
        "warmup": args.warmup if args.warmup is not None
        else max(1, args.cycles // 4),
        "interval": args.interval,
        "timeout": args.timeout,
        "threshold": args.threshold,
        "threshold_time": args.threshold_time,
        "rss_slack": args.rss_slack,
        "live_slack": args.live_slack,
        "bootstrap": args.bootstrap,
        "malloc_count": args.malloc_count,
        "prog_args": args.prog_args,
        "hlc": args.hlc,
        "cc": args.cc,
    }
    if not args.workload.endswith(".hls"):
        raise SystemExit(_exit(2, "hls-rss: the workload must be a .hls "
                                  "program"))
    if args.cycles < 3:
        raise SystemExit(_exit(2, "hls-rss: --cycles must be at least 3 "
                                  "(a slope needs points)"))
    if args.warmup is not None and args.warmup < 1:
        raise SystemExit(_exit(2, "hls-rss: --warmup must be at least 1"))
    if args.interval < 1.0:
        raise SystemExit(_exit(2, "hls-rss: --interval must be >= 1 ms"))
    if args.bootstrap < 100:
        raise SystemExit(_exit(2, "hls-rss: --bootstrap must be >= 100"))

    try:
        report, blob, verdict = verify(args.workload, cfg)
    except UsageError as e:
        return _exit(2, "hls-rss: %s" % e)
    except VerifyError as e:
        return _exit(1, "hls-rss: %s" % e)
    print(report)
    if args.json:
        try:
            with open(args.json, "w", encoding="utf-8", newline="\n") as f:
                f.write(blob + "\n")
        except OSError as e:
            return _exit(2, "hls-rss: cannot write %s: %s" % (args.json, e))
    return 0 if verdict == STABLE else 1


def _exit(code, message):
    print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
