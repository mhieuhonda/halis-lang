#!/usr/bin/env python3
"""hls-compat — the Stage 128 backwards-compatibility verifier.

Usage:
  python3 tools/hls-compat.py                     # verify the union corpus
  python3 tools/hls-compat.py --tags v0.1.0,v0.70.0-alpha
  python3 tools/hls-compat.py --native            # + the native differential leg
  python3 tools/hls-compat.py --time-travel       # + the sampled history witness
  python3 tools/hls-compat.py --time-travel v0.1.0,v0.34.0-alpha
  python3 tools/hls-compat.py --discover          # the tag census, no runs
  python3 tools/hls-compat.py --json report.json  # machine-readable report
  python3 tools/hls-compat.py --update-ledger     # print the failing set

Stage 128's question is the one every growing language eventually has
to answer honestly: does a program that compiled and ran green on
release vN still compile and run green TODAY? The roadmap phrase is
"every prior version's ok/" — the acceptance corpus each release
shipped, replayed against the current toolchain. This tool replays it
and refuses to believe the answer, it measures it:

  1. the CENSUS — every release tag in the repository is discovered
     (v<major>.<minor>[.<patch>][-suffix]; non-release tags are counted
     and named, never silently dropped) and its tests/ok corpus is
     extracted with `git archive` — no worktree checkout, no dirty
     tree. The corpora are content-addressed: a fixture that never
     changed between two releases is verified ONCE (its import closure
     is part of its identity, so a fixture whose imported sibling
     changed is a different program and re-runs). 170 releases collapse
     to a few hundred unique programs — the whole history replays in
     minutes, which is what makes the suite per-commit runnable.

  2. the INTERPRETER leg — every unique historical program runs through
     TODAY'S Stage-0 interpreter: today's checker, today's effects,
     today's stdlib (std./core. imports resolve to the current repo;
     relative imports resolve inside the extracted historical tree, so
     a fixture runs with the siblings it shipped with). The bar is the
     suite's own convention: exit 0, or exit 101 for the expected-panic
     fixtures. Anything else is a failure, named with its first error
     line.

  3. the LEDGER — failures are diffed against the sanctioned-drift
     registry (tests/compat_ledger.json), never waved through. A
     failure the ledger names is WAIVED (the sanctioned change is
     quoted back at you); a failure the ledger does not name is a
     REGRESSION — the alarm this stage exists to sound. The diff is
     two-way: a ledgered program that runs green again is RECONCILED
     (the waiver is stale and must be trimmed), and a ledger entry that
     matches no historical content is STALE. Entries come in two
     kinds: "break" (it ran green at its release; today it fails) and
     "resurrect" (it was red at its release — a toolchain bug;
     today it runs green — the drift must be owned, not accidental).
     The ledger is a living document, not an archive of excuses.

  4. the NATIVE leg (--native) — every program whose interpreter leg
     was green is compiled with the house recipe (bin/hlc + gcc -O2
     -lm -pthread; the freestanding recipe for #![freestanding]
     crates) and run; native (exit, stdout) must equal interpreter
     (exit, stdout) byte for byte. The differential suite's discipline
     extended across time: an old program must still produce the same
     result on BOTH of today's backends.

  5. the TIME-TRAVEL leg (--time-travel) — the strongest witness: for
     sampled tags the FULL tree at that tag is extracted and the tag's
     OWN interpreter runs its own corpus (its world, its stdlib — what
     vN actually shipped and tested), beside today's interpreter on the
     same programs (the census layout — today's stdlib). Exit code and
     stdout must agree byte for byte, with exactly the sanctioned
     exceptions the ledger names — and the witnessing runs BOTH ways:
     a "break" entry is checked to have really run green at its
     release, a "resurrect" entry to have really been red (an
     unnamed resurrection — a red-at-release program that runs green
     today — is a problem to own, not a pass). A program that fails
     under its own release's interpreter is an OLD-BUG, reported
     honestly and excluded from the comparison. 'all' sweeps every
     release (the one-time full-history audit the ledger was built
     from); the default samples five tags — the bookends plus one per
     sanctioned-drift family.

Exit codes: 0 = COMPATIBLE (every historical program green or
sanctioned-waived, both backends agreeing, history agreeing); 1 = not
compatible (a regression, a native divergence, a history divergence, or
ledger drift — the reason is named); 2 = usage (not a git repository,
unknown tags, missing/corrupt ledger, missing bin/hlc or gcc, bad
option values).

The default leg needs only the interpreter (repo + git). --native
needs bin/hlc + gcc (run make bootstrap first). --time-travel samples
five tags by default — the two bookends plus one tag per sanctioned
change family, so every waiver's history is checked every time the leg
runs.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOOL_VERSION = "0.146.0-alpha"

LEDGER_PATH = os.path.join(REPO_ROOT, "tests", "compat_ledger.json")
LEDGER_SCHEMA = 1

# Release tags: v<major>.<minor>[.<patch>][-suffix]. Anything else
# (deep-scan-7) is a working tag — counted and named, never a release.
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)(?:\.(\d+))?(?:[.-]([A-Za-z0-9.\-]+))?$")

_REL_IMPORT_RE = re.compile(r'^\s*import\s+"([^"]+)"')

# The default time-travel sample: the two bookends (the oldest and
# the latest release — dynamic, so the sample stays honest as the
# history grows) plus one tag per sanctioned-drift family (the html
# forward-slash escaping + the Hieu-Louis-to-Halis data rename, the
# import guard, the asm register widths + the xxHash spec fix, the
# hex64 word rendering + the config quoted-dot-key bug) — every
# waiver's history is witnessed every run.
TRAVEL_FAMILY_TAGS = ["v0.7.0-alpha", "v0.34.0-alpha",
                      "v0.70.0-alpha", "v0.103.0-alpha"]


def _default_travel(releases):
    sample = [releases[0], releases[-1]] + \
        [t for t in TRAVEL_FAMILY_TAGS if t in releases]
    seen, out = set(), []
    for t in sample:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _sha(data):
    return hashlib.sha256(data).hexdigest()[:16]


def _clean_env():
    """The interpreter must see the default world: no HL_* mode vars
    (det/rt/rss arming, package dep dirs) leak into a compat run."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("HL_"):
            del env[k]
    return env


