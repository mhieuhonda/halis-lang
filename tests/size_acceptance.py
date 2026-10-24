#!/usr/bin/env python3
"""Stage 139 acceptance gate — the per-commit binary-size regression
dashboard (v0.157.0-alpha).

Run with `make size-acceptance` (or
`python3 tests/size_acceptance.py`).

Eight sections over the REAL hls-size CLI, the REAL compilers (both
of them: the stage0 bootstrap chain and the native bin/hlc), the
REAL tracked suite and the real git surface — a dashboard that did
not measure through the instruments it judges with would be a mock,
not a gate:

  1. the convention  — hls-size exits 0 verified / 1 named violation /
     2 usage; a bare invocation and an unknown command are usage
     errors; gate/dashboard/history/sanction on a missing ledger are
     named errors, never silent renders

  2. the record law  — `record` runs the REAL bin/hlc over the REAL
     tracked suite and appends exactly one entry pinned to
     `git rev-parse HEAD` (instrument, suite digest, compiler SOURCE
     digest, compiler BINARY digest, machine fingerprint, the five
     workloads AND hlc_self on five axes each); the hlc_self axes are
     the real sizes of bin/hlc.c and bin/hlc; the stage0 record
     carries NO hlc_self and its five workloads' axes are
     BYTE-IDENTICAL to the native record's (the bootstrap invariant
     is the tool's own cross-check); the ledger is append-only (the
     bytes before are a byte prefix of the bytes after); a record
     without git and without --commit is refused; a bad --commit sha
     is a usage error; a work tree with tracked modifications is
     REFUSED — a per-commit record pins a clean commit (proven on a
     throwaway clone)

  3. the floor law   — the report import refuses a workload with a
     missing axis, a zero emission or binary, a negative axis, a
     foreign report, an unknown instrument, a report without its
     three digests, a report without its machine fingerprint, a
     report whose suite digest does not match the local suite, an
     hlc_self on the stage0 instrument — and THE STALE-BINARY LAW:
     a native record whose compiler binary was already filed against
     a different compiler source is refused (bin/hlc not rebuilt),
     while the same binary under an unchanged source (the doc-only
     commit) and a fresh binary are both filed

  4. the classification law — against controlled ledgers (synthetic
     reports, explicit --commit pins): identical pairs classify
     no-change, one byte of growth on one axis regresses the
     workload, growth-plus-shrink still regresses (the worst-axis
     law), an all-clear with one strict shrink improves, and the
     rename pair files new (reported, never failed) and vanished
     (failed BY NAME)

  5. the gate law    — a regressed workload fails the gate naming it;
     a written sanction flips it loud-but-green; a sanction without a
     reason is refused; an unknown workload is refused naming the
     known ones; --baseline resolves a prefix, refuses an unknown
     one, an ambiguous one and the record itself; stage0 and native
     are different instruments (refused by default and by explicit
     --baseline); a changed suite digest refuses comparison; a
     changed MACHINE (cc, arch or recipe) refuses comparison — sizes
     from two toolchains are not the same language

  6. the determinism law — two dashboards of one ledger are
     byte-identical; --stamp changes ONLY the Generated line; the
     document embeds the sha256 of the ledger bytes, the newest
     commit and BOTH compiler digests; the verdict table rows are
     sorted; a constant trend renders the mid band; a first-record
     ledger renders with an n/a verdict instead of crashing

  7. the ledger law  — a corrupt line, an unknown entry type, a
     record without its commit pin, without its suite digest, without
     its compiler source digest, without its compiler binary digest,
     without its machine fingerprint, a workload without an axis and
     a sanction without a written reason are all NAMED refusals
     naming the line; history --workload filters records and
     sanctions alike

  8. the corpus     — tests/ok/feat_stage139_size.hls is
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

SIZE = [sys.executable, "tools/hls-size.py"]
BOOT = [sys.executable, "tools/hlinterp/boot.py"]
FMT = [sys.executable, "tools/hlfmt.py"]
LINT = [sys.executable, "tools/hllint.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage139_size.hls")
SHOWCASE_OUT = "all 9 size laws hold\n"

SUITE = "benchmarks/size_watch"
RECIPE = "gcc -O2 -o out out.c -lm -pthread"
AXES = ("c_bytes", "bin_bytes", "text_bytes", "data_bytes",
        "bss_bytes")


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

GATE = tempfile.mkdtemp(prefix=".size-gate-", dir=ROOT)

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
# Synthetic hls-size machine reports — the schema `record
# --from-report` consumes, filled with controlled axes.
# ---------------------------------------------------------------------------

def synth_workload(c=100000, b=20000, t=7000, d=700, s=100):
    return {"c_bytes": c, "bin_bytes": b, "text_bytes": t,
            "data_bytes": d, "bss_bytes": s}


def synth_report(workloads, instrument="native",
                 source="a" * 64, compiler="b" * 64,
                 suite=SUITE, suite_digest=None,
                 cc="cc (gate) 1.0", arch="gate64",
                 recipe=RECIPE):
    return {
        "tool": "hls-size",
        "version": "0.157.0",
        "instrument": instrument,
        "suite": suite,
        "suite_digest": suite_digest or SUITE_DIGEST,
        "source_digest": source,
        "compiler_digest": compiler,
        "env": {"cc": cc, "arch": arch, "recipe": recipe,
                "python": "3.12.0", "cpus": 2},
        "workloads": workloads,
    }


def write_report(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=1) + "\n")
    return path


def record_from(report_obj, history, commit, extra=None):
    rp = write_report(os.path.join(GATE, "in.json"), report_obj)
    cmd = SIZE + ["record", "--from-report", rp,
                  "--commit", commit, "--history", history]
    if extra:
        cmd += extra
    return run(cmd)


def sha(n):
    return "%040x" % n


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
def _section_1():
    print("Section 1 — the convention")

    p = run(SIZE)
    check(p.returncode == 2, "a bare invocation is a usage error "
          "(exit 2)")
    p = run(SIZE + ["no-such-command"])
    check(p.returncode == 2, "an unknown command is a usage error "
          "(exit 2)")

    missing = hist_path("missing")
    for cmdname in ("gate", "dashboard", "history", "sanction"):
        argv = SIZE + [cmdname]
        if cmdname == "dashboard":
            argv += ["--out", os.path.join(GATE, "d.md")]
        if cmdname == "sanction":
            argv += ["fn_grid"]
        argv += ["--history", missing]
        p = run(argv)
        check(p.returncode == 2 and "does not exist" in p.stderr,
              "%s on a missing ledger is a named error" % cmdname)


# ===========================================================================
def _section_2():
    print("Section 2 — the record law")

    # The REAL native instrument: bin/hlc over the tracked suite.
    h = hist_path("real-native")
    p = run(SIZE + ["record", "--history", h])
    check(p.returncode == 0 and "recorded" in p.stdout,
          "record runs the tracked suite through the real "
          "self-hosted compiler")
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    check(("recorded %s" % head[:10]) in p.stdout,
          "the record is pinned to the live HEAD")

    p = run(SIZE + ["history", "--history", h, "--json"])
    entries = json.loads(p.stdout)
    recs = [e for e in entries if e["type"] == "record"]
    check(len(recs) == 1, "exactly one record was appended")
    r = recs[0]
    check(r["commit"] == head, "the record carries the live HEAD sha")
    check(r["instrument"] == "native", "the default instrument is "
          "native, bin/hlc")
    check(r["suite_digest"] == SUITE_DIGEST,
          "the suite digest is the sha256 of the tracked suite")
    check(r["source_digest"] == _live_source_digest(),
          "the compiler source digest pins the compiler that "
          "measured")
    hlc_digest = hashlib.sha256(open(HLC, "rb").read()).hexdigest()
    check(r["compiler_digest"] == hlc_digest,
          "the native record pins the sha256 of bin/hlc itself")
    check(len(r["workloads"]) == 6,
          "the native record carries the 5 workloads AND hlc_self")
    check("hlc_self" in r["workloads"],
          "hlc_self — the compiler's own girth — is a first-class "
          "workload")
    for name, w in r["workloads"].items():
        check(all(axis in w and isinstance(w[axis], int)
                  and w[axis] >= 0 for axis in AXES)
              and w["c_bytes"] > 0 and w["bin_bytes"] > 0,
              "workload %s carries all five axes, counted in "
              "non-negative integers" % name)
    check(r["workloads"]["hlc_self"]["c_bytes"]
          == os.path.getsize(os.path.join(ROOT, "bin", "hlc.c")),
          "hlc_self c_bytes is the real size of bin/hlc.c")
    check(r["workloads"]["hlc_self"]["bin_bytes"]
          == os.path.getsize(HLC),
          "hlc_self bin_bytes is the real size of bin/hlc")
    for key in ("cc", "arch", "recipe"):
        check(r["env"].get(key),
              "the machine fingerprint pins the %s the bytes were "
              "made on" % key)

    before = open(h, "rb").read()
    p = run(SIZE + ["record", "--history", h])
    after = open(h, "rb").read()
    check(p.returncode == 0 and after.startswith(before),
          "the ledger is append-only: the bytes before are a prefix "
          "of the bytes after")

    # The REAL stage0 instrument: boot.py running src/hlc.hls —
    # byte-identical emissions by the bootstrap invariant, so the
    # five shared workloads must agree BYTE FOR BYTE with the native
    # record, and hlc_self must be absent.
    hs = hist_path("real-stage0")
    p = run(SIZE + ["record", "--history", hs,
                    "--instrument", "stage0"])
    check(p.returncode == 0 and "instrument=stage0" in p.stdout,
          "record runs the tracked suite through the real stage0 "
          "bootstrap chain")
    p = run(SIZE + ["history", "--history", hs, "--json"])
    rs = [e for e in json.loads(p.stdout)
          if e["type"] == "record"][0]
    check(len(rs["workloads"]) == 5 and "hlc_self" not in rs["workloads"],
          "the stage0 record carries the 5 workloads and NO hlc_self "
          "(the bootstrap chain has no built compiler to weigh)")
    boot_digest = hashlib.sha256(
        open(os.path.join(ROOT, "tools", "hlinterp", "boot.py"), "rb").read()
    ).hexdigest()
    check(rs["compiler_digest"] == boot_digest,
          "the stage0 record pins the sha256 of the driver it ran "
          "through")
    same = all(rs["workloads"][n] == recs[0]["workloads"][n]
               for n in rs["workloads"])
    check(same,
          "stage0 and native emissions are byte-identical on every "
          "axis (the bootstrap invariant, cross-checked)")

    nogit = os.path.join(GATE, "nogit")
    os.makedirs(nogit, exist_ok=True)
    rp = write_report(os.path.join(GATE, "pin.json"),
                      synth_report({"floor": synth_workload()}))
    # GIT_CEILING_DIRECTORIES stops git's upward walk at the gate dir:
    # inside `nogit` there is no repository, exactly as in a bare CI
    # checkout.
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=GATE)
    p = run(SIZE + ["record", "--from-report", rp, "--root", nogit,
                    "--history", hist_path("pin")], env=env)
    check(p.returncode == 2 and "not a git work tree" in p.stderr,
          "a record without git and without --commit is refused")

    p = run(SIZE + ["record", "--from-report", rp, "--commit", "xyz",
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
        p = run(SIZE + ["record", "--root", clone,
                        "--history", hist_path("dirty")])
        check(p.returncode == 1
              and "tracked modifications" in p.stderr,
              "a work tree with tracked modifications is refused — "
              "a per-commit record pins a clean commit")


# ===========================================================================
def _section_3():
    print("Section 3 — the floor law")

    h = hist_path("floor")

    # A foreign report is refused by tool name.
    foreign = synth_report({"floor": synth_workload()})
    foreign["tool"] = "hls-ctime"
    p = record_from(foreign, h, sha(1))
    check(p.returncode == 2 and "not an hls-size report" in p.stderr,
          "a foreign tool's report is refused")

    p = record_from(synth_report({"floor": synth_workload()},
                                 instrument="interp"), h, sha(1))
    check(p.returncode == 2 and "not one of" in p.stderr,
          "an unknown instrument is refused")

    for key in ("suite_digest", "source_digest", "compiler_digest"):
        broken = synth_report({"floor": synth_workload()})
        broken[key] = "nothex"
        p = record_from(broken, h, sha(1))
        check(p.returncode == 2 and "no valid %s" % key in p.stderr,
              "a report without a valid %s is refused" % key)

    broken = synth_report({"floor": synth_workload()})
    del broken["env"]
    p = record_from(broken, h, sha(1))
    check(p.returncode == 2 and "machine" in p.stderr,
          "a report without its machine fingerprint is refused")

    broken = synth_report({"floor": synth_workload()},
                          suite_digest="c" * 64)
    p = record_from(broken, h, sha(1))
    check(p.returncode == 1 and "does not match the local suite"
          in p.stderr,
          "a report whose suite digest does not match the local "
          "suite is refused — re-baseline, do not file")

    # A workload with a missing axis measured on four axes.
    broken = synth_report({"floor": synth_workload()})
    del broken["workloads"]["floor"]["bss_bytes"]
    p = record_from(broken, h, sha(1))
    check(p.returncode == 1 and "carries no bss_bytes" in p.stderr,
          "a workload measured without one of its five axes is "
          "refused")

    # The honest zero law: an empty emission or binary is a bug.
    broken = synth_report({"floor": synth_workload(c=0)})
    p = record_from(broken, h, sha(1))
    check(p.returncode == 1 and "c_bytes is zero" in p.stderr,
          "a zero emission is a bug, not a size")
    broken = synth_report({"floor": synth_workload(b=0)})
    p = record_from(broken, h, sha(1))
    check(p.returncode == 1 and "bin_bytes is zero" in p.stderr,
          "a zero binary is a bug, not a size")

    broken = synth_report({"floor": synth_workload(s=-40)})
    p = record_from(broken, h, sha(1))
    check(p.returncode == 1 and "non-negative" in p.stderr,
          "a negative axis is refused — bytes are counted, never "
          "estimated")

    # hlc_self is a native-instrument workload only.
    p = record_from(synth_report({"hlc_self": synth_workload()},
                                 instrument="stage0"), h, sha(1))
    check(p.returncode == 1
          and "native instrument" in p.stderr
          and "hlc_self" in p.stderr,
          "an hlc_self row on the stage0 instrument is refused")

    # THE STALE-BINARY LAW, on a dedicated ledger.
    hs = hist_path("stale")
    w = {"floor": synth_workload()}
    p = record_from(synth_report(w, source="a" * 64,
                                compiler="b" * 64), hs, sha(1))
    check(p.returncode == 0, "the first native record is filed")
    p = record_from(synth_report(w, source="c" * 64,
                                compiler="b" * 64), hs, sha(2))
    check(p.returncode == 1 and "stale-binary law" in p.stderr
          and "make bootstrap" in p.stderr,
          "the same binary under a different compiler source is "
          "refused (bin/hlc was not rebuilt)")
    p = record_from(synth_report(w, source="a" * 64,
                                compiler="b" * 64), hs, sha(3))
    check(p.returncode == 0,
          "the same binary under the SAME source is expected (the "
          "doc-only commit), not stale")
    p = record_from(synth_report(w, source="c" * 64,
                                compiler="d" * 64), hs, sha(4))
    check(p.returncode == 0,
          "a fresh binary under a changed source is a rebuild, not "
          "a stale pin")


# ===========================================================================
def _section_4():
    print("Section 4 — the classification law")

    base = {"floor": synth_workload(),
            "grid": synth_workload(c=90000, b=18000, t=6000,
                                   d=600, s=90)}

    # Identical pairs: every axis holds, no workload regressed.
    h = hist_path("cls-same")
    record_from(synth_report(base), h, sha(1))
    record_from(synth_report(base), h, sha(2))
    p = run(SIZE + ["gate", "--history", h])
    check(p.returncode == 0 and "all axes held" in p.stdout,
          "identical records classify no-change on every axis "
          "(the gate passes)")

    # One byte of growth on ONE axis regresses the workload.
    h = hist_path("cls-grow")
    record_from(synth_report(base), h, sha(1))
    grown = {"floor": synth_workload(b=20001),
             "grid": synth_workload(c=90000, b=18000, t=6000,
                                    d=600, s=90)}
    record_from(synth_report(grown), h, sha(2))
    p = run(SIZE + ["gate", "--history", h])
    check(p.returncode == 1 and "floor" in p.stdout
          and "bin_bytes +1" in p.stdout,
          "one byte of growth on one axis regresses the workload "
          "naming the axis")
    check("grid" in p.stdout and "no-change" in p.stdout,
          "the untouched workload holds (reported, never failed)")

    # Growth-plus-shrink is still regressed: the worst axis wins.
    h = hist_path("cls-worst")
    record_from(synth_report(base), h, sha(1))
    mixed = {"floor": synth_workload(c=100001, s=99),
             "grid": synth_workload(c=90000, b=18000, t=6000,
                                    d=600, s=90)}
    record_from(synth_report(mixed), h, sha(2))
    p = run(SIZE + ["gate", "--history", h, "--json",
                    os.path.join(GATE, "worst.json")])
    v = json.load(open(os.path.join(GATE, "worst.json")))
    row = [r for r in v["workloads"] if r["workload"] == "floor"][0]
    check(p.returncode == 1 and row["class"] == "regressed"
          and row["axes"]["bss_bytes"]["class"] == "improved",
          "a workload that grew one axis and shrank another is "
          "regressed — the worst axis wins")

    # An all-clear with one strict shrink improves (free, always).
    h = hist_path("cls-shrink")
    record_from(synth_report(base), h, sha(1))
    shrunk = {"floor": synth_workload(t=6999),
              "grid": synth_workload(c=90000, b=18000, t=6000,
                                     d=600, s=90)}
    record_from(synth_report(shrunk), h, sha(2))
    p = run(SIZE + ["gate", "--history", h])
    check(p.returncode == 0 and "improved" in p.stdout
          and "text_bytes -1" in p.stdout,
          "every axis held but one that strictly shrank: improved, "
          "and improvements never fail the gate")

    # The rename pair: the new name is reported, the old name
    # vanished fails BY NAME.
    h = hist_path("cls-rename")
    record_from(synth_report(base), h, sha(1))
    renamed = {"floor": synth_workload(),
               "grid_wide": synth_workload(c=90000, b=18000,
                                           t=6000, d=600, s=90)}
    record_from(synth_report(renamed), h, sha(2))
    p = run(SIZE + ["gate", "--history", h])
    check(p.returncode == 1 and "grid" in p.stdout
          and "vanished" in p.stdout,
          "a vanished workload fails the gate BY NAME")
    check("new" in p.stdout and "grid_wide" in p.stdout,
          "the renamed workload is reported as new, never failed")


# ===========================================================================
def _section_5():
    print("Section 5 — the gate law")

    base = {"floor": synth_workload()}
    h = hist_path("gate")
    record_from(synth_report(base, instrument="stage0"), h, sha(1))
    record_from(synth_report(base), h, sha(2))

    # Cross-instrument comparison is refused by default.
    p = run(SIZE + ["gate", "--history", h])
    check(p.returncode == 2
          and "no earlier record of the same instrument" in p.stderr,
          "the native record has no native baseline to compare "
          "against — the stage0 record is not one")
    # And by explicit --baseline.
    p = run(SIZE + ["gate", "--history", h, "--baseline", sha(1)])
    check(p.returncode == 1 and "different instruments" in p.stderr,
          "an explicit --baseline across instruments is refused")

    # The machine law: a changed cc refuses the comparison.
    hm = hist_path("machine")
    record_from(synth_report(base), hm, sha(1))
    record_from(synth_report(base, cc="cc (other) 9.9"), hm, sha(2))
    p = run(SIZE + ["gate", "--history", hm])
    check(p.returncode == 1 and "the machine changed" in p.stderr
          and "cc" in p.stderr,
          "a changed cc refuses the comparison — sizes from two "
          "toolchains are not the same language")
    hm2 = hist_path("machine-arch")
    record_from(synth_report(base), hm2, sha(1))
    record_from(synth_report(base, arch="other64"), hm2, sha(2))
    p = run(SIZE + ["gate", "--history", hm2])
    check(p.returncode == 1 and "arch" in p.stderr,
          "a changed arch refuses the comparison")
    hm3 = hist_path("machine-recipe")
    record_from(synth_report(base), hm3, sha(1))
    record_from(synth_report(base, recipe="cc -O3 -o out out.c"),
                hm3, sha(2))
    p = run(SIZE + ["gate", "--history", hm3])
    check(p.returncode == 1 and "recipe" in p.stderr,
          "a changed link recipe refuses the comparison")

    # The suite changed between records. A record with a foreign
    # suite digest cannot even be FILED (`record --from-report`
    # checks the local suite), so this ledger is written by hand —
    # exactly as a history looks after a re-baselined suite.
    hq = hist_path("suite")
    r1 = synth_report({"floor": synth_workload()},
                      suite_digest="e" * 64)
    r1["type"] = "record"
    r1["commit"] = sha(1)
    r2 = synth_report({"floor": synth_workload()})
    r2["type"] = "record"
    r2["commit"] = sha(2)
    with open(hq, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(r1, sort_keys=True) + "\n")
        fh.write(json.dumps(r2, sort_keys=True) + "\n")
    p = run(SIZE + ["gate", "--history", hq])
    check(p.returncode == 2
          and "no earlier record of the same instrument" in p.stderr,
          "a changed suite digest leaves no comparable baseline in "
          "the default path")
    p = run(SIZE + ["gate", "--history", hq, "--baseline", sha(1)])
    check(p.returncode == 1 and "the suite changed" in p.stderr,
          "an explicit --baseline over a changed suite digest is "
          "refused — re-baseline, do not compare")

    # --baseline resolves a prefix, refuses an unknown one, an
    # ambiguous one and the record itself. Four records whose
    # commits differ from the FIRST character on: a/b/c/d.
    hb = hist_path("baseline")
    for c in ("a", "b", "c", "d"):
        record_from(synth_report(base), hb, c * 40)
    record_from(synth_report(base), hb, "e" * 39 + "1")
    record_from(synth_report(base), hb, "e" * 39 + "2")
    p = run(SIZE + ["gate", "--history", hb, "--baseline", "bbbbb"])
    check(p.returncode == 0, "--baseline resolves a commit prefix")
    p = run(SIZE + ["gate", "--history", hb, "--baseline", "fffff"])
    check(p.returncode == 2 and "no record carries commit prefix"
          in p.stderr, "an unknown --baseline prefix is refused")
    p = run(SIZE + ["gate", "--history", hb, "--baseline", "eeeee"])
    check(p.returncode == 2 and "ambiguous" in p.stderr,
          "an ambiguous --baseline prefix is refused")
    p = run(SIZE + ["gate", "--history", hb, "--baseline",
                    "e" * 39 + "2"])
    check(p.returncode == 2 and "does not regress against itself"
          in p.stderr, "the record itself cannot be its own baseline")

    # A sanction flips a regression loud-but-green.
    hg = hist_path("sanction")
    record_from(synth_report(base), hg, sha(1))
    record_from(synth_report({"floor": synth_workload(b=20064)}),
                hg, sha(2))
    p = run(SIZE + ["gate", "--history", hg])
    check(p.returncode == 1 and "GATE FAIL" in p.stdout
          and "floor" in p.stdout,
          "an unsanctioned regression fails the gate naming the "
          "workload")
    p = run(SIZE + ["sanction", "floor", "--history", hg])
    check(p.returncode == 2 and "--reason" in p.stderr,
          "a sanction without a written reason is refused")
    p = run(SIZE + ["sanction", "no_such_workload",
                    "--reason", "why", "--history", hg])
    check(p.returncode == 2 and "unknown workload" in p.stderr
          and "floor" in p.stderr,
          "an unknown workload is refused naming the known ones")
    p = run(SIZE + ["sanction", "floor", "--reason",
                    "the runtime grew a guard page check",
                    "--author", "mhieuhonda", "--history", hg])
    check(p.returncode == 0 and "mhieuhonda" in p.stdout,
          "a written sanction is filed with its author")
    p = run(SIZE + ["gate", "--history", hg])
    check(p.returncode == 0 and "sanctioned" in p.stdout
          and "GATE PASS" in p.stdout,
          "a sanctioned regression is loud but green")


# ===========================================================================
def _section_6():
    print("Section 6 — the determinism law")

    base = {"floor": synth_workload(),
            "grid": synth_workload(c=90000, b=18000, t=6000,
                                   d=600, s=90)}
    h = hist_path("dash")
    record_from(synth_report(base), h, sha(1))
    record_from(synth_report(base), h, sha(2))

    d1 = os.path.join(GATE, "dash1.md")
    d2 = os.path.join(GATE, "dash2.md")
    run(SIZE + ["dashboard", "--out", d1, "--history", h])
    run(SIZE + ["dashboard", "--out", d2, "--history", h])
    check(open(d1, "rb").read() == open(d2, "rb").read(),
          "two dashboards of one ledger are byte-identical")

    d3 = os.path.join(GATE, "dash3.md")
    run(SIZE + ["dashboard", "--out", d3, "--history", h, "--stamp"])
    l1 = open(d1, encoding="utf-8").read().splitlines()
    l3 = open(d3, encoding="utf-8").read().splitlines()
    gen1 = [l for l in l1 if l.startswith("- Generated:")]
    gen3 = [l for l in l3 if l.startswith("- Generated:")]
    check(len(gen3) == 1 and not gen1
          and [l for l in l1 if not l.startswith("- Generated:")]
          == [l for l in l3 if not l.startswith("- Generated:")],
          "--stamp changes ONLY the Generated line")

    doc = open(d1, encoding="utf-8").read()
    ledger_sha = hashlib.sha256(open(h, "rb").read()).hexdigest()
    check(ledger_sha in doc,
          "the document embeds the sha256 of the ledger bytes")
    check(sha(2) in doc,
          "the document embeds the newest record's commit")
    check("a" * 64 in doc and "b" * 64 in doc,
          "the document embeds BOTH compiler digests")
    doc_lines = doc.splitlines()
    start = doc_lines.index(
        "## Verdicts (newest record vs baseline, byte-exact)")
    end = doc_lines.index(
        "## Trends (bin_bytes across records, oldest to newest)")
    vrows = [l for l in doc_lines[start:end]
             if l.startswith("| ") and " | " in l
             and not l.startswith("| workload")]
    check(vrows == sorted(vrows, key=str),
          "the verdict table rows are sorted")
    check("\u2584\u2584" in doc,
          "a constant trend renders the mid band, not a wall of "
          "max-height blocks")

    hf = hist_path("dash-first")
    record_from(synth_report(base), hf, sha(1))
    d4 = os.path.join(GATE, "dash4.md")
    p = run(SIZE + ["dashboard", "--out", d4, "--history", hf])
    check(p.returncode == 0 and "n/a" in open(
        d4, encoding="utf-8").read(),
        "a first-record ledger renders with an n/a verdict instead "
        "of crashing")


# ===========================================================================
def _section_7():
    print("Section 7 — the ledger law")

    good = json.dumps(synth_report({"floor": synth_workload()}))
    rec = json.loads(good)

    def with_line(obj):
        return json.dumps(obj, sort_keys=True)

    cases = [
        ("corrupt line", "{not json at all\n", "corrupt"),
        ("unknown entry type", '{"type": "poem"}\n', "unknown type"),
    ]
    r = json.loads(good)
    del r["tool"]
    r["type"] = "record"
    r["commit"] = sha(1)
    base_line = with_line(r)
    variants = [
        ("a record without its commit pin",
         lambda x: x.update(commit=""), "commit pin"),
        ("a record without its suite digest",
         lambda x: x.update(suite_digest=""), "suite digest"),
        ("a record without its compiler source digest",
         lambda x: x.update(source_digest=""), "source digest"),
        ("a record without its compiler binary digest",
         lambda x: x.update(compiler_digest=""), "binary digest"),
        ("a record without its machine fingerprint",
         lambda x: x.update(env={}), "machine"),
        ("a workload without an axis",
         lambda x: x["workloads"]["floor"].pop("text_bytes"),
         "carries no text_bytes"),
        ("a workload with a negative axis",
         lambda x: x["workloads"].update(
             floor=synth_workload(d=-1)), "non-negative"),
    ]
    for name, mutate, expect in variants:
        r2 = json.loads(base_line)
        mutate(r2)
        cases.append((name, with_line(r2), expect))

    r3 = json.loads(base_line)
    r3["instrument"] = "stage0"
    r3["workloads"]["hlc_self"] = synth_workload()
    cases.append(("an hlc_self row on the stage0 instrument",
                  with_line(r3), "native instrument"))

    s = {"type": "sanction", "tool": "hls-size", "version": "0.157.0",
         "workload": "floor"}
    cases.append(("a sanction without a written reason",
                  with_line(s), "written reason"))

    for i, (name, line, expect) in enumerate(cases):
        h = hist_path("ledger-%d" % i)
        with open(h, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(json.loads(base_line)) + "\n")
            fh.write(line + "\n")
        p = run(SIZE + ["history", "--history", h])
        check(p.returncode == 1 and "line 2" in p.stderr
              and expect in p.stderr,
              "%s is a named refusal naming the line" % name)

    # history --workload filters records and sanctions alike.
    h = hist_path("filter")
    record_from(synth_report({"floor": synth_workload(),
                              "grid": synth_workload()}), h, sha(1))
    run(SIZE + ["sanction", "grid", "--reason", "wide by design",
                "--author", "mhieuhonda", "--history", h])
    p = run(SIZE + ["history", "--history", h])
    check(p.returncode == 0 and "2 entries" in p.stdout,
          "the ledger holds the record and the sanction")
    p = run(SIZE + ["history", "--workload", "grid",
                    "--history", h])
    out = p.stdout
    check(p.returncode == 0 and "record" in out and "sanction" in out
          and "sanction grid" in out,
          "history --workload filters records and sanctions alike")
    p = run(SIZE + ["history", "--workload", "grid", "--json",
                    "--history", h])
    entries = json.loads(p.stdout)
    check(all("grid" in e["workloads"] for e in entries
              if e["type"] == "record")
          and [e for e in entries if e["type"] == "sanction"][0]
          ["workload"] == "grid",
          "history --workload --json round-trips the filtered "
          "entries")


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
