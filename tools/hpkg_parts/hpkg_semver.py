"""semver — Stage 131 (v0.149.0-alpha): Semantic-versioning enforcement
for hls-pkg.

The roadmap line is "Semantic-versioning enforcement in `hls-pkg`".
Before this module, versions in the manifest were opaque strings: a
package could publish `version = "1.0"`, `"final"`, or `"2"` — and a
dependency could pin `tag = "v2.0.0"` while believing it asked for
1.x. This module makes a version a CONTRACT with three laws, each
enforced at the point where the version enters the toolchain:

  1. The grammar. A version is MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]
     per semver 2.0.0 — no leading zeros in the core or in numeric
     pre-release identifiers, pre-release/build identifiers restricted
     to [0-9A-Za-z-]. `hls-pkg lock`, `build` and `publish` refuse a
     manifest whose own version (or any dependency requirement) does
     not parse; `publish` additionally REFUSES to publish a package
     with no version at all (the silent "0.0.0" fallback is gone —
     a publish must name its version).

  2. Precedence. compare() implements spec §11 exactly: numeric
     identifiers compare numerically, alphanumeric ASCII-lexically,
     numeric < alphanumeric, a longer pre-release wins over its
     prefix, and build metadata is ignored. The canonical §11 chain
     (1.0.0-alpha < 1.0.0-alpha.1 < ... < 1.0.0-rc.1 < 1.0.0) is
     pinned by the acceptance gate.

  3. Compatibility. A dependency may declare `version = "<req>"` in
     its source table (`hls-pkg add --version`). Requirements support
     caret (^), tilde (~), comparators (>= > <= < =), wildcards
     (*, x, X — bare positions only), partial versions (1.2 means
     >=1.2.0 <1.3.0), hyphen ranges (1.2.3 - 2.0), and space/comma
     separated conjunctions. `||` alternatives are refused BY NAME —
     a requirement is one conjunction, and a resolver that guesses
     between alternatives is a resolver that lies. Pre-releases are
     OPT-IN (the npm rule): a pre-release version satisfies a
     requirement only when some comparator carries a pre-release tag
     on the same (major, minor, patch) tuple — so ^1.2.3 never pulls
     1.3.0-beta by accident.

Enforcement points:
  - `hls-pkg add --version REQ`  — the requirement grammar is checked
    BEFORE it reaches the manifest.
  - `hls-pkg lock`               — the package's own version must
    parse; every dependency requirement must parse; and each pinned
    ref must SATISFY its requirement: a `tag` must be a semver tag
    (v1.2.3 — one optional leading v is stripped), and a moving ref
    (branch/main/path) must carry its own manifest with a version
    that satisfies the requirement. A requirement that cannot be
    CHECKED is a refusal, not a warning — claiming ^1.2 while pinned
    to a moving branch is exactly the lie this stage exists to stop.
  - `hls-pkg build`              — the same manifest validation as
    lock, before a single line is compiled.
  - `hls-pkg publish`            — valid version REQUIRED (no
    fallback), plus the immutability law: a name@version already in
    the transparency log with DIFFERENT content is a hard refusal
    (semver requires a bump); identical content is a no-op (the log
    is not spammed, CI stays idempotent).
  - `hls-pkg verify`             — re-checks every lockfile entry
    against its recorded requirement AND against the manifest's
    CURRENT requirement: editing the manifest after locking is
    drift, and drift is a verification failure.
  - `hls-pkg semver check|cmp|bump|sat` — the same laws as a CLI,
    so scripts and CI can gate on them without a manifest.

Exit-code convention (matching hls-migrate): usage/convention errors
are exit 2, refusals and failures are exit 1.

The reference grammar is https://semver.org/spec/v2.0.0.html; the
requirement dialect follows npm's semantics (docs.npmjs.com/misc/
semver) where the two disagree, and every deviation from npm is named
in this file.
"""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hpkg_common import (
    Dict, List, Optional, Tuple, _re_mod,
)

# ---------------------------------------------------------------------------
# The version grammar (semver 2.0.0 §2).
# ---------------------------------------------------------------------------

