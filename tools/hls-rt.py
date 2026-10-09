#!/usr/bin/env python3
"""hls-rt — the Stage 126 soft-real-time verifier (bounded allocation
per cycle).

Usage:
  python3 tools/hls-rt.py workload.hls
  python3 tools/hls-rt.py --budget-allocs 2000 --budget-bytes 65536 w.hls
  python3 tools/hls-rt.py --budget-ms 50 --warmup 2 w.hls
  python3 tools/hls-rt.py --no-enforce w.hls
  python3 tools/hls-rt.py --json r.json w.hls data.txt

Stage 8 made the runtime garbage-collector-free; Stage 125 verified that
a steady-state workload's resident memory does not grow with the work
performed. Stage 126 adds the discipline a soft-real-time workload
actually needs: a BUDGET per cycle. The mode (runtime side: src/hlc/
runtime.hls; model side: core/rt.hls; ledger side: this tool + the
interposer tools/rt_parts/hlrt_budget_wrap.c) bounds every cycle's
allocation traffic — allocation COUNT, allocation BYTES, and (optionally)
wall-clock time — and the first cycle that closes over its budget dies
through the Stage 81 panic path with the canonical message
core/rt.hls renders:

    soft real-time violation: cycle 7 made 1200 allocations (budget 1000)

The convention (the established one — naming plus data, zero checker or
codegen edits): the workload is an ordinary Halis program; a CYCLE
MARKER is a line it prints once per steady-state cycle,

    __HLRT_CYCLE__ <i>

with i strictly increasing from 0. The runtime recognises the marker as
it passes through println (that is what makes the in-process
enforcement possible at all); this tool parses the same stream from
outside, with the same reserved-prefix discipline the Stage 119/124/125
verifiers use: a record line must parse exactly, a line merely
CONTAINING the reserved stem is a protocol error, and any other output
the program prints is captured and ignored — a workload may talk.

The three axes (--budget-allocs N, --budget-bytes B, --budget-ms MS; a
left-out axis is unchecked, exactly like a 0 in core.rt's RtBudget):

  enforcement (in-process)  — the budgets reach the program as
    HLRT_BUDGET_* environment variables; the runtime rolls the
    instrument at every mark and kills the process at the first
    over-budget cycle (warmup cycles excluded). A violation is the
    program's own death: exit 101, the canonical message on stderr —
    not a verdict the tool invented from outside.

  the ledger (out-of-process) — the interposer records every closed
    cycle's (allocs, bytes, live, frees, nanos) and reports the whole
    ledger at exit. The tool verdicts from the ledger INDEPENDENTLY:
    with --no-enforce the budgets are kept out of the environment, the
    program runs to completion, and the refusal is still earned — the
    same numbers, judged after the fact instead of during.

A certification therefore has two independent legs, and either alone is
refusable: the in-process enforcement (the mode actually armed) and the
ledger verdict (the numbers actually bounded). The report says which
legs fired.

The plan: --warmup K excludes the first K cycles from the verdict (the
working set fills: allocator bins, stdio buffers, caches — the runtime
still enforces them unless the budget allows for the fill; a workload
whose warmup legitimately allocates more passes --warmup and the
runtime, reading the same HLRT_WARMUP, skips enforcement for exactly
those cycles). Certification needs at least --min-cycles measured
cycles (default 3; a bound claimed from one sample is a rumour) and
requires the markers to COVER the work: the tail window after the last
mark must not out-allocate the biggest measured cycle, or the ledger
describes a program the markers do not describe (an honest
INCONCLUSIVE, never a pass).

The statistics in the report are the planner's numbers: per-axis
max/median/p95 over the measured window and the cycle-time jitter
(peak-to-peak) — a workload whose median is well under the deadline but
whose jitter crosses it is NOT real-time, and the median alone would
hide that. core/rt.hls computes the same numbers the same way
(rt_window_*) so a plan made in Halis and a report made here agree.

Native only, on purpose: the budgeted runtime is the runtime the
compiler EMITS. The Stage-0 interpreter's allocations are Python's, so
the tool has no interpreter mode (Stage 125's rule, inherited). The
instrument needs GNU ld's --wrap (probed per invocation; unsupported ->
exit 2 rather than measure nothing honestly).

Exit codes:
  0  certified RT BOUNDED (both legs)
  1  not certified: REFUSED (a violation — in-process or on the
     ledger), INCONCLUSIVE (too few cycles, uncovered work, protocol
     error, ledger inconsistency), or a program failure (non-zero exit,
     timeout)
  2  usage / environment: missing file, bad option values, no bin/hlc,
     no gcc, no GNU --wrap
"""

