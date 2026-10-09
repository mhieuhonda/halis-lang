#!/usr/bin/env python3
"""Stage 125 acceptance gate — hls-rss, the RSS-stability verifier.

Run with `make rss-acceptance` (or
`python3 tests/rss_acceptance.py`).

Eight sections over the REAL verifier CLI (subprocess, the same
interface a user drives) and the verifier's own statistics module
(imported for the numerics the report prints):

  1. the convention  — a workload is an ordinary program; the plan is
     & the refusals    driven through argv[1]; markers must parse and
                       advance; a malformed marker, a non-advancing
                       marker and a protocol look-alike are loud
                       failures (exit 1); ordinary output survives; a
                       missing file, a non-.hls argument and bad
                       option values are usage failures (exit 2)

  2. the sampler     — a cooperative run observes exactly the
     & the protocol    requested markers; cycle-aligned points pair
                       marker indices with resident pages; a program
                       that prints no markers is analysed against
                       time and the report says so; the pre-exec fork
                       window never enters the trace (every recorded
                       page count belongs to the workload binary)

  3. the statistics  — Theil-Sen against hand-computed data; the
                       seeded bootstrap's byte-determinism and its
                       collapse on constant data; decimation caps and
                       endpoint preservation; the verdict lattice
                       (CI under the threshold = stable, over = leak,
                       straddling = inconclusive)

  4. the interposer  — the balance census over the fresh-result
     & the census      builtins the Stage 125 expr_fresh fix
                       registered (join, env_get, cwd_get,
                       sys_hostname, read_line, fs_read_dir,
                       proc_child_read's shape via join): LIVE is a
                       small constant independent of the call count,
                       mallocs scale linearly with it, cwd_get's
                       libc buffers are FOREIGN_FREE not a
                       misbalance; the LIVE identity is re-verified
                       and a corrupt report is refused; the retained
                       probe (in-scope exit(0)) moves LIVE by exactly
                       the payload count

  5. the report      — the layout: header, plan, the timeline block,
                       the slope with its CI and threshold, the
                       retained block with both runs, the composed
                       verdict; --json parses and carries the trace,
                       the config, both instruments and the verdict;
                       the verdict word matches the exit code

  6. the two-run     — a cooperative workload gets the retained
     comparison        verdict (live delta 0, the exact-free
                       property); a workload that ignores argv[1] is
                       an HONEST skip with the reason in the report;
                       the canary's retained instrument fires (live
                       delta > 0); --no-malloc-count skips the
                       instrument and the timeline stands alone

  7. the differential— the ok fixture's output is byte-identical on
     & the leak matrix the Stage-0 interpreter and the native
                       backend; the churn lib certifies STABLE (exit
                       0), the server lib certifies STABLE, the
                       canary earns LEAK (exit 1) with BOTH
                       instruments firing; the hoisting-cycle
                       regression (a match inside a concat inside a
                       call argument — the Stage 121 corpus shape)
                       compiles, runs and leaks nothing

  8. the corpus      — the fixture and the workloads are
     hygiene           hlfmt-canonical, the fixture is hllint-clean
                       and boots through the real checker; no gate
                       litter survives anywhere

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

RSS = [sys.executable, "tools/hls-rss.py"]
BOOT = [sys.executable, "boot/boot.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

FIXTURE = os.path.join(ROOT, "tests", "ok", "feat_stage125_rss.hls")
CHURN = os.path.join(ROOT, "tests", "rss_libs", "churn_rss.hls")
SERVER = os.path.join(ROOT, "tests", "rss_libs", "server_rss.hls")
CANARY = os.path.join(ROOT, "tests", "rss_libs", "leak_canary.hls")
WRAP = os.path.join(ROOT, "tools", "rss_parts", "malloc_balance_wrap.c")

TMP = tempfile.mkdtemp(prefix="_gate_s125_", dir=os.path.join(ROOT, "tests"))

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


def interpose_run(hls_path, args, n=None):
    """Compile `hls_path` with the balance interposer, run it, return
    the counter dict from stderr. stdin is /dev/null so a read_line()
    probe reads EOF, never the gate's own terminal."""
    c_path = os.path.join(TMP, os.path.basename(hls_path) + ".c")
    bin_path = os.path.join(TMP, os.path.basename(hls_path) + ".bin")
    if not os.path.exists(bin_path):
        r = run([HLC, hls_path, c_path])
        if r.returncode != 0:
            return None
        r = run([CC, "-O2", "-o", bin_path, c_path, WRAP,
                 "-lm", "-pthread",
                 "-Wl,--wrap=malloc", "-Wl,--wrap=calloc",
                 "-Wl,--wrap=realloc", "-Wl,--wrap=free"])
        if r.returncode != 0:
            return None
    r = subprocess.run([bin_path] + list(args), capture_output=True,
                       text=True, timeout=600,
                       stdin=subprocess.DEVNULL)
    counters = {}
    for line in r.stderr.splitlines():
        if line.startswith("HL_RSS_"):
            k, _, v = line[len("HL_RSS_"):].partition("=")
            try:
                counters[k] = int(v)
            except ValueError:
                return None
    return counters if "LIVE" in counters else None


