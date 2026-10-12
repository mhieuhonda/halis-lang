#!/usr/bin/env python3
"""Stage 128 acceptance gate — hls-compat, the backwards-compatibility
suite over every prior ok corpus.

Run with `make compat-acceptance` (or
`python3 tests/compat_acceptance.py`).

Eight sections over the REAL verifier CLI (subprocess, the same
interface a user drives), the REAL git history (170 release tags),
the REAL ledger, and the verifier's own census arithmetic (imported
for the unit checks):

  1. the convention  — usage gates are exit 2 (unknown tag, unknown
     & the refusals    travel tag, --timeout 0); a missing, corrupt
                       or schema-mismatched ledger is exit 2, loudly

  2. the census      — --discover over the real repository: the
     & the keying       release-tag count, the non-release tag named
                       (never silently dropped), the dedup factor;
                       the keying laws unit-checked (version sort,
                       content addressing, import-closure context,
                       toolchain fingerprint, the clean env)

  3. the ledger      — the real registry: nine entries — five breaks,
     discipline         three drifts and one resurrection — each sha
                       spot-checked against the git object it names;
                       --update-ledger drafts a failing set honestly
                       (a green program cannot be drafted) and never
                       writes the file

  4. the             — the REAL CLI over the FULL census: exit 0,
     certification      COMPATIBLE, the json carrying the census, the
                       config and every verdict; a restricted probe
                       certifies a single release end to end

  5. the regression  — the alarm, proven three ways: a probe commit
     alarm              tagged into real history is caught as a
                       REGRESSION with its error line quoted (then
                       cleaned up, history restored); a ledger entry
                       removed turns a sanctioned break into a
                       REGRESSION; an entry pointed at a green program
                       is RECONCILED; an invented entry on the full
                       census is STALE — every direction of ledger
                       drift fails the verdict

  6. the time-travel — the history witness over six sampled releases
     witness            (the bookends plus one per drift family): the
                       hand-computed agreement matrix, every waiver
                       VERIFIED against the history it claims (the
                       break really ran green at its release; the
                       resurrection really was red; the drift's two
                       stdout digests really are what the entry pins),
                       zero divergences, zero unnamed resurrections

  7. the native      — 149 historical programs compiled with the
     differential       house recipe and run: native (exit, stdout)
                       byte-identical to the interpreter leg; the
                       stage's two new files differential-checked
                       directly

  8. the corpus      — the showcase and the demo are hlfmt-canonical,
     hygiene            hllint-clean, checker-green; the ledger parses
                       with exactly its six entries; no gate litter
                       survives anywhere the gate ran

Exit code 0 = all acceptance criteria met.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

COMPAT = [sys.executable, "tools/hls-compat.py"]
BOOT = [sys.executable, "boot/boot.py"]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

LEDGER = os.path.join(ROOT, "tests", "compat_ledger.json")
SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage128_compat.hls")
DEMO = os.path.join(ROOT, "examples", "compat_demo.hls")

# The hand-computed witness matrix. The four drift-family tags are
# frozen facts of history; the numbers were derived by running each
# release's own interpreter beside today's (the full-history sweep
# the ledger was built from). The bookends are dynamic — the oldest
# release and the LATEST one (the gate computes its corpus size at
# runtime; a release is born green, which is the census's own claim).
# agree + break + resurrect + drift + oldbugs = corpus.
FAMILY_TRAVEL = {
    "v0.7.0-alpha": {"corpus": 42, "agree": 39, "break": 1,
                     "resurrect": 0, "drift": 2},   # import guard;
    # html slash escape + data rename
    "v0.34.0-alpha": {"corpus": 103, "agree": 102, "break": 1,
                      "resurrect": 0, "drift": 0},   # feat_import ('..' path)
    "v0.70.0-alpha": {"corpus": 148, "agree": 145, "break": 3,
                      "resurrect": 0, "drift": 0},   # asm, perfection, xxHash
    "v0.103.0-alpha": {"corpus": 194, "agree": 192, "break": 1,
                       "resurrect": 1, "drift": 0},  # hex64; config quoted-dot-key
}

# The gate's own scratch namespace: any leftover _gate_s128_* dir is
# litter from a gate run that died before its cleanup — removed here
# so a crashed run never poisons the next one's clean-worktree probe.
for _stale in os.listdir(os.path.join(ROOT, "tests")):
    if _stale.startswith("_gate_s128_"):
        shutil.rmtree(os.path.join(ROOT, "tests", _stale),
                      ignore_errors=True)
TMP = tempfile.mkdtemp(prefix="_gate_s128_", dir=os.path.join(ROOT, "tests"))

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] %s" % msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  [FAIL] %s" % msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=kw.pop("timeout", 600), **kw)


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ledger_variant(object):
    """Temporarily replace the real ledger with a forged variant; the
    real bytes are ALWAYS restored (the gate never leaves the registry
    broken, even on a failed check)."""

    def __init__(self, mutate):
        self.mutate = mutate
        self.backup = open(LEDGER, "rb").read()

    def __enter__(self):
        data = json.loads(self.backup.decode("utf-8"))
        data = self.mutate(data)
        with open(LEDGER, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        return self

    def __exit__(self, *exc):
        with open(LEDGER, "wb") as fh:
            fh.write(self.backup)
        return False


def _section_1():
    # ===========================================================================
    print("Section 1 — the convention and the refusal matrix")

    p = run(COMPAT + ["--tags", "v0.999.0-nosuchtag"])
    check(p.returncode == 2 and "not a release tag" in p.stderr,
          "an unknown tag is a usage failure (exit 2)")

    p = run(COMPAT + ["--timeout", "0", "--tags", "v0.1.0"])
    check(p.returncode == 2 and "--timeout must be >= 1" in p.stderr,
          "a non-positive timeout is refused")

    p = run(COMPAT + ["--tags", "v0.1.0", "--time-travel", "v0.999.0-ghost"])
    check(p.returncode == 2 and "not a release tag" in p.stderr,
          "an unknown travel tag is a usage failure after the census")

    import time as _time
    _backup_ledger = open(LEDGER, "rb").read()
    try:
        os.rename(LEDGER, LEDGER + ".gate-bak")
        p = run(COMPAT + ["--tags", "v0.1.0"])
        check(p.returncode == 2 and "no ledger at" in p.stderr
              and "--update-ledger" in p.stderr,
              "a missing ledger is exit 2 with the drafting hint")
        with open(LEDGER, "w") as fh:
            fh.write("this is not json {{{")
        p = run(COMPAT + ["--tags", "v0.1.0"])
        check(p.returncode == 2 and "does not parse" in p.stderr,
              "a corrupt ledger is exit 2")
        with open(LEDGER, "w") as fh:
            json.dump({"schema": 99, "waivers": []}, fh)
        p = run(COMPAT + ["--tags", "v0.1.0"])
        check(p.returncode == 2 and "schema mismatch" in p.stderr,
              "a schema-mismatched ledger is exit 2")
        with open(LEDGER, "w") as fh:
            json.dump({"schema": 1, "waivers": [
                {"file": "x.hls", "sha256_16": "0" * 16,
                 "sanctioned_change": "", "reason": "x"}]}, fh)
        p = run(COMPAT + ["--tags", "v0.1.0"])
        check(p.returncode == 2 and "missing 'sanctioned_change'" in p.stderr,
              "an entry with an empty field is refused")
        with open(LEDGER, "w") as fh:
            json.dump({"schema": 1, "waivers": [
                {"file": "x.hls", "sha256_16": "0" * 16, "kind": "maybe",
                 "sanctioned_change": "x", "reason": "x"}]}, fh)
        p = run(COMPAT + ["--tags", "v0.1.0"])
        check(p.returncode == 2 and "unknown kind" in p.stderr,
              "an entry with an unknown kind is refused")
    finally:
        if os.path.exists(LEDGER + ".gate-bak"):
            os.replace(LEDGER + ".gate-bak", LEDGER)
        else:
            with open(LEDGER, "wb") as fh:
                fh.write(_backup_ledger)
    check(open(LEDGER, "rb").read() == _backup_ledger,
          "the refusal matrix leaves the real ledger untouched")

    if not NATIVE:
        print("  [SKIP] the remaining sections need bin/hlc + gcc")
        print()
        print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
        raise SystemExit(0 if FAIL == 0 else 1)

def _section_2():
    # ===========================================================================
    print("Section 2 — the census and the keying laws")

    p = run(COMPAT + ["--discover"])
    lines = p.stdout.splitlines()
    check(p.returncode == 0 and "release tags" in lines[0],
          "--discover prints the tag census")
    m = None
    for line in lines:
        if "file-instances" in line:
            m = line
            break
    check(m is not None, "the census prints the dedup line")
    if m:
        import re as _re
        mm = _re.search(r"(\d+) file-instances -> (\d+) unique programs "
                        r"\(dedup ([\d.]+)x", m)
        check(mm is not None and int(mm.group(1)) > 20000
              and 300 <= int(mm.group(2)) <= 400
              and float(mm.group(3)) > 50,
              "24k+ instances collapse to ~326 programs (dedup > 50x)")
    check("deep-scan-7" in lines[0],
          "the non-release tag is counted and named, never dropped")

    mod = load_mod("hls_compat", os.path.join(ROOT, "tools", "hls-compat.py"))

    check(mod._vkey("v0.9.0-alpha") < mod._vkey("v0.10.0-alpha")
          and mod._vkey("v0.100.0-alpha") > mod._vkey("v0.99.1-alpha"),
          "the version sort orders 9 < 10 < 100 (not alphabetically)")

    # Content addressing: the same file+helper in two different directories
    # is the same program — the temp dir's name must not leak into the key.
    ctx_a = os.path.join(TMP, "ctx_a", "tests", "ok")
    ctx_b = os.path.join(TMP, "ctx_b_whatever", "tests", "ok")
    for d in (ctx_a, ctx_b):
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "prog.hls"), "w").write(
            'import "helper.hls"\n\nfn main() -> int uses IO {\n'
            '    println("hi")\n    return 0\n}\n')
        os.makedirs(os.path.join(d, "modules"), exist_ok=True)
        open(os.path.join(os.path.dirname(d), "helper.hls"), "w").write(
            "fn helper() -> int { return 1 }\n")
    # helper must sit where the import resolves: prog dir + helper.hls
    open(os.path.join(ctx_a, "helper.hls"), "w").write(
        "fn helper() -> int { return 1 }\n")
    open(os.path.join(ctx_b, "helper.hls"), "w").write(
        "fn helper() -> int { return 1 }\n")
    ha = mod._context_hash(os.path.join(ctx_a, "prog.hls"), os.path.dirname(ctx_a))
    hb = mod._context_hash(os.path.join(ctx_b, "prog.hls"), os.path.dirname(ctx_b))
    check(ha is not None and ha == hb,
          "identical content in different trees hashes identically")
    open(os.path.join(ctx_b, "helper.hls"), "w").write(
        "fn helper() -> int { return 2 }\n")
    hc = mod._context_hash(os.path.join(ctx_b, "prog.hls"), os.path.dirname(ctx_b))
    check(hc is not None and hc != ha,
          "a changed import sibling changes the context key")

    # The toolchain fingerprint covers boot/, std/, core/ — the
    # interpreter world. Two trees with identical interpreter worlds
    # fingerprint identically; a changed interpreter file changes it.
    for root in (os.path.dirname(ctx_a), os.path.dirname(ctx_b)):
        os.makedirs(os.path.join(root, "boot"), exist_ok=True)
        open(os.path.join(root, "boot", "boot.py"), "w").write(
            "#!/usr/bin/env python3\nprint('interpreter')\n")
    fa = mod._toolchain_fingerprint(os.path.dirname(ctx_a))
    fb = mod._toolchain_fingerprint(os.path.dirname(ctx_b))
    check(fa == fb, "identical toolchain trees fingerprint identically")
    open(os.path.join(os.path.dirname(ctx_a), "boot", "boot.py"),
         "w").write(
        "#!/usr/bin/env python3\nprint('interpreter v2')\n")
    check(mod._toolchain_fingerprint(os.path.dirname(ctx_a)) != fa,
          "a changed interpreter file changes the fingerprint")

    env = mod._clean_env()
    env["HL_DET_SCHED"] = "1"
    check("HL_DET_SCHED" not in mod._clean_env(),
          "the clean env strips HL_* mode variables")

    src = ('import "std.str"\nimport "core.option"\n'
           'import "sibling.hls"\nfn f() -> int { return 0 }\n')
    rels = mod._rel_import_paths(src)
    check(rels == ["sibling.hls"],
          "relative import extraction filters std./core. prefixes")

def _section_3():
    # ===========================================================================
    print("Section 3 — the ledger discipline")

    ledger = json.load(open(LEDGER))
    kinds = sorted(w.get("kind", "break") for w in ledger["waivers"])
    check(len(ledger["waivers"]) == 9
          and kinds == ["break"] * 5 + ["drift"] * 3 + ["resurrect"],
          "the registry carries five breaks, three drifts and one "
          "resurrection")

    # Each sha names a real historical object: spot-check three of them
    # straight out of git.
    for tag, fname, want in (
            ("v0.34.0-alpha", "feat_import.hls", "4ec558d273622e1a"),
            ("v0.70.0-alpha", "feat_stage43_hash.hls", "c6f6c1cc5609a6dd"),
            ("v0.103.0-alpha", "feat_stage58_config.hls", "9105e8783d808981")):
        blob = run(["git", "show", "%s:tests/ok/%s" % (tag, fname)])
        got = hashlib.sha256(blob.stdout.encode()).hexdigest()[:16]
        check(blob.returncode == 0 and got == want,
              "the ledger sha for %s matches the git object at %s"
              % (fname, tag))

    before = open(LEDGER, "rb").read()
    p = run(COMPAT + ["--tags", "v0.103.0-alpha", "--update-ledger"])
    after = open(LEDGER, "rb").read()
    check(p.returncode == 0 and "FAIL feat_stage84_section" in p.stdout
          and "UNREVIEWED" in p.stdout,
          "--update-ledger drafts the census-visible failure honestly")
    check("FAIL feat_stage58_config" not in p.stdout,
          "the draft cannot see resurrections (the program is green today)")
    check(before == after,
          "--update-ledger never writes the ledger file")

def _section_4():
    # ===========================================================================
    print("Section 4 — the certification (the REAL CLI, the FULL census)")

    jp = os.path.join(TMP, "full_report.json")
    p = run(COMPAT + ["--json", jp], timeout=1800)
    check(p.returncode == 0 and "verdict COMPATIBLE" in p.stdout
          and "0 regressions" in p.stdout,
          "the full census certifies: every prior ok corpus replays green")
    rep = json.load(open(jp))
    check(rep["verdict"] == "COMPATIBLE"
          and rep["census"]["release_tags"] >= 170
          and rep["census"]["unique_programs"] >= 300
          and len(rep["contents"].get("WAIVED", [])) == 4
          and not rep["contents"].get("REGRESSION")
          and not rep["contents"].get("RECONCILED"),
          "the json report carries the census and every verdict (the"
          " fifth break, xxHash, panics — 101-is-green hides it from"
          " the census; the time-travel leg verifies it)")

    p = run(COMPAT + ["--tags", "v0.60.0-alpha"], timeout=600)
    check(p.returncode == 0 and "verdict COMPATIBLE" in p.stdout
          and p.stdout.count("WAIVED    feat_stage27") == 2,
          "a restricted probe certifies v0.60.0-alpha (2 asm waivers)")

def _section_5():
    # ===========================================================================
    print("Section 5 — the regression alarm (proven, then cleaned up)")

    _backup = open(LEDGER, "rb").read()

    # (a) an entry removed: a sanctioned break becomes a REGRESSION.
    def drop_import(w):
        w["waivers"] = [x for x in w["waivers"]
                        if x["file"] != "feat_import.hls"]
        return w


    with ledger_variant(drop_import):
        p = run(COMPAT + ["--tags", "v0.3.0"], timeout=600)
    check(p.returncode == 1 and "REGRESSION feat_import" in p.stdout,
          "removing the ledger entry turns the break into a REGRESSION")

    # (b) an entry pointed at a green program: RECONCILED, the stale waiver.
    def point_at_green(w):
        green_sha = hashlib.sha256(
            open(os.path.join(ROOT, "tests", "ok", "feat_import.hls"),
                 "rb").read()).hexdigest()[:16]
        for x in w["waivers"]:
            if x["file"] == "feat_import.hls":
                x["sha256_16"] = green_sha
        return w


    with ledger_variant(point_at_green):
        p = run(COMPAT + ["--tags", "v0.145.0-alpha"], timeout=900)
    check(p.returncode == 1 and "RECONCILED feat_import" in p.stdout,
          "a waiver on a green program is RECONCILED (trim the ledger)")

    # (c) an invented entry on the FULL census: STALE.
    def add_ghost(w):
        w["waivers"].append({"file": "feat_ghost.hls",
                             "sha256_16": "0" * 16, "kind": "break",
                             "sanctioned_change": "invented",
                             "reason": "matches nothing"})
        return w


    with ledger_variant(add_ghost):
        p = run(COMPAT, timeout=1800)
    check(p.returncode == 1 and "STALE ledger entry feat_ghost" in p.stdout,
          "an invented entry matches no historical content (STALE)")

    check(open(LEDGER, "rb").read() == _backup,
          "every forged-ledger run restored the real registry")

    # (d) the end-to-end alarm: a probe commit tagged into real history.
    PROBE_TAG = "v0.999.0-compatprobe"
    dirty = run(["git", "status", "--porcelain"])
    probe_head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    if dirty.stdout.strip():
        bad("the probe needs a clean worktree (found: %s)"
            % dirty.stdout.splitlines()[0])
    else:
        probe_file = os.path.join(ROOT, "tests", "ok",
                                  "feat_compat_probe.hls")
        try:
            with open(probe_file, "w") as fh:
                fh.write("# compat gate probe: deliberately broken, tagged\n"
                         "# into history for one run, then removed.\n"
                         "fn main() -> int uses IO {\n"
                         "    println(\"probe: \" + no_such_function(1))\n"
                         "    return 0\n}\n")
            run(["git", "add", probe_file])
            c = run(["git", "commit", "-m",
                     "compat gate probe (temporary)"])
            run(["git", "tag", PROBE_TAG])
            p = run(COMPAT + ["--tags", PROBE_TAG], timeout=600)
            check(p.returncode == 1
                  and "REGRESSION feat_compat_probe" in p.stdout
                  and "no_such_function" in p.stdout,
                  "a broken fixture tagged into history is caught, its "
                  "error line quoted")
        finally:
            run(["git", "tag", "-d", PROBE_TAG])
            run(["git", "reset", "--hard", "HEAD~1"])
        after_head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
        check(after_head == probe_head
              and not os.path.exists(probe_file)
              and not run(["git", "status", "--porcelain"]).stdout.strip(),
              "history restored: the probe leaves no commit, tag or file")

def _section_6():
    # ===========================================================================
    print("Section 6 — the time-travel witness (six releases, hand-computed)")

    # The bookends: the oldest release and the latest one, corpus sizes
    # computed from git at gate time (the latest changes as history grows).
    release_tags = run(["git", "tag", "--list", "v*"]).stdout.split()


    def _vkey2(t):
        import re as _re2
        m = _re2.match(r"v(\d+)\.(\d+)(?:\.(\d+))?(?:-(.*))?$", t)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0),
                m.group(4) or "")


    release_tags.sort(key=_vkey2)
    bookends = [release_tags[0], release_tags[-1]]
    trav_tags = [bookends[0]] + list(FAMILY_TRAVEL) + [bookends[1]]
    trav_arg = ",".join(trav_tags)

    jp = os.path.join(TMP, "travel_report.json")
    p = run(COMPAT + ["--tags", trav_arg, "--time-travel", trav_arg,
                      "--json", jp], timeout=2400)
    check(p.returncode == 0 and "history agrees" in p.stdout,
          "the witness certifies: six releases, history agrees")
    rep = json.load(open(jp))
    for tag in trav_tags:
        t = rep["time_travel"].get(tag, {})
        if tag in FAMILY_TRAVEL:
            exp = FAMILY_TRAVEL[tag]
            corpus = exp["corpus"]
            want_agree, want_break = exp["agree"], exp["break"]
            want_res, want_drift = exp["resurrect"], exp["drift"]
        else:
            # A bookend: born green (the oldest) or certified green (the
            # latest — the census's own claim about the newest corpus).
            listing = run(["git", "ls-tree", "--name-only", "-r", tag,
                           "tests/ok/"])
            corpus = len([f for f in listing.stdout.splitlines()
                          if f.endswith(".hls") and f.count("/") == 2])
            want_agree, want_break = corpus, 0
            want_res, want_drift = 0, 0
        got_agree = t.get("agree", -1)
        got_break = t.get("break_waived", -1)
        got_res = t.get("resurrect_waived", -1)
        got_drift = t.get("drift_waived", -1)
        n_oldbugs = len(t.get("old_bugs", ["x"]))
        n_divs = len(t.get("divergences", ["x"]))
        n_resur = len(t.get("resurrections", ["x"]))
        check(got_agree == want_agree and got_break == want_break
              and got_res == want_res and got_drift == want_drift
              and n_oldbugs == 0 and n_divs == 0 and n_resur == 0,
              "%s (corpus %d): %d agree, %d break-waived, %d resurrect-waived,"
              " %d drift-waived — the hand-computed matrix, no old bugs, no"
              " divergences"
              % (tag, corpus, want_agree, want_break, want_res, want_drift))

def _section_7():
    # ===========================================================================
    print("Section 7 — the native differential over historical programs")

    jp = os.path.join(TMP, "native_report.json")
    p = run(COMPAT + ["--tags", "v0.1.0,v0.60.0-alpha", "--native",
                      "--json", jp], timeout=2400)
    check(p.returncode == 0 and "native agrees" in p.stdout,
          "the native leg certifies two releases' corpora")
    rep = json.load(open(jp))
    check(rep["native"]["ran"] == 149
          and rep["native"]["divergences"] == [],
          "149 historical programs: native (exit, stdout) identical to the "
          "interpreter, zero divergences")

    # The stage's own two files, differential-checked directly.
    for f in (SHOWCASE, DEMO):
        label = os.path.relpath(f, ROOT)
        c_path = os.path.join(TMP, os.path.basename(f) + ".c")
        b_path = os.path.join(TMP, os.path.basename(f) + ".bin")
        run([HLC, f, c_path])
        run([CC, "-O2", "-o", b_path, c_path, "-lm", "-pthread"])
        p = run([b_path])
        p2 = run(BOOT + [f])
        check(p.returncode == p2.returncode == 0
              and p.stdout == p2.stdout,
              "%s: native == interpreter, byte for byte" % label)

def _section_8():
    # ===========================================================================
    print("Section 8 — the corpus hygiene")

    for f in (SHOWCASE, DEMO):
        label = os.path.relpath(f, ROOT)
        p = run([sys.executable, "tools/hlfmt.py", "-c", f])
        check("already formatted" in p.stdout, "%s is hlfmt-canonical" % label)
        p = run([sys.executable, "tools/hllint.py", f])
        check("no warnings" in p.stdout, "%s is hllint-clean" % label)
        p = run(BOOT + ["--check", f])
        check("OK: types and effects valid" in p.stdout,
              "%s boots through the real checker" % label)

    # The ledger is valid, nine entries, and the tool loads it.
    _mod = load_mod("hls_compat_hygiene",
                    os.path.join(ROOT, "tools", "hls-compat.py"))
    _data, by_key = _mod.load_ledger(LEDGER)
    check(len(by_key) == 9, "the registry loads through the tool's own loader")

    # No gate litter survives.
    litter = []
    for d in (TMP, os.path.join(ROOT, "tests"), ROOT):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.startswith("hls-compat-"):
                litter.append(os.path.join(d, f))
    check(not litter, "no verifier temp files survive anywhere the gate ran")


# ---------------------------------------------------------------------------
# Section selection: the whole gate is one process by default; a CI
# step timeout can run it in pieces (--sections 1,2 or the
# HLS_COMPAT_GATE_SECTIONS env). A subset run reports the sections it
# ran, never a silent skip.
_ALL_SECTIONS = list(range(1, 9))
import sys as _sys
if "--sections" in _sys.argv:
    _want = _sys.argv[_sys.argv.index("--sections") + 1]
    _selected = [int(x) for x in _want.split(",") if x.strip()]
else:
    _env = os.environ.get("HLS_COMPAT_GATE_SECTIONS")
    _selected = ([int(x) for x in _env.split(",") if x.strip()]
                 if _env else _ALL_SECTIONS)
for _bad_n in _selected:
    if _bad_n not in _ALL_SECTIONS:
        _sys.stderr.write("compat gate: no such section: %d\n" % _bad_n)
        raise SystemExit(2)
if _selected != _ALL_SECTIONS:
    print("compat gate: running sections %s of %s"
          % (",".join(str(x) for x in _selected),
             ",".join(str(x) for x in _ALL_SECTIONS)))
for _n in _selected:
    globals()["_section_%d" % _n]()

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
raise SystemExit(0 if FAIL == 0 else 1)
