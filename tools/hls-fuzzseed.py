#!/usr/bin/env python3
"""hls-fuzzseed — fuzz corpus seeding from real-world packages (Stage 134).

The Stage 18 fuzzer generates its programs from a hand-written grammar:
small `main()`-only toys over ints, strings and control flow. Real code
is nothing like that — it has structs, enums, matches, method calls,
multi-parameter functions with real shapes — and none of it ever
reached the differential oracle, because the generator cannot emit it.
Stage 134 builds the bridge: `hls-fuzzseed` harvests REAL functions out
of the real-world corpus — the packages the package manager has
downloaded (the hls-pkg cache and deps trees) plus the project's own
in-tree code — and turns each one into a type-correct, runnable,
deterministic HARNESS the fuzzer can seed from.

The three laws
--------------

1. The harvest. Sources are real code only: every `.hls` file under the
   package roots (`--pkg-cache`, default the two trees hls-pkg itself
   maintains), plus — unless `--no-in-tree` — the project's own std/,
   core/, examples/, tests/ok/ and benchmarks/. Every file goes through
   the REAL parser (boot.parser.parse); a file that does not parse is a
   named skip, never a crash. What is harvested is FUNCTIONS, not
   files: each non-extern, non-method, non-main function is an entry
   candidate.

2. The harness. An entry function alone is not a program — it has
   parameters with no values and callees with no bodies. The seeder
   closes both gaps: the CALL closure (every non-extern function and
   method the body transitively calls) and the TYPE closure (every
   struct and enum the signatures and bodies mention) are pulled in as
   raw source slices, and a `main()` is synthesized that declares every
   parameter with a type-correct corner value — ints from the corner
   pool (0, 1, 2, 3, 5, 10, -1, -2), strings from the literal pool,
   lists from the shape pool — then prints a deterministic PROJECTION
   of the result (int/float/bool via to_str, str directly, lists
   elementwise, struct fields when every projected field is
   primitive, anything else as a named constant marker). The REAL
   checker judges the result: a harness that does not type-check is a
   named skip, and the reason is the checker's own words. The harness
   shape is fixed — parameter lets, the call, the projection, `return
   0` — so the mutator (and the fuzzer) always knows where the mutable
   inputs live. Generic entries instantiate T to int at the CALL (the
   definition text is never rewritten); a Stage 98 refinement on a
   parameter is satisfied by trying corner vectors until the checker
   accepts, capped, then refusing by name.

3. The gate. A seed enters the corpus only when it is worth fuzzing
   and safe to compare:
     - purity  — the harness text passes the same deny table Stage
       133's differential suite uses: no crate modes, no world-effect
       imports (time, env, fs, net, thread, process, ...), no
       nondeterministic builtins (rand_int, clock_ms, read_file, ...),
       and no closure that DECLARES a world effect. Output must be a
       function of the source, or the differential oracle is
       meaningless.
     - closure honesty — a body that calls an extern fn, an `asm!`
       block, or a module-qualified function (`json.parse(...)`) is
       excluded BY NAME: externs are FFI, asm is not interpreter-
       runnable, and cross-module calls would drag whole std modules
       into a single-file harness (the honest Stage 134 boundary).
     - smoke    — the seed is actually run on the interpreter once,
       with a timeout. ok and panic are both fine (a panic is a
       legitimate differential observable, matched class-for-class by
       the native side); timeout is excluded by default — a corpus
       seeded with hangs is a corpus of wasted fuzz time. The
       exclusion is named; `--keep-timeouts` lifts it.
     - identity — the digest is SHA-256 over the NORMALIZED source
       (comments and whitespace stripped): the same function reached
       through two paths lands once, with both provenances recorded.
     - budget   — a harness over `--max-lines` (default 120) is a
       refusal naming the budget, not a crash.

Ranking and mutation
--------------------

Every seed is scored by an explicit FEATURE CENSUS (structs, enums
driven through match, generics, recursion, method calls, qmark
propagation, list and string operations — the weights are a static
table in this file, chosen so a struct-and-match seed outranks an
int-only toy) and the corpus is emitted in rank order under `--limit`.
The census inputs are recorded per seed in the manifest, so a rank is
always auditable.

The mutator turns one seed into `--mutate` (default 1) typed MUTANTS
through eight ops — int corner swap, operator swap, string mutation,
bool flip, list shape change, line drop, line duplicate, and the
harness-aware parameter corner re-pick — every one of which must
re-parse and re-check through the real pipeline before it enters
the corpus. A mutant that fails is counted against its op in the
manifest and dropped: the checker is the bouncer, the mutator is not
required to be smart. Mutant lineage (parent digest, op name) is
recorded; a mutant is a child, and the manifest says whose.

Output
------

    <out>/seed-<fn>-<digest8>.hls        one file per seed
    <out>/m/seed-...-<k>.hls             one file per surviving mutant
    <out>/manifest.json                  provenance, census, rank,
                                         smoke class, mutant lineage,
                                         the full skip census

Determinism: the walk is sorted, the RNG is seeded (default 134),
name hashing is crc32 (never Python's randomized hash), and no
wall-clock enters the output unless `--stamp` says so. The same tree
always seeds the same corpus — a corpus you cannot rebuild is a corpus
you cannot audit.

Integration: `hls-fuzz --seed-corpus <out>` runs the Stage 18
differential oracle (interpreter vs native) over corpus programs —
mutation included — instead of only grammar-generated toys. That is
the point of the whole stage: real-world code, both backends,
byte-for-byte.

Exit codes: 0 green (corpus non-empty, every law held) · 1 not green
(empty corpus) · 2 usage.

Usage:
  python3 tools/hls-fuzzseed.py                    # default harvest
  python3 tools/hls-fuzzseed.py --no-in-tree       # packages only
  python3 tools/hls-fuzzseed.py --pkg-cache DIR    # extra package root
  python3 tools/hls-fuzzseed.py --limit 50 --mutate 2
  python3 tools/hls-fuzzseed.py --json             # machine report
"""
import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from concurrent.futures import ThreadPoolExecutor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from tools.hlinterp.lexer import HLError                       # noqa: E402
from tools.hlinterp.parser import parse                        # noqa: E402
from tools.hlinterp.checker import check                       # noqa: E402
from tools.hlinterp.checking.helpers import BUILTIN_FNS        # noqa: E402
from tools.hlinterp.boot import load_program                   # noqa: E402

TOOL_VERSION = "hls-fuzzseed 1 (Stage 134, v0.152.0-alpha)"

# ---------------------------------------------------------------------------
# The in-tree real-world corpus
# ---------------------------------------------------------------------------
# hls-pkg's own trees are the primary source (that is what the stage
# name says); they are usually absent on a fresh checkout, so the
# project's own real code — every line of it written for humans, not
# for the fuzzer — is the default companion source.

IN_TREE_DIRS = ["std", "core", "examples", "tests/ok", "benchmarks"]

PKG_TREE_DIRS = [".hls-pkg-cache", ".hls-pkg-deps"]

