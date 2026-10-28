#!/usr/bin/env python3
"""Stage 147 acceptance gate — v1.0.0-rc.2, the bug-fix round.

Run with `make rc2-acceptance` (or `python3 tests/rc2_acceptance.py`).

Five sections over the REAL CLIs and a REAL scratch git repository —
the successor candidate is rehearsed end to end on a world built for
the purpose, and the bug fixes this round ships are pinned as laws:

  1. the regression   — the invocation errors the RC2 fixes restored:
     law                a missing input file refuses on BOTH front-ends
                        (exit 2, `error: cannot open file <path>` — no
                        panic, no compile-error shape), the audit path
                        with it, an unwritable output destination is
                        refused up front (before codegen), and a
                        broken stdout pipe dies silently the way the
                        native binary always has (no traceback);
  2. the working      — the same CLIs on the paths that always worked:
     paths               a program runs, compiles, audits; --check
                        holds; the refusals did not eat the road;
  3. the real-tree    — the freeze still holds on the live tree: the
     candidate laws      gate names every commit since the anchor
                        BUGFIX or CHORE, readiness is green, and the
                        three version markers speak with one voice;
  4. the successor    — the RC2 choreography rehearsed on a scratch
     rehearsal           world: the rc.1 candidate declared, then the
                        successor cut the same way — Stage 147 ticked,
                        the markers moved, the old anchor left with
                        the release commit, rc.2 declared, the gate
                        green across the whole rc.1..rc.2 range, the
                        record rendered and drift-checked;
  5. the corpus       — tests/ok/feat_stage147_rc2.hls is hlfmt-
     showcase            canonical, hllint-clean, boots through the
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
BOOT = [sys.executable, os.path.join(ROOT, "tools", "hlinterp", "boot.py")]
FMT = [os.path.join(ROOT, "bin", "hlfmt")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage147_rc2.hls")
SHOWCASE_OUT = ("stage 147: v1.0.0-rc.2\n"
                "  RC2 laws OK: successor, stages, refusals, diff gate, "
                "window\n")

GATE = tempfile.mkdtemp(prefix=".rc147-gate-", dir=ROOT)

GIT_ENV = dict(os.environ)
GIT_ENV["GIT_AUTHOR_NAME"] = "rc147-gate"
GIT_ENV["GIT_AUTHOR_EMAIL"] = "rc147-gate@example.invalid"
GIT_ENV["GIT_COMMITTER_NAME"] = "rc147-gate"
GIT_ENV["GIT_COMMITTER_EMAIL"] = "rc147-gate@example.invalid"
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
                          stderr=subprocess.PIPE, timeout=300,
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


def roadmap(closed_through, total=150):
    rows = []
    for n in range(1, total + 1):
        st = "✅" if n <= closed_through else "⬜"
        rows.append("| %d | stage %d title | %s | (done in v0.1.0) |"
                    % (n, n, st))
    return ("# ROADMAP\n\n"
            "| # | Stage | Status | Effort |\n"
            "|---|-------|--------|--------|\n"
            + "\n".join(rows) + "\n")


def scratch_world(name, version, closed_through, with_anchor=False):
    """A minimal compliant world at changelog `version`, stages closed
    through `closed_through`; `with_anchor` cuts the matching candidate
    on paper (declare) so the tree carries an anchor."""
    repo = os.path.join(GATE, name)
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    wf(repo, "ROADMAP.md", roadmap(closed_through))
    wf(repo, "CHANGELOG.md",
       "## [v%s] — the candidate\n- the body\n" % version)
    wf(repo, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v" + version}))
    wf(repo, "editors/vscode/halis/package.json",
       json.dumps({"name": "halis", "version": version}))
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
    if with_anchor:
        r = rc("declare", "--tag", "v" + version, "--root", repo)
        if r.returncode != 0:
            bad("scratch %s: the base candidate did not declare: %s"
                % (name, (r.stdout + r.stderr)[:160]))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "release: the base candidate")
    return repo


def commit_all(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def header(title):
    print("\n--- " + title + " ---")


def main():
    # ==================================================================
    header("1. the regression law — the invocation errors refuse, named")
    missing = os.path.join(GATE, "never-there.hls")

    r = run(BOOT + [missing])
    check(r.returncode == 2,
          "boot.py: a missing entry file refuses (exit 2, was a "
          "compile-error shape)")
    check("error: cannot open file" in r.stderr and missing in r.stderr,
          "boot.py: the refusal names the path on stderr")
    check("Traceback" not in r.stderr and "panic" not in r.stderr,
          "boot.py: no traceback, no panic — an invocation error is not "
          "a crash")

    r = run([HLC, missing, os.path.join(GATE, "out.c")])
    check(r.returncode == 2,
          "hlc: a missing input file refuses (exit 2, was panic 101)")
    check("error: cannot open file" in r.stdout and missing in r.stdout,
          "hlc: the refusal names the path")
    check("panic" not in (r.stdout + r.stderr),
          "hlc: no panic on the path the user named")

    r = run([HLC, "--audit", missing])
    check(r.returncode == 2 and "error: cannot open file" in r.stdout,
          "hlc --audit: the same refusal on the audit path")

    hello = os.path.join(GATE, "hello.hls")
    wf(GATE, "hello.hls",
       'fn main() -> int uses IO {\n    println("hello rc2")\n    return 0\n}\n')

    r = run([HLC, hello, os.path.join(GATE, "no-such-dir", "out.c")])
    check(r.returncode == 2 and "cannot write file" in r.stdout,
          "hlc: an output destination without its directory refuses up "
          "front (exit 2)")
    check("panic" not in (r.stdout + r.stderr),
          "hlc: no post-codegen panic on the destination")

    r = run([HLC, "--symbols", os.path.join(GATE, "no-such-dir", "s.json"),
             hello, os.path.join(GATE, "out_ok.c")])
    check(r.returncode == 2 and "cannot write file" in r.stdout,
          "hlc: the --symbols destination refuses the same way")

    # the broken pipe: the interpreter dies like the native binary
    p1 = subprocess.Popen(BOOT + [hello], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    p2 = subprocess.Popen(["head", "-1"], stdin=p1.stdout,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p1.stdout.close()
    _ = p2.communicate()
    p1.wait(timeout=30)
    err = p1.stderr.read().decode("utf-8", "replace")
    check("BrokenPipeError" not in err and "Traceback" not in err,
          "boot.py: a closed stdout pipe dies silently (the native "
          "shape), no traceback")

    # ==================================================================
    header("2. the working paths — the refusals did not eat the road")
    r = run(BOOT + [hello])
    check(r.returncode == 0 and "hello rc2" in r.stdout,
          "boot.py: a program still runs")
    r = run(BOOT + ["--check", hello])
    check(r.returncode == 0 and "OK" in r.stdout,
          "boot.py: --check still holds")
    if NATIVE:
        out_c = os.path.join(GATE, "hello.c")
        r = run([HLC, hello, out_c])
        check(r.returncode == 0, "hlc: a program still compiles")
        r = run([HLC, "--audit", hello])
        check(r.returncode == 0, "hlc: --audit still audits")

    # ==================================================================
    header("3. the real-tree candidate laws — the freeze holds")
    r = rc("readiness")
    check(r.returncode == 0 and "READY" in r.stdout,
          "the REAL tree is ready")
    r = rc("gate")
    check(r.returncode == 0,
          "the gate over the live range names only BUGFIX and CHORE")
    head_v = ""
    with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
        for line in f:
            if line.startswith("## [v"):
                # the tool's convention: the heading's version without
                # the release `v` (the tag wears it, the heading does not)
                head_v = line[line.find("[v") + 2:line.find("]")]
                break
    man_v = json.load(open(os.path.join(ROOT, "snapshot", "MANIFEST.json"))).get("version", "")
    vs_v = json.load(open(os.path.join(ROOT, "editors", "vscode",
                                       "halis", "package.json"))).get("version", "")
    check(man_v == "v" + head_v and vs_v == head_v,
          "the three markers speak with one voice (%s)" % head_v)

    # ==================================================================
    header("4. the successor rehearsal — the RC2 cut on a scratch world")
    repo = scratch_world("rc2-rehearsal", "1.0.0-rc.1", 146,
                         with_anchor=True)
    anchor_before = json.loads(rf(repo, ".hls-rc.json"))
    check(anchor_before.get("tag") == "v1.0.0-rc.1",
          "the scratch world carries the rc.1 anchor")
    check(anchor_before.get("next") == "v1.0.0-rc.2",
          "the anchor names rc.2 as the successor")

    # the RC2 choreography: Stage 147 ticked, the markers moved, the
    # old anchor leaves with the release commit
    wf(repo, "ROADMAP.md", roadmap(147))
    wf(repo, "CHANGELOG.md",
       "## [v1.0.0-rc.2] — Stage 147: the bug-fix round\n- the body\n")
    wf(repo, "snapshot/MANIFEST.json",
       json.dumps({"closure": {}, "version": "v1.0.0-rc.2"}))
    wf(repo, "editors/vscode/halis/package.json",
       json.dumps({"name": "halis", "version": "1.0.0-rc.2"}))
    git(repo, "rm", "-q", ".hls-rc.json")
    rc1_sha = anchor_before.get("sha", "")
    commit_all(repo, "docs: stage 147 markers, the successor line")

    r = rc("declare", "--tag", "v1.0.0-rc.2", "--root", repo)
    check(r.returncode == 0,
          "rc.2 declares on the successor line (the old anchor left "
          "with the release commit)")
    if r.returncode != 0:
        print((r.stdout + r.stderr)[:400])
    else:
        anchor = json.loads(rf(repo, ".hls-rc.json"))
        check(anchor.get("tag") == "v1.0.0-rc.2"
              and anchor.get("stage") == 147,
              "the fresh anchor records rc.2 at Stage 147")
        check(anchor.get("next") == "v1.0.0-rc.3",
              "the successor of rc.2 is rc.3")
        r = rc("report", "--out", "RC.md", "--root", repo)
        check(r.returncode == 0,
              "the record renders from the fresh anchor")
        commit_all(repo, "release: v1.0.0-rc.2 anchored, the record rendered")
        r = rc("gate", "--range", rc1_sha + "..HEAD", "--root", repo)
        check(r.returncode == 0,
              "the gate over the whole rc.1..rc.2 range is green")
        r = rc("report", "--check", "--root", repo)
        check(r.returncode == 0,
              "the record is the anchored render (drift refused)")

    # ==================================================================
    header("5. the corpus showcase — the RC2 laws in pure HLS")
    # the formatter's canonical form, checked directly
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
