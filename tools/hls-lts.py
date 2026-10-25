#!/usr/bin/env python3
"""hls-lts — the Stage 132 LTS branch policy (`backport-only after v1.0`)
and the Stage 145 support windows (`3 years bug-fix, 5 years security`).

Usage:
  python3 tools/hls-lts.py init --tag v1.0.0 [--branch release/1.0] [--repo PATH]
  python3 tools/hls-lts.py status  [--repo PATH] [--json]
  python3 tools/hls-lts.py classify <commit> [--repo PATH] [--json]
  python3 tools/hls-lts.py backport <commit> [--branch release/1.0]
                                     [--repo PATH] [--dry-run] [--at DATE]
  python3 tools/hls-lts.py tag-patch [--tag vX.Y.Z] [--branch release/1.0]
                                     [--repo PATH] [--dry-run] [--at DATE]
  python3 tools/hls-lts.py audit    [--branch release/1.0] [--repo PATH] [--json]
  python3 tools/hls-lts.py window   [--tag vX.Y.Z] [--at DATE]
                                     [--repo PATH] [--json]
  python3 tools/hls-lts.py policy   [--out PATH] [--repo PATH] [--check]

The roadmap's phrase is the scope: "LTS branch policy (backport-only
after v1.0)". Stage 132 turns that phrase into an executable policy.
After v1.0 the project maintains LTS release branches; an LTS branch
receives exactly two kinds of commits:

  1. a BACKPORT — a cherry-pick of a bug-fix commit from the main
     line, recorded with the provenance trailer `Backported-from:
     <sha>` (git's `-x` "(cherry picked from commit ...)" is kept
     too, but the trailer is the machine-readable contract);
  2. a CHORE — release bookkeeping (a version bump, a tag, a
     changelog edit) that touches no language surface.

Everything else is refused. A feature, a spec change, a stdlib
addition, a perf rewrite, an unprefixed commit — none of these may
land on an LTS branch, no matter who asks. The classifier is
fail-closed: a commit it cannot name is a refusal, not a pass.

Commands:
  init       declare the LTS branch: the base tag must exist, parse
             as semver and be >= 1.0.0 (the policy begins AT v1.0 —
             a pre-1.0 LTS is a contradiction), and writes
             .hls-lts.json (branch, base tag, base sha, policy
             version). Re-init is idempotent; a different tag or
             branch is a refusal.
  status     print the declaration, the tag chain and the audit state.
  classify   name a commit's class: BUGFIX / FEATURE / CHORE /
             UNKNOWN, with the reasons. Two gates: the message
             prefix table, then the diff gate (a `fix:` that adds
             public API surface — new pub fns in std/, new std/
             modules, SPEC.md edits — is a feature in disguise and
             is re-classed FEATURE).
  backport   cherry-pick a commit onto the LTS branch under the
             policy: it must classify BUGFIX; the pick records the
             Backported-from trailer; --dry-run predicts without
             touching anything. Any refusal leaves the branch
             byte-identical (checked out back, pick aborted).
  tag-patch  cut the next LTS patch tag: same major.minor as the
             base, patch exactly +1 over the last tag on the branch,
             no pre-release/build suffix (an LTS release is a
             release). An explicit --tag that breaks the law is
             refused by name.
  audit      walk every commit on the branch since the base and every
             tag on it; each commit must be a valid backport (the
             trailer names an existing upstream commit whose class
             is BUGFIX) or a chore; each tag must obey the version
             law. Exit 1 names every violation.

Stage 145 publishes the second dimension of the same policy — the
SUPPORT WINDOWS. The roadmap's phrase is the promise: "3 years
bug-fix, 5 years security". A published promise a tool cannot read
is prose; these commands make it arithmetic:

  window     print the line's support window: the day the base
             release was cut (the commit's committer day), the day
             bug fixes stop (base + 3 years), the day everything
             stops (base + 5 years), and the phase at a given day —
             `active`, `security` or `eol`. `--at` pins the clock
             (default: today); `--tag` judges any release tag
             without a declaration.
  policy     render the published policy document (LTS.md) from the
             tool's own constants — deterministic, so two renders
             are byte-identical and a hand-edited document is
             visible as a diff. `--check` re-derives the committed
             copy and refuses drift; `--out` regenerates it.

The window laws, enforced where a change would enter the branch:

  - the MARKING law: a bug fix is a SECURITY fix when its message
    carries a `Security: <reason>` trailer (one line, non-empty
    value — the first such line decides; fail-closed: no trailer,
    no security claim). The class gates are unchanged — a marked
    FEATURE is still a FEATURE, and still refused.
  - the BACKPORT law: in the `active` phase any BUGFIX may land; in
    the `security` phase only marked fixes may; in `eol` nothing
    lands — a dead line takes nothing, not even chores.
  - the RELEASE law: no tag is cut past end of life, and a release
    cut in the security phase carries at least one marked backport
    (a chore-only release is how a security line pretends to be
    alive).
  - the AUDIT law: history is judged the day it landed — a backport
    that arrived in the security phase without the mark, or any
    commit or tag past end of life, is a violation named sha by sha.

The calendar speaks in anniversaries: base + N years is the same
month and day, with February 29 clamped to the 28th when the target
year is not a leap year, and the horizon day itself belongs to the
phase it closes (support runs THROUGH the anniversary, not up to
it). The declaration gains no fields and the policy version stays
`halis-lts-1`: the window law is the same policy grown its second
dimension, derived live from the base tag every time.

Exit codes: 0 ok, 1 policy refusal (the rule is named on stdout/stderr),
2 usage error. Every refusal names the RULE, not just the fact.

The version laws reuse the Stage 131 semver module
(tools/hpkg_parts/hpkg_semver.py) — one grammar for the whole
project, from the package manifest to the release train.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

_TOOLS = os.path.dirname(os.path.abspath(__file__))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
from hpkg_parts import hpkg_semver as semver  # noqa: E402

POLICY_VERSION = "halis-lts-1"
POLICY_FILE = ".hls-lts.json"
DEFAULT_BRANCH = "release/1.0"
DEFAULT_UPSTREAM = "main"

TRAILER = "Backported-from"
CHERRY_MARK = "(cherry picked from commit "

# The Stage 145 support windows, in years. BUGFIX_YEARS is the full
# window (any qualifying fix lands); SECURITY_YEARS is the total
# horizon — the last two years of a five-year line are security-only.
# These constants are the single source of truth: `policy` renders
# the published document from them, and `make lts-policy-verify`
# re-derives the committed copy against the same render.
BUGFIX_YEARS = 3
SECURITY_YEARS = 5

# The security marking: a `Security: <reason>` line in the commit
# message (name matched case-insensitively, like Backported-from).
SECURITY_TRAILER = "Security"

# The published document (rendered by `policy`, committed at the
# repository root, verified by `policy --check`).
POLICY_DOC = "LTS.md"

PHASE_ACTIVE = "active"
PHASE_SECURITY = "security"
PHASE_EOL = "eol"

PHASE_MEANING = {
    PHASE_ACTIVE: "bug-fix and security backports land",
    PHASE_SECURITY: "security backports only — plain fixes are refused",
    PHASE_EOL: "end of life — nothing lands on a dead line",
}

# The --at grammar: a plain ISO day, YYYY-MM-DD. The policy speaks in
# days; the clock it reads can be pinned, so the gates and CI never
# depend on when they run.
DAY_RE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")

# ---------------------------------------------------------------------------
# The message law. The prefix table is the project's own commit
# vocabulary (see git log): the feature commit of a stage names the
# tool or the area it lands in, the stage release says "Stage N",
# the gate says "stage N acceptance gate". The LTS policy keeps the
# vocabulary and gives it consequences.
# ---------------------------------------------------------------------------

BUGFIX_PREFIXES = {
    "fix": "the fix prefix names a defect repair",
    "bugfix": "explicit bugfix prefix",
    "revert": "a revert removes a defect the safest way",
    "hotfix": "a hotfix is a fix under time pressure, still a fix",
    "regression": "a regression repair restores shipped behaviour",
}

FEATURE_PREFIXES = {
    "feat": "a feature is exactly what an LTS never takes",
    "feature": "a feature is exactly what an LTS never takes",
    "add": "new functionality is a feature, however small",
    "stage": "a roadmap stage is new functionality by definition",
    "spec": "a spec change is a language change",
    "stdlib": "new stdlib surface is a feature",
    "perf": "a perf rewrite can change semantics; only provable "
            "repairs qualify for an LTS",
    "opt": "an optimisation is not a repair",
    "refactor": "a refactor is risk without a user-visible repair",
}

CHORE_PREFIXES = {
    "chore": "release bookkeeping changes no language surface",
    "release": "a release commit is the bookkeeping itself",
    "docs": "documentation changes no language surface",
    "doc": "documentation changes no language surface",
    "test": "test-only changes are safe on an LTS",
    "tests": "test-only changes are safe on an LTS",
    "ci": "CI bookkeeping changes no language surface",
    "meta": "project meta bookkeeping",
}

PREFIX_CLASSES: Dict[str, str] = {}
for _p in BUGFIX_PREFIXES:
    PREFIX_CLASSES[_p] = "BUGFIX"
for _p in FEATURE_PREFIXES:
    PREFIX_CLASSES[_p] = "FEATURE"
for _p in CHORE_PREFIXES:
    PREFIX_CLASSES[_p] = "CHORE"

# The prefix grammar: "word: rest" or "word! ..."? Keep the project's
# own shape — `word: rest` (a colon, then the summary). A bare `word`
# with no colon is NOT a prefix (fail-closed: "stage 131 acceptance
# gate, ok corpus test" has no colon and no recognized prefix - it is
# UNKNOWN by the message law; the diff gate may still re-class it,
# and the audit treats it as a violation unless carried by a
# Backported-from trailer with a BUGFIX upstream).
PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):\s")

# The diff gate: public-surface detection lives in _new_public_names
# (a diff-aware walk that tracks which file each hunk belongs to). A
# `fix:` that introduces one of these surfaces is a feature in
# disguise — the backport gate refuses it with the reason, not the
# diff.


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> int:
    print("hls-lts: " + msg, file=sys.stderr)
    return code


class Git:
    def __init__(self, repo: str):
        self.repo = repo

    def _base(self) -> List[str]:
        return ["git", "-C", self.repo]

    def run(self, *args: str, check: bool = True,
            input: Optional[str] = None) -> subprocess.CompletedProcess:
        p = subprocess.run(self._base() + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, input=input)
        if check and p.returncode != 0:
            raise RuntimeError("git %s failed: %s"
                               % (" ".join(args), p.stdout.strip()))
        return p

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def rev_parse(self, ref: str) -> Optional[str]:
        p = self.run("rev-parse", "--verify", "--quiet",
                     ref + "^{commit}", check=False)
        if p.returncode != 0:
            return None
        return p.stdout.strip()

    def tag_exists(self, tag: str) -> bool:
        p = self.run("rev-parse", "-q", "--verify", "refs/tags/" + tag,
                     check=False)
        return p.returncode == 0

    def tag_sha(self, tag: str) -> Optional[str]:
        # Peel annotated tags to the commit.
        return self.rev_parse("refs/tags/" + tag)

    def tags_on(self, branch: str) -> List[str]:
        p = self.run("tag", "--merged", branch, "--sort=version:refname",
                     check=False)
        if p.returncode != 0:
            return []
        return [t for t in p.stdout.splitlines() if t]

    def commits(self, base: str, head: str) -> List[str]:
        p = self.run("rev-list", "--reverse", base + ".." + head)
        return [c for c in p.stdout.splitlines() if c]

    def branch_sha(self, branch: str) -> Optional[str]:
        return self.rev_parse(branch)

    def has_branch(self, branch: str) -> bool:
        p = self.run("show-ref", "--verify", "--quiet",
                     "refs/heads/" + branch, check=False)
        return p.returncode == 0

    def current_branch(self) -> str:
        p = self.run("rev-parse", "--abbrev-ref", "HEAD")
        return p.stdout.strip()

    def is_dirty(self) -> bool:
        # Tracked-file modifications only. Untracked files (the policy
        # file itself among them) do not block a cherry-pick, and a
        # policy tool that counts its own declaration as dirt would
        # refuse every backport it exists to enable.
        p = self.run("status", "--porcelain", "-uno")
        return p.stdout.strip() != ""

    def message(self, sha: str) -> str:
        return self.out("log", "-1", "--format=%B", sha)

    def subject(self, sha: str) -> str:
        return self.out("log", "-1", "--format=%s", sha)

    def commit_time(self, sha: str) -> str:
        return self.out("log", "-1", "--format=%cI", sha)

    def diff(self, sha: str) -> str:
        return self.out("show", "--format=", "--unified=0", sha)

    def diff_added_files(self, sha: str) -> List[str]:
        return self.out("show", "--format=", "--name-status",
                        "--diff-filter=A", sha).splitlines()

    def upstream_of(self, sha: str) -> Optional[str]:
        for ref in (DEFAULT_UPSTREAM, "origin/" + DEFAULT_UPSTREAM):
            if self.rev_parse(ref) is None:
                continue
            p = self.run("merge-base", "--is-ancestor", sha, ref,
                         check=False)
            if p.returncode == 0:
                return ref
        return None


# ---------------------------------------------------------------------------
# The classifier. Two gates, in order; the first to re-class wins.
# ---------------------------------------------------------------------------

def _trailer_sha(message: str) -> Optional[str]:
    for line in message.splitlines():
        s = line.strip()
        if s.lower().startswith(TRAILER.lower() + ":"):
            val = s.split(":", 1)[1].strip()
            if re.fullmatch(r"[0-9a-f]{40}", val):
                return val
    return None


def _new_public_names(diff: str) -> List[str]:
    """New top-level fn names the diff introduces in std/ or core/
    Halis modules. Halis has no access modifier — every top-level fn
    in a module is importable, so a new fn IS new API surface. A name
    rewritten in place (the -/+ pair of an edited signature) is not
    new. Python `def`s in tools/ are implementation detail, not API,
    and never trip this gate."""
    names: List[str] = []
    current = ""
    removed: set = set()
    added: List[Tuple[str, str]] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = ""
            removed = set()
            continue
        if line.startswith("--- ") or line.startswith("index "):
            continue
        if line.startswith("+++ "):
            current = line[4:].strip()
            if current.startswith("b/"):
                current = current[2:]
            removed = set()
            continue
        if line.startswith("-"):
            r = re.match(r"^-\s*(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)",
                         line)
            if r:
                removed.add(r.group(1))
        elif line.startswith("+"):
            r = re.match(r"^\+\s*(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)",
                         line)
            if r:
                added.append((current, r.group(1)))
    for path, name in added:
        if not path.endswith(".hls"):
            continue
        if not (path.startswith("std/") or path.startswith("core/")):
            continue
        if name in removed:
            continue
        if name not in names:
            names.append(name)
    return names


def _diff_gate(git: Git, sha: str, cls: str) -> Tuple[str, List[str]]:
    """The second gate. Returns (class, reasons). A BUGFIX whose diff
    introduces public API surface is a FEATURE in disguise."""
    reasons: List[str] = []
    if cls != "BUGFIX":
        return cls, reasons
    diff = git.diff(sha)
    added_files = git.diff_added_files(sha)
    for line in added_files:
        if line.startswith("A\t") and (
                line[2:].startswith("std/")
                or line[2:].startswith("core/")):
            reasons.append("new std module file '%s' — new API surface "
                           "is a feature" % line[2:])
    names = _new_public_names(diff)
    if names:
        reasons.append("new public fn(s) %s — new API surface is a "
                       "feature" % ", ".join(names[:4]))
    for line in diff.splitlines():
        if line.startswith("+++") and "SPEC.md" in line:
            reasons.append("SPEC.md edits are language changes")
            break
    if reasons:
        return "FEATURE", reasons
    return cls, reasons


def classify_commit(git: Git, sha: str) -> Dict:
    """Classify one commit. The message law first; the diff gate may
    re-class a BUGFIX into a FEATURE. The default is UNKNOWN — the
    fail-closed law. The Stage 145 marking rides along as a fact,
    not a class: `security` is true only when a `Security:` trailer
    stands in the message, whatever the class — a marked FEATURE is
    still a FEATURE, and still refused."""
    msg = git.message(sha)
    subject = msg.splitlines()[0] if msg.splitlines() else ""
    m = PREFIX_RE.match(subject)
    cls = "UNKNOWN"
    reason = "no recognized prefix — the classifier is fail-closed"
    if m:
        p = m.group(1).lower()
        if p in PREFIX_CLASSES:
            cls = PREFIX_CLASSES[p]
            table = {**BUGFIX_PREFIXES, **FEATURE_PREFIXES, **CHORE_PREFIXES}
            reason = table[p]
        else:
            cls = "UNKNOWN"
            reason = "prefix '%s' is not in the policy table" % p
    cls, extra = _diff_gate(git, sha, cls)
    reasons = ([reason] if not extra else [reason] + extra) \
        if cls != "UNKNOWN" else [reason]
    mark = _security_mark(msg)
    return {
        "sha": git.rev_parse(sha) or sha,
        "subject": subject,
        "class": cls,
        "reason": reasons[0] if reasons else reason,
        "reasons": reasons,
        "security": mark is not None,
        "security_reason": mark,
    }


def is_backport_commit(git: Git, sha: str) -> bool:
    return _trailer_sha(git.message(sha)) is not None \
        or CHERRY_MARK in git.message(sha)


# ---------------------------------------------------------------------------
# The Stage 145 window law — the calendar, the phases, the marking.
# ---------------------------------------------------------------------------

def _security_mark(message: str) -> Optional[str]:
    """The marking law. A bug fix is a security fix when its message
    carries a `Security: <reason>` line — the name matched case-
    insensitively (git trailers are case-insensitive by convention),
    the value stripped; the FIRST such line decides, and an empty
    value is not a marking (fail-closed: no trailer, no claim)."""
    for line in message.splitlines():
        s = line.strip()
        if s.lower().startswith(SECURITY_TRAILER.lower() + ":"):
            val = s.split(":", 1)[1].strip()
            return val if val else None
    return None


def _commit_day(iso: str) -> Optional[datetime.date]:
    """The day a commit landed: the committer timestamp's date part
    (git's %cI — the clock the project's landing laws already read)."""
    if not iso:
        return None
    m = DAY_RE.match(iso.strip()[:10])
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)),
                             int(m.group(3)))
    except ValueError:
        return None


def _parse_day(text: str) -> Optional[datetime.date]:
    m = DAY_RE.match(text.strip() if text else "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)),
                              int(m.group(3)))
    except ValueError:
        return None


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31


def _add_years(day: datetime.date, years: int) -> datetime.date:
    """The anniversary: same month and day, with February 29 clamped
    to the 28th when the target year is not a leap year. A promise of
    `3 years` speaks in anniversaries, not in 1095 fixed days."""
    y = day.year + years
    d = day.day
    if day.month == 2 and d == 29 and _days_in_month(y, 2) < d:
        d = 28
    return datetime.date(y, day.month, d)


def _window(base: datetime.date) -> Dict:
    """The line's two horizons, from the day the base release was cut."""
    return {
        "bug_fix_until": _add_years(base, BUGFIX_YEARS),
        "security_until": _add_years(base, SECURITY_YEARS),
    }