import argparse
import json
import math
import os
import pty
import subprocess
import sys
import tempfile
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOOL_VERSION = "0.144.0-alpha"

# The reserved protocol prefixes (the Stage 119/124/125 discipline: a
# record line must parse exactly; a stray stem is a loud error).
_PROTO_CYCLE = b"__HLRT_CYCLE__ "
_PROTO_STEM = b"__HLRT_"

# The instrument's stderr report (tools/rt_parts/hlrt_budget_wrap.c):
# a fenced block of HLRT_*= records.
_REPORT_BEGIN = b"HLRT_REPORT_BEGIN"
_REPORT_END = b"HLRT_REPORT_END"
_CYCLE_KEYS = ("ALLOCS", "BYTES", "LIVE", "FREES", "NANOS")


_WRAP_C = os.path.join(REPO_ROOT, "tools", "rt_parts",
                       "hlrt_budget_wrap.c")
_WRAP_FLAGS = ("-Wl,--wrap=malloc", "-Wl,--wrap=calloc",
               "-Wl,--wrap=realloc", "-Wl,--wrap=free")

# Verdict words (the report's and the exit code's vocabulary).
BOUNDED = "bounded"
REFUSED = "refused"
INCONCLUSIVE = "inconclusive"

_VERDICT_WORDS = {
    BOUNDED: "RT BOUNDED",
    REFUSED: "RT REFUSED",
    INCONCLUSIVE: "RT INCONCLUSIVE",
}


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
    if not payload.isdigit():
        raise VerifyError(
            "protocol error: malformed cycle marker %r (expected "
            "`__HLRT_CYCLE__ <int>`)" % line[:120].decode("utf-8",
                                                          "replace"))
    return int(payload)


