#!/usr/bin/env python3
"""Stage 144 acceptance gate — the FINAL independent third-party
security audit, the engagement that gates the v1.0 release
candidates (v0.165.0-alpha).

Run with `make finalaudit-acceptance` (or
`python3 tests/finalaudit_acceptance.py`).

Nine sections over the REAL hls-finalaudit CLI, the REAL scope
(the Stage 135 guarantee ledger, live), the REAL anchors (the
Stage 141 freeze document, the Stage 142/143 snapshot manifest),
the REAL checker (tools/hlinterp/boot.py through the imported
guarantee engine) and the real git surface — an audit kit that
did not run the instruments it judges with would be a mock, not
a gate:

  1. the convention  — hls-finalaudit exits 0 held / 1 named
     refusal / 2 usage; a bare invocation and an unknown command
     are usage errors; readers on a missing ledger are named
     errors, never silent renders; the gate on a missing ledger
     names the law — a release candidate is not cut from an
     unaudited tree

  2. the engagement  — `engage` writes entry one chained from
     law                GENESIS with the family's arithmetic (the
     gate re-derives the chain INDEPENDENTLY of the tool's own
     reader); the THREE anchors pinned at signature time are the
     LIVE tree's — the guarantee ledger's digest, the freeze
     anchor re-derived by the gate's own arithmetic over the
     committed document and its amendment chain, the snapshot
     anchor over the manifest — and a tree that carries no freeze
     or no manifest cannot even be engaged; a window that ends
     before it starts, a second engagement, a signer on their
     own roster and an empty roster are all refused

  3. the brief law   — the RFP renders on an empty world (OPEN
     SOLICITATION) and on an engaged one; the three anchors are
     quoted live; two renders are byte-identical; --stamp moves
     only the Generated line; the reporting channel is anchored
     LIVE (a tree whose SECURITY.md lost the anchor refuses); the
     closure criteria section says what the signoff and the gate
     will demand

  4. the evidence    — a pass by a roster member is INDEPENDENT,
     law                a pass by anyone else SELF-ASSESSED; every
     pass re-runs the whole scope through the real checker (the
     same engine, the same counts as hls-guarantee verify --json);
     the ledger is append-only; a FAILED pass is recorded, not
     skipped (outcome failed, the refusals verbatim, the chain
     still green) — fail-closed AND fail-recorded; and the
     anchors that moved after the engagement are a fact the pass
     records (the entry names the drifted anchor)

  5. the finding law — sequential ids AF-001, AF-002; off-roster
     filings refused (findings are the auditors' to file); an
     unknown severity, an unknown guarantee row, no reproducer,
     two reproducers and a reproducer that points at nothing are
     all refused naming the rule

  6. the triage law  — one verdict, one reason, one response per
     finding (a second is refused); an acceptance carries its
     remediation commit — 40 hex that EXISTS (a well-formed sha
     that names nothing is refused), that has LANDED (a commit
     that exists but is not an ancestor of HEAD is refused — a fix
     that has not landed is a promise, not a fix), and that
     POSTDATES the finding it fixes (the scratch tree's initial
     commit predates the finding: a fix that predates its finding
     is a contradiction); disputed and out-of-scope carry no fix;
     an auditor cannot triage; a triage without a reason is
     refused

  7. the signoff     — the seal is refused when the closure laws
     law                do not hold: no independent pass (a solo
     ledger), a finding without its triage, a fix that no longer
     lands (the tree reset past it), an anchor that moved since
     the engagement (the freeze mutated under a closure-ready
     ledger), a non-git tree (the landing law cannot be read);
     an auditor cannot sign off (the closure is the project's
     act); the happy seal records the tree, the three anchors and
     the census — and after the seal EVERY writer refuses by
     name, while the readers still read

  8. the gate law    — no ledger: not cut from an unaudited
     tree; unsealed states: refused naming the state; sealed:
     GREEN; a release cut from a DESCENDANT of the sealed tree:
     GREEN; a release cut AROUND the audit (HEAD reset past the
     seal): refused; each of the three anchors mutated after the
     seal: refused naming the anchor that moved

  9. the corpus     — tests/ok/feat_stage144_finalaudit.hls is
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
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

sys.path.insert(0, os.path.join(ROOT, "tools"))
from hltlog_parts.hltlog_common import GENESIS, chain_of  # noqa: E402

KIT = [sys.executable, "tools/hls-finalaudit.py"]
GUAR = [sys.executable, "tools/hls-guarantee.py"]
BOOT = [sys.executable, "tools/hlinterp/boot.py"]
FMT = [os.path.join(ROOT, "bin", "hlfmt")]
LINT = [sys.executable, "tools/hllint.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok",
                        "feat_stage144_finalaudit.hls")
SHOWCASE_OUT = "all 8 finalaudit laws hold\n"

GATE = tempfile.mkdtemp(prefix=".finalaudit-gate-", dir=ROOT)

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
    return os.path.join(GATE, name, "final.jsonl")


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
            ".size-gate-*", ".auditkit-gate-*", ".hlaudit",
            ".finalaudit-gate-*", ".hlfa"))
    return dst


def git_tree(tag):
    """A full copy that is also a real git repository — the
    landing laws need history to read."""
    d = full_tree(tag)
    for cmd in (["git", "init", "-q", "."],
                ["git", "config", "user.name", "gate"],
                ["git", "config", "user.email", "gate@example.com"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "the tree the "
                 "audit runs on"]):
        r = run(cmd, cwd=d)
        if r.returncode != 0:
            bad("git_tree(%s): %s failed: %s"
                % (tag, cmd[1], r.stderr.strip()[:120]))
            return d
    return d


def git_head(tree):
    return run(["git", "rev-parse", "HEAD"], cwd=tree).stdout.strip()


def git_initial(tree):
    return run(["git", "rev-list", "--max-parents=0", "HEAD"],
               cwd=tree).stdout.strip()


def git_empty_commit(tree, msg):
    r = run(["git", "commit", "-q", "--allow-empty", "-m", msg],
            cwd=tree)
    assert r.returncode == 0, r.stderr
    return git_head(tree)


def engage(path, by=SIGNER, window=WINDOW, basis=BASIS,
           firm=FIRM, auditors=None, root=None):
    argv = KIT + ["engage", "--ledger", path, "--firm", firm,
                  "--window", window, "--basis", basis,
                  "--by", by]
    for a in (auditors if auditors is not None else AUDITORS):
        argv += ["--auditor", a]
    if root:
        argv += ["--root", root]
    return run(argv)


def canon_sha256(obj):
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def live_anchor_digests():
    """The three anchors, re-derived by the gate's OWN arithmetic
    over the committed files — not through the tool's code."""
    with open(os.path.join(ROOT, "tests", "api_freeze.json"),
              encoding="utf-8") as fh:
        freeze_digest = json.load(fh)["digest"]
    head = GENESIS
    am_path = os.path.join(ROOT, "tests",
                           "api_freeze_amendments.jsonl")
    if os.path.isfile(am_path):
        with open(am_path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().split("\n") if ln.strip()]
        prev = GENESIS
        for i, line in enumerate(lines, 1):
            e = json.loads(line)
            assert e["seq"] == i and e["prev_hash"] == prev \
                and e["chain_hash"] == chain_of(prev, e)
            prev = e["chain_hash"]
        head = prev
    with open(os.path.join(ROOT, "snapshot", "MANIFEST.json"),
              encoding="utf-8") as fh:
        man = json.load(fh)
    g = json.loads(run(GUAR + ["list", "--json"]).stdout)
    return {
        "guarantee-scope": g["ledger_sha256"],
        "api-freeze": canon_sha256({"digest": freeze_digest,
                                    "amendments_head": head}),
        "bootstrap-snapshot": canon_sha256({
            "closure": man["closure_digest"],
            "snapshot": man["hlc_c_sha256"],
            "version": str(man["version"])}),
    }


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
    for cmdname in ("evidence", "file", "triage", "signoff",
                    "report", "status"):
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
        check(p.returncode == 1
              and "no final-audit ledger" in p.stderr,
              "%s on a missing ledger is a named error" % cmdname)

    # brief is the exception by design: solicitation precedes
    # contract, so the RFP renders on an empty world.
    p = run(KIT + ["brief", "--ledger", missing])
    check(p.returncode == 0 and "OPEN SOLICITATION" in p.stdout,
          "brief on a missing ledger is the RFP, open solicitation")

    # the gate's missing-ledger refusal names its own law.
    p = run(KIT + ["gate", "--ledger", missing])
    check(p.returncode == 1
          and "not cut from an unaudited tree" in p.stderr,
          "the gate on a missing ledger: a release candidate is "
          "not cut from an unaudited tree")


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

    # The THREE anchors pinned at signature time are the LIVE
    # tree's — re-derived by the gate's own arithmetic, not the
    # tool's.
    live = live_anchor_digests()
    for name, digest in live.items():
        check(e["anchors"].get(name) == digest,
              "the engagement pins the live %s anchor" % name)
    check(set(e["anchors"]) == set(live),
          "the engagement pins exactly the three anchors — no "
          "more, no less")

    p = run(GUAR + ["list", "--json"])
    check(len(json.loads(p.stdout)["guarantees"]) == 15,
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

    # A tree that carries no freeze (or no manifest) cannot be
    # engaged at all: the final audit audits the frozen artifact.
    mini = os.path.join(GATE, "mini-nofreeze")
    os.makedirs(os.path.join(mini, "tools"), exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "tools", "hls-guarantee.py"),
                 os.path.join(mini, "tools", "hls-guarantee.py"))
    shutil.copy2(os.path.join(ROOT, "SECURITY.md"),
                 os.path.join(mini, "SECURITY.md"))
    p = engage(led("nofreeze"), root=mini)
    check(p.returncode == 1 and "no API freeze" in p.stderr,
          "a tree that carries no freeze cannot be engaged — the "
          "final audit audits the frozen artifact")

    nomft = full_tree("nomft")
    os.remove(os.path.join(nomft, "snapshot", "MANIFEST.json"))
    p = engage(led("nomft"), root=nomft)
    check(p.returncode == 1 and "no snapshot manifest" in p.stderr,
          "a tree that carries no snapshot manifest cannot be "
          "engaged either")
    shutil.rmtree(nomft, ignore_errors=True)


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

    live = live_anchor_digests()
    for name, digest in live.items():
        check(("| %s |" % name) in doc and
              ("`sha256:%s`" % digest) in doc,
              "the brief quotes the live %s anchor with its "
              "digest" % name)

    check(("window %s" % WINDOW) in doc and FIRM in doc
          and BASIS in doc,
          "the engaged brief names the firm, the window, the basis")

    check("the v1.0 release candidates" in doc
          and "closure criteria" in doc.lower(),
          "the brief says what this engagement gates and what its "
          "closure demands")

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
    gone = os.path.join(GATE, "mini-nochan")
    os.makedirs(os.path.join(gone, "tools"), exist_ok=True)
    shutil.copy2(os.path.join(ROOT, "tools", "hls-guarantee.py"),
                 os.path.join(gone, "tools", "hls-guarantee.py"))
    with open(os.path.join(gone, "SECURITY.md"), "w",
              encoding="utf-8") as fh:
        fh.write("# Security policy\n(no anchored channel "
                 "here)\n")
    p = run(KIT + ["brief", "--ledger", h, "--root", gone])
    check(p.returncode == 1
          and "reporting channel anchor is gone" in p.stderr,
          "a tree that lost the anchored channel refuses to brief")
    shutil.rmtree(gone, ignore_errors=True)


