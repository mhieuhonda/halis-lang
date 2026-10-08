#!/usr/bin/env python3
"""Stage 124 acceptance gate — hls-bench, the criterion-style
micro-benchmark runner.

Run with `make bench-acceptance` (or
`python3 tests/bench_acceptance.py`).

Eight sections over the REAL runner CLI (subprocess, the same interface
a user drives) and the runner's own statistics module (imported for the
numerics the report prints):

  1. the convention  — a suite is a main-less .hls file; --list shows
     & discovery      the bench set with its units markers; a suite that
                      defines main fails by name; a stray units fn (the
                      typo case), a units fn with parameters, a units fn
                      with the wrong declared return, and a benchmark
                      with parameters all fail with the fix in the
                      message; a file with no bench_* at all is a
                      <no-benches> skip, never a failure; --grep selects
                      a subset and a grep matching nothing is a skip
  2. the driver &    — a fixed-plan run emits exactly one units record
     the protocol      (when declared), exactly N sample records at the
                      fixed iteration count, and exactly one harness
                      record; the samples' elapsed values are positive
                      and monotonic-clock honest; a bench that prints a
                      protocol look-alike fails with a protocol error; a
                      bench that prints ordinary noise measures cleanly;
                      the temp drivers are dot-files, deleted after the
                      run
  3. the statistics  — quantile/median/MAD against hand-computed values;
                      the Tukey classification's four cells on a
                      crafted vector; bootstrap determinism (same seed,
                      same interval) and sanity (constant data collapses
                      to the point); the slope identity (total elapsed /
                      total iters); Welch p on constructed vectors
                      (identical data → not significant, 5-sigma
                      separation → significant); fmt_time's unit ladder
  4. the report      — criterion's layout: the time bracket brackets the
                      median, mean/median lines, thrpt only when units
                      are declared, the harness line, the outlier
                      block's arithmetic; the change block's wording
                      matrix driven by crafted baselines (regressed /
                      improved / no change), p and threshold printed
                      criterion-style
  5. the baselines   — --save writes one JSON per bench under
                      .hlbench/<stem>/; the record is byte-stable
                      (re-saving the loaded record rewrites the same
                      bytes); --baseline against the same run compares
                      without failing; a missing baseline is refused
                      BEFORE any measurement (exit 2); a corrupt
                      baseline is a failing result naming the file
                      (exit 1); a native baseline cannot serve an
                      interpreter run and vice versa; --save and
                      --baseline of one name compare-then-overwrite
  6. exit codes &    — 0 on a clean run, 1 when a bench fails (a
     the CLI matrix    panicking bench), 2 on usage errors (no suites,
                      unrecorded baseline); --json parses and carries
                      samples, estimates, throughput and the change
                      block; -j 2 covers the same bench set as -j 1
  7. the differential— the fixed plan is the SAME (bench, iters)
                      sequence on the Stage-0 interpreter and, when
                      bin/hlc + gcc exist, on the native backend; both
                      report positive elapsed times and equal units;
                      native is decisively faster than the interpreter
                      on the same bench; the ok fixture's output is
                      byte-identical between boot.py and the native
                      binary
  8. the corpus      — the fixture is hlfmt-canonical, hllint-clean,
     hygiene           and boot --check green; the bench lib is
                      hlfmt-canonical; no runner temp files are left
                      behind anywhere; `import "std.bench"` resolves
                      through the real loader (the lib boots through
                      the runner's probe)

Exit code 0 = all acceptance criteria met.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

BENCH = [sys.executable, "tools/hls-bench.py"]
BOOT = [sys.executable, "boot/boot.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

FIXTURE = os.path.join(ROOT, "tests", "ok", "feat_stage124_bench.hls")
LIB = os.path.join(ROOT, "tests", "bench_libs", "arith_bench.hls")

TMP = tempfile.mkdtemp(prefix="_gate_s124_", dir=os.path.join(ROOT, "tests"))

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


def run_tool(args):
    """Drive the real runner; return (stdout, stderr, returncode)."""
    p = subprocess.run(BENCH + args, capture_output=True, text=True,
                       cwd=ROOT, timeout=600)
    return p.stdout, p.stderr, p.returncode


def load_runner_module():
    """Import tools/hls-bench.py (hyphenated name → spec loader) for
    the statistics unit checks."""
    spec = importlib.util.spec_from_file_location(
        "hls_bench_gate", os.path.join(ROOT, "tools", "hls-bench.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write(name, body):
    path = os.path.join(TMP, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


SIMPLE_BENCH = """# a minimal suite
fn bench_tiny() {
    let mut i: int = 0
    while i < 20 {
        i = i + 1
    }
}
"""

# ---------------------------------------------------------------------------
print("Section 1 — the convention & discovery: suites, units, refusals")

# --list shows the bench set and the units markers.
out, err, rc = run_tool(["--list", LIB])
check(rc == 0, "--list exits 0")
check("bench_loop_sum*" in out and "units_loop_sum" not in out,
      "--list marks the bench that declares units")
check("bench_no_units" in out and "bench_no_units*" not in out,
      "--list leaves the units-less bench unmarked")
check("bench_skipped" in out, "--list shows the marked-skip bench")
check("std.bench" in open(LIB).read(),
      "the suite imports std.bench (the skip helper resolves)")

# A suite with main is refused, by name, with the fix.
p = write("has_main.hls", SIMPLE_BENCH + "\nfn main() {\n}\n")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 1, "a suite with main fails the run (rc 1)")
check("bench suite defines main" in out,
      "the main refusal names the rule (the runner is their main)")

# A stray units fn is a file-level failure; the other bench still runs.
p = write("stray.hls", SIMPLE_BENCH
          + "\nfn units_orphan() -> int {\n    return 1\n}\n")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 1, "a stray units fn fails the run")
check("stray units fn 'units_orphan'" in out,
      "the stray refusal names the orphan and the convention")

# A units fn with parameters / the wrong declared return fails THE BENCH.
p = write("units_params.hls",
          SIMPLE_BENCH + "\nfn units_tiny(n: int) -> int {\n"
          "    return n\n}\n")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 1 and "units fn 'units_tiny' must take no parameters" in out,
      "a units fn with parameters fails the bench with the fix")

p = write("units_ret.hls", SIMPLE_BENCH
          + "\nfn units_tiny() -> str {\n    return \"twenty\"\n}\n")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 1 and "must return int" in out and "'str'" in out,
      "a units fn returning str fails the bench naming both types")

# A benchmark with parameters fails before anything runs.
p = write("bench_params.hls",
          "fn bench_args(n: int) {\n    let mut i: int = 0\n"
          "    while i < n {\n        i = i + 1\n    }\n}\n")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 1 and "takes 1 parameter(s)" in out,
      "a benchmark with parameters fails with the arity in the message")

# A file with no bench_* is a skip, not a failure.
p = write("no_benches.hls",
          "fn helper(n: int) -> int {\n    return n * 2\n}\n")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 0 and "<no-benches>" in out and "no bench_* functions" in out,
      "a bench-less file is a <no-benches> skip (rc 0)")

# --grep selects; a grep matching nothing is a skip.
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2",
                         "--grep", "loop_sum", LIB])
check(rc == 0 and "bench_loop_sum" in out
      and "bench_str_grow" not in out,
      "--grep selects only the matching benches")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2",
                         "--grep", "no-such-bench", LIB])
check(rc == 0 and "<no-benches>" in out,
      "a grep matching nothing is a <no-benches> skip, never an error")

# ---------------------------------------------------------------------------
print("Section 2 — the driver & the protocol: records, noise, hygiene")

# Fixed plan: the units record, N samples at the fixed iters, the
# harness record — all parsed from the real driver's stdout.
out, err, rc = run_tool(["--fixed-iters", "123", "--samples", "4",
                         "--grep", "loop_sum", LIB])
check(rc == 0, "a fixed-plan run of the units bench exits 0")
lines = out.splitlines()
time_line = next((l for l in lines if "time:" in l), "")
thrpt_line = next((l for l in lines if "thrpt:" in l), "")
harness_line = next((l for l in lines if "harness:" in l), "")
check(bool(time_line), "the report carries the time bracket")
check(bool(thrpt_line) and "units/s" in thrpt_line,
      "the units bench reports throughput in units/s")
check(bool(harness_line) and "harness:" in harness_line,
      "the report carries the harness calibration line")

# The raw protocol: recover the driver's records by running the tool
# with --json (the samples ride in the payload).
out, err, rc = run_tool(["--fixed-iters", "123", "--samples", "4",
                         "--grep", "loop_sum", LIB, "--json",
                         os.path.join(TMP, "proto.json")])
doc = json.load(open(os.path.join(TMP, "proto.json")))
benches = doc["files"][0]["benches"]
samples = benches[0]["raw_samples"]
check(len(samples) == 4, "the plan's sample count is exact (4)")
check(all(it == 123 for (it, _el) in samples),
      "every fixed-plan sample ran exactly 123 iterations")
check(all(el > 0 for (_it, el) in samples),
      "every sample's elapsed time is positive (the clock advanced)")
check(doc["files"][0]["benches"][0]["units"] == 100,
      "the units record reached the report (100 units per iteration)")

# A protocol look-alike is a loud failure, not a dropped record.
p = write("lookalike.hls",
          "fn bench_look() uses IO {\n"
          "    println(\"__HLBENCH__ 5 5 5\")\n"
          "    let mut i: int = 0\n    while i < 5 {\n"
          "        i = i + 1\n    }\n}\n")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 1 and "protocol error" in out,
      "a protocol look-alike fails the bench loudly")

# Ordinary print noise is skipped; the measurement survives.
p = write("noise.hls",
          "fn bench_talks() uses IO {\n"
          "    println(\"step 1 done\")\n"
          "    let mut i: int = 0\n    while i < 20 {\n"
          "        i = i + 1\n    }\n}\n")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 0 and "bench_talks" in out and "time:" in out,
      "a bench that prints still measures cleanly")

# Temp hygiene: no dot-drivers left beside the suite after a run.
leftovers = [f for f in os.listdir(os.path.dirname(LIB))
             if f.startswith(".hlbench-")]
check(not leftovers, "no temp drivers are left beside the suite")

# ---------------------------------------------------------------------------
print("Section 3 — the statistics: quantiles, outliers, bootstrap, Welch")

hb = load_runner_module()

# Quantile / median / MAD against hand-computed values.
xs = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0, 5.0, 3.0]
s = sorted(xs)  # [1,1,2,3,3,4,5,5,6,9]
check(hb.quantile(s, 0.5) == 3.5, "quantile .5 of the even vector is 3.5")
check(hb.median(xs) == 3.5, "median matches the hand computation")
check(abs(hb.quantile(s, 0.25) - 2.25) < 1e-12,
      "quantile .25 interpolates linearly (2.25)")
mad_v = hb.mad(xs)
check(abs(mad_v - 1.5) < 1e-12,
      "median absolute deviation matches (1.5: |x-3.5| median)")

# The Tukey classification on a crafted vector: each of the four
# cells fires exactly once (hand-computed fences: q1=2, q3=8, iqr=6).
crafted = [-100.0, -15.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0,
           20.0, 100.0]
o = hb.classify_outliers(crafted)
check(o["low_severe"] == 1, "one low severe outlier (-100)")
check(o["low_mild"] == 1, "one low mild outlier (-15)")
check(o["high_mild"] == 1 and o["high_severe"] == 1,
      "one high mild (20) and one high severe (100)")
clean = [float(i) for i in range(1, 13)]
o2 = hb.classify_outliers(clean)
check(sum(o2.values()) == 0,
      "an arithmetic progression has no outliers at all")

# Bootstrap determinism and collapse.
const = [7.0] * 20
lo, hi = hb.bootstrap_ci(const, hb.median, seed=42)
check(lo == hi == 7.0, "a constant vector's CI collapses to the point")
xs2 = [float(i * i) for i in range(1, 31)]
lo1, hi1 = hb.bootstrap_ci(xs2, hb.median, seed=1234)
lo2, hi2 = hb.bootstrap_ci(xs2, hb.median, seed=1234)
check(lo1 == lo2 and hi1 == hi2,
      "the bootstrap is seeded: same seed, same interval")
check(lo1 <= hb.median(xs2) <= hi1,
      "the bootstrap interval brackets the point estimate")

# The slope identity: total elapsed / total iters.
samples = [(10, 1000), (20, 1500), (5, 700)]
slope = sum(el for (_i, el) in samples) / float(sum(i for (i, _e) in samples))
thr_lo, thr_hi = hb.bootstrap_ci_slope(samples, 100, seed=1)
check(0.0 < thr_lo <= thr_hi,
      "the throughput bootstrap returns an ordered interval")
check(abs(slope - (3200 / 35.0)) < 1e-12,
      "the slope estimate is total-elapsed over total-iters")
check(hb.bootstrap_ci_slope(samples, None, seed=1) is None,
      "a units-less bench has no throughput interval (None, by design)")

# Welch p: identical data is not significant; a 5-sigma separation is.
same = [10.0 + (i % 3) for i in range(12)]
p_same = hb.welch_p(same, list(same))
check(p_same > 0.05, "identical distributions are not significant")
a = [10.0, 10.2, 9.8, 10.1, 9.9, 10.05, 9.95, 10.15]
b = [20.0, 20.2, 19.8, 20.1, 19.9, 20.05, 19.95, 20.15]
p_diff = hb.welch_p(a, b)
check(p_diff < 0.001,
      "a 10-unit separation at unit scale is decisive (p < 0.001)")
p_tiny = hb.welch_p([1.0, 2.0], [3.0, 4.0])
check(0.0 <= p_tiny <= 1.0,
      "tiny samples fall back to a bounded p instead of crashing")

# fmt_time's unit ladder.
check(hb.fmt_time(500.0) == "500 ns", "500 ns stays in ns")
check(hb.fmt_time(1234.0).endswith("\u00b5s"), "1234 ns moves to micros")
check(hb.fmt_time(1.5e6).endswith("ms"), "1.5 ms stays in ms")
check(hb.fmt_time(2.0e9).endswith("s"), "2 s stays in seconds")

# ---------------------------------------------------------------------------
print("Section 4 — the report: criterion's layout and the change matrix")

# Regressed / improved via crafted baselines on the real CLI.
out, err, rc = run_tool(["--fixed-iters", "100", "--samples", "4",
                         "--grep", "loop_sum", LIB, "--save", "real"])
check(rc == 0, "--save records the baseline")
bdir = os.path.join(os.path.dirname(LIB), ".hlbench", "arith_bench")
bpath = os.path.join(bdir, "bench_loop_sum.real.json")
check(os.path.isfile(bpath), "the baseline file lands in .hlbench/<stem>/")

base = json.load(open(bpath))
slow = dict(base)
slow["estimates_ns"] = [max(base["estimates_ns"]) * 0.01] * 4
slow_path = os.path.join(bdir, "bench_loop_sum.slowreg.json")
with open(slow_path, "w") as f:
    json.dump(slow, f)
huge = dict(base)
huge["estimates_ns"] = [max(base["estimates_ns"]) * 1000.0] * 4
huge_path = os.path.join(bdir, "bench_loop_sum.fastimp.json")
with open(huge_path, "w") as f:
    json.dump(huge, f)

out, err, rc = run_tool(["--fixed-iters", "100", "--samples", "4",
                         "--grep", "loop_sum", LIB,
                         "--baseline", "slowreg"])
check(rc == 0 and "Performance has regressed." in out,
      "a 100x-faster baseline reads as a regression")
check("(p = 0.00 < 5.00%)" in out,
      "the change line prints p against the 5% level, criterion-style")
check("change:" in out and out.count("%") >= 3,
      "the change bracket carries three percentages")

out, err, rc = run_tool(["--fixed-iters", "100", "--samples", "4",
                         "--grep", "loop_sum", LIB,
                         "--baseline", "fastimp"])
check(rc == 0 and "Performance has improved." in out,
      "a 1000x-slower baseline reads as an improvement")

out, err, rc = run_tool(["--fixed-iters", "100", "--samples", "4",
                         "--grep", "loop_sum", LIB,
                         "--baseline", "real"])
check(rc == 0, "comparing against the same-machine baseline passes")
check("No change in performance detected." in out
      or "Change within noise threshold." in out,
      "an honest re-run is no-change or within-noise, never regressed")

# Byte-stability of the record: re-saving the loaded record rewrites
# the same bytes.
obj = hb.load_baseline(bpath)
hb.save_baseline(os.path.join(TMP, "rewrite.json"), obj["backend"],
                 obj["bench"], obj["units"],
                 [tuple(x) for x in obj["samples"]],
                 obj["estimates_ns"])
with open(bpath, "rb") as f:
    original = f.read()
with open(os.path.join(TMP, "rewrite.json"), "rb") as f:
    rewritten = f.read()
check(original == rewritten, "the record is byte-stable under rewrite")

# ---------------------------------------------------------------------------
print("Section 5 — the baselines: refusal, corruption, cross-backend")

# A missing baseline is refused BEFORE any measurement (exit 2).
out, err, rc = run_tool(["--fixed-iters", "100", "--samples", "4",
                         LIB, "--baseline", "never-recorded"])
check(rc == 2, "an unrecorded baseline is a usage error (exit 2)")
check("does not exist" in err and "--save" in err,
      "the refusal names the baseline and the recording flag")

# A corrupt baseline is a failing result naming the file (exit 1).
cpath = os.path.join(bdir, "bench_loop_sum.corrupt.json")
with open(cpath, "w") as f:
    f.write("this is not json")
out, err, rc = run_tool(["--fixed-iters", "100", "--samples", "4",
                         "--grep", "loop_sum", LIB,
                         "--baseline", "corrupt"])
check(rc == 1 and "is corrupt" in out,
      "a corrupt baseline fails the bench naming the file")

# A baseline from the OTHER backend cannot serve this run.
npath = os.path.join(bdir, "bench_loop_sum.nat.json")
nat = dict(base)
nat["backend"] = "native"
with open(npath, "w") as f:
    json.dump(nat, f)
out, err, rc = run_tool(["--fixed-iters", "100", "--samples", "4",
                         "--grep", "loop_sum", LIB, "--baseline", "nat"])
check(rc == 1 and "different instruments" in out,
      "a cross-backend baseline is refused with the reason")

# --save and --baseline of one name: compare happens first, then the
# overwrite (the run passes and the file is fresh).
out, err, rc = run_tool(["--fixed-iters", "100", "--samples", "4",
                         "--grep", "loop_sum", LIB,
                         "--baseline", "real", "--save", "real"])
check(rc == 0 and "time:" in out,
      "--baseline + --save of one name compares then overwrites")
check(json.load(open(bpath))["backend"] == "interpreter",
      "the overwritten record is the new run's")

# ---------------------------------------------------------------------------
print("Section 6 — exit codes & the CLI matrix: rc, --json, parallelism")

# A panicking bench fails the run (exit 1) with the panic decoded.
p = write("panic.hls", "fn bench_boom() {\n    panic(\"the bridge is "
          "out\")\n}\n")
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 1 and "the bridge is out" in out,
      "a panicking bench fails with the decoded panic message")

# A skipped bench never fails the run.
p = write("skipper.hls",
          'import "std.bench"\n\nfn bench_later() {\n'
          '    mark_skip("not on this host")\n}\n')
out, err, rc = run_tool(["--fixed-iters", "10", "--samples", "2", p])
check(rc == 0 and "not on this host" in out
      and "0 bench measured, 1 skip, 0 fail" in out,
      "a mark_skip bench is a skip with its reason, rc stays 0")

# No suites at all is a usage error (exit 2).
out, err, rc = run_tool([os.path.join(TMP, "missing.hls")])
check(rc == 2 and "no .hls suite files found" in err,
      "no discoverable suites is exit 2 with the reason on stderr")

# --json parses and carries the full payload.
jpath = os.path.join(TMP, "report.json")
out, err, rc = run_tool(["--fixed-iters", "100", "--samples", "4", LIB,
                         "--json", jpath])
doc = json.load(open(jpath))
check(doc["tool"] == "hls-bench" and doc["backend"] == "interpreter",
      "the json document names the tool and the backend")
benches = {b["name"]: b for b in doc["files"][0]["benches"]}
check(set(benches) == {"bench_loop_sum", "bench_str_grow",
                       "bench_no_units", "bench_skipped"},
      "the json document covers every bench in the suite")
loop = benches["bench_loop_sum"]
est = loop["estimates_ns_per_iter"]
check(est["median_ci"][0] <= est["median"] <= est["median_ci"][1],
      "the median sits inside its own bootstrap interval")
check(len(loop["raw_samples"]) == 4,
      "the json carries the raw (iters, elapsed) samples")
sk = benches["bench_skipped"]
check(sk["status"] == "skip"
      and "wired this bench to skip" in sk.get("detail", ""),
      "the json records the skip with its reason")
check(loop["throughput_units_per_s"] > 0.0
      and benches["bench_no_units"]["throughput_units_per_s"] is None,
      "throughput appears exactly when units are declared")

# -j 2 covers the same bench set as -j 1 (the pool changes nothing).
out2, _, rc2 = run_tool(["-j", "2", "--fixed-iters", "50",
                         "--samples", "2", LIB])
check(rc2 == 0 and "bench_loop_sum" in out2 and "bench_no_units" in out2,
      "a -j 2 run measures the full bench set")

# ---------------------------------------------------------------------------
print("Section 7 — the differential: one plan, two backends")

if NATIVE:
    # The fixed plan is the same (bench, iters) sequence on both
    # backends; only the clock readings differ, as they must.
    ji = os.path.join(TMP, "plan_i.json")
    jn = os.path.join(TMP, "plan_n.json")
    out, _, rci = run_tool(["--fixed-iters", "60", "--samples", "3",
                            LIB, "--json", ji])
    out, _, rcn = run_tool(["--native", "--fixed-iters", "60",
                            "--samples", "3", LIB, "--json", jn])
    di = json.load(open(ji))
    dn = json.load(open(jn))
    bi = {b["name"]: b for b in di["files"][0]["benches"]}
    bn = {b["name"]: b for b in dn["files"][0]["benches"]}
    measured = ["bench_loop_sum", "bench_str_grow", "bench_no_units"]
    check(rci == 0 and rcn == 0, "both backends run the fixed plan clean")
    check([s[0] for s in bi["bench_loop_sum"]["raw_samples"]]
          == [s[0] for s in bn["bench_loop_sum"]["raw_samples"]]
          == [60, 60, 60],
          "the (bench, iters) plan is IDENTICAL across backends")
    check(all(el > 0 for (_i, el) in bn["bench_loop_sum"]["raw_samples"]),
          "the native samples' clocks advanced too")
    check(bi["bench_loop_sum"]["units"] == bn["bench_loop_sum"]["units"]
          == 100,
          "the units record is the same on both backends")

    # Native is decisively faster on the same bench (the interpreter is
    # ~4 orders of magnitude slower; 20x is the honest floor).
    mi = bi["bench_loop_sum"]["estimates_ns_per_iter"]["median"]
    mn = bn["bench_loop_sum"]["estimates_ns_per_iter"]["median"]
    check(mn * 20.0 < mi,
          "native median is >20x faster than the interpreter median")

    # The ok fixture: interpreter and native byte-identical.
    p = subprocess.run(BOOT + [FIXTURE], capture_output=True, timeout=120)
    interp_out = p.stdout
    cpath = os.path.join(TMP, "fixture.c")
    bpath_n = os.path.join(TMP, "fixture.bin")
    subprocess.run([HLC, FIXTURE, cpath], capture_output=True,
                   timeout=300, cwd=ROOT)
    subprocess.run([CC, "-O2", "-o", bpath_n, cpath, "-lm", "-pthread"],
                   capture_output=True, timeout=300)
    p = subprocess.run([bpath_n], capture_output=True, timeout=120)
    native_out = p.stdout
    check(interp_out == native_out and interp_out != b"",
          "the fixture's output is byte-identical across backends")
else:
    print("  [SKIP] native half (bin/hlc or gcc missing)")

# ---------------------------------------------------------------------------
print("Section 8 — corpus hygiene: canonical form, checks, no litter")

p = subprocess.run([sys.executable, "tools/hlfmt.py", "-c", FIXTURE],
                   capture_output=True, text=True)
check("already formatted" in p.stdout, "the fixture is hlfmt-canonical")
p = subprocess.run([sys.executable, "tools/hlfmt.py", "-c", LIB],
                   capture_output=True, text=True)
check("already formatted" in p.stdout, "the bench lib is hlfmt-canonical")
p = subprocess.run([sys.executable, "tools/hllint.py", FIXTURE],
                   capture_output=True, text=True)
check("no warnings" in p.stdout, "the fixture is hllint-clean")
p = subprocess.run(BOOT + ["--check", FIXTURE],
                   capture_output=True, text=True)
check("OK: types and effects valid" in p.stdout,
      "the fixture boots through the real checker")

# The lib has no main (the runner is its program) — the real loader
# accepts it only through the probe (a suite is a module, and the
# runner's probe is what gives the checker a main).
lib_src = open(LIB).read()
check("fn main" not in lib_src,
      "the bench lib defines no main (a suite is a library)")

# No runner litter anywhere: no dot-drivers, and the gate's own
# .hlbench scratch is removed.
litter = []
for d in (os.path.dirname(LIB), TMP, os.path.join(ROOT, "tests")):
    for f in os.listdir(d):
        if f.startswith(".hlbench-") and f.endswith(".hls"):
            litter.append(os.path.join(d, f))
check(not litter, "no temp drivers survive anywhere the gate ran")

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(os.path.join(os.path.dirname(LIB), ".hlbench"),
              ignore_errors=True)
print()
if FAIL == 0:
    print("ACCEPTANCE OK: Stage 124 — hls-bench (%d checks)" % PASS)
    sys.exit(0)
print("ACCEPTANCE FAILED: %d of %d checks failed" % (FAIL, PASS + FAIL))
sys.exit(1)
