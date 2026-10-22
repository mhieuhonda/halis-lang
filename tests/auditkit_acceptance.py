#!/usr/bin/env python3
"""Stage 140 acceptance gate — the independent, paid security
audit as a chained ledger (v0.158.0-alpha).

Run with `make auditkit-acceptance` (or
`python3 tests/auditkit_acceptance.py`).

Eight sections over the REAL hls-auditkit CLI, the REAL scope
(the Stage 135 guarantee ledger, live), the REAL checker
(boot/boot.py through the imported guarantee engine) and the real
git surface — an audit kit that did not run the instruments it
judges with would be a mock, not a gate:

  1. the convention  — hls-auditkit exits 0 held / 1 named
     refusal / 2 usage; a bare invocation and an unknown command
     are usage errors; readers on a missing ledger are named
     errors, never silent renders

  2. the engagement  — `engage` writes entry one chained from
     law                GENESIS with the family's arithmetic (the
     gate re-derives the chain INDEPENDENTLY of the tool's own
     reader); the scope digest pinned at signature time is the
     LIVE guarantee ledger's digest (15 guarantees); a window that
     ends before it starts, a malformed window, a second
     engagement, and a signer on their own roster are all refused

  3. the brief law   — the RFP renders on an empty world (OPEN
     SOLICITATION) and on an engaged one (the firm, the window,
     the basis); the census equals the live guarantee ledger row
     for row; two renders are byte-identical; --stamp moves only
     the Generated line; the reporting channel is anchored LIVE
     (a tree whose SECURITY.md lost the anchor refuses); the scope
     follows the tree (a 14-row ledger briefs 14 rows, the missing
     row absent)

  4. the evidence    — a pass by a roster member is INDEPENDENT,
     law                a pass by anyone else SELF-ASSESSED; every
     pass re-runs the whole scope through the real checker (the
     same engine, the same counts as hls-guarantee verify --json);
     the ledger is append-only; a FAILED pass is recorded, not
     skipped (outcome failed, the refusals verbatim, the chain
     still green) — fail-closed AND fail-recorded; and the scope
     that moved after the engagement is a fact the pass names
     (the entry records the digest it ran, the smaller tree's)

  5. the finding law — sequential ids AF-001, AF-002; off-roster
     filings refused (findings are the auditors' to file); an
     unknown severity, an unknown guarantee row, no reproducer,
     two reproducers and a reproducer that points at nothing are
     all refused naming the rule

  6. the triage law  — one verdict, one reason, one response per
     finding (a second is refused); an acceptance carries its
     remediation commit (40 hex, a commit that EXISTS — a
     well-formed sha that names nothing is refused); disputed and
     out-of-scope carry no fix (a fix on a non-acceptance is a
     contradiction); an auditor cannot triage (the response is the
     project's to give); a triage without a reason is refused

  7. the report law  — deterministic renders (two of one ledger
     byte-identical), the ledger digest embedded (the sha256 of
     the raw bytes, re-derived by the gate), the state word cross-
     checked against status --json; the independence banner says
     what the record says (an independent pass on record even
     when the latest is self-assessed; a self-run-only ledger
     renders "No independent pass on record"); the drift line
     names the moved scope; a broken chain refuses naming its
     line (an edited entry, a reordered entry), and nothing
     renders from it

  8. the corpus     — tests/ok/feat_stage140_auditkit.hls is
     showcase        hlfmt-canonical, hllint-clean, boots through
                     the real checker, runs green on the
                     interpreter and (when bin/hlc exists) matches
                     natively byte for byte

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

sys.path.insert(0, os.path.join(ROOT, "tools"))
from hltlog_parts.hltlog_common import GENESIS, chain_of  # noqa: E402

KIT = [sys.executable, "tools/hls-auditkit.py"]
GUAR = [sys.executable, "tools/hls-guarantee.py"]
BOOT = [sys.executable, "boot/boot.py"]
FMT = [sys.executable, "tools/hlfmt.py"]
LINT = [sys.executable, "tools/hllint.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok",
                        "feat_stage140_auditkit.hls")
SHOWCASE_OUT = "all 8 auditkit laws hold\n"

GATE = tempfile.mkdtemp(prefix=".auditkit-gate-", dir=ROOT)

FIRM = "Meridian Audit Partners"
AUDITORS = ["Dana Fischer", "Ravi Chandran"]
SIGNER = "mhieuhonda"
BASIS = "fixed fee against the scope census, half at engagement"
WINDOW = "2026-11-02/2026-12-24"

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


def led(name):
    """A scratch ledger path inside the gate root."""
    return os.path.join(GATE, name, "engagement.jsonl")


def read_entries(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    entries = []
    for line in raw.decode("utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries, raw


def verify_chain(path):
    """The chain law, re-derived by the gate — not the tool's own
    reader: seq 1..n in order, prev chaining, chain_hash the
    family arithmetic over the entry minus chain_hash."""
    entries, _ = read_entries(path)
    prev = GENESIS
    for i, e in enumerate(entries, 1):
        if e["seq"] != i:
            return False, "line %d: seq %d, expected %d" % (
                i, e["seq"], i)
        if e["prev_hash"] != prev:
            return False, "line %d: prev does not chain" % i
        if e["chain_hash"] != chain_of(prev, e):
            return False, "line %d: chain_hash does not hash" % i
        prev = e["chain_hash"]
    return True, "%d entries, head %s..." % (len(entries),
                                            prev[:12])


def engage(path, by=SIGNER, window=WINDOW, basis=BASIS,
           firm=FIRM, auditors=None):
    argv = KIT + ["engage", "--ledger", path, "--firm", firm,
                  "--window", window, "--basis", basis,
                  "--by", by]
    for a in (auditors if auditors is not None else AUDITORS):
        argv += ["--auditor", a]
    return run(argv)


def mini_tree(tag, security=True, shrink=False):
    """A minimal --root tree: the guarantee tool, its SECURITY.md,
    optionally a 14-row ledger (the scope-follows-the-tree law).
    No probes run here — the mini tree exists for the brief."""
    d = os.path.join(GATE, "mini-" + tag)
    os.makedirs(os.path.join(d, "tools"), exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "tools", "hls-guarantee.py"),
                 os.path.join(d, "tools", "hls-guarantee.py"))
    if shrink:
        with open(os.path.join(d, "tools", "hls-guarantee.py"),
                  "a", encoding="utf-8") as fh:
            fh.write("\nLEDGER = LEDGER[:14]\n")
    if security:
        shutil.copy2(os.path.join(ROOT, "SECURITY.md"),
                     os.path.join(d, "SECURITY.md"))
    else:
        with open(os.path.join(d, "SECURITY.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Security policy\n(no anchored channel "
                     "here)\n")
    return d


def full_tree(tag):
    """A full copy of the repo the tamper sections can mangle."""
    dst = os.path.join(GATE, "tree-" + tag)
    shutil.copytree(
        ROOT, dst,
        ignore=shutil.ignore_patterns(
            ".git", "bin", "fuzz-corpus", "__pycache__", "*.pyc",
            ".guarantee-gate-*", ".lts-gate-*", ".perf-gate-*",
            ".hlperf", ".memdash-gate-*", ".hlmem",
            ".ctime-gate-*", ".hlctime", ".hlsize", ".hlsize-out",
            ".size-gate-*", ".auditkit-gate-*", ".hlaudit"))
    return dst


# ===========================================================================
def _section_1():
    print("Section 1 — the convention")

    p = run(KIT)
    check(p.returncode == 2, "a bare invocation is a usage error "
          "(exit 2)")
    p = run(KIT + ["no-such-command"])
    check(p.returncode == 2, "an unknown command is a usage error "
          "(exit 2)")
    p = run(KIT + ["engage"])
    check(p.returncode == 2, "engage without its terms is a usage "
          "error (exit 2)")

    missing = led("missing")
    for cmdname in ("evidence", "file", "triage", "report",
                    "status"):
        argv = KIT + [cmdname, "--ledger", missing]
        if cmdname == "file":
            argv += ["--against", "G-TAINT-01", "--severity",
                     "high", "--title", "t", "--repro-text", "t"]
        if cmdname == "triage":
            argv += ["AF-001", "--verdict", "disputed",
                     "--reason", "r"]
        if cmdname == "report":
            argv += ["--out", os.path.join(GATE, "d.md")]
        p = run(argv)
        check(p.returncode == 1 and "no engagement ledger" in p.stderr,
              "%s on a missing ledger is a named error" % cmdname)

    # brief is the exception by design: solicitation precedes
    # contract, so the RFP renders on an empty world.
    p = run(KIT + ["brief", "--ledger", missing])
    check(p.returncode == 0 and "OPEN SOLICITATION" in p.stdout,
          "brief on a missing ledger is the RFP, open solicitation")


# ===========================================================================
def _section_2():
    print("Section 2 — the engagement law")

    h = led("engage")
    p = engage(h)
    check(p.returncode == 0 and "engaged" in p.stdout,
          "engage signs on a fresh ledger")

    entries, _ = read_entries(h)
    check(len(entries) == 1, "exactly one entry was written")
    e = entries[0]
    check(e["kind"] == "engagement", "the entry is an engagement")
    check(e["seq"] == 1, "the engagement is entry one")
    check(e["prev_hash"] == GENESIS,
          "entry one chains from GENESIS, 64 zeros")
    check(e["roster"] == AUDITORS, "the roster is recorded in "
          "order, verbatim")
    check(e["window"] == WINDOW and e["basis"] == BASIS
          and e["firm"] == FIRM,
          "the firm, the window and the basis are recorded verbatim")
    check(e["by"] == SIGNER, "the signer is recorded")
    good, detail = verify_chain(h)
    check(good, "the chain law holds, re-derived by the gate (%s)"
          % detail)

    # The scope pinned at signature time is the LIVE guarantee
    # ledger's digest — the gate cross-checks through the tool's
    # own JSON, not through auditkit.
    p = run(GUAR + ["list", "--json"])
    live = json.loads(p.stdout)["ledger_sha256"]
    check(e["scope_sha256"] == live,
          "the engagement pins the live guarantee ledger digest")
    check(json.loads(run(GUAR + ["list", "--json"]).stdout)
          ["guarantees"].__len__() == 15,
          "the scope is the 15 guarantees of Stage 135")

    for window, why in (
            ("2026-12-24/2026-11-02", "a window that ends before "
             "it starts"),
            ("2026-11-02", "a window that is not two dates"),
            ("2026-13-45/2026-12-24", "a window that is not ISO")):
        p = engage(led("win-%d" % len(why)), window=window)
        check(p.returncode == 1 and "window" in p.stderr,
              "%s is refused at the signature" % why)

    p = engage(h)
    check(p.returncode == 1 and "already on record" in p.stderr,
          "a second engagement is refused — a contract is not a "
          "draft")

    p = engage(led("selfsig"), by="Dana Fischer",
               auditors=["Dana Fischer", "Ravi Chandran"])
    check(p.returncode == 1 and "own roster" in p.stderr,
          "a signer on their own roster is refused — independence "
          "starts at the signature")

    p = engage(led("noroster"), auditors=[])
    check(p.returncode == 1 and "monologue" in p.stderr,
          "an empty roster is refused — an audit with no auditors "
          "is a monologue")


# ===========================================================================
def _section_3():
    print("Section 3 — the brief law")

    h = led("brief")
    engage(h)

    b1 = os.path.join(GATE, "brief-1.md")
    b2 = os.path.join(GATE, "brief-2.md")
    p = run(KIT + ["brief", "--ledger", h, "--out", b1])
    check(p.returncode == 0 and "15 guarantees" in p.stdout,
          "brief renders from the live tree")
    doc = open(b1, encoding="utf-8").read()

    p = run(GUAR + ["list", "--json"])
    live = json.loads(p.stdout)
    ids = [g["id"] for g in live["guarantees"]]
    check(all(("| %s |" % gid) in doc for gid in ids),
          "every guarantee id appears in the census table")
    check(("`sha256:%s`" % live["ledger_sha256"]) in doc,
          "the scope digest embedded is the live ledger digest")
    check(("window %s" % WINDOW) in doc and FIRM in doc
          and BASIS in doc,
          "the engaged brief names the firm, the window, the basis")
    check("90-day coordinated-disclosure" in doc
          and "security/advisories" in doc,
          "the reporting channel is quoted in the brief")

    run(KIT + ["brief", "--ledger", h, "--out", b2])
    check(open(b1, "rb").read() == open(b2, "rb").read(),
          "two renders of one tree are byte-identical")

    b3 = os.path.join(GATE, "brief-3.md")
    run(KIT + ["brief", "--ledger", h, "--out", b3, "--stamp"])
    d2 = open(b3, encoding="utf-8").read().splitlines()
    d1 = open(b1, encoding="utf-8").read().splitlines()
    only_gen = [ln for ln in d2 if ln not in d1]
    check(only_gen == ["- Generated: " + ln.split(": ", 1)[1]
                       for ln in d2 if ln.startswith("- Generated")],
          "--stamp changes ONLY the Generated line")

    # The channel anchor is LIVE: a tree whose SECURITY.md lost it
    # refuses to brief.
    gone = mini_tree("nochan", security=False)
    p = run(KIT + ["brief", "--ledger", h, "--root", gone])
    check(p.returncode == 1
          and "reporting channel anchor is gone" in p.stderr,
          "a tree that lost the anchored channel refuses to brief")

    # The scope follows the tree, not the docs: a 14-row ledger
    # briefs 14 rows and the missing row is absent.
    shrunk = mini_tree("shrunk", shrink=True)
    b4 = os.path.join(GATE, "brief-4.md")
    p = run(KIT + ["brief", "--ledger", h, "--root", shrunk,
                   "--out", b4])
    doc4 = open(b4, encoding="utf-8").read()
    check(p.returncode == 0 and "14 guarantees" in p.stdout,
          "a 14-row ledger briefs a 14-row scope")
    check("G-CORPUS-01" not in doc4 and "G-DET-01" in doc4,
          "the dropped row is absent, the rest present")

    # ... and the same shrunk tree's digest differs from the
    # engagement's — the drift the report must name later.
    p = run(KIT + ["brief", "--ledger", h, "--root", shrunk,
                   "--out", b4])
    eng_digest = read_entries(h)[0][0]["scope_sha256"]
    shrunk_digest_line = [ln for ln in
                          open(b4, encoding="utf-8").read()
                          .splitlines()
                          if ln.startswith("- Scope digest:")]
    check(shrunk_digest_line
          and shrunk_digest_line[0] != (
              "- Scope digest: `sha256:%s`" % eng_digest),
          "the shrunk tree's scope digest differs from the "
          "engagement's")

# ===========================================================================
def _section_4():
    print("Section 4 — the evidence law")

    h = led("life")
    engage(h)
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()

    # The same engine, the same counts: the gate cross-checks the
    # auditkit census against hls-guarantee verify --json.
    p = run(GUAR + ["verify", "--json"])
    gv = json.loads(p.stdout)
    g_ok = sum(1 for r in gv["guarantees"] if r["status"] == "verified")
    g_anchors = sum(1 for r in gv["guarantees"]
                     for a in r["anchors"] if a["ok"])
    g_probes = sum(1 for r in gv["guarantees"]
                   for p2 in r["probes"] if p2["verdict"] == "ok")

    p = run(KIT + ["evidence", "--ledger", h, "--by", AUDITORS[0]])
    check(p.returncode == 0 and "INDEPENDENT" in p.stdout,
          "a pass by a roster member is labelled INDEPENDENT")
    e = read_entries(h)[0][-1]
    check(e["kind"] == "evidence" and e["role"] == "auditor"
          and e["independent"] is True,
          "the transcript records the auditor role and the label")
    check(e["tree"] == head, "the transcript pins the tree it ran")
    check(e["verified_guarantees"] == g_ok
          and e["anchors"] == g_anchors and e["probes"] == g_probes,
          "the counts are the guarantee engine's counts (%d/%d "
          "guarantees, %d anchors, %d probes)"
          % (g_ok, e["anchors"], g_anchors, g_probes))

    before = open(h, "rb").read()
    p = run(KIT + ["evidence", "--ledger", h, "--by", SIGNER])
    after = open(h, "rb").read()
    check(p.returncode == 0 and after.startswith(before),
          "the ledger is append-only: the bytes before are a "
          "prefix of the bytes after")
    e = read_entries(h)[0][-1]
    check(e["role"] == "project" and e["independent"] is False
          and "SELF-ASSESSED" in p.stdout,
          "a pass by the project itself is labelled SELF-ASSESSED, "
          "role project — the roster decides, never the mood")

    # A FAILED pass is recorded, not skipped: fail-closed AND
    # fail-recorded. The tampered tree drops the G-MEM-01 anchor
    # (the ownership checker's refusal message).
    broken = full_tree("broken")
    expr = os.path.join(broken, "boot", "checking", "expr.py")
    with open(expr, encoding="utf-8") as fh:
        src = fh.read()
    with open(expr, "w", encoding="utf-8") as fh:
        fh.write(src.replace("use of moved value",
                              "use of moved VALUE"))
    p = run(KIT + ["evidence", "--ledger", h, "--root", broken,
                   "--by", AUDITORS[0]])
    check(p.returncode == 1 and "EVIDENCE FAILED" in p.stderr,
          "a failed pass exits 1 naming its refusals")
    e = read_entries(h)[0][-1]
    check(e["outcome"] == "failed" and e["refusals"],
          "the failed pass is ON RECORD — a failed pass is a "
          "record, not a shame")
    check(any("G-MEM-01" in r for r in e["refusals"]),
          "the refusals name the voided guarantee")
    good, detail = verify_chain(h)
    check(good, "the chain still verifies across a failed pass "
          "(%s)" % detail)
    p = run(KIT + ["status", "--ledger", h])
    check(p.returncode == 0, "status reads a ledger that carries "
          "a failed pass without drama")

    # The scope that moved after the engagement: a pass against a
    # 14-row ledger records the digest it RAN, and the report must
    # name the drift later.
    drift = full_tree("drift")
    with open(os.path.join(drift, "tools", "hls-guarantee.py"),
              "a", encoding="utf-8") as fh:
        fh.write("\nLEDGER = LEDGER[:14]\n")
    p = run(KIT + ["evidence", "--ledger", h, "--root", drift,
                   "--by", AUDITORS[0]])
    check(p.returncode == 0, "a pass against the smaller scope "
          "still runs — drift is a fact to record, not a refusal")
    e = read_entries(h)[0][-1]
    eng = read_entries(h)[0][0]
    check(e["scope_sha256"] != eng["scope_sha256"]
          and e["total_guarantees"] == 14,
          "the pass records the digest it ran (14 rows), which is "
          "not the digest the engagement signed (15)")

    p = run(KIT + ["evidence", "--ledger", led("noeng")])
    check(p.returncode == 1 and "no engagement ledger" in p.stderr,
          "a pass without a ledger is a named error")


# ===========================================================================
def _section_5():
    print("Section 5 — the finding law")

    h = led("life")

    p = run(KIT + ["file", "--ledger", h, "--against", "G-TAINT-01",
                   "--severity", "high",
                   "--title", "taint sink accepts through a crafted alias",
                   "--repro-path", "tests/fail/fail_taint_read_file.hls",
                   "--by", AUDITORS[0]])
    check(p.returncode == 0 and "filed AF-001" in p.stdout,
          "a roster member files the first finding, AF-001")
    f = read_entries(h)[0][-1]
    check(f["repro_mode"] == "path" and f["severity"] == "high"
          and f["against"] == "G-TAINT-01",
          "the finding carries the row, the severity and the "
          "path reproducer")

    p = run(KIT + ["file", "--ledger", h, "--against", "G-CT-01",
                   "--severity", "low",
                   "--title", "const-time margin thinner than claimed",
                   "--repro-text",
                   "echo 'CT lane leaks through the scheduler' && exit 1",
                   "--by", AUDITORS[1]])
    check(p.returncode == 0 and "filed AF-002" in p.stdout,
          "the second roster member files AF-002 — ids are "
          "sequential")
    f = read_entries(h)[0][-1]
    check(f["repro_mode"] == "text",
          "an inline reproducer is filed as text")

    p = run(KIT + ["file", "--ledger", h, "--against", "G-CT-01",
                   "--severity", "low", "--title", "x",
                   "--repro-text", "x", "--by", SIGNER])
    check(p.returncode == 1
          and "findings are the auditors' to file" in p.stderr,
          "the project cannot author a finding")

    p = run(KIT + ["file", "--ledger", h, "--against", "G-CT-01",
                   "--severity", "severe", "--title", "x",
                   "--repro-text", "x", "--by", AUDITORS[0]])
    check(p.returncode == 1 and "severity" in p.stderr,
          "a severity outside the closed vocabulary is refused")

    p = run(KIT + ["file", "--ledger", h, "--against", "G-NOPE-99",
                   "--severity", "low", "--title", "x",
                   "--repro-text", "x", "--by", AUDITORS[0]])
    check(p.returncode == 1 and "not one of the scope's" in p.stderr,
          "a finding against a row outside the scope is refused "
          "naming the known rows")

    p = run(KIT + ["file", "--ledger", h, "--against", "G-CT-01",
                   "--severity", "low", "--title", "x",
                   "--by", AUDITORS[0]])
    check(p.returncode == 1 and "opinions are not findings" in p.stderr,
          "a finding with no reproducer is an opinion")

    p = run(KIT + ["file", "--ledger", h, "--against", "G-CT-01",
                   "--severity", "low", "--title", "x",
                   "--repro-path", "a.hls", "--repro-text", "x",
                   "--by", AUDITORS[0]])
    check(p.returncode == 1 and "ONE reproducer, not two" in p.stderr,
          "a finding carries one reproducer, never two")

    p = run(KIT + ["file", "--ledger", h, "--against", "G-CT-01",
                   "--severity", "low", "--title", "x",
                   "--repro-path", "no/such/file.hls",
                   "--by", AUDITORS[0]])
    check(p.returncode == 1 and "points at nothing" in p.stderr,
          "a reproducer that points at nothing is a rumor")


# ===========================================================================
def _section_6():
    print("Section 6 — the triage law")

    h = led("life")
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()

    p = run(KIT + ["triage", "AF-001", "--ledger", h,
                   "--verdict", "accepted",
                   "--reason", "the alias route is real; fixed at "
                   "the checker boundary",
                   "--fix", head, "--by", SIGNER])
    check(p.returncode == 0 and "triaged AF-001: accepted" in p.stdout,
          "the project accepts with its remediation commit")
    t = read_entries(h)[0][-1]
    check(t["fix"] == head and t["verdict"] == "accepted",
          "the triage records the fix sha verbatim")

    p = run(KIT + ["triage", "AF-001", "--ledger", h,
                   "--verdict", "disputed", "--reason", "x",
                   "--by", SIGNER])
    check(p.returncode == 1 and "one finding, one response" in p.stderr,
          "a second triage is refused — the record is the "
          "response, not a negotiation")

    p = run(KIT + ["triage", "AF-002", "--ledger", h,
                   "--verdict", "accepted", "--reason", "x",
                   "--by", SIGNER])
    check(p.returncode == 1 and "wish, not a fix" in p.stderr,
          "an acceptance without its remediation is a wish")

    p = run(KIT + ["triage", "AF-002", "--ledger", h,
                   "--verdict", "accepted", "--reason", "x",
                   "--fix", "not-a-sha", "--by", SIGNER])
    check(p.returncode == 1 and "40 hex" in p.stderr,
          "the fix must be 40 hex")

    p = run(KIT + ["triage", "AF-002", "--ledger", h,
                   "--verdict", "accepted", "--reason", "x",
                   "--fix", "f" * 40, "--by", SIGNER])
    check(p.returncode == 1 and "names no commit" in p.stderr,
          "a well-formed sha that names no commit is refused — a "
          "remediation that exists nowhere is a promise")

    p = run(KIT + ["triage", "AF-002", "--ledger", h,
                   "--verdict", "disputed", "--reason", "x",
                   "--fix", head, "--by", SIGNER])
    check(p.returncode == 1 and "only an acceptance carries a fix"
          in p.stderr,
          "a disputed finding with a fix is a contradiction")

    p = run(KIT + ["triage", "AF-002", "--ledger", h,
                   "--verdict", "disputed", "--reason", "",
                   "--by", SIGNER])
    check(p.returncode == 1 and "sanction law" in p.stderr,
          "a triage without a reason is refused — the sanction "
          "law, on the other side of the table")

    p = run(KIT + ["triage", "AF-002", "--ledger", h,
                   "--verdict", "rejected", "--reason", "x",
                   "--by", SIGNER])
    check(p.returncode == 1 and "not one of" in p.stderr,
          "a verdict outside the closed vocabulary is refused")

    p = run(KIT + ["triage", "AF-099", "--ledger", h,
                   "--verdict", "disputed", "--reason", "x",
                   "--by", SIGNER])
    check(p.returncode == 1 and "no finding AF-099" in p.stderr,
          "a triage on a finding that is not on record is refused")

    p = run(KIT + ["triage", "AF-002", "--ledger", h,
                   "--verdict", "disputed",
                   "--reason", "the lane is measured, not guessed",
                   "--by", AUDITORS[1]])
    check(p.returncode == 1 and "the project's to give" in p.stderr,
          "an auditor cannot triage — the response is the "
          "project's to give")

    p = run(KIT + ["triage", "AF-002", "--ledger", h,
                   "--verdict", "disputed",
                   "--reason", "the lane is measured, not guessed",
                   "--by", SIGNER])
    check(p.returncode == 0 and "triaged AF-002: disputed" in p.stdout,
          "the project disputes with a written reason")

    good, detail = verify_chain(h)
    check(good, "the chain law holds over the whole life ledger "
          "(%s)" % detail)

# ===========================================================================
def _section_7():
    print("Section 7 — the report law")

    h = led("life")
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()

    # One more pass, by the project, AFTER the auditors' own: the
    # newest is self-assessed while an independent one is on
    # record — the banner must say both, honestly.
    run(KIT + ["evidence", "--ledger", h, "--by", SIGNER])

    r1 = os.path.join(GATE, "rep-1.md")
    r2 = os.path.join(GATE, "rep-2.md")
    p = run(KIT + ["report", "--ledger", h, "--out", r1])
    check(p.returncode == 0 and "state remediated" in p.stdout,
          "report renders the deliverable")
    doc = open(r1, encoding="utf-8").read()
    raw = open(h, "rb").read()
    digest = hashlib.sha256(raw).hexdigest()
    check(("`sha256:%s`" % digest) in doc,
          "the report embeds the ledger digest, re-derived by the "
          "gate from the raw bytes")

    p = run(KIT + ["report", "--ledger", h, "--out", r2])
    check(open(r1, "rb").read() == open(r2, "rb").read(),
          "two renders of one ledger are byte-identical")

    r3 = os.path.join(GATE, "rep-3.md")
    run(KIT + ["report", "--ledger", h, "--out", r3, "--stamp"])
    d2 = open(r3, encoding="utf-8").read().splitlines()
    d1 = open(r1, encoding="utf-8").read().splitlines()
    check([ln for ln in d2 if ln not in d1]
          == [ln for ln in d2 if ln.startswith("- Generated")],
          "--stamp changes ONLY the Generated line")

    check("Scope drift: **MOVED**" in doc,
          "the report names the scope that moved under the "
          "auditors")
    check("the last pass that ran a different scope" in doc,
          "naming the last pass that ran a different scope")
    check("**Independent pass on record:**" in doc
          and "latest such pass, by the auditors' own roster."
          in doc,
          "an independent pass is on record and the banner says so")
    check("The latest pass overall was run by the" in doc
          and "project itself (SELF-ASSESSED)" in doc,
          "and the newest self-run pass is named as such — both "
          "records stand, each labelled by who ran it")
    check("No independent pass on record" not in doc,
          "a ledger WITH an independent pass never renders the "
          "empty banner")
    check(("| AF-001 | high | G-TAINT-01 |") in doc
          and ("| AF-002 | low | G-CT-01 |") in doc,
          "the findings table renders both findings")
    check(("| AF-001 | accepted |") in doc and head in doc,
          "the triage table renders the acceptance and its fix")
    check(("| AF-002 | disputed |") in doc,
          "and the dispute, with its written reason")

    p = run(KIT + ["status", "--ledger", h, "--json"])
    st = json.loads(p.stdout)
    check(st["state"] == "remediated" and st["findings"]["total"] == 2
          and st["triage"]["accepted"] == 1 and st["fixes"] == 1,
          "status --json agrees with the census")
    check(("**REMEDIATED.**" in doc),
          "the report's state word is the derived one")
    check(st["evidence"]["independent"] >= 1
          and "SELF-ASSESSED" in doc,
          "the census counts independent and self-assessed passes")

    # A self-run-only ledger: the honest banner.
    solo = led("solo")
    engage(solo)
    run(KIT + ["evidence", "--ledger", solo, "--by", SIGNER])
    s1 = os.path.join(GATE, "solo-1.md")
    s2 = os.path.join(GATE, "solo-2.md")
    run(KIT + ["report", "--ledger", solo, "--out", s1])
    run(KIT + ["report", "--ledger", solo, "--out", s2])
    sdoc = open(s1, encoding="utf-8").read()
    check("No independent pass on record" in sdoc,
          "a self-run-only ledger renders the empty banner")
    check("INDEPENDENT" not in sdoc.replace(
        "INDEPENDENT — no contract on record", ""),
          "no pass on such a ledger is ever labelled independent")
    check("No findings on record" in sdoc
          and "stays underway" in sdoc,
          "a clean audit honestly stays underway")
    check(open(s1, "rb").read() == open(s2, "rb").read(),
          "the solo report is deterministic too")

    # The broken chain: an edited entry, a reordered entry — named
    # at their line, and nothing renders or appends from them.
    def write_lines(path, lines):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    edited = led("tamp-edited")
    lines = raw.decode("utf-8").splitlines()
    write_lines(edited, [lines[1].replace('"Dana Fischer"',
                                          '"Dana T. Fischer"')
                         if i == 1 else ln
                         for i, ln in enumerate(lines)])
    p = run(KIT + ["report", "--ledger", edited,
                   "--out", os.path.join(GATE, "no.md")])
    check(p.returncode == 1 and "line 2" in p.stderr
          and "edited after it was written" in p.stderr,
          "an entry edited after it was written is named at its "
          "line, and nothing renders from it")
    p = run(KIT + ["status", "--ledger", edited])
    check(p.returncode == 1, "status refuses a broken chain")
    p = run(KIT + ["evidence", "--ledger", edited,
                   "--by", AUDITORS[0]])
    check(p.returncode == 1,
          "an appender refuses a broken chain — nothing writes "
          "onto a chain it could not verify")

    reordered = led("tamp-reordered")
    swapped = list(lines)
    swapped[1], swapped[2] = swapped[2], swapped[1]
    write_lines(reordered, swapped)
    p = run(KIT + ["report", "--ledger", reordered,
                   "--out", os.path.join(GATE, "no2.md")])
    check(p.returncode == 1 and "line 2" in p.stderr
          and "hole or a lie" in p.stderr,
          "a reordered ledger is a hole or a lie, named at its line")


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
                      r2.stderr.strip()[:120])
            else:
                rn = run([bin_path])
                check(rn.returncode == 0
                      and rn.stdout == SHOWCASE_OUT,
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