# Numeric identifier: 0, or a decimal without a leading zero. This is
# the spec's own EBNF (<numeric identifier> ::= "0" / ["1"-"9"]
# ["0"-"9"]*), not an accident: "01.2.3" is INVALID, and a parser that
# accepts it by accident will silently rank "1.02.3" above "1.2.3".
_NUM = r"(?:0|[1-9][0-9]*)"
# Pre-release identifier: numeric, or alphanumeric with at least one
# letter/hyphen (so "alpha", "alpha-1", "0beta" — but NOT "00", which
# would be a numeric identifier with a leading zero).
_PRE_ID = r"(?:%s|%s)" % (_NUM, r"[0-9]*[A-Za-z-][0-9A-Za-z-]*")
# Build identifier: anything alphanumeric+hyphen; leading zeros are
# FINE here (build metadata carries no precedence, spec §10).
_BUILD_ID = r"[0-9A-Za-z-]+"

_VERSION_RE = _re_mod.compile(
    r"^(%s)\.(%s)\.(%s)" % (_NUM, _NUM, _NUM)
    + r"(?:-((?:%s)(?:\.(?:%s))*))?" % (_PRE_ID, _PRE_ID)
    + r"(?:\+((?:%s)(?:\.(?:%s))*))?$" % (_BUILD_ID, _BUILD_ID))

# The precise sub-grammar for error messages: which clause failed.
_CORE_ONLY_RE = _re_mod.compile(r"^(%s)\.(%s)\.(%s)" % (_NUM, _NUM, _NUM))

GRAMMAR = ("MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD] per semver 2.0.0 "
           "(no leading zeros in the core or in numeric pre-release "
           "identifiers)")


class SemVer:
    """A parsed semantic version.

    `prerelease` and `build` are tuples of the raw identifier strings
    (the source text is the contract — `version_string()` re-renders
    exactly what was parsed, joined canonically). Comparison goes
    through `key()`: the core triple, then the pre-release encoded so
    spec §11 falls out of tuple comparison — each identifier becomes
    (0, n, "") when numeric and (1, 0, s) when alphanumeric (numeric
    < alphanumeric, §11.4), and a shorter pre-release list sorts below
    its own extension because Python tuples compare prefix-first
    (§11.3). Build metadata is NOT in the key (§10: ignored for
    precedence)."""

    __slots__ = ("major", "minor", "patch", "prerelease", "build", "text")

    def __init__(self, major: int, minor: int, patch: int,
                 prerelease: Tuple[str, ...] = (), build: Tuple[str, ...] = (),
                 text: str = ""):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = tuple(prerelease)
        self.build = tuple(build)
        self.text = text

    # -- spec §11 precedence ------------------------------------------------
    def _pre_key(self) -> Tuple:
        """The pre-release identifiers encoded so TUPLE COMPARISON IS
        spec §11.3: each identifier becomes (0, n, "") when numeric
        and (1, 0, s) when alphanumeric (numeric < alphanumeric,
        §11.4), and a plain prefix comparison settles the rest — a
        longer identifier list beats its own prefix (1.0.0-alpha <
        1.0.0-alpha.1 < 1.0.0-alpha.beta), because Python tuples
        compare position-by-position and the shorter prefix loses at
        the position where the other continues. There is deliberately
        NO end sentinel here: release-vs-prerelease is settled by the
        has-pre flag in precedence_key (§11.2), and a sentinel inside
        the list would rank 'alpha' above 'alpha.1' — the exact
        opposite of the spec."""
        out = []
        for ident in self.prerelease:
            if ident.isdigit():
                # §11.4: numeric identifiers MUST NOT include leading
                # zeros — the grammar already refuses them, so int()
                # is lossless here.
                out.append((0, int(ident), ""))
            else:
                out.append((1, 0, ident))
        return tuple(out)

    def precedence_key(self) -> Tuple:
        # The has-pre flag (0 = pre-release, 1 = release) encodes
        # §11.2 — any pre-release sorts below its own release — while
        # keeping prerelease-vs-prerelease comparisons inside the
        # plain prefix law of _pre_key.
        return (self.major, self.minor, self.patch,
                0 if self.prerelease else 1, self._pre_key())

    def __eq__(self, other):
        return isinstance(other, SemVer) and \
            self.precedence_key() == other.precedence_key()

    def __lt__(self, other):
        return self.precedence_key() < other.precedence_key()

    def __le__(self, other):
        return self.precedence_key() <= other.precedence_key()

    def __hash__(self):
        return hash(self.precedence_key())

    def __repr__(self):
        return "SemVer(%s)" % version_string(self)