# ===========================================================================
def _section_4():
    print("Section 4 — the evidence law")

    h = led("life-real")
    engage(h)
    head = git_head(ROOT)

    # The same engine, the same counts: the gate cross-checks the
    # finalaudit census against hls-guarantee verify --json.
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
          and e["guarantee_anchors"] == g_anchors
          and e["guarantee_probes"] == g_probes,
          "the counts are the guarantee engine's counts (%d/%d "
          "guarantees, %d anchors, %d probes)"
          % (g_ok, e["guarantee_anchors"], g_anchors, g_probes))
    check(e["anchor_drift"] == [],
          "the transcript records no drift on an unmoved tree")

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
    expr = os.path.join(broken, "tools", "hlinterp", "checking", "expr.py")
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
    shutil.rmtree(broken, ignore_errors=True)

    # The anchors that moved after the engagement: a pass against
    # a mutated freeze RECORDS the drift — a fact, not a refusal.
    drift = full_tree("drift4")
    fz = os.path.join(drift, "tests", "api_freeze.json")
    with open(fz, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["digest"] = "f" * 64
    with open(fz, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    p = run(KIT + ["evidence", "--ledger", h, "--root", drift,
                   "--by", AUDITORS[0]])
    check(p.returncode == 0, "a pass against the moved anchor "
          "still runs — drift is a fact to record, not a refusal")
    e = read_entries(h)[0][-1]
    check(e["anchor_drift"] == ["api-freeze"],
          "the pass records the drifted anchor by name")
    check("drift: api-freeze" in p.stdout,
          "and says so on the record it prints")
    shutil.rmtree(drift, ignore_errors=True)

    p = run(KIT + ["evidence", "--ledger", led("noeng")])
    check(p.returncode == 1 and "no final-audit ledger" in p.stderr,
          "a pass without a ledger is a named error")


# ===========================================================================
def _section_5():
    print("Section 5 — the finding law")

    h = led("life-real")
    entries, _ = read_entries(h)
    check(any(e["kind"] == "engagement" for e in entries),
          "the finding law runs on the section-4 ledger, engaged")
    if not any(e["kind"] == "finding" for e in entries):
        p = run(KIT + ["file", "--ledger", h,
                       "--against", "G-TAINT-01",
                       "--severity", "high",
                       "--title", "taint sink accepts through a "
                       "crafted alias",
                       "--repro-path",
                       "tests/fail/fail_taint_read_file.hls",
                       "--by", AUDITORS[0]])
        check(p.returncode == 0 and "filed AF-001" in p.stdout,
              "a roster member files the first finding, AF-001")
        f = read_entries(h)[0][-1]
        check(f["repro_mode"] == "path" and f["severity"] == "high"
              and f["against"] == "G-TAINT-01",
              "the finding carries the row, the severity and the "
              "path reproducer")

        p = run(KIT + ["file", "--ledger", h, "--against", "G-CT-01",
                       "--severity", "info",
                       "--title", "const-time margin thinner than "
                       "claimed",
                       "--repro-text",
                       "echo 'CT lane leaks through the scheduler' "
                       "&& exit 1",
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
    print("Section 6 — the triage law (the landing law)")

    tree = git_tree("life")
    h = os.path.join(tree, "final.jsonl")

    def fa(*argv):
        return run(KIT + list(argv) + ["--root", tree,
                                       "--ledger", h])

    p = fa("engage", "--firm", FIRM,
           "--window", WINDOW, "--basis", BASIS, "--by", SIGNER,
           "--auditor", AUDITORS[0], "--auditor", AUDITORS[1])
    check(p.returncode == 0, "the lifecycle engages in a real git "
          "tree — the landing law needs history to read")

    p = fa("evidence", "--by", AUDITORS[0])
    check(p.returncode == 0 and "INDEPENDENT" in p.stdout,
          "the auditors' own pass, on the record")

    p = fa("file", "--against", "G-TAINT-01", "--severity", "high",
           "--title", "taint sink accepts through a crafted alias",
           "--repro-path", "tests/fail/fail_taint_read_file.hls",
           "--by", AUDITORS[0])
    check(p.returncode == 0 and "filed AF-001" in p.stdout,
          "the auditors file AF-001")

    initial = git_initial(tree)

    # The landing law, one refusal at a time — every one of these
    # is a promise, not a fix:
    p = fa("triage", "AF-001", "--verdict", "accepted",
           "--reason", "r")
    check(p.returncode == 1 and "wish, not a fix" in p.stderr,
          "an acceptance without its remediation is a wish")

    p = fa("triage", "AF-001", "--verdict", "accepted",
           "--reason", "r", "--fix", "not-a-sha")
    check(p.returncode == 1 and "40 hex" in p.stderr,
          "the fix must be 40 hex")

    p = fa("triage", "AF-001", "--verdict", "accepted",
           "--reason", "r", "--fix", "f" * 40)
    check(p.returncode == 1 and "names no commit" in p.stderr,
          "a well-formed sha that names no commit is refused — a "
          "remediation that exists nowhere is a promise")

    dangling = run(["git", "commit-tree", "HEAD^{tree}", "-m",
                    "a commit that never landed"], cwd=tree
                   ).stdout.strip()
    p = fa("triage", "AF-001", "--verdict", "accepted",
           "--reason", "r", "--fix", dangling)
    check(p.returncode == 1
          and "not in the tree being released" in p.stderr
          and "has not landed" in p.stderr,
          "a commit that exists but is not an ancestor of HEAD is "
          "refused — a fix that has not landed is a promise, not a "
          "fix")

    p = fa("triage", "AF-001", "--verdict", "accepted",
           "--reason", "r", "--fix", initial)
    check(p.returncode == 1 and "predates the finding" in p.stderr
          and "contradiction" in p.stderr,
          "the tree's initial commit predates the finding — a fix "
          "that predates its finding is a contradiction")

    p = fa("triage", "AF-001", "--verdict", "disputed",
           "--reason", "r", "--fix", initial)
    check(p.returncode == 1 and "only an acceptance carries a fix"
          in p.stderr,
          "a disputed finding with a fix is a contradiction")

    p = fa("triage", "AF-001", "--verdict", "disputed",
           "--reason", "")
    check(p.returncode == 1 and "sanction law" in p.stderr,
          "a triage without a reason is refused — the sanction "
          "law, on the other side of the table")

    p = fa("triage", "AF-001", "--verdict", "disputed",
           "--reason", "r", "--by", AUDITORS[1])
    check(p.returncode == 1 and "the project's to give" in p.stderr,
          "an auditor cannot triage — the response is the "
          "project's to give")

    p = fa("triage", "AF-099", "--verdict", "disputed",
           "--reason", "r")
    check(p.returncode == 1 and "no finding AF-099" in p.stderr,
          "a triage on a finding that is not on record is refused")

    # The happy path: commit the fix AFTER the finding (a real
    # empty commit on the real tree), then accept with it.
    time.sleep(1.1)
    fix = git_empty_commit(tree, "fix: the alias route at the "
                              "checker boundary")
    p = fa("triage", "AF-001", "--verdict", "accepted",
           "--reason", "the alias route is real; fixed at the "
           "checker boundary", "--fix", fix)
    check(p.returncode == 0 and "triaged AF-001: accepted" in p.stdout,
          "the project accepts with the remediation commit that "
          "landed — and postdates the finding")
    t = read_entries(h)[0][-1]
    check(t["fix"] == fix and t["verdict"] == "accepted",
          "the triage records the fix sha verbatim")

    p = fa("triage", "AF-001", "--verdict", "disputed",
           "--reason", "x")
    check(p.returncode == 1 and "one finding, one response" in p.stderr,
          "a second triage is refused — the record is the "
          "response, not a negotiation")

    good, detail = verify_chain(h)
    check(good, "the chain law holds over the life ledger (%s)"
          % detail)
    return tree, h


# ===========================================================================
def _section_7(tree, h):
    print("Section 7 — the signoff law")

    def fa(*argv):
        return run(KIT + list(argv) + ["--root", tree,
                                       "--ledger", h])

    p = fa("signoff", "--by", AUDITORS[0])
    check(p.returncode == 1 and "the signoff is the project's act"
          in p.stderr,
          "an auditor cannot sign off — the auditors' deliverable "
          "is the findings, not the closure")

    # A solo ledger: remediated-shaped, but no independent pass —
    # the audit does not close on the project's own evidence.
    solo = led("solo")
    engage(solo)
    run(KIT + ["evidence", "--ledger", solo, "--by", SIGNER])
    p = run(KIT + ["signoff", "--ledger", solo, "--by", SIGNER])
    check(p.returncode == 1 and "no independent pass" in p.stderr
          and "rubber stamp" in p.stderr,
          "a solo, self-assessed ledger is refused at the seal — "
          "an audit that closes on the project's own evidence is a "
          "rubber stamp with a paper trail")

    # A finding without its triage keeps the seal shut.
    untr = led("untr")
    engage(untr)
    run(KIT + ["evidence", "--ledger", untr, "--by", AUDITORS[0]])
    run(KIT + ["file", "--ledger", untr, "--against", "G-CT-01",
               "--severity", "low", "--title", "x",
               "--repro-text", "x", "--by", AUDITORS[0]])
    p = run(KIT + ["signoff", "--ledger", untr, "--by", SIGNER])
    check(p.returncode == 1 and "carries no triage" in p.stderr,
          "a finding without its triage refuses the seal")

    # A fix that no longer lands: reset the life tree past the
    # fix commit.
    fix = [e for e in read_entries(h)[0]
           if e["kind"] == "triage"][0]["fix"]
    run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=tree)
    p = fa("signoff", "--by", SIGNER)
    check(p.returncode == 1 and "not an ancestor" in p.stderr,
          "a remediation that no longer lands refuses the seal")
    run(["git", "reset", "-q", "--hard", fix], cwd=tree)

    # The drifted anchor: a closure-ready ledger against a mutated
    # freeze closes on nothing.
    drift = git_tree("drift7")
    dh = os.path.join(drift, "final.jsonl")

    def fd(*argv):
        return run(KIT + list(argv) + ["--root", drift,
                                       "--ledger", dh])

    fd("engage", "--firm", FIRM, "--window", WINDOW,
       "--basis", BASIS, "--by", SIGNER,
       "--auditor", AUDITORS[0], "--auditor", AUDITORS[1])
    fd("evidence", "--by", AUDITORS[0])
    fd("file", "--against", "G-CT-01", "--severity", "medium",
       "--title", "x", "--repro-text", "x", "--by", AUDITORS[0])
    time.sleep(1.1)
    dfx = git_empty_commit(drift, "fix: the boundary")
    fd("triage", "AF-001", "--verdict", "accepted",
       "--reason", "r", "--fix", dfx)
    fz = os.path.join(drift, "tests", "api_freeze.json")
    with open(fz, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["digest"] = "e" * 64
    with open(fz, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    p = fd("signoff", "--by", SIGNER)
    check(p.returncode == 1 and "api-freeze anchor moved" in p.stderr
          and "new engagement" in p.stderr,
          "a closure-ready ledger on a moved freeze is refused — a "
          "moved surface is a new engagement, not an amendment")

    # A non-git tree: the landing law cannot even be read.
    nogit = full_tree("nogit")
    nh = os.path.join(nogit, "final.jsonl")
    run(KIT + ["engage", "--ledger", nh, "--root", nogit,
               "--firm", FIRM, "--window", WINDOW, "--basis", BASIS,
               "--by", SIGNER, "--auditor", AUDITORS[0]])
    run(KIT + ["evidence", "--ledger", nh, "--root", nogit,
               "--by", AUDITORS[0]])
    p = run(KIT + ["signoff", "--ledger", nh, "--root", nogit,
                   "--by", SIGNER])
    check(p.returncode == 1
          and "not a git repository" in p.stderr,
          "a non-git tree refuses the seal — the final audit does "
          "not close on a tree whose history it cannot read")
    shutil.rmtree(nogit, ignore_errors=True)

    # The happy seal, on the life tree.
    p = fa("signoff", "--by", SIGNER)
    check(p.returncode == 0 and "sealed" in p.stdout,
          "the project signs the closure and the seal is written")
    seal = [e for e in read_entries(h)[0]
            if e["kind"] == "signoff"][0]
    check(seal["tree"] == git_head(tree),
          "the seal records the tree it closed")
    live = live_anchor_digests()
    for name, digest in live.items():
        check(seal["anchors"].get(name) == digest,
              "the seal pins the live %s anchor" % name)
    check(seal["findings"] == 1 and seal["fixes"] == 1
          and seal["independent_passes"] >= 1,
          "the seal records the closure census")

    p = fa("status", "--json")
    st = json.loads(p.stdout)
    check(st["state"] == "sealed",
          "status --json derives the seventh state")

    # THE SEAL LAW: after the seal, every writer refuses by name —
    # and the readers still read.
    before = open(h, "rb").read()
    for argv in (["evidence", "--by", AUDITORS[0]],
                 ["file", "--against", "G-CT-01", "--severity",
                  "low", "--title", "x", "--repro-text", "x",
                  "--by", AUDITORS[0]],
                 ["triage", "AF-001", "--verdict", "disputed",
                  "--reason", "x"],
                 ["signoff", "--by", SIGNER]):
        p = fa(*argv)
        check(p.returncode == 1
              and "after the seal is the next audit" in p.stderr,
              "`%s` after the seal is refused by name" % argv[0])
    check(open(h, "rb").read() == before,
          "the sealed ledger's bytes did not move")
    p = fa("status")
    check(p.returncode == 0 and "sealed" in p.stdout,
          "the readers still read the sealed ledger")

    # The report renders the seal.
    r1 = os.path.join(GATE, "rep-1.md")
    r2 = os.path.join(GATE, "rep-2.md")
    p = fa("report", "--out", r1)
    check(p.returncode == 0 and "state sealed" in p.stdout,
          "report renders the deliverable")
    doc = open(r1, encoding="utf-8").read()
    raw = open(h, "rb").read()
    digest = hashlib.sha256(raw).hexdigest()
    check(("`sha256:%s`" % digest) in doc,
          "the report embeds the ledger digest, re-derived by the "
          "gate from the raw bytes")
    check("**SEALED**" in doc and "What comes after the seal" in doc,
          "the report renders the signoff block and the seal's law")
    fa("report", "--out", r2)
    check(open(r1, "rb").read() == open(r2, "rb").read(),
          "two renders of one ledger are byte-identical")
    shutil.rmtree(drift, ignore_errors=True)


# ===========================================================================
def _section_8(tree, h):
    print("Section 8 — the gate law")

    def fa(*argv):
        return run(KIT + list(argv) + ["--root", tree,
                                       "--ledger", h])

    # No ledger at all: the gate names its own law.
    p = run(KIT + ["gate", "--ledger", led("none")])
    check(p.returncode == 1
          and "not cut from an unaudited tree" in p.stderr,
          "the gate on a missing ledger: a release candidate is "
          "not cut from an unaudited tree")

    # Unsealed states: refused naming the state.
    for name, want in (("engage", "open"),
                       ("solo", "underway"),
                       ("untr", "reported")):
        p = run(KIT + ["gate", "--ledger", led(name)])
        check(p.returncode == 1 and want in p.stderr,
              "the gate is red on the %s ledger, naming its state"
              % name)

    # Sealed, on the audited tree: GREEN.
    p = fa("gate")
    check(p.returncode == 0 and "GATE GREEN" in p.stdout,
          "the gate is green on the sealed audit")

    # A release cut from a DESCENDANT of the sealed tree: GREEN.
    git_empty_commit(tree, "rc1 prep — the release moves on")
    p = fa("gate")
    check(p.returncode == 0 and "GATE GREEN" in p.stdout,
          "a release cut from a descendant of the sealed tree is "
          "green — the audited artifact is still the ancestor")

    # A release cut AROUND the audit: refused.
    sealed_tree = [e for e in read_entries(h)[0]
                   if e["kind"] == "signoff"][0]["tree"]
    run(["git", "reset", "-q", "--hard", "HEAD~2"], cwd=tree)
    p = fa("gate")
    check(p.returncode == 1 and "cut around the audit" in p.stderr,
          "a release cut around the audit is refused")
    run(["git", "reset", "-q", "--hard", sealed_tree], cwd=tree)
    p = fa("gate")
    check(p.returncode == 0,
          "restored to the sealed tree, the gate is green again")

    # Each anchor mutated after the seal: refused by name.
    for name, path, mut in (
            ("api-freeze", "tests/api_freeze.json",
             lambda d: d.update(digest="a" * 64)),
            ("bootstrap-snapshot", "snapshot/MANIFEST.json",
             lambda d: d.update(hlc_c_sha256="b" * 64)),
            ("guarantee-scope", "tools/hls-guarantee.py",
             lambda d: d)):
        full = os.path.join(tree, path)
        with open(full, encoding="utf-8") as fh:
            if path.endswith(".json"):
                doc = json.load(fh)
                mut(doc)
                blob = json.dumps(doc, indent=2)
            else:
                blob = fh.read() + "\nLEDGER = LEDGER[:14]\n"
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(blob)
        p = fa("gate")
        check(p.returncode == 1
              and ("%s anchor moved after the seal" % name)
              in p.stderr,
              "the %s anchor moved after the seal — the candidate "
              "is not the artifact that was audited" % name)
        run(["git", "checkout", "-q", "--", path], cwd=tree)

    p = fa("gate")
    check(p.returncode == 0, "the tree restored, the gate is green")
    shutil.rmtree(tree, ignore_errors=True)


# ===========================================================================
def _section_9():
    print("Section 9 — the corpus showcase")

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
        tree, h = _section_6()
        _section_7(tree, h)
        _section_8(tree, h)
        _section_9()
    finally:
        shutil.rmtree(GATE, ignore_errors=True)
    print("")
    print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