def _vkey(tag):
    m = _TAG_RE.match(tag)
    return (int(m.group(1)), int(m.group(2)),
            int(m.group(3) or 0), m.group(4) or "")


def _git(args, **kw):
    return subprocess.run(["git"] + args, cwd=REPO_ROOT,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **kw)


def discover_tags():
    """(release tags sorted, non-release tags) — the census never drops
    a tag silently."""
    p = _git(["tag"])
    if p.returncode != 0:
        sys.stderr.write("hls-compat: git tag failed — is %s a git "
                         "repository?\n" % REPO_ROOT)
        sys.exit(2)
    releases, others = [], []
    for t in p.stdout.decode("utf-8", "replace").split():
        if _TAG_RE.match(t):
            releases.append(t)
        else:
            others.append(t)
    releases.sort(key=_vkey)
    return releases, others


def extract_tree(tag, dest, pathspec):
    """git archive <tag> [pathspec] -> dest. A None pathspec extracts
    the FULL tree (the time-travel leg). Returns True on success."""
    cmd = ["archive", tag] + ([pathspec] if pathspec is not None else [])
    p = _git(cmd)
    if p.returncode != 0 or not p.stdout:
        return False
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # python < 3.12: no filter arg
            tf.extractall(dest)
    return True


def _rel_import_paths(src_text):
    """Raw relative import paths (non-std./core.) in source order."""
    out = []
    for line in src_text.splitlines():
        m = _REL_IMPORT_RE.match(line)
        if m:
            path = m.group(1)
            if not path.startswith("std.") and not path.startswith("core."):
                out.append(path)
    return out


def _context_hash(filepath, tree_root):
    """Hash of the file + its relative-import closure (the siblings it
    shipped with). Paths are hashed RELATIVE to the extracted tree so
    two releases carrying identical content hash identically (the temp
    extraction dir's name must not leak into the identity). None when
    an import escapes the extracted tree — the caller falls back to the
    whole-tree manifest (the program is its entire context then)."""
    h = hashlib.sha256()
    stack, seen = [filepath], set()
    while stack:
        fp = stack.pop()
        rp = os.path.realpath(fp)
        if rp in seen:
            continue
        seen.add(rp)
        try:
            data = open(fp, "rb").read()
        except OSError:
            return None
        h.update(os.path.relpath(rp, tree_root).encode("utf-8",
                                                       "replace"))
        h.update(data)
        text = data.decode("utf-8", "replace")
        for p in _rel_import_paths(text):
            cand = os.path.normpath(os.path.join(os.path.dirname(fp), p))
            base = os.path.dirname(filepath)
            if not (cand == base or cand.startswith(base + os.sep)):
                return None  # escapes the tree (the old '..' imports)
            if not os.path.isfile(cand):
                return None  # unresolved inside the tree
            stack.append(cand)
    return h.hexdigest()[:16]