def parse_version(text: str, what: str = "version") -> SemVer:
    """Parse a strict semver 2.0.0 version; raise ValueError with the
    precise failing clause otherwise. `what` names the value in the
    error ("package version", "tag v1.2.3", ...)."""
    if not isinstance(text, str):
        raise ValueError("%s must be a string, got %s"
                         % (what, type(text).__name__))
    s = text.strip()
    if not s:
        raise ValueError("%s is empty (the grammar is: %s)" % (what, GRAMMAR))
    m = _VERSION_RE.match(s)
    if not m:
        # Name the clause that failed — a message that only says
        # "invalid" sends the user back to the spec for what a
        # one-line error could have said.
        if not _CORE_ONLY_RE.match(s):
            raise ValueError(
                "%s %r is not MAJOR.MINOR.PATCH (the grammar is: %s)"
                % (what, text, GRAMMAR))
        if "+" in s and "-" in s.split("+", 1)[0] and \
                _CORE_ONLY_RE.match(s.split("-", 1)[0]) and \
                not _VERSION_RE.match(s):
            raise ValueError(
                "%s %r has a malformed PRERELEASE clause (identifiers "
                "must be [0-9A-Za-z-], dot-separated; numeric ones "
                "without leading zeros)" % (what, text))
        raise ValueError("%s %r is not a valid semantic version "
                         "(the grammar is: %s)" % (what, text, GRAMMAR))
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre = tuple(m.group(4).split(".")) if m.group(4) else ()
    build = tuple(m.group(5).split(".")) if m.group(5) else ()
    return SemVer(major, minor, patch, pre, build, text=s)


def try_parse_version(text: str) -> Optional[SemVer]:
    """parse_version, but None instead of an exception — for the
    call sites that PROBE (does this tag carry a version?)."""
    try:
        return parse_version(text)
    except ValueError:
        return None


def version_string(v: SemVer) -> str:
    """Canonical rendering — exactly what the grammar produces."""
    s = "%d.%d.%d" % (v.major, v.minor, v.patch)
    if v.prerelease:
        s += "-" + ".".join(v.prerelease)
    if v.build:
        s += "+" + ".".join(v.build)
    return s


def compare_versions(a: SemVer, b: SemVer) -> int:
    """-1 / 0 / 1 by spec §11 precedence (build metadata ignored)."""
    ka, kb = a.precedence_key(), b.precedence_key()
    if ka < kb:
        return -1
    if ka > kb:
        return 1
    return 0


def bump_version(v: SemVer, part: str) -> SemVer:
    """Bump major/minor/patch; the lower components reset to zero and
    the pre-release/build drop (semver's own §"how do I know when to
    release": leaving a pre-release on a bumped version would claim
    the bump is not finished)."""
    if part == "major":
        return SemVer(v.major + 1, 0, 0, text=version_string(v))
    if part == "minor":
        return SemVer(v.major, v.minor + 1, 0, text=version_string(v))
    if part == "patch":
        return SemVer(v.major, v.minor, v.patch + 1, text=version_string(v))
    raise ValueError("bump part must be major, minor or patch, got %r" % part)


def tag_version(tag: str) -> Optional[SemVer]:
    """A git tag like `v1.2.3` or `v1.2.3-rc.1` -> its version, or
    None when the tag is not semver-shaped (one optional leading `v`
    is stripped — the repo's own tags are v-prefixed)."""
    if not isinstance(tag, str) or not tag:
        return None
    body = tag[1:] if tag[0] in "vV" else tag
    return try_parse_version(body)


