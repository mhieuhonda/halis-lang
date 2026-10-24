#!/usr/bin/env python3
"""hls-freeze — the Stage 141 API-freeze kit: syntax + stdlib locked.

Usage:
  python3 tools/hls-freeze.py snapshot [--root PATH] [--json]
  python3 tools/hls-freeze.py verify   [--root PATH] [--json] [--quiet]
  python3 tools/hls-freeze.py amend    --reason "..." --by NAME
                                       [--root PATH] [--json]
  python3 tools/hls-freeze.py report   --out FILE.md [--stamp]
                                       [--root PATH]
  python3 tools/hls-freeze.py status   [--root PATH] [--json]

Roadmap Stage 141 is one phrase: "API freeze — syntax + stdlib
locked". Until now the surface could move because nothing held it:
a keyword could appear, a stdlib function could change its
signature, an enum could grow a variant — every one of those is a
promise to every program ever written against this language, and
nothing failed when the promise moved. This stage turns the promise
into a FROZEN DOCUMENT — `tests/api_freeze.json`, committed to the
tree, content-addressed by the sha256 of its own surface — and a
tool that refuses to let the surface and the document disagree:

  1. the SURFACE is derived LIVE from the real front-ends, never
     from prose: the keywords, the reserved identifiers, the token
     alphabet and the crate attributes are read out of the boot
     lexer and parser (the tables the checker actually tests
     membership against); the builtin functions are the checker's
     own BUILTIN_FNS table, imported, not copied; the builtin
     method surface is DEMONSTRATED, not asserted — every method in
     the probe roster is accepted by the real checker at derive
     time, and every junk name is refused, so a checker whose
     method table moved breaks the probes loudly instead of
     freezing a lie. The self-hosted front-end must agree: the
     keyword chain in src/hlc/types.hls is grepped live and must
     equal the boot lexer's set, because a language whose two
     front-ends disagree about what a keyword IS has no surface to
     freeze;

  2. the STDLIB surface is every public item of every module under
     std/ and core/, parsed with the real parser: every fn with its
     exact signature (parameter names, types, mutability, return
     type, generic parameters, effects, purity, extern-ness,
     deprecation notice, FFI taint marking, and the rendered
     requires/ensures contracts), every struct with its fields
     (types and defaultedness), every enum with its variant
     payloads, and every import edge. Line numbers are deliberately
     NOT part of the surface — the freeze pins the CONTRACT, not
     the bytes; a comment move is not an API event;

  3. the DIGEST is the sha256 of the canonical JSON of the surface
     (sorted keys, compact separators, no wall-clock, no paths, no
     machine specifics). The frozen document carries it; every
     verify recomputes it two ways — the document must agree with
     ITSELF (the digest matches its surface) and with the WORLD
     (the live surface matches the frozen one). Either agreement
     failing is a named refusal;

  4. the DRIFT CLASSIFICATION is closed: REMOVED (in the freeze,
     gone from the tree), ADDED (in the tree, absent from the
     freeze) and CHANGED (present in both, different) — each named
     at its dotted path (stdlib.std.list.fns.list_reverse_int.ret),
     because a drift you cannot point at is a drift you cannot
     review. Post-freeze ADDITIONS are drift too: the API is
     LOCKED, not merely backwards-compatible — v1.0 cannot be
     promised while the surface grows unrecorded;

  5. the AMEND is the only legal mutation, and it is a ledger
     entry, not an edit: `amend --reason "..." --by NAME` refuses
     to run without a written reason (an unnamed change to a
     frozen API is how freezes die), refuses to run when the live
     surface equals the frozen one (a no-op amendment is noise),
     computes the exact delta, rewrites the frozen document to the
     live surface, and appends a hash-chained record to
     `tests/api_freeze_amendments.jsonl` carrying who, why, the
     commit, the delta and both digests. The chain is the family's
     ONE arithmetic (imported from hltlog_parts since Stage 13):
     sha256(prev_hash + canonical-json(entry minus chain_hash)),
     genesis from 64 zeros, every reader replaying from the start
     and naming the broken line. The amendment history is the
     release notes of the freeze itself;

  6. the REPORT is the frozen API document rendered — the syntax
     surface, the per-module census, the amendment history —
     deterministic per freeze (sorted walks, no wall-clock unless
     --stamp, the digest embedded), and NEVER rendered from a
     drifted tree: report runs verify first and refuses, because a
     document that describes a surface the tree no longer has is
     not documentation, it is fiction.

The freeze file is COMMITTED (unlike every machine-local ledger in
this tree) because the frozen API is exactly the part of the
project that must travel with the repository: a freeze that lives
only on one machine freezes nothing. The amendment chain travels
with it — the same file, the same commits.

Exit codes: 0 the contract held / 1 a named refusal or a detected
drift (fail-closed) / 2 usage error or a broken probe (the tool
refuses to freeze a surface it could not demonstrate). Every
refusal names the rule, not just the fact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
for _p in (_TOOLS, os.path.join(_TOOLS, "hltlog_parts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# NOTE: the boot front-end modules are NOT imported here. The tool
# reasons about the tree it is POINTED AT: --root decides whose
# lexer, parser and checker answer the derivation, so the imports
# happen in main() after --root is known (see _load_frontends). A
# drift law verified against a disposable copy of the tree must be
# answered by THAT copy's front-ends, never by this file's repo.

from hltlog_parts.hltlog_common import GENESIS, chain_of    # noqa: E402
from hltlog_parts.hltlog_chain import verify_chain          # noqa: E402

VERSION = "0.159.0-alpha"          # the toolchain release this run stamps
STAGE = 141
FREEZE_REL = os.path.join("tests", "api_freeze.json")
CHAIN_REL = os.path.join("tests", "api_freeze_amendments.jsonl")
SURFACE_VERSION = 1


class Refusal(Exception):
    """A named refusal — the rule, not just the fact."""


class ProbeFailure(Refusal):
    """The tool could not demonstrate a piece of the surface it was
    about to freeze. Fail-closed: an undemonstrated surface is not
    frozen, it is guessed."""


# ---------------------------------------------------------------------------
# Canonicalisation — the one rendering everything is compared through.
# ---------------------------------------------------------------------------

def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def digest_of(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def _strip_lines(e):
    """Line numbers are facts of the FILE, not of the API. Recursively
    drop them so a contract's identity survives a reformat."""
    if isinstance(e, dict):
        return {k: _strip_lines(v) for k, v in e.items() if k != "line"}
    if isinstance(e, list):
        return [_strip_lines(x) for x in e]
    return e


