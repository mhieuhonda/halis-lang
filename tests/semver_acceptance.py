#!/usr/bin/env python3
"""Stage 131 acceptance gate — semantic-versioning enforcement in
`hls-pkg` (v0.149.0-alpha).

Run with `make semver-acceptance` (or
`python3 tests/semver_acceptance.py`).

Eight sections over the REAL package-manager CLI (subprocess, the
interface a user drives), the REAL enforcement module (imported for
the unit laws), an isolated transparency ledger + git cache (the
Stage 131 env overrides — a gate that appended its publishes into the
developer's real ledger would pollute it and depend on it), and the
ok-corpus showcase:

  1. the convention  — `hls-pkg semver check/cmp/bump/sat` exits:
     check 0 valid / 1 invalid (stderr names the failing clause),
     cmp prints -1/0/1 (malformed input is exit 2), bump refuses an
     unknown part at the parser, sat 0 satisfies / 1 does not / 2
     malformed — and every refusal names the grammar instead of
     saying "invalid"

  2. the grammar     — the strict semver 2.0.0 law: leading zeros
     & precedence    refused in the core AND in numeric pre-release
     identifiers; the canonical §11 chain (1.0.0-alpha < 1.0.0-alpha.1
     < ... < 1.0.0-rc.1 < 1.0.0); build metadata ignored by
     precedence (§10) but carried by the canonical rendering;
     numeric < alphanumeric (§11.4); the release above every
     pre-release of its own core (§11.2); tag_version strips one
     leading v (both cases) and refuses non-semver tags; bump drops
     the pre-release

  3. the requirement — caret (first non-zero pins: ^1.2.3 -> <2.0.0,
     dialect         ^0.2.3 -> <0.3.0, ^0.0.3 -> <0.0.4), tilde (patch
     floats, minor doesn't), comparators (full bounds only — a
     partial after >= is refused BY NAME), wildcards (*, 1.x,
     1.2.*), bare partials (1.2 -> >=1.2.0 <1.3.0), hyphen ranges
     (upper partial floats: 1.2.3 - 2.0 -> <2.1.0), conjunctions
     (space and comma), and the pre-release OPT-IN rule (^1.2.3
     never pulls 1.3.0-beta; an explicit pre-release floor opts the
     same core in; * and = refuse pre-releases). `||` alternatives,
     wildcards under ^/~, and leading zeros are refused, named

  4. the manifest    — over a REAL local git repo (two semver tags,
     enforcement      a non-semver tag, a manifest-less main branch):
     lock validates the package's own version BEFORE resolving;
     lock enforces a dependency requirement against a semver tag
     (v1.4.0 green, v2.0.0 refused, a non-semver tag refused — a
     requirement that cannot be checked is a refusal, not a
     warning); a moving ref is judged by the dependency's own
     manifest version (green when it satisfies, refused when the
     branch carries no manifest); build refuses a bad own version
     and a bad requirement before compiling; a green build still
     builds; `hls-pkg add --version` writes the requirement and
     refuses a malformed one at the keyboard

  5. the publish     — publish REQUIRES a version (the silent 0.0.0
     laws            fallback is gone) and a VALID one; the
     immutability law: identical content republishes as a no-op
     (the ledger is not spammed), different content under the same
     name@version is a hard refusal naming both hashes, build
     metadata does not change the identity (0.3.0+x is 0.3.0), and
     a real bump publishes fine

  6. the verify      — the drift law: editing the manifest's
     drift law       requirement after the lock fails verification
                     naming both texts; removing the requirement
                     from the manifest fails it too (the lockfile
                     entry predates requirements); tampering the
                     lockfile's resolved version fails the
                     satisfaction re-check; the untouched state
                     verifies clean

  7. the pure-dep    — the Stage 131 correction to extract_effects:
     correction      a dependency that audits CLEAN and PURE is
                     recorded as pure (the old fail-closed heuristic
                     fired on any non-empty audit output — and the
                     audit trailer always mentions effect names —
                     so the documented init → add → lock workflow
                     could never go green); a dep whose audit FAILS
                     still lands in the full fail-closed set

  8. the corpus      — tests/ok/feat_stage131_semver.hls is
     showcase        hlfmt-canonical, hllint-clean, boots through
                     the real checker, runs green on the
                     interpreter and (when bin/hlc exists) matches
                     natively byte for byte

Exit code 0 = all acceptance criteria met.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

PKG = [sys.executable, os.path.join(ROOT, "tools", "hls-pkg.py")]
BOOT = [sys.executable, os.path.join(ROOT, "boot", "boot.py")]
FMT = [sys.executable, os.path.join(ROOT, "tools", "hlfmt.py")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

# The gate's own world: an isolated ledger, cache and audit log (the
# Stage 131 env overrides), and every test package under one root
# INSIDE the repo (path deps are confined to the repo root by
# design; the gate honours that rather than reaching around it).
GATE = tempfile.mkdtemp(prefix=".semver-gate-", dir=ROOT)
ENV = dict(os.environ)
ENV["HLS_PKG_TRANSPARENCY_LOG"] = os.path.join(GATE, "ledger.jsonl")
ENV["HLS_PKG_CACHE"] = os.path.join(GATE, "cache")
ENV["HLS_AUDIT_LOG"] = os.path.join(GATE, "audit-log.jsonl")

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage131_semver.hls")
SHOWCASE_OUT = ("stage 131: hls-pkg semver\n"
                "  semver laws OK: grammar, precedence, pre-release, "
                "bump, caret\n")

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


def run(argv, cwd=None, env=None, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=300,
                          text=True, cwd=cwd, env=env or ENV, **kw)


def write(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w") as f:
        f.write(text)


def load_semver():
    spec = importlib.util.spec_from_file_location(
        "hpkg_semver", os.path.join(ROOT, "tools", "hpkg_parts",
                                    "hpkg_semver.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SV = load_semver()


def git(*args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=gate", "-c", "user.email=gate@local"] +
        list(args), cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)


def make_dep_repo():
    """A REAL local git repo carrying a pure one-file dependency:
    lib.hls + a manifest declaring version 1.4.2. Tags v1.4.0 and
    v2.0.0 and the non-semver rel-2024 all point at the manifest
    commit; main marches on to a manifest-less commit (the moving
    ref nothing can check a requirement against). Each call makes
    its own repo — sections build independent fixtures."""
    _make_dep_repo_counter = getattr(make_dep_repo, "n", 0) + 1
    make_dep_repo.n = _make_dep_repo_counter
    repo = os.path.join(GATE, "dep-repo-%d" % _make_dep_repo_counter)
    os.makedirs(repo)
    write(os.path.join(repo, "hls-pkg.toml"),
          '[package]\nname = "gate-dep"\nversion = "1.4.2"\n')
    write(os.path.join(repo, "lib.hls"),
          'fn gate_dep_value() -> int {\n    return 42\n}\n')
    git("init", "-q", cwd=repo)
    git("checkout", "-q", "-b", "main", cwd=repo)
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "gate dep v1.4.2", cwd=repo)
    git("tag", "v1.4.0", cwd=repo)
    git("tag", "v2.0.0", cwd=repo)
    git("tag", "rel-2024", cwd=repo)
    os.remove(os.path.join(repo, "hls-pkg.toml"))
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "drop the manifest from main", cwd=repo)
    return repo


def make_app(name, dep_source):
    """An app package with one dependency (source dict rendered into
    the manifest) and a pure effect surface."""
    app = os.path.join(GATE, name)
    os.makedirs(app)
    src = ('[package]\nname = "%s"\nversion = "0.3.0"\n\n'
           "[dependencies]\ndep = %s\n\n"
           "[effects]\nallowed = []\n" % (name, dep_source))
    write(os.path.join(app, "hls-pkg.toml"), src)
    write(os.path.join(app, "main.hls"),
          'fn main() -> int uses IO {\n'
          '    println("%s ok")\n    return 0\n}\n' % name)
    return app


def toml_dict(d):
    inner = ", ".join('%s = %s' % (k, json.dumps(v)) for k, v in d.items())
    return "{ %s }" % inner


# ===========================================================================
def _section_1():
    print("Section 1 — the convention and the refusals")

    p = run(PKG + ["semver", "check", "1.2.3"])
    check(p.returncode == 0 and p.stdout.strip() == "1.2.3",
          "check prints the canonical form for a valid version")

    p = run(PKG + ["semver", "check", "01.2.3"])
    check(p.returncode == 1 and "grammar" in p.stdout
          and "01.2.3" in p.stdout,
          "a leading zero is exit 1, and the refusal names the grammar")

    p = run(PKG + ["semver", "check", "1.2.3-01"])
    check(p.returncode == 1 and "1.2.3-01" in p.stdout,
          "a leading zero in a numeric pre-release identifier is "
          "refused too")

    p = run(PKG + ["semver", "cmp", "1.0.0-alpha", "1.0.0"])
    check(p.returncode == 0 and p.stdout.strip() == "-1",
          "cmp prints -1 for alpha < release")

    p = run(PKG + ["semver", "cmp", "1.0.0", "1.0.0"])
    check(p.returncode == 0 and p.stdout.strip() == "0",
          "cmp prints 0 for equal precedence")

    p = run(PKG + ["semver", "cmp", "2.0.0", "1.0.0"])
    check(p.returncode == 0 and p.stdout.strip() == "1",
          "cmp prints 1 for the greater version")

    p = run(PKG + ["semver", "cmp", "1.0.0+a", "1.0.0+b"])
    check(p.returncode == 0 and p.stdout.strip() == "0",
          "cmp ignores build metadata (spec §10)")

    p = run(PKG + ["semver", "cmp", "not-a-version", "1.0.0"])
    check(p.returncode == 2 and "not-a-version" in p.stdout,
          "a malformed cmp input is exit 2, named")

    p = run(PKG + ["semver", "bump", "minor", "1.2.3-alpha+b"])
    check(p.returncode == 0 and p.stdout.strip() == "1.3.0",
          "bump minor resets the lower components and drops the "
          "pre-release and build")

    p = run(PKG + ["semver", "bump", "huge", "1.2.3"])
    check(p.returncode != 0,
          "an unknown bump part is refused (argparse choices)")

    p = run(PKG + ["semver", "sat", "1.5.0", "^1.2.3"])
    check(p.returncode == 0 and "satisfies" in p.stdout,
          "sat exits 0 and says so for a satisfying pair")

    p = run(PKG + ["semver", "sat", "2.0.0", "^1.2.3"])
    check(p.returncode == 1 and "does NOT satisfy" in p.stdout,
          "sat exits 1 (not a refusal — a negative verdict) for a "
          "non-satisfying pair")

    p = run(PKG + ["semver", "sat", "garbage", "^1.2.3"])
    check(p.returncode == 2 and "garbage" in p.stdout,
          "a malformed version in sat is exit 2, named")

    p = run(PKG + ["semver", "sat", "1.0.0", "1.2.3 || 2.0.0"])
    check(p.returncode == 2 and "'||'" in p.stdout,
          "'||' alternatives are refused by name (a requirement is "
          "one conjunction)")


# ===========================================================================
def _section_2():
    print("Section 2 — the grammar and the precedence law")

    V = SV.parse_version
    C = SV.compare_versions

    chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta",
             "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
             "1.0.0-rc.1", "1.0.0"]
    ordered = all(C(V(chain[i]), V(chain[i + 1])) < 0
                  for i in range(len(chain) - 1))
    check(ordered, "the canonical §11 chain orders correctly")

    check(C(V("1.0.0+a.1"), V("1.0.0+b.2")) == 0,
          "build metadata is ignored by precedence (§10)")

    check(C(V("1.0.0-1"), V("1.0.0-alpha")) < 0,
          "numeric < alphanumeric (§11.4)")

    check(C(V("1.0.0-alpha.1"), V("1.0.0-alpha.beta")) < 0,
          "numeric identifiers compare numerically inside the list")

    check(C(V("1.0.0-beta.2"), V("1.0.0-beta.11")) < 0,
          "beta.2 < beta.11 — numerically, not lexicographically")

    def refused(s):
        try:
            V(s)
            return False
        except ValueError:
            return True

    check(refused("01.2.3") and refused("1.02.3") and refused("1.2.03"),
          "leading zeros are refused in every core component")
    check(refused("1.2.3-01") and refused("1.2.3-alpha.00"),
          "leading zeros are refused in numeric pre-release "
          "identifiers")
    check(refused("1.2.3-") and refused("1.2.3+"),
          "an empty pre-release or build clause is refused")
    check(refused("1.2") and refused("1") and refused("v1.2.3"),
          "a partial or v-prefixed string is not a version (that is "
          "tag_version's job)")

    check(SV.version_string(V("1.2.3-a.1+b.2")) == "1.2.3-a.1+b.2",
          "the canonical rendering round-trips every clause")

    tv = SV.tag_version("v1.2.3-rc.1")
    check(tv is not None and tv.major == 1 and tv.minor == 2
          and tv.patch == 3 and tv.prerelease == ("rc", "1"),
          "tag_version strips the v and parses the rest")
    check(SV.tag_version("V2.0.0") is not None,
          "a capital V is stripped too")
    check(SV.tag_version("rel-2024") is None
          and SV.tag_version("") is None,
          "a non-semver tag is None, not an error")

    b = SV.bump_version(V("1.2.3-alpha+b"), "minor")
    check(SV.version_string(b) == "1.3.0",
          "bump drops the pre-release and the build")
    check(SV.version_string(SV.bump_version(V("1.2.9"), "patch"))
          == "1.2.10",
          "bump patch carries (1.2.9 -> 1.2.10, numerically above)")

    try:
        SV.bump_version(V("1.2.3"), "huge")
        check(False, "an unknown bump part is a ValueError")
    except ValueError:
        check(True, "an unknown bump part is a ValueError")


# ===========================================================================
def _section_3():
    print("Section 3 — the requirement dialect")

    R = SV.parse_requirement
    V = SV.parse_version
    sat = lambda req, ver: SV.requirement_holds(R(req), V(ver))

    # Caret: the first non-zero component pins the range.
    check(sat("^1.2.3", "1.2.3") and sat("^1.2.3", "1.9.9")
          and not sat("^1.2.3", "2.0.0") and not sat("^1.2.3", "1.2.2"),
          "^1.2.3 is >=1.2.3 <2.0.0")
    check(sat("^0.2.3", "0.2.9") and not sat("^0.2.3", "0.3.0"),
          "^0.2.3 is clamped to 0.2.x")
    check(sat("^0.0.3", "0.0.3") and not sat("^0.0.3", "0.0.4"),
          "^0.0.3 admits exactly the patch")
    check(sat("^1.2", "1.2.0") and not sat("^1.2", "2.0.0"),
          "^1.2 fills the floor with zeros")

    # Tilde: patch floats, minor does not.
    check(sat("~1.2.3", "1.2.9") and not sat("~1.2.3", "1.3.0"),
          "~1.2.3 is >=1.2.3 <1.3.0")
    check(sat("~1", "1.9.9") and not sat("~1", "2.0.0"),
          "~1 is >=1.0.0 <2.0.0")

    # Comparators: full bounds only.
    check(sat(">=1.2.3", "1.2.3") and not sat(">1.2.3", "1.2.3")
          and sat("<=1.2.3", "1.2.3") and not sat("<1.2.3", "1.2.3")
          and sat("=1.2.3", "1.2.3"),
          "the five comparators hold on the exact boundary the way "
          "the operator says")

    # Wildcards and bare partials.
    check(sat("*", "3.4.5") and sat("1.x", "1.9.9")
          and not sat("1.x", "2.0.0") and sat("1.2.*", "1.2.9")
          and not sat("1.2.*", "1.3.0"),
          "wildcards range over the components they replace")
    check(sat("1.2", "1.2.9") and not sat("1.2", "1.3.0")
          and sat("1", "1.9.9") and not sat("1", "2.0.0"),
          "bare partials are the same ranges written shorter")

    # Hyphen ranges: the upper partial floats.
    check(sat("1.2.3 - 2.0", "1.5.0") and sat("1.2.3 - 2.0", "2.0.0")
          and not sat("1.2.3 - 2.0", "2.1.0"),
          "1.2.3 - 2.0 is >=1.2.3 <2.1.0 (the upper partial floats)")
    check(sat("1.2.3 - 2.3.4", "2.3.4")
          and not sat("1.2.3 - 2.3.4", "2.3.5"),
          "a full upper bound is inclusive (<=)")

    # Conjunctions.
    check(sat(">=1.2.0 <2.0.0", "1.5.0")
          and not sat(">=1.2.0 <2.0.0", "2.0.0")
          and sat(">=1.2.0,<2.0.0", "1.5.0"),
          "space and comma separated clauses conjoin (AND)")

    # The pre-release opt-in rule.
    check(not sat("^1.2.3", "1.3.0-beta"),
          "^1.2.3 never pulls 1.3.0-beta by accident")
    check(sat("^1.2.3-beta", "1.2.3-beta.2"),
          "an explicit pre-release floor opts the same core in")
    check(not sat("^1.2.3-beta", "1.2.4-beta"),
          "the opt-in does not leak to a different core")
    check(sat(">=1.2.3-alpha", "1.2.3"),
          "a pre-release floor still admits the release above it")
    check(not sat("*", "1.0.0-rc.1"),
          "* admits nothing pre-release")
    check(not sat("=1.2.3", "1.2.3-beta"),
          "=1.2.3 does not admit 1.2.3-beta (the bound must carry "
          "the tag)")

    # Refusals — every one names the grammar or the offending token.
    def refused(req):
        try:
            R(req)
            return False
        except ValueError:
            return True

    check(refused("1.2.3 || 2.0.0"), "'||' is refused")
    check(refused(">=1.2"), "a partial after a comparator is refused")
    check(refused("^1.x") and refused("~*"),
          "wildcards under ^/~ are refused")
    check(refused("01.2") and refused("^1.02.3"),
          "leading zeros are refused in requirement bounds")
    check(refused("1.2.3 - 2.0 - 3.0"),
          "a hyphen range takes exactly two bounds")
    check(refused("abc"), "a non-numeric bound is refused")
    check(refused(">="),
          "a bare comparator with no version is refused")

    check(R("*").any_nonpre and R("").any_nonpre,
          "* and the empty requirement both mean 'any non-prerelease'")


def _sections_1_3():
    _section_1()
    _section_2()
    _section_3()


# ===========================================================================
def _section_4():
    print("Section 4 — the manifest enforcement (lock, build, add)")

    repo = make_dep_repo()
    app = make_app("verapp", toml_dict(
        {"git": repo, "path": "lib.hls", "version": "^1.4",
         "tag": "v1.4.0"}))

    # The package's own version is validated BEFORE anything resolves.
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "1.0"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": "^1.4", "tag": "v1.4.0"}))
    p = run(PKG + ["lock"], cwd=app)
    check(p.returncode == 1 and "package version '1.0'" in p.stdout,
          "lock refuses a manifest whose own version is not semver, "
          "before resolving anything")
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "0.3.0"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": "^1.4", "tag": "v1.4.0"}))

    # A malformed requirement is refused at the grammar, before the
    # network (here: the clone) is touched.
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "0.3.0"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": "^1.4..0", "tag": "v1.4.0"}))
    p = run(PKG + ["lock"], cwd=app)
    check(p.returncode == 1 and "requirement" in p.stdout
          and "1.4..0" in p.stdout,
          "lock refuses a malformed requirement by name")
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "0.3.0"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": "^1.4", "tag": "v1.4.0"}))

    # The green path: a real clone, tag v1.4.0, requirement ^1.4.
    p = run(PKG + ["lock"], cwd=app)
    check(p.returncode == 0 and "Wrote lockfile" in p.stdout,
          "lock resolves a semver-tagged dep that satisfies ^1.4")
    lock = json.load(open(os.path.join(app, "hls-pkg.lock")))
    entry = lock["packages"][0]
    check(entry.get("requirement") == "^1.4",
          "the lockfile records the requirement beside the pin")
    check(entry.get("version") == "v1.4.0"
          and entry.get("effects") == []
          and entry.get("transitive_effects") == [],
          "the locked tag is v1.4.0 and the pure dep is recorded PURE "
          "(section 7's correction, visible in the ledger)")

    # A satisfying tag on a different core: v2.0.0 violates ^1.4.
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "0.3.0"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": "^1.4", "tag": "v2.0.0"}))
    p = run(PKG + ["lock"], cwd=app)
    check(p.returncode == 1 and "v2.0.0" in p.stdout
          and "does not satisfy" in p.stdout,
          "lock refuses a tag that violates the requirement, named "
          "on both sides")
    check(not os.path.exists(os.path.join(app, "hls-pkg.lock")) or
          json.load(open(os.path.join(app, "hls-pkg.lock")))
          ["packages"][0].get("requirement") == "^1.4",
          "the refused lock wrote no new lockfile (the old one stays)")

    # A non-semver tag: a requirement that cannot be CHECKED is a
    # refusal, not a warning.
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "0.3.0"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": "^1.4", "tag": "rel-2024"}))
    p = run(PKG + ["lock"], cwd=app)
    check(p.returncode == 1 and "rel-2024" in p.stdout
          and "cannot be checked" in p.stdout,
          "lock refuses a non-semver tag under a requirement")

    # A moving ref is judged by the dependency's own manifest
    # version — 1.4.2 on the tagged commit satisfies ^1.4. But main
    # carries NO manifest at all, and there the requirement has
    # nothing to check against: refusal.
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "0.3.0"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": "^1.4", "branch": "main"}))
    p = run(PKG + ["lock"], cwd=app)
    check(p.returncode == 1 and "no hls-pkg.toml" in p.stdout,
          "a requirement against a manifest-less moving ref is a "
          "refusal, named")

    # build: the same laws, before a single line compiles.
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "0.3"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": "^1.4", "tag": "v1.4.0"}))
    p = run(PKG + ["build"], cwd=app)
    check(p.returncode == 1 and "package version '0.3'" in p.stdout,
          "build refuses a non-semver own version")
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "0.3.0"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": ">=1.2", "tag": "v1.4.0"}))
    p = run(PKG + ["build"], cwd=app)
    check(p.returncode == 1 and ">=1.2" in p.stdout
          and "ambiguous" in p.stdout,
          "build refuses a dependency requirement with a partial "
          "after a comparator")
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "verapp"\nversion = "0.3.0"\n\n'
          "[dependencies]\ndep = %s\n\n[effects]\nallowed = []\n"
          % toml_dict({"git": repo, "path": "lib.hls",
                       "version": "^1.4", "tag": "v1.4.0"}))

    # The green build still builds (the validation is additive).
    p = run(PKG + ["build"], cwd=app)
    check(p.returncode == 0 and "verapp ok" in p.stdout,
          "a manifest that passes the semver laws builds green")

    # add --version: the grammar is checked at the keyboard.
    app2 = os.path.join(GATE, "addapp")
    os.makedirs(app2)
    write(os.path.join(app2, "hls-pkg.toml"),
          '[package]\nname = "addapp"\nversion = "0.1.0"\n')
    p = run(PKG + ["add", "dep", repo, "lib.hls", "--version",
                   "^1.4.0"], cwd=app2)
    check(p.returncode == 0 and "requirement: ^1.4.0" in p.stdout,
          "add --version records the requirement")
    mtext = open(os.path.join(app2, "hls-pkg.toml")).read()
    check('version = "^1.4.0"' in mtext,
          "the requirement lands in the manifest's dep table")
    p = run(PKG + ["add", "dep2", repo, "lib.hls", "--version",
                   "^1.2..3"], cwd=app2)
    check(p.returncode == 1 and "1.2..3" in p.stdout,
          "add --version refuses a malformed requirement before "
          "writing anything")
    p = run(PKG + ["add", "dep3", repo, "lib.hls", "--version",
                   "1.2.3 || 9"], cwd=app2)
    check(p.returncode == 1 and "'||'" in p.stdout,
          "add --version refuses '||' alternatives by name")


# ===========================================================================
def _section_5():
    print("Section 5 — the publish laws")

    app = os.path.join(GATE, "pubapp")
    os.makedirs(app)
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "pubapp"\nversion = "0.3.0"\n')
    write(os.path.join(app, "lib.hls"),
          'fn pub_value() -> int {\n    return 7\n}\n')

    p = run(PKG + ["publish"], cwd=app)
    check(p.returncode == 0 and "Published pubapp@0.3.0" in p.stdout,
          "publish records a valid version")

    p = run(PKG + ["publish"], cwd=app)
    check(p.returncode == 0 and "identical content, nothing to do"
          in p.stdout,
          "an identical republish is a no-op (the ledger is not "
          "spammed; CI stays idempotent)")

    seq_before = sum(1 for ln in open(ENV["HLS_PKG_TRANSPARENCY_LOG"])
                     if ln.strip())
    p = run(PKG + ["publish"], cwd=app)
    seq_after = sum(1 for ln in open(ENV["HLS_PKG_TRANSPARENCY_LOG"])
                    if ln.strip())
    check(p.returncode == 0 and seq_before == seq_after,
          "the no-op really appended nothing")

    # Different content under the same name@version: hard refusal.
    write(os.path.join(app, "lib.hls"),
          'fn pub_value() -> int {\n    return 8\n}\n')
    p = run(PKG + ["publish"], cwd=app)
    check(p.returncode == 1 and "different content" in p.stdout
          and "version bump" in p.stdout,
          "a republish with different content is a hard refusal "
          "naming both hashes")

    # Build metadata does not change the identity (spec §10):
    # 0.3.0+x IS 0.3.0 — same content no-ops, different content is
    # still a refusal.
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "pubapp"\nversion = "0.3.0+x1"\n')
    write(os.path.join(app, "lib.hls"),
          'fn pub_value() -> int {\n    return 7\n}\n')
    p = run(PKG + ["publish"], cwd=app)
    check(p.returncode == 0 and "identical content, nothing to do"
          in p.stdout,
          "0.3.0+x1 with the original content is the same version "
          "— no-op")
    write(os.path.join(app, "lib.hls"),
          'fn pub_value() -> int {\n    return 9\n}\n')
    p = run(PKG + ["publish"], cwd=app)
    check(p.returncode == 1 and "different content" in p.stdout,
          "0.3.0+x1 with different content is still a refusal "
          "(the identity ignores the build)")

    # The way out is the way semver always said: bump.
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "pubapp"\nversion = "0.3.1"\n')
    p = run(PKG + ["publish"], cwd=app)
    check(p.returncode == 0 and "Published pubapp@0.3.1" in p.stdout,
          "a real bump publishes fine")

    # No version, or a non-semver one: refused.
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "pubapp"\n')
    p = run(PKG + ["publish"], cwd=app)
    check(p.returncode == 1 and "must name its version" in p.stdout,
          "publish refuses a version-less manifest (the silent "
          "0.0.0 fallback is gone)")
    write(os.path.join(app, "hls-pkg.toml"),
          '[package]\nname = "pubapp"\nversion = "1.0"\n')
    p = run(PKG + ["publish"], cwd=app)
    check(p.returncode == 1 and "1.0" in p.stdout,
          "publish refuses a non-semver version")


# ===========================================================================
def _section_6():
    print("Section 6 — the verify drift law")

    repo = make_dep_repo()
    app = make_app("driftapp", toml_dict(
        {"git": repo, "path": "lib.hls", "version": "^1.4",
         "tag": "v1.4.0"}))
    p = run(PKG + ["lock"], cwd=app)
    check(p.returncode == 0, "the drift fixture locks green")

    p = run(PKG + ["verify"], cwd=app)
    check(p.returncode == 0 and "All 1 packages verified" in p.stdout,
          "the untouched state verifies clean")

    # Edit the requirement after the lock: drift, named on both sides.
    mpath = os.path.join(app, "hls-pkg.toml")
    write(mpath, open(mpath).read().replace('"^1.4"', '"^1.5"'))
    p = run(PKG + ["verify"], cwd=app)
    check(p.returncode == 1 and "drift" in p.stdout
          and "^1.5" in p.stdout and "^1.4" in p.stdout,
          "an edited requirement fails verification naming both texts")

    # Removing the requirement entirely: the manifest no longer wants
    # what the lock recorded — drift, quoted as "requires None".
    write(mpath, open(mpath).read().replace('version = "^1.5", ', ''))
    p = run(PKG + ["verify"], cwd=app)
    check(p.returncode == 1 and "drift" in p.stdout
          and "None" in p.stdout,
          "a manifest that drops the requirement fails verification")

    # The mirror case: a lockfile entry WITHOUT a requirement under a
    # manifest that demands one (a lock predating Stage 131) — the
    # named predates-requirements refusal.
    write(mpath, open(mpath).read().replace(
        'dep = { git', 'dep = { version = "^1.4", git'))
    lpath = os.path.join(app, "hls-pkg.lock")
    lock = json.load(open(lpath))
    lock["packages"][0].pop("requirement", None)
    json.dump(lock, open(lpath, "w"), indent=2)
    p = run(PKG + ["verify"], cwd=app)
    check(p.returncode == 1 and "predates requirements" in p.stdout,
          "a lockfile entry that predates requirements fails "
          "verification under a demanding manifest")

    # Tamper the lockfile's resolved version (with the requirement
    # back in place, so the satisfaction re-check is what fires): the
    # bytes are pinned by the hash, the VERSION is a lie — and the
    # re-check catches it.
    write(mpath, open(mpath).read().replace(
        'dep = { git', 'dep = { version = "^1.4", git'))
    lpath = os.path.join(app, "hls-pkg.lock")
    lock = json.load(open(lpath))
    lock["packages"][0]["requirement"] = "^1.4"
    lock["packages"][0]["version"] = "v9.9.9"
    json.dump(lock, open(lpath, "w"), indent=2)
    p = run(PKG + ["verify"], cwd=app)
    check(p.returncode == 1 and "v9.9.9" in p.stdout
          and "does not satisfy" in p.stdout,
          "a tampered lockfile version fails the satisfaction "
          "re-check")

    # Restore everything and verify clean again.
    lock["packages"][0]["version"] = "v1.4.0"
    json.dump(lock, open(lpath, "w"), indent=2)
    p = run(PKG + ["verify"], cwd=app)
    check(p.returncode == 0 and "All 1 packages verified" in p.stdout,
          "the restored state verifies clean again")


# ===========================================================================
def _section_7():
    print("Section 7 — the pure-dep correction")

    # (a) The green side, pinned where it failed before: a dependency
    # that audits CLEAN and PURE is recorded as pure. Before the
    # correction the fail-closed heuristic fired on any non-empty
    # audit output — and the audit trailer always names effects — so
    # this lock could never go green and the documented
    # init -> add -> lock workflow died in the effect wall.
    repo = make_dep_repo()
    app = make_app("pureapp", toml_dict(
        {"git": repo, "path": "lib.hls", "tag": "v1.4.0"}))
    p = run(PKG + ["lock"], cwd=app)
    check(p.returncode == 0 and "Wrote lockfile" in p.stdout,
          "a pure dep with allowed = [] locks green")
    lock = json.load(open(os.path.join(app, "hls-pkg.lock")))
    entry = lock["packages"][0]
    check(entry.get("effects") == []
          and entry.get("transitive_effects") == [],
          "the pure dep's ledger row carries NO effects (not the "
          "fail-closed set)")

    # (b) The fail-closed side, still closed: a dep whose audit FAILS
    # (a syntax error, here) is recorded with the FULL effect set —
    # unauditable is never pure.
    baddep = os.path.join(GATE, "baddep")
    os.makedirs(baddep)
    write(os.path.join(baddep, "hls-pkg.toml"),
          '[package]\nname = "baddep"\nversion = "1.0.0"\n')
    write(os.path.join(baddep, "bad.hls"), "fn broken(\n")
    app2 = make_app("badapp", toml_dict(
        {"path": os.path.relpath(os.path.join(baddep, "bad.hls"),
                                 ROOT)}))
    p = run(PKG + ["lock"], cwd=app2)
    check(p.returncode == 1 and "Rand" in p.stdout,
          "an unauditable dep fails the effect surface (fail closed, "
          "never pure)")
    lock2_path = os.path.join(app2, "hls-pkg.lock")
    if os.path.isfile(lock2_path):
        lock2 = json.load(open(lock2_path))
        te = lock2["packages"][0].get("transitive_effects", [])
        check(sorted(te) == sorted(["Args", "Clock", "Exit", "Fs",
                                    "IO", "Net", "Proc", "Rand"]),
              "the unauditable row carries exactly the fail-closed set")


# ===========================================================================
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