# ---------------------------------------------------------------------------
# Requirements.
# ---------------------------------------------------------------------------

_REQ_GRAMMAR = ("requirement grammar: ^1.2.3, ~1.2, >=1.2.3, >1.2.3, "
                "<=1.2.3, <1.2.3, =1.2.3, exact 1.2.3, partials 1.2 / 1, "
                "wildcards * / 1.x / 1.2.*, hyphen ranges "
                "'1.2.3 - 2.0' — space or comma separated for AND; "
                "'||' alternatives are not supported")


class Comparator:
    """One clause: op over a bound version. `op` is one of >=, <=, >,
    <, = (or "" for the implicit-equality of a bare exact version —
    normalised to "=" at parse time, so the class never sees "")."""
    __slots__ = ("op", "bound")

    def __init__(self, op: str, bound: SemVer):
        self.op = op
        self.bound = bound

    def holds(self, v: SemVer) -> bool:
        c = compare_versions(v, self.bound)
        if self.op == ">=":
            return c >= 0
        if self.op == ">":
            return c > 0
        if self.op == "<=":
            return c <= 0
        if self.op == "<":
            return c < 0
        return c == 0

    def __repr__(self):
        return "%s%s" % (self.op, version_string(self.bound))


class Requirement:
    """A parsed requirement: a conjunction of comparators, plus the
    wildcard flag (`*` alone matches anything non-prerelease). The
    pre-release opt-in rule lives in requirement_holds, not here —
    satisfaction is a property of the requirement, not of a clause."""

    __slots__ = ("comparators", "any_nonpre", "text")

    def __init__(self, comparators: List[Comparator],
                 any_nonpre: bool = False, text: str = ""):
        self.comparators = comparators
        self.any_nonpre = any_nonpre
        self.text = text

    def __repr__(self):
        return "Requirement(%s)" % requirement_string(self)


def requirement_string(req: Requirement) -> str:
    """Canonical rendering — what error messages and the lockfile
    quote. The law: the lockfile quotes the requirement as WRITTEN
    (req.text), the canonical form is for messages that need to show
    what the requirement MEANS."""
    parts = [repr(c) for c in req.comparators]
    if req.any_nonpre:
        parts.append(">=0.0.0 (any non-prerelease)")
    return " ".join(parts) if parts else "*"


def _parse_partial(s: str, what: str) -> Tuple[int, int, int, int, bool]:
    """A bare core: 1 / 1.2 / 1.2.3 / 1.2.* -> (major, minor, patch,
    given, had_wildcard) where `given` counts how many components were
    written before the first wildcard. Raises on anything malformed.
    Numbers here are grammar-checked by hand: the core's
    no-leading-zero rule applies to partials too."""
    parts = s.split(".")
    if len(parts) > 3:
        raise ValueError("%s %r has too many dots (at most MAJOR.MINOR."
                         "PATCH)" % (what, s))
    nums = []
    wildcard = False
    for p in parts:
        if p in ("*", "x", "X"):
            wildcard = True
            break
        if not p.isdigit():
            raise ValueError("%s %r: %r is not a number (the core "
                             "allows digits only, no leading zeros)"
                             % (what, s, p))
        if len(p) > 1 and p[0] == "0":
            raise ValueError("%s %r: %r has a leading zero (semver "
                             "2.0.0 refuses it)" % (what, s, p))
        nums.append(int(p))
    given = len(nums)  # BEFORE padding — the components actually written
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2], given, wildcard