def expr_text(e) -> str:
    """Deterministic text form of a contract expression (requires /
    ensures). Unknown node kinds fall back to the node's canonical
    JSON (lines stripped) — never a crash, never an ordering choice."""
    k = e.get("k")
    if k == "bin":
        return "(%s %s %s)" % (expr_text(e["l"]), e["op"], expr_text(e["r"]))
    if k == "un":
        return "%s(%s)" % (e["op"], expr_text(e["e"]))
    if k == "ident":
        return e["name"]
    if k == "int":
        return str(e["v"])
    if k == "float":
        return repr(e["v"])
    if k == "str":
        v = e["v"]
        if isinstance(v, (bytes, bytearray)):
            v = bytes(v).decode("utf-8", "replace")
        return json.dumps(v)
    if k == "bool":
        return "true" if e["v"] else "false"
    if k == "call":
        return "%s(%s)" % (e["name"],
                           ", ".join(expr_text(a) for a in e["args"]))
    if k == "fieldcall":
        return "%s.%s(%s)" % (expr_text(e["target"]), e["name"],
                              ", ".join(expr_text(a) for a in e["args"]))
    if k == "field":
        return "%s.%s" % (expr_text(e["target"]), e["name"])
    if k == "index":
        return "%s[%s]" % (expr_text(e["target"]), expr_text(e["idx"]))
    if k == "qmark":
        return "%s?" % expr_text(e["e"])
    return "ast:" + canonical(_strip_lines(e))


# ---------------------------------------------------------------------------
# The syntax surface — derived from the real front-ends.
# ---------------------------------------------------------------------------

# The probe roster: the candidate builtin-method surface of the
# container / concurrency types, whose membership the checker's own
# dispatch does not expose as an importable table. Each probe is a
# real program through the real checker: accepted = exists, "no
# method" = does not, ANY other refusal = the roster disagrees with
# the live checker (a loud failure — fail-closed, never silent).
# The shapes below mirror the checker's tables; if those tables move,
# the probes break and the tool refuses to freeze. A method the
# roster never names cannot be probed — extending the roster is a
# reviewed, amendable change to THIS tool, which is the honest trust
# model (hls-guarantee's probe table lives under the same rule).
PROBE_ROSTER = {
    "list": {
        "setup": "let xs: list[int] = [1, 2]",
        "uses": "",
        "workfn": "",
        "methods": {
            "len":  {"call": "xs.len()",    "ret": "int", "bind": "int",
                     "params": []},
            "push": {"call": "xs.push(3)",  "ret": "void", "bind": "void",
                     "params": ["T"]},
            "get":  {"call": "xs.get(0)",   "ret": "T", "bind": "int",
                     "params": ["int"]},
            "set":  {"call": "xs.set(0, 3)", "ret": "void", "bind": "void",
                     "params": ["int", "T"]},
            "pop":  {"call": "xs.pop()",    "ret": "T", "bind": "int",
                     "params": []},
        },
    },
    "map": {
        "setup": "let m: map[str, int] = map_new()",
        "uses": "",
        "workfn": "",
        "methods": {
            "len":    {"call": "m.len()",        "ret": "int", "bind": "int", "params": []},
            "set":    {"call": "m.set(\"k\", 1)", "ret": "void", "bind": "void", "params": ["str", "T"]},
            "get_or": {"call": "m.get_or(\"k\", 1)", "ret": "T", "bind": "int", "params": ["str", "T"]},
            "has":    {"call": "m.has(\"k\")",    "ret": "bool", "bind": "bool", "params": ["str"]},
            "keys":   {"call": "m.keys()",       "ret": "list[str]", "bind": "list[str]", "params": []},
        },
    },
    "chan": {
        "setup": "let ch: Chan[int] = chan_new()",
        "uses": "Conc",
        "workfn": "",
        "methods": {
            "send":     {"call": "ch.send(1)",     "ret": "void", "bind": "void", "params": ["T"]},
            "try_send": {"call": "ch.try_send(1)", "ret": "bool", "bind": "bool", "params": ["T"]},
            "recv":     {"call": "ch.recv()",      "ret": "T",    "bind": "int",  "params": []},
            "recv_or":  {"call": "ch.recv_or(1)",  "ret": "T",    "bind": "int",  "params": ["T"]},
            "len":      {"call": "ch.len()",       "ret": "int",  "bind": "int",  "params": []},
        },
    },
    "task": {
        "setup": "let t: Task[int] = spawn(__freeze_work)",
        "uses": "Conc",
        "workfn": "fn __freeze_work() -> int { return 7 }",
        "methods": {
            "join": {"call": "t.join()", "ret": "T", "bind": "int", "params": []},
        },
    },
}