# ---------------------------------------------------------------------------
# The purity gate — Stage 133's deny table, verbatim in spirit
# ---------------------------------------------------------------------------
# A seed's output must be a function of its source. The same three
# checks xdiff applies to a whole program apply here to the harness
# text: crate modes, world-effect imports, nondeterministic builtins.
# The tables are intentionally the SAME LAW as tools/hls-xdiff.py —
# one purity standard across the differential tooling, not two.

_CRATE_ATTR_RE = re.compile(r"^\s*#!\[\s*([A-Za-z_]+)", re.MULTILINE)

_DENY_IMPORTS = {
    "time", "env", "process", "net", "fs", "thread", "sync",
    "http", "http2", "websocket", "sse", "session", "cookie", "csrf",
    "archive", "config", "cli", "complete", "man", "hlscli", "hlsdoc",
    "tui", "color", "progress", "log", "template", "graphql", "openapi",
    "jsffi", "dom", "taint", "sanitize", "uuid", "simd", "repl", "bench",
}
_IMPORT_RE = re.compile(r'^\s*import\s+"(?:std|core)\.([\w]+)', re.MULTILINE)

_DENY_BUILTIN_RE = re.compile(
    r"\b(rand_seed|rand_int|clock_ms|clock_ns|monotonic_ns|sleep|"
    r"proc_exec|proc_spawn|proc_kill|proc_wait|spawn|"
    r"read_file|write_file|append_file|read_dir|remove_file|"
    r"net_lookup|net_tcp_|net_udp_|hostname|"
    r"env_get|env_set|args_get|read_line|chan_new|chan_send|chan_recv|"
    r"mutex_|taint_unwrap)\b"
)

# The seed-specific rows of the same gate: machine- and build-dependent
# PROBES the differential oracle cannot compare (a terminal check, a
# cpu-feature probe, a scheduler identity, a second clock family the
# Stage 133 table predates). The core table above is xdiff's, shared;
# these rows are the fuzzer's honest additions, each named here.
_SEED_DENY_BUILTIN_RE = re.compile(
    r"\b(isatty|cwd_get|cwd_set|has_feature|simd_cpu_supports|"
    r"thread_current_id|instant_now_ns|system_time_now_ms|proc_pid)\b"
)

# ---------------------------------------------------------------------------
# Corner pools — the values the param initializer and the mutator draw from
# ---------------------------------------------------------------------------
# Small for seeds (a 2^63-deep recursion is a hang, not a finding);
# the BIG corners (INT64_MIN/MAX, powers of two) are the MUTATOR's
# pool, applied one mutant at a time where a timeout is one dropped
# mutant, not a dead corpus.

SEED_INT_CORNERS = [0, 1, 2, 3, 5, 10, -1, -2]
MUT_INT_CORNERS = [0, 1, -1, 2, 10, 100, 9223372036854775807,
                   -9223372036854775808, 65536, 1000000007]
SEED_STR_CORNERS = ["", "a", "hello", "hello world", "h\u00e9llo\u00a5"]
MUT_STR_CORNERS = ["", "a", "xyz", "h\u00e9llo\u00a5", "hello world",
                   "aaaaaaaaaa"]

PRIMITIVES = {"int", "float", "bool", "str", "void"}
FAMILY_KEYWORDS = {"list", "map", "set", "chan", "T"}

# ---------------------------------------------------------------------------
# The feature census — static weights, auditable ranks
# ---------------------------------------------------------------------------
# A seed's rank is the weighted count of the language features its
# normalized source exercises. The table is a RANKING heuristic, not a
# soundness gate: false positives only reshuffle priority, never
# correctness. Weights are chosen so structural features (a struct
# literal, an enum driven through match) outrank scalar noise. The per-
# feature hit counts are recorded in the manifest next to the score.

CENSUS_FEATURES = [
    ("struct_lit", re.compile(r"\b[A-Z][A-Za-z0-9_]*\s*\{"), 3),
    ("enum_match", re.compile(r"\bmatch\b"), 3),
    ("generic_fn", re.compile(r"\bfn\s+\w+\s*\["), 3),
    ("method_call", re.compile(r"\.\w+\s*\("), 2),
    ("list_op", re.compile(r"\.(push|pop|get|set|insert|remove)\s*\("), 1),
    ("str_op", re.compile(r"\.(len|sub|split|trim|contains|upper|lower)"
                          r"\s*\("), 1),
    ("while_loop", re.compile(r"\bwhile\b"), 1),
    ("for_loop", re.compile(r"\bfor\b"), 1),
    ("if_chain", re.compile(r"\bif\b"), 1),
    ("float_lit", re.compile(r"\b\d+\.\d+"), 1),
    ("qmark", re.compile(r"\?(\s|$|[.\[])"), 2),
    ("cast_op", re.compile(r"\b(to_int|to_str|to_float)\s*\("), 1),
]
CENSUS_RECURSION_WEIGHT = 2


# ---------------------------------------------------------------------------
# Normalization + identity
# ---------------------------------------------------------------------------