def _full_version_after(op: str, rest: str, what: str) -> SemVer:
    """The bound of an explicit comparator: a FULL MAJOR.MINOR.PATCH
    core (pre-release/build allowed). A partial after >= / > / < / <=
    / = is refused BY NAME — `>=1.2` is ambiguous at exactly the case
    that matters (does 1.2.0 itself satisfy it? does 1.2.9?) and this
    stage refuses to guess. Bare and hyphen-range partials are the
    well-defined forms and stay legal."""
    try:
        return parse_version(rest, what)
    except ValueError:
        # Distinguish "partial" (named advice) from "garbage" (the
        # plain grammar error).
        m = _re_mod.match(r"^\s*(\d+(?:\.\d+)?(?:\.\d+)?)[-.+]", rest)
        core = m.group(1) if m else rest.split("-")[0].split("+")[0].strip()
        if core and len(core.split(".")) < 3 and \
                _re_mod.fullmatch(r"\d+(?:\.\d+)*", core):
            raise ValueError(
                "%s: %s%s needs a full MAJOR.MINOR.PATCH bound — a "
                "partial after %s is ambiguous (write %s.0 explicitly)"
                % (what, op, rest, op, core)) from None
        raise


def _caret_range(maj: int, mino: int, pat: int, given: int,
                 pre: Tuple[str, ...]) -> List[Comparator]:
    """^ semantics (npm): the first NON-ZERO component pins the range.
    ^1.2.3 -> <2.0.0; ^0.2.3 -> <0.3.0; ^0.0.3 -> <0.0.4. Partials
    fill with zeros first (^0.2 -> >=0.2.0 <0.3.0, ^1 -> >=1.0.0
    <2.0.0), so a missing component can never be the non-zero one."""
    low = SemVer(maj, mino, pat, pre)
    if maj > 0:
        high = SemVer(maj + 1, 0, 0)
    elif given == 1:
        high = SemVer(1, 0, 0)          # ^0 -> <1.0.0
    elif mino > 0 or given == 2:
        high = SemVer(0, mino + 1, 0)   # ^0.2.3, ^0.2, ^0.0 -> <0.3.0 / <0.1.0
    else:
        high = SemVer(0, 0, pat + 1)    # ^0.0.3 -> <0.0.4
    return [Comparator(">=", low), Comparator("<", high)]


def parse_requirement(text: str, what: str = "requirement") -> Requirement:
    """Parse a version requirement; raise ValueError naming the
    grammar otherwise. The dialect (documented in the module header):

      ^1.2.3          >=1.2.3 <2.0.0      (caret: first non-zero pins)
      ~1.2.3          >=1.2.3 <1.3.0      (tilde: patch may float)
      ~1.2            >=1.2.0 <1.3.0
      ~1              >=1.0.0 <2.0.0
      >=X.Y.Z etc.    full bound required (a partial after a
                      comparator is refused — named, not guessed)
      1.2.3           exactly 1.2.3
      1.2 / 1.x       >=1.2.0 <1.3.0 / >=1.0.0 <2.0.0
      1.2.3 - 2.0     >=1.2.3 <2.1.0        (hyphen range; upper
                      partial floats: 2.0 -> <2.1.0, 2 -> <3.0.0)
      *               any non-prerelease
      a, b / a b      conjunction (AND)

    Pre-release opt-in (npm): a pre-release version satisfies the
    requirement only if some comparator carries a pre-release tag on
    the same (major, minor, patch) tuple — enforced in
    requirement_holds."""
    if not isinstance(text, str):
        raise ValueError("%s must be a string, got %s"
                         % (what, type(text).__name__))
    s = text.strip()
    if "||" in s:
        raise ValueError("%s %r uses '||' alternatives — %s"
                         % (what, text, _REQ_GRAMMAR))
    if not s or s == "*":
        return Requirement([], any_nonpre=True, text=s)
    # Hyphen range: ` - ` with surrounding spaces (a pre-release's
    # hyphens never carry spaces, so the split is unambiguous). One
    # range per requirement; conjunction with further comparators is
    # allowed (npm allows `1.2.3 - 2 >=1.2.7`-style lists too, and
    # the AND reading is the honest one).
    if " - " in s:
        pieces = s.split(" - ")
        if len(pieces) != 2:
            raise ValueError("%s %r: a hyphen range takes exactly two "
                             "bounds (%s)" % (what, text, _REQ_GRAMMAR))
        lo_s, hi_s = pieces[0].strip(), pieces[1].strip()
        extra = ""
        # Comparators after the range join the conjunction.
        hi_parts = hi_s.split(None, 1)
        if len(hi_parts) == 2:
            hi_s, extra = hi_parts[0].strip(), hi_parts[1].strip()
        lo_maj, lo_min, lo_pat, lo_given, _ = _parse_partial(lo_s, what)
        lo = SemVer(lo_maj, lo_min, lo_pat)
        comparators = [Comparator(">=", lo)]
        hi_maj, hi_min, hi_pat, hi_given, _ = _parse_partial(hi_s, what)
        if hi_given == 3:
            comparators.append(Comparator("<=", SemVer(hi_maj, hi_min, hi_pat)))
        elif hi_given == 2:
            comparators.append(Comparator("<", SemVer(hi_maj, hi_min + 1, 0)))
        else:
            comparators.append(Comparator("<", SemVer(hi_maj + 1, 0, 0)))
        if extra:
            for tok in _split_tokens(extra):
                comparators.extend(_parse_token(tok, what))
        req = Requirement(comparators, text=text)
        return req
    comparators: List[Comparator] = []
    for tok in _split_tokens(s):
        comparators.extend(_parse_token(tok, what))
    if not comparators:
        # Only wildcards — matches any non-prerelease.
        return Requirement([], any_nonpre=True, text=text)
    return Requirement(comparators, text=text)


