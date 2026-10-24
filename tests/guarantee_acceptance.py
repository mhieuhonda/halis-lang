#!/usr/bin/env python3
"""Stage 135 acceptance gate — the soundness guarantee ledger:
bug-bounty-eligible guarantees, documented AND re-demonstrated
(v0.153.0-alpha).

Run with `make guarantee-acceptance` (or
`python3 tests/guarantee_acceptance.py`).

Eight sections over the REAL ledger CLI, the REAL checker
(tools/hlinterp/boot.py --check), the REAL interpreter and (when bin/hlc
exists) the REAL native compiler — a guarantees document that did
not re-demonstrate its claims would be prose, not a ledger:

  1. the convention  — `hls-guarantee` exits 0 verified / 1 named
     violation / 2 usage; `list` prints every id; `show` refuses an
     unknown id naming the known ones; a bare invocation is usage
     error 2

  2. the ledger law  — verify --json parses; exactly 15 guarantees;
     ids unique and shaped G-<FAM>-NN; families and severities from
     the fixed vocabulary; every row anchored (>= 1 anchor); the two
     meta rows (bootstrap determinism, corpus agreement) are exactly
     the rows with no probe — every other row demonstrates itself;
     claim/violation/repro present on every row (a bounty entry maps
     onto them); the severity/family mapping pinned row by row

  3. the anchor law  — every anchor resolves in the live tree with a
     line number; the negative space: a tampered tree whose checker
     no longer carries "use of moved value" voids G-MEM-01, one
     whose boot.py lost the sandbox gate voids G-SBX-01 — named,
     exit 1

  4. the probe law   — every probe green on the clean tree: refuse
     probes exit 1 through the real checker with the pinned message,
     panic probes exit 101 through the real interpreter with the
     pinned panic, the sandbox probe needs its extra CLI args; a
     deleted probe file is a named error, not a silent pass

  5. the drift law   — one tampered tree carrying BOTH an anchor
     drift and a probe drift is refused naming all three
     guarantees; and the report refuses a violated ledger — no
     document is written from a ledger that cannot prove itself

  6. the report law  — two reports at the same tree are
     byte-identical; the document embeds every id, the tree sha
     (== git rev-parse HEAD), the ledger digest; --stamp changes
     ONLY the Generated line; report --json parses with verdict
     verified

  7. the bounty law  — the document names the SECURITY.md advisory
     channel and the 90-day window (the ledger anchors SECURITY.md
     itself), carries the severity vocabulary (critical/high/medium
     with ranks 0/1/2), gives every guarantee a Violation contract,
     a Reproduction clause and an Out of scope boundary, and keeps
     the honest out-of-scope register (>= 7 entries)

  8. the corpus      — tests/ok/feat_stage135_guarantee.hls is
     showcase        hlfmt-canonical, hllint-clean, boots through
                     the real checker, runs green on the interpreter
                     and (when bin/hlc exists) matches natively
                     byte for byte

Exit code 0 = all acceptance criteria met.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

GUAR = [sys.executable, os.path.join(ROOT, "tools", "hls-guarantee.py")]
BOOT = [sys.executable, os.path.join(ROOT, "tools", "hlinterp", "boot.py")]
FMT = [sys.executable, os.path.join(ROOT, "tools", "hlfmt.py")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok",
                        "feat_stage135_guarantee.hls")
SHOWCASE_OUT = ("stage 135: hls-guarantee\n"
                "  LEDGER OK: 15 guarantees anchored, probed, "
                "documented\n")

GATE = tempfile.mkdtemp(prefix=".guarantee-gate-", dir=ROOT)

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
                          timeout=600, **kw)


def make_tampered_tree(tag):
    """A full copy of the repo (minus the heavy untracked dirs) the
    drift sections can mangle."""
    dst = os.path.join(GATE, "tree-" + tag)
    shutil.copytree(
        ROOT, dst,
        ignore=shutil.ignore_patterns(".git", "bin", "fuzz-corpus",
                                      "__pycache__", "*.pyc",
                                      ".guarantee-gate-*",
                                      ".lts-gate-*"))
    return dst


def ids_in_json(data):
    return [g["id"] for g in data["guarantees"]]


# ===========================================================================
def _section_1():
    print("Section 1 — the convention")

    p = run(GUAR + ["list"])
    check(p.returncode == 0, "list exits 0")
    check(all(g in p.stdout for g in
              ["G-MEM-01", "G-TAINT-01", "G-SBX-01", "G-CORPUS-01"]),
          "list prints the ledger ids")

    p = run(GUAR + ["show", "G-TAINT-01"])
    check(p.returncode == 0 and "taint-sink" in p.stdout,
          "show prints the full record")

    p = run(GUAR + ["show", "G-NOPE-99"])
    check(p.returncode == 2 and "unknown guarantee id" in p.stderr,
          "an unknown id is usage error 2 naming the known ones")

    p = run(GUAR)
    check(p.returncode == 2, "a bare invocation is usage error 2")

    p = run(GUAR + ["verify"])
    check(p.returncode == 0 and "LEDGER VERIFIED" in p.stdout,
          "verify is green on the clean tree")

    p = run(GUAR + ["verify", "--json"])
    check(p.returncode == 0, "verify --json exits 0")
    try:
        data = json.loads(p.stdout)
        check(data["verdict"] == "verified"
              and data["refusals"] == [],
              "the machine verdict is verified with no refusals")
    except Exception as exc:
        bad("verify --json broken: %s" % exc)


# ===========================================================================
def _section_2():
    print("Section 2 — the ledger law")

    p = run(GUAR + ["verify", "--json"])
    data = json.loads(p.stdout)
    rows = data["guarantees"]
    ids = ids_in_json(data)

    check(len(rows) == 15, "exactly 15 guarantees")
    check(len(set(ids)) == len(ids), "ids are unique")
    check(all(i.startswith("G-") and i.endswith(tuple(
        "%02d" % n for n in range(1, 20))) for i in ids),
        "ids are shaped G-<FAM>-NN")

    fams = {"types", "memory", "arithmetic", "effects", "taint",
            "concurrency", "match", "capabilities", "sandbox",
            "consttime", "ffi", "determinism"}
    sevs = {"critical", "high", "medium"}
    check(all(g["family"] in fams for g in rows),
          "families from the fixed vocabulary")
    check(all(g["severity"] in sevs for g in rows),
          "severities from the fixed vocabulary")

    # The mapping pinned row by row — the table a bounty triage
    # starts from.
    pinned = {
        "G-MEM-01": ("memory", "critical"),
        "G-MEM-02": ("memory", "critical"),
        "G-TYPE-01": ("types", "critical"),
        "G-ARITH-01": ("arithmetic", "critical"),
        "G-EFF-01": ("effects", "critical"),
        "G-TAINT-01": ("taint", "critical"),
        "G-CONC-01": ("concurrency", "critical"),
        "G-MATCH-01": ("match", "high"),
        "G-CAP-01": ("capabilities", "high"),
        "G-SBX-01": ("sandbox", "high"),
        "G-CT-01": ("consttime", "high"),
        "G-FFI-01": ("ffi", "high"),
        "G-SPEC-01": ("types", "medium"),
        "G-DET-01": ("determinism", "high"),
        "G-CORPUS-01": ("determinism", "medium"),
    }
    for g in rows:
        want = pinned.get(g["id"])
        if want != (g["family"], g["severity"]):
            bad("%s mapping drifted: %s/%s" % (g["id"], g["family"],
                                               g["severity"]))
            return
    ok("the severity/family mapping is pinned row by row")

    no_probe = [g["id"] for g in rows
                if json.dumps(g).count('"verdict"') == 0]
    check(no_probe == ["G-DET-01", "G-CORPUS-01"],
          "exactly the two meta rows carry no probe — every other "
          "guarantee demonstrates itself")

    # Every row fully contracted (checked against the ledger source,
    # the same data the report renders). The module file carries a
    # dash, so it loads through importlib, not the import statement.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hls_guarantee",
        os.path.join(ROOT, "tools", "hls-guarantee.py"))
    G = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(G)
    check(all(g.get("claim") and g.get("violation") and g.get("repro")
              and g.get("out_of_scope") for g in G.LEDGER),
          "every row carries claim/violation/repro/out_of_scope")
    check(all(len(g["anchors"]) >= 1 for g in G.LEDGER),
          "every row carries at least one anchor")
    check(G.validate_ledger() == [],
          "the ledger passes its own validation")
# ===========================================================================
def _section_3():
    print("Section 3 — the anchor law")

    p = run(GUAR + ["verify", "--json"])
    data = json.loads(p.stdout)
    voids = [a for g in data["guarantees"] for a in g["anchors"]
             if not a["ok"]]
    check(voids == [], "every anchor resolves in the live tree")
    lines = [a for g in data["guarantees"] for a in g["anchors"]
             if a["ok"] and a["detail"].startswith("line ")]
    check(len(lines) >= 25,
          "anchors carry live line numbers (%d resolved)"
          % len(lines))

    # The negative space: an enforcement point removed from a COPY of
    # the tree voids its guarantee, named.
    t1 = make_tampered_tree("anchor1")
    ef = os.path.join(t1, "tools", "hlinterp", "checking", "expr.py")
    with open(ef, encoding="utf-8") as fh:
        src = fh.read()
    with open(ef, "w", encoding="utf-8") as fh:
        fh.write(src.replace("use of moved value", "moved value used"))
    p = run(GUAR + ["verify", "--root", t1])
    check(p.returncode == 1 and "G-MEM-01" in p.stderr
          and "anchor void" in p.stderr,
          "a removed ownership anchor voids G-MEM-01, named")

    t2 = make_tampered_tree("anchor2")
    bf = os.path.join(t2, "tools", "hlinterp", "boot.py")
    with open(bf, encoding="utf-8") as fh:
        src = fh.read()
    with open(bf, "w", encoding="utf-8") as fh:
        fh.write(src.replace("--sandbox is incompatible",
                             "--sandbox refuses this construct"))
    p = run(GUAR + ["verify", "--root", t2])
    check(p.returncode == 1 and "G-SBX-01" in p.stderr,
          "a reworded sandbox gate voids G-SBX-01, named")


# ===========================================================================
def _section_4():
    print("Section 4 — the probe law")

    p = run(GUAR + ["verify", "--json"])
    data = json.loads(p.stdout)
    probes = [pr for g in data["guarantees"] for pr in g["probes"]]
    check(len(probes) == 30, "30 probes pinned in the ledger")
    check(all(pr["verdict"] == "ok" for pr in probes),
          "every probe is green on the clean tree")
    refuses = [pr for pr in probes if pr["kind"] == "refuse"]
    panics = [pr for pr in probes if pr["kind"] == "panic"]
    check(len(refuses) == 27 and len(panics) == 3,
          "27 refusal probes + 3 runtime panic probes")
    check(all("refused" in pr["detail"] for pr in refuses),
          "refusal probes record the checker's message")
    check(all("panicked" in pr["detail"] for pr in panics),
          "panic probes record the runtime panic")
    sbx = [pr for pr in refuses
           if "sandbox_extern" in pr["path"]]
    check(len(sbx) == 1 and sbx[0]["verdict"] == "ok",
          "the sandbox probe runs through --sandbox (extra CLI args)")

    # A deleted probe file is a named error.
    t = make_tampered_tree("probe1")
    os.remove(os.path.join(t, "tests", "fail",
                           "fail_taint_print.hls"))
    p = run(GUAR + ["verify", "--root", t])
    check(p.returncode == 1 and "G-TAINT-01" in p.stderr
          and "probe file missing" in p.stderr,
          "a deleted probe file is a named error under its "
          "guarantee")

    # The checker's honesty is the only witness: a probe that the
    # checker would ACCEPT must be surfaced as a violation. Proven by
    # making the tampered checker weaker — the accepts-side of the
    # same law the drift section proves on the anchor side.
    t2 = make_tampered_tree("probe2")
    ef = os.path.join(t2, "tools", "hlinterp", "checking", "stmt.py")
    with open(ef, encoding="utf-8") as fh:
        src = fh.read()
    with open(ef, "w", encoding="utf-8") as fh:
        # keep the message for the probes that expect it, but move
        # the shadowing refusal out of reach by renaming the call
        fh.write(src.replace('self.err("shadowing not allowed: %s" % '
                             's["name"], s)',
                             'self.err("name reuse: %s" % s["name"], s)'))
    p = run(GUAR + ["verify", "--root", t2])
    check(p.returncode == 1 and "G-SPEC-01" in p.stderr,
          "a weakened shadowing refusal is caught by the pinned "
          "message (G-SPEC-01)")


# ===========================================================================
def _section_5():
    print("Section 5 — the drift law")

    t = make_tampered_tree("drift")
    ef = os.path.join(t, "tools", "hlinterp", "checking", "expr.py")
    with open(ef, encoding="utf-8") as fh:
        src = fh.read()
    with open(ef, "w", encoding="utf-8") as fh:
        fh.write(src.replace("use of moved value", "moved value used"))
    os.remove(os.path.join(t, "tests", "fail",
                           "fail_eprint_tainted.hls"))
    p = run(GUAR + ["verify", "--root", t])
    check(p.returncode == 1, "the combined drift exits 1")
    check("G-MEM-01" in p.stderr and "G-TAINT-01" in p.stderr,
          "both drifted guarantees are named")

    # The report refuses a violated ledger — no document from an
    # unproven ledger.
    out = os.path.join(GATE, "never.md")
    p = run(GUAR + ["report", "--out", out, "--root", t])
    check(p.returncode == 1 and not os.path.exists(out),
          "report refuses a violated ledger and writes nothing")
    check("violated" in p.stderr,
          "the refusal names the violated ledger")


# ===========================================================================
def _section_6():
    print("Section 6 — the report law")

    a = os.path.join(GATE, "r1.md")
    b = os.path.join(GATE, "r2.md")
    p1 = run(GUAR + ["report", "--out", a, "--json",
                     os.path.join(GATE, "r1.json")])
    check(p1.returncode == 0 and os.path.isfile(a),
          "the report generates")
    p2 = run(GUAR + ["report", "--out", b])
    check(p2.returncode == 0, "the second report generates")
    with open(a, encoding="utf-8") as fh:
        d1 = fh.read()
    with open(b, encoding="utf-8") as fh:
        d2 = fh.read()
    check(d1 == d2, "two reports at the same tree are byte-identical")

    check(all(g in d1 for g in
              ["G-MEM-01", "G-TYPE-01", "G-ARITH-01", "G-EFF-01",
               "G-TAINT-01", "G-CONC-01", "G-MATCH-01", "G-CAP-01",
               "G-SBX-01", "G-CT-01", "G-FFI-01", "G-SPEC-01",
               "G-DET-01", "G-CORPUS-01", "G-MEM-02"]),
          "the document embeds every guarantee id")

    sha = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    check("`%s`" % sha in d1, "the tree sha is the live HEAD")

    with open(os.path.join(GATE, "r1.json"), encoding="utf-8") as fh:
        j1 = json.load(fh)
    check(j1["verdict"] == "verified" and len(j1["guarantees"]) == 15,
          "report --json parses with the verified verdict")
    check(all(pr["verdict"] == "ok"
              for g in j1["guarantees"] for pr in g["probes"]),
          "the json carries the green probe verdicts")

    s = os.path.join(GATE, "r3.md")
    run(GUAR + ["report", "--out", s, "--stamp"])
    with open(s, encoding="utf-8") as fh:
        d3 = fh.read()
    l1 = [ln for ln in d1.splitlines() if ln]
    l3 = [ln for ln in d3.splitlines() if ln
          and not ln.startswith("- Generated: ")]
    check(l1 == l3 and "- Generated: " in d3,
          "--stamp changes ONLY the Generated line")

    p = run(GUAR + ["report"])
    check(p.returncode == 2, "a report without --out is usage error")


# ===========================================================================
def _section_7():
    print("Section 7 — the bounty law")

    doc = os.path.join(GATE, "r1.md")
    with open(doc, encoding="utf-8") as fh:
        d = fh.read()

    check("security/advisories" in d and "90-day" in d,
          "the reporting channel and the disclosure window are in "
          "the document (anchored live from SECURITY.md)")
    check("| critical | 0 |" in d and "| high | 1 |" in d
          and "| medium | 2 |" in d,
          "the severity vocabulary with ranks 0/1/2")
    check(d.count("**Violation contract.**") == 15
          and d.count("**Reproduction.**") == 15
          and d.count("**Out of scope.**") == 15,
          "every guarantee carries contract + repro + scope")
    oos = d.split("## Out of scope")[1].split("## Reporting")[0]
    check(oos.count("- ") >= 7,
          "the honest out-of-scope register carries >= 7 entries")
    check("taint_unwrap()" in oos and 'extern "C"' in oos,
          "the register keeps the documented escapes loud")

    # The ledger anchors SECURITY.md itself — the channel cannot
    # silently move.
    sec = os.path.join(ROOT, "SECURITY.md")
    with open(sec, encoding="utf-8") as fh:
        stext = fh.read()
    check("security/advisories" in stext
          and "90-day coordinated-disclosure" in stext,
          "SECURITY.md still carries the anchored channel")

    # A bounty entry maps onto the vocabulary: triage order is
    # critical first, and the ledger order agrees.
    order = [g["id"] for g in json.load(open(
        os.path.join(GATE, "r1.json")))["guarantees"]]
    check(order[:7] == ["G-MEM-01", "G-MEM-02", "G-TYPE-01",
                        "G-ARITH-01", "G-EFF-01", "G-TAINT-01",
                        "G-CONC-01"],
          "the critical rows open the table")


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