def scan_protocol_violation(line):
    """A line that merely CONTAINS the reserved stem but is not a
    record corrupts the stream: fail loudly, never silently drop."""
    if _PROTO_STEM in line and not line.startswith(_PROTO_CYCLE):
        raise VerifyError(
            "protocol error: reserved stem in a non-record line %r"
            % line[:120].decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# The ledger: parse and re-verify the interposer's fenced report.
# ---------------------------------------------------------------------------

def parse_report(stderr_bytes):
    """Extract the fenced HLRT_ report from the program's stderr.
    Returns the ledger dict or None when no complete, self-consistent
    report is present. The totals are RE-DERIVED from the per-cycle
    lines: the instrument's own summary is not trusted until it agrees
    with its ledger."""
    if not stderr_bytes:
        return None
    text = stderr_bytes.splitlines()
    try:
        begin = text.index(_REPORT_BEGIN)
        end = text.index(_REPORT_END)
    except ValueError:
        return None
    if end < begin:
        return None
    cycles = []
    tail = None
    totals = None
    live_exit = None
    overflow = None
    for raw in text[begin + 1:end]:
        if raw.startswith(b"HLRT_CYCLE="):
            # `HLRT_CYCLE=<i> ALLOCS=.. BYTES=.. LIVE=.. FREES=..
            # NANOS=..` — the index is the first token, the rest are
            # the measured axes.
            payload = raw[len(b"HLRT_CYCLE="):]
            idx_s, _, rest = payload.partition(b" ")
            idx = _int_or_none(idx_s)
            rec = _parse_kv(rest)
            if idx is None or rec is None \
                    or any(k not in rec for k in _CYCLE_KEYS):
                return None
            cycles.append({"cycle": idx,
                           "allocs": rec["ALLOCS"],
                           "bytes": rec["BYTES"],
                           "live_bytes": rec["LIVE"],
                           "frees": rec["FREES"],
                           "nanos": rec["NANOS"]})
        elif raw.startswith(b"HLRT_TAIL "):
            kv = _parse_kv(raw[len(b"HLRT_TAIL "):])
            if kv is None or "ALLOCS" not in kv or "BYTES" not in kv:
                return None
            tail = {"allocs": kv["ALLOCS"], "bytes": kv["BYTES"]}
        elif raw.startswith(b"HLRT_TOTAL "):
            kv = _parse_kv(raw[len(b"HLRT_TOTAL "):])
            if kv is None or any(k not in kv for k in
                                 ("ALLOCS", "BYTES", "FREES")):
                return None
            totals = {"allocs": kv["ALLOCS"], "bytes": kv["BYTES"],
                      "frees": kv["FREES"]}
        elif raw.startswith(b"HLRT_LIVE_EXIT="):
            live_exit = _int_or_none(raw.split(b"=", 1)[1])
        elif raw.startswith(b"HLRT_OVERFLOW="):
            overflow = _int_or_none(raw.split(b"=", 1)[1])
    if tail is None or totals is None \
            or live_exit is None or overflow is None:
        return None
    # Re-derive: the totals must be the ledger's sum. A report whose
    # summary disagrees with its own lines is a broken instrument, and
    # a broken instrument certifies nothing.
    if sum(c["allocs"] for c in cycles) + tail["allocs"] \
            != totals["allocs"]:
        return None
    if sum(c["bytes"] for c in cycles) + tail["bytes"] \
            != totals["bytes"]:
        return None
    return {
        "cycles": cycles,
        "tail": tail,
        "totals": totals,
        "live_exit": live_exit,
        "overflow": overflow,
    }


def _parse_kv(payload):
    """`K=V K=V ...` -> dict with the ORIGINAL (uppercase) keys and int
    values, or None when any field is malformed."""
    out = {}
    for field in payload.split(b" "):
        k, _, v = field.partition(b"=")
        iv = _int_or_none(v)
        if not k or iv is None:
            return None
        out[k.decode("ascii")] = iv
    return out


def _int_or_none(b):
    try:
        return int(b)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Statistics — the planner's numbers (max / median / p95 / jitter), the
# same shapes core/rt.hls computes (rt_window_*), integer-exact.
# ---------------------------------------------------------------------------

def median(xs):
    """The plain integer median (odd: the middle; even: the floor of the
    two middles' average) — the same definition rt_window_median_nanos
    uses, so a plan in Halis and a report here cannot disagree."""
    if not xs:
        raise VerifyError("median of empty data")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) // 2


def p95(xs):
    """Linear-interpolation 95th percentile (the hls-bench estimator)."""
    if not xs:
        raise VerifyError("p95 of empty data")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = 0.95 * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def fmt_bytes(n):
    """Human bytes: KiB/MiB above the k-scale, plain below it."""
    if n >= 1024 * 1024:
        return "%.2f MiB" % (n / (1024.0 * 1024.0))
    if n >= 1024:
        return "%.1f KiB" % (n / 1024.0)
    return "%d B" % n


def fmt_nanos(ns):
    """Human nanoseconds: ms above the micro scale, us above the ns
    scale, plain below."""
    if ns >= 1000000:
        return "%.2f ms" % (ns / 1e6)
    if ns >= 1000:
        return "%.1f us" % (ns / 1e3)
    return "%d ns" % ns


# ---------------------------------------------------------------------------
# Native build — the exact recipe every other gate uses, plus the wrap.
# ---------------------------------------------------------------------------