def _split_tokens(s: str) -> List[str]:
    """Split on commas and/or whitespace."""
    out: List[str] = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if chunk:
            out.extend(chunk.split())
    return out


def _parse_token(tok: str, what: str) -> List[Comparator]:
    if not tok:
        return []
    if tok == "*" or tok in ("x", "X"):
        return []
    # Caret / tilde.
    if tok[0] == "^" or tok[0] == "~":
        body = tok[1:]
        if not body:
            raise ValueError("%s: %r has no version after %r (%s)"
                             % (what, tok, tok[0], _REQ_GRAMMAR))
        # Split the pre-release off FIRST — the core parser only sees
        # digits and dots, and a pre-release identifier may contain
        # digits and hyphens of its own (1.2.3-beta-1 is one floor
        # with a two-identifier tag, not "1.2" + "3-beta").
        pre: Tuple[str, ...] = ()
        core_body = body
        if "-" in body:
            core_body, pre_part = body.split("-", 1)
            pre = tuple(pre_part.split(".")) if pre_part else ()
            if not pre or any(not _pre_id_ok(p) for p in pre):
                raise ValueError("%s: %r has a malformed pre-release "
                                 "identifier" % (what, tok))
        maj, mino, pat, given, wildcard = _parse_partial(core_body, what)
        # A pre-release on the floor carries through: ^1.2.3-beta has
        # floor 1.2.3-beta (the opt-in clause that lets 1.2.3-beta.2
        # in). Wildcards under ^ / ~ are refused — the range they
        # would produce is exactly the bare partial's range, so the
        # ^ / ~ is noise, and noise in a contract is a lie.
        if wildcard:
            raise ValueError("%s: wildcards under %r are not supported "
                             "(write the bare partial instead)"
                             % (what, tok[0]))
        if tok[0] == "^":
            return _caret_range(maj, mino, pat, given, pre)
        # Tilde: the given components are the floor, patch floats.
        low = SemVer(maj, mino, pat, pre)
        if given >= 2:
            high = SemVer(maj, mino + 1, 0)
        else:
            high = SemVer(maj + 1, 0, 0)
        return [Comparator(">=", low), Comparator("<", high)]
    # Explicit comparators.
    if tok[:2] in (">=", "<="):
        bound = _full_version_after(tok[:2], tok[2:], what)
        return [Comparator(tok[:2], bound)]
    if tok[0] in "<>":
        bound = _full_version_after(tok[0], tok[1:], what)
        return [Comparator(tok[0], bound)]
    if tok[0] == "=":
        bound = _full_version_after("=", tok[1:], what)
        return [Comparator("=", bound)]
    # Bare: exact / partial / wildcard forms.
    maj, mino, pat, given, wildcard = _parse_partial(tok, what)
    if wildcard:
        # A wildcard inside a bare version: 1.x / 1.2.* — the given
        # prefix is the range floor.
        low = SemVer(maj, mino, pat)
        if given == 1:
            return [Comparator(">=", low), Comparator("<", SemVer(maj + 1, 0, 0))]
        return [Comparator(">=", low), Comparator("<", SemVer(maj, mino + 1, 0))]
    if given == 3:
        return [Comparator("=", SemVer(maj, mino, pat))]
    if given == 2:
        return [Comparator(">=", SemVer(maj, mino, 0)),
                Comparator("<", SemVer(maj, mino + 1, 0))]
    return [Comparator(">=", SemVer(maj, 0, 0)),
            Comparator("<", SemVer(maj + 1, 0, 0))]