def _phase_at(base: datetime.date, at: datetime.date) -> str:
    """The phase on a given day. The horizon day itself belongs to the
    phase it closes: support runs THROUGH the anniversary, so the
    third birthday is the last full-support day and the day after it
    is security-only; the fifth birthday is the last supported day
    and the day after it is end of life."""
    w = _window(base)
    if at <= w["bug_fix_until"]:
        return PHASE_ACTIVE
    if at <= w["security_until"]:
        return PHASE_SECURITY
    return PHASE_EOL


def _line_base_day(git: Git, pol: Dict) -> Optional[datetime.date]:
    """The day the declared line was born: the committer day of the
    base tag's commit. Support is measured from the BASE release
    (vMAJOR.MINOR.0's day), not from each patch — the window belongs
    to the line, and every patch on it lives inside the same one."""
    sha = pol.get("base_sha")
    if not sha:
        return None
    return _commit_day(git.commit_time(sha))


def _line_base_day_of_tag(git: Git, tag: str) -> Optional[datetime.date]:
    sha = git.tag_sha(tag)
    if not sha:
        return None
    return _commit_day(git.commit_time(sha))


def _window_payload(base_day: datetime.date, at: datetime.date) -> Dict:
    w = _window(base_day)
    phase = _phase_at(base_day, at)
    return {
        "base_date": base_day.isoformat(),
        "bug_fix_until": w["bug_fix_until"].isoformat(),
        "security_until": w["security_until"].isoformat(),
        "at": at.isoformat(),
        "phase": phase,
        "days_bugfix_left": (w["bug_fix_until"] - at).days,
        "days_security_left": (w["security_until"] - at).days,
    }


