#!/usr/bin/env python3
"""Stage 137 acceptance gate — the memory regression dashboard, RSS
over time (v0.155.0-alpha).

Run with `make memdash-acceptance` (or `python3
tests/memdash_acceptance.py`).

Eight sections over the REAL hls-mem CLI, the REAL hls-rss
instrument, the REAL tracked workload suite and (when bin/hlc
exists) the REAL native compiler — a dashboard that did not measure
through the instrument it judges with would be a mock, not a gate:

  1. the convention  — hls-mem exits 0 verified / 1 named violation /
     2 usage; a bare invocation and an unknown command are usage
     errors; history/gate/dashboard on a missing or empty ledger are
     named errors, never silent renders

  2. the record law  — `record` runs the REAL hls-rss over the REAL
     tracked suite (every workload must certify STABLE — the floor
     law) and appends exactly one entry pinned to `git rev-parse
     HEAD` (suite digest over names AND bytes, pinned plan, env
     fingerprint with the page size, 4 workloads with >= 2 positive
     samples each); the ledger is append-only (the bytes before are
     a byte prefix of the bytes after); the --json artifact is the
     measurement bundle and can be RE-FILED as a record; a record
     without git and without --commit is refused; a bad --commit sha
     is a usage error; a work tree with tracked modifications is
     REFUSED — a per-commit record pins a clean commit (proven on a
     throwaway clone)

  3. the floor law   — the bundle import refuses a run that
     certified leak or inconclusive ("file a bug, not a record"), a
     workload with fewer than 2 samples, a workload with zero runs,
     a foreign report, a bundle that is not a bundle, a report with
     no traces and a stable report without its slope — every
     refusal naming its workload

  4. the            — against controlled ledgers (synthetic
     classification      bundles, explicit --commit pins, the seeded
     law                 statistics): identical samples classify
                         no-change; a ZERO-VARIANCE +40% classifies
                         regressed (the page law — Welch's t-test
                         answers p=1 on degenerate samples and
                         would call it no-change); a zero-variance
                         in-band shift classifies within-noise; -40%
                         improved; a workload that appears is new,
                         one that disappears is vanished and fails
                         the gate by name

  5. the gate law    — a regressed workload fails the gate naming
     it; a written sanction flips it loud-but-green; a sanction
     without a reason is refused; an unknown workload is refused
     naming the known ones; --baseline resolves a prefix, refuses
     an unknown one, an ambiguous one and the record itself; a
     different page size is a different instrument (refused by
     explicit baseline AND by default); a changed suite digest
     refuses comparison; --threshold and --floor both move the band

  6. the determinism law — two dashboards of one ledger are
     byte-identical; --stamp changes ONLY the Generated line; the
     document embeds the sha256 of the ledger bytes and the newest
     commit; the verdict table rows are sorted; both trend tables
     and the slope watch render; a constant trend renders the mid
     band; a first-record ledger renders with an n/a verdict
     instead of crashing

  7. the ledger law  — a corrupt line, an unknown entry type, a
     record without its commit pin, a record without its suite
     digest, a record without its page size, a record whose
     workload did not certify stable and a sanction without a
     written reason are all NAMED refusals naming the line;
     history --workload filters records and sanctions alike

  8. the corpus     — tests/ok/feat_stage137_mem.hls is
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

MEM = [sys.executable, os.path.join(ROOT, "tools", "hls-mem.py")]
BOOT = [sys.executable, os.path.join(ROOT, "tools", "hlinterp", "boot.py")]
FMT = [sys.executable, os.path.join(ROOT, "tools", "hlfmt.py")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok",
                        "feat_stage137_mem.hls")
SHOWCASE_OUT = ("stage 137: hls-mem\n"
                "  MEM OK: 4 workloads tracked, floored, classified, "
                "pinned\n")

SUITE = "benchmarks/rss_watch"
SUITE_DIR = os.path.join(ROOT, SUITE)


def suite_digest_of(dirpath):
    """The tool's own law: sha256 over the sorted (name, bytes)
    pairs — names included, so a rename is a change."""
    h = hashlib.sha256()
    for fn in sorted(os.listdir(dirpath)):
        if not fn.endswith(".hls"):
            continue
        h.update(fn[:-len(".hls")].encode("utf-8"))
        h.update(b"\0")
        with open(os.path.join(dirpath, fn), "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()


SUITE_DIGEST = suite_digest_of(SUITE_DIR)

GATE = tempfile.mkdtemp(prefix=".memdash-gate-", dir=ROOT)

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
# Synthetic hls-rss reports — the schema tools/hls-rss.py --json
# emits, filled with controlled readings. One report = one run of one
# workload; every cycle point sits at `hw` pages, so the marker
# median, the end RSS and the warmup-end RSS all read exactly `hw` —
# the zero-variance distribution the page law exists for.
# ---------------------------------------------------------------------------

def synth_rss(hw, verdict="stable", slope=0.0, tool="hls-rss",
              with_traces=True, with_slope=True):
    hw = int(hw)
    cps = [[i, float(i), hw] for i in range(50)] if with_traces else []
    timeline = [[0.5, hw]] if with_traces else []
    report = {
        "tool": tool,
        "version": "0.143.0-alpha",
        "workload": "watched",
        "verdict": verdict,
        "config": {"cycles": 40, "warmup": 10, "interval": 10.0,
                   "threshold": 0.25},
        "run_a": {"rc": 0, "observed_markers": 50,
                  "timeline": timeline, "cycle_points": cps},
        "slope": {"slope": slope,
                  "ci": [slope - 0.01, slope + 0.01],
                  "verdict": "stable", "unit": "page/cycle"},
    }
    if not with_slope:
        report["slope"] = {"verdict": "stable"}
    return report


def synth_bundle(hw_list, workload="watched", env=None, verdict="stable"):
    obj = {"tool": "hls-mem-bundle", "version": "0.155.0",
           "workloads": {workload: [synth_rss(v, verdict=verdict)
                                    for v in hw_list]}}
    if env is not None:
        obj["env"] = env
    return obj


def write_bundle(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=1) + "\n")
    return path


def record_from(bundle_obj, history, commit, extra=None, env=None):
    bp = write_bundle(os.path.join(GATE, "in.json"), bundle_obj)
    cmd = MEM + ["record", "--from-bundle", bp,
                 "--commit", commit, "--history", history]
    if extra:
        cmd += extra
    return run(cmd, env=env)


BASE = [200, 200, 200, 200, 200]


def scaled(f):
    return [v * f for v in BASE]


def sha(n):
    return "%040x" % n


# ===========================================================================
def _section_1():
    print("Section 1 — the convention")

    p = run(MEM)
    check(p.returncode == 2, "a bare invocation is usage error 2")

    p = run(MEM + ["warp"])
    check(p.returncode == 2, "an unknown command is usage error 2")

    p = run(MEM + ["history", "--history", hist_path("absent")])
    check(p.returncode == 2 and "does not exist" in p.stderr,
          "history over a missing ledger is a named usage error")

    p = run(MEM + ["gate", "--history", hist_path("absent")])
    check(p.returncode == 2 and "does not exist" in p.stderr,
          "gate over a missing ledger is a named usage error")

    p = run(MEM + ["dashboard", "--history", hist_path("absent"),
                   "--out", os.path.join(GATE, "x.md")])
    check(p.returncode == 2 and "does not exist" in p.stderr,
          "dashboard over a missing ledger is a named usage error")

    p = run(MEM + ["dashboard", "--out", os.path.join(GATE, "x.md")])
    check(p.returncode == 2, "a dashboard without --out is usage "
          "error 2")

    empty = hist_path("empty")
    open(empty, "w").close()
    p = run(MEM + ["gate", "--history", empty])
    check(p.returncode == 2 and "no records" in p.stderr,
          "gate over an empty ledger refuses, named")
    p = run(MEM + ["dashboard", "--history", empty,
                   "--out", os.path.join(GATE, "never.md")])
    check(p.returncode == 2 and "no records" in p.stderr
          and not os.path.exists(os.path.join(GATE, "never.md")),
          "dashboard over an empty ledger refuses and writes nothing")


# ===========================================================================
def _section_2():
    print("Section 2 — the record law")

    if not NATIVE:
        print("  (bin/hlc or cc not available — the real-measurement "
              "legs skipped; the import legs below still run)")
    else:
        # The pinning law refuses a dirty work tree, and the refusal
        # itself is proven further below on a throwaway clone. This
        # leg measures the REAL suite in the LIVE tree, so while the
        # tree carries in-flight development edits it runs under
        # --allow-dirty (a clean CI checkout passes no flag at all —
        # the record still pins the live HEAD either way).
        dirty = run(["git", "status", "--porcelain", "-uno"]).stdout
        dev = ["--allow-dirty"] if dirty.strip() else []
        h = hist_path("real")
        p = run(MEM + ["record", "--history", h,
                       "--samples", "2", "--cycles", "40",
                       "--warmup", "10"] + dev)
        check(p.returncode == 0 and "recorded" in p.stdout,
              "record runs the real suite through the real hls-rss")
        head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
        check(("recorded %s" % head[:10]) in p.stdout,
              "the record is pinned to the live HEAD")

        p = run(MEM + ["history", "--history", h, "--json"])
        entries = json.loads(p.stdout)
        recs = [e for e in entries if e["type"] == "record"]
        check(len(recs) == 1, "exactly one record was appended")
        r = recs[0]
        check(r["commit"] == head, "the record carries the live HEAD sha")
        check(r["suite"] == SUITE, "the record names the tracked suite")
        check(r["suite_digest"] == SUITE_DIGEST,
              "the suite digest covers workload names AND bytes")
        check(r["plan"] == {"cycles": 40, "warmup": 10,
                            "interval": 10.0, "samples": 2},
              "the measurement plan is pinned in the record")
        check(len(r["workloads"]) == 4,
              "all 4 tracked workloads measured")
        check(all(len(w["marker_rss_pages"]) >= 2
                  and all(x > 0 for x in w["marker_rss_pages"])
                  for w in r["workloads"].values()),
              "every workload carries >= 2 positive samples")
        check(all(w["hls_rss_verdict"] == "stable"
                  for w in r["workloads"].values()),
              "every workload certified STABLE under hls-rss")
        check(all(w.get("file", "").startswith(SUITE + "/")
                  and w.get("digest") for w in r["workloads"].values()),
              "every workload carries its tracked file and digest")
        check(r["env"].get("page_size", 0) > 0,
              "the env fingerprint carries the page size (the unit)")
        check(r["env"].get("aslr") in ("fixed", "random"),
              "the env fingerprint records the address-space mode "
              "(setarch fixed / random)")

        before = open(h, "rb").read()
        p = run(MEM + ["record", "--history", h,
                       "--samples", "2", "--cycles", "40",
                       "--warmup", "10"] + dev)
        after = open(h, "rb").read()
        check(p.returncode == 0 and after.startswith(before),
              "the ledger is append-only: the bytes before are a "
              "prefix of the bytes after")

    # The --json artifact is the measurement bundle, and the bundle
    # can be RE-FILED: what one machine files, another can file.
    h = hist_path("artifact")
    bp = write_bundle(os.path.join(GATE, "art.json"),
                      synth_bundle(BASE))
    jp = os.path.join(GATE, "art-out.json")
    p = run(MEM + ["record", "--from-bundle", bp,
                   "--commit", sha(31), "--history", h, "--json", jp])
    check(p.returncode == 0 and json.load(open(jp))["tool"]
          == "hls-mem-bundle",
          "record --json files the measurement bundle")
    p = run(MEM + ["record", "--from-bundle", jp,
                   "--commit", sha(32), "--history", h])
    check(p.returncode == 0,
          "the --json artifact re-files as a record (the round trip)")

    nogit = os.path.join(GATE, "nogit")
    os.makedirs(nogit, exist_ok=True)
    bp = write_bundle(os.path.join(GATE, "pin.json"),
                      synth_bundle(BASE))
    # GIT_CEILING_DIRECTORIES stops git's upward walk at the gate dir:
    # inside `nogit` there is no repository, exactly as in a bare CI
    # checkout.
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=GATE)
    p = run(MEM + ["record", "--from-bundle", bp, "--root", nogit,
                   "--history", hist_path("pin")], env=env)
    check(p.returncode == 2 and "not a git work tree" in p.stderr,
          "a record without git and without --commit is refused")

    p = run(MEM + ["record", "--from-bundle", bp, "--commit", "xyz",
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
        p = run(MEM + ["record", "--root", clone,
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
        (synth_bundle(BASE, verdict="leak"),
         1, "not stable", "a run that certified LEAK is refused"),
        (synth_bundle(BASE, verdict="inconclusive"),
         1, "not stable", "a run that certified INCONCLUSIVE is "
         "refused"),
        (synth_bundle([200]),
         1, "fewer than 2", "a one-sample workload is refused"),
        ({"tool": "hls-mem-bundle", "workloads": {"watched": []}},
         2, "no runs", "a workload with zero runs is refused"),
        ({"tool": "hls-mem-bundle", "workloads": {}},
         2, "no workloads", "a bundle with zero workloads is refused"),
        ({"tool": "not-mem", "workloads": {"watched": []}},
         2, "not an hls-mem-bundle", "a foreign bundle is refused"),
        ({"tool": "hls-mem-bundle",
          "workloads": {"watched": [dict(synth_rss(200),
                                          tool="not-rss")
                                     for _ in BASE]}},
         2, "not an hls-rss report", "a foreign report is refused"),
        ({"tool": "hls-mem-bundle", "workloads": {"watched":
         [synth_rss(200, with_traces=False)
          for _ in BASE]}},
         2, "no RSS readings", "a report with no traces is refused"),
        ({"tool": "hls-mem-bundle", "workloads": {"watched":
         [synth_rss(200, with_slope=False)
          for _ in BASE]}},
         2, "without its slope", "a stable report without its slope "
         "is refused"),
        ({"tool": "hls-mem-bundle", "env": {"page_size": 0},
          "workloads": {"watched": [synth_rss(200)
                                    for _ in BASE]}},
         2, "page_size", "a bundle env without a positive page size "
         "is refused"),
    ]
    for i, (obj, rc, needle, label) in enumerate(cases):
        p = record_from(obj, h, sha(i + 1))
        check(p.returncode == rc and needle in p.stderr,
              label + " (named)")


# ===========================================================================
def _section_4():
    print("Section 4 — the classification law (the page law)")

    # Controlled ledgers, each one baseline + one case record: the
    # gate classifies the pair and only the pair.
    cases = [
        ("identical", BASE, BASE, "no-change", 0),
        ("zero-var regressed", BASE, scaled(1.4), "regressed", 1),
        ("zero-var in-band", BASE, [210, 210, 210, 210, 210],
         "within-noise", 0),
        ("zero-var improved", BASE, scaled(0.6), "improved", 0),
        ("with-variance regressed", [200, 202, 198, 201, 199],
         [280, 282, 278, 281, 279], "regressed", 1),
    ]
    for name, base, cur, klass, want_rc in cases:
        h = hist_path("cls-" + name.replace(" ", "-"))
        r1 = record_from(synth_bundle(base), h, sha(1))
        r2 = record_from(synth_bundle(cur), h, sha(2))
        if r1.returncode != 0 or r2.returncode != 0:
            check(False, "%s: both records filed (%s / %s)"
                  % (name, r1.stderr.strip()[:80],
                     r2.stderr.strip()[:80]))
            continue
        jp = os.path.join(GATE, name.replace(" ", "-") + ".verdict.json")
        p = run(MEM + ["gate", "--history", h, "--json", jp])
        v = json.load(open(jp))
        row = v["workloads"][0]
        check(row["class"] == klass,
              "%s samples classify %s" % (name, klass))
        check(p.returncode == want_rc,
              "%s: gate exit %d" % (name, want_rc))
        if klass == "regressed":
            check("GATE FAIL" in p.stdout
                  and "watched" in p.stdout,
                  "a regression is named, not summarised")
        if klass == "no-change":
            check(row["point_pages"] == 0
                  and row["band_pages"] >= 16,
                  "identical samples: point 0 pages, band floored")
        if klass == "within-noise":
            check(row["ci_pages"][0] > 0,
                  "an in-band shift is still significant — the CI "
                  "excludes zero (the page law)")

    # new / vanished — the rename law: a workload that appears is
    # new (reported, never failed), one that disappears is vanished
    # and fails BY NAME (silently losing a canary would hide the
    # leak it was watching for).
    h = hist_path("cls-vanish")
    record_from({"tool": "hls-mem-bundle", "workloads": {
        "watched": [synth_rss(200) for _ in BASE],
        "other": [synth_rss(200) for _ in BASE]}}, h, sha(1))
    record_from({"tool": "hls-mem-bundle", "workloads": {
        "watched": [synth_rss(200) for _ in BASE],
        "third": [synth_rss(200) for _ in BASE]}}, h, sha(2))
    jp = os.path.join(GATE, "vanish.verdict.json")
    p = run(MEM + ["gate", "--history", h, "--json", jp])
    v = json.load(open(jp))
    classes = {r["workload"]: r["class"] for r in v["workloads"]}
    check(classes == {"watched": "no-change",
                      "other": "vanished",
                      "third": "new"},
          "the rename law: new is reported, vanished fails")
    check(p.returncode == 1 and "other" in p.stdout,
          "a vanished workload fails the gate by name")


# ===========================================================================
def _section_5():
    print("Section 5 — the gate law")

    # The regressed ledger, replayed with its sanctions.
    h = hist_path("gate")
    record_from(synth_bundle(BASE), h, sha(1))
    record_from(synth_bundle(scaled(1.4)), h, sha(2))
    p = run(MEM + ["gate", "--history", h])
    check(p.returncode == 1 and "watched" in p.stdout,
          "an unsanctioned regression fails the gate, named")

    p = run(MEM + ["sanction", "nope", "--reason", "x",
                   "--history", h])
    check(p.returncode == 2 and "unknown workload" in p.stderr
          and "watched" in p.stderr,
          "a sanction for an unknown workload is refused naming the "
          "known ones")

    p = run(MEM + ["sanction", "watched", "--history", h])
    check(p.returncode == 2 and "reason" in p.stderr,
          "a sanction without a written reason is refused")

    p = run(MEM + ["sanction", "watched",
                   "--reason", "the allocator now pre-sizes its bins; "
                               "the constant was the price",
                   "--history", h])
    check(p.returncode == 0, "the sanction is filed")

    jp = os.path.join(GATE, "gate.verdict.json")
    p = run(MEM + ["gate", "--history", h, "--json", jp])
    v = json.load(open(jp))
    check(p.returncode == 0 and v["verdict"] == "pass",
          "a sanctioned regression is loud but green")
    check(v["sanctioned"] == ["watched"],
          "the verdict carries the sanctioned workload")
    check("sanctioned" in p.stdout and "pre-sizes its bins" in p.stdout,
          "the gate output carries the written reason")

    # --baseline: prefix resolution, unknown, ambiguous, self.
    p = run(MEM + ["gate", "--history", h, "--baseline", "deadbeef"])
    check(p.returncode == 2 and "no record carries" in p.stderr,
          "an unknown --baseline prefix is a named usage error")

    h_amb = hist_path("ambiguous")
    record_from(synth_bundle(BASE), h_amb, sha(7))
    record_from(synth_bundle(BASE), h_amb, sha(7))
    p = run(MEM + ["gate", "--history", h_amb,
                   "--baseline", sha(7)])
    check(p.returncode == 2 and "ambiguous" in p.stderr,
          "an ambiguous commit prefix is refused")

    p = run(MEM + ["gate", "--history", h_amb,
                   "--baseline", sha(7)[:12]])
    check(p.returncode == 2 and "ambiguous" in p.stderr,
          "prefix resolution is by prefix, and ambiguity is found "
          "there too")

    p = run(MEM + ["gate", "--history", h, "--baseline", sha(2)])
    check(p.returncode == 2 and "the baseline is the record itself"
          in p.stderr,
          "the record does not regress against itself")

    # The unit law: a page is a unit. A baseline in different pages
    # is a different instrument — refused by default AND by explicit
    # --baseline.
    h_unit = hist_path("unit")
    record_from(synth_bundle(BASE), h_unit, sha(1))
    record_from(synth_bundle(BASE, env={"page_size": 8192}),
                h_unit, sha(3))
    p = run(MEM + ["gate", "--history", h_unit])
    check(p.returncode == 2
          and "no earlier record of the same instrument" in p.stderr,
          "an 8192-byte-page record with no 8192-byte-page baseline "
          "refuses by default")
    p = run(MEM + ["gate", "--history", h_unit, "--baseline", sha(1)])
    check(p.returncode == 1
          and "different units" in p.stderr
          and "8192" in p.stderr,
          "an explicit cross-page-size baseline is refused, named")

    # The suite law: a changed suite digest refuses comparison.
    h_suite = hist_path("suite")
    record_from(synth_bundle(BASE), h_suite, sha(1))
    record_from(synth_bundle(BASE), h_suite, sha(2),
                extra=["--suite", "benchmarks"])
    p = run(MEM + ["gate", "--history", h_suite, "--baseline", sha(1)])
    check(p.returncode == 1 and "suite changed" in p.stderr,
          "a changed suite digest refuses comparison — re-baseline, "
          "do not compare")

    # --threshold moves the band: +40% inside a 60% band is within.
    h_t = hist_path("threshold")
    record_from(synth_bundle(BASE), h_t, sha(1))
    record_from(synth_bundle(scaled(1.4)), h_t, sha(2))
    p = run(MEM + ["gate", "--history", h_t, "--threshold", "60"])
    check(p.returncode == 0 and "within-noise" in p.stdout,
          "--threshold moves the noise band")

    # --floor moves the band: +10 of 200 is inside the 16-page floor
    # but outside a 4-page floor at 2 percent (band 4).
    h_f = hist_path("floorband")
    record_from(synth_bundle(BASE), h_f, sha(1))
    record_from(synth_bundle([210, 210, 210, 210, 210]), h_f, sha(2))
    p = run(MEM + ["gate", "--history", h_f])
    check(p.returncode == 0 and "within-noise" in p.stdout,
          "+10 pages inside the 16-page floor is within-noise")
    p = run(MEM + ["gate", "--history", h_f, "--floor", "4",
                   "--threshold", "2"])
    check(p.returncode == 1 and "regressed" in p.stdout,
          "--floor and --threshold together move the band the other "
          "way")


# ===========================================================================
def _section_6():
    print("Section 6 — the determinism law")

    h = hist_path("gate")
    d1 = os.path.join(GATE, "dash1.md")
    d2 = os.path.join(GATE, "dash2.md")
    p1 = run(MEM + ["dashboard", "--history", h, "--out", d1])
    check(p1.returncode == 0 and os.path.isfile(d1),
          "the dashboard renders")
    run(MEM + ["dashboard", "--history", h, "--out", d2])
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
    run(MEM + ["dashboard", "--history", h, "--out", d3, "--stamp"])
    with open(d3, encoding="utf-8") as fh:
        doc3 = fh.read()
    l1 = [ln for ln in doc1.splitlines() if ln]
    l3 = [ln for ln in doc3.splitlines() if ln
          and not ln.startswith("- Generated: ")]
    check(l1 == l3 and "- Generated: " in doc3,
          "--stamp changes ONLY the Generated line")

    # Both trend tables and the slope watch render — "RSS over time"
    # is literally these tables.
    check("## Trends — resident set at markers" in doc1,
          "the resident-set-at-markers trend table renders — RSS over "
          "time, literally")
    check("## Slope watch" in doc1 and "250.0" in doc1,
          "the slope watch renders against the 0.25 page/cycle "
          "leak threshold")

    # The verdict table rows are sorted by workload name.
    jp = os.path.join(GATE, "dash.json")
    p = run(MEM + ["dashboard", "--history", h, "--out", d1,
                   "--json", jp])
    v = json.load(open(jp))
    names = [r["workload"] for r in v["workloads"]]
    check(names == sorted(names), "the verdict rows are sorted")
    check(v["verdict"] == "pass", "the sanctioned ledger gates pass")

    # A constant trend renders the mid band, not a wall.
    h_flat = hist_path("flat")
    record_from(synth_bundle(BASE), h_flat, sha(1))
    record_from(synth_bundle(BASE), h_flat, sha(2))
    p = run(MEM + ["dashboard", "--history", h_flat,
                   "--out", os.path.join(GATE, "flat.md")])
    with open(os.path.join(GATE, "flat.md"), encoding="utf-8") as fh:
        flat = fh.read()
    check("\u2584" in flat and "\u2588" not in flat,
          "a constant trend renders mid bands — no fake wall")

    # The first record of an instrument renders with an n/a verdict.
    h_one = hist_path("first")
    record_from(synth_bundle(BASE), h_one, sha(1))
    p = run(MEM + ["dashboard", "--history", h_one,
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
        p = run(MEM + ["history", "--history", h])
        check(p.returncode == 1 and needle in p.stderr, label)

    h = hist_path("corrupt")
    record_from(synth_bundle(BASE), h, sha(1))
    corrupt_entry(h, "this is not json\n", "line 2 is corrupt",
                  "a corrupt line is a named error naming the line")

    h = hist_path("badtype")
    record_from(synth_bundle(BASE), h, sha(1))
    corrupt_entry(h, '{"type": "quantum"}\n', "unknown type",
                  "an unknown entry type is a named error")

    h = hist_path("nopin")
    record_from(synth_bundle(BASE), h, sha(1))
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": "cafe",
                                 "suite": SUITE,
                                 "suite_digest": SUITE_DIGEST,
                                 "plan": {"cycles": 40, "warmup": 10,
                                          "interval": 10.0,
                                          "samples": 5},
                                 "env": {"page_size": 4096},
                                 "noise_pct": 5.0,
                                 "floor_pages": 16,
                                 "workloads": {"watched": {
                                     "marker_rss_pages": [1, 2],
                                     "end_rss_pages": [1, 2],
                                     "warmup_end_rss_pages": [1, 2],
                                     "slope_pages_per_cycle":
                                         [0.0, 0.0],
                                     "slope_ci_pages":
                                         [[-0.1, 0.1], [-0.1, 0.1]],
                                     "hls_rss_verdict": "stable"}}})
                  + "\n",
                  "commit pin", "a record without a 40-hex commit pin "
                  "is a named error")

    h = hist_path("nodigest")
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": sha(5),
                                 "suite": SUITE,
                                 "plan": {"cycles": 40, "warmup": 10,
                                          "interval": 10.0,
                                          "samples": 5},
                                 "env": {"page_size": 4096},
                                 "noise_pct": 5.0,
                                 "floor_pages": 16,
                                 "workloads": {"watched": {
                                     "marker_rss_pages": [1, 2],
                                     "end_rss_pages": [1, 2],
                                     "warmup_end_rss_pages": [1, 2],
                                     "slope_pages_per_cycle":
                                         [0.0, 0.0],
                                     "slope_ci_pages":
                                         [[-0.1, 0.1], [-0.1, 0.1]],
                                     "hls_rss_verdict": "stable"}}})
                  + "\n",
                  "suite digest", "a record without a suite digest "
                  "is a named error")

    h = hist_path("nounit")
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": sha(5),
                                 "suite": SUITE,
                                 "suite_digest": SUITE_DIGEST,
                                 "plan": {"cycles": 40, "warmup": 10,
                                          "interval": 10.0,
                                          "samples": 5},
                                 "env": {"cpus": 8},
                                 "noise_pct": 5.0,
                                 "floor_pages": 16,
                                 "workloads": {"watched": {
                                     "marker_rss_pages": [1, 2],
                                     "end_rss_pages": [1, 2],
                                     "warmup_end_rss_pages": [1, 2],
                                     "slope_pages_per_cycle":
                                         [0.0, 0.0],
                                     "slope_ci_pages":
                                         [[-0.1, 0.1], [-0.1, 0.1]],
                                     "hls_rss_verdict": "stable"}}})
                  + "\n",
                  "page size", "a record without its page size is a "
                  "record without its unit — a named error")

    h = hist_path("leaky")
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": sha(5),
                                 "suite": SUITE,
                                 "suite_digest": SUITE_DIGEST,
                                 "plan": {"cycles": 40, "warmup": 10,
                                          "interval": 10.0,
                                          "samples": 5},
                                 "env": {"page_size": 4096},
                                 "noise_pct": 5.0,
                                 "floor_pages": 16,
                                 "workloads": {"watched": {
                                     "marker_rss_pages": [1, 2],
                                     "end_rss_pages": [1, 2],
                                     "warmup_end_rss_pages": [1, 2],
                                     "slope_pages_per_cycle":
                                         [0.0, 0.0],
                                     "slope_ci_pages":
                                         [[-0.1, 0.1], [-0.1, 0.1]],
                                     "hls_rss_verdict": "leak"}}})
                  + "\n",
                  "certify stable", "a record whose workload did not "
                  "certify stable is a named error (the floor law "
                  "holds on read too)")

    h = hist_path("noreason")
    corrupt_entry(h, json.dumps({"type": "sanction",
                                 "workload": "watched"}) + "\n",
                  "reason", "a sanction without a written reason is a "
                  "named error")

    # history --workload filters records and sanctions alike.
    h = hist_path("filter")
    record_from(synth_bundle(BASE), h, sha(1))
    record_from({"tool": "hls-mem-bundle", "workloads": {
        "other": [synth_rss(200) for _ in BASE]}}, h, sha(2))
    run(MEM + ["sanction", "watched", "--reason", "later",
               "--history", h])
    p = run(MEM + ["history", "--history", h, "--workload",
                   "watched", "--json"])
    entries = json.loads(p.stdout)
    check(len(entries) == 2
          and entries[0]["type"] == "record"
          and entries[1]["type"] == "sanction",
          "history --workload keeps exactly the entries that touch "
          "the workload (records and sanctions)")


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