def probe_wrap(cc):
    """Compile the instrument and probe whether the linker understands
    -Wl,--wrap (GNU ld / BFD do; ld64 does not). Returns the .o path
    inside a caller-owned temp dir, or None."""
    try:
        with tempfile.TemporaryDirectory(prefix="hlrt-probe-") as tmp:
            obj = os.path.join(tmp, "budget.o")
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


def build_instrument(cc, tmpdir):
    """Compile the instrument .o into `tmpdir` (probe_wrap already
    proved the toolchain can). Returns the .o path."""
    obj = os.path.join(tmpdir, "hlrt_budget.o")
    r = subprocess.run([cc, "-O2", "-c", _WRAP_C, "-o", obj],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=120)
    if r.returncode != 0:
        raise UsageError("cannot compile the budget instrument:\n%s"
                         % r.stderr.decode("utf-8", "replace"))
    return obj


def compile_workload(workload, cfg, tmpdir, wrap_obj):
    """bin/hlc -> C -> gcc -O2 binary (+ the instrument). hlc runs with
    cwd = the repo root so `import "std.**"` resolves exactly where it
    resolves for every other gate. Raises UsageError on a compile
    failure (exit 2: a workload that does not build is an environment
    problem, not a verdict)."""
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
# The run — pty stdout, the marker stream, the stderr tail.
# ---------------------------------------------------------------------------

class RunResult(object):
    """Everything one execution of the workload yielded."""

    def __init__(self):
        self.rc = None
        self.timed_out = False
        self.markers = []         # observed cycle indices, in order
        self.protocol_error = None
        self.report = None        # parsed ledger dict or None
        self.stderr_tail = b""    # last 8 KiB (panic messages)
        self.elapsed_s = 0.0


def run_workload(bin_path, argv, env, cfg):
    """Run the binary once. stdout is a pty (glibc line-buffers a tty,
    so each println reaches the parent the moment it is written — the
    same alignment every marker-reading instrument here depends on;
    ONLCR maps \\n to \\r\\n, which the reader strips). stderr is a
    pipe (the ledger reports at exit; it needs no alignment). Returns
    a RunResult; raises VerifyError on a timeout."""
    res = RunResult()
    mfd, sfd = pty.openpty()
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        try:
            proc = subprocess.Popen(
                [bin_path] + list(argv),
                stdout=sfd, stderr=subprocess.PIPE, stdin=devnull,
                env=env, close_fds=True)
        except OSError as e:
            raise UsageError("cannot launch the workload: %s" % e)
        finally:
            os.close(sfd)
            os.close(devnull)
    except Exception:
        os.close(mfd)
        raise

    t0 = time.monotonic()

    def reader():
        """Line reader: markers advance the observed sequence; any
        other line is protocol-scanned then ignored."""
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
                        res.protocol_error = str(e)
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
                        res.protocol_error = (
                            "protocol error: cycle marker %d does not "
                            "advance (previous %d)" % (idx, last_cycle))
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        return
                    last_cycle = idx
                    res.markers.append(idx)
        except OSError:
            pass  # EIO on Linux once the slave side closed: EOF
        finally:
            try:
                os.close(mfd)
            except OSError:
                pass

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    try:
        out_err = None
        try:
            _, out_err = proc.communicate(timeout=cfg["timeout"])
        except subprocess.TimeoutExpired:
            res.timed_out = True
            proc.kill()
            proc.communicate()
            raise VerifyError("workload timed out after %.0fs"
                              % cfg["timeout"])
    finally:
        rt.join(timeout=5)
        res.rc = proc.returncode
        res.elapsed_s = time.monotonic() - t0
        if out_err is not None:
            res.stderr_tail = out_err[-8192:]
    res.report = parse_report(res.stderr_tail)
    return res


# ---------------------------------------------------------------------------
# Analysis — the verdict, from the two legs.
# ---------------------------------------------------------------------------