# ===========================================================================
print("Section 1 — the convention and the refusal matrix")

p = run(RSS + ["--cycles", "4", "no-such-file.hls"])
check(p.returncode == 2 and "no such workload" in p.stderr,
      "a missing workload is a usage failure (exit 2)")
p = run(RSS + ["--cycles", "4", "README.md"])
check(p.returncode == 2 and "must be a .hls program" in p.stderr,
      "a non-.hls argument is a usage failure (exit 2)")
for bad_args, why in (
    (["--cycles", "2", "x.hls"], "cycles < 3"),
    (["--interval", "0.5", "x.hls"], "interval < 1 ms"),
    (["--bootstrap", "10", "x.hls"], "bootstrap < 100"),
    (["--warmup", "0", "--cycles", "10", "x.hls"], "warmup < 1"),
):
    p = run(RSS + bad_args)
    check(p.returncode == 2, "bad option (%s) refused with exit 2" % why)

# A malformed marker: the record line must parse exactly.
MALFORMED = """fn main() -> int uses IO {
    println("__HLRSS_CYCLE__ soon")
    return 0
}
"""
p = run(RSS + ["--cycles", "5", write_probe("malformed.hls", MALFORMED)])
check(p.returncode == 1 and "malformed cycle marker" in p.stderr,
      "a malformed marker is a loud protocol failure (exit 1)")

# A look-alike: a line CONTAINING the stem that is not a record.
LOOKALIKE = """fn main() -> int uses IO {
    println("the magic prefix is __HLRSS_X__ (do not print this)")
    return 0
}
"""
p = run(RSS + ["--cycles", "5", write_probe("lookalike.hls", LOOKALIKE)])
check(p.returncode == 1 and "reserved stem" in p.stderr,
      "a protocol look-alike corrupts the stream and fails loudly")

# A non-advancing marker: indices must strictly increase.
REWIND = """fn main() -> int uses IO {
    println("__HLRSS_CYCLE__ 0")
    println("__HLRSS_CYCLE__ 0")
    return 0
}
"""
p = run(RSS + ["--cycles", "5", write_probe("rewind.hls", REWIND)])
check(p.returncode == 1 and "does not advance" in p.stderr,
      "a non-advancing marker is a protocol failure")

# Ordinary output survives: noise lines are captured, never fatal.
NOISY = """fn main() -> int uses IO {
    let mut i: int = 0
    while i < 9 {
        println("worker " + i.to_str() + " says things")
        println("__HLRSS_CYCLE__ " + i.to_str())
        i = i + 1
    }
    return 0
}
"""
p = run(RSS + ["--cycles", "6", "--warmup", "3",
               write_probe("noisy.hls", NOISY)])
check(p.returncode in (0, 1) and "protocol error" not in p.stderr,
      "a workload that talks between markers measures cleanly")

# ===========================================================================
print("Section 2 — the sampler and the protocol shape")

p = run(RSS + ["--cycles", "30", "--warmup", "10", "--bootstrap", "200",
               "--json", os.path.join(TMP, "fx.json"), FIXTURE])
try:
    blob = json.load(open(os.path.join(TMP, "fx.json")))
except Exception:
    blob = {}
check(p.returncode == 0, "the fixture certifies STABLE through the CLI")
check(blob.get("run_a", {}).get("observed_markers") == 40,
      "a cooperative run observes exactly the requested 40 markers")
cps = blob.get("run_a", {}).get("cycle_points", [])
check(len(cps) >= 8 and all(c[2] > 0 for c in cps),
      "cycle-aligned points exist and every page count is positive "
      "(zombie and pre-exec reads never enter the trace)")
check(all(cps[i][0] < cps[i + 1][0] for i in range(len(cps) - 1)),
      "the marker indices in the trace strictly advance")