# The negative probes: one junk name per probed type. The probe
# channel is only trustworthy if it REFUSES — a checker that accepts
# anything accepts nothing, and a surface derived through it would be
# a guess. Every negative must be refused with the no-method words.
JUNK_METHOD = "zz_freeze_junk_probe"


_RECEIVER = {"list": "xs", "map": "m", "chan": "ch", "task": "t"}


def probe_program(kind: str, method: str) -> str:
    spec = PROBE_ROSTER[kind]
    if method != JUNK_METHOD:
        entry = dict(spec["methods"][method])
    else:
        # The negative probe: a name nothing should answer to.
        entry = {"call": "%s.%s(1)" % (_RECEIVER[kind], JUNK_METHOD),
                 "ret": "int", "bind": "int"}
    # "ret" is the SURFACE shape ("T" = the element type); "bind" is
    # the concrete type the probe binds the result to. Every roster
    # entry carries both — a generic surface type is never written
    # into a probe program.
    entry.setdefault("bind", entry["ret"])
    uses = (" uses " + spec["uses"]) if spec["uses"] else ""
    body = [spec["setup"]]
    if entry["bind"] == "void":
        body.append("    " + entry["call"])
    else:
        body.append("    let _v: %s = %s" % (entry["bind"], entry["call"]))
    body.append("    return")
    parts = []
    if spec["workfn"]:
        parts.append(spec["workfn"])
    parts.append("fn main() -> void%s {" % uses)
    parts.extend(body)
    parts.append("}")
    return "\n".join(parts) + "\n"


def checker_verdict(src: bytes):
    """Run one probe through the REAL checker. Returns None when the
    program is accepted, else the diagnostic text."""
    try:
        toks = tokenize(src)
        program = Parser(toks, src).parse_program()
        check(program)
        return None
    except HLError as e:
        return str(e)
    except Exception as e:                                   # noqa: BLE001
        return "%s: %s" % (type(e).__name__, e)


def probe_method_surface():
    """Demonstrate the container / concurrency method surface."""
    out = {}
    for kind in sorted(PROBE_ROSTER):
        spec = PROBE_ROSTER[kind]
        # The negative first: the channel must refuse.
        neg = checker_verdict(probe_program(kind, JUNK_METHOD).encode())
        if neg is None or "no method" not in neg:
            raise ProbeFailure(
                "the %s negative probe was not refused with a no-method "
                "diagnostic (got: %s) — the probe channel cannot be "
                "trusted for this type" % (kind, neg or "ACCEPTED"))
        tbl = {}
        for name in sorted(spec["methods"]):
            verdict = checker_verdict(
                probe_program(kind, name).encode("utf-8"))
            if verdict is not None:
                raise ProbeFailure(
                    "the probe roster disagrees with the live checker: "
                    "%s.%s was refused (%s) — the method surface moved; "
                    "the roster in tools/hls-freeze.py must be amended "
                    "to match reality before the surface can be frozen"
                    % (kind, name, verdict))
            shape = spec["methods"][name]
            tbl[name] = {"params": shape["params"], "ret": shape["ret"]}
        out[kind] = tbl
    return out


def primitive_method_surface():
    """The primitive method tables — imported, not copied. These ARE
    the tables the checker tests membership against."""
    def tuple_tbl(tbl):
        return {name: {"params": list(sig[0]), "ret": sig[1]}
                for name, sig in sorted(tbl.items())}

    def plain_tbl(tbl):
        return {name: {"params": [], "ret": ret}
                for name, ret in sorted(tbl.items())}

    return {
        "str": tuple_tbl(STR_M),
        "int": plain_tbl(INT_M),
        "float": plain_tbl(FLOAT_M),
        "bool": plain_tbl(BOOL_M),
    }


def selfhost_keywords(root: str):
    """The self-hosted front-end's keyword chain, grepped LIVE from
    src/hlc/types.hls. The two front-ends must agree — a language
    whose boot lexer and self-hosted compiler disagree about what a
    keyword IS has no surface to freeze."""
    path = os.path.join(root, "src", "hlc", "types.hls")
    if not os.path.isfile(path):
        raise ProbeFailure("the self-hosted compiler is missing: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"fn is_keyword\(w: str\) -> bool \{(.*?)\n\}",
                  text, re.S)
    if not m:
        raise ProbeFailure(
            "src/hlc/types.hls no longer carries the is_keyword chain — "
            "the self-hosted keyword table moved; update the derivation")
    words = sorted(set(re.findall(r'==\s*"([a-z_]+)"', m.group(1))))
    if not words:
        raise ProbeFailure(
            "the is_keyword chain in src/hlc/types.hls parsed to an "
            "empty keyword set — the derivation is broken")
    return words


_ATTR_ARG_WORDS = ("always", "never")


