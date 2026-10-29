#!/usr/bin/env python3
"""Stage 149 acceptance gate — v1.0.0-rc.4, the sign-off.

Run with `make rc4-acceptance` (or `python3 tests/rc4_acceptance.py`).

Six sections over the REAL CLIs and REAL scratch git repositories —
the last approval is treated like every other law in this project:
re-derived, not narrated:

  1. the destination — the RC4 fixes pinned as laws: the record and
     law               the policy refuse an unwritable destination
                       with its NAME (exit 2) — a directory sitting
                       where the file would be, a parent that does
                       not exist — and never panic (101) on a path
                       the user typed; the writable path still
                       renders;
  2. the signature  — the signing act over a world walked to rc.4:
     law               the name law (no --by, blank --by: usage
                       errors), the anchor law (no anchor: nothing to
                       sign), the tree law (a dirty tracked tree
                       refuses the signature), the landing (the
                       signature rides in the anchor, its name and
                       its day), the once law (a second signature
                       refused — one per candidate);
  3. the witness    — `signoff --check` re-derives the verdict: an
     law               unsigned candidate says so, the signed world
                       is green (--json parses, the laws named), a
                       FEATURE commit past the anchor voids the gate
                       law, a hand-edited record voids the record
                       law — and restoring the world greens the SAME
                       signature again: re-earned, not archived;
  4. the record     — the render speaks the signature only when the
     law               candidate carries one: the unsigned render
                       carries no sign-off line, the signed render
                       gains exactly one (the name, the day), the
                       render-before/after diff is that one line,
                       and the record must be re-rendered before the
                       release commit or the drift check refuses;
  5. the real-tree  — the tree this gate runs on: readiness green,
     pre-flight        the gate green, the record drift-free, the
                       markers one voice, the anchor a candidate of
                       the line speaking its stage's own phrase —
                       and signoff --check pinned DYNAMICALLY: green
                       on a signed anchor, the unsigned refusal on an
                       unsigned one (both states are legal states of
                       the line);
  6. the corpus     — tests/ok/feat_stage149_rc4.hls is hlfmt-
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

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage149_rc4.hls")
SHOWCASE_OUT = ("stage 149: v1.0.0-rc.4\n"
                "  RC4 laws OK: signature, green, order, witness, "
                "refusal, anchor\n")

GATE = tempfile.mkdtemp(prefix=".rc149-gate-", dir=ROOT)

GIT_ENV = dict(os.environ)
GIT_ENV["GIT_AUTHOR_NAME"] = "rc149-gate"
GIT_ENV["GIT_AUTHOR_EMAIL"] = "rc149-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_NAME"] = "rc149-gate"
GIT_ENV["GIT_COMMITTER_EMAIL"] = "rc149-gate@example.invalid"
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
    release is not a state. This gate certified the signature; after
    the release it must still run — the signature's tree outlives
    the signature's office.
    """
    anchor = os.path.exists(os.path.join(ROOT, ".hls-rc.json"))
    line = os.path.exists(os.path.join(ROOT, ".hls-lts.json"))
    if anchor and not line:
        return "eve"
    if line and not anchor:
        return "released"
    return "half-cut"

# the signer the scratch world is signed off by
SIGNER = "the maintainer"

# the sign-off line the render speaks for a signed candidate
def signoff_line(name, day):
    return ("- the sign-off: **%s**, %s — the candidate carries its "
            "signer's approval; `bin/hls-rc signoff --check` re-derives "
            "it at every gate" % (name, day))

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


def move_markers(repo, n, stage):
    wf(repo, "ROADMAP.md", fancy_roadmap(stage))
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


def walk_candidate(repo, n):
    """Cut candidate n the unsigned way: the row ticked, the markers
    moved, the old anchor leaving with the release commit, declare,
    the record rendered, the tag cut."""
    stage = 145 + n
    move_markers(repo, n, stage)
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


def walk_signed_candidate(repo, n, signer):
    """Cut candidate n the SIGNED way (Stage 149's own choreography):
    the markers moved, declare, the signature into the anchor, the
    record re-rendered so it speaks the signature, the release commit,
    the tag. Returns the anchor dict."""
    stage = 145 + n
    move_markers(repo, n, stage)
    r = rc("declare", "--tag", "v1.0.0-rc.%d" % n, cwd=repo)
    if r.returncode != 0:
        bad("candidate rc.%d did not declare: %s"
            % (n, (r.stdout + r.stderr)[:200]))
        return {}
    r = rc("signoff", "--by", signer, cwd=repo)
    if r.returncode != 0:
        bad("candidate rc.%d did not sign off: %s"
            % (n, (r.stdout + r.stderr)[:200]))
        return {}
    rc("report", "--out", "RC.md", cwd=repo)
    commit_all(repo, "release: v1.0.0-rc.%d anchored, the record rendered"
               % n)
    git(repo, "tag", "v1.0.0-rc.%d" % n)
    return json.loads(rf(repo, ".hls-rc.json"))