check(blob.get("run_b", {}).get("observed_markers") == 10,
      "run B (warmup-only) observed exactly its 10 markers")
check("retained" in blob and blob["retained"] is not None,
      "the retained instrument ran (both runs cooperative)")

# A program that prints no markers: the time-based fallback, labelled.
QUIET = """fn main() -> int uses IO {
    let mut i: int = 0
    let mut acc: int = 0
    while i < 400000 {
        let s: str = "x" + i.to_str()
        acc = acc + s.len()
        i = i + 1
    }
    println("acc=" + acc.to_str())
    return 0
}
"""
p = run(RSS + ["--cycles", "30", write_probe("quiet.hls", QUIET)])
check("none (time-based analysis)" in p.stdout,
      "a marker-less program is analysed against time and says so")

# ===========================================================================
print("Section 3 — the statistics against hand-computed values")

spec = importlib.util.spec_from_file_location(
    "hls_rss_mod", os.path.join(ROOT, "tools", "hls-rss.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Theil-Sen: for (0,0), (1,1), (2,2), (10,1) the pairwise slopes are
# 1,1,0.1,1,0.1,0.1 -> median 0.55... hand-compute: sorted slopes
# [0.1,0.1,0.1,1,1,1] -> median 0.55 = (0.1+1)/2.
slope, pairs = mod.theil_sen([(0, 0), (1, 1), (2, 2), (10, 1)])
check(abs(slope - 0.55) < 1e-12 and pairs == 6,
      "theil-sen matches the hand-computed median of pairwise slopes")
check(mod.theil_sen([(i, 2 * i) for i in range(10)])[0] == 2.0,
      "a perfect line's theil-sen slope is exact")
try:
    mod.theil_sen([(5, 1)])
    check(False, "theil-sen refuses a single point")
except mod.VerifyError:
    check(True, "theil-sen refuses data with no distinct-x pairs")

# The bootstrap: same seed -> same interval, byte for byte.
pts = [(i, 1.0 + (i % 7) * 0.01) for i in range(40)]
a1 = mod.bootstrap_ci_slope(pts, 200, mod._seed("churn_rss"))
a2 = mod.bootstrap_ci_slope(pts, 200, mod._seed("churn_rss"))
check(a1 == a2, "the seeded bootstrap is byte-deterministic")
flat = [(i, 5.0) for i in range(30)]
lo, hi = mod.bootstrap_ci_slope(flat, 200, 7)
check(abs(lo) < 1e-9 and abs(hi) < 1e-9,
      "constant data collapses the CI to the point (no slope)")

# Decimation: cap respected, endpoints kept.
many = [(i, i) for i in range(3000)]
few = mod.decimate(many)
check(len(few) <= 500 and few[0][0] == 0 and few[-1][0] == 2999,
      "decimation caps the fit and keeps both endpoints")
check(mod.decimate([(1, 1), (2, 2)]) == [(1, 1), (2, 2)],
      "small traces pass decimation untouched")

# Quantile on sorted data (linear interpolation).
xs = sorted([3.0, 1.0, 2.0, 4.0, 5.0])
check(mod.quantile(xs, 0.5) == 3.0, "the median quantile interpolates")
check(abs(mod.quantile(xs, 0.25) - 2.0) < 1e-12,
      "the lower quartile interpolates")

# The verdict lattice (the slope classifier's contract).
cfg = {"threshold": 0.25, "threshold_time": 2.0}


class T:  # a trace stub for the classifier
    def __init__(self, points, timeline=None):
        self.cycle_points = points
        self.timeline = timeline or []
        self.protocol_error = None
        self.markers = [c for c, _t, _p in points]


t_stable = mod.analyse_slope(T([(c, 100, 100) for c in range(40)]),
                             10, dict(cfg, bootstrap=200), "s")
check(t_stable["verdict"] == mod.STABLE, "a flat trace certifies STABLE")
rising = [(c, 100, 100 + 3 * c) for c in range(40)]
t_leak = mod.analyse_slope(T(rising), 10, dict(cfg, bootstrap=200), "s")
check(t_leak["verdict"] == mod.LEAK,
      "a +3 page/cycle trace is LEAK (CI above the threshold)")
tiny = [(c, 100, 100 + 0.01 * c) for c in range(40)]
t_slow = mod.analyse_slope(T(tiny), 10, dict(cfg, bootstrap=200), "s")
check(t_slow["verdict"] == mod.STABLE,
      "a +0.01 page/cycle trace is within the default threshold")
check(mod.compose_verdict([{"verdict": mod.STABLE},
                           {"verdict": mod.STABLE}]) == mod.STABLE,
      "a clean sheet composes STABLE")
check(mod.compose_verdict([{"verdict": mod.STABLE},
                           {"verdict": mod.LEAK}]) == mod.LEAK,
      "any leak composes LEAK")
check(mod.compose_verdict([{"verdict": mod.STABLE},
                           {"verdict": mod.INCONCLUSIVE}])
      == mod.INCONCLUSIVE,
      "an unresolved question composes INCONCLUSIVE")

# ===========================================================================
print("Section 4 — the interposer and the fresh-builtin census")

check(mod.parse_interposer(b"") is None,
      "no report is no counters (refused, not zeroed)")
check(mod.parse_interposer(b"HL_RSS_MALLOC=x\n") is None,
      "a corrupt report line is refused")
check(mod.parse_interposer(b"HL_RSS_MALLOC=1\nHL_RSS_CALLOC=0\n"
      b"HL_RSS_REALLOC=0\nHL_RSS_REALLOC_NULL=0\nHL_RSS_REALLOC_ZERO=0\n"
      b"HL_RSS_FREE=1\nHL_RSS_FOREIGN_FREE=0\nHL_RSS_LIVE=5\n"
      b"HL_RSS_OVERFLOW=0\n") is None,
      "a report whose LIVE identity does not close is refused")

# The census: every fresh-result builtin the Stage 125 expr_fresh fix
# registered must BALANCE under the interposer — LIVE independent of
# the call count, mallocs growing linearly with it. Before the fix
# each of these leaked one heap object per call on the native backend
# (invisible to the differential suite: the interpreter is GC-ed).
# proc_child_read needs a live spawned child to probe; its result is
# a fresh str through the same codegen path read_line takes, which
# the census pins instead.
CENSUS = """import "std.taint"

fn main() -> int uses IO, Proc {
    let a: list[str] = args()
    let mut mode: int = 0
    if a.len() > 1 {
        mode = a.get(1).to_int()
    }
    let mut checksum: int = 0
    let mut i: int = 0
    let n: int = 2000
    while i < n {
        if mode == 0 {
            let j: str = join(["alpha", "beta"], "-")
            checksum = checksum + j.len()
        } else if mode == 1 {
            let p: str = env_get("PATH")
            checksum = checksum + p.len()
        } else if mode == 2 {
            let d: str = cwd_get()
            checksum = checksum + d.len()
        } else if mode == 3 {
            let h: str = sys_hostname()
            checksum = checksum + h.len()
        } else if mode == 4 {
            let xs: list[str] = fs_read_dir(".")
            checksum = checksum + xs.len()
        } else if mode == 5 {
            let l: tainted[str] = read_line()
            checksum = checksum + taint_check_len(l)
        }
        i = i + 1
    }
    println("checksum=" + checksum.to_str())
    return 0
}
"""
census = write_probe("census.hls", CENSUS)
if NATIVE:
    base = {}
    balanced = True
    for m, name in enumerate(("join", "env_get", "cwd_get",
                              "sys_hostname", "fs_read_dir",
                              "read_line")):
        c1 = interpose_run(census, ["%d" % m]) or {}
        c2 = interpose_run(census, ["%d" % m]) or {}
        live1, live2 = c1.get("LIVE"), c2.get("LIVE")
        if live1 is None or live1 != live2 or live1 > 32:
            balanced = False
            bad("census %s: LIVE %r/%r (expected a small constant)"
                % (name, live1, live2))
        else:
            base[name] = live1
    check(balanced and len(base) == 6,
          "every fresh-result builtin balances: LIVE is a small "
          "constant across runs (%s)"
          % ", ".join("%s=%d" % kv for kv in sorted(base.items())))
    c_join = interpose_run(census, ["0"]) or {}
    check(c_join.get("MALLOC", 0) > 1000,
          "the census really churned (thousands of tracked mallocs)")
    # cwd_get hands libc's getcwd buffer back to free(): a FOREIGN
    # free, not a misbalance (the counting-only draft read LIVE=-1994
    # here).
    c_cwd = interpose_run(census, ["2"]) or {}
    check(c_cwd.get("FOREIGN_FREE", -1) == 2000
          and c_cwd.get("LIVE") == base.get("cwd_get"),
          "cwd_get's libc buffers are FOREIGN_FREE, not a misbalance "
          "(%d foreign frees)" % c_cwd.get("FOREIGN_FREE", -1))
    check(c_join.get("OVERFLOW") == 0,
          "the tracking table never overflowed")

    # The retained shape: an in-scope exit(0) freezes the scope, and
    # LIVE moves by exactly the payload count.
    HOLD = """fn main() -> int uses IO {
    let a: list[str] = args()
    let mut keep: int = 3
    if a.len() > 1 {
        keep = a.get(1).to_int()
    }
    let held: list[str] = []
    let mut i: int = 0
    while i < keep {
        held.push("payload-" + i.to_str())
        i = i + 1
    }
    println("held=" + held.len().to_str())
    exit(0)
    return 0
}
"""
    hold = write_probe("hold.hls", HOLD)
    h_small = interpose_run(hold, ["3"]) or {}
    h_big = interpose_run(hold, ["53"]) or {}
    d_live = (h_big.get("LIVE", 0) - h_small.get("LIVE", 0))
    check(d_live == 100,
          "an in-scope exit(0) holds exactly its payloads at exit "
          "(LIVE delta %d for 50 more payloads — two heap blocks per "
          "str: the hl_str and its data)" % d_live)
else:
    print("  [SKIP] native half (bin/hlc or gcc missing)")

# ===========================================================================
print("Section 5 — the report layout and the JSON payload")

p = run(RSS + ["--cycles", "20", "--warmup", "5", "--bootstrap", "200",
               CHURN])
for needle, why in (
    ("hls-rss 0.143.0-alpha", "the header names the tool and version"),
    ("plan: warmup 5 + measured 20 cycles", "the plan line"),
    ("timeline (external sampler, /proc/<pid>/statm)", "the timeline block"),
    ("slope:", "the slope line"),
    ("threshold:", "the threshold line"),
    ("retained (two-run, malloc interposer)", "the retained block"),
    ("live delta:", "the live delta line"),
    ("RSS STABLE", "the composed verdict"),
):
    check(needle in p.stdout, why)
check(p.returncode == 0, "the churn lib certifies STABLE (exit 0)")
jp = os.path.join(TMP, "churn.json")
p = run(RSS + ["--cycles", "20", "--warmup", "5", "--bootstrap", "200",
               "--json", jp, CHURN])
blob = json.load(open(jp))
check(blob["verdict"] == "stable" and blob["tool"] == "hls-rss",
      "the JSON payload carries the tool and the composed verdict")
check(blob["slope"]["ci"][0] <= blob["slope"]["slope"]
      <= blob["slope"]["ci"][1] or blob["slope"]["slope"] == 0,
      "the slope sits inside its own CI (or is exactly zero)")
check("timeline" in blob["run_a"] and len(blob["run_a"]["timeline"]) > 0,
      "the raw ticker trace ships in the payload")
check(blob["retained"]["live_delta"] == 0,
      "the payload carries the exact-free live delta (0)")
check(blob["config"]["cycles"] == 20 and blob["config"]["warmup"] == 5,
      "the payload echoes the plan it ran")

# ===========================================================================
print("Section 6 — the two-run comparison and the honest skips")

p = run(RSS + ["--cycles", "20", "--warmup", "5", "--bootstrap", "200",
               CHURN])
check("live delta:        0 objects (slack 0)" in p.stdout,
      "the churn lib's two runs hold the same live set (delta 0)")
check("the retained set is independent of the work volume" in p.stdout,
      "the retained verdict states the exact-free property")

# A workload that ignores argv[1]: the retained instrument skips
# itself, honestly, and the timeline stands alone.
FIXEDCYC = """fn main() -> int uses IO {
    let mut i: int = 0
    while i < 24 {
        let s: str = "tick-" + i.to_str()
        println(s)
        println("__HLRSS_CYCLE__ " + i.to_str())
        i = i + 1
    }
    return 0
}
"""
p = run(RSS + ["--cycles", "40", "--warmup", "8",
               write_probe("fixedcyc.hls", FIXEDCYC)])
check("does not follow the argv[1] cycle convention" in p.stdout,
      "an argv-ignoring workload gets the honest retained skip")
check(p.returncode in (0, 1),
      "the run itself is judged by its timeline alone")

# --no-malloc-count: the instrument is off, the skip says why.
p = run(RSS + ["--cycles", "20", "--warmup", "5", "--bootstrap", "200",
               "--no-malloc-count", CHURN])
check("skipped" in p.stdout and "RSS STABLE" in p.stdout,
      "--no-malloc-count skips the balance and the timeline certifies "
      "alone")

# ===========================================================================
print("Section 7 — the differential and the leak matrix")

if NATIVE:
    a = run(BOOT + [FIXTURE])
    c2 = os.path.join(TMP, "fixture.c")
    b2 = os.path.join(TMP, "fixture.bin")
    r = run([HLC, FIXTURE, c2])
    r2 = run([CC, "-O2", "-o", b2, c2, "-lm", "-pthread"])
    if r.returncode == 0 and r2.returncode == 0:
        b = run([b2])
        check(a.stdout == b.stdout and a.returncode == b.returncode,
              "the ok fixture is byte-identical on interpreter and "
              "native")
    else:
        check(False, "the fixture compiles natively")

    # The leak matrix: churn STABLE, server STABLE, canary LEAK with
    # BOTH instruments firing.
    p = run(RSS + ["--cycles", "20", "--warmup", "5",
                   "--bootstrap", "200", SERVER])
    check(p.returncode == 0 and "RSS STABLE" in p.stdout,
          "the server lib (json parse + stringify per request) "
          "certifies STABLE")
    p = run(RSS + ["--cycles", "40", "--warmup", "10",
                   "--bootstrap", "200", CANARY])
    both = ("slope CI entirely above the threshold" in p.stdout
            and "more live objects at exit" in p.stdout)
    check(p.returncode == 1 and "RSS LEAK" in p.stdout and both,
          "the canary earns LEAK with both instruments firing")

    # The hoisting-cycle regression: the Stage 121 corpus shape (a
    # match inside a concat inside a call argument) — infinite
    # mutual recursion between hoist_all and gen_expr at Stage 124
    # and every earlier release; fixed in Stage 125's two-phase
    # hoist_all. Compile, run, and balance-check it.
    MATCHCONCAT = """enum Kind {
    Food(int),
    Tool(int)
}

fn main() uses IO {
    let k: Kind = Kind.Food(1)
    println("kind " +
    match k {
        Kind.Food(_) => "food",
        Kind.Tool(_) => "tool"
    })
    return
}
"""
    mc = write_probe("matchconcat.hls", MATCHCONCAT)
    r = run([HLC, mc, os.path.join(TMP, "mc.c")])
    if r.returncode == 0:
        r2 = run([CC, "-O2", "-o", os.path.join(TMP, "mc.bin"),
                  os.path.join(TMP, "mc.c"), WRAP, "-lm", "-pthread",
                  "-Wl,--wrap=malloc", "-Wl,--wrap=calloc",
                  "-Wl,--wrap=realloc", "-Wl,--wrap=free"])
        if r2.returncode == 0:
            rr = run([os.path.join(TMP, "mc.bin")])
            cnt = interpose_run(mc, [])
            check(rr.stdout.strip() == "kind food",
                  "the match-in-concat program runs and prints its "
                  "answer")
            check(cnt is not None and cnt["LIVE"] <= 32,
                  "the match-in-concat program leaks nothing "
                  "(LIVE=%s)" % (cnt or {}).get("LIVE"))
        else:
            check(False, "the match-in-concat program links")
    else:
        check(False, "the match-in-concat program compiles (the "
                     "hoisting-cycle fix)")
else:
    print("  [SKIP] native half (bin/hlc or gcc missing)")

# ===========================================================================
print("Section 8 — corpus hygiene")

for path in (FIXTURE, CHURN, SERVER, CANARY):
    p = run([sys.executable, "tools/hlfmt.py", "-c", path])
    check("already formatted" in p.stdout,
          "%s is hlfmt-canonical" % os.path.basename(path))
p = run([sys.executable, "tools/hllint.py", FIXTURE])
check("no warnings" in p.stdout, "the fixture is hllint-clean")
p = run(BOOT + ["--check", FIXTURE])
check("OK: types and effects valid" in p.stdout,
      "the fixture boots through the real checker")

litter = []
for d in (os.path.join(ROOT, "tests"),
          os.path.join(ROOT, "tests", "rss_libs")):
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if os.path.isfile(p) and (f.startswith("hlrss-")
                                  or f.startswith("_gate_s125_")):
            litter.append(f)
check(not litter, "no gate temp files survive anywhere (%s)"
      % (", ".join(litter) or "clean"))

# ===========================================================================
shutil.rmtree(TMP, ignore_errors=True)
print()
if FAIL == 0:
    print("ACCEPTANCE OK: Stage 125 — hls-rss (%d checks)" % PASS)
    sys.exit(0)
print("ACCEPTANCE FAILED: %d of %d checks failed" % (FAIL, PASS + FAIL))
sys.exit(1)
