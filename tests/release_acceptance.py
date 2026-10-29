#!/usr/bin/env python3
"""Stage 150 acceptance gate — v1.0.0, the LTS release.

Run with `make release-acceptance` (or
`python3 tests/release_acceptance.py`).

Six sections over the REAL tree and a REAL scratch git world — the
release judged where it lands, at both legal states of the line:

  1. the state     — the tree this gate runs on is a legal state of
     law              the candidate line: the eve (the rc.4 anchor
                      stands, no line declared) or the release (the
                      anchor retired with the release commit, the LTS
                      line declared). A half-cut release — an anchor
                      gone without a line, or a line under a live
                      anchor — is not a state, and the gate names it.
                      The roadmap parses to exactly 150 rows, numbered
                      1..150, the closed prefix contiguous; the three
                      version markers speak with one voice;
  2. the retirement — the RC kit answers as its state demands: on the
     law               eve, readiness/gate/record/signoff all green;
                      after the release, every one of them refuses,
                      NAMED — readiness on both the roadmap law and
                      the lts law, gate, record and witness on the
                      missing anchor. The kit that certified the road
                      does not pretend (0) and does not crash (101)
                      when its office closes;
  3. the line       — the clock the release opens, on the real tree:
     law               `.hls-lts.json` declares branch release/1.0
                      from the v1.0.0 tag, the branch stands where
                      the tag stands, `hls-lts status` reads the
                      windows off the base day (3 years of bug fixes,
                      5 of security), the release day itself is
                      active, the line audits clean, the SAME
                      declaration again is an idempotent no-op, a
                      different base is a new policy decision —
                      refused. On the eve, the section asserts the
                      preconditions instead: no line, the promise
                      published, and LTS.md still matching its render;
  4. the wrong      — on a scratch world cut the release the real
     turns, the cut    way: the refusals first (a bare v1.0.0 is not
                       a candidate, a pre-release is not a base, a
                       pre-1.0 tag is not a line, a tag that does not
                       exist opens nothing), then the choreography —
                       the markers move, the anchor retires with the
                       release commit, the tag is cut, the line opens,
                       the windows become the clock, the kit retires
                       on both laws, the line idempotent, a second
                       line refused;
  5. the docs law   — the release speaks: the release record's
     law               head section is the v1.0.0 entry naming the
                       LTS release, the journey docs retired with
                       the release (the roadmap and the changelog
                       became RELEASES.md), the README carries the
                       gate and the clock, and LTS.md still matches
                       its render — the promise and the clock are
                       the same numbers;
  6. the corpus     — tests/ok/feat_stage150_release.hls is hlfmt-
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

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage150_release.hls")
SHOWCASE_OUT = ("stage 150: v1.0.0\n"
                "  RELEASE laws OK: order, handoff, retirement, window, "
                "marker, line\n")

GATE = tempfile.mkdtemp(prefix=".rc150-gate-", dir=ROOT)

GIT_ENV = dict(os.environ)
GIT_ENV["GIT_AUTHOR_NAME"] = "rc150-gate"
GIT_ENV["GIT_AUTHOR_EMAIL"] = "rc150-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_NAME"] = "rc150-gate"
GIT_ENV["GIT_COMMITTER_EMAIL"] = "rc150-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_DATE"] = "2026-10-29T11:00:00+00:00"
GIT_ENV["GIT_AUTHOR_DATE"] = "2026-10-29T11:00:00+00:00"

SCRATCH_DAY = "2026-10-29"
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


def release_state():
    """The candidate line's legal states, read off the real tree:
    'eve' — the anchor stands, no line declared (Stage 150 has not
    run); 'released' — the anchor retired with the release commit
    and the LTS line is declared. Anything else is a half-cut
    release, and a half-cut release is not a state.
    """
    anchor = os.path.exists(os.path.join(ROOT, ".hls-rc.json"))
    line = os.path.exists(os.path.join(ROOT, ".hls-lts.json"))
    if anchor and not line:
        return "eve"
    if line and not anchor:
        return "released"
    return "half-cut"


def add_years(day, n):
    """base + N years — an anniversary, not a count of days:
    February 29 clamps to the 28th when the target year is not leap
    (the calendar law the tool enforces, re-derived here)."""
    y, m, d = day.split("-")
    y, m, d = int(y) + n, int(m), int(d)
    leap = y % 400 == 0 or (y % 100 != 0 and y % 4 == 0)
    if m == 2 and d == 29 and not leap:
        d = 28
    return "%04d-%02d-%02d" % (y, m, d)


def head_version():
    """The head release of the tree's record — RELEASES.md since
    v1.0.0 retired the changelog; the old CHANGELOG.md remains a
    fallback so an eve-shaped tree still reads."""
    for doc in ("RELEASES.md", "CHANGELOG.md"):
        path = os.path.join(ROOT, doc)
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                if line.startswith("## [v"):
                    return line[line.find("[v") + 2:line.find("]")]
    return ""


def roadmap_rows():
    """The stage table, parsed the way the kit reads it: any row
    whose first cell parses as a stage number (1..3 digits) is a
    stage row; a narrow row (a cancelled stage — row 122 carries no
    status cells at all) is neither ticked nor open."""
    rows = []
    with open(os.path.join(ROOT, "ROADMAP.md")) as f:
        for line in f:
            if not line.startswith("|"):
                continue
            cells = line.split("|")
            if len(cells) < 3:
                continue
            num_s = cells[1].strip()
            if not (num_s.isdigit() and 0 < len(num_s) <= 3):
                continue
            wide = len(cells) >= 5
            status = cells[3].strip() if wide else ""
            rows.append({
                "num": int(num_s),
                "title": cells[2].strip(),
                "status": status,
                "effort": cells[4].strip() if wide else "",
                "open": "⬜" in status or "🔄" in status,
            })
    return rows


def seed_world(name):
    """A minimal compliant world at Stage 149 closed — the eve of the
    release, built to cut it the real way."""
    repo = os.path.join(GATE, name)
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    titles = {
        146: "v1.0 Release Candidate 1 (feature freeze)",
        147: "v1.0 Release Candidate 2 (bug-fix only)",
        148: "v1.0 Release Candidate 3 (final dry-run)",
        149: "v1.0 Release Candidate 4 (sign-off)",
        150: "HLS v1.0 — LTS release",
    }
    rows = []
    for n in range(1, 151):
        st = "✅" if n <= 149 else "⬜"
        title = titles.get(n, "stage %d title" % n)
        rows.append("| %d | %s | %s | (done in v0.1.0) |" % (n, title, st))
    wf(repo, "ROADMAP.md",
       "# ROADMAP\n\n"
       "| # | Stage | Status | Effort |\n"
       "|---|-------|--------|--------|\n" + "\n".join(rows) + "\n")
    wf(repo, "CHANGELOG.md",
       "## [v1.0.0-rc.4] — Stage 149: the sign-off\n- the body\n")
    wf(repo, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v1.0.0-rc.4"}))
    wf(repo, "editors/vscode/halis/package.json",
       json.dumps({"name": "halis", "version": "1.0.0-rc.4"}))
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


def header(title):
    print("\n--- " + title + " ---")


def main():
    state = release_state()

    # ==================================================================
    header("1. the state law — this tree, a legal state of the line")
    if state == "half-cut":
        bad("the tree is a HALF-CUT release (anchor and line "
            "disagree) — not a state; restore one or the other")
    else:
        check(state in ("eve", "released"),
              "the tree is a legal state of the line (%s)" % state)
        check(has(ROOT, "RC.md"),
              "the candidate's record still travels (the last render "
              "of the line)")
        check(not has(ROOT, ".hls-lts.json") or state == "released",
              "the line declaration only exists after the release")
        # the road retired with the release: the roadmap and the
        # changelog left the tree, and RELEASES.md carries the record.
        # On an eve-shaped tree the old documents still read.
        if state == "released":
            check(not has(ROOT, "ROADMAP.md"),
                  "the roadmap retired with the release (the road is "
                  "kept, the diary is not)")
            check(not has(ROOT, "CHANGELOG.md"),
                  "the changelog retired with the release (the record "
                  "lives in RELEASES.md)")
            check(has(ROOT, "RELEASES.md"),
                  "the release record travels (RELEASES.md)")
        if has(ROOT, "ROADMAP.md"):
            # the stage table, parsed the way the kit reads it — 150
            # rows numbered 1..150, the closed prefix contiguous
            rows = roadmap_rows()
            check(len(rows) == 150,
                  "the roadmap parses to exactly 150 stage rows (%d)"
                  % len(rows))
            nums = [r["num"] for r in rows]
            check(nums == list(range(1, 151)),
                  "the rows are numbered 1..150 in order")
            closed = [r for r in rows if not r["open"]]
            open_rows = [r for r in rows if r["open"]]
            if state == "eve":
                check(len(open_rows) == 1
                      and open_rows[0]["num"] == 150,
                      "the eve roadmap: 149 closed, the tail is Stage 150")
                check(len(closed) == 149,
                      "149 stages closed on the eve (%d)" % len(closed))
            else:
                check(len(open_rows) == 0,
                      "the released roadmap: every stage ticked (nothing "
                      "left to candidate for)")
                check(len(closed) == 150,
                      "150 stages closed (%d)" % len(closed))
                row150 = rows[149]
                check("LTS release" in row150["title"],
                      "Stage 150's row keeps its own phrase (%s)"
                      % row150["title"])
                check("v1.0.0" in row150["effort"],
                      "Stage 150's row names its release (%s)"
                      % row150["effort"])
        # the three markers speak with one voice
        head_v = head_version()
        man_v = json.load(open(os.path.join(
            ROOT, "snapshot", "MANIFEST.json"))).get("version", "")
        vs_v = json.load(open(os.path.join(
            ROOT, "editors", "vscode", "halis",
            "package.json"))).get("version", "")
        check(man_v == "v" + head_v and vs_v == head_v,
              "the three markers speak with one voice (%s)" % head_v)
        if state == "eve":
            check(head_v == "1.0.0-rc.4",
                  "on the eve the voice is still the candidate's "
                  "(%s)" % head_v)
        else:
            check(head_v == "1.0.0",
                  "the release's voice is v1.0.0 (%s)" % head_v)

    # ==================================================================
    header("2. the retirement law — the kit answers as its state "
           "demands")
    if state == "eve":
        r = rc("readiness")
        check(r.returncode == 0 and "READY" in r.stdout,
              "the eve tree is ready (the last candidate stands)")
        r = rc("gate")
        check(r.returncode == 0,
              "the gate over the live range names only BUGFIX and "
              "CHORE")
        r = rc("report", "--check")
        check(r.returncode == 0,
              "the committed record matches the render (drift "
              "refused)")
        r = rc("signoff", "--check")
        check(r.returncode == 0 and "signed off by" in r.stdout,
              "the signature is re-derived green on the eve")
    elif state == "released":
        r = rc("readiness")
        check(r.returncode == 1 and "NOT READY: the roadmap law"
              in r.stdout and "NOT READY: the lts law" in r.stdout,
              "readiness refuses on BOTH laws, named (nothing left "
              "to candidate for; a line is declared)")
        r = rc("gate")
        check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
              "the gate refuses on the missing anchor")
        r = rc("report", "--check")
        check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
              "the record refuses on the missing anchor")
        r = rc("signoff", "--check")
        check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
              "the witness refuses on the missing anchor (the office "
              "closed with the release commit)")

    # ==================================================================
    header("3. the line law — the clock on the real tree")
    r = lts("policy", "--check")
    check(r.returncode == 0,
          "LTS.md still matches its render (the promise and the "
          "clock are the same numbers)")
    if state == "eve":
        check(not has(ROOT, ".hls-lts.json"),
              "no line is declared on the eve (a candidate opens no "
              "window)")
        with open(os.path.join(ROOT, "LTS.md")) as f:
            promise = f.read()
        check("hls-lts init --tag v1.0.0" in promise,
              "the promise names the day it becomes the clock")
    elif state == "released":
        check(has(ROOT, ".hls-lts.json"),
              "the line is declared (the promise became the clock)")
        decl = json.loads(rf(ROOT, ".hls-lts.json"))
        check(decl.get("base_tag") == "v1.0.0",
              "the line's base is the release tag (%s)"
              % decl.get("base_tag"))
        check(decl.get("branch") == "release/1.0",
              "the line's branch is release/1.0 (%s)"
              % decl.get("branch"))
        check(decl.get("policy") == "halis-lts-1",
              "the declaration carries the policy's name (%s)"
              % decl.get("policy"))
        tag_sha = git(ROOT, "rev-parse",
                      "v1.0.0^{commit}").stdout.strip()
        check(decl.get("base_sha") == tag_sha and tag_sha != "",
              "the base sha is the commit the tag names")
        branch_sha = git(ROOT, "rev-parse",
                         "release/1.0^{commit}").stdout.strip()
        check(branch_sha == tag_sha,
              "the branch stands where the release stands")
        check(decl.get("created", "") != "",
              "the declaration records the day it opened")
        # the clock: windows read off the base commit's day
        base_day = git(ROOT, "log", "-1", "--format=%cd",
                       "--date=short", tag_sha).stdout.strip()
        bug_until = add_years(base_day, 3)
        sec_until = add_years(base_day, 5)
        r = lts("status")
        check(r.returncode == 0, "status is green on the real line")
        check("branch release/1.0 from v1.0.0" in r.stdout,
              "status names the branch and its base")
        check("bug fixes until " + bug_until in r.stdout,
              "the bug-fix window runs to the third anniversary (%s)"
              % bug_until)
        check("security until " + sec_until in r.stdout,
              "the security window runs to the fifth (%s)" % sec_until)
        check("phase today: active" in r.stdout,
              "the line is active today")
        check("audit: clean" in r.stdout,
              "the fresh line audits clean")
        r = lts("window", "--tag", "v1.0.0", "--at", base_day)
        check(r.returncode == 0
              and "phase at %s: active" % base_day in r.stdout,
              "the release day itself is active (the horizon belongs "
              "to the phase it closes)")
        # idempotent, and one declaration only
        r = lts("init", "--tag", "v1.0.0")
        check(r.returncode == 0 and "idempotent" in r.stdout,
              "declaring the same line again is a no-op, not noise")
        r = lts("init", "--tag", "v1.1.0")
        check(r.returncode == 1
              and "one declaration" in r.stdout + r.stderr,
              "a different base is a new policy decision, refused")

    # ==================================================================
    header("4. the wrong turns, the cut — a scratch world runs the "
           "release day")
    repo = seed_world("the-release")
    r = rc("declare", "--tag", "v1.0.0-rc.4", cwd=repo)
    check(r.returncode == 0,
          "the scratch world declares its last candidate")
    commit_all(repo, "release: v1.0.0-rc.4 anchored, the record "
                     "rendered")
    git(repo, "tag", "v1.0.0-rc.4")
    # the wrong turns first
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
    # commit, the tag is cut, the line opens
    titles = {
        146: "v1.0 Release Candidate 1 (feature freeze)",
        147: "v1.0 Release Candidate 2 (bug-fix only)",
        148: "v1.0 Release Candidate 3 (final dry-run)",
        149: "v1.0 Release Candidate 4 (sign-off)",
        150: "HLS v1.0 — LTS release",
    }
    rows = []
    for n in range(1, 151):
        title = titles.get(n, "stage %d title" % n)
        rows.append("| %d | %s | ✅ | (done in v1.0.0) |" % (n, title))
    wf(repo, "ROADMAP.md",
       "# ROADMAP\n\n"
       "| # | Stage | Status | Effort |\n"
       "|---|-------|--------|--------|\n" + "\n".join(rows) + "\n")
    wf(repo, "CHANGELOG.md",
       "## [v1.0.0] — Stage 150: the LTS release\n- the body\n")
    wf(repo, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v1.0.0"}))
    wf(repo, "editors/vscode/halis/package.json",
       json.dumps({"name": "halis", "version": "1.0.0"}))
    git(repo, "rm", "-q", ".hls-rc.json")
    release_sha = commit_all(repo, "docs: stage 150 markers, the "
                                  "release")
    git(repo, "tag", "v1.0.0")
    r = lts("init", "--tag", "v1.0.0", cwd=repo)
    check(r.returncode == 0 and "release/1.0" in r.stdout,
          "the LTS line opens on the v1.0.0 tag (branch release/1.0)")
    check(git(repo, "rev-parse", "release/1.0^{commit}").stdout.strip()
          == release_sha,
          "the branch stands at the release commit")
    commit_all(repo, "release: the LTS line opens")
    # the windows became the clock
    r = lts("status", cwd=repo)
    check(r.returncode == 0
          and "bug fixes until " + add_years(SCRATCH_DAY, 3)
          in r.stdout
          and "security until " + add_years(SCRATCH_DAY, 5)
          in r.stdout,
          "the clock runs from the base day (3 years, 5 years)")
    check("audit: clean" in r.stdout,
          "the fresh line audits clean")
    r = lts("window", "--tag", "v1.0.0", "--at", SCRATCH_DAY, cwd=repo)
    check(r.returncode == 0
          and "phase at %s: active" % SCRATCH_DAY in r.stdout,
          "the release day itself is active")
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
    r = rc("signoff", "--check", cwd=repo)
    check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
          "the witness refuses on the missing anchor")
    # the line is idempotent; a second line is a new decision
    r = lts("init", "--tag", "v1.0.0", cwd=repo)
    check(r.returncode == 0 and "idempotent" in r.stdout,
          "declaring the same line again is a no-op, not noise")
    r = lts("init", "--tag", "v1.1.0", cwd=repo)
    check(r.returncode == 1
          and "one declaration" in r.stdout + r.stderr,
          "a different base is a new policy decision, not a re-init")

    # ==================================================================
    header("5. the docs law — the release speaks where the road "
           "wrote")
    if state == "released":
        with open(os.path.join(ROOT, "RELEASES.md")) as f:
            head_line = ""
            for line in f:
                if line.startswith("## [v"):
                    head_line = line.strip()
                    break
        check("[v1.0.0]" in head_line
              and "LTS release" in head_line,
              "the release record's head section is the release's "
              "own (%s)" % head_line)
        if has(ROOT, "ROADMAP.md"):
            rows = roadmap_rows()
            check("✅" in rows[149]["status"],
                  "the roadmap's last row is ticked")
            check("HLS v1.0" in rows[149]["title"],
                  "the last row keeps the roadmap's own phrase (%s)"
                  % rows[149]["title"])
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        check("make release-acceptance" in readme,
              "the README tour carries the stage 150 gate")
        check("hls-lts status" in readme,
              "the README tour carries the clock")
        check("release/1.0" in readme,
              "the README speaks the line the release opened")
    else:
        check(head_version() == "1.0.0-rc.4",
              "on the eve the record's head is still the "
              "candidate's")
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        check("make rc4-acceptance" in readme,
              "the README tour still carries the candidate gates")

    # ==================================================================
    header("6. the corpus showcase — the release laws in pure HLS")
    check(os.path.isfile(SHOWCASE), SHOWCASE + " exists")
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
