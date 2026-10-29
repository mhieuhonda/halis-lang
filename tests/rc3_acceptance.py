#!/usr/bin/env python3
"""Stage 148 acceptance gate — v1.0.0-rc.3, the final dry-run.

Run with `make rc3-acceptance` (or `python3 tests/rc3_acceptance.py`).

Six sections over the REAL CLIs and REAL scratch git repositories —
the release day itself is rehearsed end to end, exactly as Stage 150
will run it, on worlds built for the purpose:

  1. the record    — the render fixes this round ships, pinned as
     law             laws: the record's tag line quotes the roadmap
                     row's OWN phrase (rc.1 froze the features, rc.2
                     fixed the bugs, rc.3 dried the run, rc.4 signs
                     off — a record that hardcoded the first
                     candidate's phrase mis-spoke every one after
                     it), the last candidate's successor line speaks
                     the RELEASE cut (the anchor retiring, the tag,
                     the LTS line, the windows becoming the clock —
                     not "cut the same way this one was"), and a
                     roadmap row without a phrase makes no identity
                     claim at all;
  2. the real-tree — the tree this gate runs on is fit to be a
     pre-flight       candidate: readiness green, the gate green
                     over the live range, the markers one voice, the
                     committed record drift-free and speaking the
                     anchor stage's own phrase off the live roadmap;
  3. the eve       — the scratch world walked candidate by candidate
     rehearsal        (rc.1..rc.4, each cut the real way: the row
                     ticked, the markers moved, the old anchor
                     leaving with the release commit, the record
                     rendered and drift-checked) until it stands at
                     the eve of the release, the last anchor naming
                     v1.0.0 as its successor;
  4. the release   — the wrong turns refused first (a bare v1.0.0 is
     day               not a candidate, a pre-release is not an LTS
                     base, a pre-1.0 tag is not a line, a tag that
                     does not exist opens nothing), then the cut
                     itself: the markers move to 1.0.0, the anchor
                     retires with the release commit, the tag is
                     cut, the LTS line opens, the windows become the
                     clock (3 years, 5 years, from the base day) —
                     and the RC kit RETIRES, named: readiness
                     refusing on both the roadmap law and the lts
                     law, the gate and the record refusing on the
                     missing anchor, the line idempotent, a second
                     line refused;
  5. the day       — the maintenance loop the morning after: a fix
     after             lands on main, backports onto the line with
                     its provenance trailer, tag-patch cuts v1.0.1,
                     the audit stays clean — and the gate over the
                     WHOLE road (rc.1 to the release) is green;
  6. the corpus    — tests/ok/feat_stage148_rc3.hls is hlfmt-
     showcase          canonical, hllint-clean, boots through the
                     real checker, runs green on the interpreter
                     and (when bin/hlc exists) matches natively
                     byte for byte.

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
LTS = [os.path.join(ROOT, "bin", "hls-lts")]
BOOT = [sys.executable, os.path.join(ROOT, "tools", "hlinterp", "boot.py")]
FMT = [os.path.join(ROOT, "bin", "hlfmt")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage148_rc3.hls")
SHOWCASE_OUT = ("stage 148: v1.0.0-rc.3\n"
                "  RC3 laws OK: sequence, handoff, retirement, order, "
                "window\n")

GATE = tempfile.mkdtemp(prefix=".rc148-gate-", dir=ROOT)

GIT_ENV = dict(os.environ)
GIT_ENV["GIT_AUTHOR_NAME"] = "rc148-gate"
GIT_ENV["GIT_AUTHOR_EMAIL"] = "rc148-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_NAME"] = "rc148-gate"
GIT_ENV["GIT_COMMITTER_EMAIL"] = "rc148-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_DATE"] = "2026-10-29T09:00:00+00:00"
GIT_ENV["GIT_AUTHOR_DATE"] = "2026-10-29T09:00:00+00:00"

RELEASE_DAY = "2026-10-29"
FREEZE_DIGEST = ("84d1068f80d62f3f741785ce2241b423fa5bd36b4e28e0a"
                 "872c6e772e4decaf6")


def release_state():
    """The candidate line's legal states, read off the real tree:
    'eve' — the anchor stands, no line declared; 'released' — the
    anchor retired with the release commit and the LTS line is
    declared. Anything else is a half-cut release, and a half-cut
    release is not a state. This gate rehearsed the release day;
    after the release it must still run — the rehearsal becomes the
    record.
    """
    anchor = os.path.exists(os.path.join(ROOT, ".hls-rc.json"))
    line = os.path.exists(os.path.join(ROOT, ".hls-lts.json"))
    if anchor and not line:
        return "eve"
    if line and not anchor:
        return "released"
    return "half-cut"

# the candidate line's own phrases, straight off the roadmap's rows
PHRASES = {
    1: "feature freeze",
    2: "bug-fix only",
    3: "final dry-run",
    4: "sign-off",
}

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
                          stderr=subprocess.PIPE, timeout=300,
                          text=True, cwd=cwd, env=env or GIT_ENV)


def rc(*argv, cwd=None):
    return run(RC + list(argv) + ["--root", cwd or ROOT], cwd=cwd)


def lts(*argv, cwd=None):
    return run(LTS + list(argv) + ["--repo", cwd or ROOT], cwd=cwd)


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


def has(repo, rel):
    return os.path.exists(os.path.join(repo, rel))


def commit_all(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


# ----------------------------------------------------------------------
# The scratch worlds.
# ----------------------------------------------------------------------

def generic_roadmap(closed_through, total=150):
    rows = []
    for n in range(1, total + 1):
        st = "✅" if n <= closed_through else "⬜"
        rows.append("| %d | stage %d title | %s | (done in v0.1.0) |"
                    % (n, n, st))
    return ("# ROADMAP\n\n"
            "| # | Stage | Status | Effort |\n"
            "|---|-------|--------|--------|\n"
            + "\n".join(rows) + "\n")


def fancy_roadmap(closed_through, total=150):
    """The roadmap with the REAL tail titles — the rows whose
    parentheticals name each candidate's identity."""
    titles = {
        146: "v1.0 Release Candidate 1 (feature freeze)",
        147: "v1.0 Release Candidate 2 (bug-fix only)",
        148: "v1.0 Release Candidate 3 (final dry-run)",
        149: "v1.0 Release Candidate 4 (sign-off)",
        150: "HLS v1.0 — LTS release",
    }
    rows = []
    for n in range(1, total + 1):
        st = "✅" if n <= closed_through else "⬜"
        title = titles.get(n, "stage %d title" % n)
        rows.append("| %d | %s | %s | (done in v0.1.0) |"
                    % (n, title, st))
    return ("# ROADMAP\n\n"
            "| # | Stage | Status | Effort |\n"
            "|---|-------|--------|--------|\n"
            + "\n".join(rows) + "\n")


