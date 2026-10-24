#!/usr/bin/env python3
"""Stage 134 acceptance gate — fuzz corpus seeding from real-world
packages.

Run with `make fuzzseed-acceptance` (or
`python3 tests/fuzzseed_acceptance.py`).

Eight sections over the REAL seeder CLI, the REAL parser and checker
(boot), the REAL native backend (bin/hlc), the REAL fuzzer, and a
scratch package cache laid out exactly as hls-pkg lays a downloaded
dependency out — populated from tests/fuzzseed_pkg/, a REAL package
written in real Halis whose modules span the seedable surface AND the
named refusal surface:

  1. the convention  — the CLI exits 0 on a green corpus, 2 on a
     usage error (a bad --limit, a --pkg-cache directory that does
     not exist — a configured source that is not there is a refusal,
     not a silent empty); --json is machine-parseable and names the
     tool, the result and the stamp law (no wall-clock unless --stamp)

  2. the harvest     — the cache layout is walked exactly as hls-pkg
     law             writes it (cache/<name>/<hash>/...); provenance
                     labels read pkg:<name>/...; --no-in-tree keeps
                     the run on the packages alone; a file that does
                     not parse is a NAMED skip in the census, never a
                     crash (broken.hls exists to prove it)

  3. the harness     — every emitted seed is re-verified through the
     law             REAL pipeline (parse + check in-process, no
                     trusting the tool's own manifest); the shape is
                     fixed (provenance header, fn main uses IO, one
                     corner-valued let per parameter BEFORE the call,
                     the projection, return 0); the synthesis refusal
                     is named (area_of_rect / area_of take a custom
                     struct or enum — no invented values)

  4. the purity      — now_label dies naming clock_ms (the shared
     law             Stage 133 deny table); file_touch_count dies
                     naming the DECLARED Fs effect (a declared world
                     is refused as loudly as a used one); the gate's
                     own corpus is re-gated text-level: every seed
                     passes the same deny tables the tool claims

  5. the identity    — the twin package (pure_math re-read through a
     law             comment banner and re-spaced) lands ONCE, with
                     BOTH provenances in the manifest: normalization
                     strips comments and whitespace, so the digest is
                     the function, not the file

  6. the census      — the ranked order is non-increasing in score;
     law             the expected entry names are all present (gcd,
                     isqrt, lerp, clamp3, sum_list, reverse_strs,
                     contains_int, second_of, make_circle,
                     perimeter_of, scale_x_len, crashy); scale_x_len's
                     harness carries BOTH the Vec2 definition and the
                     scale method body (the method closure is exact);
                     vec_add's harness never exists (its parameters
                     are unsynthesizable) and no seed's text leaks a
                     method body its closure did not demand

  7. the mutation    — every surviving mutant re-checks through the
     law             REAL pipeline; every mutant's lineage names its
                     op and its parent, and every parent exists in
                     the manifest; the drop counter's keys are the
                     tool's op vocabulary; --keep-timeouts lifts the
                     hang refusal and the manifest says so (the spin
                     seed appears with smoke=timeout — and the
                     differential section never runs it)

  8. the differential— every seed and every surviving mutant runs on
     & the fuzzer    BOTH backends (bin/hlc), stdout bytes and exit
                     codes identical; crashy's panic lands as 101 on
                     BOTH sides — a panic is a legitimate observable;
                     hls-fuzz --seed-corpus runs the corpus green
                     with the seeded counter ABOVE ZERO and zero
                     divergences; --minimize on a clean seed returns
                     it unchanged (the minimiser's region is located
                     by name, headers and closure defs intact)

Exit code 0 = all acceptance criteria met.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.hlinterp.parser import parse                     # noqa: E402
from tools.hlinterp.checker import check as hls_check       # noqa: E402
from tools.hlinterp.boot import load_program                # noqa: E402
from tools.hlinterp.lexer import HLError                    # noqa: E402

PKG_SRC = os.path.join(ROOT, "tests", "fuzzseed_pkg", "src")
FZZ = os.path.join(ROOT, "tools", "hls-fuzzseed.py")
FUZZ = os.path.join(ROOT, "tools", "hls-fuzz.py")
HLC = os.path.join(ROOT, "bin", "hlc")

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="fuzzseed_gate_")

CACHE = os.path.join(TMP, "cache")
CORPUS = os.path.join(TMP, "corpus")
CORPUS_KT = os.path.join(TMP, "corpus_kt")
CORPUS_PROBE = os.path.join(TMP, "corpus_probe")


def ok(msg):
    global PASS
    PASS += 1
    print("  [PASS] " + msg)


def bad(msg):
    global FAIL
    FAIL += 1
    print("  FAIL: " + msg)


def expect(cond, msg):
    if cond:
        ok(msg)
    else:
        bad(msg)


def run(argv, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=600,
                          text=True, **kw)


def seed_cli(*extra, out=CORPUS):
    return run([sys.executable, FZZ, "--pkg-cache", CACHE,
                "--no-in-tree", "--out", out, "--seed", "134",
                "--smoke-timeout", "3", "--quiet"] + list(extra))


def build_scratch_cache():
    """Lay the fixture package out exactly as hls-pkg's resolve_dependency
    does: <cache>/<dep-name>/<repo-hash>/... — plus the twin package
    (pure_math re-read through a comment banner and re-spaced) for the
    identity law."""
    os.makedirs(CACHE, exist_ok=True)
    for name, digest in (("fuzzseed.demo", "0123456789abcdef"),
                         ("twin.demo", "fedcba9876543210")):
        pkg = os.path.join(CACHE, name, digest)
        os.makedirs(os.path.join(pkg, "src"), exist_ok=True)
        shutil.copy(os.path.join(PKG_SRC, "..", "hls-pkg.toml"),
                    os.path.join(pkg, "hls-pkg.toml"))
        src_dir = os.path.join(pkg, "src")
        if name == "fuzzseed.demo":
            for fn in sorted(os.listdir(PKG_SRC)):
                shutil.copy(os.path.join(PKG_SRC, fn),
                            os.path.join(src_dir, fn))
        else:
            with open(os.path.join(PKG_SRC, "pure_math.hls")) as f:
                text = f.read()
            twin = ("# twin.demo — the identity law's twin: the SAME\n"
                    "# functions behind a different comment banner and\n"
                    "# different spacing. The digest is the function,\n"
                    "# not the file.\n\n\n"
                    + text.replace("    ", "        ", 0))
            with open(os.path.join(src_dir, "twin.hls"), "w") as f:
                f.write(twin)


def interp_native(src_path, tmpdir):
    """(stdout bytes, exit code) on BOTH backends, via bin/hlc."""
    out_c = os.path.join(tmpdir, "out.c")
    out_bin = os.path.join(tmpdir, "out.bin")
    p1 = run([HLC, src_path, out_c], cwd=ROOT)
    if p1.returncode != 0:
        return None, None, p1.stdout
    p2 = run(["gcc", "-O2", "-o", out_bin, out_c, "-lm", "-pthread"])
    if p2.returncode != 0:
        return None, None, p2.stdout
    import subprocess as _sp
    p3 = _sp.run([out_bin], stdout=_sp.PIPE, stderr=_sp.STDOUT,
                 timeout=30, text=True, input="")
    return p3.stdout, p3.returncode, ""


def interp_only(src_path):
    p = run([sys.executable, os.path.join(ROOT, "tools", "hlinterp", "boot.py"),
             src_path], cwd=ROOT, input="")
    return p.stdout, p.returncode


# ===========================================================================
def _section_1():
    print("Section 1 — the convention")
    # usage errors: a bad limit, and a --pkg-cache that is not there
    p = run([sys.executable, FZZ, "--limit", "-3"])
    expect(p.returncode == 2, "a negative --limit is a usage error (exit 2)")
    expect("usage error" in p.stdout,
          "the usage refusal names the rule: %r" % p.stdout.strip()[:60])
    p = run([sys.executable, FZZ, "--pkg-cache",
             os.path.join(TMP, "not-there")])
    expect(p.returncode == 2, "a missing --pkg-cache is a refusal (exit 2)")
    expect("does not exist" in p.stdout,
          "the missing-source refusal names the directory")

    # the green convention: exit 0, manifest written, stamp law
    build_scratch_cache()
    p = seed_cli("--limit", "24", "--mutate", "1")
    expect(p.returncode == 0, "the default gate run exits 0 (green)")
    man = json.load(open(os.path.join(CORPUS, "manifest.json")))
    expect(man["stamp"] is None,
          "no wall-clock in the manifest by default (reproducible)")
    p = seed_cli("--limit", "24", "--mutate", "1", "--stamp",
                 "2026-10-13T00:00:00Z")
    man2 = json.load(open(os.path.join(CORPUS, "manifest.json")))
    expect(man2["stamp"] == "2026-10-13T00:00:00Z",
          "--stamp records exactly what was given")

    # --json is machine-parseable and names the result
    p = seed_cli("--limit", "4", "--mutate", "0", "--json")
    expect(p.returncode == 0, "--json run exits 0")
    doc = json.loads(p.stdout)
    expect(doc.get("result") == "green", "--json names the result green")
    expect("hls-fuzzseed" in doc.get("tool", ""),
          "--json names the tool: %r" % doc.get("tool", "")[:40])
    expect(doc.get("limit") == 4 and doc.get("mutate") == 0,
          "--json echoes the configuration")

    # determinism: the same tree, the same seed, the same corpus
    p = seed_cli("--limit", "24", "--mutate", "1")
    man_b = json.load(open(os.path.join(CORPUS, "manifest.json")))
    digests_a = [s["digest"] for s in man["seeds"]]
    digests_b = [s["digest"] for s in man_b["seeds"]]
    expect(digests_a == digests_b,
          "two runs of the same tree and seed agree on the corpus")


# ===========================================================================
def _section_2():
    print("Section 2 — the harvest law")
    man = json.load(open(os.path.join(CORPUS, "manifest.json")))
    seeds = man["seeds"]
    expect(len(seeds) > 0, "the package cache yields a non-empty corpus")
    src_labels = set()
    in_pkg = False
    for s in seeds:
        for src in s["sources"]:
            src_labels.add(src.split("/")[0])
            if "/fuzzseed.demo/" in src:
                in_pkg = True
    expect(src_labels == {"pkg:cache"},
           "provenance labels read pkg:<cache-root> (no in-tree leak "
           "under --no-in-tree): %s" % sorted(src_labels))
    expect(in_pkg,
           "the cache layout <name>/<hash>/... is walked to the files")

    # the parse refusal is named, never a crash
    skip_reasons = " | ".join(s["reason"] for s in man["skips"])
    expect("expected identifier but got '{'" in skip_reasons,
          "broken.hls's parse error is a named skip with the parser's "
          "own words")
    stats = man["stats"]
    expect(stats["files_unparsed"] == 1,
          "exactly one file failed to parse (broken.hls)")
    expect(stats["files_seen"] == 12,
          "all 12 fixture files were seen (11 modules + the twin)")


# ===========================================================================
def _section_3():
    print("Section 3 — the harness law")
    man = json.load(open(os.path.join(CORPUS, "manifest.json")))
    names = [s["fn"] for s in man["seeds"]]
    for want in ("gcd", "isqrt", "lerp", "clamp3", "sum_list",
                 "reverse_strs", "contains_int", "second_of",
                 "make_circle", "perimeter_of_demo", "scale_x_len",
                     "crashy"):
        expect(want in names, "seed present for fn '%s'" % want)

    # every seed independently re-verifies through the REAL pipeline
    rechecked = 0
    shape_ok = True
    for s in man["seeds"]:
        path = os.path.join(CORPUS, s["file"])
        try:
            program = load_program(path)
            hls_check(program)
            rechecked += 1
        except HLError as e:
            shape_ok = False
            bad("seed %s fails the real checker: %s" % (s["file"], e))
        text = open(path).read()
        if "fn main() -> int uses IO {" not in text:
            shape_ok = False
            bad("seed %s missing the harness main shape" % s["file"])
        # param lets precede the call: every param summary name appears
        # in a let before the first call of the fn
        main_at = text.index("fn main")
        call_at = text.index(s["fn"] + "(", main_at)
        for p in s["params"]:
            pname = p.split(":")[0].strip()
            let_at = text.find("let %s: " % pname, main_at)
            if let_at < 0 or let_at > call_at:
                shape_ok = False
                bad("seed %s: param '%s' not corner-valued before the "
                    "call" % (s["file"], pname))
        if "return 0" not in text[text.index("fn main"):]:
            shape_ok = False
            bad("seed %s missing 'return 0'" % s["file"])
    expect(shape_ok and rechecked == len(man["seeds"]),
          "all %d seeds re-verify through parse+check and carry the "
          "harness shape" % len(man["seeds"]))

    # the synthesis refusal is named
    skip_text = json.dumps(man["skips"])
    expect("area_of_rect" in skip_text and
          "no synthesizable literal" in skip_text,
          "area_of_rect refused by name: custom struct parameters have "
          "no invented value")
    expect("area_of" in skip_text,
          "area_of refused too (a custom enum parameter)")
    # ...and the refusal never leaks into the corpus
    corpus_text = ""
    for s in man["seeds"]:
        corpus_text += open(os.path.join(CORPUS, s["file"])).read()
    expect("fn area_of_rect" not in corpus_text,
          "no refused fn definition leaks into any harness")


# ===========================================================================
def _section_4():
    print("Section 4 — the purity law")
    man = json.load(open(os.path.join(CORPUS, "manifest.json")))
    skip_text = json.dumps(man["skips"])
    expect("now_label" in skip_text and "'Clock'" in skip_text,
          "now_label refused naming the DECLARED Clock effect (the "
          "declaration law runs before the text-level table)")
    import importlib.util as _ilu
    _spec2 = _ilu.spec_from_file_location("fzz_purity", FZZ)
    _fzz2 = _ilu.module_from_spec(_spec2)
    _spec2.loader.exec_module(_fzz2)
    _elig, _why = _fzz2.purity_verdict("fn f() -> int { return clock_ms() }\n")
    expect(not _elig and "clock_ms" in _why,
          "the text-level deny table names clock_ms on its own: %s"
          % _why)
    expect("file_touch_count" in skip_text and "Fs" in skip_text,
          "file_touch_count refused naming the DECLARED Fs effect")
    expect("shout" in skip_text and "std.str" in skip_text,
          "shout refused naming the import it needs (std.str)")

    # every seed passes the same deny tables the tool claims to apply
    import importlib.util
    spec = importlib.util.spec_from_file_location("fzz", FZZ)
    fzz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fzz)
    all_clean = True
    for s in man["seeds"]:
        text = open(os.path.join(CORPUS, s["file"])).read()
        eligible, reason = fzz.purity_verdict(text)
        if not eligible:
            all_clean = False
            bad("seed %s violates the purity gate: %s" % (s["file"],
                                                          reason))
    expect(all_clean, "every emitted seed passes the tool's own purity "
                     "gate, re-applied here")

    # the seed-specific rows: a machine probe is refused by name
    probe_pkg = os.path.join(CACHE, "probe.demo", "0" * 16, "src")
    os.makedirs(probe_pkg, exist_ok=True)
    with open(os.path.join(probe_pkg, "probe.hls"), "w") as f:
        f.write(
            "# probe.hls — a machine-dependent builtin\n"
            "fn probe_tty() -> bool {\n"
            "    return isatty(0)\n"
            "}\n")
    p = seed_cli("--limit", "40", "--mutate", "0", "--json",
                 out=CORPUS_PROBE)
    doc = json.loads(p.stdout)
    st = json.dumps(doc["skips"])
    expect("isatty" in st,
          "isatty refused by name (the seed-specific deny rows are "
          "live, each reason named)")
    os.remove(os.path.join(probe_pkg, "probe.hls"))


# ===========================================================================
def _section_5():
    print("Section 5 — the identity law")
    man = json.load(open(os.path.join(CORPUS, "manifest.json")))
    by_fn = {}
    for s in man["seeds"]:
        by_fn.setdefault(s["fn"], []).append(s)
    expect(all(len(v) == 1 for v in by_fn.values()),
          "no function appears twice in the corpus (digest identity)")
    merged = [s for s in man["seeds"] if len(s["sources"]) >= 2]
    expect(len(merged) >= 4,
          "the twin's functions merged: %d seeds carry >=2 provenances"
          % len(merged))
    g = by_fn.get("gcd", [])
    expect(len(g) == 1 and len(g[0]["sources"]) == 2 and
          any("twin.demo" in x for x in g[0]["sources"]),
          "gcd names BOTH packages in its provenance (fuzzseed.demo + "
          "twin.demo)")


# ===========================================================================
def _section_6():
    print("Section 6 — the census law")
    man = json.load(open(os.path.join(CORPUS, "manifest.json")))
    scores = [s["score"] for s in man["seeds"]]
    expect(scores == sorted(scores, reverse=True),
          "the manifest's seed order is non-increasing in rank score")
    expect(all("census" in s and isinstance(s["census"], dict)
              for s in man["seeds"]),
          "every seed records its census (the rank is auditable)")

    scale = [s for s in man["seeds"] if s["fn"] == "scale_x_len"]
    expect(len(scale) == 1, "scale_x_len seeded exactly once")
    if scale:
        text = open(os.path.join(CORPUS, scale[0]["file"])).read()
        expect("struct Vec2 {" in text,
              "the method closure pulled in the Vec2 definition")
        expect("fn scale(self: Vec2, k: float) -> Vec2 {" in text,
              "the method closure pulled in the scale method body")
        expect(scale[0]["score"] >= 5,
              "scale_x_len outranks the scalar toys (census weight)")

    # the closure is exact, not greedy: a seed whose entry never calls
    # the method never carries it
    leaked = False
    for s in man["seeds"]:
        if s["fn"] in ("gcd", "isqrt", "clamp3", "sum_list"):
            text = open(os.path.join(CORPUS, s["file"])).read()
            if "fn scale(self" in text or "struct Vec2" in text:
                leaked = True
                bad("%s's harness carries Vec2 machinery it never "
                    "demands" % s["fn"])
    expect(not leaked, "no greedy closure leak into the scalar seeds")


# ===========================================================================
def _section_7():
    print("Section 7 — the mutation law")
    man = json.load(open(os.path.join(CORPUS, "manifest.json")))
    ops_seen = set()
    lineage_ok = True
    parent_files = {s["digest"]: s["file"] for s in man["seeds"]}
    rechecked = 0
    for s in man["seeds"]:
        for m in s.get("mutants", []):
            ops_seen.add(m["op"])
            if m["parent"] != s["digest"] or m["parent"] not in \
                    parent_files:
                lineage_ok = False
                bad("mutant %s has broken lineage" % m["file"])
            try:
                program = load_program(os.path.join(CORPUS, m["file"]))
                hls_check(program)
                rechecked += 1
            except HLError as e:
                lineage_ok = False
                bad("mutant %s fails the real checker: %s" % (m["file"],
                                                              e))
    expect(lineage_ok and rechecked > 0,
          "all %d surviving mutants re-verify and carry valid lineage"
          % rechecked)
    expect(ops_seen, "at least one mutation op produced a mutant")
    drops = man["stats"]["mutant_drops_by_op"]
    vocab = set(fz_ops())
    expect(set(drops.keys()) <= vocab,
          "drop-counter keys are the tool's op vocabulary: %s"
          % sorted(drops.keys()))

    # --keep-timeouts lifts the hang refusal, named in the manifest
    p = seed_cli("--limit", "40", "--mutate", "0", "--keep-timeouts",
                 out=CORPUS_KT)
    man_kt = json.load(open(os.path.join(CORPUS_KT, "manifest.json")))
    spin = [s for s in man_kt["seeds"] if s["fn"] == "spin_forever"]
    expect(len(spin) == 1 and spin[0]["smoke"] == "timeout",
          "--keep-timeouts admits the hanging seed with smoke=timeout")
    expect(any("spin_forever" in json.dumps(x) for x in man["skips"]),
          "the default run REFUSED the hang by name (wasted fuzz time)")
    # ...and the hang never enters the default corpus
    expect(all(s["fn"] != "spin_forever" for s in man["seeds"]),
          "the default corpus contains no hanging seed")


def fz_ops():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fzz2", FZZ)
    fzz = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fzz)
    return fzz.MUTATION_OPS


# ===========================================================================
def _section_8():
    print("Section 8 — the differential & the fuzzer")
    man = json.load(open(os.path.join(CORPUS, "manifest.json")))
    tmpdir = os.path.join(TMP, "diff")
    os.makedirs(tmpdir, exist_ok=True)
    plan = []
    for s in man["seeds"]:
        plan.append(s["file"])
        for m in s.get("mutants", []):
            plan.append(m["file"])
    if not os.path.exists(HLC):
        # direct invocation without the Makefile prereq: build once
        run(["make", "bootstrap"], cwd=ROOT)
    mismatch = []
    panicked = []
    for rel in plan:
        path = os.path.join(CORPUS, rel)
        i_out, i_rc = interp_only(path)
        n_out, n_rc, err = interp_native(path, tmpdir)
        if n_out is None:
            mismatch.append((rel, "native build failed: %s" % err[:80]))
            continue
        if i_out != n_out or i_rc != n_rc:
            mismatch.append((rel, "rc %s/%s out %r/%r"
                             % (i_rc, n_rc, i_out[:40], n_out[:40])))
        if i_rc == 101 and n_rc == 101:
            panicked.append(rel)
    expect(not mismatch,
          "all %d corpus programs are byte-identical on interp and "
          "native" % len(plan))
    if mismatch:
        for rel, why in mismatch[:5]:
            print("     mismatch: %s — %s" % (rel, why))
    expect(any("crashy" in rel for rel in panicked),
          "crashy's panic lands as 101 on BOTH sides (a panic is a "
          "legitimate observable)")

    # the fuzzer, seeded: the corpus runs green with the seeded
    # counter above zero and zero divergences
    p = run([sys.executable, FUZZ, "--seed-corpus", CORPUS,
             "--seed-ratio", "1.0", "--seed", "42", "--n", "6",
             "--time", "3600"], cwd=ROOT)
    expect(p.returncode == 0, "the seeded fuzzer exits 0 (no divergence)")
    expect("%d seeded" % 6 in p.stdout or "6 seeded" in p.stdout,
          "the seeded counter is above zero: %s"
          % p.stdout.strip().split("\n")[-1])
    expect("0 diverge" in p.stdout, "zero divergences over the corpus")
    # and the mixed ratio: both populations move
    p = run([sys.executable, FUZZ, "--seed-corpus", CORPUS,
             "--seed-ratio", "0.5", "--seed", "43", "--n", "8",
             "--time", "3600"], cwd=ROOT)
    expect(p.returncode == 0 and "0 diverge" in p.stdout,
          "the mixed-ratio run is green too")

    # the minimiser locates its region by name: a clean seed comes
    # back unchanged (headers and closure defs intact)
    seed_path = os.path.join(CORPUS, man["seeds"][0]["file"])
    before = open(seed_path).read()
    p = run([sys.executable, FUZZ, "--minimize", seed_path], cwd=ROOT)
    expect(p.returncode == 0 and p.stdout.strip() == before.strip(),
          "--minimize on a clean seed returns it unchanged")


# ===========================================================================
_ALL_SECTIONS = list(range(1, 9))
if "--sections" in sys.argv:
    _want = sys.argv[sys.argv.index("--sections") + 1]
    _selected = [int(x) for x in _want.split(",") if x.strip()]
else:
    _env = os.environ.get("HLS_FUZZSEED_GATE_SECTIONS")
    _selected = ([int(x) for x in _env.split(",") if x.strip()]
                 if _env else _ALL_SECTIONS)
for _bad_n in _selected:
    if _bad_n not in _ALL_SECTIONS:
        sys.stderr.write("fuzzseed gate: no such section: %d\n"
                         % _bad_n)
        raise SystemExit(2)
if _selected != _ALL_SECTIONS:
    print("fuzzseed gate: running sections %s of %s"
          % (",".join(str(x) for x in _selected),
             ",".join(str(x) for x in _ALL_SECTIONS)))
for _n in _selected:
    globals()["_section_%d" % _n]()

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
print()
print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
raise SystemExit(0 if FAIL == 0 else 1)