def cycle_verdict(cycle, cfg):
    """One closed cycle against the plan, in the canonical axis order
    (allocs, bytes, nanos) — the order the runtime enforces in and
    core.rt classifies in. Returns (verdict_word, axis_or_None)."""
    if cfg["budget_allocs"] > 0 and cycle["allocs"] > cfg["budget_allocs"]:
        return (REFUSED, "allocs")
    if cfg["budget_bytes"] > 0 and cycle["bytes"] > cfg["budget_bytes"]:
        return (REFUSED, "bytes")
    if cfg["budget_nanos"] > 0 and cycle["nanos"] > cfg["budget_nanos"]:
        return (REFUSED, "nanos")
    return (BOUNDED, None)


def violation_message_from(cycle, axis, cfg):
    """The canonical violation line the RUNTIME would have died with for
    this cycle — the tool's ledger-side refusal quotes the same shape,
    so a message never names a number the ledger disagrees with."""
    if axis == "allocs":
        return ("soft real-time violation: cycle %d made %d allocations "
                "(budget %d)" % (cycle["cycle"], cycle["allocs"],
                                 cfg["budget_allocs"]))
    if axis == "bytes":
        return ("soft real-time violation: cycle %d allocated %d bytes "
                "(budget %d)" % (cycle["cycle"], cycle["bytes"],
                                 cfg["budget_bytes"]))
    if axis == "nanos":
        return ("soft real-time violation: cycle %d took %d ns "
                "(deadline %d ns)" % (cycle["cycle"], cycle["nanos"],
                                      cfg["budget_nanos"]))
    return ""


def analyse(run, cfg):
    """The composed verdict. Returns (verdict_word, reason, detail)."""
    enforcement_armed = cfg["enforce"]
    measured = [c for c in _ledger_cycles(run) if c["cycle"] >= cfg["warmup"]]

    # Leg 1 — the program's own death. The mode was armed and the
    # runtime killed it: that is a refusal with the runtime as witness,
    # whatever the ledger says.
    if run.rc == 101:
        panic_line = _panic_violation_line(run.stderr_tail)
        if panic_line:
            return (REFUSED, panic_line,
                    "in-process enforcement fired (exit 101)")

    # Leg 2 — the ledger. Every measured cycle inside its budget.
    if measured:
        for c in measured:
            word, axis = cycle_verdict(c, cfg)
            if word == REFUSED:
                msg = violation_message_from(c, axis, cfg)
                if enforcement_armed:
                    msg += " (the in-process mode should have fired " \
                           "first — a runtime/tool disagreement is " \
                           "itself a failure)"
                return (REFUSED, msg, "the ledger shows the overrun")

    # Program failures that are not violations.
    if run.timed_out:
        return (REFUSED, "workload timed out after %.0fs"
                % cfg["timeout"], "no verdict from a dead run")
    if run.protocol_error:
        return (REFUSED, run.protocol_error, "the stream is corrupt")
    if run.rc != 0:
        return (REFUSED, "workload exited %d" % run.rc,
                "not a soft-real-time violation — see the stderr tail")

    # Instrument health.
    if run.report is None:
        return (INCONCLUSIVE,
                "the instrument produced no ledger (HLRT_REPORT fence "
                "absent or inconsistent)", "cannot certify blind")
    if run.report["overflow"] != 0:
        return (INCONCLUSIVE,
                "the instrument's tracking table overflowed — the "
                "ledger is incomplete", "cannot certify a partial "
                "ledger")

    # Coverage: the markers must describe the work.
    tail = run.report["tail"]
    if measured:
        biggest = max(c["allocs"] for c in measured)
        if tail["allocs"] > biggest:
            return (INCONCLUSIVE,
                    "the tail window after the last mark made %d "
                    "allocations — more than the biggest measured "
                    "cycle (%d): the markers do not cover the work"
                    % (tail["allocs"], biggest),
                    "an honest gap, not a pass")

    # Sample size.
    if len(measured) < cfg["min_cycles"]:
        return (INCONCLUSIVE,
                "only %d measured cycle(s) after the warmup of %d — a "
                "bound claimed from fewer than %d samples is a rumour"
                % (len(measured), cfg["warmup"], cfg["min_cycles"]),
                "run more cycles")

    return (BOUNDED,
            "every measured cycle is inside its budget",
            "in-process enforcement: %s; ledger: clean"
            % ("armed" if enforcement_armed else "off (--no-enforce)"))