def seed_world(name, roadmap_text):
    """A minimal compliant world at Stage 145 closed, freeze anchored."""
    repo = os.path.join(GATE, name)
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    wf(repo, "ROADMAP.md", roadmap_text)
    wf(repo, "CHANGELOG.md",
       "## [v0.165.0-alpha] — the last alpha\n- the body\n")
    wf(repo, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v0.165.0-alpha"}))
    wf(repo, "editors/vscode/halis/package.json",
       json.dumps({"name": "halis", "version": "0.165.0-alpha"}))
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


def walk_candidate(repo, n, roadmap_text_fn):
    """Cut candidate n the real way: the row ticked, the markers
    moved, the old anchor leaving with the release commit, declare,
    the record rendered, the tag cut. Returns the RC.md text."""
    stage = 145 + n
    wf(repo, "ROADMAP.md", roadmap_text_fn(stage))
    wf(repo, "CHANGELOG.md",
       "## [v1.0.0-rc.%d] — Stage %d: the candidate\n- the body\n"
       % (n, stage))
    wf(repo, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v1.0.0-rc.%d" % n}))
    wf(repo, "editors/vscode/halis/package.json",
       json.dumps({"name": "halis", "version": "1.0.0-rc.%d" % n}))
    if has(repo, ".hls-rc.json"):
        git(repo, "rm", "-q", ".hls-rc.json")
    commit_all(repo, "docs: stage %d markers, the candidate %d line"
               % (stage, n))
    r = rc("declare", "--tag", "v1.0.0-rc.%d" % n, cwd=repo)
    if r.returncode != 0:
        bad("candidate rc.%d did not declare: %s"
            % (n, (r.stdout + r.stderr)[:200]))
        return ""
    rc("report", "--out", "RC.md", cwd=repo)
    commit_all(repo, "release: v1.0.0-rc.%d anchored, the record rendered"
               % n)
    git(repo, "tag", "v1.0.0-rc.%d" % n)
    return rf(repo, "RC.md")


def tag_line_of(text):
    for line in text.split("\n"):
        if line.startswith("- the tag: "):
            return line
    return ""


def successor_line_of(text):
    for line in text.split("\n"):
        if line.startswith("- the successor: "):
            return line
    return ""


def header(title):
    print("\n--- " + title + " ---")


def main():
    # ==================================================================
    header("1. the record law — the render speaks the row's own phrase")
    repo = seed_world("the-line", fancy_roadmap(145))

    record = walk_candidate(repo, 1, fancy_roadmap)
    check("(v1.0 Release Candidate 1, feature freeze)" in tag_line_of(record),
          "rc.1's record carries rc.1's own phrase (feature freeze)")
    record = walk_candidate(repo, 2, fancy_roadmap)
    check("(v1.0 Release Candidate 2, bug-fix only)" in tag_line_of(record),
          "rc.2's record carries rc.2's own phrase (bug-fix only)")
    check("feature freeze" not in tag_line_of(record),
          "rc.2's record no longer wears rc.1's phrase")
    record = walk_candidate(repo, 3, fancy_roadmap)
    check("(v1.0 Release Candidate 3, final dry-run)" in tag_line_of(record),
          "rc.3's record carries rc.3's own phrase (final dry-run)")
    check("cut the same way this one was" in successor_line_of(record),
          "rc.3's successor line still describes a candidate cut")
    record = walk_candidate(repo, 4, fancy_roadmap)
    check("(v1.0 Release Candidate 4, sign-off)" in tag_line_of(record),
          "rc.4's record carries rc.4's own phrase (sign-off)")
    succ = successor_line_of(record)
    check("v1.0.0" in succ and "hls-lts init" in succ
          and "the windows becoming the clock" in succ,
          "rc.4's successor line speaks the release cut (the LTS line)")
    check("cut the same way this one was" not in succ,
          "rc.4's successor line no longer claims the release is cut "
          "like a candidate")

    # a roadmap row without a phrase makes no identity claim at all
    plain = seed_world("no-phrase", generic_roadmap(146))
    wf(plain, "CHANGELOG.md",
       "## [v1.0.0-rc.1] — Stage 146: the candidate\n- the body\n")
    wf(plain, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v1.0.0-rc.1"}))
    wf(plain, "editors/vscode/halis/package.json",
       json.dumps({"name": "halis", "version": "1.0.0-rc.1"}))
    commit_all(plain, "docs: stage 146 markers")
    r = rc("declare", "--tag", "v1.0.0-rc.1", cwd=plain)
    check(r.returncode == 0, "the generic world declares rc.1")
    rc("report", "--out", "RC.md", cwd=plain)
    record = rf(plain, "RC.md")
    check(tag_line_of(record).endswith("(v1.0 Release Candidate 1)"),
          "a row without a phrase makes no identity claim")
    check("feature freeze" not in record,
          "the record never speaks a phrase the roadmap did not say")

    # ==================================================================
    header("2. the real-tree pre-flight — the tree may be a candidate")
    r = rc("readiness")
    if release_state() == "released":
        check(r.returncode == 1 and "NOT READY: the roadmap law"
              in r.stdout and "NOT READY: the lts law" in r.stdout,
              "the REAL tree is released (readiness refuses on both "
              "laws, named — the rehearsal's own retirement, come "
              "true)")
        r = rc("gate")
        check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
              "the gate refuses on the missing anchor")
        r = rc("report", "--check")
        check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
              "the record refuses on the missing anchor")
    else:
        check(r.returncode == 0 and "READY" in r.stdout,
              "the REAL tree is ready")
        r = rc("gate")
        check(r.returncode == 0,
              "the gate over the live range names only BUGFIX and CHORE")
        r = rc("report", "--check")
        check(r.returncode == 0,
              "the committed record matches the render (drift refused)")

    head_v = ""
    with open(os.path.join(ROOT, "RELEASES.md")) as f:
        for line in f:
            if line.startswith("## [v"):
                head_v = line[line.find("[v") + 2:line.find("]")]
                break
    man_v = json.load(open(os.path.join(ROOT, "snapshot",
                                       "MANIFEST.json"))).get("version", "")
    vs_v = json.load(open(os.path.join(ROOT, "editors", "vscode",
                                       "halis", "package.json"))).get("version", "")
    check(man_v == "v" + head_v and vs_v == head_v,
          "the three markers speak with one voice (%s)" % head_v)

    # the committed record speaks the anchor stage's own phrase, off
    # the live roadmap (the law stays green for every candidate: each
    # row's phrase is read where the record stands)
    if has(ROOT, ".hls-rc.json"):
        anchor = json.loads(rf(ROOT, ".hls-rc.json"))
        atag = anchor.get("tag", "")
        check(atag in ("v1.0.0-rc.1", "v1.0.0-rc.2", "v1.0.0-rc.3",
                       "v1.0.0-rc.4"),
              "the real tree's anchor is a candidate of the line ("
              + atag + ")")
        stage = anchor.get("stage", 0)
        phrase = ""
        with open(os.path.join(ROOT, "ROADMAP.md")) as f:
            for line in f:
                if not line.startswith("|"):
                    continue
                cells = line.split("|")
                if len(cells) >= 4 and cells[1].strip() == str(stage):
                    title = cells[2].strip()
                    if "(" in title and title.endswith(")"):
                        phrase = title[title.find("(") + 1:-1]
                    break
        committed = rf(ROOT, "RC.md")
        if phrase:
            check(phrase in tag_line_of(committed),
                  "the committed record speaks Stage %d's own phrase "
                  "(%s)" % (stage, phrase))
        else:
            check(", " not in tag_line_of(committed).split(
                "Candidate " + atag[-1])[-1],
                "a row without a phrase makes no identity claim")
    else:
        check(release_state() == "released",
              "no anchor on the tree — and the line is declared (the "
              "release, not a gap)")

    # ==================================================================
    header("3. the eve rehearsal — the last candidate, the real cut")
    anchor = json.loads(rf(repo, ".hls-rc.json"))
    check(anchor.get("tag") == "v1.0.0-rc.4"
          and anchor.get("stage") == 149,
          "the world stands at rc.4, Stage 149 closed")
    check(anchor.get("next") == "v1.0.0",
          "the last anchor names the release as its successor")
    r = rc("readiness", cwd=repo)
    check(r.returncode == 0 and "READY" in r.stdout,
          "the eve world is ready for the release")
    r = rc("report", "--check", cwd=repo)
    check(r.returncode == 0,
          "the rc.4 record is the anchored render (drift refused)")

    # ==================================================================
    header("4. the release day — the wrong turns, then the cut")
    # the wrong turns first: the release cannot be cut wrong
    r = rc("declare", "--tag", "v1.0.0", cwd=repo)
    check(r.returncode == 1 and "the LTS release" in r.stdout + r.stderr,
          "a bare v1.0.0 is refused as a candidate (the rc grammar)")
    r = lts("init", "--tag", "v1.0.0-rc.4", cwd=repo)
    check(r.returncode == 1 and "pre-release" in r.stdout + r.stderr,
          "a pre-release is not an LTS base")
    r = lts("init", "--tag", "v0.166.0", cwd=repo)
    check(r.returncode == 1 and "pre-1.0" in r.stdout + r.stderr,
          "a pre-1.0 tag is not a line")
    r = lts("init", "--tag", "v1.0.0", cwd=repo)
    check(r.returncode == 1 and "does not exist" in r.stdout + r.stderr,
          "the line opens only on a tag that exists (the order law)")

    # the cut: the markers move, the anchor retires with the release
    # commit, the tag is cut, the LTS line opens
    wf(repo, "ROADMAP.md", fancy_roadmap(150))
    wf(repo, "CHANGELOG.md",
       "## [v1.0.0] — Stage 150: the LTS release\n- the body\n")
    wf(repo, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v1.0.0"}))
    wf(repo, "editors/vscode/halis/package.json",
       json.dumps({"name": "halis", "version": "1.0.0"}))
    git(repo, "rm", "-q", ".hls-rc.json")
    commit_all(repo, "docs: stage 150 markers, the release")
    git(repo, "tag", "v1.0.0")

    r = lts("init", "--tag", "v1.0.0", cwd=repo)
    check(r.returncode == 0 and "release/1.0" in r.stdout,
          "the LTS line opens on the v1.0.0 tag (branch release/1.0)")
    commit_all(repo, "release: the LTS line opens")

    # the windows became the clock
    r = lts("status", cwd=repo)
    check(r.returncode == 0
          and "bug fixes until 2029-%s" % RELEASE_DAY[5:] in r.stdout
          and "security until 2031-%s" % RELEASE_DAY[5:] in r.stdout,
          "the clock runs from the base day (3 years, 5 years)")
    check("audit: clean" in r.stdout,
          "the fresh line audits clean")
    r = lts("window", "--tag", "v1.0.0", "--at", RELEASE_DAY, cwd=repo)
    check(r.returncode == 0 and "phase at %s: active" % RELEASE_DAY
          in r.stdout,
          "the release day itself is active (the horizon belongs to "
          "the phase it closes)")

    # the RC kit retires, named — both laws, not one
    r = rc("readiness", cwd=repo)
    check(r.returncode == 1
          and "NOT READY: the roadmap law" in r.stdout
          and "NOT READY: the lts law" in r.stdout,
          "readiness refuses on BOTH laws after the release")
    r = rc("gate", cwd=repo)
    check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
          "the gate refuses on the missing anchor")
    r = rc("report", "--check", cwd=repo)
    check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
          "the record refuses on the missing anchor")

    # the line is idempotent; a second line is a new decision
    r = lts("init", "--tag", "v1.0.0", cwd=repo)
    check(r.returncode == 0 and "idempotent" in r.stdout,
          "declaring the same line again is a no-op, not noise")
    r = lts("init", "--tag", "v1.1.0", cwd=repo)
    check(r.returncode == 1 and "one declaration" in r.stdout + r.stderr,
          "a different base is a new policy decision, not a re-init")

    # ==================================================================
    header("5. the day after — the maintenance loop, the whole road")
    wf(repo, "src/thing.hls", "fn broken() -> int {\n    return 1\n}\n")
    fix_sha = commit_all(repo, "fix: the defect the first morning found")
    r = lts("backport", fix_sha, cwd=repo)
    check(r.returncode == 0 and "applied" in r.stdout
          and "Backported-from" in r.stdout,
          "the fix backports onto the line with its provenance")
    r = lts("tag-patch", cwd=repo)
    check(r.returncode == 0 and "v1.0.1" in r.stdout,
          "tag-patch cuts v1.0.1 from the line")
    r = lts("status", cwd=repo)
    check(r.returncode == 0 and "audit: clean" in r.stdout
          and "v1.0.1" in r.stdout,
          "the maintained line audits clean, v1.0.1 on the branch")

    # the whole road, judged at the release: every commit from the
    # first candidate's tag to the release inside the freeze
    git(repo, "checkout", "-q", "main")
    r = rc("gate", "--range", "v1.0.0-rc.1..HEAD", cwd=repo)
    check(r.returncode == 0,
          "the gate over the whole road (rc.1 to the release) is green")

    # ==================================================================
    header("6. the corpus showcase — the dry-run laws in pure HLS")
    fmt_out = run(FMT + [SHOWCASE])
    with open(SHOWCASE) as f:
        src = f.read()
    check(fmt_out.returncode == 0 and fmt_out.stdout == src,
          "the showcase is hlfmt-canonical")
    r = run(LINT + [SHOWCASE])
    check(r.returncode == 0 and "no warnings" in r.stdout,
          "the showcase is hllint-clean")
    r = run(BOOT + ["--check", SHOWCASE])
    check(r.returncode == 0, "the showcase passes the real checker")
    r = run(BOOT + [SHOWCASE])
    check(r.returncode == 0 and r.stdout == SHOWCASE_OUT,
          "the showcase runs green on the interpreter")
    if NATIVE:
        out_c = os.path.join(GATE, "showcase.c")
        r = run([HLC, SHOWCASE, out_c])
        if r.returncode != 0:
            bad("the showcase does not compile natively: %s"
                % (r.stdout + r.stderr)[:160])
        else:
            binp = os.path.join(GATE, "showcase")
            r = run([CC, "-O2", "-o", binp, out_c, "-lm", "-pthread"])
            if r.returncode != 0:
                bad("the showcase binary does not link")
            else:
                r = run([binp])
                check(r.returncode == 0 and r.stdout == SHOWCASE_OUT,
                      "the showcase matches natively, byte for byte")

    print("\n==========================================")
    print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
    print("==========================================")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(GATE, ignore_errors=True)
