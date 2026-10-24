#!/usr/bin/env python3
"""Stage 138 acceptance gate — the per-commit compile-time regression
dashboard (v0.156.0-alpha).

Run with `make ctime-acceptance` (or
`python3 tests/ctime_acceptance.py`).

Eight sections over the REAL hls-ctime CLI, the REAL compilers (both
of them: the stage0 bootstrap chain and the native bin/hlc), the REAL
tracked suite and the real git surface — a dashboard that did not
measure through the instruments it judges with would be a mock, not
a gate:

  1. the convention  — hls-ctime exits 0 verified / 1 named violation /
     2 usage; a bare invocation and an unknown command are usage
     errors; history/gate/dashboard on a missing or empty ledger are
     named errors, never silent renders

  2. the record law  — `record` runs the REAL stage0 chain
     (tools/hlinterp/boot.py running src/hlc.hls) over the REAL tracked suite
     and appends exactly one entry pinned to `git rev-parse HEAD`
     (instrument, suite digest, compiler SOURCE digest, compiler
     BINARY digest, plan, env fingerprint, 5 workloads with >= 2
     positive samples each); the native instrument records through
     the REAL bin/hlc with the binary digest pinned; the ledger is
     append-only (the bytes before are a byte prefix of the bytes
     after); a record without git and without --commit is refused; a
     bad --commit sha is a usage error; a work tree with tracked
     modifications is REFUSED — a per-commit record pins a clean
     commit (proven on a throwaway clone)

  3. the floor law   — the report import refuses a workload with
     fewer than 2 samples ("a median no sample supports is not a
     measurement"), non-positive compile times, zero workloads, a
     foreign report, an unknown instrument, a report without its
     three digests, a report whose suite digest does not match the
     local suite — and THE STALE-BINARY LAW: a native record whose
     compiler binary was already filed against a different compiler
     source is refused (bin/hlc not rebuilt), while the same binary
     under an unchanged source (the doc-only commit) and a fresh
     binary are both filed

  4. the classification law — against controlled ledgers (synthetic
     reports, explicit --commit pins, the seeded statistics):
     identical / +40% / significant-but-in-band / -40% pairs
     classify no-change / regressed / within-noise / improved, and
     the rename pair files new (reported, never failed) and
     vanished (failed BY NAME)

  5. the gate law    — a regressed workload fails the gate naming it;
     a written sanction flips it loud-but-green; a sanction without
     a reason is refused; an unknown workload is refused naming the
     known ones; --baseline resolves a prefix, refuses an unknown
     one, an ambiguous one and the record itself; stage0 and native
     are different instruments (refused by default and by explicit
     --baseline); a changed suite digest refuses comparison;
     --threshold moves the band

  6. the determinism law — two dashboards of one ledger are
     byte-identical; --stamp changes ONLY the Generated line; the
     document embeds the sha256 of the ledger bytes, the newest
     commit and BOTH compiler digests; the verdict table rows are
     sorted; a constant trend renders the mid band; a first-record
     ledger renders with an n/a verdict instead of crashing

  7. the ledger law  — a corrupt line, an unknown entry type, a
     record without its commit pin, a record without its suite
     digest, a record without its compiler source digest, a record
     without its compiler binary digest and a sanction without a
     written reason are all NAMED refusals naming the line; history
     --workload filters records and sanctions alike

  8. the corpus     — tests/ok/feat_stage138_ctime.hls is
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

CTIME = [sys.executable, os.path.join(ROOT, "tools", "hls-ctime.py")]
BOOT = [sys.executable, os.path.join(ROOT, "tools", "hlinterp", "boot.py")]
FMT = [sys.executable, os.path.join(ROOT, "tools", "hlfmt.py")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok",
                        "feat_stage138_ctime.hls")
SHOWCASE_OUT = ("stage 138: hls-ctime\n"
                "  CTIME OK: 5 workloads tracked, classified, "
                "sanctioned, pinned\n")

SUITE = "benchmarks/ctime_watch"


def suite_digest_of(rel_dir):
    """The same digest the tool computes: sha256 over the sorted
    (name, bytes) pairs of the suite directory — names included. The
    test spells it out so the digest algorithm itself is pinned by
    the gate, not just consumed."""
    h = hashlib.sha256()
    d = os.path.join(ROOT, rel_dir)
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".hls"):
            h.update(fn[:-len(".hls")].encode("utf-8"))
            h.update(b"\0")
            with open(os.path.join(d, fn), "rb") as fh:
                h.update(fh.read())
            h.update(b"\0")
    return h.hexdigest()


SUITE_DIGEST = suite_digest_of(SUITE)

GATE = tempfile.mkdtemp(prefix=".ctime-gate-", dir=ROOT)

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
# Synthetic hls-ctime machine reports — the schema `record
# --from-report` consumes, filled with controlled compile times.
# ---------------------------------------------------------------------------

def synth_workload(samples_ms):
    return {"samples_ms": list(samples_ms)}


def synth_report(workloads, instrument="stage0",
                 source="a" * 64, compiler="b" * 64,
                 suite=SUITE, suite_digest=None):
    return {
        "tool": "hls-ctime",
        "version": "0.156.0",
        "instrument": instrument,
        "suite": suite,
        "suite_digest": suite_digest or SUITE_DIGEST,
        "source_digest": source,
        "compiler_digest": compiler,
        "workloads": workloads,
    }


def write_report(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=1) + "\n")
    return path


def record_from(report_obj, history, commit, extra=None):
    rp = write_report(os.path.join(GATE, "in.json"), report_obj)
    cmd = CTIME + ["record", "--from-report", rp,
                   "--commit", commit, "--history", history]
    if extra:
        cmd += extra
    return run(cmd)


BASE = [1000.0, 1001.0, 1000.0, 999.0, 1000.0, 1001.0]


def scaled(f):
    return [v * f for v in BASE]


def sha(n):
    return "%040x" % n


# ===========================================================================
def _section_1():
    print("Section 1 — the convention")

    p = run(CTIME)
    check(p.returncode == 2, "a bare invocation is usage error 2")

    p = run(CTIME + ["warp"])
    check(p.returncode == 2, "an unknown command is usage error 2")

    p = run(CTIME + ["history", "--history", hist_path("absent")])
    check(p.returncode == 2 and "does not exist" in p.stderr,
          "history over a missing ledger is a named usage error")

    p = run(CTIME + ["gate", "--history", hist_path("absent")])
    check(p.returncode == 2 and "does not exist" in p.stderr,
          "gate over a missing ledger is a named usage error")

    p = run(CTIME + ["dashboard", "--history", hist_path("absent"),
                    "--out", os.path.join(GATE, "x.md")])
    check(p.returncode == 2 and "does not exist" in p.stderr,
          "dashboard over a missing ledger is a named usage error")

    p = run(CTIME + ["dashboard"])
    check(p.returncode == 2, "a dashboard without --out is usage "
          "error 2")

    empty = hist_path("empty")
    open(empty, "w").close()
    p = run(CTIME + ["gate", "--history", empty])
    check(p.returncode == 2 and "no records" in p.stderr,
          "gate over an empty ledger refuses, named")
    p = run(CTIME + ["dashboard", "--history", empty,
                    "--out", os.path.join(GATE, "never.md")])
    check(p.returncode == 2 and "no records" in p.stderr
          and not os.path.exists(os.path.join(GATE, "never.md")),
          "dashboard over an empty ledger refuses and writes nothing")


# ===========================================================================
def _section_2():
    print("Section 2 — the record law")

    # The REAL stage0 instrument: boot.py running src/hlc.hls over
    # every workload of the tracked suite.
    h = hist_path("real")
    p = run(CTIME + ["record", "--history", h,
                    "--samples", "3", "--warmup", "0"])
    check(p.returncode == 0 and "recorded" in p.stdout,
          "record runs the tracked suite through the real stage0 "
          "bootstrap chain")
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    check(("recorded %s" % head[:10]) in p.stdout,
          "the record is pinned to the live HEAD")

    p = run(CTIME + ["history", "--history", h, "--json"])
    entries = json.loads(p.stdout)
    recs = [e for e in entries if e["type"] == "record"]
    check(len(recs) == 1, "exactly one record was appended")
    r = recs[0]
    check(r["commit"] == head, "the record carries the live HEAD sha")
    check(r["instrument"] == "stage0", "the instrument is the stage0 "
          "bootstrap chain")
    check(r["suite_digest"] == SUITE_DIGEST,
          "the suite digest is the sha256 of the tracked suite")
    check(r["source_digest"] == _live_source_digest(),
          "the compiler source digest pins the compiler being timed")
    boot_digest = hashlib.sha256(
        open(os.path.join(ROOT, "tools", "hlinterp", "boot.py"), "rb").read()
    ).hexdigest()
    check(r["compiler_digest"] == boot_digest,
          "the stage0 compiler digest is the sha256 of the driver "
          "actually timed")
    check(r["plan"] == {"samples": 3, "warmup": 0},
          "the measurement plan is pinned in the record")
    check(len(r["workloads"]) == 5, "all 5 tracked workloads compiled")
    check(all(len(w["samples_ms"]) >= 2
              and all(x > 0 for x in w["samples_ms"])
              for w in r["workloads"].values()),
          "every workload carries >= 2 positive compile times")
    check("python" in r["env"] and "cpus" in r["env"],
          "the env fingerprint is recorded")

    before = open(h, "rb").read()
    p = run(CTIME + ["record", "--history", h,
                    "--samples", "3", "--warmup", "0"])
    after = open(h, "rb").read()
    check(p.returncode == 0 and after.startswith(before),
          "the ledger is append-only: the bytes before are a prefix "
          "of the bytes after")

    # The REAL native instrument: bin/hlc, with the binary pinned.
    if NATIVE:
        hn = hist_path("native-real")
        p = run(CTIME + ["record", "--history", hn,
                        "--instrument", "native",
                        "--samples", "4", "--warmup", "1"])
        check(p.returncode == 0 and "instrument=native" in p.stdout,
              "record runs the tracked suite through the real "
              "self-hosted compiler")
        p = run(CTIME + ["history", "--history", hn, "--json"])
        rn = [e for e in json.loads(p.stdout)
              if e["type"] == "record"][0]
        hlc_digest = hashlib.sha256(
            open(HLC, "rb").read()).hexdigest()
        check(rn["compiler_digest"] == hlc_digest,
              "the native record pins the sha256 of bin/hlc itself")
        check(rn["source_digest"] == _live_source_digest(),
              "the native record pins the compiler source it was "
              "built from")
        check(len(rn["workloads"]) == 5,
              "all 5 workloads compiled natively")
    else:
        print("  (bin/hlc or cc not available — the native record "
              "leg skipped, the stage0 legs ran)")

    nogit = os.path.join(GATE, "nogit")
    os.makedirs(nogit, exist_ok=True)
    rp = write_report(os.path.join(GATE, "pin.json"),
                      synth_report({"lex_dense": synth_workload(BASE)}))
    # GIT_CEILING_DIRECTORIES stops git's upward walk at the gate dir:
    # inside `nogit` there is no repository, exactly as in a bare CI
    # checkout.
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=GATE)
    p = run(CTIME + ["record", "--from-report", rp, "--root", nogit,
                    "--history", hist_path("pin")], env=env)
    check(p.returncode == 2 and "not a git work tree" in p.stderr,
          "a record without git and without --commit is refused")

    p = run(CTIME + ["record", "--from-report", rp, "--commit", "xyz",
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
        p = run(CTIME + ["record", "--root", clone,
                        "--history", hist_path("dirty")])
        check(p.returncode == 1
              and "tracked modifications" in p.stderr,
              "a work tree with tracked modifications is refused — "
              "a per-commit record pins a clean commit")


def _live_source_digest():
    """The compiler source digest the tool computes (the test spells
    it out: src/hlc.hls + every src/hlc/*.hls, names and bytes)."""
    h = hashlib.sha256()
    members = [("src/hlc.hls",
                os.path.join(ROOT, "src", "hlc.hls"))]
    srcdir = os.path.join(ROOT, "src", "hlc")
    for fn in sorted(os.listdir(srcdir)):
        if fn.endswith(".hls"):
            members.append((os.path.join("src", "hlc", fn),
                            os.path.join(srcdir, fn)))
    for rel, path in members:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(open(path, "rb").read())
        h.update(b"\0")
    return h.hexdigest()


# ===========================================================================
def _section_3():
    print("Section 3 — the floor law")

    h = hist_path("floor")
    cases = [
        (synth_report({"lex_dense": synth_workload([1000])}),
         1, "samples", "a one-sample workload is refused"),
        (synth_report({"lex_dense": synth_workload([])}),
         1, "samples", "a zero-sample workload is refused"),
        (synth_report({"lex_dense": synth_workload(
            [1000, 1002, 0, 1001])}),
         1, "non-positive", "a zero compile time is refused"),
        (synth_report({"lex_dense": synth_workload(
            [1000, 1002, -5, 1001])}),
         1, "non-positive", "a negative compile time is refused"),
        ({"tool": "hls-ctime", "instrument": "stage0",
          "suite_digest": SUITE_DIGEST,
          "source_digest": "a" * 64, "compiler_digest": "b" * 64,
          "workloads": {}},
         2, "no measured workloads",
         "a report with zero workloads is refused"),
        ({"tool": "not-ctime", "instrument": "stage0",
          "workloads": {"lex_dense": synth_workload(BASE)}},
         2, "not an hls-ctime report",
         "a foreign report is refused"),
        ({"tool": "hls-ctime", "instrument": "gpu",
          "suite_digest": SUITE_DIGEST,
          "source_digest": "a" * 64, "compiler_digest": "b" * 64,
          "workloads": {"lex_dense": synth_workload(BASE)}},
         2, "instrument", "an unknown instrument is refused"),
    ]
    for i, (obj, rc, needle, label) in enumerate(cases):
        p = record_from(obj, h, sha(i + 1))
        check(p.returncode == rc and needle in p.stderr,
              label + " (named)")

    # The report must pin all three digests of what it measured.
    for key in ("suite_digest", "source_digest", "compiler_digest"):
        obj = {"tool": "hls-ctime", "instrument": "stage0",
               "suite": SUITE, "suite_digest": SUITE_DIGEST,
               "source_digest": "a" * 64,
               "compiler_digest": "b" * 64,
               "workloads": {"lex_dense": synth_workload(BASE)}}
        obj.pop(key)
        p = record_from(obj, h, sha(40 + len(key)))
        check(p.returncode == 2 and key in p.stderr,
              "a report without its %s is refused" % key)

    # The suite law on import: the machine measured a different suite.
    obj = synth_report({"lex_dense": synth_workload(BASE)},
                       suite_digest="f" * 64)
    p = record_from(obj, h, sha(50))
    check(p.returncode == 1 and "different suite" in p.stderr,
          "a report whose suite digest does not match the local suite "
          "is refused")

    # THE STALE-BINARY LAW — the compile-time-specific floor.
    hs = hist_path("stale")
    r = record_from(synth_report(
        {"lex_dense": synth_workload(BASE)},
        instrument="native", source="a" * 64, compiler="c" * 64),
        hs, sha(1))
    check(r.returncode == 0, "the first native record is filed")
    r = record_from(synth_report(
        {"lex_dense": synth_workload(BASE)},
        instrument="native", source="d" * 64, compiler="c" * 64),
        hs, sha(2))
    check(r.returncode == 1 and "stale-binary" in r.stderr
          and "make bootstrap" in r.stderr,
          "a native record whose binary was already filed against a "
          "different compiler source is refused — the binary was not "
          "rebuilt")
    r = record_from(synth_report(
        {"lex_dense": synth_workload(BASE)},
        instrument="native", source="a" * 64, compiler="c" * 64),
        hs, sha(3))
    check(r.returncode == 0,
          "the same binary under an unchanged source (the doc-only "
          "commit) is expected, not stale")
    r = record_from(synth_report(
        {"lex_dense": synth_workload(BASE)},
        instrument="native", source="f" * 64, compiler="9" * 64),
        hs, sha(4))
    check(r.returncode == 0,
          "a fresh binary against a fresh source is filed")


# ===========================================================================
def _section_4():
    print("Section 4 — the classification law")

    # Four single-workload ledgers, each one baseline + one case
    # record: the gate classifies the pair and only the pair.
    cases = [
        ("identical", BASE, BASE, "no-change", 0),
        ("regressed", BASE, scaled(1.4), "regressed", 1),
        ("improved", BASE, scaled(0.6), "improved", 0),
        ("within-noise", BASE, [1020, 1021, 1020, 1019, 1020, 1021],
         "within-noise", 0),
    ]
    for name, base, cur, klass, want_rc in cases:
        h = hist_path("cls-" + name)
        r1 = record_from(synth_report(
            {"lex_dense": synth_workload(base)}), h, sha(1))
        r2 = record_from(synth_report(
            {"lex_dense": synth_workload(cur)}), h, sha(2))
        if r1.returncode != 0 or r2.returncode != 0:
            check(False, "%s: both records filed (%s / %s)"
                  % (name, r1.stderr.strip()[:80],
                     r2.stderr.strip()[:80]))
            continue
        jp = os.path.join(GATE, name + ".verdict.json")
        p = run(CTIME + ["gate", "--history", h, "--json", jp])
        v = json.load(open(jp))
        row = v["workloads"][0]
        check(row["class"] == klass,
              "%s samples classify %s" % (name, klass))
        check(p.returncode == want_rc,
              "%s: gate exit %d" % (name, want_rc))
        if klass == "regressed":
            check("GATE FAIL" in p.stdout
                  and "lex_dense" in p.stdout,
                  "a regression is named, not summarised")
        if klass == "no-change":
            check(abs(row["point_pct"]) < 1e-9 and row["p"] >= 0.05,
                  "identical samples: point 0%%, p not significant")

    # new / vanished — the rename law: a workload that appears is new
    # (reported, never failed), one that disappears is vanished and
    # fails BY NAME (silently losing a workload would hide
    # regressions).
    h = hist_path("cls-vanish")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE),
         "parse_deep": synth_workload(BASE)}), h, sha(1))
    record_from(synth_report(
        {"parse_deep": synth_workload(BASE),
         "gen_data": synth_workload(BASE)}), h, sha(2))
    jp = os.path.join(GATE, "vanish.verdict.json")
    p = run(CTIME + ["gate", "--history", h, "--json", jp])
    v = json.load(open(jp))
    classes = {r["workload"]: r["class"] for r in v["workloads"]}
    check(classes == {"parse_deep": "no-change",
                      "lex_dense": "vanished",
                      "gen_data": "new"},
          "the rename law: new is reported, vanished fails")
    check(p.returncode == 1 and "lex_dense" in p.stdout,
          "a vanished workload fails the gate by name")


# ===========================================================================
def _section_5():
    print("Section 5 — the gate law")

    # The regressed ledger from section 4, replayed with its
    # sanctions.
    h = hist_path("gate")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h, sha(1))
    record_from(synth_report(
        {"lex_dense": synth_workload(scaled(1.4))}), h, sha(2))
    p = run(CTIME + ["gate", "--history", h])
    check(p.returncode == 1 and "lex_dense" in p.stdout,
          "an unsanctioned regression fails the gate, named")

    p = run(CTIME + ["sanction", "nosuch", "--reason", "x",
                    "--history", h])
    check(p.returncode == 2 and "unknown workload" in p.stderr
          and "lex_dense" in p.stderr,
          "a sanction for an unknown workload is refused naming the "
          "known ones")

    p = run(CTIME + ["sanction", "lex_dense", "--history", h])
    check(p.returncode == 2 and "reason" in p.stderr,
          "a sanction without a written reason is refused")

    p = run(CTIME + ["sanction", "lex_dense",
                    "--reason", "the checker grew a second pass; "
                                "the compile paid for it",
                    "--history", h])
    check(p.returncode == 0, "the sanction is filed")

    jp = os.path.join(GATE, "gate.verdict.json")
    p = run(CTIME + ["gate", "--history", h, "--json", jp])
    v = json.load(open(jp))
    check(p.returncode == 0 and v["verdict"] == "pass",
          "a sanctioned regression is loud but green")
    check(v["sanctioned"] == ["lex_dense"],
          "the verdict carries the sanctioned workload")
    check("sanctioned" in p.stdout and "second pass" in p.stdout,
          "the gate output carries the written reason")

    # --baseline: prefix resolution, unknown, ambiguous, self.
    p = run(CTIME + ["gate", "--history", h, "--baseline", "deadbeef"])
    check(p.returncode == 2 and "no record carries" in p.stderr,
          "an unknown --baseline prefix is a named usage error")

    h_amb = hist_path("ambiguous")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h_amb, sha(7))
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h_amb, sha(7))
    p = run(CTIME + ["gate", "--history", h_amb,
                    "--baseline", sha(7)])
    check(p.returncode == 2 and "ambiguous" in p.stderr,
          "an ambiguous commit prefix is refused")
    p = run(CTIME + ["gate", "--history", h_amb,
                    "--baseline", sha(7)[:12]])
    check(p.returncode == 2 and "ambiguous" in p.stderr,
          "prefix resolution is by prefix, and ambiguity is found "
          "there too")

    h_one = hist_path("selfbase")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h_one, sha(9))
    p = run(CTIME + ["gate", "--history", h_one, "--baseline", sha(9)])
    check(p.returncode == 2 and "regress against itself" in p.stderr,
          "the record itself is refused as its own baseline")

    # The instrument law: the bootstrap chain and the self-hosted
    # compiler are different machines; the ledger never compares
    # across them.
    h_inst = hist_path("instrument")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h_inst, sha(1))
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}, instrument="native",
        compiler="e" * 64), h_inst, sha(3))
    p = run(CTIME + ["gate", "--history", h_inst])
    check(p.returncode == 2
          and "no earlier record of the same instrument" in p.stderr,
          "a native record with no native baseline refuses by default")
    p = run(CTIME + ["gate", "--history", h_inst, "--baseline", sha(1)])
    check(p.returncode == 1 and "different instruments" in p.stderr,
          "an explicit cross-instrument baseline is refused, named")

    # The suite law: a changed suite digest refuses comparison.
    h_suite = hist_path("suite")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h_suite, sha(1))
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)},
        suite="benchmarks", suite_digest=suite_digest_of("benchmarks")),
        h_suite, sha(2), extra=["--suite", "benchmarks"])
    p = run(CTIME + ["gate", "--history", h_suite, "--baseline", sha(1)])
    check(p.returncode == 1 and "suite changed" in p.stderr,
          "a changed suite digest refuses comparison — re-baseline, "
          "do not compare")

    # --threshold moves the band: +40% inside a 60% band is within.
    h_t = hist_path("threshold")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h_t, sha(1))
    record_from(synth_report(
        {"lex_dense": synth_workload(scaled(1.4))}), h_t, sha(2))
    p = run(CTIME + ["gate", "--history", h_t, "--threshold", "60"])
    check(p.returncode == 0 and "within-noise" in p.stdout,
          "--threshold moves the noise band")


# ===========================================================================
def _section_6():
    print("Section 6 — the determinism law")

    h = hist_path("gate")
    d1 = os.path.join(GATE, "dash1.md")
    d2 = os.path.join(GATE, "dash2.md")
    p1 = run(CTIME + ["dashboard", "--history", h, "--out", d1])
    check(p1.returncode == 0 and os.path.isfile(d1),
          "the dashboard renders")
    run(CTIME + ["dashboard", "--history", h, "--out", d2])
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
    check(("`%s`" % ("a" * 64)) in doc1
          and ("`%s`" % ("b" * 64)) in doc1,
          "the document embeds both compiler digests (source and "
          "binary)")

    d3 = os.path.join(GATE, "dash3.md")
    run(CTIME + ["dashboard", "--history", h, "--out", d3, "--stamp"])
    with open(d3, encoding="utf-8") as fh:
        doc3 = fh.read()
    l1 = [ln for ln in doc1.splitlines() if ln]
    l3 = [ln for ln in doc3.splitlines() if ln
          and not ln.startswith("- Generated: ")]
    check(l1 == l3 and "- Generated: " in doc3,
          "--stamp changes ONLY the Generated line")

    # The verdict table rows are sorted by workload name.
    jp = os.path.join(GATE, "dash.json")
    p = run(CTIME + ["dashboard", "--history", h, "--out", d1,
                    "--json", jp])
    v = json.load(open(jp))
    names = [r["workload"] for r in v["workloads"]]
    check(names == sorted(names), "the verdict rows are sorted")
    check(v["verdict"] == "pass", "the sanctioned ledger gates pass")

    # A constant trend renders the mid band, not a wall.
    h_flat = hist_path("flat")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h_flat, sha(1))
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h_flat, sha(2))
    p = run(CTIME + ["dashboard", "--history", h_flat,
                    "--out", os.path.join(GATE, "flat.md")])
    with open(os.path.join(GATE, "flat.md"), encoding="utf-8") as fh:
        flat = fh.read()
    check("\u2584" in flat and "\u2588" not in flat,
          "a constant trend renders mid bands — no fake wall")

    # The first record of an instrument renders with an n/a verdict.
    h_one = hist_path("first")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h_one, sha(1))
    p = run(CTIME + ["dashboard", "--history", h_one,
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
        p = run(CTIME + ["history", "--history", h])
        check(p.returncode == 1 and needle in p.stderr, label)

    h = hist_path("corrupt")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h, sha(1))
    corrupt_entry(h, "this is not json\n", "line 2 is corrupt",
                  "a corrupt line is a named error naming the line")

    h = hist_path("badtype")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h, sha(1))
    corrupt_entry(h, '{"type": "quantum"}\n', "unknown type",
                  "an unknown entry type is a named error")

    h = hist_path("nopin")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h, sha(1))
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": "cafe",
                                 "instrument": "stage0",
                                 "suite_digest": SUITE_DIGEST,
                                 "source_digest": "a" * 64,
                                 "compiler_digest": "b" * 64,
                                 "workloads": {"w": {
                                     "samples_ms": [1, 2]}}}) + "\n",
                  "commit pin", "a record without a 40-hex commit pin "
                  "is a named error")

    h = hist_path("nodigest")
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": sha(5),
                                 "instrument": "stage0",
                                 "workloads": {"w": {
                                     "samples_ms": [1, 2]}}}) + "\n",
                  "suite digest", "a record without a suite digest "
                  "is a named error")

    h = hist_path("nosource")
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": sha(5),
                                 "instrument": "stage0",
                                 "suite_digest": SUITE_DIGEST,
                                 "compiler_digest": "b" * 64,
                                 "workloads": {"w": {
                                     "samples_ms": [1, 2]}}}) + "\n",
                  "source digest",
                  "a record without a compiler source digest is a "
                  "named error")

    h = hist_path("nobinary")
    corrupt_entry(h, json.dumps({"type": "record",
                                 "commit": sha(5),
                                 "instrument": "stage0",
                                 "suite_digest": SUITE_DIGEST,
                                 "source_digest": "a" * 64,
                                 "workloads": {"w": {
                                     "samples_ms": [1, 2]}}}) + "\n",
                  "binary digest",
                  "a record without a compiler binary digest is a "
                  "named error")

    h = hist_path("noreason")
    corrupt_entry(h, json.dumps({"type": "sanction",
                                 "workload": "lex_dense"}) + "\n",
                  "reason", "a sanction without a written reason is a "
                  "named error")

    # history --workload filters records and sanctions alike.
    h = hist_path("filter")
    record_from(synth_report(
        {"lex_dense": synth_workload(BASE)}), h, sha(1))
    record_from(synth_report(
        {"parse_deep": synth_workload(BASE)}), h, sha(2))
    run(CTIME + ["sanction", "lex_dense", "--reason", "later",
                "--history", h])
    p = run(CTIME + ["history", "--history", h, "--workload",
                    "lex_dense", "--json"])
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