def _ledger_cycles(run):
    """The closed cycles from the ledger, tolerating its absence (the
    absence is analyse's problem, not the caller's)."""
    if run.report is None:
        return []
    return run.report["cycles"]


def _panic_violation_line(stderr_bytes):
    """The canonical `panic: soft real-time violation: ...` line from
    the program's stderr, if it died of one."""
    for raw in stderr_bytes.splitlines():
        line = raw.decode("utf-8", "replace")
        if line.startswith("panic: soft real-time violation:"):
            return line[len("panic: "):].strip()
    return None


# ---------------------------------------------------------------------------
# The report.
# ---------------------------------------------------------------------------

def render(run, cfg, verdict, reason, detail):
    out = []
    out.append("hls-rt %s — soft-real-time verification (Stage 126)"
               % TOOL_VERSION)
    out.append("workload:        %s" % cfg["workload"])
    out.append("plan:            budget %s allocs/cycle, %s/cycle, "
               "deadline %s/cycle; warmup %d"
               % (_axis(cfg["budget_allocs"], "off"),
                  fmt_bytes(cfg["budget_bytes"])
                  if cfg["budget_bytes"] else "off",
                  fmt_nanos(cfg["budget_nanos"])
                  if cfg["budget_nanos"] else "off",
                  cfg["warmup"]))
    out.append("mode:            %s"
               % ("enforce" if cfg["enforce"] else "measure only "
                  "(--no-enforce)"))
    out.append("exit:            %s in %.2fs"
               % (run.rc if run.rc is not None else "signal",
                  run.elapsed_s))
    out.append("")

    measured = [c for c in _ledger_cycles(run)
                if c["cycle"] >= cfg["warmup"]]
    if measured:
        out.append("per-cycle ledger (measured window; allocs / bytes / "
                       "live / time):")
        for c in measured:
            out.append("    cycle %-4d %8d allocs %12s %12s live %10s"
                       % (c["cycle"], c["allocs"], fmt_bytes(c["bytes"]),
                          fmt_bytes(c["live_bytes"]),
                          fmt_nanos(c["nanos"])))
        out.append("")
        allocs = [c["allocs"] for c in measured]
        bytes_ = [c["bytes"] for c in measured]
        nanos = [c["nanos"] for c in measured]
        out.append("measured window: %d cycles (warmup %d excluded)"
                   % (len(measured), cfg["warmup"]))
        out.append("    allocs/cycle:  max %d   median %d"
                   % (max(allocs), median(allocs)))
        out.append("    bytes/cycle:   max %s   median %s"
                   % (fmt_bytes(max(bytes_)), fmt_bytes(median(bytes_))))
        out.append("    cycle time:    max %s   median %s   p95 %s   "
                   "jitter %s"
                   % (fmt_nanos(max(nanos)), fmt_nanos(median(nanos)),
                      fmt_nanos(int(p95(nanos))),
                      fmt_nanos(max(nanos) - min(nanos))))
        if run.report is not None:
            tail = run.report["tail"]
            out.append("    tail after the last mark: %d allocs, %s"
                       % (tail["allocs"], fmt_bytes(tail["bytes"])))
        out.append("")
    elif run.report is not None:
        out.append("the ledger holds no measured cycles (warmup %d, "
                   "markers seen %d)" % (cfg["warmup"],
                                         len(run.markers)))
        out.append("")

    out.append("%s — %s" % (_VERDICT_WORDS[verdict], reason))
    out.append("(%s)" % detail)
    return "\n".join(out)


