#!/usr/bin/env python3
"""Stage 136 acceptance gate — the per-commit performance regression
dashboard (v0.154.0-alpha).

Run with `make perf-acceptance` (or `python3 tests/perf_acceptance.py`).

Eight sections over the REAL hls-perf CLI, the REAL hls-bench runner,
the REAL history ledger and (when bin/hlc exists) the REAL native
compiler — a dashboard that did not measure through the instrument it
judges with would be a mock, not a gate:

  1. the convention  — hls-perf exits 0 verified / 1 named violation /
     2 usage; a bare invocation and an unknown command are usage
     errors; history/gate/dashboard on a missing or empty ledger are
     named errors, never silent renders

  2. the record law  — `record` runs the REAL bench suite through the
     REAL runner and appends exactly one entry pinned to `git
     rev-parse HEAD` (backend, suite digest, plan, env fingerprint, 5
     benches with >= 2 positive samples each); the ledger is
     append-only (the bytes before are a byte prefix of the bytes
     after); a record without git and without --commit is refused; a
     bad --commit sha is a usage error; a work tree with tracked
     modifications is REFUSED — a per-commit record pins a clean
     commit (proven on a throwaway clone)

  3. the floor law   — the report import refuses a bench with fewer
     than 2 samples ("a median no sample supports is not a
     measurement"), non-positive estimates, a FAILED bench, zero
     measured benches, a foreign report, an unknown backend — every
     refusal naming its bench

  4. the classification law — against controlled ledgers (synthetic
     reports, explicit --commit pins, the seeded statistics): identical
     samples classify no-change, +40% classifies regressed (exit 1,
     named), -40% improved, a significant +2% shift inside the 5% band
     classifies within-noise; a bench that appears is new, one that
     disappears is vanished and fails the gate by name

  5. the gate law    — a regressed bench fails the gate naming it; a
     written sanction flips it loud-but-green; a sanction without a
     reason is refused; an unknown bench is refused naming the known
     ones; --baseline resolves a prefix, refuses an unknown one and an
     ambiguous one; interpreter vs native are different instruments
     (refused, both by default and by explicit --baseline); a changed
     suite digest refuses comparison; --threshold moves the band

  6. the determinism law — two dashboards of one ledger are
     byte-identical; --stamp changes ONLY the Generated line; the
     document embeds the sha256 of the ledger bytes and the newest
     commit; the verdict table rows are sorted; a constant trend
     renders the mid band; a first-record ledger renders with an
     n/a verdict instead of crashing

  7. the ledger law  — a corrupt line, an unknown entry type, a record
     without its commit pin, a record without a suite digest and a
     sanction without a written reason are all NAMED refusals naming
     the line; history --bench filters records and sanctions alike

  8. the corpus      — tests/ok/feat_stage136_perf.hls is
     showcase        hlfmt-canonical, hllint-clean, boots through
                     the real checker, runs green on the interpreter
                     and (when bin/hlc exists) matches natively
                     byte for byte

Exit code 0 = all acceptance criteria met.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PERF = [sys.executable, os.path.join(ROOT, "tools", "hls-perf.py")]
BOOT = [sys.executable, os.path.join(ROOT, "boot", "boot.py")]
FMT = [sys.executable, os.path.join(ROOT, "tools", "hlfmt.py")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok",
                        "feat_stage136_perf.hls")
SHOWCASE_OUT = ("stage 136: hls-perf\n"
                "  PERF OK: 5 benches tracked, classified, sanctioned, "
                "pinned\n")

SUITE = "benchmarks/regression_watch.hls"
SUITE_DIGEST = hashlib.sha256(
    open(os.path.join(ROOT, SUITE), "rb").read()).hexdigest()

GATE = tempfile.mkdtemp(prefix=".perf-gate-", dir=ROOT)

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print("  ok: " + msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  FAIL: " + msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def run(args, **kw):
    return subprocess.run(args, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True,
                          timeout=900, **kw)


def hist_path(name):
    return os.path.join(GATE, name + ".jsonl")


# ---------------------------------------------------------------------------
# Synthetic hls-bench reports — the schema write_json emits, filled
# with controlled samples. samples are ns/iter values; the report
# carries them as raw (iters, elapsed) pairs, exactly as the runner
# writes them.
# ---------------------------------------------------------------------------

def synth_bench(name, samples_ns, status="ok", with_samples=True):
    b = {"name": name, "status": status,
         "units": 400,
         "samples_count": len(samples_ns) if status == "ok" else 0,
         "estimates_ns_per_iter": {
             "median": sorted(samples_ns)[len(samples_ns) // 2]
             if samples_ns else 0.0}}
    if with_samples:
        b["samples"] = [[400, 400 * v] for v in samples_ns]
    if status != "ok":
        b["detail"] = "synthetic %s" % status
        b["samples_count"] = 0
    return b


def synth_report(benches, backend="interpreter"):
    return {
        "tool": "hls-bench",
        "version": "0.142.0",
        "backend": backend,
        "plan": {"mode": "fixed", "samples": 10, "iters": 400},
        "noise_pct": 5.0,
        "files": [{"file": SUITE, "benches": benches}],
    }


def write_report(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=1) + "\n")
    return path


def record_from(report_obj, history, commit, extra=None):
    rp = write_report(os.path.join(GATE, "in.json"), report_obj)
    cmd = PERF + ["record", "--from-report", rp,
                  "--commit", commit, "--history", history]
    if extra:
        cmd += extra
    return run(cmd)


BASE = [1000, 1001, 1000, 999, 1000, 1001, 1000, 999, 1000, 1000]


def scaled(f):
    return [v * f for v in BASE]


def sha(n):
    return "%040x" % n


# ===========================================================================
def _section_1():
    print("Section 1 — the convention")

    p = run(PERF)
    check(p.returncode == 2, "a bare invocation is usage error 2")

    p = run(PERF + ["warp"])
    check(p.returncode == 2, "an unknown command is usage error 2")

    p = run(PERF + ["history", "--history", hist_path("absent")])
    check(p.returncode == 2 and "does not exist" in p.stderr,
          "history over a missing ledger is a named usage error")

    p = run(PERF + ["gate", "--history", hist_path("absent")])
    check(p.returncode == 2 and "does not exist" in p.stderr,
          "gate over a missing ledger is a named usage error")

    p = run(PERF + ["dashboard", "--history", hist_path("absent"),
                    "--out", os.path.join(GATE, "x.md")])
    check(p.returncode == 2 and "does not exist" in p.stderr,
          "dashboard over a missing ledger is a named usage error")

    p = run(PERF + ["dashboard", "--out", os.path.join(GATE, "x.md")])
    check(p.returncode == 2, "a dashboard without --out is usage "
          "error 2")

    empty = hist_path("empty")
    open(empty, "w").close()
    p = run(PERF + ["gate", "--history", empty])
    check(p.returncode == 2 and "no records" in p.stderr,
          "gate over an empty ledger refuses, named")
    p = run(PERF + ["dashboard", "--history", empty,
                    "--out", os.path.join(GATE, "never.md")])
    check(p.returncode == 2 and "no records" in p.stderr
          and not os.path.exists(os.path.join(GATE, "never.md")),
          "dashboard over an empty ledger refuses and writes nothing")


# ===========================================================================
def _section_2():
    print("Section 2 — the record law")

    h = hist_path("real")
    p = run(PERF + ["record", "--history", h,
                    "--samples", "5", "--fixed-iters", "100"])
    check(p.returncode == 0 and "recorded" in p.stdout,
          "record runs the real suite through the real runner")
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    check(("recorded %s" % head[:10]) in p.stdout,
          "the record is pinned to the live HEAD")

    p = run(PERF + ["history", "--history", h, "--json"])
    entries = json.loads(p.stdout)
    recs = [e for e in entries if e["type"] == "record"]
    check(len(recs) == 1, "exactly one record was appended")
    r = recs[0]
    check(r["commit"] == head, "the record carries the live HEAD sha")
    check(r["backend"] == "interpreter", "the backend is the "
          "interpreter instrument")
    check(r["suite_digest"] == SUITE_DIGEST,
          "the suite digest is the sha256 of the tracked suite")
    check(r["plan"] == {"mode": "fixed", "samples": 5, "iters": 100},
          "the measurement plan is pinned in the record")
    check(len(r["benches"]) == 5, "all 5 tracked benches measured")
    check(all(len(b["estimates_ns"]) >= 2
              and all(x > 0 for x in b["estimates_ns"])
              for b in r["benches"].values()),
          "every bench carries >= 2 positive samples")
    check("python" in r["env"] and "cc" in r["env"],
          "the env fingerprint is recorded")

    before = open(h, "rb").read()
    p = run(PERF + ["record", "--history", h,
                    "--samples", "5", "--fixed-iters", "100"])
    after = open(h, "rb").read()
    check(p.returncode == 0 and after.startswith(before),
          "the ledger is append-only: the bytes before are a prefix "
          "of the bytes after")

    nogit = os.path.join(GATE, "nogit")
    os.makedirs(nogit, exist_ok=True)
    rp = write_report(os.path.join(GATE, "pin.json"),
                      synth_report([synth_bench("bench_watched",
                                                BASE)]))
    # GIT_CEILING_DIRECTORIES stops git's upward walk at the gate dir:
    # inside `nogit` there is no repository, exactly as in a bare CI
    # checkout.
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=GATE)
    p = run(PERF + ["record", "--from-report", rp, "--root", nogit,
                    "--history", hist_path("pin")], env=env)
    check(p.returncode == 2 and "not a git work tree" in p.stderr,
          "a record without git and without --commit is refused")

    p = run(PERF + ["record", "--from-report", rp, "--commit", "xyz",
                    "--history", hist_path("pin")])
    check(p.returncode == 2 and "40-hex" in p.stderr,
          "a bad --commit sha is a usage error")

    clone = os.path.join(GATE, "clone")
    c = run(["git", "clone", "-q", ROOT, clone])
    if c.returncode != 0:
        check(False, "git clone for the dirty-tree law: %s"
              % c.stderr.strip()[:120])
    else:
        with open(os.path.join(clone, "README.md"), "a",
                  encoding="utf-8") as fh:
            fh.write("\n# dirt\n")
        p = run(PERF + ["record", "--root", clone,
                        "--history", hist_path("dirty")])
        check(p.returncode == 1
              and "tracked modifications" in p.stderr,
              "a work tree with tracked modifications is refused — "
              "a per-commit record pins a clean commit")


# ===========================================================================
def _section_3():
    print("Section 3 — the floor law")

    h = hist_path("floor")
    cases = [
        (synth_report([synth_bench("bench_watched", [1000])]),
         1, "samples", "a one-sample bench is refused"),
        (synth_report([synth_bench("bench_watched", [])]),
         1, "samples", "a zero-sample bench is refused"),
        (synth_report([synth_bench("bench_watched",
                                   [1000, 1002, 0, 1001])]),
         1, "non-positive", "a zero estimate is refused"),
        (synth_report([synth_bench("bench_watched",
                                   [1000, 1002, -5, 1001])]),
         1, "non-positive", "a negative estimate is refused"),
        ({"tool": "hls-bench", "backend": "interpreter",
          "files": [{"file": SUITE, "benches": [
              {"name": "bench_watched", "status": "skip",
               "detail": "no"}]}]},
         2, "no measured benches", "a report with zero measured "
         "benches is refused"),
        ({"tool": "hls-bench", "backend": "interpreter",
          "files": [{"file": SUITE, "benches": [
              {"name": "bench_watched", "status": "fail",
               "detail": "boom"}]}]},
         1, "failed", "a FAILED bench fails the record"),
        ({"tool": "not-bench", "backend": "interpreter",
          "files": []},
         2, "not an hls-bench report", "a foreign report is refused"),
        (synth_report([synth_bench("bench_watched", BASE)]),
         2, "backend", None),
    ]
    # (the last case is patched below — an unknown backend)
    cases[-1] = ({"tool": "hls-bench", "backend": "gpu",
                  "files": [{"file": SUITE, "benches": []}]},
                 2, "backend", "an unknown backend is refused")

    for i, (obj, rc, needle, label) in enumerate(cases):
        if label is None:
            continue
        p = record_from(obj, h, sha(i + 1))
        check(p.returncode == rc and needle in p.stderr,
              label + " (named)")

    # A report whose bench carries a median but no raw samples still
    # cannot slip past the floor: the median alone is one number, not
    # a distribution.
    obj = synth_report([synth_bench("bench_watched", BASE,
                                    with_samples=False)])
    p = record_from(obj, h, sha(99))
    check(p.returncode == 1 and "samples" in p.stderr,
          "a median without raw samples is not a distribution — "
          "refused")


# ===========================================================================
def _section_4():
    print("Section 4 — the classification law")

    # Four single-bench ledgers, each one baseline + one case record:
    # the gate classifies the pair and only the pair.
    cases = [
        ("identical", BASE, BASE, "no-change", 0),
        ("regressed", BASE, scaled(1.4), "regressed", 1),
        ("improved", BASE, scaled(0.6), "improved", 0),
        ("within-noise", BASE, [1020, 1021, 1020, 1019, 1020, 1021,
                                1020, 1019, 1020, 1020],
         "within-noise", 0),
    ]
    for name, base, cur, klass, want_rc in cases:
        h = hist_path("cls-" + name)
        r1 = record_from(synth_report([synth_bench("bench_watched",
                                                   base)]),
                         h, sha(1))
        r2 = record_from(synth_report([synth_bench("bench_watched",
                                                   cur)]),
                         h, sha(2))
        if r1.returncode != 0 or r2.returncode != 0:
            check(False, "%s: both records filed (%s / %s)"
                  % (name, r1.stderr.strip()[:80],
                     r2.stderr.strip()[:80]))
            continue
        jp = os.path.join(GATE, name + ".verdict.json")
        p = run(PERF + ["gate", "--history", h, "--json", jp])
        v = json.load(open(jp))
        row = v["benches"][0]
        check(row["class"] == klass,
              "%s samples classify %s" % (name, klass))
        check(p.returncode == want_rc,
              "%s: gate exit %d" % (name, want_rc))
        if klass == "regressed":
            check("GATE FAIL" in p.stdout
                  and "bench_watched" in p.stdout,
                  "a regression is named, not summarised")
        if klass == "no-change":
            check(abs(row["point_pct"]) < 1e-9 and row["p"] >= 0.05,
                  "identical samples: point 0%%, p not significant")

    # new / vanished — the rename law: a bench that appears is new
    # (reported, never failed), one that disappears is vanished and
    # fails BY NAME (silently losing a bench would hide regressions).
    h = hist_path("cls-vanish")
    record_from(synth_report([
        synth_bench("bench_watched", BASE),
        synth_bench("bench_other", BASE)]), h, sha(1))
    record_from(synth_report([
        synth_bench("bench_watched", BASE),
        synth_bench("bench_third", BASE)]), h, sha(2))
    jp = os.path.join(GATE, "vanish.verdict.json")
    p = run(PERF + ["gate", "--history", h, "--json", jp])
    v = json.load(open(jp))
    classes = {r["bench"]: r["class"] for r in v["benches"]}
    check(classes == {"bench_watched": "no-change",
                      "bench_other": "vanished",
                      "bench_third": "new"},
          "the rename law: new is reported, vanished fails")
    check(p.returncode == 1 and "bench_other" in p.stdout,
          "a vanished bench fails the gate by name")


# ===========================================================================
def _section_5():
    print("Section 5 — the gate law")

    # The regressed ledger from section 4, replayed with its sanctions.
    h = hist_path("gate")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h, sha(1))
    record_from(synth_report([synth_bench("bench_watched",
                                          scaled(1.4))]),
                h, sha(2))
    p = run(PERF + ["gate", "--history", h])
    check(p.returncode == 1 and "bench_watched" in p.stdout,
          "an unsanctioned regression fails the gate, named")

    p = run(PERF + ["sanction", "bench_nope", "--reason", "x",
                    "--history", h])
    check(p.returncode == 2 and "unknown bench" in p.stderr
          and "bench_watched" in p.stderr,
          "a sanction for an unknown bench is refused naming the "
          "known ones")

    p = run(PERF + ["sanction", "bench_watched", "--history", h])
    check(p.returncode == 2 and "reason" in p.stderr,
          "a sanction without a written reason is refused")

    p = run(PERF + ["sanction", "bench_watched",
                    "--reason", "switched to a correct algorithm; "
                                "the constant factor was the price",
                    "--history", h])
    check(p.returncode == 0, "the sanction is filed")

    jp = os.path.join(GATE, "gate.verdict.json")
    p = run(PERF + ["gate", "--history", h, "--json", jp])
    v = json.load(open(jp))
    check(p.returncode == 0 and v["verdict"] == "pass",
          "a sanctioned regression is loud but green")
    check(v["sanctioned"] == ["bench_watched"],
          "the verdict carries the sanctioned bench")
    check("sanctioned" in p.stdout and "correct algorithm" in p.stdout,
          "the gate output carries the written reason")

    # --baseline: prefix resolution, unknown, ambiguous.
    p = run(PERF + ["gate", "--history", h, "--baseline", "deadbeef"])
    check(p.returncode == 2 and "no record carries" in p.stderr,
          "an unknown --baseline prefix is a named usage error")

    h_amb = hist_path("ambiguous")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h_amb, sha(7))
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h_amb, sha(7))
    p = run(PERF + ["gate", "--history", h_amb,
                    "--baseline", sha(7)])
    check(p.returncode == 2 and "ambiguous" in p.stderr,
          "an ambiguous commit prefix is refused")

    p = run(PERF + ["gate", "--history", h_amb,
                    "--baseline", sha(7)[:12]])
    check(p.returncode == 2 and "ambiguous" in p.stderr,
          "prefix resolution is by prefix, and ambiguity is found "
          "there too")

    # The instrument law: interpreter and native are different
    # instruments; the ledger never compares across backends.
    h_inst = hist_path("instrument")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h_inst, sha(1))
    record_from(synth_report([synth_bench("bench_watched", BASE)],
                             backend="native"),
                h_inst, sha(3))
    p = run(PERF + ["gate", "--history", h_inst])
    check(p.returncode == 2
          and "no earlier record of the same instrument" in p.stderr,
          "a native record with no native baseline refuses by default")
    p = run(PERF + ["gate", "--history", h_inst, "--baseline", sha(1)])
    check(p.returncode == 1 and "different instruments" in p.stderr,
          "an explicit cross-backend baseline is refused, named")

    # The suite law: a changed suite digest refuses comparison.
    h_suite = hist_path("suite")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h_suite, sha(1))
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h_suite, sha(2),
                extra=["--suite", "benchmarks/sieve.hls"])
    p = run(PERF + ["gate", "--history", h_suite, "--baseline", sha(1)])
    check(p.returncode == 1 and "suite changed" in p.stderr,
          "a changed suite digest refuses comparison — re-baseline, "
          "do not compare")

    # --threshold moves the band: +40% inside a 60% band is within.
    h_t = hist_path("threshold")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h_t, sha(1))
    record_from(synth_report([synth_bench("bench_watched",
                                          scaled(1.4))]),
                h_t, sha(2))
    p = run(PERF + ["gate", "--history", h_t, "--threshold", "60"])
    check(p.returncode == 0 and "within-noise" in p.stdout,
          "--threshold moves the noise band")


# ===========================================================================
def _section_6():
    print("Section 6 — the determinism law")

    h = hist_path("gate")
    d1 = os.path.join(GATE, "dash1.md")
    d2 = os.path.join(GATE, "dash2.md")
    p1 = run(PERF + ["dashboard", "--history", h, "--out", d1])
    check(p1.returncode == 0 and os.path.isfile(d1),
          "the dashboard renders")
    run(PERF + ["dashboard", "--history", h, "--out", d2])
    with open(d1, encoding="utf-8") as fh:
        doc1 = fh.read()
    with open(d2, encoding="utf-8") as fh:
        doc2 = fh.read()
    check(doc1 == doc2, "two dashboards of one ledger are "
          "byte-identical")

    digest = hashlib.sha256(open(h, "rb").read()).hexdigest()
    check(("sha256 `%s`" % digest) in doc1,
          "the document embeds the sha256 of the ledger bytes")
    check(("`%s`" % sha(2)) in doc1,
          "the newest record's commit is embedded")

    d3 = os.path.join(GATE, "dash3.md")
    run(PERF + ["dashboard", "--history", h, "--out", d3, "--stamp"])
    with open(d3, encoding="utf-8") as fh:
        doc3 = fh.read()
    l1 = [ln for ln in doc1.splitlines() if ln]
    l3 = [ln for ln in doc3.splitlines() if ln
          and not ln.startswith("- Generated: ")]
    check(l1 == l3 and "- Generated: " in doc3,
          "--stamp changes ONLY the Generated line")

    # The verdict table rows are sorted by bench name.
    jp = os.path.join(GATE, "dash.json")
    p = run(PERF + ["dashboard", "--history", h, "--out", d1,
                    "--json", jp])
    v = json.load(open(jp))
    names = [r["bench"] for r in v["benches"]]
    check(names == sorted(names), "the verdict rows are sorted")
    check(v["verdict"] == "pass", "the sanctioned ledger gates pass")

    # A constant trend renders the mid band, not a wall.
    h_flat = hist_path("flat")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h_flat, sha(1))
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h_flat, sha(2))
    p = run(PERF + ["dashboard", "--history", h_flat,
                    "--out", os.path.join(GATE, "flat.md")])
    with open(os.path.join(GATE, "flat.md"), encoding="utf-8") as fh:
        flat = fh.read()
    check("\u2584" in flat and "\u2588" not in flat,
          "a constant trend renders mid bands — no fake wall")

    # The first record of an instrument renders with an n/a verdict.
    h_one = hist_path("first")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h_one, sha(1))
    p = run(PERF + ["dashboard", "--history", h_one,
                    "--out", os.path.join(GATE, "first.md")])
    with open(os.path.join(GATE, "first.md"), encoding="utf-8") as fh:
        first = fh.read()
    check(p.returncode == 0 and "n/a" in first
          and "No comparison yet" in first,
          "a first-record dashboard renders with an n/a verdict")


# ===========================================================================
def _section_7():
    print("Section 7 — the ledger law")

    def corrupt_entry(h, line, needle, label):
        with open(h, "a", encoding="utf-8") as fh:
            fh.write(line)
        p = run(PERF + ["history", "--history", h])
        check(p.returncode == 1 and needle in p.stderr, label)

    h = hist_path("corrupt")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h, sha(1))
    corrupt_entry(h, "this is not json\n", "line 2 is corrupt",
                  "a corrupt line is a named error naming the line")

    h = hist_path("badtype")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h, sha(1))
    corrupt_entry(h, '{"type": "quantum"}\n', "unknown type",
                  "an unknown entry type is a named error")

    h = hist_path("nopin")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h, sha(1))
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": "cafe",
                                 "backend": "interpreter",
                                 "suite_digest": SUITE_DIGEST,
                                 "benches": {"b": {
                                     "estimates_ns": [1, 2]}}}) + "\n",
                  "commit pin", "a record without a 40-hex commit pin "
                  "is a named error")

    h = hist_path("nodigest")
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": sha(5),
                                 "backend": "interpreter",
                                 "benches": {"b": {
                                     "estimates_ns": [1, 2]}}}) + "\n",
                  "suite digest", "a record without a suite digest "
                  "is a named error")

    h = hist_path("noreason")
    corrupt_entry(h, json.dumps({"type": "sanction",
                                 "bench": "bench_watched"}) + "\n",
                  "reason", "a sanction without a written reason is a "
                  "named error")

    # history --bench filters records and sanctions alike.
    h = hist_path("filter")
    record_from(synth_report([synth_bench("bench_watched", BASE)]),
                h, sha(1))
    record_from(synth_report([synth_bench("bench_other", BASE)]),
                h, sha(2))
    run(PERF + ["sanction", "bench_watched", "--reason", "later",
                "--history", h])
    p = run(PERF + ["history", "--history", h, "--bench",
                    "bench_watched", "--json"])
    entries = json.loads(p.stdout)
    check(len(entries) == 2
          and entries[0]["type"] == "record"
          and entries[1]["type"] == "sanction",
          "history --bench keeps exactly the entries that touch the "
          "bench (records and sanctions)")


# ===========================================================================
def _section_8():
    print("Section 8 — the corpus showcase")

    p = run(BOOT + ["--check", SHOWCASE])
    check(p.returncode == 0, "the showcase boots through the real "
          "checker")

    p = run(BOOT + [SHOWCASE])
    check(p.returncode == 0 and p.stdout == SHOWCASE_OUT,
          "the showcase runs green on the interpreter, exact output")

    p = run(FMT + ["--check", SHOWCASE])
    check(p.returncode == 0, "the showcase is hlfmt-canonical")

    p = run(LINT + [SHOWCASE])
    check(p.returncode == 0 and "no warnings" in p.stdout,
          "the showcase is hllint-clean")

    if NATIVE:
        c_path = os.path.join(GATE, "showcase.c")
        bin_path = os.path.join(GATE, "showcase_bin")
        r1 = run([HLC, SHOWCASE, c_path])
        if r1.returncode != 0:
            check(False, "hlc compiled the showcase: " +
                  r1.stdout.strip()[:120])
        else:
            r2 = run([CC, "-O2", "-o", bin_path, c_path, "-lm",
                      "-pthread"])
            if r2.returncode != 0:
                check(False, "gcc compiled the showcase: " +
                      r2.stdout.strip()[:120])
            else:
                rn = run([bin_path])
                check(rn.returncode == 0 and rn.stdout == SHOWCASE_OUT,
                      "the showcase matches natively, byte for byte")
    else:
        print("  (bin/hlc or cc not available — native differential "
              "skipped, interpreter legs ran)")


# ===========================================================================
def main():
    try:
        _section_1()
        _section_2()
        _section_3()
        _section_4()
        _section_5()
        _section_6()
        _section_7()
        _section_8()
    finally:
        shutil.rmtree(GATE, ignore_errors=True)
    print("")
    print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