def tag_line_of(text):
    for line in text.split("\n"):
        if line.startswith("- the tag: "):
            return line
    return ""


def signoff_lines_of(text):
    out = []
    for line in text.split("\n"):
        if line.startswith("- the sign-off: "):
            out.append(line)
    return out


def header(title):
    print("\n--- " + title + " ---")


def main():
    # ==================================================================
    header("1. the destination law — the refusal names the path, "
           "exit 2, no panic")
    repo = seed_world("the-destination", fancy_roadmap(149))
    missing = os.path.join(GATE, "no-such-dir")

    r = rc("report", "--out", os.path.join(missing, "RC.md"), cwd=repo)
    check(r.returncode == 2 and "cannot write file" in r.stderr
          and "(the directory does not exist)" in r.stderr,
          "the record refuses a missing directory (exit 2, named)")
    check("panic" not in (r.stdout + r.stderr).lower(),
          "no panic on the path the user typed")
    r = rc("report", "--out", repo, cwd=repo)
    check(r.returncode == 2 and "(a directory sits there)" in r.stderr,
          "the record refuses a directory destination (exit 2, named)")
    r = lts("policy", "--out", os.path.join(missing, "LTS.md"), cwd=repo)
    check(r.returncode == 2 and "cannot write file" in r.stderr
          and "(the directory does not exist)" in r.stderr,
          "the policy refuses a missing directory (exit 2, named)")
    check("panic" not in (r.stdout + r.stderr).lower(),
          "the policy refuses without a panic either")
    r = lts("policy", "--out", repo, cwd=repo)
    check(r.returncode == 2 and "(a directory sits there)" in r.stderr,
          "the policy refuses a directory destination (exit 2, named)")

    # the writable path still renders (the refusals did not eat the road)
    move_markers(repo, 4, 149)
    r = rc("declare", "--tag", "v1.0.0-rc.4", cwd=repo)
    check(r.returncode == 0, "the candidate declares for the render")
    out_dir = os.path.join(GATE, "rendered")
    os.makedirs(out_dir)
    r = rc("report", "--out", os.path.join(out_dir, "RC.md"), cwd=repo)
    check(r.returncode == 0 and has(out_dir, "RC.md"),
          "the writable destination still renders the record")
    r = lts("policy", "--out", os.path.join(out_dir, "LTS.md"), cwd=repo)
    check(r.returncode == 0 and has(out_dir, "LTS.md"),
          "the writable destination still renders the policy")

    # the working paths the kit already had stay green
    r = rc("readiness", cwd=repo)
    check(r.returncode == 0 and "READY" in r.stdout,
          "the scratch world is ready")
    r = rc("report", "--check", cwd=repo)
    check(r.returncode == 1 and "not published" in r.stdout + r.stderr,
          "a declared world without a published record says so, named")

    # ==================================================================
    header("2. the signature law — the signing act, refused and landed")
    repo = seed_world("the-signature", fancy_roadmap(145))

    # no anchor: nothing to sign
    r = rc("signoff", "--by", SIGNER, cwd=repo)
    check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
          "a world without a candidate has nothing to sign")

    # the name law: a signature names its signer
    r = rc("signoff", cwd=repo)
    check(r.returncode == 2 and "--by NAME is required" in r.stderr,
          "an anonymous sign-off is a usage error")
    r = rc("signoff", "--by", "   ", cwd=repo)
    check(r.returncode == 2 and "--by NAME is required" in r.stderr,
          "a blank signer is a usage error")

    # the wrong tag is not the candidate the stage names
    move_markers(repo, 4, 149)
    r = rc("declare", "--tag", "v1.0.0-rc.3", cwd=repo)
    check(r.returncode == 1 and "closes Stage" in r.stdout + r.stderr,
          "declare refuses the candidate whose stage is not the open "
          "one")

    # the signed walk: declare, sign, render, release, tag
    anchor = walk_signed_candidate(repo, 4, SIGNER)
    check(anchor.get("tag") == "v1.0.0-rc.4"
          and anchor.get("stage") == 149,
          "the rc.4 anchor carries the candidate and its stage")
    so = anchor.get("signoff", {})
    check(so.get("by") == SIGNER and so.get("day") == RELEASE_DAY,
          "the anchor carries the signature (the name, the day)")

    # the once law: a live signature is not overwritten
    r = rc("signoff", "--by", "someone else", cwd=repo)
    check(r.returncode == 1 and "already signed off" in r.stdout + r.stderr,
          "a second signature over a live one is refused")
    check(SIGNER in r.stdout + r.stderr,
          "the refusal names the signer who owns the candidate")

    # the tree law, on its own world: a dirty tracked tree refuses
    repo2 = seed_world("the-dirty", fancy_roadmap(149))
    move_markers(repo2, 4, 149)
    rc("declare", "--tag", "v1.0.0-rc.4", cwd=repo2)
    wf(repo2, "ROADMAP.md", fancy_roadmap(149) + "drift\n")
    r = rc("signoff", "--by", SIGNER, cwd=repo2)
    check(r.returncode == 1 and "dirty" in r.stdout + r.stderr,
          "a signature refuses a dirty tracked tree")
    git(repo2, "checkout", "-q", "--", "ROADMAP.md")
    r = rc("signoff", "--by", SIGNER, cwd=repo2)
    check(r.returncode == 0 and "signed off by" in r.stdout,
          "the committed tree signs (the anchor file itself is exempt)")
    so2 = json.loads(rf(repo2, ".hls-rc.json")).get("signoff", {})
    check(so2.get("by") == SIGNER and so2.get("day") == RELEASE_DAY,
          "the day is the committer day (--at pins it)")

    # --at pins the day the signature is judged on
    repo3 = seed_world("the-at-day", fancy_roadmap(149))
    move_markers(repo3, 4, 149)
    rc("declare", "--tag", "v1.0.0-rc.4", cwd=repo3)
    r = rc("signoff", "--by", SIGNER, "--at", "2026-12-24", cwd=repo3)
    check(r.returncode == 0, "the signature cuts with --at")
    so3 = json.loads(rf(repo3, ".hls-rc.json")).get("signoff", {})
    check(so3.get("day") == "2026-12-24",
          "--at pins the day the signature speaks")

    # ==================================================================
    header("3. the witness law — the check re-derives, re-earns")
    repo = seed_world("the-witness", fancy_roadmap(145))
    walk_candidate(repo, 4)

    # the unsigned law: the check says so, named
    r = rc("signoff", "--check", cwd=repo)
    check(r.returncode == 1 and "unsigned" in r.stdout + r.stderr,
          "an unsigned candidate says so at --check")
    r = rc("signoff", "--check", "--json", cwd=repo)
    check(r.returncode == 1, "the unsigned verdict parses as JSON too")
    try:
        data = json.loads(r.stdout)
        check(data.get("signed") is False and data.get("ok") is False
              and isinstance(data.get("laws"), list)
              and len(data["laws"]) >= 9,
              "the unsigned JSON carries the named laws (%d)"
              % len(data.get("laws", [])))
    except Exception as exc:
        bad("the unsigned JSON does not parse: %s" % exc)

    # the signing act on the witness world
    r = rc("signoff", "--by", SIGNER, cwd=repo)
    check(r.returncode == 0, "the candidate signs")

    # the record must be re-rendered: the committed record no longer
    # speaks the signed state (the drift the choreography renders away)
    r = rc("report", "--check", cwd=repo)
    check(r.returncode == 1,
          "the record drifts the moment the signature lands (render "
          "before commit)")
    r = rc("signoff", "--check", cwd=repo)
    check(r.returncode == 1 and "the record law" in r.stdout,
          "the witness refuses on the record law until the render")

    rc("report", "--out", "RC.md", cwd=repo)
    commit_all(repo, "release: v1.0.0-rc.4 anchored, the record rendered")
    git(repo, "tag", "v1.0.0-rc.4")
    r = rc("signoff", "--check", cwd=repo)
    check(r.returncode == 0 and "signed off by" in r.stdout,
          "the signed world is green at the witness")
    r = rc("signoff", "--check", "--json", cwd=repo)
    check(r.returncode == 0, "the signed verdict parses as JSON")
    try:
        data = json.loads(r.stdout)
        check(data.get("signed") is True and data.get("by") == SIGNER
              and data.get("day") == RELEASE_DAY and data.get("ok") is True
              and isinstance(data.get("laws"), list)
              and len(data["laws"]) >= 9,
              "the signed JSON carries the signature and the laws (%d)"
              % len(data.get("laws", [])))
    except Exception as exc:
        bad("the signed JSON does not parse: %s" % exc)

    # the gate law voids: a FEATURE past the anchor is not covered
    wf(repo, "src/new_surface.hls", "fn added() -> int {\n    return 1\n}\n")
    commit_all(repo, "feat: a surface the freeze refuses")
    r = rc("gate", cwd=repo)
    check(r.returncode == 1 and "VIOLATION" in r.stdout,
          "the gate refuses the feature (the kit's own voice)")
    r = rc("signoff", "--check", cwd=repo)
    check(r.returncode == 1 and "the gate law" in r.stdout,
          "the witness voids on the gate law")
    git(repo, "reset", "-q", "--hard", "HEAD~1")

    # the record law voids: a hand-edited record is visible
    wf(repo, "RC.md", rf(repo, "RC.md").replace(SIGNER, "someone else"))
    r = rc("signoff", "--check", cwd=repo)
    check(r.returncode == 1 and "the record law" in r.stdout,
          "the witness voids on the record law")
    git(repo, "checkout", "-q", "--", "RC.md")

    # re-earned: the SAME signature is green again
    r = rc("signoff", "--check", cwd=repo)
    check(r.returncode == 0 and "signed off by" in r.stdout,
          "the signature is re-earned, not archived")

    # ==================================================================
    header("4. the record law — the render speaks the signature, once")
    repo = seed_world("the-record", fancy_roadmap(145))
    before = walk_candidate(repo, 4)
    check(len(signoff_lines_of(before)) == 0,
          "the unsigned render carries no sign-off line")
    r = rc("report", "--check", cwd=repo)
    check(r.returncode == 0,
          "the committed unsigned record is the kit's render")

    r = rc("signoff", "--by", SIGNER, cwd=repo)
    check(r.returncode == 0, "the candidate signs")
    after = render_to_text(repo)
    check(len(signoff_lines_of(after)) == 1,
          "the signed render carries exactly one sign-off line")
    check(signoff_line(SIGNER, RELEASE_DAY) in signoff_lines_of(after),
          "the sign-off line names the signer and the day")
    added = [l for l in after.split("\n") if l not in before.split("\n")]
    check(added == signoff_lines_of(after),
          "the render diff is exactly the sign-off line (%d line(s) "
          "added)" % len(added))

    # the choreography: render, commit, tag — then the record is the
    # anchored one
    rc("report", "--out", "RC.md", cwd=repo)
    commit_all(repo, "release: v1.0.0-rc.4 anchored, the record rendered")
    git(repo, "tag", "v1.0.0-rc.4")
    r = rc("report", "--check", cwd=repo)
    check(r.returncode == 0,
          "the committed record is the signed render (drift refused)")

    # ==================================================================
    header("5. the real-tree pre-flight — this tree, this moment")
    r = rc("readiness")
    if release_state() == "released":
        check(r.returncode == 1 and "NOT READY: the roadmap law"
              in r.stdout and "NOT READY: the lts law" in r.stdout,
              "the REAL tree is released (readiness refuses on both "
              "laws, named)")
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

    # the anchor is a candidate of the line, speaking its stage's own
    # phrase off the live roadmap (the law stays green for every
    # candidate: each row's phrase is read where the record stands)
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

        # signoff --check, pinned DYNAMICALLY: green on a signed
        # anchor, the unsigned refusal on an unsigned one — both are
        # legal states of the line, and the witness names its state
        r = rc("signoff", "--check")
        signed = json.loads(rf(ROOT, ".hls-rc.json")).get(
            "signoff", {}).get("by", "") != ""
        if signed:
            check(r.returncode == 0 and "signed off by" in r.stdout,
                  "the real tree's signature is re-derived green")
        else:
            check(r.returncode == 1 and "unsigned" in r.stdout
                  + r.stderr,
                  "the real tree's unsigned state is named (the "
                  "instrument reads the anchor, not the wish)")
    else:
        check(release_state() == "released",
              "no anchor on the tree — and the line is declared (the "
              "release, not a gap)")
        r = rc("signoff", "--check")
        check(r.returncode == 1 and "no anchor" in r.stdout + r.stderr,
              "the signature's office closed with the anchor (the "
              "witness names it)")

    # ==================================================================
    header("6. the corpus showcase — the sign-off laws in pure HLS")
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


# ----------------------------------------------------------------------
# Render helpers (the record as the kit would render it, without
# touching the working tree's committed copy).
# ----------------------------------------------------------------------

def render_to_text(repo):
    out_dir = os.path.join(GATE, "render-probe")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    r = rc("report", "--out", os.path.join(out_dir, "RC.md"), cwd=repo)
    if r.returncode != 0:
        bad("the render probe failed: %s" % (r.stdout + r.stderr)[:160])
        return ""
    return rf(out_dir, "RC.md")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(GATE, ignore_errors=True)