def _pre_id_ok(p: str) -> bool:
    return bool(_re_mod.fullmatch(_PRE_ID, p))


def requirement_holds(req: Requirement, v: SemVer) -> bool:
    """Does `v` satisfy `req`? Spec precedence plus the opt-in rule:
    a pre-release version only matches when some comparator's bound
    itself carries a pre-release on the SAME (major, minor, patch)
    tuple — ^1.2.3 admits 1.2.9 but never 1.3.0-beta, =1.2.3 never
    admits 1.2.3-beta (the bound must carry the tag), and * admits
    nothing pre-release at all."""
    for c in req.comparators:
        if not c.holds(v):
            return False
    if v.prerelease:
        for c in req.comparators:
            if c.bound.prerelease and \
                    (c.bound.major, c.bound.minor, c.bound.patch) == \
                    (v.major, v.minor, v.patch):
                break
        else:
            return False
    if req.any_nonpre and not req.comparators and v.prerelease:
        return False
    return True


# ---------------------------------------------------------------------------
# Enforcement helpers (called by the commands).
# ---------------------------------------------------------------------------

def require_package_version(manifest: Dict, what: str,
                            required: bool = False) -> Optional[SemVer]:
    """Validate [package].version when present (refuse a bad one);
    when `required`, ALSO refuse its absence — publish has no silent
    fallback. Returns the parsed version or None (absent, not
    required)."""
    pkg = manifest.get("package", {})
    if not isinstance(pkg, dict):
        raise ValueError("%s: [package] section is corrupt (not a table)" % what)
    if "version" not in pkg or pkg.get("version") is None:
        if required:
            raise ValueError(
                "%s: no [package].version — a publish must name its "
                "version (%s)" % (what, GRAMMAR))
        return None
    return parse_version(pkg["version"], "%s: package version" % what)


def require_dependency_requirements(deps: Dict) -> Dict[str, Requirement]:
    """Validate every dependency's `version` requirement (grammar);
    returns {name: Requirement} for the deps that carry one. Called
    by lock/build/verify — the grammar is checked BEFORE anything is
    resolved or compiled."""
    reqs: Dict[str, Requirement] = {}
    if not isinstance(deps, dict):
        return reqs
    for name, source in deps.items():
        if not isinstance(source, dict):
            continue
        if "version" not in source or source.get("version") is None:
            continue
        reqs[name] = parse_requirement(
            source["version"], "dependency %s version requirement" % name)
    return reqs


def enforce_dep_requirement(name: str, requirement: Requirement,
                            source: Dict, resolved_path: str) -> SemVer:
    """The lock-time law: the pinned ref must satisfy `requirement`.

    A `tag` source must BE a semver tag (one optional leading v); a
    moving ref (branch/main/path) must carry its own manifest whose
    version satisfies the requirement. A requirement that cannot be
    checked is a refusal — the caller turns the ValueError into the
    clean CLI error and a non-zero exit."""
    tag = source.get("tag") if isinstance(source, dict) else None
    if tag:
        v = tag_version(tag)
        if v is None:
            raise ValueError(
                "dependency %s: requirement %r cannot be checked against "
                "tag %r — the tag is not a semantic version (%s)"
                % (name, requirement.text, tag, GRAMMAR))
        if not requirement_holds(requirement, v):
            raise ValueError(
                "dependency %s: tag %s does not satisfy requirement "
                "%r (semver requires a compatible pin)" % (name, tag,
                                                           requirement.text))
        return v
    # Moving ref: the dependency's own manifest is the claim.
    mv = manifest_version_of(resolved_path)
    if mv is None:
        raise ValueError(
            "dependency %s: requirement %r cannot be checked — the "
            "resolved path has no hls-pkg.toml with [package].version "
            "(pin a semver tag, or give the dependency a version)"
            % (name, requirement.text))
    if not requirement_holds(requirement, mv):
        raise ValueError(
            "dependency %s: its manifest version %s does not satisfy "
            "requirement %r" % (name, version_string(mv), requirement.text))
    return mv