def fn_attr_census():
    """The fn-attribute vocabulary, read out of the parser's OWN
    census: a probe with a junk attribute name is refused with the
    'known: ...' enumeration. The raw enumeration is frozen verbatim
    (it is the parser's own words — any vocabulary change moves it)
    alongside the cleaned name list."""
    src = b"#[zz_freeze_census]\nfn main() -> void { return }\n"
    diag = checker_verdict(src)
    if diag is None or "unknown attribute" not in diag or "known:" not in diag:
        raise ProbeFailure(
            "the attribute census probe was not refused with the "
            "known-attributes enumeration (got: %s) — the parser's "
            "attribute vocabulary cannot be read live" % (diag or "ACCEPTED"))
    tail = diag.split("known:", 1)[1]
    # The diagnostic carries a location suffix ("(line N[:C])") after
    # the message; cut it, then drop the census's own closing paren —
    # the enumeration legitimately ends with one (taint_sink(ARGS)).
    cut = tail.find(" (line")
    if cut != -1:
        tail = tail[:cut]
    tail = tail.strip()
    if not tail.endswith("))"):
        raise ProbeFailure(
            "the attribute enumeration does not end with the expected "
            "'...))' shape (%r) — the parser's diagnostic format "
            "moved; update the census reader" % tail[-24:])
    raw = tail[:-1]
    names = []
    for piece in raw.split(","):
        piece = piece.strip()
        if piece.startswith("and "):
            # "and — inside extern blocks only — taint_source"
            piece = piece.split("—")[-1].strip()
        m = re.match(r"^([a-z_]+)(\([^)]*\))?$", piece)
        if not m:
            continue
        name, argshape = m.group(1), (m.group(2) or "")
        if argshape:
            inner = argshape[1:-1].strip().strip('"')
            if inner in _ATTR_ARG_WORDS:
                names.append("%s(%s)" % (name, inner))
                continue
        names.append(name)
    if not names:
        raise ProbeFailure(
            "the parser's attribute enumeration cleaned to an empty "
            "vocabulary — the census format moved; update the cleaner")
    return raw, names


def syntax_surface(root: str):
    keywords = sorted(KEYWORDS)
    selfhost = selfhost_keywords(root)
    if selfhost != keywords:
        raise ProbeFailure(
            "the two front-ends disagree about the keywords: boot lexer "
            "has %s, src/hlc/types.hls has %s — a language whose "
            "front-ends disagree has no surface to freeze"
            % (keywords, selfhost))
    raw_attrs, attr_names = fn_attr_census()
    return {
        "keywords": keywords,
        "reserved_identifiers": sorted(RESERVED_IDENTIFIERS),
        "tokens_two_char": sorted(TWO_CHAR),
        "tokens_one_char": sorted(ONE_CHAR),
        "effects": sorted(KNOWN_EFFECTS),
        "crate_attrs": sorted(CRATE_ATTRS),
        "fn_attr_census_raw": raw_attrs,
        "fn_attrs": attr_names,
        "builtin_fns": sorted(BUILTIN_FNS),
        "builtin_methods": _merge_method_surfaces(),
        "selfhost_keyword_agreement": True,
        "probe_law": True,
    }


def _merge_method_surfaces():
    out = primitive_method_surface()
    out.update(probe_method_surface())
    return out


# ---------------------------------------------------------------------------
# The stdlib surface — every public item of every module, parsed.
# ---------------------------------------------------------------------------

def module_files(root: str):
    """Every module file under std/ and core/, as (module-id, path).
    The module id is the file's repo-relative path with separators
    dotted off the .hls suffix (std/http2/http2_conn.hls ->
    std.http2.http2_conn) — the census's naming convention for the
    ledger; the exact import spellings live in each module's
    imports row."""
    out = []
    for fam in ("std", "core"):
        base = os.path.join(root, fam)
        if not os.path.isdir(base):
            raise Refusal("the %s/ family is missing under %s" % (fam, root))
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for fn in sorted(filenames):
                if not fn.endswith(".hls"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                out.append((rel[:-4].replace("/", "."), full))
    return sorted(out)


def deprecated_text(v):
    if v is None or v is False:
        return None
    if v is True:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, str):
        return v
    return canonical(_strip_lines(v))


def fn_record(f):
    rec = {
        "params": [[n, t, bool(mut)] for (n, t, mut) in f["params"]],
        "ret": f["ret"],
        "effects": sorted(f.get("effects") or []),
        "pure": bool(f.get("pure")),
        "extern": bool(f.get("extern")),
        "generic": list(f.get("typeparams") or []),
    }
    if f.get("requires") is not None:
        rec["requires"] = expr_text(f["requires"])
    if f.get("ensures") is not None:
        rec["ensures"] = expr_text(f["ensures"])
    dep = deprecated_text((f.get("attrs") or {}).get("deprecated"))
    if dep is not None:
        rec["deprecated"] = dep
    if f.get("taint_source"):
        rec["taint_source"] = True
    if f.get("taint_sinks"):
        rec["taint_sinks"] = sorted(f["taint_sinks"])
    return rec


def struct_record(s):
    return {
        "generic": list(s.get("typeparams") or []),
        "fields": [[n, t, dflt is not None]
                   for (n, t, dflt) in s["fields"]],
    }


def enum_record(e):
    return {
        "generic": list(e.get("typeparams") or []),
        "variants": [[name, list(pays)]
                     for (name, pays) in e["variants"]],
    }


def module_record(path: str):
    with open(path, "rb") as fh:
        src = fh.read()
    try:
        toks = tokenize(src)
        program = Parser(toks, src).parse_program()
    except HLError as e:
        raise Refusal("module %s does not parse (%s) — the stdlib "
                      "surface can only be frozen from a tree that "
                      "parses" % (path, e))
    fns = {}
    for key in sorted(program["fns"]):
        fns[key] = fn_record(program["fns"][key])
    structs = {}
    for name in sorted(program["structs"]):
        structs[name] = struct_record(program["structs"][name])
    enums = {}
    for name in sorted(program["enums"]):
        enums[name] = enum_record(program["enums"][name])
    imports = sorted(i["path"] for i in program["imports"])
    return {"fns": fns, "structs": structs, "enums": enums,
            "imports": imports}


def stdlib_surface(root: str):
    out = {}
    for mid, path in module_files(root):
        out[mid] = module_record(path)
    return out


def derive_surface(root: str):
    return {
        "syntax": syntax_surface(root),
        "stdlib": stdlib_surface(root),
    }


# ---------------------------------------------------------------------------
# Provenance, freeze IO, the chain.
# ---------------------------------------------------------------------------

