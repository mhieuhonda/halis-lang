#!/usr/bin/env python3
"""hls-auditkit — the Stage 140 third-party audit kit: the
independent, paid security audit run as a chained ledger.

Usage:
  python3 tools/hls-auditkit.py engage --firm "..." --auditor "A"
                                [--auditor "B" ...]
                                --window "YYYY-MM-DD/YYYY-MM-DD"
                                --basis "..." [--by NAME]
  python3 tools/hls-auditkit.py brief [--out AUDITBRIEF.md]
                                [--stamp] [--root PATH]
                                [--ledger PATH]
  python3 tools/hls-auditkit.py evidence [--by NAME] [--root PATH]
                                [--ledger PATH] [--json]
  python3 tools/hls-auditkit.py file --against G-XXX-NN
                                --severity critical|high|medium|low|info
                                --title "..."
                                (--repro-path PATH | --repro-text "...")
                                [--by NAME] [--root PATH] [--ledger PATH]
  python3 tools/hls-auditkit.py triage AF-001
                                --verdict accepted|disputed|out-of-scope
                                --reason "..." [--fix <40-hex>]
                                [--by NAME] [--root PATH] [--ledger PATH]
  python3 tools/hls-auditkit.py report --out AUDITREPORT.md
                                [--stamp] [--root PATH] [--ledger PATH]
  python3 tools/hls-auditkit.py status [--json] [--root PATH]
                                [--ledger PATH]

Roadmap Stage 140 is one phrase: "Independent security audit
(third-party, paid)". A promise about the future — "we will
commission one" — is prose, and prose rots. This stage turns the
promise into a LEDGER: one hash-chained, append-only record per
engagement (`.hlaudit/engagement.jsonl`, machine-local like every
ledger in this tree), where every fact of the audit is an entry
somebody signed, and every law below is enforced at write time:

  1. the SCOPE is derived LIVE from the Stage 135 guarantee
     ledger (`tools/hls-guarantee.py` is imported at run time and
     read, never copied): the brief can only scope what the
     CURRENT tree still demonstrates, and a guarantee removed from
     the ledger is removed from the audit the same day. The
     engagement records the scope digest it was signed against,
     every later pass records the digest it ran, and the report
     NAMES the drift when they disagree — a scope that moved under
     the auditors is a fact, never a silence;

  2. the BRIEF (the RFP) is a deterministic render of that live
     scope: the census table, the method, the findings protocol,
     the commercial terms. It is the only command that may run
     before a ledger exists — solicitation precedes contract, and
     a brief sent to candidate firms is how one of them becomes
     the engagement;

  3. the INDEPENDENCE LAW is the stage's whole point: WHO ran a
     pass is recorded on the entry, and the label is derived from
     the roster, never asserted — a pass by a roster member is
     INDEPENDENT, anyone else's is SELF-ASSESSED, and the report
     refuses to call a self-run pass independent. Symmetrically:
     findings are the AUDITORS' to file (the project cannot author
     one), the triage is the PROJECT'S to give (an auditor cannot
     respond to one), and the signer of the contract cannot be on
     their own roster. Independence is a fact the ledger names,
     not a fact it manufactures;

  4. the FINDINGS PROTOCOL: a finding names ONE guarantee from
     the scope, carries a severity from the closed audit
     vocabulary (critical/high/medium/low/info — wider than the
     bounty's three, because auditors also file hardening notes),
     and carries a REPRODUCER — a path that exists in the tree or
     inline text, never both, never neither. A finding that cannot
     be reproduced is an opinion, and opinions are not findings;

  5. the TRIAGE: one response per finding, with a written reason
     (a triage without a reason is refused — the sanction law,
     here on the other side of the table). An ACCEPTED finding
     carries its remediation commit at triage time — 40 hex that
     names a commit that exists; an acceptance without its
     remediation is a wish, not a fix. A disputed or out-of-scope
     finding carries no fix at all — the vocabulary is closed and
     the pairing is strict;

  6. the CHAIN is the family's one arithmetic (imported from
     hltlog_parts, byte-identical to every chained ledger since
     Stage 13): sha256(prev_hash + canonical-json(entry minus
     chain_hash)). Every reader verifies the chain first and
     names the broken line — a tampered ledger is evidence, not a
     document, and nothing renders from it;

  7. the STATE is derived from the census, never stored:
     vacant -> open -> underway -> reported -> responded ->
     remediated. Responded requires every finding triaged;
     remediated requires every acceptance to carry its fix. A
     finding-free audit honestly stays underway — the ledger
     cannot know the auditors rested; the report is the
     deliverable either way.

The timestamps on entries are facts (when the op happened), in
the family's tradition; the RENDERED documents are deterministic
per ledger — sorted walks, no wall-clock unless --stamp — so two
renders of one ledger are byte-identical, and any hand-edit is
visible as a diff.

Exit codes: 0 the command's contract held / 1 a named refusal or
a failed pass (fail-closed) / 2 usage error. Every refusal names
the rule, not just the fact.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
for _p in (_TOOLS, os.path.join(_TOOLS, "hltlog_parts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hltlog_parts.hltlog_common import (          # noqa: E402
    GENESIS, canon_bytes, chain_of, is_hex64,
)

TOOL = "hls-auditkit"
VERSION = "0.158.0-alpha"          # the toolchain release this run stamps
POLICY = "halis-auditkit-1"
LEDGER_REL = os.path.join(".hlaudit", "engagement.jsonl")

GUARANTEE_TOOL = "hls-guarantee.py"     # the Stage 135 ledger, imported
SECURITY_MD = "SECURITY.md"

# ---------------------------------------------------------------------------
# The closed vocabularies. The audit severity vocabulary is wider than
# the bounty's three (Stage 135 ranks critical/high/medium for what a
# VIOLATION means): auditors also file low and info rows — hardening
# notes that carry no bounty but belong in a paid report.
# ---------------------------------------------------------------------------
AUDIT_SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3,
                 "info": 4}
SEVERITY_MEANING = {
    "critical":
    "a soundness boundary is bypassable in practice, or the two "
    "implementations diverge on an accepted program",
    "high":
    "a security boundary (matching, capability, sandbox, "
    "constant-time, taint, FFI honesty) is bypassed with a "
    "reproducer the toolchain accepts",
    "medium":
    "a documented guarantee does not hold as written, but no "
    "exploit path is demonstrated",
    "low":
    "hardening: the guarantee holds, the margin around it is "
    "thinner than the ledger claims",
    "info":
    "an observation the auditors want on record that maps onto "
    "no guarantee row",
}

TRIAGE_VERDICTS = ("accepted", "disputed", "out-of-scope")
VERDICT_MEANING = {
    "accepted":
    "the finding stands; the remediation commit is recorded on "
    "the triage itself",
    "disputed":
    "the finding stands as filed but the project contests it — "
    "with a written reason",
    "out-of-scope":
    "the finding falls outside the contract it was filed "
    "against — with a written reason",
}

# The lifecycle, ordered. Derived from the census on every render;
# never stored, never asserted by a writer.
STATES = ("vacant", "open", "underway", "reported", "responded",
          "remediated")
STATE_MEANING = {
    "vacant":
    "no ledger at all — nothing has happened",
    "open":
    "the contract is on record; no evidence pass has run",
    "underway":
    "at least one evidence pass is on record; no finding filed",
    "reported":
    "the auditors have spoken — at least one finding is on "
    "record, not yet fully triaged",
    "responded":
    "every finding carries its one triage",
    "remediated":
    "every finding is triaged and every acceptance carries its "
    "remediation commit",
}

REPRO_MODES = ("path", "text")
PASS_OUTCOMES = ("ok", "failed")
ROLES = ("auditor", "project")

# The finding id grammar: AF-NNN, three digits, sequential from 1.
FINDING_PREFIX = "AF-"
FINDING_WIDTH = 3

# ---------------------------------------------------------------------------
# Identity. --by is recorded verbatim on every entry; the default is
# the tree's own git identity (the maintainer's name), so the normal
# `make auditkit-evidence` run is honestly labelled SELF-ASSESSED
# unless the auditors run it themselves.
# ---------------------------------------------------------------------------
def default_actor(tree):
    """-> the name --by falls back to: git user.name of the tree,
    else $USER, else a named unknown. A principal, not a machine."""
    for cmd in (["git", "config", "user.name"],):
        try:
            out = subprocess.run(cmd, cwd=tree, timeout=10,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return os.environ.get("USER") or "<unknown>"


def git_sha(tree):
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree,
                             timeout=15, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown (not a git tree)"


def commit_exists(tree, sha):
    """-> True when `sha` names a commit that exists in the tree.
    In a non-git tree (an auditor working from a tarball) the answer
    is None — the caller validates shape only and says so."""
    try:
        out = subprocess.run(
            ["git", "cat-file", "-e", sha + "^{commit}"], cwd=tree,
            timeout=15, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        if out.returncode == 0:
            return True
        # a real git tree that does not know this sha: refused
        probe = subprocess.run(["git", "rev-parse", "--git-dir"],
                               cwd=tree, timeout=15,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        if probe.returncode == 0:
            return False
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None

# ---------------------------------------------------------------------------
# The scope. The Stage 135 guarantee ledger is a Python module with a
# dash in its filename; it is IMPORTED at run time from the --root
# tree, never copied, never re-derived. The scope of an audit is what
# the CURRENT tree still demonstrates — the brief quotes it live and
# the evidence pass re-runs it through the real checker.
# ---------------------------------------------------------------------------
_SCOPE_CACHE = {}


def load_scope(root):
    """-> the hls-guarantee module loaded from `root` (cached per
    root). Raises ScopeError when the tree carries no ledger."""
    root = os.path.abspath(root)
    if root in _SCOPE_CACHE:
        return _SCOPE_CACHE[root]
    path = os.path.join(root, "tools", GUARANTEE_TOOL)
    if not os.path.isfile(path):
        shown = (os.path.relpath(path, root)
                 if os.path.isdir(root) else path)
        raise ScopeError(
            "no guarantee ledger at %s — the audit scope is the "
            "Stage 135 ledger, and this tree does not carry one"
            % shown)
    spec = importlib.util.spec_from_file_location(
        "hls_guarantee_at_%d" % (abs(hash(root)) % 100000), path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as ex:                       # noqa: BLE001
        raise ScopeError("the guarantee ledger at %s does not load: %s"
                         % (os.path.relpath(path, root), ex))
    _SCOPE_CACHE[root] = mod
    return mod


class ScopeError(Exception):
    """The scope cannot be loaded — nothing can be scoped, filed or
    verified until it can."""


def scope_census(mod):
    """The census rows the brief and the report agree on, in ledger
    order (the ledger's own order is the presentation order)."""
    return [{"id": g["id"], "family": g["family"],
             "severity": g["severity"], "claim": g["claim"],
             "anchors": len(g["anchors"]),
             "probes": len(g["probes"])}
            for g in mod.LEDGER]


def policy_anchor_ok(root, mod):
    """The live-quote law: the reporting channel anchored in
    SECURITY.md must still be there, or the brief refuses."""
    problems = []
    for rel, pat in mod.POLICY_ANCHORS:
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            problems.append("the reporting channel anchor is gone "
                            "(%s: missing %s)" % (rel, pat))
            continue
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            if pat not in fh.read():
                problems.append("the reporting channel anchor is gone "
                                "(%s: %r not found)" % (rel, pat))
    return problems

# ---------------------------------------------------------------------------
# The ledger. One JSON object per line, the family's mandatory chain
# fields (seq, timestamp, prev_hash, chain_hash) on every entry, one
# of four kinds: engagement / evidence / finding / triage. The chain
# arithmetic is imported, not re-derived; every reader verifies the
# whole chain first and names the broken line — nothing renders from
# a ledger it could not verify.
# ---------------------------------------------------------------------------
KINDS = ("engagement", "evidence", "finding", "triage")


class LedgerError(Exception):
    """A refusal the CLI reports on stderr with exit 1."""


def is_hex40(s):
    return isinstance(s, str) and len(s) == 40 \
        and all(c in "0123456789abcdef" for c in s)


def is_finding_id(s):
    if not isinstance(s, str) or not s.startswith(FINDING_PREFIX):
        return False
    tail = s[len(FINDING_PREFIX):]
    return len(tail) == FINDING_WIDTH and tail.isdigit()


def parse_window(window):
    """-> (start_date, end_date) or raises ValueError. The window is
    two ISO dates joined by a slash; a window that ends before it
    starts is refused at write time."""
    parts = window.split("/")
    if len(parts) != 2:
        raise ValueError("not two ISO dates joined by /")
    import datetime
    start = datetime.date.fromisoformat(parts[0])
    end = datetime.date.fromisoformat(parts[1])
    if end < start:
        raise ValueError("the window ends before it starts")
    return start, end


def _need(entry, key, types, what, line_no):
    v = entry.get(key)
    if not isinstance(v, types) or (isinstance(v, str) and not v):
        raise LedgerError("%s: line %d: %s must be a non-empty %s"
                          % (what, line_no, key,
                             "/".join(t.__name__ for t in types)
                             if isinstance(types, tuple)
                             else types.__name__))
    return v


def entry_shape(entry, line_no):
    """The per-kind shape, checked on every read. Shape only — the
    ledger reader never re-derives live facts (the scope may have
    moved; that is the drift law's business, not the parser's)."""
    what = "broken entry"
    kind = entry.get("kind")
    if kind not in KINDS:
        raise LedgerError(
            "unknown entry kind: line %d: kind %r is not one of %s"
            % (line_no, kind, "/".join(KINDS)))
    what = "%s entry" % kind
    if not isinstance(entry.get("seq"), int) or entry["seq"] < 1:
        raise LedgerError("%s: line %d: seq must be an int >= 1"
                          % (what, line_no))
    if not isinstance(entry.get("timestamp"), int) \
            or entry["timestamp"] < 0:
        raise LedgerError("%s: line %d: timestamp must be an int >= 0"
                          % (what, line_no))
    if not is_hex64(entry.get("prev_hash")):
        raise LedgerError("%s: line %d: prev_hash is not 64 hex"
                          % (what, line_no))
    if not is_hex64(entry.get("chain_hash")):
        raise LedgerError("%s: line %d: chain_hash is not 64 hex"
                          % (what, line_no))

    if kind == "engagement":
        _need(entry, "by", str, what, line_no)
        _need(entry, "firm", str, what, line_no)
        roster = entry.get("roster")
        if not isinstance(roster, list) or not roster \
                or not all(isinstance(r, str) and r for r in roster) \
                or len(set(roster)) != len(roster):
            raise LedgerError(
                "%s: line %d: roster must be a non-empty list of "
                "unique names" % (what, line_no))
        window = entry.get("window")
        if not isinstance(window, str):
            raise LedgerError("%s: line %d: window must be a string"
                              % (what, line_no))
        try:
            parse_window(window)
        except ValueError as ex:
            raise LedgerError("%s: line %d: bad window %r (%s)"
                              % (what, line_no, window, ex))
        _need(entry, "basis", str, what, line_no)
        if not is_hex64(entry.get("scope_sha256")):
            raise LedgerError("%s: line %d: scope_sha256 is not 64 "
                              "hex" % (what, line_no))

    elif kind == "evidence":
        _need(entry, "by", str, what, line_no)
        if entry.get("role") not in ROLES:
            raise LedgerError("%s: line %d: role %r is not one of %s"
                              % (what, line_no, entry.get("role"),
                                 "/".join(ROLES)))
        if not isinstance(entry.get("independent"), bool):
            raise LedgerError("%s: line %d: independent must be a "
                              "boolean" % (what, line_no))
        _need(entry, "tree", str, what, line_no)
        if not is_hex64(entry.get("scope_sha256")):
            raise LedgerError("%s: line %d: scope_sha256 is not 64 "
                              "hex" % (what, line_no))
        if entry.get("outcome") not in PASS_OUTCOMES:
            raise LedgerError("%s: line %d: outcome %r is not one of "
                              "%s" % (what, line_no, entry.get("outcome"),
                                      "/".join(PASS_OUTCOMES)))
        for key in ("verified_guarantees", "total_guarantees",
                    "anchors", "probes"):
            if not isinstance(entry.get(key), int) or entry[key] < 0:
                raise LedgerError("%s: line %d: %s must be an int >= 0"
                                  % (what, line_no, key))
        if entry["verified_guarantees"] > entry["total_guarantees"]:
            raise LedgerError("%s: line %d: more verified guarantees "
                              "than the scope has" % (what, line_no))
        refusals = entry.get("refusals", [])
        if not isinstance(refusals, list) \
                or not all(isinstance(r, str) for r in refusals):
            raise LedgerError("%s: line %d: refusals must be a list "
                              "of strings" % (what, line_no))
        if entry["outcome"] == "failed" and not refusals:
            raise LedgerError("%s: line %d: a failed pass with no "
                              "refusals names nothing" % (what, line_no))
        if entry["outcome"] == "ok" and refusals:
            raise LedgerError("%s: line %d: a green pass carrying "
                              "refusals is a contradiction"
                              % (what, line_no))

    elif kind == "finding":
        if not is_finding_id(entry.get("id")):
            raise LedgerError("%s: line %d: id must be AF-NNN"
                              % (what, line_no))
        _need(entry, "by", str, what, line_no)
        _need(entry, "against", str, what, line_no)
        if entry.get("severity") not in AUDIT_SEVERITIES:
            raise LedgerError("%s: line %d: severity %r is not one of "
                              "%s" % (what, line_no, entry.get("severity"),
                                      "/".join(AUDIT_SEVERITIES)))
        _need(entry, "title", str, what, line_no)
        if entry.get("repro_mode") not in REPRO_MODES:
            raise LedgerError("%s: line %d: repro_mode %r is not one "
                              "of %s" % (what, line_no,
                                         entry.get("repro_mode"),
                                         "/".join(REPRO_MODES)))
        _need(entry, "repro_value", str, what, line_no)

    elif kind == "triage":
        _need(entry, "by", str, what, line_no)
        if not is_finding_id(entry.get("for")):
            raise LedgerError("%s: line %d: for must be AF-NNN"
                              % (what, line_no))
        if entry.get("verdict") not in TRIAGE_VERDICTS:
            raise LedgerError("%s: line %d: verdict %r is not one of "
                              "%s" % (what, line_no, entry.get("verdict"),
                                      "/".join(TRIAGE_VERDICTS)))
        _need(entry, "reason", str, what, line_no)
        if "fix" in entry and not is_hex40(entry["fix"]):
            raise LedgerError("%s: line %d: fix must be 40 hex"
                              % (what, line_no))
        if entry["verdict"] != "accepted" and "fix" in entry:
            raise LedgerError("%s: line %d: only an acceptance carries "
                              "a fix" % (what, line_no))


def read_ledger(path, must_exist=True):
    """-> (entries, head, log_sha256). Verifies the whole chain first:
    a tampered ledger is named at its broken line, never rendered."""
    if not os.path.isfile(path):
        if must_exist:
            raise LedgerError("no engagement ledger at %s — nothing "
                              "to read" % path)
        return [], GENESIS, None
    with open(path, "rb") as fh:
        raw = fh.read()
    log_sha256 = hashlib.sha256(raw).hexdigest()
    entries = []
    prev = GENESIS
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    for i, rawline in enumerate(lines, 1):
        line = rawline.strip()
        if not line:
            raise LedgerError("blank line %d: the ledger is a JSONL "
                              "chain, not a notepad" % i)
        try:
            entry = json.loads(line)
        except ValueError as ex:
            raise LedgerError("line %d is not JSON: %s" % (i, ex))
        if not isinstance(entry, dict):
            raise LedgerError("line %d is not an object" % i)
        entry_shape(entry, i)
        if entry["seq"] != i:
            raise LedgerError("line %d: seq is %d, expected %d — the "
                              "chain has a hole or a lie"
                              % (i, entry["seq"], i))
        if entry["prev_hash"] != prev:
            raise LedgerError(
                "line %d: prev_hash %s... does not chain onto %s... — "
                "the chain is BROKEN here: the ledger was rewritten, "
                "reordered or truncated" % (i, str(entry["prev_hash"])[:12],
                                            str(prev)[:12]))
        want = chain_of(prev, entry)
        if entry["chain_hash"] != want:
            raise LedgerError(
                "line %d: chain_hash %s... does not hash onto its "
                "predecessor (expected %s...) — the entry was edited "
                "after it was written" % (i, str(entry["chain_hash"])[:12],
                                          want[:12]))
        entries.append(entry)
        prev = entry["chain_hash"]
    return entries, prev, log_sha256


def append_chained(path, body):
    """Append one entry with the family's arithmetic; returns the
    entry as written. The chain is re-verified BEFORE the write (a
    tool that appends onto a chain it never read is a tool that
    trusts a file it has never seen)."""
    entries, head, _ = read_ledger(path, must_exist=False)
    entry = dict(body)
    entry["seq"] = len(entries) + 1
    entry["timestamp"] = int(time.time())
    entry["prev_hash"] = head
    entry["chain_hash"] = chain_of(head, entry)
    blob = json.dumps(entry, sort_keys=True,
                      separators=(",", ":")) + "\n"
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(blob)
    return entry


# ---------------------------------------------------------------------------
# The census. The state and every aggregate are DERIVED from the
# entries on each render — never stored, never asserted.
# ---------------------------------------------------------------------------
def engagement_of(entries):
    """The engagement is the first entry or none: only `engage`
    writes one, and every other writer requires it to be there
    already, so an engagement mid-ledger is a shape lie the reader
    has already refused."""
    if entries and entries[0].get("kind") == "engagement":
        return entries[0]
    return None


def findings_of(entries):
    """-> {AF-NNN: finding}, insertion order."""
    out = {}
    for e in entries:
        if e["kind"] == "finding":
            out[e["id"]] = e
    return out


def triages_of(entries):
    """-> {AF-NNN: triage}, insertion order. One triage per finding
    is enforced at write time; the census takes the first it sees."""
    out = {}
    for e in entries:
        if e["kind"] == "triage" and e["for"] not in out:
            out[e["for"]] = e
    return out


def passes_of(entries):
    return [e for e in entries if e["kind"] == "evidence"]


def derive_state(entries):
    """The lifecycle word, from the census alone. A finding-free
    audit honestly stays underway — see the report's honesty note."""
    if not entries:
        return "vacant"
    if engagement_of(entries) is None:
        # only engage writes entry zero; a reader past shape checks
        # never sees a ledger without one. Derive anyway: open.
        return "open"
    findings = findings_of(entries)
    triages = triages_of(entries)
    if not passes_of(entries):
        return "open"
    if not findings:
        return "underway"
    if len(triages) < len(findings):
        return "reported"
    if not all("fix" in t for t in triages.values()
               if t["verdict"] == "accepted"):
        return "responded"
    return "remediated"

# ---------------------------------------------------------------------------
# Renders. Both documents are deterministic per input (the tree for
# the brief, the ledger bytes for the report): sorted walks, no
# wall-clock unless --stamp, two renders byte-identical — so a
# hand-edited document is visible as a diff.
# ---------------------------------------------------------------------------
def _fmt_ts(entry):
    """The entry's timestamp as a UTC second, the family's tradition:
    entries carry facts; renders carry them verbatim."""
    import datetime
    return datetime.datetime.fromtimestamp(
        entry["timestamp"], datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")


def _stamp_line(stamp):
    return ("- Generated: %s" % stamp) if stamp else None


def render_brief(root, mod, engagement, stamp=None):
    census = scope_census(mod)
    digest = mod.ledger_digest()
    tree = git_sha(root)
    firm_line = ("OPEN SOLICITATION — no contract on record; the terms "
                 "below are the standing proposal, to be recorded "
                 "verbatim at engage time")
    if engagement is not None:
        firm_line = ("%s — window %s, basis on record: %r (signed by %s)"
                     % (engagement["firm"], engagement["window"],
                        engagement["basis"], engagement["by"]))
    L = []
    L.append("# Halis — third-party security audit brief (RFP)")
    L.append("")
    L.append("Regenerated from the live tree by `%s`." % TOOL)
    L.append("This document is generated, not maintained: the scope")
    L.append("below is what the CURRENT tree still demonstrates,")
    L.append("quoted live from the Stage 135 guarantee ledger. Do")
    L.append("not hand-edit; regenerate with")
    L.append("`python3 tools/%s.py brief --out AUDITBRIEF.md`." % TOOL)
    L.append("")
    L.append("- Policy: %s" % POLICY)
    L.append("- Toolchain: %s" % VERSION)
    L.append("- Tree: `%s`" % tree)
    L.append("- Scope: the Stage 135 guarantee ledger, LIVE — %d "
             "guarantees" % len(census))
    L.append("- Scope digest: `sha256:%s`" % digest)
    L.append("- Engagement: %s" % firm_line)
    if stamp:
        L.append("- Generated: %s" % stamp)
    L.append("")
    L.append("## Why this brief is generated, not written")
    L.append("")
    L.append("An audit scoped by a document is scoped by prose, and")
    L.append("prose rots: nothing fails when the code it describes")
    L.append("changes. This brief is derived at render time from the")
    L.append("guarantee ledger the toolchain itself is held to, so a")
    L.append("guarantee removed from the ledger is removed from the")
    L.append("audit the same day, and a guarantee the tree no longer")
    L.append("demonstrates takes the brief with it — the render")
    L.append("refuses. The scope digest above pins the ledger this")
    L.append("brief was scoped from; the engagement records it")
    L.append("again at signature time, and every evidence pass")
    L.append("records the digest it actually ran, so a scope that")
    L.append("moved under the auditors is a named fact, never a")
    L.append("silence.")
    L.append("")
    L.append("## What the auditors are asked to do")
    L.append("")
    L.append("1. RE-DERIVE every anchor: each guarantee rests on named")
    L.append("   enforcement points in the tree; find them, read them,")
    L.append("   and say whether the mechanism is what the row claims.")
    L.append("2. RE-RUN every probe through the real checker: the")
    L.append("   fixtures are pinned refusals and panics; confirm the")
    L.append("   toolchain still refuses and still panics as written.")
    L.append("3. ATTEMPT the violation contracts, bounty-style: write")
    L.append("   programs that should be refused and are not; the")
    L.append("   contract of each row below is the boundary to attack.")
    L.append("4. FILE findings per the protocol below — and nothing")
    L.append("   else: an observation that maps onto no row and carries")
    L.append("   no reproducer is out of the protocol by definition.")
    L.append("")
    L.append("## The findings protocol")
    L.append("")
    L.append("- The severity vocabulary is closed, five words, ranked:")
    L.append("")
    L.append("  | severity | rank | meaning |")
    L.append("  |----------|-----:|---------|")
    for sev in AUDIT_SEVERITIES:
        L.append("  | %s | %d | %s |"
                 % (sev, SEVERITY_RANK[sev], SEVERITY_MEANING[sev]))
    L.append("")
    L.append("- A finding names ONE guarantee id from the scope (the")
    L.append("  `against` law); a finding against nothing in the scope")
    L.append("  is refused naming the known rows.")
    L.append("- A finding carries a REPRODUCER: a path that exists in")
    L.append("  the tree, or inline text — never both, never neither.")
    L.append("  A finding that cannot be reproduced is an opinion, and")
    L.append("  opinions are not findings.")
    L.append("- THE INDEPENDENCE LAW: findings are the AUDITORS' to")
    L.append("  file — the project cannot author one, and an auditor")
    L.append("  cannot triage one. The response is the project's to")
    L.append("  give, with a written reason; an acceptance carries its")
    L.append("  remediation commit, 40 hex naming a commit that exists.")
    L.append("  An acceptance without its remediation is a wish, not a")
    L.append("  fix.")
    L.append("- Every evidence pass is run by SOMEONE, and the label")
    L.append("  follows the roster, never the runner's mood: a pass by")
    L.append("  a roster member is INDEPENDENT, anyone else's is")
    L.append("  SELF-ASSESSED, and the report refuses to call a")
    L.append("  self-run pass independent. Independence is a fact the")
    L.append("  ledger names, not a fact it manufactures.")
    L.append("")
    L.append("## The commercial terms (the paid part)")
    L.append("")
    L.append("- The engagement is PAID. The fee basis is a term of the")
    L.append("  contract, recorded verbatim at `engage` time, and the")
    L.append("  kit refuses an engagement without one: a gratis review")
    L.append("  is not an independent audit — it has no deliverable")
    L.append("  schedule, no remedy when the deliverable is late, and")
    L.append("  no obligation to file what it found.")
    L.append("- The standing proposal, until the engagement records its")
    L.append("  own basis: fixed fee against the scope census (the %d"
             % len(census))
    L.append("  rows below), half payable at engagement, half on")
    L.append("  delivery of the report; expenses within the window")
    L.append("  borne by the engaging party.")
    L.append("- The deliverable schedule: findings filed within the")
    L.append("  window; the project triages within 14 days of each")
    L.append("  finding; remediation commits recorded at triage time.")
    L.append("- The window is recorded at signature time as two ISO")
    L.append("  dates; a window that ends before it starts is refused")
    L.append("  at the signature, not discovered at the invoice.")
    L.append("")
    L.append("## The acceptance criteria for the engagement")
    L.append("")
    L.append("- every guarantee re-derived by the auditor's OWN pass —")
    L.append("  an INDEPENDENT transcript on record by a roster member;")
    L.append("- every finding carries a reproducer and names a row in")
    L.append("  the scope;")
    L.append("- every triage carries a written reason; every acceptance")
    L.append("  carries its remediation commit;")
    L.append("- the report renders from the chained ledger, and the")
    L.append("  chain verifies — a tampered ledger is evidence, not a")
    L.append("  document.")
    L.append("")
    L.append("## The scope census")
    L.append("")
    L.append("| id | family | severity | anchors | probes |")
    L.append("|----|--------|----------|---------:|-------:|")
    for row in census:
        L.append("| %s | %s | %s | %d | %d |"
                 % (row["id"], row["family"], row["severity"],
                    row["anchors"], row["probes"]))
    L.append("")
    L.append("### The rows in full — the boundary of each claim")
    L.append("")
    for g in mod.LEDGER:
        L.append("#### %s — %s — %s" % (g["id"], g["family"],
                                        g["severity"]))
        L.append("")
        L.append("**Claim.** %s" % g["claim"])
        L.append("")
        L.append("**Violation contract.** %s" % g["violation"])
        L.append("")
        L.append("**Reproduction.** %s" % g["repro"])
        L.append("")
    L.append("## Reporting channel (anchored live)")
    L.append("")
    L.append("The channel is anchored in SECURITY.md and checked at")
    L.append("render time — if it moves, this brief refuses until it")
    L.append("is re-anchored: open a private GitHub Security Advisory")
    L.append("at")
    L.append("<https://github.com/mhieuhonda/halis-lang/security/advisories/new>.")
    L.append("The project follows a 90-day coordinated-disclosure")
    L.append("window and aims to acknowledge within 48 hours. Please")
    L.append("do NOT open a public issue for security reports.")
    L.append("")
    L.append("## Reproducing this brief")
    L.append("")
    L.append("`python3 tools/%s.py brief --out AUDITBRIEF.md` — two" % TOOL)
    L.append("renders of one tree are byte-identical; `--stamp` adds a")
    L.append("Generated line and nothing else. The scope digest above")
    L.append("pins the ledger this brief was scoped from.")
    L.append("")
    return "\n".join(L) + "\n"


def render_report(path, entries, head, log_sha256, stamp=None):
    state = derive_state(entries)
    engagement = engagement_of(entries)
    passes = passes_of(entries)
    findings = findings_of(entries)
    triages = triages_of(entries)
    L = []
    L.append("# Halis security audit — engagement report")
    L.append("")
    L.append("Regenerated from the chained ledger by `%s`." % TOOL)
    L.append("Deterministic per ledger: two renders of one ledger are")
    L.append("byte-identical. Do not hand-edit; regenerate with")
    L.append("`python3 tools/%s.py report --out AUDITREPORT.md`." % TOOL)
    L.append("")
    L.append("- Ledger: %s — %d entries, digest `sha256:%s`"
             % (os.path.relpath(path, _ROOT)
                if path.startswith(_ROOT) else path,
                len(entries), log_sha256))
    L.append("- State: **%s** — %s" % (state, STATE_MEANING[state]))
    if engagement is not None:
        L.append("- Engagement: %s — roster: %s"
                 % (engagement["firm"], ", ".join(engagement["roster"])))
        L.append("- Window: %s" % engagement["window"])
        L.append("- Basis on record: %r (signed by %s)"
                 % (engagement["basis"], engagement["by"]))
        L.append("- Scope at engagement: `sha256:%s`"
                 % engagement["scope_sha256"])
        newest = passes[-1] if passes else None
        if newest is not None \
                and newest["scope_sha256"] != engagement["scope_sha256"]:
            L.append("- Scope drift: **MOVED** — engaged at "
                     "`sha256:%s`, the latest pass ran `sha256:%s`:"
                     % (engagement["scope_sha256"][:24],
                        newest["scope_sha256"][:24]))
            L.append("  the scope moved after the engagement; every")
            L.append("  pass below names the scope it actually ran.")
        else:
            L.append("- Scope drift: none on record")
    else:
        L.append("- Engagement: none on record — this ledger predates")
        L.append("  its own contract, which no writer in this kit")
        L.append("  produces; the chain refused to guess.")
    if stamp:
        L.append("- Generated: %s" % stamp)
    L.append("")
    L.append("## The evidence passes")
    L.append("")
    if not passes:
        L.append("No evidence pass on record. The engagement has no")
        L.append("transcript yet; nothing below is demonstrated by")
        L.append("anyone, by anyone, in any role.")
    else:
        L.append("| # | by | role | independence | scope | outcome |"
                 " guarantees |")
        L.append("|---|----|------|--------------|-------|---------|"
                 "------------|")
        for e in passes:
            label = "INDEPENDENT" if e["independent"] else "SELF-ASSESSED"
            L.append("| %d | %s | %s | %s | %s | %s | %d/%d |"
                     % (e["seq"], e["by"], e["role"], label,
                        e["scope_sha256"][:12], e["outcome"],
                        e["verified_guarantees"], e["total_guarantees"]))
        L.append("")
        newest = passes[-1]
        indep = [e for e in passes if e["independent"]]
        if indep:
            newest_i = indep[-1]
            L.append("**Independent pass on record:** %s, %s — the"
                     % (newest_i["by"], _fmt_ts(newest_i)))
            L.append("latest such pass, by the auditors' own roster.")
            if not newest["independent"]:
                L.append("The latest pass overall was run by the")
                L.append("project itself (SELF-ASSESSED); both records")
                L.append("stand, and each is labelled by who ran it.")
        else:
            L.append("**No independent pass on record.** The latest")
            L.append("pass was run by the project itself and is")
            L.append("labelled SELF-ASSESSED; this report refuses to")
            L.append("call it independent — independence is a fact the")
            L.append("ledger names, not a fact it manufactures.")
        if newest["outcome"] == "failed":
            L.append("")
            L.append("**The latest pass FAILED** — the refusals it")
            L.append("recorded, verbatim:")
            for r in newest.get("refusals", []):
                L.append("- %s" % r)
    L.append("")
    L.append("## The findings")
    L.append("")
    if not findings:
        L.append("No findings on record. A clean pass so far — the")
        L.append("state line honestly stays underway, because the")
        L.append("ledger cannot know the auditors rested; this report")
        L.append("is the deliverable either way.")
    else:
        L.append("| id | severity | against | title | filed by |"
                 " reproducer |")
        L.append("|----|----------|---------|-------|-----------|"
                 "------------|")
        for fid in sorted(findings):
            f = findings[fid]
            repro = ("%s: %s" % (f["repro_mode"], f["repro_value"]))
            if f["repro_mode"] == "text" and len(repro) > 64:
                repro = repro[:61] + "..."
            L.append("| %s | %s | %s | %s | %s | %s |"
                     % (fid, f["severity"], f["against"], f["title"],
                        f["by"], repro))
    L.append("")
    L.append("## The triage and remediation")
    L.append("")
    if not triages:
        L.append("No triage on record yet — every finding above is")
        L.append("still awaiting the project's response, with a")
        L.append("written reason and, for acceptances, the remediation")
        L.append("commit itself.")
    else:
        L.append("| finding | verdict | by | reason | remediation |")
        L.append("|---------|---------|----|--------|-------------|")
        for fid in sorted(triages):
            t = triages[fid]
            L.append("| %s | %s | %s | %s | %s |"
                     % (fid, t["verdict"], t["by"], t["reason"],
                        t.get("fix", "—")))
    L.append("")
    L.append("## The state, honestly")
    L.append("")
    L.append("**%s.** %s" % (state.upper(), STATE_MEANING[state]))
    L.append("The word is derived from the census on every render,")
    L.append("never stored: responded requires every finding triaged;")
    L.append("remediated requires every acceptance to carry its")
    L.append("remediation commit. A finding-free audit stays underway")
    L.append("— the ledger cannot know the auditors rested; the report")
    L.append("is the deliverable either way.")
    L.append("")
    L.append("## The render contract")
    L.append("")
    L.append("No wall-clock unless `--stamp` (and then only the")
    L.append("Generated line). The ledger digest above is the sha256")
    L.append("of the raw ledger bytes: tamper with one byte of it and")
    L.append("the chain names the line — a tampered ledger is")
    L.append("evidence, not a document, and nothing renders from it.")
    L.append("")
    return "\n".join(L) + "\n"

# ---------------------------------------------------------------------------
# Shared plumbing.
# ---------------------------------------------------------------------------
def _tree_of(args):
    root = (os.path.abspath(args.root)
            if getattr(args, "root", None) else _ROOT)
    if not os.path.isdir(root):
        raise LedgerError("--root is not a directory: %s" % root)
    return root


def _ledger_of(args):
    if getattr(args, "ledger", None):
        return os.path.abspath(args.ledger)
    return os.path.join(_ROOT, LEDGER_REL)


def _stamp_or_none(args):
    if not getattr(args, "stamp", False):
        return None
    return subprocess.run(
        ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], timeout=10,
        stdout=subprocess.PIPE, text=True).stdout.strip()


def _require_engagement(entries):
    eng = engagement_of(entries)
    if eng is None:
        raise LedgerError(
            "no engagement on record — engage first: the audit kit "
            "writes nothing before the contract does")
    return eng


def _roster_refuse(by, roster, side):
    raise LedgerError(
        "%r is not on the auditor roster (%s) — %s. The roster is "
        "recorded at engage time and the ledger does not negotiate "
        "it afterwards" % (by, ", ".join(roster) or "empty", side))


# ---------------------------------------------------------------------------
# engage — the contract. One engagement per ledger, ever: a second is
# a new ledger (the chain arithmetic makes history immutable anyway).
# ---------------------------------------------------------------------------
def cmd_engage(args):
    root = _tree_of(args)
    path = _ledger_of(args)
    entries, _, _ = read_ledger(path, must_exist=False)
    if engagement_of(entries) is not None:
        raise LedgerError(
            "an engagement is already on record (seq 1, %s) — a "
            "contract is not a draft; start a new ledger for a new "
            "engagement" % entries[0]["firm"])
    by = args.by or default_actor(root)
    roster = [r for r in args.auditor if r]
    if not roster:
        raise LedgerError("the roster is empty — an audit with no "
                          "auditors is a monologue")
    if by in roster:
        raise LedgerError(
            "the signer (%r) cannot be on their own roster — "
            "independence starts at the signature" % by)
    try:
        parse_window(args.window)
    except ValueError as ex:
        raise LedgerError("the window %r is refused: %s"
                          % (args.window, ex))
    mod = load_scope(root)
    entry = append_chained(path, {
        "kind": "engagement",
        "tool": TOOL,
        "version": VERSION,
        "by": by,
        "firm": args.firm,
        "roster": roster,
        "window": args.window,
        "basis": args.basis,
        "scope_sha256": mod.ledger_digest(),
    })
    print("engaged: %s — %s" % (args.firm, args.window))
    print("  roster   : %s" % ", ".join(roster))
    print("  basis    : %r" % args.basis)
    print("  signed by: %s (the project side)" % by)
    print("  scope    : %d guarantees, sha256:%s"
          % (len(mod.LEDGER), entry["scope_sha256"][:16]))
    print("  chain    : seq %d, prev %s..."
          % (entry["seq"], entry["prev_hash"][:12]))
    return 0


# ---------------------------------------------------------------------------
# brief — the RFP. The only command that may run before a ledger
# exists: solicitation precedes contract.
# ---------------------------------------------------------------------------
def cmd_brief(args):
    root = _tree_of(args)
    path = _ledger_of(args)
    entries, _, _ = read_ledger(path, must_exist=False)
    mod = load_scope(root)
    problems = policy_anchor_ok(root, mod)
    if problems:
        for p in problems:
            sys.stderr.write("error: %s\n" % p)
        return 1
    engagement = engagement_of(entries)
    doc = render_brief(root, mod, engagement, stamp=_stamp_or_none(args))
    if args.out:
        out = os.path.abspath(args.out)
        d = os.path.dirname(out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print("brief: %s (%d guarantees, tree %s)"
              % (os.path.relpath(out, _ROOT)
                 if out.startswith(_ROOT) else out,
                 len(mod.LEDGER), git_sha(root)[:12]))
    else:
        sys.stdout.write(doc)
    return 0


# ---------------------------------------------------------------------------
# evidence — the pass. Re-runs the whole scope through the real
# checker (the hls-guarantee engine, imported), appends ONE chained
# transcript naming who ran it. Fail-closed AND fail-recorded: a
# failed pass is a record, not a shame.
# ---------------------------------------------------------------------------
def cmd_evidence(args):
    root = _tree_of(args)
    path = _ledger_of(args)
    entries, _, _ = read_ledger(path, must_exist=True)
    eng = _require_engagement(entries)
    by = args.by or default_actor(root)
    mod = load_scope(root)
    okc, results = mod.verify(root, quiet=True)
    n_ok = sum(1 for r in results["guarantees"]
               if r["status"] == "verified")
    n_anchors = sum(1 for r in results["guarantees"]
                    for a in r["anchors"] if a["ok"])
    n_probes = sum(1 for r in results["guarantees"]
                   for p in r["probes"] if p["verdict"] == "ok")
    refusals = list(results["refusals"])
    body = {
        "kind": "evidence",
        "tool": TOOL,
        "version": VERSION,
        "by": by,
        "role": "auditor" if by in eng["roster"] else "project",
        "independent": by in eng["roster"],
        "tree": git_sha(root),
        "scope_sha256": mod.ledger_digest(),
        "outcome": "ok" if okc else "failed",
        "verified_guarantees": n_ok,
        "total_guarantees": len(mod.LEDGER),
        "anchors": n_anchors,
        "probes": n_probes,
    }
    if not okc:
        body["refusals"] = refusals
    entry = append_chained(path, body)
    if args.json:
        print(json.dumps(entry, indent=2, sort_keys=True))
    else:
        label = "INDEPENDENT" if entry["independent"] \
            else "SELF-ASSESSED"
        print("transcript: seq %d by %s — %s"
              % (entry["seq"], by, label))
        print("  scope    : %d/%d guarantees, %d anchors, %d probes"
              % (n_ok, len(mod.LEDGER), n_anchors, n_probes))
        print("  outcome  : %s" % ("ok" if okc else "FAILED"))
    if not okc:
        sys.stderr.write(
            "EVIDENCE FAILED — %d refusal(s), recorded verbatim:\n"
            % len(refusals))
        for r in refusals:
            sys.stderr.write("  - %s\n" % r)
        return 1
    return 0


# ---------------------------------------------------------------------------
# file — the auditor's instrument.
# ---------------------------------------------------------------------------
def cmd_file(args):
    root = _tree_of(args)
    path = _ledger_of(args)
    entries, _, _ = read_ledger(path, must_exist=True)
    eng = _require_engagement(entries)
    by = args.by or default_actor(root)
    if by not in eng["roster"]:
        _roster_refuse(by, eng["roster"],
                       "findings are the auditors' to file")
    mod = load_scope(root)
    known = [g["id"] for g in mod.LEDGER]
    if args.against not in known:
        raise LedgerError(
            "finding against %r is refused — not one of the scope's "
            "rows (known: %s); a finding against nothing in the "
            "scope is out of the protocol by definition"
            % (args.against, ", ".join(known)))
    if args.severity not in AUDIT_SEVERITIES:
        raise LedgerError("severity %r is not one of %s"
                          % (args.severity,
                             "/".join(AUDIT_SEVERITIES)))
    if not args.title:
        raise LedgerError("a finding without a title names nothing")
    have_path = args.repro_path is not None
    have_text = args.repro_text is not None
    if have_path and have_text:
        raise LedgerError("a finding carries ONE reproducer, not "
                          "two: --repro-path or --repro-text")
    if not have_path and not have_text:
        raise LedgerError("a finding that cannot be reproduced is an "
                          "opinion, and opinions are not findings — "
                          "pass --repro-path or --repro-text")
    if have_path:
        if not os.path.isfile(os.path.join(root, args.repro_path)):
            raise LedgerError(
                "repro path %r does not exist in the tree — a "
                "reproducer that points at nothing is a rumor"
                % args.repro_path)
        mode, value = "path", args.repro_path
    else:
        mode, value = "text", args.repro_text
    existing = findings_of(entries)
    next_n = 1 + max((int(f["id"][len(FINDING_PREFIX):])
                      for f in existing.values()), default=0)
    fid = "%s%0*d" % (FINDING_PREFIX, FINDING_WIDTH, next_n)
    entry = append_chained(path, {
        "kind": "finding",
        "tool": TOOL,
        "version": VERSION,
        "id": fid,
        "by": by,
        "against": args.against,
        "severity": args.severity,
        "title": args.title,
        "repro_mode": mode,
        "repro_value": value,
    })
    print("filed %s (%s, against %s) by %s — seq %d"
          % (fid, args.severity, args.against, by, entry["seq"]))
    print("  reproducer: %s: %s"
          % (mode, value if len(value) <= 64
             else value[:61] + "..."))
    return 0


# ---------------------------------------------------------------------------
# triage — the project's response.
# ---------------------------------------------------------------------------
def cmd_triage(args):
    root = _tree_of(args)
    path = _ledger_of(args)
    entries, _, _ = read_ledger(path, must_exist=True)
    eng = _require_engagement(entries)
    by = args.by or default_actor(root)
    if by in eng["roster"]:
        raise LedgerError(
            "%r is on the auditor roster — the response is the "
            "project's to give, and an auditor cannot triage their "
            "own finding" % by)
    findings = findings_of(entries)
    triages = triages_of(entries)
    fid = args.finding
    if not is_finding_id(fid):
        raise LedgerError("a finding id is AF-NNN, not %r" % fid)
    if fid not in findings:
        raise LedgerError(
            "no finding %s on record (filed: %s)"
            % (fid, ", ".join(sorted(findings)) or "none"))
    if fid in triages:
        raise LedgerError(
            "%s already carries its triage (seq %d) — one finding, "
            "one response; the record is the response, not a "
            "negotiation" % (fid, triages[fid]["seq"]))
    if args.verdict not in TRIAGE_VERDICTS:
        raise LedgerError("verdict %r is not one of %s"
                          % (args.verdict, "/".join(TRIAGE_VERDICTS)))
    if not args.reason:
        raise LedgerError("a triage without a reason is refused — "
                          "the sanction law, on the other side of "
                          "the table")
    body = {
        "kind": "triage",
        "tool": TOOL,
        "version": VERSION,
        "by": by,
        "for": fid,
        "verdict": args.verdict,
        "reason": args.reason,
    }
    if args.verdict == "accepted":
        if not args.fix:
            raise LedgerError(
                "an acceptance without its remediation is a wish, "
                "not a fix — pass --fix <40-hex commit>")
        if not is_hex40(args.fix):
            raise LedgerError("the fix must be 40 hex, not %r"
                              % args.fix[:44])
        known = commit_exists(root, args.fix)
        if known is False:
            raise LedgerError(
                "fix %s names no commit in this tree — a remediation "
                "that exists nowhere is a promise, not a commit"
                % args.fix[:12])
        if known is None:
            print("  note: not a git tree — the fix is validated by "
                  "shape only")
        body["fix"] = args.fix
    else:
        if args.fix:
            raise LedgerError(
                "only an acceptance carries a fix: a %s finding with "
                "a remediation is a contradiction — fix it first, "
                "then re-file" % args.verdict)
    entry = append_chained(path, body)
    print("triaged %s: %s by %s — seq %d"
          % (fid, args.verdict, by, entry["seq"]))
    print("  reason: %r" % args.reason)
    if "fix" in entry:
        print("  fix    : %s" % entry["fix"])
    return 0


# ---------------------------------------------------------------------------
# report — the deliverable.
# ---------------------------------------------------------------------------
def cmd_report(args):
    if not args.out:
        sys.stderr.write("error: report needs --out FILE.md\n")
        return 2
    path = _ledger_of(args)
    entries, head, log_sha = read_ledger(path, must_exist=True)
    doc = render_report(path, entries, head, log_sha,
                        stamp=_stamp_or_none(args))
    out = os.path.abspath(args.out)
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print("report: %s (%d entries, state %s)"
          % (os.path.relpath(out, _ROOT) if out.startswith(_ROOT)
             else out, len(entries), derive_state(entries)))
    return 0


# ---------------------------------------------------------------------------
# status — the census, one look.
# ---------------------------------------------------------------------------
def cmd_status(args):
    path = _ledger_of(args)
    entries, _, _ = read_ledger(path, must_exist=True)
    state = derive_state(entries)
    eng = engagement_of(entries)
    passes = passes_of(entries)
    findings = findings_of(entries)
    triages = triages_of(entries)
    n_ind = sum(1 for p in passes if p["independent"])
    sev = {s: 0 for s in AUDIT_SEVERITIES}
    for f in findings.values():
        sev[f["severity"]] += 1
    n_acc = sum(1 for t in triages.values()
                if t["verdict"] == "accepted")
    if args.json:
        print(json.dumps({
            "tool": TOOL, "version": VERSION, "policy": POLICY,
            "ledger": path, "state": state,
            "state_meaning": STATE_MEANING[state],
            "entries": len(entries),
            "engagement": None if eng is None else {
                "firm": eng["firm"], "roster": eng["roster"],
                "window": eng["window"], "basis": eng["basis"],
                "by": eng["by"],
                "scope_sha256": eng["scope_sha256"]},
            "evidence": {
                "passes": len(passes), "independent": n_ind,
                "self_assessed": len(passes) - n_ind,
                "latest_outcome": passes[-1]["outcome"] if passes
                else None},
            "findings": {"total": len(findings), **sev},
            "triage": {"total": len(triages), "accepted": n_acc,
                       "disputed": sum(1 for t in triages.values()
                                       if t["verdict"] == "disputed"),
                       "out_of_scope": sum(
                           1 for t in triages.values()
                           if t["verdict"] == "out-of-scope")},
            "fixes": sum(1 for t in triages.values() if "fix" in t),
        }, indent=2, sort_keys=True))
        return 0
    print("%s %s — %s" % (TOOL, VERSION,
                          os.path.relpath(path, _ROOT)
                          if path.startswith(_ROOT) else path))
    print("  state     : %s — %s" % (state, STATE_MEANING[state]))
    if eng is not None:
        print("  engagement: %s — window %s" % (eng["firm"],
                                                eng["window"]))
        print("              basis %r (signed by %s)"
              % (eng["basis"], eng["by"]))
        print("              roster: %s" % ", ".join(eng["roster"]))
    else:
        print("  engagement: none on record")
    if passes:
        latest = passes[-1]
        print("  evidence  : %d pass(es) — %d independent, %d "
              "self-assessed; latest outcome %s"
              % (len(passes), n_ind, len(passes) - n_ind,
                 latest["outcome"]))
    else:
        print("  evidence  : no pass on record")
    print("  findings  : %d on record — %s"
          % (len(findings),
             ", ".join("%s %d" % (s, sev[s])
                       for s in AUDIT_SEVERITIES)))
    print("  triage    : %d on record — accepted %d, disputed %d, "
          "out-of-scope %d"
          % (len(triages), n_acc,
             sum(1 for t in triages.values()
                 if t["verdict"] == "disputed"),
             sum(1 for t in triages.values()
                 if t["verdict"] == "out-of-scope")))
    print("  fixes     : %d remediation commit(s) recorded"
          % sum(1 for t in triages.values() if "fix" in t))
    return 0


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog=TOOL,
        description="the Stage 140 third-party audit kit: the "
                    "independent, paid security audit as a chained "
                    "ledger")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ledger", default=None, help="the "
                         "engagement ledger (default: "
                         ".hlaudit/engagement.jsonl under the repo "
                         "the tool lives in — never under --root: "
                         "one project, one engagement ledger)")
    common.add_argument("--root", default=None,
                         help="the tree to audit (default: the repo "
                              "the tool lives in)")
    sub = ap.add_subparsers(dest="cmd")

    p_eng = sub.add_parser("engage", parents=[common], help="sign "
                            "the contract: the firm, the roster, the "
                            "window, the basis")
    p_eng.add_argument("--firm", required=True)
    p_eng.add_argument("--auditor", action="append", default=[],
                       metavar="NAME", help="a roster member; "
                       "repeatable")
    p_eng.add_argument("--window", required=True,
                       help="YYYY-MM-DD/YYYY-MM-DD")
    p_eng.add_argument("--basis", required=True,
                       help="the fee basis, recorded verbatim")
    p_eng.add_argument("--by", default=None,
                       help="the signer (default: the tree's git "
                            "identity)")
    p_eng.set_defaults(fn=cmd_engage)

    p_brf = sub.add_parser("brief", parents=[common], help="render "
                           "the RFP from the live scope")
    p_brf.add_argument("--out", default=None,
                        help="write to FILE.md (default: stdout)")
    p_brf.add_argument("--stamp", action="store_true")
    p_brf.set_defaults(fn=cmd_brief)

    p_evd = sub.add_parser("evidence", parents=[common], help="run "
                           "a pass; the label follows the roster")
    p_evd.add_argument("--by", default=None,
                       help="who ran the pass (default: the tree's "
                            "git identity)")
    p_evd.add_argument("--json", action="store_true")
    p_evd.set_defaults(fn=cmd_evidence)

    p_fil = sub.add_parser("file", parents=[common], help="the "
                           "auditors' instrument: file one finding")
    p_fil.add_argument("--against", required=True,
                       help="the guarantee id from the scope")
    p_fil.add_argument("--severity", required=True)
    p_fil.add_argument("--title", required=True)
    p_fil.add_argument("--repro-path", default=None,
                       help="a reproducer path in the tree")
    p_fil.add_argument("--repro-text", default=None,
                       help="inline reproducer text")
    p_fil.add_argument("--by", default=None)
    p_fil.set_defaults(fn=cmd_file)

    p_tri = sub.add_parser("triage", parents=[common], help="the "
                           "project's response: one verdict, one "
                           "reason")
    p_tri.add_argument("finding", help="AF-NNN")
    p_tri.add_argument("--verdict", required=True)
    p_tri.add_argument("--reason", required=True)
    p_tri.add_argument("--fix", default=None,
                       help="the remediation commit (40 hex); "
                            "required for accepted")
    p_tri.add_argument("--by", default=None)
    p_tri.set_defaults(fn=cmd_triage)

    p_rep = sub.add_parser("report", parents=[common], help="render "
                           "the deliverable from the chained ledger")
    p_rep.add_argument("--out", required=True)
    p_rep.add_argument("--stamp", action="store_true")
    p_rep.set_defaults(fn=cmd_report)

    p_sta = sub.add_parser("status", parents=[common], help="the "
                           "census, one look")
    p_sta.add_argument("--json", action="store_true")
    p_sta.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help(sys.stderr)
        return 2
    try:
        return args.fn(args)
    except (LedgerError, ScopeError) as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