class Census(object):
    """The union corpus: every program every release ever shipped.

    Per tag, tests/ok is extracted; every top-level .hls fixture (the
    suite_01 convention — modules/ holds imported siblings, not
    entries) gets a run key: its own sha16, plus the closure hash when
    it has relative imports (a fixture whose sibling changed is a
    different program). The FIRST tree that produced a key owns the
    run's concrete path; later tags just attribute themselves.
    """

    def __init__(self, tmp_root):
        self.tmp_root = tmp_root
        self.plan = {}        # key -> record
        self.tag_corpora = {}  # tag -> [(file, sha, key)]

    def scan_tag(self, tag):
        dest = os.path.join(self.tmp_root, "corpus_" + tag.replace(".", "_"))
        okdir = os.path.join(dest, "tests", "ok")
        if not extract_tree(tag, dest, "tests/ok"):
            self.tag_corpora[tag] = []
            return
        files = sorted(f for f in os.listdir(okdir)
                       if f.endswith(".hls")
                       and os.path.isfile(os.path.join(okdir, f)))
        entries = []
        for f in files:
            fp = os.path.join(okdir, f)
            src = open(fp, "rb").read()
            sha = _sha(src)
            rels = _rel_import_paths(
                src.decode("utf-8", "replace"))
            if not rels:
                key = sha
            else:
                ctx = _context_hash(fp, dest)
                if ctx is None:
                    manifest = hashlib.sha256()
                    for root, _d, fnames in os.walk(okdir):
                        for fn in sorted(fnames):
                            fpx = os.path.join(root, fn)
                            manifest.update(
                                os.path.relpath(fpx, okdir).encode())
                            manifest.update(open(fpx, "rb").read())
                    key = sha + ":" + manifest.hexdigest()[:16]
                else:
                    key = sha + ":" + ctx
            entries.append((f, sha, key))
            rec = self.plan.get(key)
            if rec is None:
                self.plan[key] = {
                    "file": f, "sha": sha, "path": fp,
                    "rel_imports": bool(rels), "tags": [tag],
                }
            else:
                rec["tags"].append(tag)
        self.tag_corpora[tag] = entries

    def stats(self):
        instances = sum(len(v) for v in self.tag_corpora.values())
        return instances, len(self.plan)


def run_interpreter(path, timeout_s):
    """One green-check run: (rc, stdout, first error line)."""
    cmd = [sys.executable, os.path.join(REPO_ROOT, "tools", "hlinterp", "boot.py"), path]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout_s,
                           cwd=REPO_ROOT, env=_clean_env())
    except subprocess.TimeoutExpired:
        return "timeout", b"", "timeout after %ds" % timeout_s
    err = p.stderr.decode("utf-8", "replace").splitlines()
    first = next((l for l in err if l.strip()), "")
    return p.returncode, p.stdout, first


