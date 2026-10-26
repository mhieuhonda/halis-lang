#!/usr/bin/env python3
"""Stage 145 acceptance gate — the LTS policy published: 3 years
bug-fix, 5 years security (v0.166.0-alpha).

Run with `make lts-policy-acceptance` (or
`python3 tests/lts_policy_acceptance.py`).

Nine sections over the REAL policy CLI (subprocess, the interface a
release manager drives) and REAL scratch git repositories whose
birthdays are fabricated on command (GIT_COMMITTER_DATE) — a support
window cannot wait three years to be tested:

  1. the convention  — `window` without a policy or a tag refuses
                       and names both remedies; the --at grammar; the
                       exits (0 ok / 1 refusal / 2 usage)

  2. the calendar    — anniversaries, not day counts: the February-29
     law               clamp (2020-02-29 -> 2023-02-28 ->
                       2025-02-28), the plain anniversary, and the
                       horizon day that belongs to the phase it
                       closes (judged with --at, never with a slept-on
                       clock)

  3. the phase       — active / security / eol over fabricated bases,
     machine           --json exact (the two horizons, the phase, the
                       day counts), the same law through the declared
                       line and through --tag

  4. the marking     — `classify` reports the Security mark beside
     law                the class: the trailer grammar (case-
                       insensitive name, trimmed value, the first
                       line decides, empty is no claim), and the
                       fact that a marked FEATURE is still a FEATURE

  5. the backport    — the window gate: a plain fix refused in
     window law        security-only (both horizons named), the
                       marked fix landing with the provenance
                       trailer, the dead line refusing everything
                       (even a marked fix, even a chore), the active
                       line taking both, dry runs reflecting the
                       phase, and the pick dated by the clock the
                       gate ran under

  6. the tag law     — no release from a dead line; a chore-only
                       release in the security phase refused as the
                       pretend-alive release it is; the release
                       carrying a marked backport cutting green;
                       chore releases cutting green while the line is
                       alive

  7. the audit law   — history judged the day it landed: the unmarked
                       backport in security-only, the commit past end
                       of life, the tag cut past end of life — each
                       named, each a violation; the clean window
                       auditing clean

  8. the publication — the render deterministic (two renders byte-
     law                identical), the committed LTS.md matching it
                       on THIS tree, the drifted copy refused with
                       the regenerate command named, the constants
                       the document carries being the tool's own

  9. the corpus      — tests/ok/feat_stage145_ltspolicy.hls is
     showcase          hlfmt-canonical, hllint-clean, boots through
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

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage145_ltspolicy.hls")
SHOWCASE_OUT = ("stage 145: hls-lts windows\n"
                "  LTS windows OK: calendar, phases, marking, "
                "admission\n")

LTS_DOC = os.path.join(ROOT, "LTS.md")

GATE = tempfile.mkdtemp(prefix=".lts145-gate-", dir=ROOT)

GIT_ENV = dict(os.environ)
GIT_ENV["GIT_AUTHOR_NAME"] = "lts145-gate"
GIT_ENV["GIT_AUTHOR_EMAIL"] = "lts145-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_NAME"] = "lts145-gate"
GIT_ENV["GIT_COMMITTER_EMAIL"] = "lts145-gate@example.invalid"

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
                          text=True, cwd=cwd,
                          env=env if env is not None else GIT_ENV)


def lts(args, repo=None, when=None):
    a = LTS + args
    if repo is not None:
        a += ["--repo", repo]
    env = GIT_ENV
    if when is not None:
        # The clock the pick lands under: a cherry-pick's committer
        # day is the day it landed — the day the audit will judge.
        env = dict(GIT_ENV)
        env["GIT_COMMITTER_DATE"] = when + "T12:00:00+00:00"
        env["GIT_AUTHOR_DATE"] = when + "T12:00:00+00:00"
    return run(a, env=env)


def git(repo, *args, when=None):
    env = GIT_ENV
    if when is not None:
        env = dict(GIT_ENV)
        env["GIT_COMMITTER_DATE"] = when + "T12:00:00+00:00"
        env["GIT_AUTHOR_DATE"] = when + "T12:00:00+00:00"
    return run(["git", "-C", repo] + list(args), env=env)


def make_repo(name, base_day, tag="v1.0.0"):
    """A fresh scratch repo whose base release was cut on base_day."""
    r = os.path.join(GATE, name)
    os.makedirs(r)
    assert git(r, "init", "-q", "-b", "main", ".").returncode == 0
    commit(r, {"app.txt": "seed line\n"}, "stage 1: seed the project",
           when=base_day)
    assert git(r, "tag", tag).returncode == 0
    return r


def commit(repo, files, message, when=None, tag=None):
    for name, content in files.items():
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    git(repo, "add", "--", *files.keys())
    p = git(repo, "commit", "-q", "--allow-empty-message", "-m", message,
            when=when)
    assert p.returncode == 0, p.stdout
    if tag:
        assert git(repo, "tag", tag).returncode == 0
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def branch_head(repo, branch):
    p = git(repo, "rev-parse", branch)
    return p.stdout.strip() if p.returncode == 0 else None


def load_json(p):
    try:
        return json.loads(p.stdout)
    except Exception as exc:
        bad("json is not well-formed: %s" % exc)
        return None


# ===========================================================================
def _section_1():
    print("Section 1 — the convention")

    # window needs a line: the declaration or a tag.
    r = make_repo("conv", "2026-10-01")
    p = lts(["window"], repo=r)
    check(p.returncode == 1 and "no LTS policy declared" in p.stdout
          and "--tag" in p.stdout and "init --tag" in p.stdout,
          "window without a policy names both remedies")

    # The --at grammar.
    p = lts(["window", "--tag", "v1.0.0", "--at", "not-a-day"], repo=r)
    check(p.returncode == 1 and "--at must be a plain ISO day" in p.stdout,
          "a malformed --at is refused, named")

    p = lts(["window", "--tag", "v1.0.0", "--at", "2026-13-40"], repo=r)
    check(p.returncode == 1 and "--at must be a plain ISO day" in p.stdout,
          "an impossible calendar day is refused, named")

    # The tag laws: the windows are a release's promise.
    p = lts(["window", "--tag", "v1.0.0-rc.1"], repo=r)
    check(p.returncode == 1 and "pre-release" in p.stdout,
          "a pre-release tag is refused (the windows are a release's "
          "promise)")

    p = lts(["window", "--tag", "v0.9.0"], repo=r)
    check(p.returncode == 1 and "pre-1.0" in p.stdout,
          "a pre-1.0 tag is refused (the policy begins after v1.0)")

    p = lts(["window", "--tag", "v9.9.9"], repo=r)
    check(p.returncode == 1 and "does not exist" in p.stdout,
          "a nonexistent tag is refused, named")

    # The policy document command: unknown flags are usage errors.
    p = lts(["policy", "--no-such-flag"])
    check(p.returncode == 2, "an unknown flag is a usage error (2)")

    # The publication is live on THIS tree.
    p = lts(["policy", "--check"])
    check(p.returncode == 0 and "matches the render" in p.stdout
          and "3 years bug-fix, 5 years security" in p.stdout,
          "the committed LTS.md matches the render on this tree")


def _section_2():
    print("Section 2 — the calendar law")

    # The February-29 clamp: the anniversary lands on a day that
    # exists. A line born on the leap day of 2020 is bug-fixed
    # through 2023-02-28 and dies on 2025-02-28 (through).
    r = make_repo("leap", "2020-02-29")
    p = lts(["window", "--tag", "v1.0.0", "--at", "2020-03-01"], repo=r)
    check(p.returncode == 0 and "base released 2020-02-29" in p.stdout
          and "bug fixes until:  2023-02-28" in p.stdout
          and "security until:   2025-02-28" in p.stdout,
          "the February-29 base clamps to the 28th on both horizons")

    # The horizon day belongs to the phase it closes: 2023-02-28 is
    # still active; 2023-03-01 is security-only.
    p = lts(["window", "--tag", "v1.0.0", "--at", "2023-02-28", "--json"],
            repo=r)
    data = load_json(p)
    check(data is not None and data["phase"] == "active",
          "the third anniversary (clamped) is still active")

    p = lts(["window", "--tag", "v1.0.0", "--at", "2023-03-01", "--json"],
            repo=r)
    data = load_json(p)
    check(data is not None and data["phase"] == "security",
          "the day after the clamped horizon is security-only")

    p = lts(["window", "--tag", "v1.0.0", "--at", "2025-02-28", "--json"],
            repo=r)
    data = load_json(p)
    check(data is not None and data["phase"] == "security",
          "the fifth anniversary (clamped) is the last supported day")

    p = lts(["window", "--tag", "v1.0.0", "--at", "2025-03-01", "--json"],
            repo=r)
    data = load_json(p)
    check(data is not None and data["phase"] == "eol",
          "the day after the fifth anniversary is end of life")

    # The plain anniversary: no clamping, exact month and day.
    r2 = make_repo("plain", "2021-01-31")
    p = lts(["window", "--tag", "v1.0.0", "--at", "2021-02-01"], repo=r2)
    check(p.returncode == 0 and "bug fixes until:  2024-01-31" in p.stdout
          and "security until:   2026-01-31" in p.stdout,
          "the plain anniversary keeps month and day")

    # The leap-day base into a leap target keeps the day.
    r3 = make_repo("leap2", "2024-02-29", tag="v1.0.0")
    p = lts(["window", "--tag", "v1.0.0", "--at", "2024-03-01"], repo=r3)
    check(p.returncode == 0 and "bug fixes until:  2027-02-28" in p.stdout
          and "security until:   2029-02-28" in p.stdout,
          "a 2024-02-29 base clamps through both horizons")


def _section_3():
    print("Section 3 — the phase machine")

    # A base 2 years old: active, with the day counts in the json.
    r = make_repo("young", "2024-06-15")
    lts(["init", "--tag", "v1.0.0"], repo=r)
    p = lts(["window", "--at", "2026-06-15", "--json"], repo=r)
    data = load_json(p)
    check(data is not None
          and data["base_date"] == "2024-06-15"
          and data["bug_fix_until"] == "2027-06-15"
          and data["security_until"] == "2029-06-15"
          and data["phase"] == "active"
          and data["days_bugfix_left"] == 365,
          "a 2-year-old line is active, horizons and days exact")

    # A base 4 years old: security-only.
    r2 = make_repo("mid", "2022-06-15")
    lts(["init", "--tag", "v1.0.0"], repo=r2)
    p = lts(["window", "--at", "2026-06-15", "--json"], repo=r2)
    data = load_json(p)
    check(data is not None and data["phase"] == "security"
          and data["days_security_left"] == 365,
          "a 4-year-old line is security-only, one year of it left")

    # A base 6 years old: dead.
    r3 = make_repo("old", "2020-06-15")
    lts(["init", "--tag", "v1.0.0"], repo=r3)
    p = lts(["window", "--at", "2026-06-15", "--json"], repo=r3)
    data = load_json(p)
    check(data is not None and data["phase"] == "eol",
          "a 6-year-old line is past end of life")

    # The human render names the phase and its meaning.
    p = lts(["window", "--at", "2026-06-15"], repo=r3)
    check(p.returncode == 0 and "phase at 2026-06-15: eol" in p.stdout
          and "end of life — nothing lands on a dead line" in p.stdout
          and "the line ended 2025-06-15" in p.stdout,
          "the eol render names the day the line died")

    # status carries the live window.
    p = lts(["status", "--repo", r2])
    check(p.returncode == 0 and "window: bug fixes until 2025-06-15"
          in p.stdout and "phase today: security" in p.stdout,
          "status prints the live window line")

    p = lts(["status", "--repo", r2, "--json"])
    data = load_json(p)
    check(data is not None and data["window"]["phase"] == "security",
          "status --json carries the window")

    # The same law through --tag, no declaration needed.
    p = lts(["window", "--tag", "v1.0.0", "--at", "2024-06-15", "--json"],
            repo=r)
    data = load_json(p)
    check(data is not None and data["base_tag"] == "v1.0.0"
          and data["phase"] == "active",
          "window --tag judges the line without a declaration")


def _section_4():
    print("Section 4 — the marking law")
    r = make_repo("marking", "2026-10-01")

    marked = commit(r, {"b1.txt": "one\n"},
                    "fix: the taint escape\n\nSecurity: CVE-2027-1234")
    p = lts(["classify", marked, "--repo", r, "--json"])
    data = load_json(p)
    check(data is not None and data["class"] == "BUGFIX"
          and data["security"] is True
          and data["security_reason"] == "CVE-2027-1234",
          "the Security trailer marks the fix, value carried")

    # The human render reports the marking beside the class.
    p = lts(["classify", marked, "--repo", r])
    check(p.returncode == 0 and "security: marked — CVE-2027-1234"
          in p.stdout, "classify prints the mark beside the class")

    # Case-insensitive name, trimmed value.
    lower = commit(r, {"b2.txt": "two\n"},
                   "fix: lowercase trailer\n\nsecurity:   high  ")
    p = lts(["classify", lower, "--repo", r, "--json"])
    data = load_json(p)
    check(data is not None and data["security"] is True
          and data["security_reason"] == "high",
          "the name is case-insensitive, the value trimmed")

    # An empty value is not a marking — fail-closed.
    empty = commit(r, {"b3.txt": "three\n"}, "fix: empty claim\n\nSecurity:")
    p = lts(["classify", empty, "--repo", r, "--json"])
    data = load_json(p)
    check(data is not None and data["security"] is False,
          "an empty Security value is no claim")

    # The first such line decides, even when it is empty.
    first = commit(r, {"b4.txt": "four\n"},
                   "fix: first decides\n\nSecurity:\nsecurity: real")
    p = lts(["classify", first, "--repo", r, "--json"])
    data = load_json(p)
    check(data is not None and data["security"] is False,
          "the first Security line decides, empty or not")

    # A marked FEATURE is still a FEATURE — the marking is a fact,
    # not a key past the class law.
    feat = commit(r, {"std/extra.hls": "fn shiny() -> int { return 1 }\n"},
                  "feat: confetti mode\n\nSecurity: does not help")
    p = lts(["classify", feat, "--repo", r, "--json"])
    data = load_json(p)
    check(data is not None and data["class"] == "FEATURE"
          and data["security"] is True,
          "a marked FEATURE is still a FEATURE, mark carried as fact")

    # No trailer, no claim.
    plain = commit(r, {"b5.txt": "five\n"}, "fix: plain bug fix")
    p = lts(["classify", plain, "--repo", r, "--json"])
    data = load_json(p)
    check(data is not None and data["security"] is False,
          "no trailer, no security claim")

    # A word in front of the name is not the name.
    prose = commit(r, {"b6.txt": "six\n"},
                   "fix: prose\n\nthe Security: docs say no")
    p = lts(["classify", prose, "--repo", r, "--json"])
    data = load_json(p)
    check(data is not None and data["security"] is False,
          "a word in front of the name is not the name")


def _section_5():
    print("Section 5 — the backport window law")

    # A line in its security-only phase (base 2022, judged 2026).
    r = make_repo("bp", "2022-06-15")
    plain_fix = commit(r, {"bug.txt": "one\n"}, "fix: the wrap on leap days",
                       when="2026-06-10")
    sec_fix = commit(r, {"bug2.txt": "two\n"},
                     "fix: the taint escape\n\n"
                     "Security: CVE-2027-1234", when="2026-06-11")
    feat = commit(r, {"widget.txt": "w\n"}, "feat: confetti mode",
                  when="2026-06-12")
    assert lts(["init", "--tag", "v1.0.0"], repo=r).returncode == 0

    # The plain fix is refused in security-only, both horizons named.
    p = lts(["backport", plain_fix, "--repo", r, "--at", "2026-06-15"])
    check(p.returncode == 1 and "security-only" in p.stdout
          and "since 2025-06-15" in p.stdout
          and "until 2027-06-15" in p.stdout
          and "Security" in p.stdout,
          "a plain fix is refused in security-only, horizons named")

    # The marked fix lands, with the window line.
    p = lts(["backport", sec_fix, "--repo", r, "--at", "2026-06-15",
             ], when="2026-06-15")
    check(p.returncode == 0 and "applied" in p.stdout
          and "Backported-from" in p.stdout
          and "security-only — the line takes security fixes until "
          "2027-06-15" in p.stdout,
          "the marked fix lands in security-only, window named")

    # The feature is refused by the class law, as ever.
    p = lts(["backport", feat, "--repo", r, "--at", "2026-06-15"])
    check(p.returncode == 1 and "classifies FEATURE" in p.stdout,
          "the class law is unchanged under the window law")

    # A dry run reflects the phase, touches nothing.
    plain2 = commit(r, {"bug.txt": "three\n"}, "fix: another plain one",
                    when="2026-06-13")
    before = branch_head(r, "release/1.0")
    p = lts(["backport", plain2, "--repo", r, "--at", "2026-06-15",
             "--dry-run"])
    check(p.returncode == 0 and "window: security" in p.stdout
          and "would refuse" in p.stdout
          and "no 'Security:' trailer" in p.stdout
          and branch_head(r, "release/1.0") == before,
          "the dry run names the phase and the refusal, touching nothing")

    # A dead line refuses everything — the marked fix too.
    r2 = make_repo("dead", "2020-06-15")
    dead_sec = commit(r2, {"bug.txt": "x\n"},
                      "fix: the taint escape\n\n"
                      "Security: CVE-2027-9999", when="2026-06-10")
    assert lts(["init", "--tag", "v1.0.0"], repo=r2).returncode == 0
    p = lts(["backport", dead_sec, "--repo", r2, "--at", "2026-06-15"])
    check(p.returncode == 1 and "end of life" in p.stdout
          and "2025-06-15" in p.stdout and "dead line" in p.stdout,
          "a dead line refuses even a marked fix")

    # An active line takes the plain fix without ceremony.
    r3 = make_repo("alive", "2024-06-15")
    alive_fix = commit(r3, {"bug.txt": "y\n"}, "fix: plain but welcome",
                       when="2026-06-10")
    assert lts(["init", "--tag", "v1.0.0"], repo=r3).returncode == 0
    p = lts(["backport", alive_fix, "--repo", r3, "--at", "2026-06-15"],
            when="2026-06-15")
    check(p.returncode == 0 and "applied" in p.stdout
          and "window: active" in p.stdout,
          "an active line takes the plain fix, window named")

    # The same repo judged inside its active window (2025) would
    # have taken it too — --at is the whole clock.
    r4 = make_repo("clock", "2024-06-15")
    clock_fix = commit(r4, {"bug.txt": "z\n"}, "fix: judged early",
                       when="2026-06-10")
    assert lts(["init", "--tag", "v1.0.0"], repo=r4).returncode == 0
    p = lts(["backport", clock_fix, "--repo", r4, "--at", "2026-01-01"],
            when="2026-01-01")
    check(p.returncode == 0 and "window: active" in p.stdout,
          "--at moves the clock: the same line takes the fix a year "
          "earlier")


def _section_6():
    print("Section 6 — the tag law")

    # Security-only line: a chore-only release is refused.
    r = make_repo("tag1", "2022-06-15")
    assert lts(["init", "--tag", "v1.0.0"], repo=r).returncode == 0
    # Land a chore directly on the branch (bookkeeping is allowed in
    # the security phase — but it is not a security backport).
    git(r, "checkout", "-q", "release/1.0")
    commit(r, {"notes.txt": "n\n"}, "docs: release notes draft",
           when="2026-06-15")
    git(r, "checkout", "-q", "main")
    p = lts(["tag-patch", "--repo", r, "--at", "2026-06-15"])
    check(p.returncode == 1 and "security-only" in p.stdout
          and "at least one security backport" in p.stdout
          and "pretends to be alive" in p.stdout,
          "a chore-only release in the security phase is refused")

    # With a marked backport, the release cuts.
    sec_fix = commit(r, {"bug.txt": "s\n"},
                     "fix: the taint escape\n\n"
                     "Security: CVE-2027-1234", when="2026-06-16")
    p = lts(["backport", sec_fix, "--repo", r, "--at", "2026-06-17"],
            when="2026-06-17")
    check(p.returncode == 0, "the marked fix backports")
    p = lts(["tag-patch", "--repo", r, "--at", "2026-06-17"])
    check(p.returncode == 0 and "tagged v1.0.1" in p.stdout,
          "the security release carrying the marked fix cuts green")

    # A dead line cuts no release at all.
    r2 = make_repo("tag2", "2020-06-15")
    sec_fix2 = commit(r2, {"bug.txt": "s\n"},
                      "fix: too late\n\nSecurity: CVE-2031-0001",
                      when="2026-06-10")
    assert lts(["init", "--tag", "v1.0.0"], repo=r2).returncode == 0
    p = lts(["backport", sec_fix2, "--repo", r2, "--at", "2026-06-15"])
    check(p.returncode == 1 and "end of life" in p.stdout,
          "the dead line refused the backport on the way")
    p = lts(["tag-patch", "--repo", r2, "--at", "2026-06-15"])
    check(p.returncode == 1 and "no release is cut from a dead line"
          in p.stdout and "2025-06-15" in p.stdout,
          "a dead line cuts no release")

    # An active line cuts a chore release with no ceremony.
    r3 = make_repo("tag3", "2024-06-15")
    assert lts(["init", "--tag", "v1.0.0"], repo=r3).returncode == 0
    git(r3, "checkout", "-q", "release/1.0")
    commit(r3, {"notes.txt": "n\n"}, "docs: note", when="2026-06-15")
    git(r3, "checkout", "-q", "main")
    p = lts(["tag-patch", "--repo", r3, "--at", "2026-06-15"])
    check(p.returncode == 0 and "tagged v1.0.1" in p.stdout,
          "an alive line cuts bookkeeping releases freely")


def _section_7():
    print("Section 7 — the audit law")

    # History judged the day it landed. The security-phase landing of
    # an unmarked fix: planted by hand (the bypass the audit exists
    # to catch), dated inside the security window.
    r = make_repo("aud", "2022-06-15")
    plain_fix = commit(r, {"bug.txt": "one\n"}, "fix: the wrap on leap days",
                       when="2022-06-20")
    assert lts(["init", "--tag", "v1.0.0"], repo=r).returncode == 0
    git(r, "checkout", "-q", "release/1.0")
    commit(r, {"bug.txt": "one\n"},
           "fix: the wrap on leap days\n\n"
           "Backported-from: %s" % plain_fix, when="2026-06-15")
    git(r, "checkout", "-q", "main")

    p = lts(["audit", "--repo", r])
    check(p.returncode == 1 and "security-only phase" in p.stdout
          and "without a Security mark" in p.stdout
          and "after 2025-06-15" in p.stdout,
          "an unmarked backport landing in security-only is a violation")

    # Roll it back; a marked backport landing in the security phase
    # audits clean.
    git(r, "update-ref", "refs/heads/release/1.0",
        branch_head(r, "v1.0.0"))
    sec_fix = commit(r, {"bugsec.txt": "two\n"},
                     "fix: the taint escape\n\n"
                     "Security: CVE-2027-1234", when="2026-06-16")
    p = lts(["backport", sec_fix, "--repo", r, "--at", "2026-06-17"],
            when="2026-06-17")
    check(p.returncode == 0, "the marked fix backports, dated by the gate")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 0 and "clean" in p.stdout,
          "a marked backport in security-only audits clean")

    # A commit past end of life is a violation, whatever it is
    # (this line's end of life is 2027-06-15 — the plant is dated
    # after it).
    git(r, "checkout", "-q", "release/1.0")
    commit(r, {"notes.txt": "n\n"}, "docs: on a dead line",
           when="2027-07-01")
    git(r, "checkout", "-q", "main")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 1 and "past end of life" in p.stdout
          and "dead line takes nothing" in p.stdout,
          "a commit past end of life is a violation, even bookkeeping")
    git(r, "update-ref", "refs/heads/release/1.0",
        branch_head(r, "release/1.0~1"))

    # A tag cut from a commit dated past end of life is a violation.
    git(r, "tag", "v1.0.1", "release/1.0")
    p = lts(["audit", "--repo", r])
    check(p.returncode == 0, "the tag on the live commit audits clean")

    # A tag whose commit is dated past the horizon: plant the
    # branch's head in the future, tag it, roll back the branch.
    r2 = make_repo("aud2", "2020-06-15")
    future_fix = commit(r2, {"bug.txt": "f\n"}, "fix: from the future",
                        when="2027-01-01")
    assert lts(["init", "--tag", "v1.0.0"], repo=r2).returncode == 0
    git(r2, "checkout", "-q", "release/1.0")
    commit(r2, {"bug.txt": "f\n"},
           "fix: from the future\n\n"
           "Backported-from: %s" % future_fix, when="2027-01-01")
    git(r2, "tag", "v1.0.1")
    git(r2, "checkout", "-q", "main")
    p = lts(["audit", "--repo", r2])
    check(p.returncode == 1
          and ("tag v1.0.1 is cut from a commit dated 2027-01-01, past "
               "end of life") in p.stdout,
          "a tag cut past end of life is a violation, named with its day")


def _section_8():
    print("Section 8 — the publication law")

    # Two renders are byte-identical.
    p1 = lts(["policy"])
    p2 = lts(["policy"])
    check(p1.returncode == 0 and p1.stdout == p2.stdout
          and p1.stdout.startswith("# Long-term support policy — Halis"),
          "the policy render is deterministic")

    # The committed document IS the render, on this tree.
    with open(LTS_DOC, "r", encoding="utf-8") as fh:
        committed = fh.read()
    check(committed == p1.stdout,
          "the committed LTS.md is byte-identical to the render")

    # The document carries the tool's own constants — the numbers the
    # gates enforce are the numbers on the page.
    check("**3 years of bug-fix" in committed
          and "**5 years of security support**" in committed
          and "last 2 years of a 5-year" in committed,
          "the published numbers are the tool's constants")

    # A drifted copy is refused, with the regenerate command named.
    r = make_repo("pub", "2026-10-01")
    doc = os.path.join(r, "LTS.md")
    with open(doc, "w", encoding="utf-8") as fh:
        fh.write(p1.stdout.replace("3 years of bug-fix",
                                  "2 years of bug-fix", 1))
    p = lts(["policy", "--check"], repo=r)
    check(p.returncode == 1 and "drifted" in p.stdout
          and "hand-edited" in p.stdout
          and "policy --out LTS.md" in p.stdout,
          "a hand-edited policy is refused, the regenerate command named")

    # Regenerating repairs it (the --out path is the caller's, not
    # the repo's — an absolute one keeps it inside the scratch repo).
    p = lts(["policy", "--out", os.path.join(r, "LTS.md")], repo=r)
    check(p.returncode == 0 and "rendered" in p.stdout,
          "the regenerate command writes the render")
    p = lts(["policy", "--check"], repo=r)
    check(p.returncode == 0 and "matches the render" in p.stdout,
          "the regenerated copy verifies green")

    # A repo without the document says so.
    r2 = make_repo("pub2", "2026-10-01")
    p = lts(["policy", "--check"], repo=r2)
    check(p.returncode == 1 and "not published here" in p.stdout,
          "a tree without LTS.md says it is not published")


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
        _section_9()
    finally:
        shutil.rmtree(GATE, ignore_errors=True)
    print("")
    print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