def git_commit(root: str) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:                                        # noqa: BLE001
        pass
    return ""


def freeze_path(root: str) -> str:
    return os.path.join(root, FREEZE_REL)


def chain_path(root: str) -> str:
    return os.path.join(root, CHAIN_REL)


def load_freeze(root: str):
    p = freeze_path(root)
    if not os.path.isfile(p):
        raise Refusal("no freeze exists (%s) — the surface cannot be "
                      "verified against a document that was never "
                      "written; run snapshot first" % FREEZE_REL)
    with open(p, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as e:
            raise Refusal("the freeze document is not valid JSON (%s) "
                          "— a frozen document that cannot be read is "
                          "not frozen, it is lost" % e)
    if doc.get("surface_version") != SURFACE_VERSION:
        raise Refusal("the freeze document carries surface version %r, "
                      "this tool speaks version %d"
                      % (doc.get("surface_version"), SURFACE_VERSION))
    if "surface" not in doc or "digest" not in doc:
        raise Refusal("the freeze document is missing its surface or "
                      "its digest — it disagrees with itself by "
                      "construction")
    return doc


def check_freeze_self(doc):
    """The document must agree with ITSELF: the digest is the sha256
    of the canonical surface it carries."""
    want = digest_of(doc["surface"])
    if doc["digest"] != want:
        raise Refusal(
            "the freeze document disagrees with itself: the recorded "
            "digest is %s, the surface it carries hashes to %s — the "
            "document was hand-edited or corrupted" % (doc["digest"], want))


def load_chain(root: str):
    p = chain_path(root)
    if not os.path.isfile(p):
        return []
    records = []
    with open(p, "r", encoding="utf-8") as fh:
        for ln, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise Refusal("the amendment chain is not valid JSON at "
                              "line %d (%s) — a chain that cannot be "
                              "replayed proves nothing" % (ln, e))
    return records


def verify_chain_records(root: str):
    records = load_chain(root)
    head, errors = verify_chain(records, origin=CHAIN_REL)
    if errors:
        where, msg = errors[0]
        raise Refusal("the amendment chain is BROKEN at %s: %s — a "
                      "tampered chain is evidence, not a document, and "
                      "nothing proceeds from it (%d break(s) total)"
                      % (where, msg, len(errors)))
    return records, head


def write_atomic(path: str, data: str):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-freeze-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# The drift classification — closed, path-addressed.
# ---------------------------------------------------------------------------

def _join(path):
    return ".".join(path) if path else "<surface>"


def diff_surface(frozen, live):
    """Compare two surfaces. Returns (removed, added, changed) as lists
    of (dotted-path, old, new) — old/new None for pure add/remove,
    canonical JSON for complex values. A path you can point at is a
    drift you can review."""
    removed, added, changed = [], [], []

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) - set(b)):
                removed.append((_join(path + [k]), a[k], None))
            for k in sorted(set(b) - set(a)):
                added.append((_join(path + [k]), None, b[k]))
            for k in sorted(set(a) & set(b)):
                walk(a[k], b[k], path + [k])
        elif isinstance(a, list) and isinstance(b, list):
            if a != b:
                changed.append((_join(path), a, b))
        else:
            if not isinstance(a, (dict, list)) and \
               not isinstance(b, (dict, list)) and a == b:
                return
            changed.append((_join(path), a, b))

    walk(frozen, live, [])
    return removed, added, changed


def drift_lines(removed, added, changed, cap=40):
    lines = []
    for path, old, _new in removed:
        lines.append("REMOVED   %s" % path)
    for path, _old, new in added:
        lines.append("ADDED     %s" % path)
    for path, old, new in changed:
        o = old if isinstance(old, str) else canonical(_strip_lines(old))
        n = new if isinstance(new, str) else canonical(_strip_lines(new))
        if len(o) > 60:
            o = o[:57] + "..."
        if len(n) > 60:
            n = n[:57] + "..."
        lines.append("CHANGED   %s : %s -> %s" % (path, o, n))
    if len(lines) > cap:
        lines = lines[:cap] + ["... and %d more" % (len(lines) - cap)]
    return lines


# ---------------------------------------------------------------------------
# The commands.
# ---------------------------------------------------------------------------

def counts_of(surface):
    syn = surface["syntax"]
    std = surface["stdlib"]
    fns = structs = enums = imports = 0
    for mid in sorted(std):
        mod = std[mid]
        fns += len(mod["fns"])
        structs += len(mod["structs"])
        enums += len(mod["enums"])
        imports += len(mod["imports"])
    methods = 0
    for _ty in sorted(syn["builtin_methods"]):
        methods += len(syn["builtin_methods"][_ty])
    return {
        "keywords": len(syn["keywords"]),
        "reserved_identifiers": len(syn["reserved_identifiers"]),
        "tokens": len(syn["tokens_two_char"]) + len(syn["tokens_one_char"]),
        "effects": len(syn["effects"]),
        "crate_attrs": len(syn["crate_attrs"]),
        "fn_attrs": len(syn["fn_attrs"]),
        "builtin_fns": len(syn["builtin_fns"]),
        "builtin_methods": methods,
        "modules": len(std),
        "stdlib_fns": fns,
        "stdlib_structs": structs,
        "stdlib_enums": enums,
        "import_edges": imports,
    }