def _axis(v, off_word):
    return str(v) if v > 0 else off_word


def to_json(run, cfg, verdict, reason, detail):
    blob = {
        "tool": "hls-rt",
        "version": TOOL_VERSION,
        "workload": cfg["workload"],
        "config": {
            "budget_allocs": cfg["budget_allocs"],
            "budget_bytes": cfg["budget_bytes"],
            "budget_nanos": cfg["budget_nanos"],
            "warmup": cfg["warmup"],
            "min_cycles": cfg["min_cycles"],
            "enforce": cfg["enforce"],
            "timeout": cfg["timeout"],
        },
        "exit": run.rc,
        "elapsed_s": round(run.elapsed_s, 3),
        "observed_markers": run.markers,
        "protocol_error": run.protocol_error,
        "ledger": (None if run.report is None else {
            "cycles": run.report["cycles"],
            "tail": run.report["tail"],
            "totals": run.report["totals"],
            "live_exit": run.report["live_exit"],
            "overflow": run.report["overflow"],
        }),
        "stats": _stats_blob(run, cfg),
        "verdict": verdict,
        "reason": reason,
        "detail": detail,
    }
    return blob


def _stats_blob(run, cfg):
    measured = [c for c in _ledger_cycles(run)
                if c["cycle"] >= cfg["warmup"]]
    if not measured:
        return None
    allocs = [c["allocs"] for c in measured]
    bytes_ = [c["bytes"] for c in measured]
    nanos = [c["nanos"] for c in measured]
    return {
        "cycles": len(measured),
        "allocs_max": max(allocs),
        "allocs_median": median(allocs),
        "bytes_max": max(bytes_),
        "bytes_median": median(bytes_),
        "nanos_max": max(nanos),
        "nanos_median": median(nanos),
        "nanos_p95": int(p95(nanos)),
        "nanos_jitter": max(nanos) - min(nanos),
    }