def compile_and_run_native(src, workdir, name, timeout_s):
    """The house recipe (freestanding-aware). Returns (rc, stdout) or
    raises RuntimeError with the failing stage named."""
    hlc = os.path.join(REPO_ROOT, "bin", "hlc")
    if not (os.path.isfile(hlc) and os.access(hlc, os.X_OK)):
        raise RuntimeError("no bin/hlc under %s — run make bootstrap"
                           % REPO_ROOT)
    if not shutil.which("gcc"):
        raise RuntimeError("gcc not found on PATH")
    c_path = os.path.join(workdir, name + ".c")
    bin_path = os.path.join(workdir, name)
    comp = subprocess.run([hlc, src, c_path], cwd=REPO_ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if comp.returncode != 0:
        raise RuntimeError("hlc compile failed on %s (exit %d)"
                           % (os.path.basename(src), comp.returncode))
    head = open(src, encoding="utf-8", errors="replace").read(400)
    if "#![freestanding]" in head:
        gcc = subprocess.run(
            ["gcc", "-O2", "-ffreestanding", "-nostdlib",
             "-ffunction-sections", "-fno-stack-protector",
             "-Wl,--gc-sections", "-o", bin_path, c_path],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        gcc = subprocess.run(
            ["gcc", "-O2", "-o", bin_path, c_path, "-lm", "-pthread"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if gcc.returncode != 0:
        raise RuntimeError("gcc failed on %s (exit %d)"
                           % (os.path.basename(src), gcc.returncode))
    try:
        p = subprocess.run([bin_path], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout_s, cwd=REPO_ROOT,
                           env=_clean_env())
    except subprocess.TimeoutExpired:
        raise RuntimeError("native run timeout after %ds on %s"
                           % (timeout_s, os.path.basename(src)))
    return p.returncode, p.stdout


def load_ledger(path):
    """The sanctioned-drift registry: strict schema, loud failures.

    Three kinds of entry, all verified against history by the
    time-travel leg:
      kind "break"     — the program ran green under its own release;
                         today's toolchain fails it (or panics it). A
                         regression the ledger names.
      kind "resurrect" — the program was RED under its own release (a
                         toolchain bug, usually); today it runs green.
                         An unnamed resurrection is a problem — the
                         change of behaviour must be owned, not
                         accidental.
      kind "drift"     — the program still exits 0 on both toolchains
                         but its OUTPUT changed (a formatting or data
                         change). The entry pins BOTH stdout digests —
                         the old and the new — and the last release
                         that still produced the old one, so any
                         FURTHER drift of the same program fails the
                         witness.
    Break and resurrect entries may carry "old_red_from": the tag
    from which the program's OWN release interpreter already showed
    the drift-era verdict — for a break, the migration gap (the
    breaking change landed but the fixture had not yet been migrated:
    those releases shipped a corpus entry their own interpreter
    rejected); for a resurrect, the bug's landing (green before it,
    red from it).
    """
    if not os.path.isfile(path):
        sys.stderr.write("hls-compat: no ledger at %s — the sanctioned-"
                         "drift registry is part of the stage; run with "
                         "--update-ledger to draft one\n" % path)
        sys.exit(2)
    try:
        data = json.load(open(path))
    except (ValueError, OSError) as ex:
        sys.stderr.write("hls-compat: ledger does not parse (%s)\n" % ex)
        sys.exit(2)
    if not isinstance(data, dict) or data.get("schema") != LEDGER_SCHEMA \
            or not isinstance(data.get("waivers"), list):
        sys.stderr.write("hls-compat: ledger schema mismatch — expected "
                         "schema %d with a waivers[] list\n" % LEDGER_SCHEMA)
        sys.exit(2)
    by_key = {}
    for w in data["waivers"]:
        for field in ("file", "sha256_16", "sanctioned_change", "reason"):
            if not isinstance(w.get(field), str) or not w[field].strip():
                sys.stderr.write("hls-compat: ledger entry missing '%s':"
                                 " %s\n" % (field, json.dumps(w)[:120]))
                sys.exit(2)
        kind = w.get("kind", "break")
        if kind not in ("break", "resurrect", "drift"):
            sys.stderr.write("hls-compat: ledger entry for %s has unknown "
                             "kind %r (break|resurrect|drift)\n"
                             % (w["file"], kind))
            sys.exit(2)
        w["kind"] = kind
        gap = w.get("old_red_from")
        if gap is not None and (not isinstance(gap, str)
                                or _TAG_RE.match(gap) is None):
            sys.stderr.write("hls-compat: ledger entry for %s has a "
                             "malformed old_red_from %r (a release "
                             "tag or null)\n" % (w["file"], gap))
            sys.exit(2)
        if kind == "drift":
            _hex16 = re.compile(r"^[0-9a-f]{16}$")
            for field in ("old_stdout_16", "new_stdout_16"):
                if not _hex16.match(str(w.get(field, ""))):
                    sys.stderr.write("hls-compat: drift entry for %s needs "
                                     "a 16-hex %s\n" % (w["file"], field))
                    sys.exit(2)
            until = w.get("drift_until")
            if not isinstance(until, str) or _TAG_RE.match(until) is None:
                sys.stderr.write("hls-compat: drift entry for %s needs a "
                                 "release tag in drift_until\n" % w["file"])
                sys.exit(2)
        by_key[(w["file"], w["sha256_16"])] = w
    return data, by_key


def _toolchain_fingerprint(tree_root):
    """Content fingerprint of a release's own interpreter world (boot/
    in old trees, tools/hlinterp/ in current ones, plus std/ and
    core/). Two releases with the same fingerprint run the same file
    content identically — the sweep memoizes on this."""
    h = hashlib.sha256()
    paths = []
    for sub in ("boot", "tools/hlinterp", "std", "core"):
        d = os.path.join(tree_root, sub)
        if not os.path.isdir(d):
            continue
        for root, _dirs, fnames in os.walk(d):
            for fn in fnames:
                paths.append(os.path.join(root, fn))
    for fp in sorted(paths):
        h.update(os.path.relpath(fp, tree_root).encode("utf-8",
                                                       "replace"))
        h.update(open(fp, "rb").read())
    return h.hexdigest()[:16]


# Why the old leg has NO parallel mode (a lesson the first sweep
# taught): the corpus contains port-binding fixtures (a fixed listen
# port — 127.0.0.1:18037) and timing-sensitive handshakes (a client
# retrying ~500ms while a server thread races to listen). Two
# interpreters racing for the same port — or the same fixture slowed
# past its handshake budget by CPU contention — turn a green corpus
# into a spurious panic: parallelism could manufacture failures that
# never happened. A verifier must not have a mode that can falsify
# history; the historical runs are strictly sequential.


def _run_old_interpreter(old_boot, old_path, oldtree, timeout_s):
    """One historical run: (rc, stdout) — rc 'timeout' on timeout."""
    try:
        p = subprocess.run(
            [sys.executable, old_boot, old_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout_s, cwd=oldtree,
            env=_clean_env())
    except subprocess.TimeoutExpired:
        return "timeout", b""
    return p.returncode, p.stdout


def main():
    ap = argparse.ArgumentParser(add_help=True, prog="hls-compat",
                                 description="Stage 128 backwards-"
                                 "compatibility verifier")
    ap.add_argument("--tags", metavar="T1,T2,...",
                    help="restrict the census to these release tags")
    ap.add_argument("--timeout", type=int, default=60, metavar="SEC",
                    help="per-run interpreter timeout (default 60)")
    ap.add_argument("--native", action="store_true",
                    help="add the native differential leg (needs bin/hlc "
                         "+ gcc)")
    ap.add_argument("--time-travel", nargs="?", const="default",
                    metavar="T1,T2,...|all",
                    help="add the history witness leg (default: the "
                         "oldest + latest release and one tag per "
                         "sanctioned-drift family; 'all' sweeps every "
                         "release)")
    ap.add_argument("--discover", action="store_true",
                    help="print the tag census and exit (no runs)")
    ap.add_argument("--json", metavar="FILE",
                    help="write a machine-readable report")
    ap.add_argument("--update-ledger", action="store_true",
                    help="print the current failing set as a ledger draft "
                         "(review, add reasons, splice by hand)")
    args = ap.parse_args()

    if args.timeout < 1:
        sys.stderr.write("hls-compat: --timeout must be >= 1 second\n")
        return 2

    releases, others = discover_tags()
    if not releases:
        sys.stderr.write("hls-compat: no release tags found — nothing "
                         "to verify\n")
        return 2

    if args.tags:
        wanted = [t.strip() for t in args.tags.split(",") if t.strip()]
        missing = [t for t in wanted if t not in releases]
        if missing:
            sys.stderr.write("hls-compat: not a release tag: %s\n"
                             % ", ".join(missing))
            return 2
        releases = sorted(wanted, key=_vkey)

    # ---- the census (no ledger needed for --discover / --update-ledger)
    tmp_root = tempfile.mkdtemp(prefix="hls-compat-")
    census = Census(tmp_root)
    for tag in releases:
        census.scan_tag(tag)
    instances, unique = census.stats()

    if args.discover:
        print("hls-compat: %d release tags, %d non-release tags ignored "
              "(%s)" % (len(releases), len(others),
                        ", ".join(others) or "none"))
        for tag in releases:
            n = len(census.tag_corpora[tag])
            print("  %-24s %3d fixtures" % (tag, n))
        print("hls-compat: %d file-instances -> %d unique programs "
              "(dedup %.1fx)"
              % (instances, unique,
                 (instances / unique) if unique else 0.0))
        shutil.rmtree(tmp_root, ignore_errors=True)
        return 0

    if args.update_ledger:
        print("hls-compat: drafting the ledger from the CURRENT failing "
              "set (%d unique programs, %d file-instances)..."
              % (unique, instances))
        draft = []
        for key, rec in sorted(census.plan.items()):
            rc, _out, err = run_interpreter(rec["path"], args.timeout)
            if rc in (0, 101):
                continue
            draft.append({"file": rec["file"], "sha256_16": rec["sha"],
                          "sanctioned_change": "UNREVIEWED",
                          "reason": err[:160],
                          "migrated_in": "UNREVIEWED"})
            print("  FAIL %-28s %s" % (rec["file"], rec["sha"]))
        print("---- ledger draft (tests/compat_ledger.json) ----")
        print(json.dumps({"schema": LEDGER_SCHEMA, "waivers": draft},
                         indent=2))
        print("---- end draft ----")
        print("hls-compat: review every entry, name the sanctioned change "
              "and the release that migrated the fixture, then splice "
              "into tests/compat_ledger.json — the tool never writes the "
              "ledger itself")
        shutil.rmtree(tmp_root, ignore_errors=True)
        return 0

    ledger, waivers = load_ledger(LEDGER_PATH)

    report = {
        "tool": "hls-compat",
        "version": TOOL_VERSION,
        "config": {
            "tags": list(releases),
            "timeout_s": args.timeout,
            "native": args.native,
            "time_travel": (args.time_travel if args.time_travel
                            else None),
        },
        "census": {
            "release_tags": len(releases),
            "non_release_tags": others,
            "file_instances": instances,
            "unique_programs": unique,
        },
        "verdict": None,
        "reasons": [],
        "contents": {},
        "native": None,
        "time_travel": None,
    }

    # ---- the interpreter leg: every unique program, today's world
    t0 = time.time()
    counts = {"compatible": 0, "compatible_panic": 0, "waived": 0,
              "regression": 0, "reconciled": 0}
    for key, rec in sorted(census.plan.items(),
                           key=lambda kv: (kv[1]["file"], kv[1]["sha"])):
        rc, out, err = run_interpreter(rec["path"], args.timeout)
        waiver = waivers.get((rec["file"], rec["sha"]))
        rec["rc"] = rc
        rec["stdout_sha"] = _sha(out)
        rec["err"] = err if rc not in (0, 101) else ""
        if rc == 0:
            if waiver is not None and waiver["kind"] == "break":
                # the ledger says today fails this program; it does not —
                # the break-waiver is stale and must be trimmed
                verdict = "RECONCILED"
            else:
                verdict = "COMPATIBLE"
        elif rc == 101:
            # the expected-panic convention's green (the suite's own
            # since v0.1.0). A behavioural break that manifests as a
            # panic — a program that ran to completion at its release
            # and panics today — is invisible to this leg by design;
            # the time-travel leg is the one that sees it, and the
            # ledger entry that names it.
            verdict = "COMPATIBLE-PANIC"
        else:
            if waiver is not None and waiver["kind"] == "break":
                verdict = "WAIVED"
            else:
                # unledgered — or a resurrect-waiver whose "green
                # today" claim just broke. Either way: loud.
                verdict = "REGRESSION"
        bucket = {"COMPATIBLE": "compatible",
                  "COMPATIBLE-PANIC": "compatible_panic",
                  "WAIVED": "waived", "REGRESSION": "regression",
                  "RECONCILED": "reconciled"}[verdict]
        counts[bucket] += 1
        rec["verdict"] = verdict
        rec["stdout"] = out  # kept for the native comparison
    duration = time.time() - t0

    # Aggregate run-contexts into PROGRAMS (file, sha): one historical
    # program may appear in many import contexts (a fixture whose
    # sibling changed re-runs per context); the verdict that matters
    # is the worst context it was ever seen in.
    _WORST = {"COMPATIBLE": 0, "COMPATIBLE-PANIC": 1, "RECONCILED": 2,
              "WAIVED": 3, "REGRESSION": 4}
    agg = {}
    for rec in census.plan.values():
        a = agg.get((rec["file"], rec["sha"]))
        if a is None:
            a = {"file": rec["file"], "sha": rec["sha"],
                 "verdict": rec["verdict"], "contexts": 0,
                 "tags": [], "exit": rec["rc"],
                 "error": rec["err"][:200]}
            agg[(rec["file"], rec["sha"])] = a
        a["contexts"] += 1
        a["tags"] = sorted(set(a["tags"]) | set(rec["tags"]), key=_vkey)
        if _WORST[rec["verdict"]] > _WORST[a["verdict"]]:
            a["verdict"] = rec["verdict"]
            a["exit"] = rec["rc"]
            a["error"] = rec["err"][:200]
    programs = sorted(agg.values(), key=lambda a: (a["file"], a["sha"]))
    prog_counts = {}
    for a in programs:
        bucket = {"COMPATIBLE": "compatible",
                  "COMPATIBLE-PANIC": "compatible_panic",
                  "WAIVED": "waived", "REGRESSION": "regression",
                  "RECONCILED": "reconciled"}[a["verdict"]]
        prog_counts[bucket] = prog_counts.get(bucket, 0) + 1
        report["contents"].setdefault(a["verdict"], []).append(
            {"file": a["file"], "sha": a["sha"], "tags": a["tags"],
             "contexts": a["contexts"], "exit": a["exit"],
             "error": a["error"]})

    # A (file, sha) -> representative record map for the legs below.
    by_program = {(rec["file"], rec["sha"]): rec
                  for rec in census.plan.values()}

    print("hls-compat: %d release tags censused%s"
          % (len(releases),
             (" (%d non-release tags ignored: %s)"
              % (len(others), ", ".join(others))) if others else ""))
    print("hls-compat: union corpus: %d file-instances -> %d unique "
          "programs (dedup %.1fx)"
          % (instances, unique,
             (instances / unique) if unique else 0.0))
    print("hls-compat: interpreter leg: %d run-contexts green (%d "
          "exit-0, %d expected-panic), %d fail; %d distinct programs "
          "[%d tags/%.0fs]"
          % (counts["compatible"] + counts["compatible_panic"],
             counts["compatible"], counts["compatible_panic"],
             counts["waived"] + counts["regression"],
             len(programs), len(releases), duration))

    problems = []
    for a in programs:
        if a["verdict"] == "WAIVED":
            w = waivers[(a["file"], a["sha"])]
            print("  WAIVED    %s@%s  %s..%s (%d tags, %d contexts)"
                  % (a["file"], a["sha"], a["tags"][0],
                     a["tags"][-1], len(a["tags"]), a["contexts"]))
            print("            %s" % w["sanctioned_change"])
        elif a["verdict"] == "REGRESSION":
            problems.append("REGRESSION %s@%s (exit %s): %s"
                            % (a["file"], a["sha"], a["exit"],
                               a["error"] or "no error output"))
        elif a["verdict"] == "RECONCILED":
            problems.append("RECONCILED %s@%s runs green — trim the "
                            "ledger entry" % (a["file"], a["sha"]))

    # Ledger entries that match nothing in the census. On the FULL
    # census that means history was rewritten or the entry was
    # invented — loud. On a restricted --tags run the entry may simply
    # predate or postdate the sampled releases — said so, not failed:
    # the full run is the certificate, the restricted run a probe.
    seen = set((r["file"], r["sha"]) for r in census.plan.values())
    stale = [w for k, w in sorted(waivers.items()) if k not in seen]
    for w in stale:
        if args.tags is None:
            problems.append("STALE ledger entry %s@%s matches no "
                            "historical content"
                            % (w["file"], w["sha256_16"]))
        else:
            print("  NOTE     %s@%s not censused (restricted run)"
                  % (w["file"], w["sha256_16"]))

    # ---- the native differential leg (--native)
    if args.native:
        nat_work = os.path.join(tmp_root, "native")
        os.makedirs(nat_work, exist_ok=True)
        ndiv, nran = [], 0
        for key, rec in sorted(census.plan.items(),
                               key=lambda kv: (kv[1]["file"],
                                               kv[1]["sha"])):
            if rec["verdict"] not in ("COMPATIBLE", "COMPATIBLE-PANIC"):
                continue  # waived/regressed programs cannot compile today
            nran += 1
            try:
                nrc, nout = compile_and_run_native(
                    rec["path"], nat_work, "n_" + rec["sha"], 120)
            except RuntimeError as ex:
                ndiv.append("%s@%s: %s" % (rec["file"], rec["sha"], ex))
                continue
            if nrc != rec["rc"] or nout != rec["stdout"]:
                ndiv.append("%s@%s: native exit %d vs interpreter %d; "
                            "stdout %s vs %s"
                            % (rec["file"], rec["sha"], nrc, rec["rc"],
                               _sha(nout), rec["stdout_sha"]))
        report["native"] = {"ran": nran, "divergences": ndiv}
        print("hls-compat: native leg: %d programs compiled and run; "
              "%d divergences" % (nran, len(ndiv)))
        for d in ndiv:
            problems.append("NATIVE-DIVERGENCE " + d)

    # ---- the time-travel leg (--time-travel)
    if args.time_travel:
        if args.time_travel == "default":
            travel_tags = _default_travel(releases)
        elif args.time_travel == "all":
            travel_tags = list(releases)
        else:
            travel_tags = [t.strip() for t in args.time_travel.split(",")
                           if t.strip()]
            missing = [t for t in travel_tags
                       if t not in census.tag_corpora]
            if missing:
                sys.stderr.write("hls-compat: not a release tag: %s\n"
                                 % ", ".join(missing))
                return 2
        tt = {}
        oldmemo = {}   # (run key, toolchain fingerprint) -> (rc, stdout)
        for tag in travel_tags:
            oldtree = os.path.join(tmp_root,
                                   "tt_" + tag.replace(".", "_"))
            if not extract_tree(tag, oldtree, None):
                sys.stderr.write("hls-compat: git archive failed on "
                                 "%s\n" % tag)
                return 2
            old_boot = os.path.join(oldtree, "boot", "boot.py")
            old_ok = os.path.join(oldtree, "tests", "ok")
            if not (os.path.isfile(old_boot) and os.path.isdir(old_ok)):
                sys.stderr.write("hls-compat: %s ships no Stage-0 "
                                 "interpreter — cannot time-travel\n"
                                 % tag)
                return 2
            fingerprint = _toolchain_fingerprint(oldtree)
            agree = break_waived = resurrect_waived = drift_waived = 0
            oldbugs, divs, resurrections = [], [], []
            # The historical runs first (memoised across tags), then
            # the sequential, deterministic classification over their
            # results. The runs themselves are strictly sequential —
            # see the comment above _run_old_interpreter for why this
            # leg has no parallel mode.
            file_key = {f: key
                        for f, _sha16, key
                        in census.tag_corpora[tag]}
            pending = []
            for f in file_key:
                old_path = os.path.join(old_ok, f)
                if not os.path.isfile(old_path):
                    continue
                if (file_key[f], fingerprint) not in oldmemo:
                    pending.append((f, old_path))
            for f, old_path in pending:
                oldmemo[(file_key[f], fingerprint)] = \
                    _run_old_interpreter(old_boot, old_path, oldtree,
                                         args.timeout)
            for f, sha, key in census.tag_corpora[tag]:
                old_path = os.path.join(old_ok, f)
                if not os.path.isfile(old_path):
                    continue
                memo_key = (key, fingerprint)
                if memo_key in oldmemo:
                    orc, oout = oldmemo[memo_key]
                else:
                    orc, oout = _run_old_interpreter(old_boot,
                                                     old_path,
                                                     oldtree,
                                                     args.timeout)
                    oldmemo[memo_key] = (orc, oout)
                if orc == "timeout":
                    oldbugs.append((f, "old timeout"))
                    continue
                # Today's verdict for THIS tag's own copy — the census
                # already ran it in exactly this import context, so the
                # comparison is like-for-like, not against a verdict
                # from a different tree.
                rec = census.plan[key]
                nrc, nout = rec["rc"], rec["stdout"]
                w = waivers.get((f, sha))
                gap = (w or {}).get("old_red_from")
                in_red_era = (gap is not None and _vkey(tag) >= _vkey(gap))
                if w is not None and w["kind"] == "drift":
                    if _vkey(tag) <= _vkey(w["drift_until"]):
                        # The claim, verified against history: both
                        # toolchains exit 0 and the stdout digests are
                        # EXACTLY the two the entry pins — any further
                        # drift of the same program fails here.
                        if (orc == 0 and nrc == 0
                                and _sha(oout) == w["old_stdout_16"]
                                and _sha(nout) == w["new_stdout_16"]):
                            drift_waived += 1
                        else:
                            problems.append(
                                "WAIVER-DRIFT %s@%s at %s: the ledger "
                                "pinned old stdout %s / today stdout %s, "
                                "but history shows old exit %s stdout %s "
                                "and today exit %s stdout %s"
                                % (f, sha, tag, w["old_stdout_16"],
                                   w["new_stdout_16"], orc, _sha(oout),
                                   nrc, _sha(nout)))
                        continue
                    # After the change landed, old and new agree — the
                    # waiver does not apply; the normal classification
                    # takes over.
                if w is not None and w["kind"] == "break":
                    # The claim, verified against history: the program
                    # ran to completion under its own release and does
                    # not run green today. With a migration gap
                    # (old_red_from), the gap-era releases are claimed
                    # the opposite way: the historical interpreter
                    # rejected the program too — the releases that
                    # shipped the breaking change but not yet the
                    # migrated fixture.
                    if in_red_era:
                        verified = (orc != 0 and nrc != 0)
                    else:
                        verified = (orc == 0 and nrc != 0)
                    if verified:
                        break_waived += 1
                    else:
                        problems.append(
                            "WAIVER-DRIFT %s@%s at %s: the ledger says a "
                            "sanctioned break, but history shows old exit "
                            "%s and today exit %s"
                            % (f, sha, tag, orc, nrc))
                    continue
                if w is not None and w["kind"] == "resurrect":
                    if not in_red_era:
                        # Before the bug landed the program was green
                        # under its own release AND is green today —
                        # plain agreement; the waiver does not apply
                        # and the normal classification takes over.
                        pass
                    else:
                        # The claim, verified against history: the
                        # program was red under its own release and is
                        # green today.
                        if orc != 0 and nrc == 0:
                            resurrect_waived += 1
                        else:
                            problems.append(
                                "WAIVER-DRIFT %s@%s at %s: the ledger "
                                "says a sanctioned resurrection, but "
                                "history shows old exit %s and today "
                                "exit %s" % (f, sha, tag, orc, nrc))
                        continue
                if orc not in (0, 101):
                    oldbugs.append((f, orc))
                    continue
                if orc == 0 and nrc == 0:
                    if oout == nout:
                        agree += 1
                    else:
                        divs.append("%s: old stdout %s; today stdout %s "
                                    "(both exit 0)"
                                    % (f, _sha(oout), _sha(nout)))
                elif orc == 0:
                    divs.append("%s: old exit 0 / stdout %s; today exit "
                                "%s / stdout %s — a working program "
                                "broke"
                                % (f, _sha(oout), nrc, _sha(nout)))
                elif nrc == 0:
                    resurrections.append(
                        "%s: red at its own release (exit %s), green "
                        "today — name it in the ledger (kind: resurrect) "
                        "or investigate" % (f, orc))
                elif oout == nout:
                    agree += 1     # both panic, same stdout: convention-identical
                else:
                    divs.append("%s: old exit 101 / stdout %s; today exit "
                                "%s / stdout %s"
                                % (f, _sha(oout), nrc, _sha(nout)))
            tt[tag] = {"agree": agree, "break_waived": break_waived,
                       "resurrect_waived": resurrect_waived,
                       "drift_waived": drift_waived,
                       "old_bugs": oldbugs, "divergences": divs,
                       "resurrections": resurrections}
            print("hls-compat: time-travel %s: %d agree, %d break-waived, "
                  "%d resurrect-waived, %d drift-waived (verified against "
                  "history), %d old bugs, %d divergences, %d resurrections"
                  % (tag, agree, break_waived, resurrect_waived,
                     drift_waived, len(oldbugs), len(divs),
                     len(resurrections)))
            for f, rc in oldbugs:
                print("            OLD-BUG %s (its own release exits %s)"
                      % (f, rc))
            for d in divs:
                problems.append("TRAVEL-DIVERGENCE %s: %s" % (tag, d))
            for r in resurrections:
                problems.append("RESURRECTED %s: %s" % (tag, r))
        report["time_travel"] = tt

    # ---- the verdict
    for p in problems:
        print("  PROBLEM  %s" % p)
    nprog = len(programs)
    ngreen = (prog_counts.get("compatible", 0)
              + prog_counts.get("compatible_panic", 0))
    nwaived = prog_counts.get("waived", 0)
    if not problems:
        print("hls-compat: verdict COMPATIBLE — %d/%d distinct programs "
              "green, %d waived under %d sanctioned drifts (ledger), 0 "
              "regressions%s%s"
              % (ngreen, nprog, nwaived, len(ledger["waivers"]),
                 "; native agrees" if args.native else "",
                 "; history agrees" if args.time_travel else ""))
        report["verdict"] = "COMPATIBLE"
        code = 0
    else:
        nreg = prog_counts.get("regression", 0)
        print("hls-compat: verdict NOT COMPATIBLE — %d regression(s) "
              "among %d distinct programs, %d problem(s) named above"
              % (nreg, nprog, len(problems)))
        report["verdict"] = "NOT COMPATIBLE"
        code = 1

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")

    shutil.rmtree(tmp_root, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
