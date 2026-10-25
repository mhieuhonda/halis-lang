#!/usr/bin/env python3
"""hls-finalaudit — the Stage 144 final independent third-party
security audit: the engagement that gates the v1.0 release
candidates.

Usage:
  python3 tools/hls-finalaudit.py engage --firm "..."
                                --auditor "A" [--auditor "B" ...]
                                --window "YYYY-MM-DD/YYYY-MM-DD"
                                --basis "..." [--by NAME]
                                [--root PATH] [--ledger PATH]
  python3 tools/hls-finalaudit.py brief [--out FINALBRIEF.md]
                                [--stamp] [--root PATH]
                                [--ledger PATH]
  python3 tools/hls-finalaudit.py evidence [--by NAME] [--root PATH]
                                [--ledger PATH] [--json]
  python3 tools/hls-finalaudit.py file --against G-XXX-NN
                                --severity critical|high|medium|low|info
                                --title "..."
                                (--repro-path PATH | --repro-text "...")
                                [--by NAME] [--root PATH] [--ledger PATH]
  python3 tools/hls-finalaudit.py triage AF-001
                                --verdict accepted|disputed|out-of-scope
                                --reason "..." [--fix <40-hex>]
                                [--by NAME] [--root PATH] [--ledger PATH]
  python3 tools/hls-finalaudit.py signoff [--by NAME] [--root PATH]
                                [--ledger PATH]
  python3 tools/hls-finalaudit.py gate [--root PATH] [--ledger PATH]
                                [--json]
  python3 tools/hls-finalaudit.py report --out FINALREPORT.md
                                [--stamp] [--root PATH] [--ledger PATH]
  python3 tools/hls-finalaudit.py status [--json] [--root PATH]
                                [--ledger PATH]

Roadmap Stage 144 is one phrase: "Independent third-party security
audit (final)". Stage 140 built the ENGAGEMENT kit — the machinery
any audit runs on: a chained ledger, the independence labels, the
findings protocol, the one-response triage. What Stage 140 could
not promise is the word in this stage's parentheses. FINAL is a
property of the artifact, not of the paperwork: an audit is final
when it was run against the thing that will ship, when its closure
is demonstrated rather than declared, and when nothing can be
released while it is open. This stage turns that word into nine
commands and one ledger (.hlfa/final.jsonl, machine-local like
every ledger in this tree):

  1. the PINNED-ARTIFACT LAW. A Stage 140 engagement scoped one
     digest — the guarantee ledger's — and honestly recorded the
     drift when the scope moved under the auditors. The final
     audit is narrower on purpose: it audits THE FROZEN
     ARTIFACT, and pins THREE anchors at the signature — the
     Stage 135 guarantee scope (imported live, never copied),
     the Stage 141 API freeze (the committed document's surface
     digest plus the head of its amendment chain), and the
     Stage 142/143 bootstrap snapshot (the manifest's closure
     and snapshot digests). Every evidence pass re-derives all
     three live and records what it ran; the signoff refuses to
     close on any anchor that moved since the engagement — a
     moved surface is a new engagement, not an amendment, and
     the gate enforces the same law after the seal;

  2. the CLOSURE-REQUIRES-INDEPENDENCE LAW. Stage 140's state
     machine reached "remediated" on self-assessed evidence
     alone — honest (the labels never lied) but not final: an
     audit that closes on the project's own passes is a rubber
     stamp with a paper trail. The signoff refuses unless at
     least one INDEPENDENT pass is on record and the LATEST
     pass of any label ran green;

  3. the FIX-LANDING LAW. Stage 140 required the remediation
     commit to EXIST; the final audit requires it to have
     LANDED: the fix must be an ancestor of the tree's HEAD at
     triage time, again at signoff time, and again at the gate
     — a fix that is not in the tree being released is a
     promise, not a fix. And it must postdate the finding it
     fixes: a commit whose committer timestamp precedes the
     finding entry's is a contradiction, refused with both
     timestamps named. The landing law needs a real git tree;
     the signoff refuses to close on a tree whose history it
     cannot read;

  4. the SEAL LAW. `signoff` is the project's closing act (an
     auditor cannot sign off — the auditors' deliverable is the
     findings, not the closure). It writes ONE entry carrying
     the tree, the three anchors re-derived live at seal time,
     and the closure census — and it seals the ledger: after
     the seal every writer refuses, by name, because what
     comes after the seal is the next audit, not an amendment.
     Readers still read; the chain still verifies; the record
     is closed;

  5. the GATE LAW. `gate` is what the release process runs
     before cutting a candidate: exit 0 only when the ledger
     is sealed, the sealed tree is an ANCESTOR of the current
     HEAD (the candidate is cut from the audited tree or its
     descendants — never around the audit), the three anchors
     still match what the seal pinned, and every fix still
     lands. Anything else exits 1 with the named refusal. RC1
     is not a date on a calendar; it is what this gate says
     yes to.

The findings protocol, the triage, the independence labels and
the chain arithmetic are Stage 140's, unchanged: findings are the
auditors' to file (against a live scope row, a closed severity
word, exactly one reproducer), the triage is the project's to
give (one verdict, a written reason, the strict verdict/fix
pairing), and the ledger speaks the family's ONE arithmetic
(sha256(prev + canonical-json(entry minus chain_hash)),
imported from hltlog_parts since Stage 13) — every reader
verifies the whole chain first and names the broken line.

The state is derived from the census, never stored:
vacant -> open -> underway -> reported -> responded ->
remediated -> sealed. The seventh word is this stage's own.

The timestamps on entries are facts (when the op happened); the
rendered documents are deterministic per input — sorted walks, no
wall-clock unless --stamp — so two renders of one ledger are
byte-identical, and any hand-edit is visible as a diff.

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

TOOL = "hls-finalaudit"
VERSION = "0.165.0-alpha"          # the toolchain release this run stamps
POLICY = "halis-finalaudit-1"
LEDGER_REL = os.path.join(".hlfa", "final.jsonl")

GUARANTEE_TOOL = "hls-guarantee.py"     # the Stage 135 ledger, imported
FREEZE_DOC = os.path.join("tests", "api_freeze.json")
FREEZE_AMENDMENTS = os.path.join("tests", "api_freeze_amendments.jsonl")
SNAPSHOT_MANIFEST = os.path.join("snapshot", "MANIFEST.json")
SECURITY_MD = "SECURITY.md"

# ---------------------------------------------------------------------------
# The closed vocabularies. The severity words, the verdict words and the
# reproducer laws are Stage 140's, carried over verbatim: the final audit
# changes what CLOSES an engagement, not how a finding is filed.
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

REPRO_MODES = ("path", "text")
PASS_OUTCOMES = ("ok", "failed")
ROLES = ("auditor", "project")

# The finding id grammar: AF-NNN, three digits, sequential from 1.
FINDING_PREFIX = "AF-"
FINDING_WIDTH = 3

# The lifecycle, ordered. Derived from the census on every render;
# never stored, never asserted by a writer. The seventh word —
# sealed — is Stage 144's own contribution to the family.
STATES = ("vacant", "open", "underway", "reported", "responded",
          "remediated", "sealed")
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
    "sealed":
    "the project signed the closure — the three anchors pinned, "
    "the independent pass on record, every fix landed — and the "
    "ledger is closed; what comes after the seal is the next "
    "audit, not an amendment",
}

# The three Phase X anchors. A Stage 140 engagement pinned ONE
# digest and watched it drift; the final audit pins the FROZEN
# ARTIFACT whole: what the toolchain guarantees (Stage 135), what
# the surface is (Stage 141), and what the shipped compiler bytes
# are (Stage 142/143). An audit is final when the thing it audited
# is the thing that ships.
ANCHOR_NAMES = ("guarantee-scope", "api-freeze", "bootstrap-snapshot")
ANCHOR_MEANING = {
    "guarantee-scope":
    "the Stage 135 soundness guarantee ledger, imported live — "
    "the scope the evidence passes re-run through the real "
    "checker",
    "api-freeze":
    "the Stage 141 frozen API document: the surface digest plus "
    "the head of its amendment chain — the contract every "
    "program ever written against this language rests on",
    "bootstrap-snapshot":
    "the Stage 142/143 snapshot manifest: the compiler bytes and "
    "their import closure — the artifact every release build "
    "starts from, byte for byte",
}

# ---------------------------------------------------------------------------
# Identity. --by is recorded verbatim on every entry; the default is
# the tree's own git identity, so a project-run pass is honestly
# labelled SELF-ASSESSED unless the auditors run it themselves.
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
    if _own_repo(tree) is not None:
        try:
            out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree,
                                 timeout=15, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
            if out.returncode == 0:
                return out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "unknown (not a git tree)"


def _own_repo(tree):
    """-> the tree's own repository toplevel, or None. `git
    rev-parse` walks UP the directory tree, so a tarball unpacked
    inside somebody else's repo would silently answer questions
    about the WRONG history — the landing law refuses to read a
    history the tree does not own."""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             cwd=tree, timeout=15,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        if out.returncode == 0 and out.stdout.strip():
            if os.path.realpath(out.stdout.strip()) \
                    == os.path.realpath(tree):
                return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def is_git_tree(tree):
    """-> True when the tree carries its OWN .git — not a parent's."""
    return _own_repo(tree) is not None


def commit_exists(tree, sha):
    """-> True when `sha` names a commit that exists in the tree's
    own repository. None when the tree owns no repository (an
    auditor working from a tarball) — the caller validates shape
    only and says so."""
    if _own_repo(tree) is None:
        return None
    try:
        out = subprocess.run(
            ["git", "cat-file", "-e", sha + "^{commit}"], cwd=tree,
            timeout=15, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        if out.returncode == 0:
            return True
        return False
    except (OSError, subprocess.TimeoutExpired):
        return None


def commit_landed(tree, sha):
    """-> True when `sha` is an ancestor of the tree's HEAD — the
    fix-landing law. False when it exists but never landed; None
    when the tree's history cannot be asked."""
    if _own_repo(tree) is None:
        return None
    try:
        out = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
            cwd=tree, timeout=15, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def commit_timestamp(tree, sha):
    """-> the commit's committer timestamp (unix seconds), or None
    when the tree cannot be asked. The fix-date law compares this
    against the finding entry's own timestamp."""
    if _own_repo(tree) is None:
        return None
    try:
        out = subprocess.run(
            ["git", "show", "-s", "--format=%ct", sha], cwd=tree,
            timeout=15, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None

# ---------------------------------------------------------------------------
# The three anchors. Each is derived LIVE from the tree --root
# points at, never copied, never cached across roots; each collapses
# to one 64-hex digest plus the human detail the renders quote.
# ---------------------------------------------------------------------------
class AnchorError(Exception):
    """An anchor cannot be derived — nothing can be pinned, scoped
    or closed until it can."""


_SCOPE_CACHE = {}


def load_scope(root):
    """-> the hls-guarantee module loaded from `root` (cached per
    root). Raises AnchorError when the tree carries no ledger."""
    root = os.path.abspath(root)
    if root in _SCOPE_CACHE:
        return _SCOPE_CACHE[root]
    path = os.path.join(root, "tools", GUARANTEE_TOOL)
    if not os.path.isfile(path):
        shown = (os.path.relpath(path, root)
                 if os.path.isdir(root) else path)
        raise AnchorError(
            "no guarantee ledger at %s — the final audit's first "
            "anchor is the Stage 135 ledger, and this tree does "
            "not carry one" % shown)
    spec = importlib.util.spec_from_file_location(
        "hls_guarantee_at_%d" % (abs(hash(root)) % 100000), path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as ex:                       # noqa: BLE001
        raise AnchorError("the guarantee ledger at %s does not "
                          "load: %s"
                          % (os.path.relpath(path, root), ex))
    _SCOPE_CACHE[root] = mod
    return mod


def read_freeze(root):
    """-> (surface_digest, amendments_head, amendments_count) from
    the committed Stage 141 document and its amendment chain. The
    chain is verified with the family's arithmetic — the final
    audit refuses to pin a broken freeze."""
    doc_path = os.path.join(root, FREEZE_DOC)
    if not os.path.isfile(doc_path):
        shown = (os.path.relpath(doc_path, root)
                 if os.path.isdir(root) else doc_path)
        raise AnchorError(
            "no API freeze at %s — the final audit audits the "
            "frozen artifact, and this tree carries no freeze to "
            "audit" % shown)
    try:
        with open(doc_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as ex:
        raise AnchorError("the freeze document at %s is not JSON: "
                          "%s" % (FREEZE_DOC, ex))
    digest = doc.get("digest")
    if not is_hex64(digest):
        raise AnchorError("the freeze document at %s carries no "
                          "64-hex surface digest — a freeze that "
                          "cannot name itself cannot be pinned"
                          % FREEZE_DOC)

    head = GENESIS
    count = 0
    am_path = os.path.join(root, FREEZE_AMENDMENTS)
    if os.path.isfile(am_path):
        with open(am_path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().split("\n") if ln.strip()]
        prev = GENESIS
        for i, line in enumerate(lines, 1):
            try:
                entry = json.loads(line)
            except ValueError as ex:
                raise AnchorError(
                    "the freeze amendment chain is broken at line "
                    "%d (not JSON: %s) — the final audit refuses "
                    "to pin a broken freeze" % (i, ex))
            if entry.get("seq") != i \
                    or entry.get("prev_hash") != prev \
                    or entry.get("chain_hash") != chain_of(prev, entry):
                raise AnchorError(
                    "the freeze amendment chain is broken at line "
                    "%d — the final audit refuses to pin a broken "
                    "freeze" % i)
            prev = entry["chain_hash"]
        head = prev
        count = len(lines)
    return digest, head, count


def read_snapshot(root):
    """-> (closure_digest, hlc_c_sha256, version) from the Stage
    142/143 manifest. The manifest is the only witness of what the
    shipped compiler bytes are."""
    path = os.path.join(root, SNAPSHOT_MANIFEST)
    if not os.path.isfile(path):
        shown = (os.path.relpath(path, root)
                 if os.path.isdir(root) else path)
        raise AnchorError(
            "no snapshot manifest at %s — the final audit audits "
            "the artifact every release build starts from, and "
            "this tree carries none" % shown)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            man = json.load(fh)
    except ValueError as ex:
        raise AnchorError("the snapshot manifest at %s is not "
                          "JSON: %s" % (SNAPSHOT_MANIFEST, ex))
    closure = man.get("closure_digest")
    snap = man.get("hlc_c_sha256")
    if not is_hex64(closure) or not is_hex64(snap):
        raise AnchorError("the snapshot manifest at %s carries no "
                          "64-hex closure/snapshot digests — a "
                          "manifest that cannot name the bytes "
                          "cannot be pinned" % SNAPSHOT_MANIFEST)
    return closure, snap, str(man.get("version", "?"))


def _anchor_digest(parts):
    """One digest over the canonical JSON of the parts."""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def anchors_of(root):
    """-> {name: {"digest", "detail"}} for all three anchors,
    derived live. Raises AnchorError when any cannot be."""
    mod = load_scope(root)
    g_digest = mod.ledger_digest()
    f_digest, f_head, f_count = read_freeze(root)
    s_closure, s_snap, s_version = read_snapshot(root)
    return {
        "guarantee-scope": {
            "digest": g_digest,
            "detail": "%d guarantees, live from the Stage 135 "
                      "ledger" % len(mod.LEDGER),
        },
        "api-freeze": {
            "digest": _anchor_digest({"digest": f_digest,
                                      "amendments_head": f_head}),
            "detail": "surface sha256:%s, %d amendment(s) on "
                      "chain" % (f_digest[:16], f_count),
        },
        "bootstrap-snapshot": {
            "digest": _anchor_digest({"closure": s_closure,
                                      "snapshot": s_snap,
                                      "version": s_version}),
            "detail": "snapshot sha256:%s, closure sha256:%s "
                      "(%s)" % (s_snap[:16], s_closure[:16],
                                s_version),
        },
    }


def anchor_drift(pinned, live):
    """-> [names whose digest differs], in ANCHOR_NAMES order."""
    return [n for n in ANCHOR_NAMES
            if pinned.get(n) != live.get(n)]


def policy_anchor_ok(root, mod):
    """The live-quote law inherited from Stage 140: the reporting
    channel anchored in SECURITY.md must still be there, or the
    brief refuses."""
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
# of five kinds: engagement / evidence / finding / triage /
# signoff. The chain arithmetic is imported, not re-derived; every
# reader verifies the whole chain first and names the broken line.
# ---------------------------------------------------------------------------
KINDS = ("engagement", "evidence", "finding", "triage", "signoff")


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


def _need_anchors(entry, what, line_no):
    v = entry.get("anchors")
    if not isinstance(v, dict) or set(v) != set(ANCHOR_NAMES) \
            or not all(is_hex64(v[n]) for n in ANCHOR_NAMES):
        raise LedgerError(
            "%s: line %d: anchors must carry all three (%s), each "
            "64 hex" % (what, line_no, "/".join(ANCHOR_NAMES)))
    return v


def entry_shape(entry, line_no):
    """The per-kind shape, checked on every read. Shape only — the
    ledger reader never re-derives live facts (the anchors may have
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
        _need_anchors(entry, what, line_no)

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
        _need_anchors(entry, what, line_no)
        drift = entry.get("anchor_drift", [])
        if not isinstance(drift, list) \
                or not all(d in ANCHOR_NAMES for d in drift):
            raise LedgerError("%s: line %d: anchor_drift must be a "
                              "list of anchor names" % (what, line_no))
        if entry.get("outcome") not in PASS_OUTCOMES:
            raise LedgerError("%s: line %d: outcome %r is not one of "
                              "%s" % (what, line_no, entry.get("outcome"),
                                      "/".join(PASS_OUTCOMES)))
        for key in ("verified_guarantees", "total_guarantees",
                    "guarantee_anchors", "guarantee_probes"):
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

    elif kind == "signoff":
        _need(entry, "by", str, what, line_no)
        _need(entry, "tree", str, what, line_no)
        _need_anchors(entry, what, line_no)
        for key in ("passes", "independent_passes", "findings",
                    "triages", "fixes"):
            if not isinstance(entry.get(key), int) or entry[key] < 0:
                raise LedgerError("%s: line %d: %s must be an int >= 0"
                                  % (what, line_no, key))


def read_ledger(path, must_exist=True):
    """-> (entries, head, log_sha256). Verifies the whole chain first:
    a tampered ledger is named at its broken line, never rendered."""
    if not os.path.isfile(path):
        if must_exist:
            raise LedgerError("no final-audit ledger at %s — nothing "
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


def signoff_of(entries):
    """The seal, or none. The writer refuses to write a second; the
    census takes the first it sees, like every derived fact."""
    for e in entries:
        if e["kind"] == "signoff":
            return e
    return None


def derive_state(entries):
    """The lifecycle word, from the census alone. The seventh word
    — sealed — is a signoff entry on record; a finding-free audit
    honestly stays underway, exactly as Stage 140 taught."""
    if not entries:
        return "vacant"
    if engagement_of(entries) is None:
        return "open"
    if signoff_of(entries) is not None:
        return "sealed"
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


def closure_refusals(entries, root):
    """-> [refusal strings] between this ledger and a legal signoff.
    The closure laws, in order: the census, the independence, the
    anchors, the landing. Empty list == the seal may be written."""
    out = []
    eng = engagement_of(entries)
    if eng is None:
        out.append("no engagement on record — engage first: the "
                   "final audit writes nothing before the contract "
                   "does")
        return out
    findings = findings_of(entries)
    triages = triages_of(entries)
    passes = passes_of(entries)
    if not passes:
        out.append("no evidence pass on record — an audit with no "
                   "transcript has nothing to close")
    unfixed = [t for t in triages.values()
               if t["verdict"] == "accepted" and "fix" not in t]
    for fid in sorted(findings):
        if fid not in triages:
            out.append("finding %s carries no triage — every finding "
                       "is answered before the audit closes" % fid)
    if unfixed:
        out.append("%d acceptance(s) without their remediation — an "
                   "acceptance without its fix is a wish, not a fix"
                   % len(unfixed))
    if not any(p["independent"] for p in passes):
        out.append("no independent pass on record — the final audit "
                   "does not close on the project's own evidence; "
                   "an audit that closes on self-assessed passes is "
                   "a rubber stamp with a paper trail")
    if passes and passes[-1]["outcome"] != "ok":
        out.append("the latest pass failed — the final audit does "
                   "not close on a failed pass; re-run the evidence "
                   "and let the refusals be fixed first")
    # the pinned-artifact law, at closure time: live == engaged
    try:
        live = anchors_of(root)
    except AnchorError as ex:
        out.append("the live anchors cannot be derived (%s) — the "
                   "final audit closes only on the artifact it was "
                   "signed against" % ex)
        live = None
    if live is not None:
        flat = {n: live[n]["digest"] for n in ANCHOR_NAMES}
        moved = anchor_drift(eng["anchors"], flat)
        for n in moved:
            out.append(
                "the %s anchor moved since the engagement (engaged "
                "sha256:%s..., live sha256:%s...) — the final audit "
                "closes only on the artifact it was signed against; "
                "a moved surface is a new engagement, not an "
                "amendment" % (n, eng["anchors"][n][:24],
                               live[n]["digest"][:24]))
    # the landing law, at closure time
    if not is_git_tree(root):
        out.append("the tree is not a git repository — the landing "
                   "law cannot be verified, and the final audit does "
                   "not close on a tree whose history it cannot read")
    else:
        head = git_sha(root)
        for fid in sorted(triages):
            t = triages[fid]
            if t["verdict"] != "accepted":
                continue
            landed = commit_landed(root, t["fix"])
            if landed is False:
                out.append(
                    "the remediation for %s (%s) is not an ancestor "
                    "of HEAD %s — a fix that has not landed is a "
                    "promise, not a fix" % (fid, t["fix"][:12],
                                            head[:12]))
            finding = findings.get(fid)
            if finding is not None:
                when = commit_timestamp(root, t["fix"])
                if when is not None and when < finding["timestamp"]:
                    out.append(
                        "the remediation for %s predates the finding "
                        "it fixes (committed %d, filed %d) — a fix "
                        "that predates its finding is a contradiction"
                        % (fid, when, finding["timestamp"]))
    return out

# ---------------------------------------------------------------------------
# Renders. Both documents are deterministic per input (the tree for
# the brief, the ledger bytes for the report): sorted walks, no
# wall-clock unless --stamp — two renders byte-identical, so a
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


def render_brief(root, mod, anchors, engagement, stamp=None):
    census = [{"id": g["id"], "family": g["family"],
               "severity": g["severity"], "claim": g["claim"],
               "anchors": len(g["anchors"]),
               "probes": len(g["probes"])}
              for g in mod.LEDGER]
    tree = git_sha(root)
    firm_line = ("OPEN SOLICITATION — no contract on record; the terms "
                 "below are the standing proposal, to be recorded "
                 "verbatim at engage time")
    if engagement is not None:
        firm_line = ("%s — window %s, basis on record: %r (signed by %s)"
                     % (engagement["firm"], engagement["window"],
                        engagement["basis"], engagement["by"]))
    L = []
    L.append("# Halis — FINAL third-party security audit brief (RFP)")
    L.append("")
    L.append("Regenerated from the live tree by `%s`." % TOOL)
    L.append("This document is generated, not maintained. Do not")
    L.append("hand-edit; regenerate with")
    L.append("`python3 tools/%s.py brief --out FINALBRIEF.md`." % TOOL)
    L.append("")
    L.append("This is the engagement the v1.0 release candidates")
    L.append("wait on: its closure is what `gate` checks before a")
    L.append("candidate is cut, and a candidate cut while this audit")
    L.append("is open is a candidate cut from an unaudited tree.")
    L.append("")
    L.append("- Policy: %s" % POLICY)
    L.append("- Toolchain: %s" % VERSION)
    L.append("- Tree: `%s`" % tree)
    L.append("- Engagement: %s" % firm_line)
    if stamp:
        L.append("- Generated: %s" % stamp)
    L.append("")
    L.append("## The pinned artifact — the three anchors")
    L.append("")
    L.append("A Stage 140 engagement scoped one digest and watched it")
    L.append("drift. The final audit is narrower on purpose: it audits")
    L.append("the FROZEN ARTIFACT, and the signature pins three")
    L.append("anchors at once — what the toolchain guarantees, what")
    L.append("the surface is, and what the shipped compiler bytes are.")
    L.append("The signoff refuses to close on any anchor that moved")
    L.append("since the engagement, and the gate enforces the same law")
    L.append("after the seal. An audit is final when the thing it")
    L.append("audited is the thing that ships.")
    L.append("")
    L.append("| anchor | meaning | digest | live detail |")
    L.append("|--------|---------|--------|-------------|")
    for n in ANCHOR_NAMES:
        L.append("| %s | %s | `sha256:%s` | %s |"
                 % (n, ANCHOR_MEANING[n], anchors[n]["digest"],
                    anchors[n]["detail"]))
    L.append("")
    L.append("## What the auditors are asked to do")
    L.append("")
    L.append("1. RE-DERIVE every guarantee anchor: each row below")
    L.append("   rests on named enforcement points in the tree; find")
    L.append("   them, read them, and say whether the mechanism is what")
    L.append("   the row claims.")
    L.append("2. RE-RUN every probe through the real checker: the")
    L.append("   fixtures are pinned refusals and panics; confirm the")
    L.append("   toolchain still refuses and still panics as written.")
    L.append("3. ATTEMPT the violation contracts, bounty-style: write")
    L.append("   programs that should be refused and are not; the")
    L.append("   contract of each row below is the boundary to attack.")
    L.append("4. RUN your evidence through this kit, under your own")
    L.append("   names: the pass is INDEPENDENT only when the roster")
    L.append("   ran it, and the closure below requires one.")
    L.append("5. FILE findings per the protocol below — and nothing")
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
    L.append("- Findings are the AUDITORS' to file — the project cannot")
    L.append("  author one, and an auditor cannot triage one. The")
    L.append("  response is the project's to give, with a written")
    L.append("  reason; an acceptance carries its remediation commit,")
    L.append("  40 hex naming a commit that exists.")
    L.append("")
    L.append("## The closure criteria — what makes this audit final")
    L.append("")
    L.append("This is where the final audit leaves the Stage 140")
    L.append("machinery behind. An engagement that ends `remediated` is")
    L.append("a finished audit; only these close a FINAL one:")
    L.append("")
    L.append("- an INDEPENDENT evidence pass on record — the audit does")
    L.append("  not close on the project's own evidence, and the latest")
    L.append("  pass of any label must have run green;")
    L.append("- every acceptance's remediation commit LANDED: an")
    L.append("  ancestor of the tree's HEAD, and not older than the")
    L.append("  finding it fixes — a fix that predates its finding is")
    L.append("  a contradiction, refused with both timestamps named;")
    L.append("- the three anchors unchanged since the signature: the")
    L.append("  signoff closes only on the artifact engaged; a moved")
    L.append("  surface is a new engagement, not an amendment;")
    L.append("- the SIGNOFF: the project's own act, written by the")
    L.append("  project side, sealing the ledger — after the seal")
    L.append("  nothing appends, because what comes after the seal is")
    L.append("  the next audit;")
    L.append("- the GATE: sealed tree an ancestor of the candidate's")
    L.append("  HEAD, anchors still pinned, fixes still landed — the")
    L.append("  release is cut from the audited tree or its")
    L.append("  descendants, never around the audit.")
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
    L.append("  delivery of the sealed report; expenses within the")
    L.append("  window borne by the engaging party.")
    L.append("- The deliverable schedule: findings filed within the")
    L.append("  window; the project triages within 14 days of each")
    L.append("  finding; remediation commits recorded at triage time;")
    L.append("  the signoff and the gate follow the closure criteria")
    L.append("  above.")
    L.append("- The window is recorded at signature time as two ISO")
    L.append("  dates; a window that ends before it starts is refused")
    L.append("  at the signature, not discovered at the invoice.")
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
    L.append("`python3 tools/%s.py brief --out FINALBRIEF.md` — two" % TOOL)
    L.append("renders of one tree are byte-identical; `--stamp` adds a")
    L.append("Generated line and nothing else. The three digests above")
    L.append("pin the artifact this brief was scoped from.")
    L.append("")
    return "\n".join(L) + "\n"


def render_report(path, entries, head, log_sha256, stamp=None):
    state = derive_state(entries)
    engagement = engagement_of(entries)
    passes = passes_of(entries)
    findings = findings_of(entries)
    triages = triages_of(entries)
    signoff = signoff_of(entries)
    L = []
    L.append("# Halis FINAL security audit — engagement report")
    L.append("")
    L.append("Regenerated from the chained ledger by `%s`." % TOOL)
    L.append("Deterministic per ledger: two renders of one ledger are")
    L.append("byte-identical. Do not hand-edit; regenerate with")
    L.append("`python3 tools/%s.py report --out FINALREPORT.md`." % TOOL)
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
        L.append("- Anchors pinned at the signature:")
        for n in ANCHOR_NAMES:
            L.append("  - %s: `sha256:%s`"
                     % (n, engagement["anchors"][n]))
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
        L.append("| # | by | role | independence | anchors | outcome |"
                 " guarantees |")
        L.append("|---|----|------|--------------|---------|---------|"
                 "------------|")
        for e in passes:
            label = "INDEPENDENT" if e["independent"] else "SELF-ASSESSED"
            amark = ("drift: %s" % "/".join(e["anchor_drift"])
                     if e.get("anchor_drift") else "pinned")
            L.append("| %d | %s | %s | %s | %s | %s | %d/%d |"
                     % (e["seq"], e["by"], e["role"], label,
                        amark, e["outcome"],
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
            L.append("ledger names, not a fact it manufactures. The")
            L.append("signoff will refuse to close on this evidence")
            L.append("alone.")
        if newest.get("anchor_drift"):
            L.append("")
            L.append("**The latest pass ran DRIFTED anchors** — the")
            L.append("engagement was signed against one artifact and")
            L.append("this pass ran another (%s); the drift is a fact"
                     % ", ".join(newest["anchor_drift"]))
            L.append("on record, and the signoff refuses to close")
            L.append("until the evidence re-runs on the artifact that")
            L.append("was engaged.")
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
        L.append("commit itself — landed, and not older than the")
        L.append("finding it fixes.")
    else:
        L.append("| finding | verdict | by | reason | remediation |")
        L.append("|---------|---------|----|--------|-------------|")
        for fid in sorted(triages):
            t = triages[fid]
            L.append("| %s | %s | %s | %s | %s |"
                     % (fid, t["verdict"], t["by"], t["reason"],
                        t.get("fix", "—")))
    L.append("")
    L.append("## The signoff")
    L.append("")
    if signoff is None:
        L.append("No signoff on record. The closure laws that must")
        L.append("hold before the seal is written:")
        L.append("")
        L.append("- an independent evidence pass on record, and the")
        L.append("  latest pass green;")
        L.append("- every finding triaged, every acceptance carrying")
        L.append("  its remediation commit — landed, and not older")
        L.append("  than its finding;")
        L.append("- the three anchors unchanged since the signature.")
        L.append("")
    else:
        L.append("**SEALED** by %s at %s, from the tree `%s`."
                 % (signoff["by"], _fmt_ts(signoff), signoff["tree"]))
        L.append("")
        L.append("The closure census at seal time: %d pass(es), %d "
                 "independent; %d finding(s), %d triaged, %d fix(es) "
                 "recorded."
                 % (signoff["passes"], signoff["independent_passes"],
                    signoff["findings"], signoff["triages"],
                    signoff["fixes"]))
        L.append("")
        L.append("The anchors pinned at the seal:")
        L.append("")
        for n in ANCHOR_NAMES:
            L.append("- %s: `sha256:%s`" % (n, signoff["anchors"][n]))
        L.append("")
        L.append("The ledger is closed — after the seal nothing")
        L.append("appends. What comes after the seal is the next")
        L.append("audit, not an amendment.")
    L.append("")
    L.append("## The state, honestly")
    L.append("")
    L.append("**%s.** %s" % (state.upper(), STATE_MEANING[state]))
    L.append("The word is derived from the census on every render,")
    L.append("never stored: responded requires every finding triaged;")
    L.append("remediated requires every acceptance to carry its")
    L.append("remediation commit; sealed requires the signoff — and")
    L.append("the signoff itself required the independent pass, the")
    L.append("landed fixes and the unchanged anchors before it could")
    L.append("be written. A finding-free audit stays underway — the")
    L.append("ledger cannot know the auditors rested; the report is")
    L.append("the deliverable either way.")
    L.append("")
    L.append("## The render contract")
    L.append("")
    L.append("No wall-clock unless `--stamp` (and then only the")
    L.append("Generated line). The ledger digest above is the sha256")
    L.append("of the raw ledger bytes: tamper with one byte of it and")
    L.append("the chain names the line — a tampered ledger is")
    L.append("evidence, not a document, and nothing renders from it.")
    L.append("This report is a render of the LEDGER; the live")
    L.append("verification is the gate's business, not the")
    L.append("document's.")
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
            "no engagement on record — engage first: the final audit "
            "kit writes nothing before the contract does")
    return eng


def _refuse_if_sealed(entries):
    seal = signoff_of(entries)
    if seal is not None:
        raise LedgerError(
            "the final audit is sealed (seq %d, by %s, tree %s) — "
            "what comes after the seal is the next audit, not an "
            "amendment" % (seal["seq"], seal["by"],
                           seal["tree"][:12]))
    return seal


def _roster_refuse(by, roster, side):
    raise LedgerError(
        "%r is not on the auditor roster (%s) — %s. The roster is "
        "recorded at engage time and the ledger does not negotiate "
        "it afterwards" % (by, ", ".join(roster) or "empty", side))


# ---------------------------------------------------------------------------
# engage — the contract. One engagement per ledger, ever; the
# signature pins the FROZEN ARTIFACT, all three anchors at once.
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
    anchors = anchors_of(root)
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
        "anchors": {n: anchors[n]["digest"] for n in ANCHOR_NAMES},
    })
    print("engaged: %s — %s" % (args.firm, args.window))
    print("  roster   : %s" % ", ".join(roster))
    print("  basis    : %r" % args.basis)
    print("  signed by: %s (the project side)" % by)
    print("  scope    : %d guarantees, sha256:%s"
          % (len(mod.LEDGER),
             anchors["guarantee-scope"]["digest"][:16]))
    for n in ("api-freeze", "bootstrap-snapshot"):
        print("  anchor   : %s = %s (%s)"
              % (n, anchors[n]["digest"][:16], anchors[n]["detail"]))
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
    anchors = anchors_of(root)
    engagement = engagement_of(entries)
    doc = render_brief(root, mod, anchors, engagement,
                       stamp=_stamp_or_none(args))
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
# checker (the hls-guarantee engine, imported), re-derives the three
# anchors live, records what it ran and any drift as a fact. The seal
# is refused later on drift that was never re-pinned; the pass itself
# is honest about both outcomes — fail-closed AND fail-recorded.
# ---------------------------------------------------------------------------
def cmd_evidence(args):
    root = _tree_of(args)
    path = _ledger_of(args)
    entries, _, _ = read_ledger(path, must_exist=True)
    eng = _require_engagement(entries)
    _refuse_if_sealed(entries)
    by = args.by or default_actor(root)
    mod = load_scope(root)
    anchors = anchors_of(root)
    okc, results = mod.verify(root, quiet=True)
    n_ok = sum(1 for r in results["guarantees"]
               if r["status"] == "verified")
    n_anchors = sum(1 for r in results["guarantees"]
                    for a in r["anchors"] if a["ok"])
    n_probes = sum(1 for r in results["guarantees"]
                   for p in r["probes"] if p["verdict"] == "ok")
    refusals = list(results["refusals"])
    drift = anchor_drift(eng["anchors"],
                         {n: anchors[n]["digest"]
                          for n in ANCHOR_NAMES})
    body = {
        "kind": "evidence",
        "tool": TOOL,
        "version": VERSION,
        "by": by,
        "role": "auditor" if by in eng["roster"] else "project",
        "independent": by in eng["roster"],
        "tree": git_sha(root),
        "anchors": {n: anchors[n]["digest"] for n in ANCHOR_NAMES},
        "anchor_drift": drift,
        "outcome": "ok" if okc else "failed",
        "verified_guarantees": n_ok,
        "total_guarantees": len(mod.LEDGER),
        "guarantee_anchors": n_anchors,
        "guarantee_probes": n_probes,
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
        print("  anchors  : %s" % ("drift: " + ", ".join(drift)
                                   if drift else "pinned"))
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
    _refuse_if_sealed(entries)
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
# triage — the project's response. The fix-landing law lives here:
# exist, postdate the finding, land in the tree being released.
# ---------------------------------------------------------------------------
def cmd_triage(args):
    root = _tree_of(args)
    path = _ledger_of(args)
    entries, _, _ = read_ledger(path, must_exist=True)
    eng = _require_engagement(entries)
    _refuse_if_sealed(entries)
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
        else:
            landed = commit_landed(root, args.fix)
            if landed is False:
                head = git_sha(root)
                raise LedgerError(
                    "the fix %s is not in the tree being released "
                    "(not an ancestor of HEAD %s) — a fix that has "
                    "not landed is a promise, not a fix"
                    % (args.fix[:12], head[:12]))
            when = commit_timestamp(root, args.fix)
            filed = findings[fid]["timestamp"]
            if when is not None and when < filed:
                raise LedgerError(
                    "the fix %s predates the finding it fixes "
                    "(committed %d, filed %d) — a fix that predates "
                    "its finding is a contradiction"
                    % (args.fix[:12], when, filed))
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
# signoff — the project's closing act. Writes the seal, and closes
# the ledger: after the seal nothing appends.
# ---------------------------------------------------------------------------
def cmd_signoff(args):
    root = _tree_of(args)
    path = _ledger_of(args)
    entries, _, _ = read_ledger(path, must_exist=True)
    eng = _require_engagement(entries)
    _refuse_if_sealed(entries)
    by = args.by or default_actor(root)
    if by in eng["roster"]:
        raise LedgerError(
            "%r is on the auditor roster — the signoff is the "
            "project's act; the auditors' deliverable is the "
            "findings, not the closure" % by)
    refusals = closure_refusals(entries, root)
    if refusals:
        sys.stderr.write(
            "SIGNOFF REFUSED — the closure laws that did not hold:\n")
        for r in refusals:
            sys.stderr.write("  - %s\n" % r)
        return 1
    anchors = anchors_of(root)
    passes = passes_of(entries)
    findings = findings_of(entries)
    triages = triages_of(entries)
    entry = append_chained(path, {
        "kind": "signoff",
        "tool": TOOL,
        "version": VERSION,
        "by": by,
        "tree": git_sha(root),
        "anchors": {n: anchors[n]["digest"] for n in ANCHOR_NAMES},
        "passes": len(passes),
        "independent_passes": sum(1 for p in passes
                                  if p["independent"]),
        "findings": len(findings),
        "triages": len(triages),
        "fixes": sum(1 for t in triages.values() if "fix" in t),
    })
    print("sealed: seq %d by %s — the final audit is closed"
          % (entry["seq"], by))
    print("  tree    : %s" % entry["tree"][:24])
    for n in ANCHOR_NAMES:
        print("  anchor  : %s = %s" % (n, entry["anchors"][n][:16]))
    print("  census  : %d pass(es), %d independent; %d finding(s), "
          "%d triaged, %d fix(es)"
          % (entry["passes"], entry["independent_passes"],
             entry["findings"], entry["triages"], entry["fixes"]))
    return 0


# ---------------------------------------------------------------------------
# gate — the RC gate. What the release process runs before cutting a
# candidate; exit 0 only when the final audit holds on THIS tree.
# ---------------------------------------------------------------------------
def cmd_gate(args):
    root = _tree_of(args)
    path = _ledger_of(args)

    def refuse(msg):
        if getattr(args, "json", False):
            print(json.dumps({
                "tool": TOOL, "version": VERSION, "policy": POLICY,
                "gate": "red", "refusal": msg}, sort_keys=True))
        else:
            sys.stderr.write("GATE RED — %s\n" % msg)
        return 1

    if not os.path.isfile(path):
        return refuse("no final audit on record (%s) — a release "
                      "candidate is not cut from an unaudited tree"
                      % (os.path.relpath(path, _ROOT)
                         if path.startswith(_ROOT) else path))
    entries, _, _ = read_ledger(path, must_exist=True)
    seal = signoff_of(entries)
    state = derive_state(entries)
    if seal is None:
        return refuse(
            "the final audit is %s, not sealed — the gate opens "
            "only on a sealed audit; the closure laws that did not "
            "hold:\n%s" % (state, "\n".join(
                "  - %s" % r for r in closure_refusals(entries, root))))
    if not is_git_tree(root):
        return refuse("the tree is not a git repository — the gate "
                      "cannot verify that the candidate is cut from "
                      "the audited tree")
    head = git_sha(root)
    if commit_landed(root, seal["tree"]) is not True:
        return refuse(
            "the sealed tree %s is not an ancestor of the current "
            "HEAD %s — the candidate is cut around the audit"
            % (seal["tree"][:24], head[:24]))
    try:
        live = anchors_of(root)
    except AnchorError as ex:
        return refuse("the live anchors cannot be derived (%s) — "
                      "the sealed artifact cannot be confirmed" % ex)
    moved = anchor_drift(seal["anchors"],
                         {n: live[n]["digest"] for n in ANCHOR_NAMES})
    if moved:
        return refuse(
            "the %s anchor moved after the seal (sealed %s..., "
            "live %s...) — the candidate is not the artifact that "
            "was audited" % (moved[0], seal["anchors"][moved[0]][:24],
                             live[moved[0]]["digest"][:24]))
    eng = engagement_of(entries)
    for fid in sorted(triages_of(entries)):
        t = triages_of(entries)[fid]
        if t["verdict"] == "accepted" \
                and commit_landed(root, t["fix"]) is not True:
            return refuse(
                "the remediation for %s no longer lands in this "
                "tree — a fix that is not in the tree being "
                "released is a promise, not a fix" % fid)
    if getattr(args, "json", False):
        print(json.dumps({
            "tool": TOOL, "version": VERSION, "policy": POLICY,
            "gate": "green", "state": state,
            "sealed_tree": seal["tree"], "head": head,
            "anchors": {n: live[n]["digest"] for n in ANCHOR_NAMES},
            "findings": seal["findings"], "fixes": seal["fixes"],
        }, sort_keys=True))
    else:
        print("GATE GREEN — the final audit holds; the candidate "
              "may be cut")
        print("  sealed tree : %s (an ancestor of HEAD %s)"
              % (seal["tree"][:24], head[:12]))
        print("  engagement  : %s, %d finding(s), %d fix(es) landed"
              % (eng["firm"], seal["findings"], seal["fixes"]))
        for n in ANCHOR_NAMES:
            print("  anchor      : %s = %s (%s)"
                  % (n, live[n]["digest"][:16], live[n]["detail"]))
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
    seal = signoff_of(entries)
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
                "by": eng["by"], "anchors": eng["anchors"]},
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
            "signoff": None if seal is None else {
                "by": seal["by"], "tree": seal["tree"],
                "anchors": seal["anchors"]},
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
        for n in ANCHOR_NAMES:
            print("              anchor %s: %s"
                  % (n, eng["anchors"][n][:16]))
    else:
        print("  engagement: none on record")
    if passes:
        latest = passes[-1]
        print("  evidence  : %d pass(es) — %d independent, %d "
              "self-assessed; latest outcome %s%s"
              % (len(passes), n_ind, len(passes) - n_ind,
                 latest["outcome"],
                 ", drifted anchors" if latest.get("anchor_drift")
                 else ""))
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
    if seal is not None:
        print("  signoff   : sealed by %s from tree %s"
              % (seal["by"], seal["tree"][:24]))
    else:
        print("  signoff   : not sealed — the gate is red")
    return 0


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog=TOOL,
        description="the Stage 144 final audit: the independent "
                    "third-party security audit that gates the v1.0 "
                    "release candidates")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ledger", default=None, help="the "
                        "final-audit ledger (default: "
                        ".hlfa/final.jsonl under the repo the tool "
                        "lives in — never under --root: one project, "
                        "one final audit)")
    common.add_argument("--root", default=None,
                        help="the tree to audit (default: the repo "
                             "the tool lives in)")
    sub = ap.add_subparsers(dest="cmd")

    p_eng = sub.add_parser("engage", parents=[common], help="sign "
                           "the contract: the firm, the roster, the "
                           "window, the basis — and the three "
                           "anchors, pinned live")
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
                           "the RFP from the live tree and its three "
                           "anchors")
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
                           "reason, the fix landed")
    p_tri.add_argument("finding", help="AF-NNN")
    p_tri.add_argument("--verdict", required=True)
    p_tri.add_argument("--reason", required=True)
    p_tri.add_argument("--fix", default=None,
                       help="the remediation commit (40 hex); "
                            "required for accepted")
    p_tri.add_argument("--by", default=None)
    p_tri.set_defaults(fn=cmd_triage)

    p_sig = sub.add_parser("signoff", parents=[common], help="the "
                           "project's closing act: write the seal "
                           "and close the ledger")
    p_sig.add_argument("--by", default=None,
                       help="who signs the closure (default: the "
                            "tree's git identity)")
    p_sig.set_defaults(fn=cmd_signoff)

    p_gte = sub.add_parser("gate", parents=[common], help="the RC "
                           "gate: green only when the sealed audit "
                           "holds on this tree")
    p_gte.add_argument("--json", action="store_true")
    p_gte.set_defaults(fn=cmd_gate)

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
    except (LedgerError, AnchorError) as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