def manifest_version_of(resolved_path: str) -> Optional[SemVer]:
    """The self-declared version of a resolved dependency: the
    [package].version of the hls-pkg.toml beside it (a directory dep
    carries its manifest inside; a single-file dep's manifest lives
    in its directory). None when there is nothing to read — the
    caller decides whether that is a refusal."""
    d = resolved_path
    if _os.path.isfile(d):
        d = _os.path.dirname(d)
    mpath = _os.path.join(d, "hls-pkg.toml")
    if not _os.path.isfile(mpath):
        return None
    # Local import to avoid a cycle (hpkg_manifest imports nothing
    # from here, but the facade re-exports everything — keep the
    # dependency edge explicit and one-way).
    from hpkg_manifest import parse_manifest
    try:
        manifest = parse_manifest(mpath)
    except (ValueError, OSError):
        return None
    pkg = manifest.get("package", {})
    if not isinstance(pkg, dict) or "version" not in pkg:
        return None
    try:
        return parse_version(pkg["version"], "dependency manifest version")
    except ValueError:
        return None


def check_publish_immutability(name: str, version: SemVer, content_sha: str,
                               log_path: str) -> Tuple[Optional[str],
                                                       Optional[int],
                                                       Optional[str]]:
    """The publish-time law: name@version is a contract with the
    content it was first published with.

    Returns (verdict, existing_seq, message):
      (None, None, None)              — nothing published under this
                                        identity; proceed with the
                                        append
      ("identical", seq, None)        — the same content is already
                                        in the ledger at `seq`; the
                                        caller no-ops (and still
                                        records the op, quoting that
                                        seq — a no-op publish is a
                                        run privileged op, and the
                                        audit trail records runs)
      ("refuse", seq, message)        — the version exists with
                                        DIFFERENT content; the
                                        message is the refusal

    Build metadata does not change the identity (§10) — 1.2.3+a and
    1.2.3+b are one version and one content."""
    core = (version.major, version.minor, version.patch, version.prerelease)
    import json as _json
    try:
        with open(log_path, "rb") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return None, None, None
    for ln in lines:
        ln = ln.strip()
        if not ln or b'"kind": "publish"' not in ln:
            continue
        try:
            rec = _json.loads(ln.decode("utf-8"))
        except ValueError:
            continue
        if rec.get("name") != name or rec.get("kind") != "publish":
            continue
        try:
            rv = parse_version(rec.get("version", ""),
                               "transparency log version")
        except ValueError:
            continue
        if (rv.major, rv.minor, rv.patch, rv.prerelease) != core:
            continue
        seq = rec.get("seq") if isinstance(rec.get("seq"), int) else None
        if rec.get("sha256") == content_sha:
            return "identical", seq, None
        return ("refuse", seq,
                "refusing to publish %s@%s: this version is already "
                "published with different content (sha256 %s, this build "
                "%s) — semver requires a version bump, not a rewrite"
                % (name, version_string(version),
                   str(rec.get("sha256"))[:12], content_sha[:12]))
    return None, None, None


__all__ = [
    "GRAMMAR",
    "Comparator",
    "Requirement",
    "SemVer",
    "bump_version",
    "check_publish_immutability",
    "compare_versions",
    "enforce_dep_requirement",
    "manifest_version_of",
    "parse_requirement",
    "parse_version",
    "require_dependency_requirements",
    "require_package_version",
    "requirement_holds",
    "requirement_string",
    "tag_version",
    "try_parse_version",
    "version_string",
]