# ---------------------------------------------------------------------------
# The CLI.
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hls-rt",
        description="Stage 126 soft-real-time verifier: certify that a "
                    "workload's per-cycle allocation traffic stays "
                    "inside a budget.",
        epilog="extra arguments after the workload reach the program "
               "as its argv.")
    ap.add_argument("workload", help="the .hls workload")
    ap.add_argument("--budget-allocs", type=int, default=0, metavar="N",
                    help="max heap allocations per cycle "
                         "(0/absent = unchecked)")
    ap.add_argument("--budget-bytes", type=int, default=0, metavar="B",
                    help="max allocated bytes per cycle "
                         "(0/absent = unchecked)")
    ap.add_argument("--budget-ms", type=int, default=0, metavar="MS",
                    help="max wall-clock milliseconds per cycle "
                         "(0/absent = unchecked)")
    ap.add_argument("--warmup", type=int, default=0, metavar="K",
                    help="the first K cycles are measured but not "
                         "enforced (default 0)")
    ap.add_argument("--min-cycles", type=int, default=3, metavar="N",
                    help="measured cycles required for a certification "
                         "(default 3)")
    ap.add_argument("--no-enforce", action="store_true",
                    help="keep the budgets out of the environment: "
                         "measure and judge from the ledger only")
    ap.add_argument("--timeout", type=float, default=120.0, metavar="S",
                    help="workload timeout in seconds (default 120)")
    ap.add_argument("--json", metavar="PATH",
                    help="also write a machine-readable report")
    args, passthrough = ap.parse_known_args(argv)

    workload = args.workload
    if not os.path.isfile(workload):
        print("hls-rt: no such workload: %s" % workload, file=sys.stderr)
        return 2
    if not workload.endswith(".hls"):
        print("hls-rt: the workload must be a .hls program: %s"
              % workload, file=sys.stderr)
        return 2
    if args.budget_allocs < 0 or args.budget_bytes < 0 \
            or args.budget_ms < 0:
        print("hls-rt: budgets must be non-negative", file=sys.stderr)
        return 2
    if args.budget_allocs == 0 and args.budget_bytes == 0 \
            and args.budget_ms == 0:
        print("hls-rt: nothing to bound — set at least one of "
              "--budget-allocs/--budget-bytes/--budget-ms (the model's "
              "rule: a budget with every axis 0 is not a mode)",
              file=sys.stderr)
        return 2
    if args.warmup < 0:
        print("hls-rt: --warmup must be >= 0", file=sys.stderr)
        return 2
    if args.min_cycles < 1:
        print("hls-rt: --min-cycles must be >= 1", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("hls-rt: --timeout must be > 0", file=sys.stderr)
        return 2

    cc = os.environ.get("CC", "gcc")
    if not shutil_which(cc):
        print("hls-rt: no C compiler (%s) — the mode is a property of "
              "the native runtime" % cc, file=sys.stderr)
        return 2

    cfg = {
        "workload": workload,
        "budget_allocs": args.budget_allocs,
        "budget_bytes": args.budget_bytes,
        "budget_nanos": args.budget_ms * 1000000,
        "warmup": args.warmup,
        "min_cycles": args.min_cycles,
        "enforce": not args.no_enforce,
        "timeout": args.timeout,
        "hlc": os.path.join(REPO_ROOT, "bin", "hlc"),
        "cc": cc,
    }

    with tempfile.TemporaryDirectory(prefix="hlrt-") as tmp:
        if probe_wrap(cc) is None:
            print("hls-rt: the linker does not support -Wl,--wrap "
                  "(GNU ld does; this tool has no other sensor)",
                  file=sys.stderr)
            return 2
        try:
            wrap_obj = build_instrument(cc, tmp)
            bin_path = compile_workload(workload, cfg, tmp, wrap_obj)
        except UsageError as e:
            print("hls-rt: %s" % e, file=sys.stderr)
            return 2

        # The environment IS the mode's interface: the budgets reach the
        # runtime as HLRT_BUDGET_*, the ledger switch as HLRT_REPORT,
        # and the warmup as HLRT_WARMUP (the runtime and the tool must
        # agree on which cycles are warmup, or enforcement and verdict
        # judge different windows). --no-enforce keeps the budgets out
        # and the judgment in this process.
        env = dict(os.environ)
        env["HLRT_REPORT"] = "1"
        if cfg["enforce"]:
            env["HLRT_BUDGET_ALLOCS"] = str(cfg["budget_allocs"])
            env["HLRT_BUDGET_BYTES"] = str(cfg["budget_bytes"])
            env["HLRT_BUDGET_NANOS"] = str(cfg["budget_nanos"])
        else:
            env.pop("HLRT_BUDGET_ALLOCS", None)
            env.pop("HLRT_BUDGET_BYTES", None)
            env.pop("HLRT_BUDGET_NANOS", None)
        env["HLRT_WARMUP"] = str(cfg["warmup"])

        try:
            run = run_workload(bin_path, passthrough, env, cfg)
        except VerifyError as e:
            print("hls-rt: %s" % e, file=sys.stderr)
            return 1

    verdict, reason, detail = analyse(run, cfg)
    print(render(run, cfg, verdict, reason, detail))

    if args.json:
        try:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(to_json(run, cfg, verdict, reason, detail),
                          f, indent=2, sort_keys=False)
                f.write("\n")
        except OSError as e:
            print("hls-rt: cannot write the json report: %s" % e,
                  file=sys.stderr)
            return 2

    return 0 if verdict == BOUNDED else 1


def shutil_which(cmd):
    """A tiny local PATH lookup (importing shutil for one call is fine,
    but the probe paths above already run subprocesses directly)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, cmd)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UsageError as e:
        print("hls-rt: %s" % e, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
