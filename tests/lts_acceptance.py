#!/usr/bin/env python3
"""Stage 132 acceptance gate — the LTS branch policy: backport-only
after v1.0 (v0.150.0-alpha).

Run with `make lts-acceptance` (or
`python3 tests/lts_acceptance.py`).

Eight sections over the REAL policy CLI (subprocess, the interface a
release manager drives) and REAL scratch git repositories — an LTS
gate that did not drive git would be a memo, not a policy:

  1. the convention  — `hls-lts` exits 0 ok / 1 policy refusal /
     2 usage error; every refusal names the RULE (the missing policy,
     the non-semver base, the unknown commit) instead of saying
     "error"; an unknown subcommand is argparse's 2

  2. the init law    — the base tag must exist, parse as semver and
     be a >= 1.0.0 RELEASE (pre-1.0 refused — the policy begins
     after v1.0; a pre-release base refused — an LTS is a release);
     the declaration is written (.hls-lts.json: branch, base tag,
     base sha); a missing branch is created AT the base; re-init is
     an idempotent no-op; a different tag or branch is a refusal;
     status prints the declaration

  3. the classifier  — the message table over real commits (the five
     BUGFIX prefixes, the FEATURE set, the CHORE set), the fail-closed
     default (bare messages, unrecognized prefixes and colon-less
     "Stage N (...)" subjects are UNKNOWN), and the diff gate: a
     `fix:` that adds a top-level fn in std/ is a FEATURE in
     disguise (named), a `fix:` that edits one in place stays BUGFIX,
     a `def` in tools/ is implementation detail (stays BUGFIX), and
     SPEC.md edits are language changes (FEATURE)

  4. the backport    — the green path: a BUGFIX cherry-picks onto the
     law             branch with a `Backported-from:` trailer, the
     worktree returns to the branch it started on; --dry-run
     predicts and touches nothing; FEATURE, CHORE and UNKNOWN
     upstreams are refused BY CLASS; a conflicted pick is refused
     with the branch byte-identical; a dirty worktree is refused;
     a duplicate pick (already applied) is refused

  5. the tag law     — tag-patch on a branch with no commits since
     the base refuses (an empty release is not a release); after a
     backport it cuts v1.0.1; an explicit v1.1.0 is refused (minor),
     a v1.0.1-pre refused (pre-release), a v1.0.3 refused (the chain
     has no gaps — expected v1.0.2), a non-semver tag refused; a
     second tag-patch without new commits is refused

  6. the audit law   — a branch of backports and chores audits clean;
     a rogue direct commit is a violation (named sha + subject); a
     forged `Backported-from:` that names a FEATURE upstream is a
     violation (the trailer is checked, not trusted); a trailer
     naming a nonexistent sha is a violation; a tag gap and an
     out-of-line tag are violations

  7. the release     — the whole train: init -> backport -> v1.0.1 ->
     train           backport -> v1.0.2 -> audit clean -> status
                     shows the chain — twice through the loop proves
                     the tool is a workflow, not a demo

  8. the corpus      — tests/ok/feat_stage132_lts.hls is
     showcase        hlfmt-canonical, hllint-clean, boots through
                     the real checker, runs green on the
                     interpreter and (when bin/hlc exists) matches
                     natively byte for byte

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

LTS = [os.path.join(ROOT, "bin", "hls-lts")]
BOOT = [sys.executable, os.path.join(ROOT, "tools", "hlinterp", "boot.py")]
FMT = [os.path.join(ROOT, "bin", "hlfmt")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage132_lts.hls")
SHOWCASE_OUT = ("stage 132: hls-lts\n"
                "  LTS policy OK: classifier, tag law, chain, trailer, "
                "release\n")

GATE = tempfile.mkdtemp(prefix=".lts-gate-", dir=ROOT)

GIT_ENV = dict(os.environ)
GIT_ENV["GIT_AUTHOR_NAME"] = "lts-gate"
GIT_ENV["GIT_AUTHOR_EMAIL"] = "lts-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_NAME"] = "lts-gate"
GIT_ENV["GIT_COMMITTER_EMAIL"] = "lts-gate@example.invalid"

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


def run(argv, cwd=None):
    return subprocess.run(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=300,
                          text=True, cwd=cwd, env=GIT_ENV)


def lts(args, repo=None):
    a = LTS + args
    if repo is not None:
        a += ["--repo", repo]
    return run(a)


def git(repo, *args):
    return run(["git", "-C", repo] + list(args))


def make_repo(name, base_file="app.txt"):
    """A fresh scratch repo with one base commit tagged v1.0.0."""
    r = os.path.join(GATE, name)
    os.makedirs(r)
    assert git(r, "init", "-q", "-b", "main", ".").returncode == 0
    commit(r, {base_file: "seed line\n"}, "stage 1: seed the project")
    assert git(r, "tag", "v1.0.0").returncode == 0
    return r


def commit(repo, files, message, tag=None):
    for name, content in files.items():
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    # Add ONLY the named files: a blanket `git add -A` would sweep the
    # untracked .hls-lts.json into branch commits, and a later
    # checkout back to main would then DELETE the policy file (git
    # removes files tracked by the departing branch that the arriving
    # one does not track).
    git(repo, "add", "--", *files.keys())
    p = git(repo, "commit", "-q", "--allow-empty-message", "-m", message)
    assert p.returncode == 0, p.stdout
    if tag:
        assert git(repo, "tag", tag).returncode == 0
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def branch_head(repo, branch):
    p = git(repo, "rev-parse", branch)
    return p.stdout.strip() if p.returncode == 0 else None


def messages(repo, branch, base_sha):
    p = git(repo, "log", "--format=%s", base_sha + ".." + branch,
            "--reverse")
    return p.stdout.splitlines()


# ===========================================================================
def _section_1():
    print("Section 1 — the convention")

    # The no-policy refusals run against a scratch repo: the real tree
    # declared its line with the v1.0.0 release, and the refusal this
    # section pins belongs to a tree that has not.
    nopolicy = make_repo("no-policy")

    p = lts(["status", "--repo", nopolicy])
    check(p.returncode == 1 and "no LTS policy declared" in p.stdout
          and "init --tag v1.0.0" in p.stdout,
          "status without a policy refuses and names the init remedy")

    p = lts(["backport", "HEAD", "--repo", nopolicy])
    check(p.returncode == 1 and "no LTS policy declared" in p.stdout,
          "backport without a policy refuses, named")

    p = lts(["audit", "--repo", nopolicy])
    check(p.returncode == 1 and "no LTS policy declared" in p.stdout,
          "audit without a policy refuses, named")

    p = lts(["nosuchcmd"])
    check(p.returncode == 2,
          "an unknown subcommand is a usage error (exit 2)")

    p = lts(["classify", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
             "--repo", GATE])
    check(p.returncode == 1 and "does not name a commit" in p.stdout,
          "classify of an unknown sha refuses, named")

    p = lts(["init"])
    check(p.returncode == 2, "init without --tag is a usage error (2)")

    # Not a git repository at all.
    p = lts(["status", "--repo", os.path.join(GATE, "not-a-repo")])
    check(p.returncode == 1 and "not a git repository" in p.stdout,
          "a non-repo target refuses, named")


def _section_2():
    print("Section 2 — the init law")
    r = make_repo("init-law")

    # The base tag must be a >= 1.0.0 release.
    p = lts(["init", "--tag", "v0.9.0", "--repo", r])
    check(p.returncode == 1 and "pre-1.0" in p.stdout,
          "a pre-1.0 base is refused (the policy begins after v1.0)")

    p = lts(["init", "--tag", "v1.0.0-rc.1", "--repo", r])
    check(p.returncode == 1 and "pre-release" in p.stdout,
          "a pre-release base is refused (an LTS is a release)")

    p = lts(["init", "--tag", "not-a-version", "--repo", r])
    check(p.returncode == 1 and "not a valid semantic version"
          in p.stdout, "a non-semver base is refused, named")

    p = lts(["init", "--tag", "v9.9.9", "--repo", r])
    check(p.returncode == 1 and "does not exist" in p.stdout,
          "a nonexistent tag is refused, named")

    # The green declaration.
    p = lts(["init", "--tag", "v1.0.0", "--branch", "release/1.0",
             "--repo", r])
    check(p.returncode == 0 and "declared LTS branch" in p.stdout,
          "init from v1.0.0 declares the branch")
    pol_path = os.path.join(r, ".hls-lts.json")
    check(os.path.isfile(pol_path),
          "the declaration lands in .hls-lts.json")
    with open(pol_path, encoding="utf-8") as fh:
        pol = json.load(fh)
    check(pol.get("policy") == "halis-lts-1" and
          pol.get("branch") == "release/1.0" and
          pol.get("base_tag") == "v1.0.0",
          "the declaration names the policy, branch and base tag")
    base_sha = branch_head(r, "main")
    check(pol.get("base_sha") == base_sha,
          "the base sha is the tagged commit")
    check(branch_head(r, "release/1.0") == base_sha,
          "the branch was created at the base commit")

    # Idempotence.
    p = lts(["init", "--tag", "v1.0.0", "--branch", "release/1.0",
             "--repo", r])
    check(p.returncode == 0 and "no-op" in p.stdout,
          "re-init with the same tag and branch is a no-op")

    p = lts(["init", "--tag", "v1.0.1", "--repo", r])
    check(p.returncode == 1 and "already declared" in p.stdout,
          "re-init with a different base is a refusal, named")

    # Status reports the declaration.
    p = lts(["status", "--repo", r])
    check(p.returncode == 0 and "release/1.0" in p.stdout
          and "v1.0.0" in p.stdout and "commits since base: 0"
          in p.stdout and "audit: clean" in p.stdout,
          "status prints the declaration and a clean audit")
    p = lts(["status", "--repo", r, "--json"])
    check(p.returncode == 0, "status --json exits 0")
    try:
        data = json.loads(p.stdout)
        check(data["policy"]["base_tag"] == "v1.0.0"
              and data["commits_since_base"] == 0
              and data["violations"] == [],
              "status --json carries the declaration and state")
    except Exception as exc:
        bad("status --json is not well-formed: %s" % exc)


def _section_3():
    print("Section 3 — the classifier")
    r = make_repo("classifier")
    # A std module and a tools file for the diff gate.
    commit(r, {"std/alpha.hls": "fn existing() -> int { return 1 }\n",
               "tools/impl.py": "def old():\n    return 1\n"},
           "stage 1: seed the modules")

    # The BUGFIX prefixes.
    fix_sha = commit(r, {"fix.txt": "one\n"},
                     "fix: widget crash on empty input")
    p = lts(["classify", fix_sha, "--repo", r])
    check(p.returncode == 0 and "class:   BUGFIX" in p.stdout,
          "fix: classifies BUGFIX")
    for subject in ["bugfix: restore shipped behaviour",
                    "revert: the bad constant",
                    "hotfix: unbreak the build",
                    "regression: 0.149 broke str_join"]:
        sha = commit(r, {"notes.txt": subject + "\n"}, subject)
        p = lts(["classify", sha, "--repo", r, "--json"])
        try:
            data = json.loads(p.stdout)
            check(data["class"] == "BUGFIX",
                  "%s -> BUGFIX" % subject.split(":")[0])
        except Exception as exc:
            bad("classify --json broken for %r: %s" % (subject, exc))

    # The FEATURE set.
    for subject in ["feat: new widget", "feature: nicer",
                    "add: another entrypoint", "stage: 133 begins",
                    "spec: the grammar grows", "stdlib: new api",
                    "perf: rewrite the loop", "opt: tighten it",
                    "refactor: move things around"]:
        sha = commit(r, {"notes.txt": subject + "\n"}, subject)
        p = lts(["classify", sha, "--repo", r, "--json"])
        data = json.loads(p.stdout)
        check(data["class"] == "FEATURE",
              "%s -> FEATURE" % subject.split(":")[0])

    # The CHORE set.
    for subject in ["chore: changelog", "release: 1.0.1 notes",
                    "docs: readme typo", "test: more pins",
                    "ci: cache the toolchain", "meta: meeting notes"]:
        sha = commit(r, {"notes.txt": subject + "\n"}, subject)
        p = lts(["classify", sha, "--repo", r, "--json"])
        data = json.loads(p.stdout)
        check(data["class"] == "CHORE",
              "%s -> CHORE" % subject.split(":")[0])

    # The fail-closed law.
    for subject in ["bare message no prefix",
                    "semver: an unrecognized prefix",
                    "Stage 131 (v0.149.0-alpha): a release",
                    ""]:
        sha = commit(r, {"notes.txt": (subject or "empty") + "\n"},
                     subject)
        p = lts(["classify", sha, "--repo", r, "--json"])
        data = json.loads(p.stdout)
        check(data["class"] == "UNKNOWN",
              "fail-closed: %r -> UNKNOWN" % subject[:34])

    # The diff gate: a fix that adds a top-level fn in std/ is a
    # feature in disguise.
    sha = commit(r, {"std/alpha.hls":
                     "fn existing() -> int { return 1 }\n"
                     "fn brand_new_api() -> int { return 9 }\n"},
                 "fix: correct the delta constant")
    p = lts(["classify", sha, "--repo", r, "--json"])
    data = json.loads(p.stdout)
    check(data["class"] == "FEATURE"
          and any("brand_new_api" in x for x in data["reasons"]),
          "a fix adding a std fn re-classes FEATURE, reason named")

    # A fix that edits a fn IN PLACE stays BUGFIX.
    sha = commit(r, {"std/alpha.hls":
                     "fn existing() -> int { return 3 }\n"
                     "fn brand_new_api() -> int { return 9 }\n"},
                 "fix: existing returns 3")
    p = lts(["classify", sha, "--repo", r, "--json"])
    data = json.loads(p.stdout)
    check(data["class"] == "BUGFIX",
          "a fix editing a fn in place stays BUGFIX")

    # A def in tools/ is implementation detail.
    sha = commit(r, {"tools/impl.py": "def old():\n    return 2\n\n"
                     "def new_helper():\n    pass\n"},
                 "fix: tooling repair")
    p = lts(["classify", sha, "--repo", r, "--json"])
    data = json.loads(p.stdout)
    check(data["class"] == "BUGFIX",
          "a def in tools/ does not trip the diff gate")

    # SPEC.md edits are language changes.
    sha = commit(r, {"SPEC.md": "# the grammar\n\nmore of it\n"},
                 "fix: document the real behaviour")
    p = lts(["classify", sha, "--repo", r, "--json"])
    data = json.loads(p.stdout)
    check(data["class"] == "FEATURE"
          and any("SPEC.md" in x for x in data["reasons"]),
          "a fix touching SPEC.md re-classes FEATURE, reason named")


def _section_4():
    print("Section 4 — the backport law")
    r = make_repo("backport-law")
    fix_sha = commit(r, {"bug.txt": "fixed\n"},
                     "fix: the off-by-one in the counter")
    feat_sha = commit(r, {"widget.txt": "new\n"},
                      "feat: the widget nobody asked for")
    chore_sha = commit(r, {"notes.txt": "release notes\n"},
                       "chore: changelog note")
    bare_sha = commit(r, {"notes.txt": "more\n"},
                      "tidy up a few things")

    p = lts(["init", "--tag", "v1.0.0", "--repo", r])
    check(p.returncode == 0, "the policy is declared")

    # Dry run predicts and touches nothing.
    before = branch_head(r, "release/1.0")
    p = lts(["backport", fix_sha, "--repo", r, "--dry-run"])
    check(p.returncode == 0 and "dry run" in p.stdout
          and "BUGFIX" in p.stdout,
          "a dry run predicts the BUGFIX pick")
    check(branch_head(r, "release/1.0") == before,
          "the dry run touched nothing")

    # The green path.
    p = lts(["backport", fix_sha, "--repo", r])
    check(p.returncode == 0 and "applied" in p.stdout
          and "provenance" in p.stdout,
          "the BUGFIX backports cleanly")
    picked = branch_head(r, "release/1.0")
    p = git(r, "log", "-1", "--format=%B", picked)
    body = p.stdout
    check("Backported-from: " + fix_sha in body,
          "the pick carries the Backported-from trailer")
    check("(cherry picked from commit " + fix_sha in body,
          "git's own -x provenance is preserved")
    cur = git(r, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    check(cur == "main",
          "the worktree returned to the branch it started on")
    check(git(r, "status", "--porcelain", "-uno").stdout.strip() == "",
          "the worktree is clean after the pick (the untracked "
          "policy file is the declaration, not dirt)")

    # Refusals: by class.
    before = branch_head(r, "release/1.0")
    p = lts(["backport", feat_sha, "--repo", r])
    check(p.returncode == 1 and "REFUSED" in p.stdout
          and "FEATURE" in p.stdout,
          "a FEATURE upstream is refused by class")
    check(branch_head(r, "release/1.0") == before,
          "the refusal left the branch byte-identical")

    p = lts(["backport", chore_sha, "--repo", r])
    check(p.returncode == 1 and "CHORE" in p.stdout,
          "a CHORE upstream is refused by class (it lands directly, "
          "not via backport)")

    p = lts(["backport", bare_sha, "--repo", r])
    check(p.returncode == 1 and "UNKNOWN" in p.stdout,
          "an UNKNOWN upstream is refused (fail-closed)")
    check(branch_head(r, "release/1.0") == before,
          "all refusals left the branch untouched")

    # A duplicate pick (already applied) is refused, branch intact.
    p = lts(["backport", fix_sha, "--repo", r])
    check(p.returncode == 1 and "REFUSED" in p.stdout,
          "a duplicate pick is refused")
    check(branch_head(r, "release/1.0") == before,
          "the duplicate refusal left the branch untouched")

    # A dirty worktree is refused before anything happens.
    with open(os.path.join(r, "bug.txt"), "a",
              encoding="utf-8") as fh:
        fh.write("uncommitted\n")
    p = lts(["backport", fix_sha, "--repo", r])
    check(p.returncode == 1 and "uncommitted" in p.stdout,
          "a dirty worktree is refused, named")
    os.remove(os.path.join(r, "bug.txt"))
    git(r, "checkout", "-q", "--", ".")

    # A conflicted pick is refused with the branch byte-identical:
    # the branch's own chore edits the same line the upstream fix
    # edits, so git cannot apply it.
    br = branch_head(r, "release/1.0")
    git(r, "checkout", "-q", "release/1.0")
    commit(r, {"app.txt": "branch line\n"},
           "chore: branch-side bookkeeping edit")
    git(r, "checkout", "-q", "main")
    conflict_sha = commit(r, {"app.txt": "main line\n"},
                          "fix: the same line fixed on main")
    p = lts(["backport", conflict_sha, "--repo", r])
    check(p.returncode == 1 and "REFUSED" in p.stdout
          and "untouched" in p.stdout,
          "a conflicted pick is refused, branch untouched")
    check(branch_head(r, "release/1.0") == git(
        r, "rev-parse", "release/1.0").stdout.strip(),
        "the branch ref still resolves")
    p = git(r, "log", "--format=%s", br + "..release/1.0")
    check("fix: the same line fixed on main" not in p.stdout,
          "the conflicted fix did not land")
    check(git(r, "status", "--porcelain",
              "-uno").stdout.strip() == "",
          "no cherry-pick state was left behind")


def _section_5():
    print("Section 5 — the tag law")
    r = make_repo("tag-law")
    lts(["init", "--tag", "v1.0.0", "--repo", r])

    # The empty-release law fires first.
    p = lts(["tag-patch", "--repo", r])
    check(p.returncode == 1 and "empty release is not a release"
          in p.stdout and "v1.0.0" in p.stdout,
          "tag-patch on a branch with no commits refuses, named")

    fix_sha = commit(r, {"bug.txt": "fixed\n"},
                     "fix: the counter wrap")
    p = lts(["backport", fix_sha, "--repo", r])
    check(p.returncode == 0, "the fix backports")

    # Explicit tags are judged before anything is cut.
    p = lts(["tag-patch", "--tag", "v1.1.0", "--repo", r])
    check(p.returncode == 1 and "patch-only" in p.stdout,
          "a minor bump is refused (an LTS is patch-only)")

    p = lts(["tag-patch", "--tag", "v1.0.1-pre", "--repo", r])
    check(p.returncode == 1 and "pre-release" in p.stdout,
          "a pre-release tag is refused (an LTS release is a release)")

    p = lts(["tag-patch", "--tag", "v1.0.3", "--repo", r])
    check(p.returncode == 1 and "no gaps" in p.stdout
          and "expected v1.0.1" in p.stdout,
          "a tag off the chain is refused with the expected name "
          "(no patch release exists yet, so v1.0.1 is next)")

    p = lts(["tag-patch", "--tag", "banana", "--repo", r])
    check(p.returncode == 1 and "not a valid semantic version"
          in p.stdout, "a non-semver tag is refused, named")

    # The auto name.
    p = lts(["tag-patch", "--repo", r])
    check(p.returncode == 0 and "tagged v1.0.1" in p.stdout,
          "tag-patch cuts v1.0.1 after the backport")
    check(git(r, "rev-parse", "v1.0.1^{commit}").stdout.strip()
          == branch_head(r, "release/1.0"),
          "the tag names the branch head")

    # No commits since v1.0.1 -> refused.
    p = lts(["tag-patch", "--repo", r])
    check(p.returncode == 1 and "no commits since v1.0.1" in p.stdout,
          "a second tag without new commits is refused")

    # An off-chain duplicate name is refused by the chain law (the
    # expected next tag is named, which subsumes the duplicate).
    fix2 = commit(r, {"bug.txt": "fixed again\n"},
                  "fix: the wrap again")
    lts(["backport", fix2, "--repo", r])
    p = lts(["tag-patch", "--tag", "v1.0.1", "--repo", r])
    check(p.returncode == 1 and "expected v1.0.2" in p.stdout,
          "a stale tag name is refused by the chain law")

    # The dry run.
    p = lts(["tag-patch", "--repo", r, "--dry-run"])
    check(p.returncode == 0 and "dry run" in p.stdout
          and "v1.0.2" in p.stdout,
          "the dry run predicts v1.0.2 and touches nothing")
    p = lts(["tag-patch", "--repo", r])
    check(p.returncode == 0 and "tagged v1.0.2" in p.stdout,
          "the real second release cuts v1.0.2")


def _section_6():
    print("Section 6 — the audit law")
    r = make_repo("audit-law")
    fix1 = commit(r, {"bug.txt": "one\n"},
                  "fix: the parser crash")
    feat1 = commit(r, {"widget.txt": "new\n"},
                   "feat: the seasonal widget")
    lts(["init", "--tag", "v1.0.0", "--repo", r])
    lts(["backport", fix1, "--repo", r])
    base_sha = branch_head(r, "main~1")

    # A clean branch audits clean.
    p = lts(["audit", "--repo", r])
    check(p.returncode == 0 and "clean" in p.stdout,
          "a branch of backports audits clean")

    # A chore may land directly on the branch.
    git(r, "checkout", "-q", "release/1.0")
    commit(r, {"notes.txt": "release notes\n"}, "chore: 1.0.1 notes")
    git(r, "checkout", "-q", "main")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 0 and "clean" in p.stdout,
          "a chore on the branch is allowed")

    # A rogue direct commit is a violation, named.
    git(r, "checkout", "-q", "release/1.0")
    rogue = commit(r, {"rogue.txt": "sneaky\n"},
                   "feat: sneaky feature")
    git(r, "checkout", "-q", "main")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 1 and rogue[:12] in p.stdout
          and "sneaky feature" in p.stdout and "FEATURE" in p.stdout,
          "a rogue feature commit is a violation, sha and subject "
          "named")

    # A forged trailer naming a FEATURE upstream is a violation: the
    # trailer is checked, not trusted.
    git(r, "checkout", "-q", "release/1.0")
    forged = commit(r, {"fake.txt": "forged\n"},
                    "fix: honest-looking subject\n\nBackported-from: "
                    + feat1)
    git(r, "checkout", "-q", "main")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 1 and forged[:12] in p.stdout
          and "FEATURE" in p.stdout,
          "a forged trailer backporting a FEATURE is a violation")

    # A trailer naming a nonexistent sha is a violation.
    git(r, "checkout", "-q", "release/1.0")
    ghost = commit(r, {"ghost.txt": "boo\n"},
                   "fix: references a ghost\n\nBackported-from: "
                   + "0" * 40)
    git(r, "checkout", "-q", "main")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 1 and ghost[:12] in p.stdout
          and "does not exist" in p.stdout,
          "a trailer naming a nonexistent upstream is a violation")

    # An unclassified direct commit is a violation (fail-closed).
    git(r, "checkout", "-q", "release/1.0")
    sneaky = commit(r, {"sneaky.txt": "no prefix\n"},
                    "sneak it in without a prefix")
    git(r, "checkout", "-q", "main")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 1 and sneaky[:12] in p.stdout
          and "UNKNOWN" in p.stdout,
          "an unclassified direct commit is a violation (fail-closed)")

    # The JSON report carries every violation.
    p = lts(["audit", "--repo", r, "--json"])
    try:
        data = json.loads(p.stdout)
        check(len(data["violations"]) >= 4,
              "audit --json reports every violation")
    except Exception as exc:
        bad("audit --json is not well-formed: %s" % exc)

    # Reset to the clean state; the tag law then judges the chain.
    git(r, "update-ref", "refs/heads/release/1.0",
        branch_head(r, "release/1.0~5"))
    p = lts(["audit", "--repo", r])
    check(p.returncode == 0 and "clean" in p.stdout,
          "rolling the branch back restores a clean audit")

    # A tag gap is a violation.
    git(r, "tag", "v1.0.2", "release/1.0")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 1 and "gap" in p.stdout,
          "a tag-chain gap is a violation, named")
    git(r, "tag", "-d", "v1.0.2")

    # An out-of-line tag (minor bump) is a violation.
    git(r, "tag", "v1.1.0", "release/1.0")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 1 and "beyond the LTS line" in p.stdout,
          "a tag beyond the LTS line is a violation")
    git(r, "tag", "-d", "v1.1.0")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 0, "the audit is green again")


def _section_7():
    print("Section 7 — the release train")
    r = make_repo("train")
    f1 = commit(r, {"bug.txt": "one\n"}, "fix: the wrap on leap days")
    f2 = commit(r, {"bug.txt": "two\n"},
                "fix: the parser timeout on huge inputs")
    feat = commit(r, {"widget.txt": "w\n"}, "feat: confetti mode")

    p = lts(["init", "--tag", "v1.0.0", "--repo", r])
    check(p.returncode == 0, "init from v1.0.0")

    p = lts(["backport", f1, "--repo", r])
    check(p.returncode == 0, "first fix backports")
    p = lts(["tag-patch", "--repo", r])
    check(p.returncode == 0 and "tagged v1.0.1" in p.stdout,
          "v1.0.1 cut")

    p = lts(["backport", f2, "--repo", r])
    check(p.returncode == 0, "second fix backports")
    p = lts(["backport", feat, "--repo", r])
    check(p.returncode == 1 and "FEATURE" in p.stdout,
          "the feature is refused on the way")
    p = lts(["tag-patch", "--repo", r])
    check(p.returncode == 0 and "tagged v1.0.2" in p.stdout,
          "v1.0.2 cut")

    p = lts(["audit", "--repo", r])
    check(p.returncode == 0 and "clean" in p.stdout,
          "the whole train audits clean")

    p = lts(["status", "--repo", r])
    check(p.returncode == 0 and "v1.0.0, v1.0.1, v1.0.2" in p.stdout
          and "commits since base: 2" in p.stdout,
          "status shows the tag chain and the commit count")

    p = lts(["status", "--repo", r, "--json"])
    try:
        data = json.loads(p.stdout)
        check(data["tags"] == ["v1.0.0", "v1.0.1", "v1.0.2"]
              and data["violations"] == [],
              "status --json carries the chain")
    except Exception as exc:
        bad("status --json broken: %s" % exc)

    # The subjects on the branch tell the release story.
    base_sha = branch_head(r, "main~2")
    subs = messages(r, "release/1.0", base_sha)
    check(subs == ["fix: the wrap on leap days",
                   "fix: the parser timeout on huge inputs"],
          "the branch history is exactly the backported fixes")


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