def cmd_snapshot(args):
    root = os.path.abspath(args.root)
    p = freeze_path(root)
    if os.path.exists(p):
        raise Refusal("a freeze already exists (%s) — the freeze is "
                      "created ONCE; the surface is changed through "
                      "amend, never by re-snapshotting, or it freezes "
                      "nothing" % FREEZE_REL)
    if os.path.exists(chain_path(root)):
        raise Refusal("an amendment chain already exists (%s) without a "
                      "freeze beside it — that is a corrupted state no "
                      "snapshot may paper over" % CHAIN_REL)
    surface = derive_surface(root)
    counts = counts_of(surface)
    doc = {
        "surface_version": SURFACE_VERSION,
        "tool": "hls-freeze",
        "stage": STAGE,
        "release": VERSION,
        "frozen_commit": git_commit(root),
        "digest": digest_of(surface),
        "surface": surface,
    }
    write_atomic(p, canonical(doc) + "\n")
    if args.json:
        print(canonical({"ok": True, "command": "snapshot",
                         "digest": doc["digest"],
                         "frozen_commit": doc["frozen_commit"],
                         "counts": counts}))
    else:
        print("FROZEN: the API surface is locked under %s" % FREEZE_REL)
        print("  digest        %s" % doc["digest"])
        print("  frozen at     %s" % (doc["frozen_commit"] or "(no git)"))
        print("  keywords      %d   tokens %d   effects %d"
              % (counts["keywords"], counts["tokens"], counts["effects"]))
        print("  builtin fns   %d   builtin methods %d"
              % (counts["builtin_fns"], counts["builtin_methods"]))
        print("  modules       %d   fns %d   structs %d   enums %d"
              % (counts["modules"], counts["stdlib_fns"],
                 counts["stdlib_structs"], counts["stdlib_enums"]))
        print("The surface moves from here only through amend --reason.")
    return 0


def verify_full(root: str):
    """The whole contract: self-consistency, chain integrity, and the
    world. Returns (doc, counts, removed, added, changed)."""
    doc = load_freeze(root)
    check_freeze_self(doc)
    verify_chain_records(root)
    live = derive_surface(root)
    removed, added, changed = diff_surface(doc["surface"], live)
    return doc, counts_of(doc["surface"]), removed, added, changed


def cmd_verify(args):
    doc, counts, removed, added, changed = verify_full(
        os.path.abspath(args.root))
    n = len(removed) + len(added) + len(changed)
    if n:
        if args.json:
            print(canonical({
                "ok": False, "command": "verify", "verdict": "DRIFTED",
                "digest": doc["digest"], "drift": n,
                "removed": [p for p, _o, _n in removed],
                "added": [p for p, _o, _n in added],
                "changed": [p for p, _o, _n in changed]}))
        else:
            print("DRIFTED: the live surface disagrees with the freeze "
                  "(%d drift point(s))" % n)
            for line in drift_lines(removed, added, changed):
                print("  " + line)
            print("The API is frozen. The surface moves through amend "
                  "--reason, or the drift is reverted.")
        return 1
    if args.json:
        print(canonical({"ok": True, "command": "verify",
                         "verdict": "IDENTICAL", "digest": doc["digest"],
                         "counts": counts}))
    elif not args.quiet:
        print("IDENTICAL: the live surface matches the freeze.")
        print("  digest  %s" % doc["digest"])
        print("  modules %d   stdlib fns %d   builtin fns %d   "
              "keywords %d" % (counts["modules"], counts["stdlib_fns"],
                               counts["builtin_fns"], counts["keywords"]))
    return 0