def normalize_source(src):
    """Strip line comments and collapse whitespace runs.

    The identity law: two functions that differ only in comments and
    layout are the SAME seed. String literals are preserved verbatim —
    a comment stripper that ate '#' inside a string would conflate
    distinct programs; the scanner below tracks quotes.
    """
    out = []
    i, n = 0, len(src)
    in_str = False
    while i < n:
        c = src[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "#":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c.isspace():
            out.append(" ")
            while i < n and src[i].isspace():
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def digest_of(src):
    return hashlib.sha256(normalize_source(src).encode("utf-8")).hexdigest()


def stable_name_hash(name):
    """crc32 — NEVER Python's hash(), which is randomized per process
    and would break the corpus determinism law."""
    return zlib.crc32(name.encode("utf-8")) & 0xffffffff


# ---------------------------------------------------------------------------
# Raw-source slicing — brace matching with string/comment awareness
# ---------------------------------------------------------------------------
# The seeder does not pretty-print the AST; it slices the ORIGINAL
# text. Every definition enters the harness verbatim (minus the
# attribute lines above it — a seed is a semantic skeleton, not a byte
# copy). The scanner finds the matching closing brace for an opening
# one, honouring strings, escapes and # comments.

def find_def_end(src_lines, start_line):
    """1-based line of the '}' closing the definition whose first '{'
    opens at start_line. Returns None when unbalanced."""
    depth = 0
    opened = False
    for idx in range(start_line - 1, len(src_lines)):
        line = src_lines[idx]
        i, n = 0, len(line)
        in_str = False
        while i < n:
            c = line[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == '"':
                    in_str = False
                i += 1
                continue
            if c == '"':
                in_str = True
                i += 1
                continue
            if c == "#":
                break  # comment to end of line
            if c == "{":
                depth += 1
                opened = True
            elif c == "}":
                depth -= 1
                if opened and depth == 0:
                    return idx + 1
            i += 1
    return None


def slice_def(src_lines, start_line):
    """The verbatim source lines of a definition starting at start_line
    (1-based), or None when the braces do not balance."""
    end = find_def_end(src_lines, start_line)
    if end is None:
        return None
    return "\n".join(src_lines[start_line - 1:end])


# ---------------------------------------------------------------------------
# AST walkers — what a body calls, mentions, and constructs
# ---------------------------------------------------------------------------

def walk_exprs(e, out):
    if not isinstance(e, dict):
        return
    out.append(e)
    for v in e.values():
        if isinstance(v, dict):
            walk_exprs(v, out)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    walk_exprs(x, out)
                elif isinstance(x, (tuple, list)):
                    for t in x:
                        if isinstance(t, dict):
                            walk_exprs(t, out)


STMT_KINDS = {"let", "return", "if", "while", "for", "assign", "expr",
              "break", "continue", "asm"}


def walk_stmts(stmts, out):
    for s in stmts:
        if not isinstance(s, dict):
            continue
        out.append(s)
        for v in s.values():
            if isinstance(v, dict) and v.get("k") in STMT_KINDS:
                walk_stmts([v], out)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, dict) and x.get("k") in STMT_KINDS:
                        walk_stmts([x], out)


def type_names_in(ty):
    """Identifiers inside a type string, minus primitives and the
    parameterized family keywords (their ELEMENT types still matter and
    are extracted individually)."""
    names = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", ty or ""):
        w = m.group(0)
        if w not in PRIMITIVES and w not in FAMILY_KEYWORDS:
            names.add(w)
    return names


def fn_signature_types(f):
    names = set()
    for _p, pt, _m in f["params"]:
        names |= type_names_in(pt)
    names |= type_names_in(f["ret"])
    return names


class BodyFacts:
    """Everything closure-building needs to know about one fn body."""

    def __init__(self):
        self.calls = set()          # plain fn calls
        self.method_calls = set()   # receiver.method names
        self.module_calls = set()   # module.fn — the named boundary
        self.struct_lits = set()
        self.type_mentions = set()  # names in annotations + literals
        self.has_asm = False
        self.effects = set()


def body_facts(f, known_types):
    facts = BodyFacts()
    facts.effects = set(f["effects"])
    stmts = []
    walk_stmts(f["body"], stmts)

    # the fn's LOCAL names — params, let bindings, for-loop variables,
    # assignment roots, match payload bindings. A fieldcall whose
    # receiver is one of these is a METHOD call on a value; a
    # fieldcall whose receiver is neither a local nor a type name is
    # what a module-qualified call looks like.
    locals_ = {p for p, _t, _m in f["params"]}
    for s in stmts:
        k = s.get("k")
        if k == "let":
            locals_.add(s.get("name"))
        elif k == "for":
            locals_.add(s.get("var"))
        elif k == "assign":
            tgt = s.get("target", {})
            if tgt.get("k") == "ident":
                locals_.add(tgt.get("name"))

    exprs = []
    for s in stmts:
        walk_exprs(s, exprs)
    for e in exprs:
        if e.get("k") == "match":
            for arm in e.get("arms", []):
                pat = arm.get("pattern", {})
                if isinstance(pat, dict) and pat.get("k") == "variant":
                    en = pat.get("enum")
                    if en:
                        facts.type_mentions.add(en)
                    for b in pat.get("bindings", []):
                        if b != "_":
                            locals_.add(b)

    for s in stmts:
        if s.get("k") == "asm":
            facts.has_asm = True
        if s.get("k") == "let":
            facts.type_mentions |= type_names_in(s.get("t", ""))
        if s.get("k") == "for":
            facts.type_mentions |= type_names_in(s.get("vtype", ""))
    for e in exprs:
        k = e.get("k")
        if k == "call":
            facts.calls.add(e["name"])
        elif k == "fieldcall":
            tgt = e.get("target", {})
            if tgt.get("k") == "ident":
                root = tgt.get("name")
                if root in known_types:
                    # enum variant constructor: Option.none()
                    facts.type_mentions.add(root)
                elif root in locals_:
                    # receiver call: xs.get(i), v.scale(k)
                    facts.method_calls.add(e.get("name"))
                else:
                    facts.module_calls.add(
                        "%s.%s" % (root, e.get("name")))
            else:
                facts.method_calls.add(e.get("name"))
        elif k == "structlit":
            facts.struct_lits.add(e["name"])
            facts.type_mentions.add(e["name"])
    return facts


# ---------------------------------------------------------------------------
# The purity gate (harness text level)
# ---------------------------------------------------------------------------

def purity_verdict(source):
    """(eligible, reason) — Stage 133's law over the harness text."""
    m = _CRATE_ATTR_RE.search(source)
    if m:
        return False, "crate mode #![%s] — not a hosted program" % m.group(1)
    for m in _IMPORT_RE.finditer(source):
        mod = m.group(1)
        if mod in _DENY_IMPORTS:
            return False, ("import std.%s — output depends on the world, "
                           "not the implementation" % mod)
    m = _DENY_BUILTIN_RE.search(source)
    if m:
        return False, ("builtin '%s' — nondeterministic or "
                       "environment-bound" % m.group(1))
    m = _SEED_DENY_BUILTIN_RE.search(source)
    if m:
        return False, ("builtin '%s' — machine- or build-dependent; the "
                       "differential oracle cannot compare it"
                       % m.group(1))
    return True, None


# ---------------------------------------------------------------------------
# The closure builder
# ---------------------------------------------------------------------------

MAX_FIXPOINT = 32


class Refusal(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def build_closure(prog, src_lines, entry_name, max_lines):
    """Assemble the raw-source closure for one entry fn.

    Returns (closure_text, facts_of_entry) or raises Refusal.

    Laws, each named at the point of refusal:
      - unknown callee          → the checker would reject; refuse
      - extern callee           → FFI, not differentiable standalone
      - asm! in the closure     → the interpreter cannot run it
      - module-qualified call   → the cross-module boundary
      - non-IO declared effect  → impure by declaration
      - unbalanced braces       → cannot slice
      - over the line budget    → a harness, not a reimplementation
    """
    fns = prog["fns"]
    structs = prog["structs"]
    enums = prog["enums"]
    known_types = set(structs) | set(enums)

    facts_by_fn = {}

    def facts_of(key):
        if key not in facts_by_fn:
            f = fns[key]
            facts_by_fn[key] = body_facts(f, known_types)
        return facts_by_fn[key]

    # the language's own builtins resolve everywhere — a plain call to
    # println/clone/panic/math_sqrt is NOT a closure hole (the impure
    # ones die at the purity gate, the effect-declaration law, or both)
    builtin_calls = BUILTIN_FNS

    def add_callable(key):
        """Everything a plain fn OR a method key contributes."""
        f = fns[key]
        if f["extern"]:
            raise Refusal("reaches extern fn '%s' — FFI is not "
                          "differentiable standalone" % key)
        facts = facts_of(key)
        for callee in sorted(facts.calls):
            if callee in included or callee in method_included:
                continue
            if callee in builtin_calls:
                continue
            if callee not in fns:
                # most non-builtin unknowns resolve through the file's
                # own imports (std.result gives result_unwrap, ...) —
                # name the module when the prefix agrees, so the skip
                # census reads like a map of the ecosystem, not a wall
                # of 'unknown'
                mod = None
                for imp in prog.get("imports", []):
                    base = str(imp.get("path", "")).rsplit(".", 1)[-1]
                    if base and callee.startswith(base + "_"):
                        mod = imp.get("path")
                        break
                if mod:
                    raise Refusal(
                        "calls '%s' from import '%s' — cross-module "
                        "seeding is the honest Stage 134 boundary"
                        % (callee, mod))
                raise Refusal("calls unknown function '%s'" % callee)
            if fns[callee]["extern"]:
                raise Refusal("reaches extern fn '%s' — FFI is not "
                              "differentiable standalone" % callee)
            if fns[callee]["struct"]:
                # a direct plain-name call can only reach non-method
                # fns; a struct-keyed name here means the call site is
                # a method the receiver resolution owns — pull it in.
                method_included.add(callee)
            else:
                included.add(callee)

    entry = fns[entry_name]
    if entry["extern"]:
        raise Refusal("entry is an extern declaration")
    included = {entry_name}
    method_included = set()
    facts_of(entry_name)
    add_callable(entry_name)

    type_closure = set()

    for _round in range(MAX_FIXPOINT):
        changed = False
        # extend the fn closure through every included body
        for key in sorted(list(included) + list(method_included)):
            add_callable(key)
        # recompute the type closure over everything included — AND
        # over the shapes of the types already closed: a struct field
        # typed Rgb, or an enum variant payload typed Rgb, drags Rgb
        # in (its definition is sliced, or the harness cannot spell
        # the type it constructs)
        new_types = set()
        for key in list(included) + list(method_included):
            f = fns[key]
            facts = facts_of(key)
            new_types |= fn_signature_types(f)
            new_types |= facts.type_mentions
            new_types |= facts.struct_lits
        for tname in sorted(type_closure):
            st = structs.get(tname)
            if st:
                for _fname, ftype, _d in st["fields"]:
                    new_types |= type_names_in(ftype)
                continue
            en = enums.get(tname)
            if en:
                for _vname, payloads in en["variants"]:
                    for pt in payloads:
                        new_types |= type_names_in(pt)
        new_types &= known_types
        if new_types != type_closure:
            type_closure = new_types
            changed = True
        # pull in the methods some receiver call demands
        wanted = set()
        for key in list(included) + list(method_included):
            wanted |= facts_of(key).method_calls
        for key in sorted(fns):
            f = fns[key]
            if f["extern"] or not f["struct"]:
                continue
            if key in method_included:
                continue
            if f["struct"] in type_closure and f["name"] in wanted:
                method_included.add(key)
                changed = True
        if not changed:
            break

    # --- the named boundary laws ------------------------------------------
    for key in sorted(included | method_included):
        facts = facts_of(key)
        f = fns[key]
        bad = sorted(facts.effects - {"IO"})
        if bad:
            raise Refusal("closure fn '%s' declares effect '%s' — "
                          "world-bound by declaration" % (key, bad[0]))
        if facts.has_asm:
            raise Refusal("closure contains an asm! body ('%s') — the "
                          "interpreter cannot execute it" % key)
        if facts.module_calls:
            mc = sorted(facts.module_calls)[0]
            raise Refusal("'%s' calls module function '%s' — cross-module "
                          "seeding is the honest Stage 134 boundary"
                          % (key, mc))

    # --- the import-only type law ------------------------------------------
    # A type that exists only in an imported module cannot be sliced
    # into a self-contained harness — name the refusal instead of
    # shipping a harness that cannot type-check. Bare single-capital
    # names are exempt (a generic's own parameters, instantiated at
    # the call; the checker is still the final judge for any custom
    # struct that happens to share the spelling).
    for key in sorted(included | method_included):
        f = fns[key]
        facts = facts_of(key)
        mentions = fn_signature_types(f) | facts.type_mentions
        for t in sorted(mentions):
            if len(t) == 1 and t.isupper():
                continue
            if t not in known_types:
                raise Refusal("type '%s' in '%s' resolves only through "
                              "an import — self-contained harness "
                              "impossible" % (t, key))

    # --- slicing ------------------------------------------------------------
    pieces = []
    # structs and enums first (definitions before use keeps the harness
    # readable; the checker is order-independent but readers are not)
    for tname in sorted(type_closure):
        node = structs.get(tname) or enums.get(tname)
        line = node.get("line")
        text = slice_def(src_lines, line) if line else None
        if text is None:
            raise Refusal("type '%s' cannot be sliced (unbalanced "
                          "braces)" % tname)
        pieces.append(text)
    for key in sorted(included):
        text = slice_def(src_lines, fns[key]["line"])
        if text is None:
            raise Refusal("fn '%s' cannot be sliced (unbalanced "
                          "braces)" % key)
        pieces.append(text)
    for key in sorted(method_included):
        text = slice_def(src_lines, fns[key]["line"])
        if text is None:
            raise Refusal("method '%s' cannot be sliced" % key)
        # a method sliced bare would be a PLAIN fn — the impl block is
        # what makes the checker resolve the receiver call — so every
        # method rides inside a synthesized one-method impl block
        pieces.append("impl %s {\n%s\n}" % (fns[key]["struct"], text))

    body = "\n\n".join(pieces)
    n_lines = body.count("\n") + 1
    if n_lines > max_lines:
        raise Refusal("closure is %d lines — over the %d-line harness "
                      "budget" % (n_lines, max_lines))
    return body, facts_of(entry_name)


# ---------------------------------------------------------------------------
# Type instantiation + corner synthesis + projection
# ---------------------------------------------------------------------------

def inst_type(ty):
    """Instantiate the harness-side spelling of a generic type: the
    call drives inference with ints, so a bare T becomes int in every
    `let` annotation. Only bare T is rewritten — Tree stays Tree."""
    return re.sub(r"\bT\b", "int", ty or "")


def synth_value(ty, rng, depth=0):
    """(hls_expr, ok) — a type-correct literal for a parameter type.

    Generic parameters (T) instantiate to int. Custom struct and enum
    parameters have no synthesizable literal (value construction needs
    field-by-field knowledge the signature does not carry): not-ok,
    and the caller names the refusal.
    """
    ty = (ty or "").strip()
    base = ty.split("[")[0].strip()
    if re.fullmatch(r"[A-Z]", base) and base == "T":
        return str(rng.choice([c for c in SEED_INT_CORNERS if c >= 0])), True
    if base in ("A", "B", "C") or (len(base) == 1 and base.isupper()):
        return None, False
    if ty == "int":
        return str(rng.choice(SEED_INT_CORNERS)), True
    if ty == "float":
        return rng.choice(["0.0", "1.5", "-2.25", "3.14159"]), True
    if ty == "bool":
        return rng.choice(["true", "false"]), True
    if ty == "str":
        return '"%s"' % rng.choice(SEED_STR_CORNERS), True
    if ty.startswith("list["):
        elem = ty[len("list["):-1]
        v, ok = synth_value(elem, rng, depth + 1)
        if not ok or depth >= 1:
            # an empty list is type-correct for ANY element type —
            # the honest corner when the element cannot be built
            return "[]", True
        n = rng.choice([0, 1, 2, 3])
        return "[%s]" % ", ".join([v] * n), True
    return None, False


def projection_lines(var, ty, structs):
    """HLS statements (unindented) that print a deterministic
    projection of `var: ty`.

    int/float/bool via to_str, str directly, list[T] elementwise over
    the primitive element families, struct returns field-by-field when
    every projected field is primitive, anything else a named constant
    marker. Determinism is the law; the exact bytes are the
    differential oracle's business.
    """
    ty = (ty or "").strip()
    if ty in ("int", "float", "bool"):
        return ['print(%s.to_str())' % var]
    if ty == "str":
        return ['print(%s)' % var]
    if ty.startswith("list["):
        elem = ty[len("list["):-1]
        if elem in ("int", "float", "bool", "str"):
            cast = "" if elem == "str" else ".to_str()"
            return [
                'print("[")',
                'let mut _si: int = 0',
                'while _si < %s.len() {' % var,
                'if _si > 0 { print(",") }',
                'print(%s.get(_si)%s)' % (var, cast),
                '_si = _si + 1',
                '}',
                'print("]")',
            ]
        return ['print("<%s>")' % ty]
    base = ty.split("[")[0].strip()
    st = structs.get(base)
    if st and not st["typeparams"]:
        # field-by-field: a label line, then the value line — no string
        # concat gymnastics, every field primitive-checked
        lines = ['print("<%s")' % base]
        for fname, ftype, _d in st["fields"][:4]:
            if ftype in ("int", "float", "bool"):
                lines.append('print(" %s=")' % fname)
                lines.append('print(%s.%s.to_str())' % (var, fname))
            elif ftype == "str":
                lines.append('print(" %s=")' % fname)
                lines.append('print(%s.%s)' % (var, fname))
        lines.append('print(">")')
        return lines
    return ['print("<%s>")' % (ty or "void")]


def build_harness(entry_name, entry, closure_body, rng, source_rel, prog):
    """(harness_text, param_summary) or (None, reason).

    Shape (fixed, so the mutator and the fuzzer know where the inputs
    live): seed header, closure definitions, main with one corner-valued
    let per parameter, the call, the projection, `return 0`.
    """
    structs = prog["structs"]
    params = entry["params"]
    lets = []
    call_args = []
    for pname, ptype, _pmut in params:
        v, ok = synth_value(ptype, rng)
        if not ok:
            return None, ("parameter '%s' has type '%s' — no "
                          "synthesizable literal" % (pname, ptype))
        lets.append("    let %s: %s = %s" % (pname, inst_type(ptype), v))
        call_args.append(pname)
    ret = entry["ret"]
    body_stmts = list(lets)
    call = "%s(%s)" % (entry_name, ", ".join(call_args)) \
        if call_args else "%s()" % entry_name
    if ret == "void":
        body_stmts.append("    %s" % call)
        body_stmts.append('    println("")')
    else:
        iret = inst_type(ret)
        body_stmts.append("    let _r: %s = %s" % (iret, call))
        for ln in projection_lines("_r", iret, structs):
            body_stmts.append("    " + ln)
        body_stmts.append('    println("")')
    main_body = "\n".join(body_stmts)

    header = ("# seed: %s @ %s:%d (hls-fuzzseed, Stage 134)\n"
              "# provenance: %s\n"
              "# harness: corner-valued params, the call, the projection\n"
              % (entry_name, source_rel, entry["line"], source_rel))
    text = ("%s%s\n\n"
            "fn main() -> int uses IO {\n%s\n    return 0\n}\n"
            % (header, closure_body, main_body))
    return text, [("%s: %s" % (p, inst_type(t))) for p, t, _m in params]


# ---------------------------------------------------------------------------
# Smoke runner
# ---------------------------------------------------------------------------

def smoke_run(path, timeout_s):
    """'ok' | 'panic:<first line>' | 'timeout' | 'error:<first line>'."""
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools/hlinterp/boot.py"), path],
            capture_output=True, timeout=timeout_s, input=b"",
            cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return "timeout"
    if p.returncode == 0:
        return "ok"
    first = (p.stderr or p.stdout).decode("utf-8", "replace").strip()
    first = first.split("\n")[0][:120] if first else "rc=%d" % p.returncode
    # A panic is a legitimate observable; a checker rejection here is
    # not (the harness was checked in-process — a check/smoke
    # disagreement is an internal bug worth surfacing, so it keeps its
    # message).
    if "panic" in first.lower():
        return "panic:%s" % first
    return "error:%s" % first


# ---------------------------------------------------------------------------
# The feature census
# ---------------------------------------------------------------------------

def census_breakdown(seed_src, recursive):
    """({feature: hits}, score) — the auditable rank input."""
    norm = normalize_source(seed_src)
    hits = {}
    score = 0
    for name, rx, weight in CENSUS_FEATURES:
        n = len(rx.findall(norm))
        if n:
            hits[name] = n
            score += weight * min(n, 8)
    if recursive:
        hits["recursion"] = 1
        score += CENSUS_RECURSION_WEIGHT
    return hits, score


# ---------------------------------------------------------------------------
# The mutator
# ---------------------------------------------------------------------------
# Lexical ops, applied OUTSIDE string literals and comments (the same
# scanner discipline as normalize_source). Every mutant is re-parsed
# and re-checked through the real pipeline before it enters the
# corpus; failures are counted per-op and dropped.

def _mask(src):
    """A copy of src with string contents and comments blanked —
    regex mutations never touch them. Positions are preserved."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            i += 1
            while i < n and src[i] != '"':
                out[i] = " "
                if src[i] == "\\" and i + 1 < n:
                    out[i + 1] = " "
                    i += 1
                i += 1
            i += 1  # the closing quote stays
            continue
        if c == "#":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


_INT_RE = re.compile(r"\b\d+\b")
_BOOL_RE = re.compile(r"\b(true|false)\b")
_BINOP_RE = re.compile(
    r"(?<=[\w\)\]])\s*(==|!=|<=|>=|&&|\|\||[-+*/%<>])\s*(?=[\w\(\"\[])")
_LINE_KEEP_RE = re.compile(r"\S")

OP_SWAP_PAIRS = [("==", "!="), ("!=", "=="), ("<", ">"), (">", "<"),
                 ("<=", ">="), (">=", "<="), ("&&", "||"), ("||", "&&"),
                 ("+", "-"), ("-", "+"), ("*", "/"), ("/", "*")]

MUTATION_OPS = ["int_corner", "op_swap", "str_mut", "bool_flip",
                "list_mut", "line_dup", "line_drop", "param_corner"]


def mutate_source(src, rng):
    """One random mutation op, applied. Returns (new_src, op_name) or
    (None, op_name) when no candidate existed for any op."""
    ops = list(MUTATION_OPS)
    rng.shuffle(ops)
    masked = _mask(src)
    lines = src.split("\n")

    def main_block():
        try:
            mi = max(i for i, l in enumerate(lines)
                     if l.startswith("fn main"))
        except ValueError:
            return None
        return mi

    for op in ops:
        if op == "int_corner":
            cands = list(_INT_RE.finditer(masked))
            if not cands:
                continue
            m = rng.choice(cands)
            corner = str(rng.choice(MUT_INT_CORNERS))
            return src[:m.start()] + corner + src[m.end():], op
        if op == "op_swap":
            cands = []
            for m in _BINOP_RE.finditer(masked):
                o = m.group(1).strip()
                for a, b in OP_SWAP_PAIRS:
                    if o == a:
                        cands.append((m.start(1), m.end(1), b))
            if not cands:
                continue
            s, e, b = rng.choice(cands)
            return src[:s] + b + src[e:], op
        if op == "str_mut":
            cands = []
            i, n = 0, len(src)
            while i < n:
                if src[i] == '"':
                    j = i + 1
                    while j < n and src[j] != '"':
                        if src[j] == "\\":
                            j += 1
                        j += 1
                    cands.append((i + 1, j))
                    i = j + 1
                else:
                    i += 1
            if not cands:
                continue
            s, e = rng.choice(cands)
            # the span EXCLUDES the surrounding quotes — insert the bare
            # content, or the mutant grows doubled quotes and dies at
            # the parser
            return src[:s] + rng.choice(MUT_STR_CORNERS) + src[e:], op
        if op == "bool_flip":
            cands = list(_BOOL_RE.finditer(masked))
            if not cands:
                continue
            m = rng.choice(cands)
            flip = "false" if m.group(1) == "true" else "true"
            return src[:m.start()] + flip + src[m.end():], op
        if op == "list_mut":
            cands = []
            i, n = 0, len(src)
            while i < n:
                c = src[i]
                if c == '"':
                    i += 1
                    while i < n and src[i] != '"':
                        if src[i] == "\\":
                            i += 1
                        i += 1
                    i += 1
                    continue
                if c == "#":
                    while i < n and src[i] != "\n":
                        i += 1
                    continue
                if c == "[":
                    depth = 0
                    j = i
                    while j < n:
                        cj = src[j]
                        if cj == '"':
                            j += 1
                            while j < n and src[j] != '"':
                                if src[j] == "\\":
                                    j += 1
                                j += 1
                            j += 1
                            continue
                        if cj == "[":
                            depth += 1
                        elif cj == "]":
                            depth -= 1
                            if depth == 0:
                                break
                        j += 1
                    inner = src[i + 1:j]
                    if j < n and "[" not in inner and "{" not in inner:
                        cands.append((i, j + 1, inner))
                    i = j + 1
                else:
                    i += 1
            if not cands:
                continue
            s, e, inner = rng.choice(cands)
            elems = [x.strip() for x in inner.split(",") if x.strip()]
            if rng.random() < 0.4 and elems:
                elems.pop(rng.randrange(len(elems)))
            else:
                elems.append(rng.choice(["0", "1", "-1", "10", '"a"',
                                         "true"]))
            return src[:s] + "[" + ", ".join(elems) + "]" + src[e:], op
        if op in ("line_dup", "line_drop"):
            mi = main_block()
            if mi is None:
                continue
            body = [i for i in range(mi + 1, len(lines))
                    if _LINE_KEEP_RE.match(lines[i])
                    and lines[i].strip() not in ("return 0", "}")]
            # single-line simple statements only: never a brace-bearing
            # line, never a parameter let (a dropped param-let is a
            # guaranteed refusal — the call below needs it)
            body = [i for i in body
                    if "{" not in lines[i] and "}" not in lines[i]
                    and not re.match(r"\s*let \w+", lines[i])]
            if not body:
                continue
            i = rng.choice(body)
            if op == "line_dup":
                lines.insert(i + 1, lines[i])
            else:
                del lines[i]
            return "\n".join(lines), op
        if op == "param_corner":
            mi = main_block()
            if mi is None:
                continue
            pcands = []
            for i in range(mi + 1, len(lines)):
                m = re.match(r"\s*let (\w+): (int|str|bool) = (.+)",
                             lines[i])
                if m:
                    pcands.append((i, m))
            if not pcands:
                continue
            i, m = rng.choice(pcands)
            cur = m.group(3).strip()
            if m.group(2) == "int":
                pool = [str(c) for c in MUT_INT_CORNERS if str(c) != cur]
            elif m.group(2) == "str":
                pool = ['"%s"' % s for s in MUT_STR_CORNERS
                        if '"%s"' % s != cur]
            else:
                pool = ["true"] if cur == "false" else ["false"]
            if not pool:
                continue
            lines[i] = "%slet %s: %s = %s" % (
                re.match(r"\s*", lines[i]).group(0),
                m.group(1), m.group(2), rng.choice(pool))
            return "\n".join(lines), op
    return None, "none"


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------

class Skip:
    def __init__(self, source, fn, reason):
        self.source = source
        self.fn = fn
        self.reason = reason


def harvest_files(roots):
    """[(abs_path, rel_label)] — sorted, deduped by realpath."""
    seen = set()
    out = []
    for root_label, base in roots:
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in sorted(filenames):
                if not fn.endswith(".hls"):
                    continue
                p = os.path.join(dirpath, fn)
                rp = os.path.realpath(p)
                if rp in seen:
                    continue
                seen.add(rp)
                out.append((p, os.path.join(root_label,
                                            os.path.relpath(p, base))))
    return out


def parse_file(path):
    """program dict or (None, reason)."""
    try:
        with open(path, "rb") as f:
            src = f.read()
    except OSError as e:
        return None, "cannot read: %s" % e.strerror
    try:
        return parse(src), None
    except HLError as e:
        return None, "parse: %s" % str(e)[:100]
    except Exception as e:
        return None, "parser crash: %s: %s" % (type(e).__name__, str(e)[:80])


# ---------------------------------------------------------------------------
# The seeder
# ---------------------------------------------------------------------------

REFINE_ATTEMPTS = 8


def seed_all(args):
    """The full pipeline, in five phases.

      1. parse + close + build + check  (serial, in-process — the real
         parser, the real closure laws, the real checker)
      2. rank by the feature census and cut with overprovision (the
         rank needs only the text — no execution)
      3. smoke the shortlist (parallel interpreter runs; timeouts and
         check/smoke disagreements are named skips)
      4. write the accepted seeds in rank order
      5. mutate ONLY the accepted seeds (rank is the contract; mutants
         for pruned seeds are wasted work), checks in-process, smokes
         pooled

    Returns (seeds, skips, stats) with seeds in rank order, each
    already written to disk.
    """
    rng = random.Random(args.seed)
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "m"), exist_ok=True)

    roots = []
    for d in args.pkg_cache:
        label = "pkg:" + os.path.basename(d.rstrip("/"))
        roots.append((label, d))
    if not args.no_in_tree:
        for d in IN_TREE_DIRS:
            roots.append(("tree", os.path.join(REPO_ROOT, d)))

    files = harvest_files(roots)
    skips = []
    candidates = []
    stats = {
        "files_seen": len(files),
        "files_unparsed": 0,
        "fns_seen": 0,
        "entries_considered": 0,
        "harness_checked": 0,
        "harness_refined": 0,
        "rank_pruned": 0,
        "smoke_ok": 0,
        "smoke_panic": 0,
        "smoke_timeout": 0,
        "smoke_error": 0,
        "mutants_attempted": 0,
        "mutants_born": 0,
        "mutants_dropped": 0,
        "mutant_drops_by_op": {},
    }
    seen_digests = {}

    # ---- phase 1: parse, close, build, check -----------------------------
    for path, rel in files:
        prog, err = parse_file(path)
        if prog is None:
            stats["files_unparsed"] += 1
            skips.append(Skip(rel, "<file>", err))
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                src_lines = f.read().split("\n")
        except OSError:
            skips.append(Skip(rel, "<file>", "cannot re-read source"))
            continue
        known_types = set(prog["structs"]) | set(prog["enums"])
        for fn_name, f in sorted(prog["fns"].items()):
            stats["fns_seen"] += 1
            if f["extern"]:
                continue
            if fn_name == "main":
                skips.append(Skip(rel, fn_name,
                                  "entry is main — the harness IS a main"))
                continue
            if f["struct"]:
                continue  # methods are deps, never entries
            if not f["body"]:
                continue
            stats["entries_considered"] += 1

            try:
                closure, _facts = build_closure(prog, src_lines, fn_name,
                                                args.max_lines)
            except Refusal as r:
                skips.append(Skip(rel, fn_name, r.reason))
                continue

            # --- harness attempts (refinement-safe corner vectors) ---
            harness = None
            param_summary = []
            attempts = 0
            last_err = "no attempt"
            while attempts < REFINE_ATTEMPTS:
                attempts += 1
                cand_rng = random.Random(
                    args.seed * 1000003 + attempts
                    + stable_name_hash(fn_name) % 65536)
                built, psum_or_why = build_harness(
                    fn_name, f, closure, cand_rng, rel, prog)
                if built is None:
                    # a synthesis refusal is deterministic — new
                    # corners cannot fix it, so name it and move on
                    last_err = psum_or_why
                    break
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".hls", delete=False,
                    encoding="utf-8")
                try:
                    tmp.write(built)
                    tmp.close()
                    try:
                        program = load_program(tmp.name)
                        check(program)
                        stats["harness_checked"] += 1
                        if f["param_preds"] or f["ret_pred"]:
                            stats["harness_refined"] += 1
                        harness = built
                        param_summary = psum_or_why
                        break
                    except HLError as e:
                        last_err = "check: %s" % str(e)[:120]
                finally:
                    os.unlink(tmp.name)
            if harness is None:
                skips.append(Skip(rel, fn_name, last_err))
                continue

            digest = digest_of(harness)
            if digest in seen_digests:
                seen_digests[digest]["sources"].append(rel)
                continue

            # --- purity gate over the harness text -------------------
            ok, reason = purity_verdict(harness)
            if not ok:
                skips.append(Skip(rel, fn_name, "purity: " + reason))
                continue

            cand = {
                "name": fn_name,
                "sources": [rel],
                "digest": digest,
                "params": param_summary,
                "recursive": facts_of_entry_calls(prog, fn_name,
                                                  known_types),
                "text": harness,
            }
            candidates.append(cand)
            seen_digests[digest] = cand

    # ---- phase 2: rank, cut with overprovision ---------------------------
    for c in candidates:
        hits, score = census_breakdown(c["text"], c["recursive"])
        c["census"] = hits
        c["score"] = score
    candidates.sort(key=lambda c: (-c["score"], c["name"], c["digest"]))
    if args.limit:
        provision = candidates[:args.limit * 3]
    else:
        provision = list(candidates)

    # ---- phase 3: smoke the shortlist (parallel) --------------------------
    def temp_write(text):
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".hls",
                                         delete=False, encoding="utf-8")
        tf.write(text)
        tf.close()
        return tf.name

    smoke_tasks = []
    temp_paths = {}
    for c in provision:
        p = temp_write(c["text"])
        temp_paths[c["digest"]] = p
        smoke_tasks.append((c["digest"], p))
    smoke_map = smoke_many(smoke_tasks, args.smoke_timeout)
    for p in temp_paths.values():
        try:
            os.unlink(p)
        except OSError:
            pass

    accepted = []
    for c in provision:
        v = smoke_map.get(c["digest"], "error:no verdict")
        if v == "ok":
            stats["smoke_ok"] += 1
        elif v == "timeout":
            stats["smoke_timeout"] += 1
        elif v.startswith("panic:"):
            stats["smoke_panic"] += 1
        else:
            stats["smoke_error"] += 1
        if v == "timeout" and not args.keep_timeouts:
            skips.append(Skip(c["sources"][0], c["name"],
                              "smoke: timeout — a hanging seed is wasted "
                              "fuzz time (--keep-timeouts lifts this)"))
            continue
        if v.startswith("error:"):
            # a check/smoke disagreement is an internal bug worth
            # surfacing with its message, never a silent drop
            skips.append(Skip(c["sources"][0], c["name"],
                              "smoke: %s" % v))
            continue
        if args.limit and len(accepted) >= args.limit:
            stats["rank_pruned"] += 1
            continue
        c["smoke"] = v
        accepted.append(c)

    # ---- phase 4: write the accepted seeds --------------------------------
    seeds = []
    for c in accepted:
        fname = "seed-%s-%s.hls" % (re.sub(r"\W+", "_", c["name"]),
                                    c["digest"][:8])
        with open(os.path.join(out_dir, fname), "w",
                  encoding="utf-8") as fh:
            fh.write(c["text"])
        c["file"] = fname
        c["mutants"] = []
        c["mutants_made"] = 0
        seeds.append(c)

    # ---- phase 5: mutants for the accepted seeds only ---------------------
    if args.mutate > 0 and seeds:
        mutant_recs = []
        for c in seeds:
            for k in range(args.mutate):
                stats["mutants_attempted"] += 1
                mrng = random.Random(args.seed ^ int(c["digest"][:12], 16)
                                     + k * 7919)
                msrc, op = mutate_source(c["text"], mrng)
                if msrc is None:
                    stats["mutants_dropped"] += 1
                    stats["mutant_drops_by_op"][op] = \
                        stats["mutant_drops_by_op"].get(op, 0) + 1
                    continue
                # the checker is the bouncer — in-process, cheap
                mt = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".hls", delete=False,
                    encoding="utf-8")
                rejected = False
                try:
                    mt.write(msrc)
                    mt.close()
                    try:
                        check(load_program(mt.name))
                    except HLError:
                        rejected = True
                    except Exception:
                        rejected = True
                finally:
                    os.unlink(mt.name)
                if rejected:
                    stats["mutants_dropped"] += 1
                    stats["mutant_drops_by_op"][op] = \
                        stats["mutant_drops_by_op"].get(op, 0) + 1
                    continue
                mfname = "%s-m%d.hls" % (c["file"][:-5], k)
                mpath = os.path.join(out_dir, "m", mfname)
                with open(mpath, "w", encoding="utf-8") as fh:
                    fh.write(msrc)
                mutant_recs.append((c, {
                    "file": "m/" + mfname,
                    "op": op,
                    "parent": c["digest"],
                    "_path": mpath,
                }))
        # pooled mutant smoke
        mtasks = [(id(m), m["_path"]) for _c, m in mutant_recs]
        msmoke = smoke_many(mtasks, args.smoke_timeout)
        for c, m in mutant_recs:
            v = msmoke.get(id(m), "error:no verdict")
            del m["_path"]
            if v == "timeout" and not args.keep_timeouts:
                os.unlink(os.path.join(out_dir, m["file"]))
                stats["mutants_dropped"] += 1
                stats["mutant_drops_by_op"][m["op"]] = \
                    stats["mutant_drops_by_op"].get(m["op"], 0) + 1
                continue
            if v.startswith("error:"):
                os.unlink(os.path.join(out_dir, m["file"]))
                stats["mutants_dropped"] += 1
                stats["mutant_drops_by_op"][m["op"]] = \
                    stats["mutant_drops_by_op"].get(m["op"], 0) + 1
                continue
            m["smoke"] = v
            c["mutants"].append(m)
            c["mutants_made"] += 1
            stats["mutants_born"] += 1

    return seeds, skips, stats


def smoke_many(tasks, timeout_s):
    """Run smoke_run over [(key, path)] on a bounded thread pool; the
    subprocesses are independent, so the verdicts are deterministic —
    only the wall-clock improves. Returns {key: verdict}."""
    workers = min(8, os.cpu_count() or 2)
    out = {}
    if not tasks:
        return out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(smoke_run, p, timeout_s): k for k, p in tasks}
        for fut, key in futs.items():
            out[key] = fut.result()
    return out

def facts_of_entry_calls(prog, fn_name, known_types):
    f = prog["fns"][fn_name]
    facts = body_facts(f, known_types)
    return fn_name in facts.calls


# ---------------------------------------------------------------------------
# Manifest + report
# ---------------------------------------------------------------------------

def write_manifest(out_dir, seeds, skips, stats, args):
    doc = {
        "tool": TOOL_VERSION,
        "seed": args.seed,
        "limit": args.limit,
        "mutate": args.mutate,
        "max_lines": args.max_lines,
        "no_in_tree": bool(args.no_in_tree),
        "pkg_cache": args.pkg_cache,
        "stamp": args.stamp,
        "stats": stats,
        "seeds": [
            {
                "file": s["file"],
                "fn": s["name"],
                "digest": s["digest"],
                "sources": s["sources"],
                "params": s["params"],
                "recursive": s["recursive"],
                "smoke": s["smoke"],
                "score": s["score"],
                "census": s.get("census", {}),
                "mutants_made": s.get("mutants_made", 0),
                "mutants": s.get("mutants", []),
            }
            for s in seeds
        ],
        "skips": [
            {"source": k.source, "fn": k.fn, "reason": k.reason}
            for k in skips
        ],
    }
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def print_report(seeds, skips, stats, out_dir):
    print("== %s ==" % TOOL_VERSION)
    print("corpus: %d seeds (+%d mutants), %d named skips"
          % (len(seeds), sum(len(s.get("mutants", [])) for s in seeds),
             len(skips)))
    for s in seeds[:12]:
        print("  %-42s score=%3d smoke=%-8s src=%s"
              % (s["file"], s["score"], s["smoke"].split(":")[0],
                 s["sources"][0]))
    if len(seeds) > 12:
        print("  ... and %d more" % (len(seeds) - 12))
    print("stats: %s" % json.dumps(stats, sort_keys=True))
    if skips:
        print("skip census (top reasons):")
        by_reason = {}
        for k in skips:
            key = re.sub(r"'[^']*'", "X", k.reason)[:70]
            by_reason.setdefault(key, 0)
            by_reason[key] += 1
        for reason, count in sorted(by_reason.items(),
                                    key=lambda kv: -kv[1])[:10]:
            print("  %4d  %s" % (count, reason))
    print("manifest: %s" % os.path.join(out_dir, "manifest.json"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hls-fuzzseed",
        description="Fuzz corpus seeding from real-world packages "
                    "(Stage 134).")
    ap.add_argument("--out", default=os.path.join(REPO_ROOT,
                                                  "fuzz-corpus", "seed"),
                    help="corpus output directory")
    ap.add_argument("--pkg-cache", action="append", default=[],
                    help="extra package root (repeatable)")
    ap.add_argument("--no-in-tree", action="store_true",
                    help="skip the project's own std/core/examples/tests")
    ap.add_argument("--limit", type=int, default=200,
                    help="max seeds emitted, in rank order (0 = all)")
    ap.add_argument("--mutate", type=int, default=1,
                    help="mutants attempted per seed")
    ap.add_argument("--max-lines", type=int, default=120,
                    help="harness line budget")
    ap.add_argument("--seed", type=int, default=134,
                    help="RNG seed (default 134 — deterministic)")
    ap.add_argument("--smoke-timeout", type=int, default=5,
                    help="interpreter smoke timeout, seconds")
    ap.add_argument("--keep-timeouts", action="store_true",
                    help="keep seeds whose smoke run timed out")
    ap.add_argument("--stamp", default=None,
                    help="timestamp recorded in the manifest (default: "
                         "none — the corpus is reproducible)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable report on stdout")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.limit < 0 or args.mutate < 0 or args.max_lines < 4:
        print("hls-fuzzseed: usage error — limit/mutate/max-lines out "
              "of range", file=sys.stderr)
        return 2
    for d in args.pkg_cache:
        if not os.path.isdir(d):
            print("hls-fuzzseed: --pkg-cache '%s' does not exist — a "
                  "configured source that is not there is a refusal, "
                  "not a silent empty" % d, file=sys.stderr)
            return 2

    # a stale corpus under the output dir is pruned first: the
    # manifest must describe EXACTLY what this run produced
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out, exist_ok=True)

    seeds, skips, stats = seed_all(args)
    manifest_path = write_manifest(args.out, seeds, skips, stats, args)

    if args.json:
        with open(manifest_path, encoding="utf-8") as f:
            doc = json.load(f)
        doc["result"] = "green" if seeds else "empty"
        print(json.dumps(doc, indent=2, sort_keys=True))
    elif not args.quiet:
        print_report(seeds, skips, stats, args.out)

    return 0 if seeds else 1


if __name__ == "__main__":
    sys.exit(main())