def _resolve_at(args) -> Optional[datetime.date]:
    """--at pins the clock the policy reads. Default: today."""
    if not getattr(args, "at", None):
        return datetime.date.today()
    return _parse_day(args.at)


# ---------------------------------------------------------------------------
# The policy file
# ---------------------------------------------------------------------------

def load_policy(git: Git) -> Optional[Dict]:
    path = os.path.join(git.repo, POLICY_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("policy") != POLICY_VERSION:
        raise RuntimeError("policy file %s has unknown policy version %r "
                           "(expected %s)"
                           % (POLICY_FILE, data.get("policy"),
                              POLICY_VERSION))
    return data


def save_policy(git: Git, data: Dict) -> None:
    path = os.path.join(git.repo, POLICY_FILE)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _repo_arg(args) -> str:
    repo = args.repo
    if repo is None:
        repo = os.path.dirname(_TOOLS)
    return os.path.abspath(repo)


def _require_repo(repo: str) -> str:
    p = subprocess.run(["git", "-C", repo, "rev-parse", "--git-dir"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True)
    if p.returncode != 0:
        raise SystemExit(die("'%s' is not a git repository" % repo, 1))
    return repo


def cmd_init(args) -> int:
    repo = _require_repo(_repo_arg(args))
    git = Git(repo)
    tag = args.tag
    branch = args.branch or DEFAULT_BRANCH

    # An existing declaration speaks FIRST: a re-init with a
    # different base is a policy decision, refused as such — even
    # when the new base tag does not exist at all.
    existing = load_policy(git)
    if existing is not None:
        base_sha_now = git.tag_sha(tag) if git.tag_exists(tag) else None
        if existing.get("base_tag") == tag and existing.get("branch") == branch \
                and base_sha_now == existing.get("base_sha"):
            print("hls-lts: policy already declared (idempotent no-op): "
                  "branch %s from %s" % (branch, tag))
            return 0
        return die("init: a policy is already declared (%s from %s) — "
                   "one declaration per repository; a different base is "
                   "a new policy decision, not a re-init"
                   % (existing.get("branch"), existing.get("base_tag")))

    v = semver.tag_version(tag)
    if v is None:
        return die("init: base tag '%s' is not a valid semantic version "
                   "(semver 2.0.0) — the LTS base must be a release tag"
                   % tag)
    if v.prerelease:
        return die("init: base tag '%s' carries a pre-release — the LTS "
                   "policy begins AT v1.0.0, on a release" % tag)
    if v.major < 1:
        return die("init: base tag '%s' is pre-1.0 — the LTS policy "
                   "begins after v1.0 (backport-only AFTER v1.0)" % tag)
    if not git.tag_exists(tag):
        return die("init: tag '%s' does not exist in the repository" % tag)
    base_sha = git.tag_sha(tag)
    if base_sha is None:
        return die("init: tag '%s' does not name a commit" % tag)

    if not git.has_branch(branch):
        # Declare the branch by creating it at the base tag's commit.
        git.run("branch", branch, base_sha)
        print("hls-lts: created branch %s at %s (%s)"
              % (branch, base_sha[:12], tag))
    save_policy(git, {
        "policy": POLICY_VERSION,
        "branch": branch,
        "base_tag": tag,
        "base_sha": base_sha,
        "created": git.commit_time("HEAD"),
    })
    print("hls-lts: declared LTS branch '%s' from tag %s (%s)"
          % (branch, tag, base_sha[:12]))
    print("hls-lts: policy: backport-only — BUGFIX backports with "
          "'%s: <sha>' trailers and chores only" % TRAILER)
    return 0


def cmd_status(args) -> int:
    repo = _require_repo(_repo_arg(args))
    git = Git(repo)
    pol = load_policy(git)
    if pol is None:
        return die("status: no LTS policy declared — run `hls-lts init "
                   "--tag v1.0.0` after v1.0 exists", 1)
    branch = pol["branch"]
    base_sha = pol["base_sha"]
    commits = git.commits(base_sha, branch)
    tags = [t for t in git.tags_on(branch) if semver.tag_version(t)]
    violations = audit_violations(git, pol)

    # The Stage 145 window, live: where the line stands today.
    base_day = _line_base_day(git, pol)
    win = _window_payload(base_day, datetime.date.today()) \
        if base_day is not None else None

    if args.json:
        data = {
            "policy": pol,
            "commits_since_base": len(commits),
            "tags": tags,
            "violations": violations,
            "window": win,
        }
        print(json.dumps(data, indent=2))
    else:
        print("LTS policy %s — branch %s from %s (%s)"
              % (pol["policy"], branch, pol["base_tag"], base_sha[:12]))
        if win is not None:
            print("window: bug fixes until %s, security until %s — "
                  "phase today: %s (%s)"
                  % (win["bug_fix_until"], win["security_until"],
                     win["phase"], PHASE_MEANING[win["phase"]]))
        else:
            print("window: the base commit's day cannot be read — the "
                  "phase is unknown (fail-closed)")
        print("commits since base: %d" % len(commits))
        print("tags on branch: %s"
              % (", ".join(tags) if tags else "(none yet)"))
        if violations:
            print("audit: %d violation(s)" % len(violations))
            for v in violations:
                print("  - %s" % v)
        else:
            print("audit: clean")
    return 1 if violations else 0


def cmd_classify(args) -> int:
    repo = _require_repo(_repo_arg(args))
    git = Git(repo)
    sha = git.rev_parse(args.commit)
    if sha is None:
        return die("classify: '%s' does not name a commit" % args.commit, 1)
    c = classify_commit(git, sha)
    if args.json:
        print(json.dumps(c, indent=2))
    else:
        print("commit %s" % c["sha"])
        print("  subject: %s" % c["subject"])
        print("  class:   %s" % c["class"])
        if c["security"]:
            print("  security: marked — %s" % c["security_reason"])
        else:
            print("  security: unmarked — no 'Security: <reason>' trailer "
                  "in the message (fail-closed)")
        for r in c["reasons"]:
            print("  - %s" % r)
    return 0


def _checkout_or_restore(git: Git, original: str, had_pick: bool) -> None:
    if had_pick:
        git.run("cherry-pick", "--abort", check=False)
        # A refused pick that left no merge state makes --abort a
        # no-op error; --quit clears any residual CHERRY_PICK_HEAD
        # either way.
        git.run("cherry-pick", "--quit", check=False)
    git.run("checkout", "-q", original, check=False)


def cmd_backport(args) -> int:
    repo = _require_repo(_repo_arg(args))
    git = Git(repo)
    pol = load_policy(git)
    if pol is None:
        return die("backport: no LTS policy declared — run `hls-lts init "
                   "--tag v1.0.0` first", 1)
    branch = args.branch or pol["branch"]
    if branch != pol["branch"]:
        return die("backport: branch '%s' is not the declared LTS branch "
                   "('%s') — one policy, one branch"
                   % (branch, pol["branch"]))
    sha = git.rev_parse(args.commit)
    if sha is None:
        return die("backport: '%s' does not name a commit"
                   % args.commit, 1)

    # The worktree must be clean BEFORE anything: a policy tool that
    # stomps uncommitted work is itself a policy violation.
    original = git.current_branch()
    if git.is_dirty():
        return die("backport: the worktree has uncommitted changes — "
                   "commit or stash them first (the tool refuses to "
                   "stomp work)", 1)

    # The Stage 145 window gate, part one: the phase. A dead line
    # takes nothing — not even a chore, not even a question about
    # the commit's class; the refusal names the day the line died.
    at = _resolve_at(args)
    if at is None:
        return die("backport: --at must be a plain ISO day, YYYY-MM-DD "
                   "(the policy speaks in days)", 1)
    base_day = _line_base_day(git, pol)
    if base_day is None:
        return die("backport: the base commit's day cannot be read from "
                   "git — the window law refuses to guess (fail-closed)",
                   1)
    phase = _phase_at(base_day, at)
    w = _window(base_day)
    if phase == PHASE_EOL:
        print("backport: REFUSED — the LTS line of %s reached end of "
              "life on %s — nothing lands on a dead line (judged at %s)"
              % (pol["base_tag"], w["security_until"].isoformat(),
                 at.isoformat()))
        return 1

    c = classify_commit(git, sha)
    if args.dry_run:
        print("backport: dry run")
        print("  upstream commit %s (%s)" % (sha[:12], c["subject"]))
        print("  class: %s — %s" % (c["class"], c["reason"]))
        print("  window: %s (since %s, until %s; judged at %s)"
              % (phase, w["bug_fix_until"].isoformat(),
                 w["security_until"].isoformat(), at.isoformat()))
        if c["security"]:
            print("  security: marked — %s" % c["security_reason"])
        if c["class"] == "BUGFIX":
            if phase == PHASE_SECURITY and not c["security"]:
                print("  would refuse: the line is security-only and "
                      "this fix carries no 'Security:' trailer")
            else:
                print("  would cherry-pick onto %s with a '%s: %s' "
                      "trailer" % (branch, TRAILER, sha))
        else:
            print("  would refuse: only BUGFIX commits may be backported")
        return 0

    if c["class"] != "BUGFIX":
        print("backport: REFUSED — commit %s classifies %s, only BUGFIX "
              "may be backported" % (sha[:12], c["class"]))
        print("  reason: %s" % c["reason"])
        for r in c["reasons"][1:]:
            print("  - %s" % r)
        return 1

    # The window gate, part two: the security-only phase takes only
    # marked fixes. The class law said WHAT the commit is; the window
    # law says whether the line still wants it.
    if phase == PHASE_SECURITY and not c["security"]:
        print("backport: REFUSED — the LTS line of %s is security-only "
              "since %s (until %s, judged at %s): only fixes marked "
              "'Security: <reason>' may land — this one carries no "
              "Security trailer"
              % (pol["base_tag"], w["bug_fix_until"].isoformat(),
                 w["security_until"].isoformat(), at.isoformat()))
        return 1

    git.run("checkout", "-q", branch)
    had_pick = False
    try:
        p = git.run("cherry-pick", "-x", sha, check=False)
        if p.returncode != 0:
            had_pick = True
            raise RuntimeError("cherry-pick conflicted: %s"
                               % p.stdout.strip()[:400])
        # Append the machine-readable provenance trailer.
        new_msg = git.message("HEAD").rstrip("\n")
        if not _trailer_sha(new_msg):
            new_msg = new_msg + "\n\n%s: %s\n" % (TRAILER, sha)
            git.run("commit", "--amend", "-q", "-F", "-", check=False,
                    input=new_msg)
        picked = git.rev_parse("HEAD")
        print("backport: applied %s onto %s as %s"
              % (sha[:12], branch, picked[:12]))
        print("  provenance: %s: %s" % (TRAILER, sha))
        if phase == PHASE_SECURITY:
            print("  window: security-only — the line takes security "
                  "fixes until %s"
                  % w["security_until"].isoformat())
        else:
            print("  window: %s — the line takes bug fixes until %s "
                  "(security until %s)"
                  % (phase, w["bug_fix_until"].isoformat(),
                     w["security_until"].isoformat()))
        git.run("checkout", "-q", original)
        return 0
    except RuntimeError as exc:
        _checkout_or_restore(git, original, had_pick)
        git.run("checkout", "-q", original)
        return die("backport: REFUSED — %s (the branch is untouched)"
                   % exc, 1)


def _lts_tag_law(pol: Dict, v: semver.SemVer, tag: str) -> Optional[str]:
    """The version law for an LTS tag. Returns the violation text or
    None when the tag obeys."""
    base = semver.tag_version(pol["base_tag"])
    if v.prerelease:
        return ("tag '%s' carries a pre-release — an LTS release is a "
                "release" % tag)
    if v.build:
        return ("tag '%s' carries build metadata — LTS tags are plain "
                "vMAJOR.MINOR.PATCH" % tag)
    if v.major != base.major or v.minor != base.minor:
        return ("tag '%s' changes major.minor — an LTS branch is "
                "patch-only (base %s)" % (tag, pol["base_tag"]))
    if v.major < 1:
        return ("tag '%s' is pre-1.0 — the LTS policy begins at v1.0"
                % tag)
    return None


def cmd_tag_patch(args) -> int:
    repo = _require_repo(_repo_arg(args))
    git = Git(repo)
    pol = load_policy(git)
    if pol is None:
        return die("tag-patch: no LTS policy declared — run `hls-lts init "
                   "--tag v1.0.0` first", 1)
    branch = args.branch or pol["branch"]
    if branch != pol["branch"]:
        return die("tag-patch: branch '%s' is not the declared LTS branch "
                   "('%s')" % (branch, pol["branch"]))
    base = semver.tag_version(pol["base_tag"])

    # The Stage 145 window gate: no release is cut from a dead line.
    # Judged before anything else — the empty-release question does
    # not even arise on a line that ended.
    at = _resolve_at(args)
    if at is None:
        return die("tag-patch: --at must be a plain ISO day, YYYY-MM-DD "
                   "(the policy speaks in days)", 1)
    base_day = _line_base_day(git, pol)
    if base_day is None:
        return die("tag-patch: the base commit's day cannot be read from "
                   "git — the window law refuses to guess (fail-closed)",
                   1)
    phase = _phase_at(base_day, at)
    w = _window(base_day)
    if phase == PHASE_EOL:
        print("tag-patch: REFUSED — no release is cut from a dead line: "
              "the LTS line of %s reached end of life on %s (judged at "
              "%s)" % (pol["base_tag"],
                       w["security_until"].isoformat(),
                       at.isoformat()))
        return 1

    # The last LTS tag on the branch: same major.minor, the highest
    # patch. The chain starts at the base tag itself.
    lts_tags: List[Tuple[int, str]] = []
    for t in git.tags_on(branch):
        v = semver.tag_version(t)
        if v is None:
            continue
        if v.major == base.major and v.minor == base.minor and \
                v.patch >= base.patch and not v.prerelease:
            lts_tags.append((v.patch, t))
    lts_tags.sort()
    last_patch = lts_tags[-1][0] if lts_tags else base.patch - 1
    # Base tag itself counts as the chain's floor: next is base.patch+1
    # when no tag has been cut yet. Guard: when the only tag so far IS
    # the base, last_patch == base.patch and next is +1. Correct.
    last_tag = lts_tags[-1][1] if lts_tags else pol["base_tag"]

    # The empty-release law: an LTS release carries at least one
    # commit since the previous tag (the base tag for the first). A
    # tag on the same commit as the last release is noise, and a
    # changelog of noise is how policy drift starts.
    since_ref = last_tag
    head_sha = git.branch_sha(branch)
    since_sha = git.rev_parse(since_ref)
    if head_sha is None:
        return die("tag-patch: branch '%s' does not resolve" % branch)

    if args.tag:
        v = semver.tag_version(args.tag)
        if v is None:
            return die("tag-patch: '%s' is not a valid semantic version"
                       % args.tag)
        law = _lts_tag_law(pol, v, args.tag)
        if law is not None:
            print("tag-patch: REFUSED — %s" % law)
            return 1
        expected = "v%d.%d.%d" % (base.major, base.minor, last_patch + 1)
        if v.patch != last_patch + 1:
            print("tag-patch: REFUSED — the LTS tag chain has no gaps: "
                  "%s follows %s (expected %s)"
                  % (args.tag, last_tag, expected))
            return 1
        want = args.tag
    else:
        want = "v%d.%d.%d" % (base.major, base.minor, last_patch + 1)

    if head_sha == since_sha:
        print("tag-patch: REFUSED — no commits since %s: an empty "
              "release is not a release (backport a fix first)"
              % last_tag)
        return 1

    # The security-release law: a release cut in the security-only
    # phase carries at least one marked backport since the previous
    # tag. A chore-only release in that phase is how a security line
    # pretends to be alive — the refusal names it as such.
    if phase == PHASE_SECURITY:
        marked = 0
        for csha in git.commits(since_sha, branch):
            t = _trailer_sha(git.message(csha))
            if t is None:
                continue
            up = git.rev_parse(t)
            if up is not None and classify_commit(git, up)["security"]:
                marked += 1
        if marked == 0:
            print("tag-patch: REFUSED — a release in the security-only "
                  "phase carries at least one security backport — since "
                  "%s there is none (chore-only releases are how a "
                  "security line pretends to be alive; judged at %s)"
                  % (last_tag, at.isoformat()))
            return 1

    if git.tag_exists(want):
        return die("tag-patch: tag '%s' already exists — the chain has "
                   "no duplicates" % want)
    if args.dry_run:
        print("tag-patch: dry run — would tag %s at %s (%s)"
              % (want, branch, git.branch_sha(branch)[:12]))
        return 0
    msg = "Release %s (LTS %s)\n\nPatch release on the %s LTS branch " \
          "(backport-only policy).\n" % (want, pol["base_tag"], branch)
    git.run("tag", "-a", want, branch, "-m", msg)
    print("tag-patch: tagged %s at %s (%s)" % (want, branch,
                                               git.branch_sha(branch)[:12]))
    return 0


def audit_violations(git: Git, pol: Dict) -> List[str]:
    """Every commit on the branch since the base and every tag on it,
    each judged by the policy. Returns the violation list."""
    violations: List[str] = []
    branch = pol["branch"]
    base_sha = pol["base_sha"]

    # The Stage 145 window the history is judged against. Fail-closed:
    # a base day git cannot name is itself a violation — the audit
    # refuses to judge history it cannot date.
    base_day = _line_base_day(git, pol)
    if base_day is not None:
        w = _window(base_day)
    else:
        w = None
        violations.append(
            "the base commit's day cannot be read — the window law "
            "refuses to judge history it cannot date (fail-closed)")

    for sha in git.commits(base_sha, branch):
        msg = git.message(sha)
        subject = msg.splitlines()[0] if msg.splitlines() else ""
        trailer = _trailer_sha(msg)
        if trailer is None and CHERRY_MARK in msg:
            m = re.search(r"\(cherry picked from commit ([0-9a-f]{7,40})\)",
                          msg)
            if m and git.rev_parse(m.group(1)):
                trailer = git.rev_parse(m.group(1))

        # The window audit, part one: WHEN it landed. History is judged
        # the day it happened — a commit past end of life is a commit
        # on a dead line, whatever else it is.
        landed = _commit_day(git.commit_time(sha))
        past_eol = w is not None and landed is not None \
            and landed > w["security_until"]
        in_security = w is not None and landed is not None \
            and w["bug_fix_until"] < landed <= w["security_until"]
        if past_eol:
            violations.append(
                "%s '%s' landed on %s, past end of life (%s) — a dead "
                "line takes nothing, not even bookkeeping"
                % (sha[:12], subject[:60], landed.isoformat(),
                   w["security_until"].isoformat()))
            continue

        if trailer is None:
            c = classify_commit(git, sha)
            if c["class"] == "CHORE":
                continue
            violations.append(
                "%s '%s' is %s on the LTS branch — %s"
                % (sha[:12], subject[:60], c["class"],
                   "only BUGFIX backports and chores may land"
                   if c["class"] == "FEATURE" else
                   "unclassified commits are refused (fail-closed)"))
            continue
        up = git.rev_parse(trailer)
        if up is None:
            violations.append(
                "%s '%s' carries %s: %s — the upstream commit does not "
                "exist here (provenance must be checkable)"
                % (sha[:12], subject[:60], TRAILER, trailer[:12]))
            continue
        c = classify_commit(git, up)
        if c["class"] != "BUGFIX":
            violations.append(
                "%s '%s' backports %s, which classifies %s — %s"
                % (sha[:12], subject[:60], trailer[:12], c["class"],
                   c["reason"]))
            continue

        # The window audit, part two: WHAT landed in the security-only
        # phase — a plain fix that arrived after the bug-fix horizon
        # is a violation even though the class law smiled on it.
        if in_security and not c["security"]:
            violations.append(
                "%s '%s' landed in the security-only phase (on %s, after "
                "%s) without a Security mark — plain fixes do not land "
                "in that window"
                % (sha[:12], subject[:60], landed.isoformat(),
                   w["bug_fix_until"].isoformat()))
            continue

        # The backport's own diff must not introduce surface either
        # (conflict resolution is where features sneak in).
        c2 = classify_commit(git, sha)
        if c2["class"] == "FEATURE":
            violations.append(
                "%s '%s' resolves its backport into new API surface — %s"
                % (sha[:12], subject[:60], c2["reason"]))
    # The tag law. The branch's release chain is the tags that CLAIM
    # to be its releases: same major.minor as the base. Everything
    # else reachable (the pre-1.0 history the branch forks from, tags
    # of other lines) is outside this policy's scope — except a
    # release BEYOND the line, which is a breach wherever it was cut
    # from.
    base = semver.tag_version(pol["base_tag"])
    chain: List[Tuple[int, str]] = []
    for t in git.tags_on(branch):
        v = semver.tag_version(t)
        if v is None:
            continue  # a non-semver tag claims no release here
        if v.major == base.major and v.minor == base.minor \
                and v.patch == base.patch:
            continue  # the base tag is the floor, not a step
        if v.major > base.major or (v.major == base.major
                                    and v.minor > base.minor):
            violations.append(
                "tag %s cuts a release beyond the LTS line (%s) from "
                "this branch" % (t, pol["base_tag"]))
            continue
        if v.major != base.major or v.minor != base.minor:
            continue  # older history the branch descends from
        law = _lts_tag_law(pol, v, t)
        if law is not None:
            violations.append("tag %s: %s" % (t, law))
            continue

        # The window audit, part three: a release cut past end of
        # life. A release is cut from the commit it names; the commit's
        # day is the release's day.
        tday = _line_base_day_of_tag(git, t)
        if w is not None and tday is not None \
                and tday > w["security_until"]:
            violations.append(
                "tag %s is cut from a commit dated %s, past end of life "
                "(%s) — no release from a dead line"
                % (t, tday.isoformat(),
                   w["security_until"].isoformat()))
            continue
        chain.append((v.patch, t))
    chain.sort()
    expect = base.patch + 1
    for p, t in chain:
        if p != expect:
            violations.append(
                "tag chain has a gap or duplicate: %s — expected "
                "v%d.%d.%d (the chain is +1 each release)"
                % (t, base.major, base.minor, expect))
            expect = p
        expect += 1
    return violations


def cmd_audit(args) -> int:
    repo = _require_repo(_repo_arg(args))
    git = Git(repo)
    pol = load_policy(git)
    if pol is None:
        return die("audit: no LTS policy declared — run `hls-lts init "
                   "--tag v1.0.0` first", 1)
    violations = audit_violations(git, pol)
    if args.json:
        print(json.dumps({"violations": violations}, indent=2))
        return 1 if violations else 0
    print("audit: branch %s (base %s) — %d commit(s), %d tag(s)"
          % (pol["branch"], pol["base_tag"],
             len(git.commits(pol["base_sha"], pol["branch"])),
             len(git.tags_on(pol["branch"]))))
    if violations:
        print("audit: %d violation(s):" % len(violations))
        for v in violations:
            print("  - %s" % v)
        return 1
    print("audit: clean — every commit is a BUGFIX backport or a "
          "chore, every tag obeys the patch law")
    return 0


def cmd_window(args) -> int:
    repo = _require_repo(_repo_arg(args))
    git = Git(repo)

    at = _resolve_at(args)
    if at is None:
        return die("window: --at must be a plain ISO day, YYYY-MM-DD "
                   "(the policy speaks in days)", 1)

    # The line to judge: the declaration's, or any release tag named
    # explicitly. Both roads meet at the same law: the tag must parse
    # as semver and be a >= 1.0.0 release (the windows begin where
    # the policy does).
    if args.tag:
        v = semver.tag_version(args.tag)
        if v is None:
            return die("window: tag '%s' is not a valid semantic version"
                       % args.tag, 1)
        if v.prerelease:
            return die("window: tag '%s' carries a pre-release — the "
                       "support windows are a release's promise" % args.tag,
                       1)
        if v.major < 1:
            return die("window: tag '%s' is pre-1.0 — the policy begins "
                       "after v1.0" % args.tag, 1)
        if not git.tag_exists(args.tag):
            return die("window: tag '%s' does not exist in the repository"
                       % args.tag, 1)
        base_day = _line_base_day_of_tag(git, args.tag)
        label = args.tag
    else:
        pol = load_policy(git)
        if pol is None:
            return die("window: no LTS policy declared — name a tag with "
                       "`window --tag v1.0.0`, or declare the line with "
                       "`hls-lts init --tag v1.0.0`", 1)
        base_day = _line_base_day(git, pol)
        label = pol["base_tag"]

    if base_day is None:
        return die("window: the base commit's day cannot be read from "
                   "git — the window law refuses to guess (fail-closed)",
                   1)

    win = _window_payload(base_day, at)
    if args.json:
        win["base_tag"] = label
        print(json.dumps(win, indent=2))
        return 0

    print("LTS line of %s — base released %s" % (label, win["base_date"]))
    print("  bug fixes until:  %s (through the anniversary day)"
          % win["bug_fix_until"])
    print("  security until:   %s (through the anniversary day)"
          % win["security_until"])
    print("  phase at %s: %s — %s"
          % (win["at"], win["phase"], PHASE_MEANING[win["phase"]]))
    if win["phase"] == PHASE_ACTIVE:
        print("  full support days left: %d (then security-only until %s)"
              % (win["days_bugfix_left"], win["security_until"]))
    elif win["phase"] == PHASE_SECURITY:
        print("  security-only days left: %d" % win["days_security_left"])
    else:
        print("  the line ended %s — %d days past end of life"
              % (win["security_until"], -win["days_security_left"]))
    return 0


# ---------------------------------------------------------------------------
# The published document (Stage 145). Rendered from the constants above,
# deterministic per input: two renders are byte-identical, and a
# hand-edited copy is visible as a diff.
# ---------------------------------------------------------------------------

def render_policy() -> str:
    L: List[str] = []
    a = L.append
    a("# Long-term support policy — Halis (HLS)")
    a("")
    a("Generated from the live constants of `tools/hls-lts.py` —")
    a("this document is rendered, not maintained. Do not hand-edit;")
    a("regenerate it with `python3 tools/hls-lts.py policy --out")
    a("LTS.md`. `make lts-policy-verify` re-derives the committed copy")
    a("against the same render and refuses drift — the numbers below")
    a("are the numbers the gates enforce, or the tree says no.")
    a("")
    a("## The promise")
    a("")
    a("Every LTS line this project opens gets **%d years of bug-fix"
      % BUGFIX_YEARS)
    a("support** and **%d years of security support**, counted from"
      % SECURITY_YEARS)
    a("the day its base release was cut. The last %d years of a %d-year"
      % (SECURITY_YEARS - BUGFIX_YEARS, SECURITY_YEARS))
    a("line are security-only: after the third anniversary a plain")
    a("bug fix is refused on the line even when it is a perfectly good")
    a("fix, and after the fifth the line is dead and takes nothing at")
    a("all. A published promise a tool cannot read is prose; this one")
    a("is arithmetic (`hls-lts window`), it is enforced where a change")
    a("would enter the branch (`hls-lts backport`, `hls-lts")
    a("tag-patch`), and it audits history after the fact (`hls-lts")
    a("audit`).")
    a("")
    a("## The line, and the day it is measured from")
    a("")
    a("An LTS line is a major.minor — `1.0` is the line of `v1.0.0`,")
    a("`v1.0.1`, `v1.0.2` and every patch after. The window belongs to")
    a("the LINE, not to each patch: it is measured from the committer")
    a("day of the base release (`vMAJOR.MINOR.0`'s commit), and every")
    a("patch on the line lives inside the same window. A line born")
    a("late does not get a late window; a patch cut late does not")
    a("extend one. `hls-lts init --tag vX.Y.0` declares the line;")
    a("`hls-lts window` reads it, with `--at` to pin the day the law")
    a("is judged on (default: today) — the same law, on any day.")
    a("")
    a("## The three phases")
    a("")
    a("| phase | days | what may land on the line |")
    a("|-------|------|----------------------------|")
    a("| `active` | base → base +%d years | any BUGFIX backport and "
      "chores |" % BUGFIX_YEARS)
    a("| `security` | base +%d → +%d years | SECURITY-marked BUGFIX "
      "backports and chores |" % (BUGFIX_YEARS, SECURITY_YEARS))
    a("| `eol` | past base +%d years | nothing — a dead line takes "
      "nothing |" % SECURITY_YEARS)
    a("")
    a("The horizon day itself belongs to the phase it closes: support")
    a("runs THROUGH the anniversary, so the third birthday is the last")
    a("full-support day and the day after it is security-only; the")
    a("fifth birthday is the last supported day and the day after it")
    a("is end of life.")
    a("")
    a("## The calendar law")
    a("")
    a("`base + N years` is the same month and day — an anniversary,")
    a("not a fixed count of days. February 29 clamps to the 28th when")
    a("the target year is not a leap year (a line born 2024-02-29 is")
    a("bug-fixed through 2027-02-28). The policy speaks in days: every")
    a("date is a calendar day, read from git's committer timestamp —")
    a("the same clock the landing laws already read.")
    a("")
    a("## What makes a fix a security fix")
    a("")
    a("A `Security: <reason>` line in the commit message (a CVE id, a")
    a("one-word severity, a sentence — anything non-empty). The name")
    a("matches case-insensitively, the first such line decides, and an")
    a("empty value is not a marking. Fail-closed, like everything in")
    a("this policy: **no trailer, no security claim** — an unmarked")
    a("fix is a plain fix, and a plain fix does not land on a")
    a("security-only line. The trailer is a line of the message,")
    a("wherever it stands; `hls-lts classify` reports the marking")
    a("beside the class (a marked FEATURE is still a FEATURE, and")
    a("still refused — the marking is a fact about a fix, not a key")
    a("past the class law).")
    a("")
    a("## What the tool refuses")
    a("")
    a("- `backport` in the `security` phase: a plain BUGFIX is")
    a("  refused with the phase and both horizons named; in `eol`,")
    a("  everything is refused — the dead line takes no chores either.")
    a("- `tag-patch` past end of life: no release is cut from a dead")
    a("  line. In the `security` phase a release must carry at least")
    a("  one security backport since the previous tag — a chore-only")
    a("  release is how a security line pretends to be alive.")
    a("- `audit`: history is judged the day it landed. A backport")
    a("  that arrived in the security phase without the mark, any")
    a("  commit past end of life, and any tag cut from a commit dated")
    a("  past end of life are violations, named sha by sha.")
    a("")
    a("## Relationship to the branch policy")
    a("")
    a("This is the second dimension of the Stage 132 policy, not a new")
    a("one. `halis-lts-1` keeps its name and its declaration; the")
    a("window law derives from the base tag live on every run and")
    a("adds no state. Stage 132 said WHAT may land on an LTS branch")
    a("(backport-only, patch-only, fail-closed); this policy says")
    a("WHEN — the same line, with a birthday and a death day.")
    a("")
    a("## When it begins")
    a("")
    a("The policy begins at v1.0 — a pre-1.0 LTS is a contradiction.")
    a("Until the first LTS line is declared this document is the")
    a("promise; the day `hls-lts init --tag v1.0.0` runs, it becomes")
    a("the clock. The gates prove the law in the meantime on scratch")
    a("lines with fabricated birthdays (`make lts-policy-acceptance`),")
    a("so the enforcement is tested before it is ever needed.")
    a("")
    return "\n".join(L)


def cmd_policy(args) -> int:
    text = render_policy()
    if args.check:
        repo = _repo_arg(args)
        path = os.path.join(repo, POLICY_DOC)
        if not os.path.isfile(path):
            return die("policy: %s is not published here — the policy "
                       "document is missing (regenerate with "
                       "`python3 tools/hls-lts.py policy --out %s`)"
                       % (POLICY_DOC, POLICY_DOC), 1)
        with open(path, "r", encoding="utf-8") as fh:
            committed = fh.read()
        if committed != text:
            print("policy: REFUSED — the committed %s drifted from the "
                  "tool's render — a hand-edited policy is visible as a "
                  "diff (regenerate with `python3 tools/hls-lts.py "
                  "policy --out %s`)" % (POLICY_DOC, POLICY_DOC))
            return 1
        print("policy: %s matches the render — the published promise "
              "is the enforced one (%d years bug-fix, %d years security)"
              % (POLICY_DOC, BUGFIX_YEARS, SECURITY_YEARS))
        return 0
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("policy: rendered %s (%d lines)" % (args.out, len(text.splitlines())))
        return 0
    sys.stdout.write(text)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    import argparse
    ap = argparse.ArgumentParser(
        prog="hls-lts",
        description="LTS branch policy — backport-only after v1.0 "
                    "(Stage 132), support windows 3y bug-fix / 5y "
                    "security (Stage 145)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("init", help="declare the LTS branch from a "
                                    "release tag (>= 1.0.0)")
    p.add_argument("--tag", required=True, help="the base release tag")
    p.add_argument("--branch", help="the LTS branch (default %s)"
                   % DEFAULT_BRANCH)
    p.add_argument("--repo", help="target repository (default: this repo)")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("status", help="print the declaration and state")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("classify", help="classify a commit")
    p.add_argument("commit")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_classify)

    p = sub.add_parser("backport", help="cherry-pick a BUGFIX under the "
                                        "policy")
    p.add_argument("commit")
    p.add_argument("--branch")
    p.add_argument("--repo")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--at", help="pin the day the window law is judged "
                                 "on, YYYY-MM-DD (default: today)")
    p.set_defaults(fn=cmd_backport)

    p = sub.add_parser("tag-patch", help="cut the next LTS patch tag")
    p.add_argument("--tag", help="explicit tag (the law still applies)")
    p.add_argument("--branch")
    p.add_argument("--repo")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--at", help="pin the day the window law is judged "
                                 "on, YYYY-MM-DD (default: today)")
    p.set_defaults(fn=cmd_tag_patch)

    p = sub.add_parser("audit", help="judge the branch against the "
                                     "policy")
    p.add_argument("--branch")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("window", help="the line's support window and "
                                     "phase (3y bug-fix, 5y security)")
    p.add_argument("--tag", help="judge this release tag's line "
                                 "(default: the declared line)")
    p.add_argument("--at", help="pin the day the law is judged on, "
                                 "YYYY-MM-DD (default: today)")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_window)

    p = sub.add_parser("policy", help="render the published policy "
                                     "document (LTS.md); --check verifies "
                                     "the committed copy")
    p.add_argument("--out", help="write the render to this path")
    p.add_argument("--repo")
    p.add_argument("--check", action="store_true")
    p.set_defaults(fn=cmd_policy)

    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    try:
        return args.fn(args)
    except SystemExit:
        raise
    except RuntimeError as exc:
        return die(str(exc), 1)


if __name__ == "__main__":
    sys.exit(main())