def cmd_amend(args):
    root = os.path.abspath(args.root)
    if not args.reason or not args.reason.strip():
        raise Refusal("an amendment without a written reason is "
                      "refused — an unnamed change to a frozen API is "
                      "how freezes die (--reason is required)")
    if not args.by or not args.by.strip():
        raise Refusal("an amendment without a name is refused — the "
                      "sanction law applies to the freezer too "
                      "(--by is required)")
    doc, _counts, removed, added, changed = verify_full(root)
    n = len(removed) + len(added) + len(changed)
    if not n:
        raise Refusal("nothing to amend: the live surface already "
                      "equals the freeze — a no-op amendment is noise, "
                      "and noise in the amendment ledger is how real "
                      "amendments stop being read")
    live = derive_surface(root)
    new_digest = digest_of(live)
    records, head = verify_chain_records(root)
    entry = {
        "seq": len(records) + 1,
        "kind": "amend",
        "by": args.by.strip(),
        "reason": args.reason.strip(),
        "commit": git_commit(root),
        "delta": {
            "removed": [p for p, _o, _n in removed],
            "added": [p for p, _o, _n in added],
            "changed": [p for p, _o, _n in changed],
        },
        "from_digest": doc["digest"],
        "to_digest": new_digest,
        "prev_hash": head,
    }
    entry["chain_hash"] = chain_of(entry["prev_hash"], entry)
    new_doc = {
        "surface_version": SURFACE_VERSION,
        "tool": "hls-freeze",
        "stage": STAGE,
        "release": VERSION,
        "frozen_commit": git_commit(root),
        "digest": new_digest,
        "surface": live,
    }
    write_atomic(freeze_path(root), canonical(new_doc) + "\n")
    with open(chain_path(root), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(canonical(entry) + "\n")
    if args.json:
        print(canonical({"ok": True, "command": "amend",
                         "seq": entry["seq"], "digest": new_digest,
                         "delta": n, "chain_hash": entry["chain_hash"]}))
    else:
        print("AMENDED: the freeze now carries the live surface "
              "(seq %d)." % entry["seq"])
        print("  from  %s" % doc["digest"])
        print("  to    %s" % new_digest)
        print("  delta %d drift point(s), recorded on the chain" % n)
        for line in drift_lines(removed, added, changed, cap=12):
            print("  " + line)
        print("Commit BOTH %s and %s together — the document and its "
              "history are one contract." % (FREEZE_REL, CHAIN_REL))
    return 0


def cmd_report(args):
    root = os.path.abspath(args.root)
    doc, counts, removed, added, changed = verify_full(root)
    n = len(removed) + len(added) + len(changed)
    if n:
        raise Refusal("the tree has drifted from the freeze (%d drift "
                      "point(s), starting at %s) — a report is never "
                      "rendered from a surface the tree no longer has; "
                      "amend or revert first"
                      % (n, drift_lines(removed, added, changed,
                                        cap=1)[0].split()[1] if
                         drift_lines(removed, added, changed, cap=1)
                         else "<surface>"))
    records, head = verify_chain_records(root)
    L = []
    L.append("# The frozen API of Halis — Stage %d (v0.159.0-alpha)" % STAGE)
    L.append("")
    L.append("Rendered from the Stage 141 freeze ledger by"
             " `tools/hls-freeze.py report`; deterministic per freeze —")
    L.append("sorted walks, no wall-clock%s, the digest embedded — so"
             " two renders of one freeze are byte-identical and any"
             " hand-edit is a diff."
             % (" (stamped)" if args.stamp else ""))
    if args.stamp:
        L.append("")
        L.append("Generated: %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime()))
    L.append("")
    L.append("| Field | Value |")
    L.append("|---|---|")
    L.append("| Freeze digest | `%s` |" % doc["digest"])
    L.append("| Frozen at commit | `%s` |"
             % (doc.get("frozen_commit") or "(unknown)"))
    L.append("| Release | %s |" % doc.get("release", "?"))
    L.append("| Amendment head | `%s` |" % head[:16])
    L.append("| Amendments | %d |" % len(records))
    L.append("")
    L.append("## The contract")
    L.append("")
    L.append("From this freeze the language's surface is LOCKED: no"
             " keyword, token, attribute, builtin, method or stdlib"
             " signature moves — not even by addition — without an"
             " `amend --reason` record on the chain. Verify with"
             " `make freeze-verify`; the surface moves with"
             " `tools/hls-freeze.py amend`.")
    L.append("")
    L.append("## Counts")
    L.append("")
    L.append("| Census | Count |")
    L.append("|---|---|")
    for key in ("keywords", "reserved_identifiers", "tokens", "effects",
                "crate_attrs", "fn_attrs", "builtin_fns",
                "builtin_methods", "modules", "stdlib_fns",
                "stdlib_structs", "stdlib_enums", "import_edges"):
        L.append("| %s | %d |" % (key.replace("_", " "), counts[key]))
    L.append("")
    syn = doc["surface"]["syntax"]
    L.append("## The syntax surface")
    L.append("")
    L.append("Derived live from the boot front-ends; the self-hosted"
             " compiler's keyword chain is grepped and must agree.")
    L.append("")
    L.append("**Keywords (%d):** %s" % (len(syn["keywords"]),
                                        ", ".join(syn["keywords"])))
    L.append("")
    L.append("**Reserved identifiers (%d):** %s"
             % (len(syn["reserved_identifiers"]),
                ", ".join(syn["reserved_identifiers"])))
    L.append("")
    L.append("**Tokens:** two-char %s; one-char %s"
             % (" ".join(syn["tokens_two_char"]),
                " ".join(syn["tokens_one_char"])))
    L.append("")
    L.append("**Effects (%d):** %s" % (len(syn["effects"]),
                                       ", ".join(syn["effects"])))
    L.append("")
    L.append("**Crate attributes:** %s" % ", ".join(syn["crate_attrs"]))
    L.append("")
    L.append("**Function attributes:** %s" % ", ".join(syn["fn_attrs"]))
    L.append("")
    L.append("**Builtin functions (%d):** %s"
             % (len(syn["builtin_fns"]), ", ".join(syn["builtin_fns"])))
    L.append("")
    L.append("**Builtin methods:**")
    L.append("")
    L.append("| Receiver | Method | Params | Returns |")
    L.append("|---|---|---|---|")
    for ty in sorted(syn["builtin_methods"]):
        for name in sorted(syn["builtin_methods"][ty]):
            m = syn["builtin_methods"][ty][name]
            L.append("| %s | %s | (%s) | %s |"
                     % (ty, name, ", ".join(m["params"]), m["ret"]))
    L.append("")
    L.append("## The stdlib surface")
    L.append("")
    std = doc["surface"]["stdlib"]
    for mid in sorted(std):
        mod = std[mid]
        head_n = "### `%s`" % mid
        tail_n = []
        if mod["structs"]:
            tail_n.append("%d struct(s)" % len(mod["structs"]))
        if mod["enums"]:
            tail_n.append("%d enum(s)" % len(mod["enums"]))
        tail_n.append("%d fn(s)" % len(mod["fns"]))
        L.append("%s — %s" % (head_n, ", ".join(tail_n)))
        L.append("")
        if mod["imports"]:
            L.append("Imports: %s"
                     % ", ".join("`%s`" % i for i in mod["imports"]))
            L.append("")
        for name in sorted(mod["structs"]):
            s = mod["structs"][name]
            gp = ("[%s]" % ", ".join(s["generic"])) if s["generic"] else ""
            fields = ", ".join(
                "%s: %s%s" % (f[0], f[1],
                              " = default" if f[2] else "")
                for f in s["fields"])
            L.append("- `struct %s%s { %s }`" % (name + gp, "", fields))
        for name in sorted(mod["enums"]):
            e = mod["enums"][name]
            gp = ("[%s]" % ", ".join(e["generic"])) if e["generic"] else ""
            vs = " | ".join(
                v[0] + (("(" + ", ".join(v[1]) + ")") if v[1] else "")
                for v in e["variants"])
            L.append("- `enum %s%s { %s }`" % (name + gp, "", vs))
        for key in sorted(mod["fns"]):
            f = mod["fns"][key]
            gp = ("[%s]" % ", ".join(f["generic"])) if f["generic"] else ""
            params = ", ".join(
                ("mut " if p[2] else "") + p[0] + ": " + p[1]
                for p in f["params"])
            sig = "`fn %s%s(%s) -> %s`" % (key + gp, "", params, f["ret"])
            notes = []
            if f["extern"]:
                notes.append("extern")
            if f["pure"]:
                notes.append("pure")
            if f["effects"]:
                notes.append("uses " + ", ".join(f["effects"]))
            if f.get("deprecated") is not None:
                notes.append("deprecated")
            if "requires" in f:
                notes.append("requires %s" % f["requires"])
            if "ensures" in f:
                notes.append("ensures %s" % f["ensures"])
            L.append("- %s%s" % (sig,
                                 (" — " + "; ".join(notes))
                                 if notes else ""))
        L.append("")
    L.append("## The amendment history")
    L.append("")
    if not records:
        L.append("No amendments — the freeze of v0.159.0-alpha is the"
                 " surface as it was frozen.")
    else:
        L.append("| Seq | By | Delta | Reason |")
        L.append("|---|---|---|---|")
        for rec in records:
            d = rec.get("delta", {})
            nd = len(d.get("removed", [])) + len(d.get("added", [])) \
                + len(d.get("changed", []))
            L.append("| %d | %s | %d | %s |"
                     % (rec.get("seq", 0), rec.get("by", "?"), nd,
                        (rec.get("reason", "") or "").replace("|", "\\|")))
    L.append("")
    L.append("---")
    L.append("")
    L.append("Freeze digest `%s` · amendment head `%s` · rendered by"
             " hls-freeze %s. This document is generated; edit the"
             " surface through the tree, not through the prose."
             % (doc["digest"], head, VERSION))
    L.append("")
    if args.out:
        write_atomic(os.path.abspath(args.out), "\n".join(L))
        print("REPORT: %s written from freeze %s (%d modules, %d "
              "amendments)" % (args.out, doc["digest"][:16] + "...",
                               counts["modules"], len(records)))
    else:
        print("\n".join(L))
    return 0


def cmd_status(args):
    root = os.path.abspath(args.root)
    doc, counts, removed, added, changed = verify_full(root)
    n = len(removed) + len(added) + len(changed)
    verdict = "IDENTICAL" if n == 0 else "DRIFTED"
    if args.json:
        print(canonical({"ok": n == 0, "command": "status",
                         "verdict": verdict, "digest": doc["digest"],
                         "drift": n, "counts": counts}))
    else:
        print("%s: freeze %s" % (verdict, doc["digest"]))
        print("  modules %d   stdlib fns %d   structs %d   enums %d"
              % (counts["modules"], counts["stdlib_fns"],
                 counts["stdlib_structs"], counts["stdlib_enums"]))
        if n:
            for line in drift_lines(removed, added, changed):
                print("  " + line)
    return 0 if n == 0 else 1


def _preparse_root(argv):
    """Find --root in raw argv (before argparse) — the front-end
    imports depend on it."""
    for i, a in enumerate(argv):
        if a == "--root" and i + 1 < len(argv):
            return os.path.abspath(argv[i + 1])
        if a.startswith("--root="):
            return os.path.abspath(a.split("=", 1)[1])
    return _ROOT


def _load_frontends(root):
    """Import the boot front-end FROM THE TREE UNDER ROOT: the
    derivation, the probes and the census must be answered by the
    tree being verified, not by the tree this file happens to live
    in."""
    if root not in sys.path:
        sys.path.insert(0, root)
    from tools.hlinterp.lexer import (KEYWORDS, RESERVED_IDENTIFIERS, TWO_CHAR,
                            ONE_CHAR, tokenize, HLError)
    from tools.hlinterp.parser import Parser, CRATE_ATTRS, KNOWN_EFFECTS
    from tools.hlinterp.checker import check
    from tools.hlinterp.checking.helpers import (BUILTIN_FNS, STR_M, INT_M,
                                       FLOAT_M, BOOL_M)
    globals().update({
        "KEYWORDS": KEYWORDS, "RESERVED_IDENTIFIERS": RESERVED_IDENTIFIERS,
        "TWO_CHAR": TWO_CHAR, "ONE_CHAR": ONE_CHAR,
        "tokenize": tokenize, "HLError": HLError,
        "Parser": Parser, "CRATE_ATTRS": CRATE_ATTRS,
        "KNOWN_EFFECTS": KNOWN_EFFECTS, "check": check,
        "BUILTIN_FNS": BUILTIN_FNS, "STR_M": STR_M,
        "INT_M": INT_M, "FLOAT_M": FLOAT_M, "BOOL_M": BOOL_M,
    })


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    _load_frontends(_preparse_root(argv))
    ap = argparse.ArgumentParser(
        prog="hls-freeze",
        description="the Stage 141 API-freeze kit: syntax + stdlib "
                    "locked, frozen once, amended on the chain")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--root", default=_ROOT,
                       help="tree root (default: this repository)")

    p = sub.add_parser("snapshot", help="freeze the live surface (once)")
    common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("verify", help="the anti-rot pass")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("amend", help="the only legal mutation")
    common(p)
    p.add_argument("--reason", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_amend)

    p = sub.add_parser("report", help="render the frozen API document")
    common(p)
    p.add_argument("--out", required=True)
    p.add_argument("--stamp", action="store_true")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("status", help="quick verdict")
    common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except Refusal as e:
        print("REFUSED: %s" % e, file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
