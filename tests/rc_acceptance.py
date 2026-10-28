#!/usr/bin/env python3
"""Stage 146 acceptance gate — the release candidate, the feature
freeze declared (v1.0.0-rc.1).

Run with `make rc-acceptance` (or `python3 tests/rc_acceptance.py`).

Nine sections over the REAL rc CLI (subprocess, the interface a
release manager drives) and REAL scratch git repositories — a
candidate cannot be rehearsed on the tree it will one day bless:

  1. the convention  — the usage law (no command, unknown command,
                       unknown flag: exit 2), the readiness --json
                       shape (ready, head, first_open, laws)

  2. the readiness   — the checklist over the REAL tree (green) and
     law               scratch trees that fail one law at a time: a
                       feature stage still open, a missing freeze
                       document, a freeze that hls-freeze did not
                       write, a frozen commit outside the history,
                       the markers disagreeing

  3. the tag law     — the candidate grammar refused shape by shape:
                       v2.0.0-rc.1 (another line), v1.0.0 (the LTS
                       release), v1.0.0-alpha (a retired channel),
                       v1.0.0-rc.0 / rc.02 / rc.x (not a candidate
                       number), v1.0.0-rc.5 (the roadmap planned
                       four), build metadata (names a build, not a
                       candidate)

  4. the declare     — the candidate cut ON PAPER over a scratch
     law               release: refused while the roadmap's own row
                       is unticked, the anchor written with its
                       tag/commit/day/freeze digest once the release
                       carries it, the re-declare refused as noise,
                       a second anchor refused, the dirty tree
                       refused, the missing --tag a usage error

  5. the gate law    — the feature freeze, commit by commit over
                       REAL history: fixes and chores land, a feat
                       is named sha by sha, an unprefixed commit is
                       refused fail-closed, a "fix" that adds std/
                       surface is re-classed into the FEATURE it
                       really is (the diff gate), --range judges an
                       arbitrary range, gate without an anchor names
                       the declare command

  6. the report law  — the record rendered deterministic (two
                       renders byte-identical), --check green on the
                       committed copy, drift refused with the
                       regenerate command named, report before any
                       declaration refused

  7. the sort law    — the candidate is the newest release the tree
                       has ever named: an older or equal tag
                       refused, judged over REAL git tags

  8. the LTS         — the candidate opens no support window: the
     handoff            anchor (when declared on this tree) stands
                       beside NO .hls-lts.json, LTS.md stays the
                       promise, and the real-tree publication (when
                       present) matches its render

  9. the corpus      — tests/ok/feat_stage146_rc1.hls is hlfmt-
     showcase          canonical, hllint-clean, boots through the
                       real checker, runs green on the interpreter
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

RC = [os.path.join(ROOT, "bin", "hls-rc")]
BOOT = [sys.executable, os.path.join(ROOT, "tools", "hlinterp", "boot.py")]
FMT = [os.path.join(ROOT, "bin", "hlfmt")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage146_rc1.hls")
SHOWCASE_OUT = ("stage 146: v1.0.0-rc.1\n"
                "  RC1 laws OK: grammar, stages, precedence, "
                "admission, readiness\n")

GATE = tempfile.mkdtemp(prefix=".rc146-gate-", dir=ROOT)

GIT_ENV = dict(os.environ)
GIT_ENV["GIT_AUTHOR_NAME"] = "rc146-gate"
GIT_ENV["GIT_AUTHOR_EMAIL"] = "rc146-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_NAME"] = "rc146-gate"
GIT_ENV["GIT_COMMITTER_EMAIL"] = "rc146-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_DATE"] = "2026-10-28T09:00:00+00:00"
GIT_ENV["GIT_AUTHOR_DATE"] = "2026-10-28T09:00:00+00:00"

FREEZE_DIGEST = ("84d1068f80d62f3f741785ce2241b423fa5bd36b4e28e0a"
                 "872c6e772e4decaf6")

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


def run(argv, cwd=None, env=None):
    return subprocess.run(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=300,
                          text=True, cwd=cwd, env=env or GIT_ENV)


def rc(*argv, cwd=None):
    return run(RC + list(argv), cwd=cwd)


def git(repo, *args):
    return run(["git", "-C", repo] + list(args))


def wf(repo, rel, content):
    path = os.path.join(repo, rel)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def rf(repo, rel):
    with open(os.path.join(repo, rel)) as f:
        return f.read()


def roadmap(closed_through, total=150, reopen=None):
    """The stage table, live-shaped: closed_through rows ticked, the
    rest open; `reopen` marks one closed row open again (a hole)."""
    rows = []
    for n in range(1, total + 1):
        if n <= closed_through:
            st = "✅"
        else:
            st = "⬜"
        if reopen is not None and n == reopen:
            st = "⬜"
        rows.append("| %d | stage %d title | %s | (done in v0.1.0) |"
                    % (n, n, st))
    return ("# ROADMAP\n\n"
            "| # | Stage | Status | Effort |\n"
            "|---|-------|--------|--------|\n"
            + "\n".join(rows) + "\n")


def scratch_world(name, closed_through=145):
    """A minimal compliant world at changelog v1.0.0-rc.1, stages
    closed through `closed_through`. Returns the repo path."""
    repo = os.path.join(GATE, name)
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    wf(repo, "ROADMAP.md", roadmap(closed_through))
    wf(repo, "CHANGELOG.md",
       "## [v1.0.0-rc.1] — Stage 146: the candidate\n- the body\n")
    wf(repo, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v1.0.0-rc.1"}))
    wf(repo, "editors/vscode/halis/package.json",
       json.dumps({"name": "halis", "version": "1.0.0-rc.1"}))
    wf(repo, "LTS.md", "the promise\n")
    wf(repo, "snapshot/hlc.c", "int main(void){return 0;}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    freeze = {
        "digest": FREEZE_DIGEST,
        "frozen_commit": git(repo, "rev-parse", "HEAD").stdout.strip(),
        "release": "0.159.0-alpha",
        "stage": 141,
        "surface": {"syntax": {"keywords": ["fn"]}},
        "surface_version": 1,
        "tool": "hls-freeze",
    }
    wf(repo, "tests/api_freeze.json", json.dumps(freeze))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "the freeze document")
    return repo


def commit_all(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def header(title):
    print("\n--- " + title + " ---")


def main():
    # ==================================================================
    header("1. the convention — the usage law, the --json shape")
    r = rc()
    check(r.returncode == 2, "no command is a usage error (exit 2)")
    r = rc("frobnicate")
    check(r.returncode == 2, "an unknown command is a usage error")
    r = rc("readiness", "--frobnicate")
    check(r.returncode == 2, "an unknown flag is a usage error")
    r = rc("declare")
    check(r.returncode == 2, "declare without --tag is a usage error")
    r = rc("gate", "--range", "norange")
    check(r.returncode == 2 and "A..B" in r.stdout,
          "a malformed range is a usage error naming the grammar")

    r = rc("readiness", "--json")
    check(r.returncode == 0, "readiness --json on the real tree is green")
    try:
        data = json.loads(r.stdout)
        check(data.get("ready") is True, "the verdict is ready:true")
        check("head" in data and "first_open" in data,
              "the verdict names the head version and first open stage")
        check(isinstance(data.get("laws"), list) and len(data["laws"]) >= 6,
              "the verdict carries the named laws (%d)"
              % len(data.get("laws", [])))
    except Exception as exc:
        bad("readiness --json does not parse: %s" % exc)

    # ==================================================================
    header("2. the readiness law — the checklist, one law at a time")
    r = rc("readiness")
    check(r.returncode == 0 and "READY" in r.stdout,
          "the REAL tree is ready (145 closed, the tail from 146)")

    repo = scratch_world("ready-ok")
    r = rc("readiness", "--root", repo)
    check(r.returncode == 0, "a compliant scratch world is ready")

    repo = scratch_world("ready-feature-open", closed_through=144)
    r = rc("readiness", "--root", repo)
    check(r.returncode == 1 and "stage 145" in r.stdout,
          "a feature stage still open contradicts a candidate (named)")

    repo = scratch_world("ready-no-freeze")
    os.remove(os.path.join(repo, "tests", "api_freeze.json"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "the freeze leaves")
    r = rc("readiness", "--root", repo)
    check(r.returncode == 1 and "api_freeze.json" in r.stdout,
          "a missing freeze document is named")

    repo = scratch_world("ready-foreign-freeze")
    freeze = json.loads(rf(repo, "tests/api_freeze.json"))
    freeze["tool"] = "someone-elses-tool"
    wf(repo, "tests/api_freeze.json", json.dumps(freeze))
    commit_all(repo, "docs: whose surface is this")
    r = rc("readiness", "--root", repo)
    check(r.returncode == 1 and "hls-freeze" in r.stdout,
          "a freeze hls-freeze did not write is refused")

    repo = scratch_world("ready-unanchored-freeze")
    freeze = json.loads(rf(repo, "tests/api_freeze.json"))
    freeze["frozen_commit"] = "f" * 40
    wf(repo, "tests/api_freeze.json", json.dumps(freeze))
    commit_all(repo, "docs: the freeze unanchored")
    r = rc("readiness", "--root", repo)
    check(r.returncode == 1 and "ancestor" in r.stdout,
          "a frozen commit outside this history is refused")

    repo = scratch_world("ready-markers")
    wf(repo, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v0.9.0"}))
    commit_all(repo, "chore: the manifest speaks alone")
    r = rc("readiness", "--root", repo)
    check(r.returncode == 1 and "disagree" in r.stdout
          and "snapshot-regen" in r.stdout,
          "disagreeing markers are named with the regenerate command")

    # ==================================================================
    header("3. the tag law — the candidate grammar, shape by shape")
    for tag, why in [
        ("v2.0.0-rc.1", "another line"),
        ("v1.0.0", "the LTS release"),
        ("v1.0.0-alpha", "a retired channel"),
        ("v1.0.0-beta.1", "a retired channel"),
        ("v1.0.0-rc.0", "the numbering starts at 1"),
        ("v1.0.0-rc.02", "a leading zero"),
        ("v1.0.0-rc.x", "not a number"),
        ("v1.0.0-rc.5", "the roadmap planned four"),
        ("v1.0.0-rc.1+b7", "build metadata"),
        ("not-a-version", "the grammar"),
    ]:
        repo = scratch_world("tag-" + tag.replace(".", "-")
                             .replace("+", "-") + "-t", closed_through=146)
        r = rc("declare", "--tag", tag, "--root", repo)
        check(r.returncode == 1, tag + " refused (" + why + ")")

    # ==================================================================
    header("4. the declare law — the candidate cut on paper")
    repo = scratch_world("declare-stage", closed_through=145)
    r = rc("declare", "--tag", "v1.0.0-rc.1", "--root", repo)
    check(r.returncode == 1 and "Stage 146" in r.stdout,
          "refused while the roadmap's own row is unticked")

    wf(repo, "ROADMAP.md", roadmap(146))
    commit_all(repo, "docs: stage 146 changelog + roadmap")
    r = rc("declare", "--tag", "v1.0.0-rc.1", "--root", repo)
    check(r.returncode == 0 and "anchored" in r.stdout,
          "the release that carries the candidate declares it")
    anchor = json.loads(rf(repo, ".hls-rc.json"))
    check(anchor.get("kit") == "halis-rc-1", "the anchor names its kit")
    check(anchor.get("tag") == "v1.0.0-rc.1", "the anchor names the tag")
    check(len(anchor.get("sha", "")) == 40, "the anchor pins a commit")
    check(anchor.get("stage") == 146, "the anchor names the stage")
    check(anchor.get("day") == "2026-10-28",
          "the anchor's day is the committer day")
    check(anchor.get("freeze_digest") == FREEZE_DIGEST,
          "the anchor carries the freeze digest")
    check(anchor.get("next") == "v1.0.0-rc.2",
          "the anchor names the successor candidate")

    r = rc("declare", "--tag", "v1.0.0-rc.1", "--root", repo)
    check(r.returncode == 1 and "noise" in r.stdout,
          "a re-declaration is refused as noise")

    repo2 = scratch_world("declare-second", closed_through=146)
    r = rc("declare", "--tag", "v1.0.0-rc.1", "--root", repo2)
    check(r.returncode == 0, "the second world declares clean")
    r = rc("declare", "--tag", "v1.0.0-rc.2", "--root", repo2)
    check(r.returncode == 1 and "one anchor per tree" in r.stdout,
          "a second anchor is refused (one candidate per tree)")

    repo3 = scratch_world("declare-dirty", closed_through=146)
    wf(repo3, "snapshot/hlc.c", "int main(void){return 1;}\n")
    r = rc("declare", "--tag", "v1.0.0-rc.1", "--root", repo3)
    check(r.returncode == 1 and "dirty" in r.stdout,
          "a dirty tree is refused (the anchor pins a commit)")

    # ==================================================================
    header("5. the gate law — the freeze, commit by commit")
    repo = scratch_world("gate-noanchor")
    r = rc("gate", "--root", repo)
    check(r.returncode == 1 and "declare" in r.stdout,
          "gate without an anchor names the declare command")

    repo = scratch_world("gate-live", closed_through=146)
    r = rc("declare", "--tag", "v1.0.0-rc.1", "--root", repo)
    check(r.returncode == 0, "the candidate declared for the gate")
    r = rc("gate", "--root", repo)
    check(r.returncode == 0 and "0 commit(s)" in r.stdout,
          "an empty range is green")

    wf(repo, "notes.md", "one\n")
    commit_all(repo, "fix: the proc table reaps spent slots")
    r = rc("gate", "--root", repo)
    check(r.returncode == 0 and "BUGFIX 1" in r.stdout,
          "a fix lands")

    wf(repo, "notes.md", "one\ntwo\n")
    commit_all(repo, "docs: the record moves")
    r = rc("gate", "--root", repo)
    if r.returncode == 0 and "CHORE 1" in r.stdout:
        ok("a chore lands")
    else:
        bad("a chore lands — the gate said: "
            + r.stdout[-300:].strip())

    wf(repo, "notes.md", "one\ntwo\nthree\n")
    commit_all(repo, "feat: a surface nobody froze")
    r = rc("gate", "--root", repo)
    if r.returncode == 1 and "VIOLATION" in r.stdout \
            and "FEATURE" in r.stdout:
        ok("a feature is named sha by sha")
    else:
        bad("a feature is named sha by sha — the gate said: "
            + r.stdout[-300:].strip())

    wf(repo, "notes.md", "one\ntwo\nthree\nfour\n")
    commit_all(repo, "just a thing")
    r = rc("gate", "--root", repo)
    if r.returncode == 1 and "UNKNOWN" in r.stdout:
        ok("an unprefixed commit is refused fail-closed")
    else:
        bad("an unprefixed commit is refused fail-closed — the gate "
            + "said: " + r.stdout[-300:].strip())

    # the diff gate: a fix that grows the surface is a feature
    wf(repo, "std/new_surface.hls", "fn list_new_int() -> int {\n    return 0\n}\n")
    commit_all(repo, "fix: patch the allocator")
    r = rc("gate", "--root", repo)
    check(r.returncode == 1
          and "new API surface is a feature" in r.stdout,
          "a fix that adds std/ surface is re-classed FEATURE")

    # --range judges an arbitrary range: only the fix commit inside
    r = rc("gate", "--range", "HEAD~4..HEAD~3", "--root", repo)
    if r.returncode == 0 and "1 commit(s)" in r.stdout:
        ok("--range judges the range it is given")
    else:
        bad("--range judges the range it is given — the gate said: "
            + r.stdout[-300:].strip())

    # ==================================================================
    header("6. the report law — the record rendered and defended")
    repo = scratch_world("report-live", closed_through=146)
    r = rc("report", "--out", "RC.md", "--root", repo)
    check(r.returncode == 1 and "declare" in r.stdout,
          "report before any declaration names the declare command")
    r = rc("declare", "--tag", "v1.0.0-rc.1", "--root", repo)
    check(r.returncode == 0, "declared for the report")

    out_a = os.path.join(GATE, "report-a.md")
    out_b = os.path.join(GATE, "report-b.md")
    ra = rc("report", "--out", out_a, "--root", repo)
    rb = rc("report", "--out", out_b, "--root", repo)
    check(ra.returncode == 0 and rb.returncode == 0,
          "two renders both succeed")
    check(open(out_a).read() == open(out_b).read(),
          "the render is deterministic (two runs byte-identical)")

    r = rc("report", "--out", "RC.md", "--root", repo)
    r2 = rc("report", "--check", "--root", repo)
    check(r.returncode == 0 and r2.returncode == 0
          and "matches the render" in r2.stdout,
          "--check is green on the committed copy")

    wf(repo, "RC.md", rf(repo, "RC.md") + "hand edit\n")
    r = rc("report", "--check", "--root", repo)
    check(r.returncode == 1 and "regenerate" in r.stdout,
          "a drifted record is refused with the regenerate command")

    # ==================================================================
    header("7. the sort law — the newest release the tree ever named")
    repo = scratch_world("sort-law", closed_through=146)
    git(repo, "tag", "v1.0.0-rc.1")
    r = rc("declare", "--tag", "v1.0.0-rc.1", "--root", repo)
    check(r.returncode == 1 and "sort after" in r.stdout,
          "an equal tag is refused")
    r = rc("declare", "--tag", "v0.166.0-alpha", "--root", repo)
    check(r.returncode == 1,
          "an older line is refused by the grammar before the sort")
    git(repo, "tag", "v0.166.0-alpha")
    r = rc("declare", "--tag", "v1.0.0-rc.1", "--root", repo)
    check(r.returncode == 1 and "sort after" in r.stdout,
          "the candidate must outrank every tag that exists")

    # ==================================================================
    header("8. the LTS handoff — the candidate opens no window")
    check(not os.path.exists(os.path.join(ROOT, ".hls-lts.json")),
          "no LTS line is declared on the real tree")
    check(os.path.isfile(os.path.join(ROOT, "LTS.md")),
          "LTS.md stays the promise")
    lts = open(os.path.join(ROOT, "LTS.md")).read()
    check("hls-lts init --tag v1.0.0" in lts,
          "the promise names the day it becomes the clock")
    anchor_path = os.path.join(ROOT, ".hls-rc.json")
    if os.path.exists(anchor_path):
        anchor = json.load(open(anchor_path))
        # RC2 fix: the anchor is whatever candidate the tree has
        # legitimately reached — the kit's own successor rule moves it
        # (rc.1 declares rc.2, and so on), so pinning THIS gate to
        # rc.1 made every later candidate fail the gate that certified
        # the line. The law is the SHAPE: the anchor carries a
        # candidate tag of the v1.0.0 line, rc.1..rc.4.
        atag = anchor.get("tag", "")
        check(atag in ("v1.0.0-rc.1", "v1.0.0-rc.2", "v1.0.0-rc.3",
                       "v1.0.0-rc.4"),
              "the real tree's anchor is a candidate of the line ("
              + atag + ")")
        r = rc("report", "--check")
        check(r.returncode == 0,
              "the committed RC.md matches its render")
        r = rc("gate")
        check(r.returncode == 0,
              "the real tree's range since the anchor is inside the freeze")
    else:
        r = rc("readiness")
        check(r.returncode == 0,
              "the anchor is not cut yet; readiness still green")

    # ==================================================================
    header("9. the corpus showcase — the laws as a pure-HLS program")
    check(os.path.isfile(SHOWCASE), SHOWCASE + " exists")
    r = run(FMT + [SHOWCASE])
    check(r.stdout == open(SHOWCASE).read(),
          "hlfmt-canonical (the formatter would not change a byte)")
    r = run(LINT + [SHOWCASE])
    check(r.returncode == 0 and "no warnings" in r.stdout,
          "hllint-clean")
    r = run(BOOT + [SHOWCASE])
    check(r.returncode == 0 and r.stdout == SHOWCASE_OUT,
          "boots green on the interpreter with the pinned output")
    if NATIVE:
        cpath = os.path.join(GATE, "rc1_corpus.c")
        bpath = os.path.join(GATE, "rc1_corpus")
        r = run([HLC, SHOWCASE, cpath])
        check(r.returncode == 0, "bin/hlc compiles the showcase")
        r = run([CC, "-O2", "-o", bpath, cpath, "-lm", "-pthread"])
        check(r.returncode == 0, "cc links the showcase")
        r = run([bpath])
        check(r.returncode == 0 and r.stdout == SHOWCASE_OUT,
              "the native binary matches the interpreter byte for byte")
    else:
        print("  skip: native differential (bin/hlc or cc missing)")

    # ==================================================================
    print("\n=== %d passed, %d failed ===" % (PASS, FAIL))
    shutil.rmtree(GATE, ignore_errors=True)
    if FAIL > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
