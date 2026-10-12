#!/usr/bin/env python3
"""hls-lts — the Stage 132 LTS branch policy (`backport-only after v1.0`).

Usage:
  python3 tools/hls-lts.py init --tag v1.0.0 [--branch release/1.0] [--repo PATH]
  python3 tools/hls-lts.py status  [--repo PATH] [--json]
  python3 tools/hls-lts.py classify <commit> [--repo PATH] [--json]
  python3 tools/hls-lts.py backport <commit> [--branch release/1.0]
                                     [--repo PATH] [--dry-run]
  python3 tools/hls-lts.py tag-patch [--tag vX.Y.Z] [--branch release/1.0]
                                     [--repo PATH] [--dry-run]
  python3 tools/hls-lts.py audit    [--branch release/1.0] [--repo PATH] [--json]

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

Exit codes: 0 ok, 1 policy refusal (the rule is named on stdout/stderr),
2 usage error. Every refusal names the RULE, not just the fact.

The version laws reuse the Stage 131 semver module
(tools/hpkg_parts/hpkg_semver.py) — one grammar for the whole
project, from the package manifest to the release train.
"""
from __future__ import annotations

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
    fail-closed law."""
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
    return {
        "sha": git.rev_parse(sha) or sha,
        "subject": subject,
        "class": cls,
        "reason": reasons[0] if reasons else reason,
        "reasons": reasons,
    }


def is_backport_commit(git: Git, sha: str) -> bool:
    return _trailer_sha(git.message(sha)) is not None \
        or CHERRY_MARK in git.message(sha)


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
    if args.json:
        print(json.dumps({
            "policy": pol,
            "commits_since_base": len(commits),
            "tags": tags,
            "violations": violations,
        }, indent=2))
    else:
        print("LTS policy %s — branch %s from %s (%s)"
              % (pol["policy"], branch, pol["base_tag"], base_sha[:12]))
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

    c = classify_commit(git, sha)
    if args.dry_run:
        print("backport: dry run")
        print("  upstream commit %s (%s)" % (sha[:12], c["subject"]))
        print("  class: %s — %s" % (c["class"], c["reason"]))
        if c["class"] == "BUGFIX":
            print("  would cherry-pick onto %s with a '%s: %s' trailer"
                  % (branch, TRAILER, sha))
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
    for sha in git.commits(base_sha, branch):
        msg = git.message(sha)
        subject = msg.splitlines()[0] if msg.splitlines() else ""
        trailer = _trailer_sha(msg)
        if trailer is None and CHERRY_MARK in msg:
            m = re.search(r"\(cherry picked from commit ([0-9a-f]{7,40})\)",
                          msg)
            if m and git.rev_parse(m.group(1)):
                trailer = git.rev_parse(m.group(1))
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    import argparse
    ap = argparse.ArgumentParser(
        prog="hls-lts",
        description="LTS branch policy — backport-only after v1.0 "
                    "(Stage 132)")
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
    p.set_defaults(fn=cmd_backport)

    p = sub.add_parser("tag-patch", help="cut the next LTS patch tag")
    p.add_argument("--tag", help="explicit tag (the law still applies)")
    p.add_argument("--branch")
    p.add_argument("--repo")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_tag_patch)

    p = sub.add_parser("audit", help="judge the branch against the "
                                     "policy")
    p.add_argument("--branch")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_audit)

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
