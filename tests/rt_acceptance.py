#!/usr/bin/env python3
"""Stage 126 acceptance gate — hls-rt, the soft-real-time verifier.

Run with `make rt-acceptance` (or `python3 tests/rt_acceptance.py`).

Eight sections over the REAL verifier CLI (subprocess, the same
interface a user drives), the REAL instrument (its own C harness,
linked with the same -Wl,--wrap recipe the verifier uses), and the
verifier's own statistics module (imported for the numerics the report
prints):

  1. the convention  — a workload is an ordinary program; the plan is
     & the refusals    flags, the axes are optional but at least one
                       must be set (a budget with every axis 0 is not a
                       mode); missing files, non-.hls arguments and bad
                       option values are usage failures (exit 2)

  2. the instrument  — the wrap driven directly from a C harness: the
     & its ledger     per-cycle snapshot/roll semantics, the size
                       tracking through realloc (in-place delta, moved
                       blocks), the live-bytes sum, the tail window, the
                       fenced report format, and the verifier's
                       re-derivation rule (a report whose totals
                       disagree with its lines is refused whole)

  3. the runtime     — the emitted C carries the weak instrument hooks,
     wiring           the arm call in main and the marker scan — and a
                       --no-libc emission carries none of them; a
                       program run WITHOUT the instrument is unaffected
                       by its own markers (the mode costs one GOT test
                       when it is not armed)

  4. the steady      — the steady workload certifies RT BOUNDED; the
     certification    measured ledger is a constant line (identical
                       churn per cycle: max allocs == median allocs,
                       live bytes flat); --json parses and carries the
                       config, the ledger and the stats; extra argv
                       reaches the program and the marker count follows

  5. the enforcement — the three canaries die IN-PROCESS, each on its
     matrix           own axis (allocs, bytes, nanos) with the
                       canonical message; --warmup 1 rescues exactly the
                       fat-first-cycle workload; --no-enforce runs the
                       same canary to completion and still refuses from
                       the ledger alone — measuring and judging are
                       separable

  6. the honesty     — a marker-free program is RT INCONCLUSIVE (a
     gates            bound over zero observed cycles is a rumour);
                       work done after the last mark is INCONCLUSIVE
                       (the markers do not cover the work); malformed,
                       non-advancing and look-alike markers are loud
                       protocol errors, never silent skips

  7. the report      — the layout (header, plan, the per-cycle ledger,
     & the numbers    the window stats, the verdict pair) and the
                       statistics against hand-computed values (the
                       integer median, the interpolated p95, the jitter,
                       the human formats); --json agrees with the text

  8. the differential— the model fixture's output is byte-identical on
     & the corpus     the Stage-0 interpreter and the native backend;
     hygiene          the enforce-over fixture dies 101 on both with the
                       same canonical message; every new .hls file is
                       hlfmt-canonical, hllint-clean and boots through
                       the real checker; no gate litter survives

Exit code 0 = all acceptance criteria met.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

RT = [sys.executable, "tools/hls-rt.py"]
BOOT = [sys.executable, "boot/boot.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

FIXTURE = os.path.join(ROOT, "tests", "ok", "feat_stage126_rt.hls")
PANIC_FIXTURE = os.path.join(ROOT, "tests", "ok", "panic_stage126_rt.hls")
STEADY = os.path.join(ROOT, "tests", "rt_libs", "steady_rt.hls")
OVER_ALLOCS = os.path.join(ROOT, "tests", "rt_libs", "over_allocs_rt.hls")
OVER_BYTES = os.path.join(ROOT, "tests", "rt_libs", "over_bytes_rt.hls")
DEADLINE = os.path.join(ROOT, "tests", "rt_libs", "deadline_rt.hls")
WARMUP = os.path.join(ROOT, "tests", "rt_libs", "warmup_rt.hls")
NO_CYCLES = os.path.join(ROOT, "tests", "rt_libs", "no_cycles_rt.hls")
LATE_WORK = os.path.join(ROOT, "tests", "rt_libs", "late_work_rt.hls")
WRAP = os.path.join(ROOT, "tools", "rt_parts", "hlrt_budget_wrap.c")

TMP = tempfile.mkdtemp(prefix="_gate_s126_", dir=os.path.join(ROOT, "tests"))

PASS = 0
FAIL = 0


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


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=kw.pop("timeout", 600), **kw)


def write_probe(name, src):
    path = os.path.join(TMP, name)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    return path


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
print("Section 1 — the convention and the refusal matrix")

p = run(RT + ["no-such-file.hls", "--budget-allocs", "10"])
check(p.returncode == 2 and "no such workload" in p.stderr,
      "a missing workload is a usage failure (exit 2)")

p = run(RT + ["--budget-allocs", "10", "tools/hls-rt.py"])
check(p.returncode == 2 and "must be a .hls program" in p.stderr,
      "a non-.hls workload is a usage failure (exit 2)")

for why, flags in (
    ("a budget with every axis 0 is not a mode", ["--budget-allocs", "0"]),
    ("a negative allocs budget is refused", ["--budget-allocs", "-1"]),
    ("a negative bytes budget is refused", ["--budget-bytes", "-8"]),
    ("a negative deadline is refused", ["--budget-ms", "-3"]),
    ("a negative warmup is refused", ["--budget-allocs", "10",
                                      "--warmup", "-1"]),
    ("--min-cycles below 1 is refused", ["--budget-allocs", "10",
                                         "--min-cycles", "0"]),
    ("a non-positive timeout is refused", ["--budget-allocs", "10",
                                           "--timeout", "0"]),
):
    p = run(RT + flags + [STEADY])
    check(p.returncode == 2, "%s (exit 2)" % why)

p = run(RT + ["--budget-allocs", "10", "--budget-bytes", "0",
              "--budget-ms", "0", STEADY])
check(p.returncode in (0, 1),
      "a single non-zero axis is a mode (the run happened)")

# ===========================================================================
print("Section 2 — the instrument and its ledger")

mod = load_mod("hls_rt", os.path.join(ROOT, "tools", "hls-rt.py"))

# The wrap driven directly from a C harness — the same --wrap recipe the
# verifier links, the counters exercised by hand.
harness_c = write_probe("rt_harness.c", r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void hlrt_cycle_mark(long long idx);
long long hlrt_cycle_allocs(void);
long long hlrt_cycle_bytes(void);
long long hlrt_cycle_live_bytes(void);

int main(void) {
    /* the getters read the SNAPSHOT (the last CLOSED cycle); before any
     * mark nothing has closed */
    if (hlrt_cycle_allocs() != 0) return 1;
    /* cycle 0: two allocations, 24 bytes asked */
    void* a = malloc(8);
    void* b = malloc(16);
    memset(a, 1, 8);
    memset(b, 2, 16);
    /* the mark CLOSES cycle 0: the getters now read the SNAPSHOT (the
     * just-closed cycle), never the already-reset open one */
    hlrt_cycle_mark(0);
    if (hlrt_cycle_allocs() != 2 || hlrt_cycle_bytes() != 24) return 2;
    /* cycle 1: a realloc grows in place (its DELTA is counted, not the
     * size, and it IS an allocation event), one free, one fresh. The
     * getters stay on cycle 0's snapshot until the next mark — the
     * ledger lines below carry the cycle-1 numbers. */
    a = realloc(a, 32);                          /* +24 bytes */
    free(b);
    void* c = malloc(40);
    ((char*)c)[0] = 1;   /* the store keeps -O2 from dead-malloc-eliminating it */
    hlrt_cycle_mark(1);
    if (hlrt_cycle_allocs() != 2 || hlrt_cycle_bytes() != 64) return 3;
    /* the live set at exit: a (32) + c (40) = 72 bytes over 2 objects */
    if (hlrt_cycle_live_bytes() != 72) return 4;
    return 0;
}
''')
harness_bin = os.path.join(TMP, "rt_harness.bin")
r = run([CC, "-O2", "-o", harness_bin, harness_c, WRAP, "-pthread"]
        + list(mod._WRAP_FLAGS))
if r.returncode == 0:
    p = subprocess.run([harness_bin], capture_output=True, text=True,
                       env=dict(os.environ, HLRT_REPORT="1"),
                       stdin=subprocess.DEVNULL, timeout=120)
    check(p.returncode == 0,
          "the harness's own assertions hold (snapshot, delta, live)")
    check("HLRT_REPORT_BEGIN" in p.stderr
          and "HLRT_REPORT_END" in p.stderr,
          "the report is fenced (BEGIN/END)")
    lines = p.stderr.splitlines()
    cyc = [l for l in lines if l.startswith("HLRT_CYCLE=")]
    check(len(cyc) == 2, "two closed cycles in the ledger")
    c0 = cyc[0].split()
    check(c0[0] == "HLRT_CYCLE=0" and c0[1] == "ALLOCS=2"
          and c0[2] == "BYTES=24",
          "cycle 0: 2 allocations, 24 bytes (the mallocs, not the "
          "instrument's own)")
    c1 = cyc[1].split()
    check(c1[0] == "HLRT_CYCLE=1" and c1[1] == "ALLOCS=2"
          and c1[2] == "BYTES=64",
          "cycle 1: the in-place realloc's DELTA (24) + the fresh 40, "
          "the free counted once")
    live0 = int(dict(kv.split("=") for kv in c0)["LIVE"])
    live1 = int(dict(kv.split("=") for kv in c1)["LIVE"])
    check(live0 == 24 and live1 == 72,
          "live bytes: 24 after cycle 0, 72 at the mark (a-32 + c-40)")
    tail = [l for l in lines if l.startswith("HLRT_TAIL ")][0].split()
    check(tail[1] == "ALLOCS=0" and tail[2] == "BYTES=0",
          "the tail window is empty (nothing allocated after the mark)")
    total = [l for l in lines if l.startswith("HLRT_TOTAL ")][0]
    check("ALLOCS=4" in total and "BYTES=88" in total and "FREES=1" in total,
          "the totals re-derive: 4 allocations, 88 bytes, 1 free")
    check(any(l.startswith("HLRT_LIVE_EXIT=2") for l in lines),
          "two live objects at exit")
    check(any(l == "HLRT_OVERFLOW=0" for l in lines),
          "no tracking-table overflow")
else:
    check(False, "the instrument harness compiles")
    cyc = []
    c0 = c1 = [""] * 3

# The verifier's parser: the same report text, and the refusals.
rep = mod.parse_report(p.stderr.encode())
check(rep is not None and len(rep["cycles"]) == 2,
      "parse_report accepts the real ledger")
check(rep["totals"]["allocs"] == 4 and rep["tail"]["allocs"] == 0,
      "parse_report re-derives the totals from the lines")
broken = (b"HLRT_REPORT_BEGIN\n"
          b"HLRT_CYCLE=0 ALLOCS=2 BYTES=24 LIVE=24 FREES=0 NANOS=5\n"
          b"HLRT_TAIL ALLOCS=0 BYTES=0\n"
          b"HLRT_TOTAL ALLOCS=7 BYTES=24 FREES=0\n"     # disagrees
          b"HLRT_LIVE_EXIT=2\nHLRT_OVERFLOW=0\n"
          b"HLRT_REPORT_END\n")
check(mod.parse_report(broken) is None,
      "a report whose totals disagree with its lines is refused whole")
no_end = p.stderr.encode().split(b"HLRT_REPORT_END")[0]
check(mod.parse_report(no_end) is None,
      "an unfenced report (no END) is refused")
check(mod.parse_report(b"") is None,
      "no report at all is not a ledger")

# ===========================================================================
print("Section 3 — the runtime wiring")

hello = write_probe("hello.hls",
                    'fn main() -> int uses IO {\n'
                    '    println("hello")\n'
                    '    return 0\n'
                    '}\n')
c_path = os.path.join(TMP, "hello.c")
if NATIVE:
    r = run([HLC, hello, c_path])
    check(r.returncode == 0, "the hosted probe compiles")
    src = open(c_path).read()
    check("extern void hlrt_cycle_mark" in src
          and "__attribute__((weak))" in src,
          "the emitted C declares the weak instrument hooks")
    check("hl_srt_init();" in src,
          "main arms the mode before any user code")
    check('__HLRT_CYCLE__ ' in src and "hl_srt_on_mark" in src,
          "println scans the reserved marker prefix when armed")
    check("HLRT_BUDGET_ALLOCS" in src and "HLRT_BUDGET_NANOS" in src
          and "HLRT_WARMUP" in src,
          "the budget/warmup environment is read by name")

    c_nl = os.path.join(TMP, "hello_nl.c")
    r = run([HLC, "--no-libc", hello, c_nl])
    check(r.returncode == 0, "the --no-libc probe compiles")
    src_nl = open(c_nl).read()
    check("hl_srt_init" not in src_nl and "hlrt_cycle_mark" not in src_nl,
          "a --no-libc emission carries none of the mode")

    fs_src = ('#![freestanding]\n'
              'fn main() -> int {\n'
              '    return 42\n'
              '}\n')
    fs_path = write_probe("fs_probe.hls", fs_src)
    c_fs = os.path.join(TMP, "hello_fs.c")
    r = run([HLC, fs_path, c_fs])
    check(r.returncode == 0, "the freestanding probe compiles")
    check("hl_srt_init" not in open(c_fs).read(),
          "a freestanding emission carries none of the mode")

    bin_path = os.path.join(TMP, "hello.bin")
    r = run([CC, "-O2", "-o", bin_path, c_path, "-lm", "-pthread"])
    check(r.returncode == 0, "the hosted probe links WITHOUT the wrap")
    p = subprocess.run([bin_path], capture_output=True, text=True,
                       env=dict(os.environ, HLRT_REPORT="1"),
                       stdin=subprocess.DEVNULL, timeout=120)
    check(p.stdout == "hello\n" and p.returncode == 0,
          "an uninstrumented binary ignores its own markers and the "
          "report switch (mode not armed, one GOT test)")
    check("HLRT_REPORT_BEGIN" not in p.stderr,
          "no ledger without the instrument (the runtime never rolls)")
else:
    check(False, "bin/hlc missing — the wiring section cannot run")

# ===========================================================================
print("Section 4 — the steady certification")

if NATIVE:
    json_path = os.path.join(TMP, "steady.json")
    p = run(RT + ["--budget-allocs", "20000", "--budget-bytes", "1048576",
                  "--warmup", "2", "--json", json_path, STEADY])
    check(p.returncode == 0 and "RT BOUNDED" in p.stdout,
          "the steady workload certifies RT BOUNDED")
    check("every measured cycle is inside its budget" in p.stdout,
          "the reason line names the bounded leg")
    check("in-process enforcement: armed" in p.stdout,
          "the report says the in-process leg was armed")
    blob = json.load(open(json_path))
    check(blob["tool"] == "hls-rt" and blob["verdict"] == "bounded",
          "the json carries the tool name and the verdict")
    check(blob["config"]["budget_allocs"] == 20000
          and blob["config"]["warmup"] == 2
          and blob["config"]["enforce"] is True,
          "the config echoes the plan")
    ledger = blob["ledger"]["cycles"]
    check(len(ledger) == 24
          and [c["cycle"] for c in ledger] == list(range(24)),
          "the ledger holds all 24 cycles, in mark order")
    check(blob["observed_markers"] == list(range(24)),
          "the marker stream was observed complete")
    stats = blob["stats"]
    check(stats["cycles"] == 22,
          "22 measured cycles after the warmup of 2")
    check(stats["allocs_max"] == stats["allocs_median"],
          "identical churn per cycle: max allocs == median allocs")
    lives = [c["live_bytes"] for c in ledger[2:]]
    drift = max(lives) - min(lives)
    check(drift <= 64,
          "live bytes are flat across the measured window (drift %d B "
          "-- the in-flight marker strings' width grows at two-digit "
          "indices; retained state does not)" % drift)
    check(stats["nanos_jitter"] >= 0 and stats["nanos_max"] > 0,
          "the time stats are present")
    check(blob["exit"] == 0 and blob["protocol_error"] is None,
          "a clean exit, no protocol error")

    # extra argv reaches the program: the marker count follows the plan
    p = run(RT + ["--budget-allocs", "20000", "--budget-bytes", "1048576",
                  STEADY, "6"])
    check(p.returncode == 0 and "cycle 5" in p.stdout
          and "cycle 6 " not in p.stdout.replace("cycle 5", "X"),
          "extra argv drives the workload's own plan (6 cycles)")
else:
    check(False, "bin/hlc missing — the steady section cannot run")

# ===========================================================================
print("Section 5 — the enforcement matrix")

if NATIVE:
    p = run(RT + ["--budget-allocs", "200", OVER_ALLOCS])
    check(p.returncode == 1 and "RT REFUSED" in p.stdout,
          "the allocs canary is refused (exit 1)")
    check("in-process enforcement fired (exit 101)" in p.stdout,
          "the refusal names the in-process leg")
    check(re.search(r"exit:\s+101", p.stdout) is not None,
          "the program died its own death (exit 101), not the tool's")
    m = re.search(r"soft real-time violation: cycle \d+ made \d+ "
                  r"allocations \(budget 200\)", p.stdout)
    check(m is not None,
          "the canonical allocs-axis message, the runtime's own line")
    # the ramp, verified from the ledger: the per-cycle traffic grows
    # strictly, and the cycle the runtime killed is EXACTLY the first
    # cycle the ledger shows over 200 — enforcement and ledger agree on
    # the witness
    pj = os.path.join(TMP, "ramp.json")
    run(RT + ["--no-enforce", "--budget-allocs", "1000000", "--json",
              pj, OVER_ALLOCS])
    ramp = json.load(open(pj))["ledger"]["cycles"]
    allocs_seq = [c["allocs"] for c in ramp]
    check(all(a < b for a, b in zip(allocs_seq, allocs_seq[1:])),
          "the ramp is strictly increasing cycle over cycle (%s...)"
          % allocs_seq[:4])
    first_over = next(i for i, c in enumerate(ramp)
                      if c["allocs"] > 200)
    named = int(re.search(r"cycle (\d+)", m.group(0)).group(1))
    made = int(re.search(r"made (\d+) allocations", m.group(0)).group(1))
    check(named == first_over
          and made == ramp[first_over]["allocs"],
          "the runtime killed cycle %d with %d allocations — the ledger's "
          "first over-budget cycle, to the allocation"
          % (named, made))

    p = run(RT + ["--budget-bytes", "8192", OVER_BYTES])
    check(p.returncode == 1 and "RT REFUSED" in p.stdout,
          "the bytes canary is refused (exit 1)")
    m = re.search(r"soft real-time violation: cycle \d+ allocated \d+ "
                  r"bytes \(budget 8192\)", p.stdout)
    check(m is not None,
          "the canonical bytes-axis message (few allocations, big "
          "payload)")

    p = run(RT + ["--budget-ms", "10", DEADLINE])
    check(p.returncode == 1 and "RT REFUSED" in p.stdout,
          "the deadline canary is refused (exit 1)")
    m = re.search(r"soft real-time violation: cycle \d+ took \d+ ns "
                  r"\(deadline 10000000 ns\)", p.stdout)
    check(m is not None,
          "the canonical time-axis message (the runtime's own clock)")

    p = run(RT + ["--budget-allocs", "60", WARMUP])
    check(p.returncode == 1 and "RT REFUSED" in p.stdout,
          "the fat-first-cycle canary without warmup is refused")
    p = run(RT + ["--budget-allocs", "60", "--warmup", "1", "--json",
                  os.path.join(TMP, "w.json"), WARMUP])
    check(p.returncode == 0 and "RT BOUNDED" in p.stdout,
          "the same canary with --warmup 1 certifies: cycle 0 measured "
          "but not enforced, the quiet line bounded")
    blob = json.loads(open(os.path.join(TMP, "w.json")).read())
    check(blob["config"]["warmup"] == 1 and blob["stats"]["cycles"] == 11,
          "the warmup cycle is excluded from the measured window")

    p = run(RT + ["--no-enforce", "--budget-allocs", "200", OVER_ALLOCS])
    check(p.returncode == 1 and "RT REFUSED" in p.stdout,
          "--no-enforce: the run completes, the verdict still refuses")
    check("in-process enforcement fired" not in p.stdout,
          "the refusal is NOT the program's death (the mode was off)")
    check("the ledger shows the overrun" in p.stdout
          and "measure only" in p.stdout,
          "the ledger leg judged alone, and says so")
    check(re.search(r"exit:\s+0", p.stdout) is not None,
          "the canary ran to completion (exit 0) — measured, not "
          "judged in-process")
else:
    check(False, "bin/hlc missing — the enforcement section cannot run")

# ===========================================================================
print("Section 6 — the honesty gates")

if NATIVE:
    p = run(RT + ["--budget-allocs", "1000", NO_CYCLES])
    check(p.returncode == 1 and "RT INCONCLUSIVE" in p.stdout,
          "a marker-free program is INCONCLUSIVE (exit 1)")
    check("only 0 measured cycle(s)" in p.stdout,
          "the reason says the sample was empty, not the verdict vague")

    p = run(RT + ["--budget-allocs", "100000", "--budget-bytes",
                  "10000000", LATE_WORK])
    check(p.returncode == 1 and "RT INCONCLUSIVE" in p.stdout,
          "the late-work canary is INCONCLUSIVE (exit 1)")
    check("the markers do not cover the work" in p.stdout,
          "the tail rule fires: %d tail allocations vs a quiet marked "
          "phase" % 4000)

    bad_marker = write_probe("bad_marker.hls",
                             'fn main() -> int uses IO {\n'
                             '    println("__HLRT_CYCLE__ abc")\n'
                             '    return 0\n'
                             '}\n')
    p = run(RT + ["--budget-allocs", "1000", bad_marker])
    check(p.returncode == 1 and "malformed cycle marker" in p.stdout,
          "a malformed marker is a loud protocol error")

    twice = write_probe("twice.hls",
                        'fn main() -> int uses IO {\n'
                        '    println("__HLRT_CYCLE__ 0")\n'
                        '    println("__HLRT_CYCLE__ 0")\n'
                        '    return 0\n'
                        '}\n')
    p = run(RT + ["--budget-allocs", "1000", twice])
    check(p.returncode == 1 and "does not advance" in p.stdout,
          "a non-advancing marker is a loud protocol error")

    stem = write_probe("stem.hls",
                       'fn main() -> int uses IO {\n'
                       '    println("hello __HLRT_CYCLE__ 0")\n'
                       '    return 0\n'
                       '}\n')
    p = run(RT + ["--budget-allocs", "1000", stem])
    check(p.returncode == 1 and "reserved stem" in p.stdout,
          "a look-alike (stem in a non-record) is a loud protocol error")

    talker = write_probe("talker.hls",
                         'fn main() -> int uses IO {\n'
                         '    println("chatter before")\n'
                         '    println("__HLRT_CYCLE__ 0")\n'
                         '    println("chatter after")\n'
                         '    return 0\n'
                         '}\n')
    p = run(RT + ["--budget-allocs", "1000", talker])
    check(p.returncode == 1 and "RT INCONCLUSIVE" in p.stdout
          and "only 1 measured cycle(s)" in p.stdout,
          "ordinary output survives the protocol scan (a talking "
          "workload is legal; one cycle is just too few to certify)")
else:
    check(False, "bin/hlc missing — the honesty section cannot run")

# ===========================================================================
print("Section 7 — the report and the numbers")

check(mod.median([5]) == 5, "the median of one is the one")
check(mod.median([3, 1, 2]) == 2, "the odd median sorts to the middle")
check(mod.median([100, 201]) == 150,
      "the even median floors the average (the core.rt definition)")
check(abs(mod.p95([0] * 19 + [100]) - 5.0) < 1e-9,
      "the p95 interpolates (the hls-bench estimator)")
check(mod.fmt_bytes(2048) == "2.0 KiB"
      and mod.fmt_bytes(3 * 1024 * 1024) == "3.00 MiB"
      and mod.fmt_bytes(512) == "512 B",
      "the human byte formats")
check(mod.fmt_nanos(1500000) == "1.50 ms"
      and mod.fmt_nanos(2500) == "2.5 us"
      and mod.fmt_nanos(40) == "40 ns",
      "the human time formats")

for flag, why in (("--budget-allocs", "allocs"), ("--budget-bytes",
                                                  "bytes")):
    pass  # the plan line is asserted through the steady run above

if NATIVE:
    p = run(RT + ["--budget-allocs", "20000", "--budget-bytes", "1048576",
                  "--warmup", "2", STEADY, "6"])
    for needle, why in (
        ("hls-rt 0.144.0-alpha", "the header carries the version"),
        ("budget 20000 allocs/cycle", "the plan line names the axis"),
        ("deadline off", "an unset axis says off"),
        ("warmup 2", "the plan line names the warmup"),
        ("mode:            enforce", "the mode line says enforce"),
        ("per-cycle ledger", "the ledger block is titled"),
        ("measured window: 4 cycles", "the window after the warmup"),
        ("tail after the last mark", "the coverage line is present"),
        ("RT BOUNDED", "the verdict word"),
    ):
        check(needle in p.stdout, "%s" % why)

    text_p = p.stdout
    jpath = os.path.join(TMP, "layout.json")
    run(RT + ["--budget-allocs", "20000", "--budget-bytes", "1048576",
              "--warmup", "2", "--json", jpath, STEADY, "6"])
    blob = json.load(open(jpath))
    check(blob["verdict"] == "bounded" and blob["exit"] == 0,
          "the json verdict agrees with the text and the exit code")
    check(blob["stats"] is not None and blob["stats"]["cycles"] == 4,
          "the json stats agree with the measured window")
    check(len(blob["ledger"]["cycles"]) == 6,
          "the json ledger holds every closed cycle (warmup included)")

# ===========================================================================
print("Section 8 — the differential and the corpus hygiene")

p = run(BOOT + ["--check", FIXTURE])
check("OK: types and effects valid" in p.stdout,
      "the model fixture boots through the real checker")

interp = run(BOOT + [FIXTURE])
check(interp.returncode == 0 and "rt model ok: 62 assertions"
      in interp.stdout,
      "the model fixture passes on the interpreter (62 assertions)")

if NATIVE:
    fc = os.path.join(TMP, "fixture.c")
    fb = os.path.join(TMP, "fixture.bin")
    r = run([HLC, FIXTURE, fc])
    check(r.returncode == 0, "the model fixture compiles natively")
    r = run([CC, "-O2", "-o", fb, fc, "-lm", "-pthread"])
    check(r.returncode == 0, "the model fixture links")
    native = subprocess.run([fb], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=600)
    check(native.returncode == 0
          and native.stdout == interp.stdout,
          "interpreter and native agree byte for byte")

    pc = os.path.join(TMP, "panic.c")
    pb = os.path.join(TMP, "panic.bin")
    r = run([HLC, PANIC_FIXTURE, pc])
    r2 = run([CC, "-O2", "-o", pb, pc, "-lm", "-pthread"])
    if r.returncode == 0 and r2.returncode == 0:
        pnat = subprocess.run([pb], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=600)
        CANON = "soft real-time violation: cycle 7 made 1200 " \
                "allocations (budget 1000)"
        check(pnat.returncode == 101 and CANON in pnat.stderr,
              "the native enforce-over dies 101 with the canonical "
              "message")
        pinterp = run(BOOT + [PANIC_FIXTURE])
        check(pinterp.returncode == 101 and CANON in pinterp.stderr,
              "the interpreter agrees (exit 101, same message)")
    else:
        check(False, "the panic fixture compiles natively")

for path in (FIXTURE, PANIC_FIXTURE, STEADY, OVER_ALLOCS, OVER_BYTES,
             DEADLINE, WARMUP, NO_CYCLES, LATE_WORK):
    p = run([sys.executable, "tools/hlfmt.py", "-c", path])
    check("already formatted" in p.stdout,
          "%s is hlfmt-canonical" % os.path.basename(path))
p = run([sys.executable, "tools/hllint.py", FIXTURE])
check("no warnings" in p.stdout, "the model fixture is hllint-clean")
p = run([sys.executable, "tools/hllint.py", STEADY])
check("no warnings" in p.stdout, "the steady workload is hllint-clean")

litter = []
for d in (os.path.join(ROOT, "tests"),
          os.path.join(ROOT, "tests", "rt_libs")):
    for f in os.listdir(d):
        pth = os.path.join(d, f)
        if os.path.isfile(pth) and (f.startswith("hlrt-")
                                    or f.startswith("_gate_s126_")):
            litter.append(f)
check(not litter, "no gate temp files survive anywhere (%s)"
      % (", ".join(litter) or "clean"))

# ===========================================================================
shutil.rmtree(TMP, ignore_errors=True)
print()
if FAIL == 0:
    print("ACCEPTANCE OK: Stage 126 — hls-rt (%d checks)" % PASS)
    sys.exit(0)
print("ACCEPTANCE FAILED: %d of %d checks failed" % (FAIL, PASS + FAIL))
sys.exit(1)
