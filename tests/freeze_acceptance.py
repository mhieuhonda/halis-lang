#!/usr/bin/env python3
"""Stage 141 acceptance gate — the API freeze: syntax + stdlib
locked (v0.159.0-alpha).

Run with `make freeze-acceptance` (or
`python3 tests/freeze_acceptance.py`).

Eight sections over the REAL freeze (the committed
tests/api_freeze.json + its amendment chain), the REAL front-ends
(boot lexer/parser/checker imported and probed directly — the gate
never trusts the tool's own reader where it can re-derive), the
REAL self-hosted compiler source (the keyword chain grepped live),
disposable copies of the tree for the drift laws, the tool's real
CLI (subprocess, the interface a user drives), and the ok-corpus
showcase:

  1. the convention  — the freeze exists, is valid JSON at surface
     version 1, stage 141, release 0.159.0-alpha; the digest the
     document carries is the sha256 of the canonical surface it
     carries (the gate's own canonicaliser, not the tool's); the
     frozen commit is a 40-hex sha; the amendment chain exists and
     replays clean from genesis; `status` and `verify` answer
     IDENTICAL on the tree; and the freeze is COMMITTED —
     `git ls-files` names both the document and the chain, because
     a freeze that does not travel with the repository freezes
     nothing

  2. the syntax      — the gate re-derives the syntax surface from
     law             the live front-ends and requires the freeze to
                     carry exactly it: keywords/reserved/tokens
                     from boot.lexer, crate attributes and the
                     effect vocabulary from boot.parser, the
                     builtin functions from the checker's own
                     BUILTIN_FNS table, the primitive method tables
                     from the checker's own STR_M/INT_M/FLOAT_M/
                     BOOL_M; the self-hosted keyword chain is
                     grepped from src/hlc/types.hls and must equal
                     the boot set; the attribute census is probed
                     live (a junk attribute, refused, its
                     known-list captured) and must equal the frozen
                     raw census; and the container/concurrency
                     method surface is DEMONSTRATED — the gate's
                     own probes through the real checker accept
                     exactly the frozen roster and refuse a junk
                     name

  3. the census      — the gate walks std/ and core/ itself, parses
     law             every module with the real parser and rebuilds
                     the stdlib surface with its own record
                     builder, then requires the rebuilt census to
                     equal the frozen one canonically — 98 modules,
                     3402 fns, 185 structs, 36 enums, row for row,
                     signature for signature. An independent
                     re-derivation, not a replay of the tool's
                     code path

  4. the removal     — over a disposable copy of the tree: a
     law             deleted stdlib fn is REMOVED at its dotted
                     path; a keyword deleted from BOTH front-ends
                     is named in the syntax drift; a keyword
                     deleted from ONE front-end is refused outright
                     (the two-front-end agreement law fires before
                     any comparison)

  5. the mutation    — a parameter type change, a return type
     law             change, an effect clause change, a removed
                     enum variant, a removed struct field and a
                     newly-deprecated fn are each CHANGED/ADDED at
                     their exact path — a drift you can point at is
                     a drift you can review

  6. the addition    — a new stdlib fn and a new builtin are
     law             ADDED drift (the API is locked, not merely
                     backwards-compatible); and the contrast law: a
                     comment-only edit moves NO row — the freeze
                     pins the contract, not the bytes

  7. the amend       — missing --reason is a usage error;
     law             whitespace-only --reason, missing --by and a
                     no-op delta are named refusals; a real
                     amendment rewrites the document to the live
                     surface and appends exactly one record whose
                     delta is the diff the tool computed, whose
                     from_digest is the old digest and to_digest the
                     new one, and whose chain_hash the gate
                     recomputes through the family's imported
                     arithmetic (chain_of from hltlog_parts, the
                     ONE arithmetic since Stage 13); verify is green
                     after; a second amend is refused (nothing to
                     amend); a tampered record breaks the replay at
                     its line

  8. the report      — the frozen API document renders twice
     + corpus         byte-identically (deterministic per freeze,
     showcase         no wall-clock), embeds the digest and the
                     amendment table, and REFUSES to render from a
                     drifted tree (a report from drift is fiction);
                     the corpus showcase
                     tests/ok/feat_stage141_apifreeze.hls boots
                     through the real checker, runs green on the
                     interpreter with its pinned output, is
                     hlfmt-canonical, hllint-clean, and matches
                     natively byte for byte

Exit code 0 = all acceptance criteria met.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "hltlog_parts"))

from boot.lexer import (                     # noqa: E402
    KEYWORDS, RESERVED_IDENTIFIERS, TWO_CHAR, ONE_CHAR, tokenize, HLError,
)
from boot.parser import Parser, CRATE_ATTRS, KNOWN_EFFECTS   # noqa: E402
from boot.checker import check as checker_check   # noqa: E402
from boot.checking.helpers import (          # noqa: E402
    BUILTIN_FNS, STR_M, INT_M, FLOAT_M, BOOL_M,
)
from hltlog_parts.hltlog_common import GENESIS, chain_of    # noqa: E402

FREEZE = os.path.join(ROOT, "tests", "api_freeze.json")
CHAIN = os.path.join(ROOT, "tests", "api_freeze_amendments.jsonl")
TOOL = [sys.executable, os.path.join(ROOT, "tools", "hls-freeze.py")]
BOOT = [sys.executable, os.path.join(ROOT, "boot", "boot.py")]
FMT = [sys.executable, os.path.join(ROOT, "tools", "hlfmt.py")]
LINT = [sys.executable, os.path.join(ROOT, "tools", "hllint.py")]
HLC = os.path.join(ROOT, "bin", "hlc")
CC = os.environ.get("CC", "gcc")
NATIVE = os.path.isfile(HLC) and shutil.which(CC) is not None

SHOWCASE = os.path.join(ROOT, "tests", "ok", "feat_stage141_apifreeze.hls")
SHOWCASE_OUT = ("stage 141: api freeze\n"
                "  freeze laws OK: drift vocabulary, amendment, digest,\n"
                "  chain, count, method roster, contract, state machine\n")

# The scratch tree: exactly what the tool reads — the boot front-end,
# the tools (the tool itself + the chain arithmetic it imports), the
# stdlib families, the self-hosted keyword chain, and the freeze
# documents. Drift laws run against disposable copies of THIS.
SCRATCH_PARTS = ["boot", "tools", "std", "core",
                 os.path.join("src", "hlc", "types.hls"),
                 os.path.join("tests", "api_freeze.json"),
                 os.path.join("tests", "api_freeze_amendments.jsonl")]

GATE = tempfile.mkdtemp(prefix=".freeze-gate-", dir=ROOT)

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


def run(argv, cwd=None, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=600,
                          text=True, cwd=cwd, **kw)


def canon(obj) -> str:
    """The gate's own canonicaliser — written independently of the
    tool's, and required to agree with it."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def make_scratch(tag):
    """A disposable copy of the tree parts the tool reads."""
    root = os.path.join(GATE, "tree-" + tag)
    os.makedirs(root)
    for part in SCRATCH_PARTS:
        src = os.path.join(ROOT, part)
        dst = os.path.join(root, part)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    return root


def gate_expr_text(e):
    """The gate's own contract renderer — same shape, same
    fallback, written independently."""
    k = e.get("k")
    if k == "bin":
        return "(%s %s %s)" % (gate_expr_text(e["l"]), e["op"],
                               gate_expr_text(e["r"]))
    if k == "un":
        return "%s(%s)" % (e["op"], gate_expr_text(e["e"]))
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
                           ", ".join(gate_expr_text(a)
                                     for a in e["args"]))
    if k == "fieldcall":
        return "%s.%s(%s)" % (gate_expr_text(e["target"]), e["name"],
                              ", ".join(gate_expr_text(a)
                                        for a in e["args"]))
    if k == "field":
        return "%s.%s" % (gate_expr_text(e["target"]), e["name"])
    if k == "index":
        return "%s[%s]" % (gate_expr_text(e["target"]),
                           gate_expr_text(e["idx"]))
    if k == "qmark":
        return "%s?" % gate_expr_text(e["e"])
    strip = json.dumps(e, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, default=repr)
    return "ast:" + strip


def gate_fn_record(f):
    rec = {
        "extern": bool(f.get("extern")),
        "effects": sorted(f.get("effects") or []),
        "generic": list(f.get("typeparams") or []),
        "params": [[n, t, bool(m)] for (n, t, m) in f["params"]],
        "pure": bool(f.get("pure")),
        "ret": f["ret"],
    }
    if f.get("requires") is not None:
        rec["requires"] = gate_expr_text(f["requires"])
    if f.get("ensures") is not None:
        rec["ensures"] = gate_expr_text(f["ensures"])
    dep = (f.get("attrs") or {}).get("deprecated")
    if dep is True:
        rec["deprecated"] = ""
    elif isinstance(dep, (bytes, bytearray)):
        rec["deprecated"] = bytes(dep).decode("utf-8", "replace")
    elif isinstance(dep, str):
        rec["deprecated"] = dep
    if f.get("taint_source"):
        rec["taint_source"] = True
    if f.get("taint_sinks"):
        rec["taint_sinks"] = sorted(f["taint_sinks"])
    return rec


def gate_module_record(path):
    src = open(path, "rb").read()
    prog = Parser(tokenize(src), src).parse_program()
    fns = {}
    for key in sorted(prog["fns"]):
        fns[key] = gate_fn_record(prog["fns"][key])
    structs = {}
    for name in sorted(prog["structs"]):
        s = prog["structs"][name]
        structs[name] = {
            "generic": list(s.get("typeparams") or []),
            "fields": [[n, t, d is not None]
                       for (n, t, d) in s["fields"]],
        }
    enums = {}
    for name in sorted(prog["enums"]):
        e = prog["enums"][name]
        enums[name] = {
            "generic": list(e.get("typeparams") or []),
            "variants": [[n, list(p)] for (n, p) in e["variants"]],
        }
    return {"fns": fns, "structs": structs, "enums": enums,
            "imports": sorted(i["path"] for i in prog["imports"])}


def gate_stdlib_surface():
    out = {}
    for fam in ("std", "core"):
        base = os.path.join(ROOT, fam)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for fn in sorted(filenames):
                if not fn.endswith(".hls"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
                out[rel[:-4].replace("/", ".")] = gate_module_record(full)
    return out


def gate_census_diag():
    """The gate's own attribute census — a junk attribute through
    the real parser, the known-list captured from the refusal."""
    src = b"#[zz_gate_census]\nfn main() -> void { return }\n"
    try:
        prog = Parser(tokenize(src), src).parse_program()
        checker_check(prog)
        return None
    except HLError as e:
        return str(e)


def gate_probe(src_text):
    """One probe through the REAL checker. None = accepted."""
    src = src_text.encode("utf-8")
    try:
        prog = Parser(tokenize(src), src).parse_program()
        checker_check(prog)
        return None
    except HLError as e:
        return str(e)
    except Exception as ex:                                  # noqa: BLE001
        return "%s: %s" % (type(ex).__name__, ex)


# ===========================================================================
def _section_1():
    print("Section 1 — the convention")

    check(os.path.isfile(FREEZE), "the freeze document exists at "
          "tests/api_freeze.json")
    doc = json.loads(read(FREEZE))
    check(doc.get("surface_version") == 1, "the freeze is surface "
          "version 1")
    check(doc.get("tool") == "hls-freeze", "the freeze names its tool")
    check(doc.get("stage") == 141, "the freeze is Stage 141")
    check(doc.get("release") == "0.159.0-alpha", "the freeze stamps "
          "v0.159.0-alpha")
    check(re.fullmatch(r"[0-9a-f]{40}", doc.get("frozen_commit", ""))
          is not None, "the frozen commit is a 40-hex sha")

    # The self-consistency law, through the gate's own canonicaliser.
    want = sha(canon(doc["surface"]))
    check(doc.get("digest") == want, "the digest is the sha256 of the "
          "canonical surface it carries (gate's own canonicaliser)")

    # The chain: present, zero records (genesis), replaying clean.
    check(os.path.isfile(CHAIN), "the amendment chain exists beside "
          "the freeze")
    records = [json.loads(l) for l in read(CHAIN).splitlines()
               if l.strip()]
    check(records == [], "the chain is at genesis (no amendments yet)")

    # The tools answer on the real tree.
    p = run(TOOL + ["status"])
    check(p.returncode == 0 and "IDENTICAL" in p.stdout,
          "status answers IDENTICAL on the tree")
    p = run(TOOL + ["verify", "--quiet"])
    check(p.returncode == 0, "verify is green on the tree")

    # The freeze travels with the repository.
    p = run(["git", "ls-files", "tests/api_freeze.json",
             "tests/api_freeze_amendments.jsonl"])
    named = set(p.stdout.split())
    check(os.path.join("tests", "api_freeze.json") in named
          and os.path.join("tests", "api_freeze_amendments.jsonl")
          in named,
          "both freeze files are committed (git ls-files names them)")


# ===========================================================================
def _section_2():
    print("Section 2 — the syntax law")

    doc = json.loads(read(FREEZE))
    syn = doc["surface"]["syntax"]

    check(syn["keywords"] == sorted(KEYWORDS),
          "the frozen keywords are exactly the boot lexer's set")
    check(syn["reserved_identifiers"] == sorted(RESERVED_IDENTIFIERS),
          "the frozen reserved identifiers are exactly the boot set")
    check(syn["tokens_two_char"] == sorted(TWO_CHAR),
          "the frozen two-char tokens are exactly the boot set")
    check(syn["tokens_one_char"] == sorted(ONE_CHAR),
          "the frozen one-char tokens are exactly the boot set")
    check(syn["effects"] == sorted(KNOWN_EFFECTS),
          "the frozen effect vocabulary is exactly the parser's")
    check(syn["crate_attrs"] == sorted(CRATE_ATTRS),
          "the frozen crate attributes are exactly the parser's")
    check(syn["builtin_fns"] == sorted(BUILTIN_FNS),
          "the frozen builtin fns are exactly the checker's "
          "BUILTIN_FNS table")

    # The two-front-end agreement, grepped by the gate itself.
    text = read(os.path.join(ROOT, "src", "hlc", "types.hls"))
    m = re.search(r"fn is_keyword\(w: str\) -> bool \{(.*?)\n\}",
                  text, re.S)
    check(m is not None, "the self-hosted keyword chain is greppable")
    if m:
        selfhost = sorted(set(re.findall(r'==\s*"([a-z_]+)"',
                                         m.group(1))))
        check(selfhost == sorted(KEYWORDS),
              "the self-hosted chain equals the boot lexer's set")
        check(syn["keywords"] == selfhost,
              "the frozen keywords equal the self-hosted chain")

    # The attribute census, probed live by the gate.
    diag = gate_census_diag()
    check(diag is not None and "known:" in diag,
          "the junk-attribute probe is refused with the known-list")
    if diag and "known:" in diag:
        tail = diag.split("known:", 1)[1]
        cut = tail.find(" (line")
        if cut != -1:
            tail = tail[:cut]
        live_raw = tail.strip()
        if live_raw.endswith("))"):
            live_raw = live_raw[:-1]
        check(syn["fn_attr_census_raw"] == live_raw,
              "the frozen attribute census is the parser's live words")
        check(len(syn["fn_attrs"]) == 16 and "deprecated" in syn["fn_attrs"]
              and "taint_sink" in syn["fn_attrs"],
              "the frozen attribute vocabulary carries 16 names "
              "including deprecated and taint_sink")

    # The primitive method tables, imported and compared row for row.
    def tuple_tbl(tbl):
        return {n: {"params": list(s[0]), "ret": s[1]}
                for n, s in sorted(tbl.items())}

    def plain_tbl(tbl):
        return {n: {"params": [], "ret": r} for n, r in sorted(tbl.items())}

    bm = syn["builtin_methods"]
    check(bm.get("str") == tuple_tbl(STR_M), "the str method table is "
          "the checker's own")
    check(bm.get("int") == plain_tbl(INT_M), "the int method table is "
          "the checker's own")
    check(bm.get("float") == plain_tbl(FLOAT_M), "the float method "
          "table is the checker's own")
    check(bm.get("bool") == plain_tbl(BOOL_M), "the bool method table "
          "is the checker's own")

    # The container / concurrency method surface, DEMONSTRATED: the
    # gate's own probes through the real checker.
    probes = [
        ("fn main() -> void {\n"
         "    let xs: list[int] = [1, 2]\n"
         "    xs.push(3)\n"
         "    let _n: int = xs.len()\n"
         "    return\n}\n", "list.push", True),
        ("fn main() -> void {\n"
         "    let xs: list[int] = [1]\n"
         "    let _x: int = xs.get(0)\n"
         "    return\n}\n", "list.get", True),
        ("fn main() -> void uses Conc {\n"
         "    let ch: Chan[int] = chan_new()\n"
         "    let _x: int = ch.recv()\n"
         "    return\n}\n", "chan.recv", True),
        ("fn main() -> void uses Conc {\n"
         "    let ch: Chan[int] = chan_new()\n"
         "    let _b: bool = ch.try_send(1)\n"
         "    return\n}\n", "chan.try_send", True),
        ("fn __work() -> int { return 7 }\n"
         "fn main() -> void uses Conc {\n"
         "    let t: Task[int] = spawn(__work)\n"
         "    let _x: int = t.join()\n"
         "    return\n}\n", "task.join", True),
        ("fn main() -> void {\n"
         "    let m: map[str, int] = map_new()\n"
         "    let _x: int = m.get_or(\"k\", 1)\n"
         "    return\n}\n", "map.get_or", True),
        ("fn main() -> void {\n"
         "    let s: str = \"abc\"\n"
         "    let _n: int = s.byte_at(0)\n"
         "    return\n}\n", "str.byte_at", True),
        ("fn main() -> void {\n"
         "    let xs: list[int] = [1]\n"
         "    let _x: int = xs.zz_gate_junk(1)\n"
         "    return\n}\n", "list junk", False),
        ("fn main() -> void uses Conc {\n"
         "    let ch: Chan[int] = chan_new()\n"
         "    let _x: int = ch.zz_gate_junk()\n"
         "    return\n}\n", "chan junk", False),
    ]
    for src_text, label, expect_accept in probes:
        verdict = gate_probe(src_text)
        if expect_accept:
            check(verdict is None,
                  "the probe channel accepts %s (demonstrated, not "
                  "asserted)" % label)
        else:
            check(verdict is not None and "no method" in verdict,
                  "the probe channel refuses %s" % label)
        if expect_accept and label in ("list.push", "chan.recv",
                                       "task.join", "map.get_or",
                                       "str.byte_at"):
            kind, name = label.split(".")
            check(name in bm.get(kind, {}),
                  "the frozen roster carries %s for %s" % (name, kind))


# ===========================================================================
def _section_3():
    print("Section 3 — the census law")

    doc = json.loads(read(FREEZE))
    frozen_std = doc["surface"]["stdlib"]
    live_std = gate_stdlib_surface()

    check(canon(frozen_std) == canon(live_std),
          "the frozen stdlib census equals the gate's own "
          "re-derivation, canonically")

    fns = sum(len(m["fns"]) for m in frozen_std.values())
    structs = sum(len(m["structs"]) for m in frozen_std.values())
    enums = sum(len(m["enums"]) for m in frozen_std.values())
    edges = sum(len(m["imports"]) for m in frozen_std.values())
    check(len(frozen_std) == 98, "98 modules are frozen")
    check(fns == 3402, "3402 stdlib fns are frozen")
    check(structs == 185, "185 stdlib structs are frozen")
    check(enums == 36, "36 stdlib enums are frozen")
    check(edges == 211, "211 import edges are frozen")

    # Row-level spot checks: the freeze is signatures, and the gate
    # reads three of them straight from the parser's own output.
    hex_mod = frozen_std.get("std.hex", {})
    check("hex_byte" in hex_mod.get("fns", {}),
          "std.hex carries hex_byte in the freeze")
    if "hex_byte" in hex_mod.get("fns", {}):
        rec = hex_mod["fns"]["hex_byte"]
        check(rec["params"] == [["b", "int", False]]
              and rec["ret"] == "str",
              "hex_byte's frozen signature is (b: int) -> str")

    # A method-keyed fn (impl Struct.method) lands in the census.
    method_keys = [k for m in frozen_std.values() for k in m["fns"]
                   if "." in k]
    check(len(method_keys) > 0, "impl methods are frozen under their "
          "Struct.method keys (%d of them)" % len(method_keys))


# ===========================================================================
def _section_4():
    print("Section 4 — the removal law")

    root = make_scratch("removal")

    # (a) a deleted stdlib fn is REMOVED at its dotted path.
    hex_path = os.path.join(root, "std", "hex.hls")
    src = read(hex_path)
    i = src.find("fn hex_byte")
    j = src.find("\nfn ", i + 1)
    check(i > 0 and j > i, "the scratch edit finds hex_byte's span")
    write(hex_path, src[:i] + src[j:])
    p = run(TOOL + ["verify", "--root", root], cwd=root)
    check(p.returncode == 1 and "DRIFTED" in p.stdout,
          "a removed export is drift, exit 1")
    check("REMOVED" in p.stdout
          and "stdlib.std.hex.fns.hex_byte" in p.stdout,
          "the removal is named at its dotted path")
    write(hex_path, src)
    p = run(TOOL + ["verify", "--quiet", "--root", root], cwd=root)
    check(p.returncode == 0, "the revert is green again")

    # (b) a keyword removed from BOTH front-ends is syntax drift.
    boot_lexer = os.path.join(root, "boot", "lexer.py")
    types_hls = os.path.join(root, "src", "hlc", "types.hls")
    bl = read(boot_lexer)
    th = read(types_hls)
    write(boot_lexer, bl.replace('"where",', '"__moved__",', 1))
    p = run(TOOL + ["verify", "--root", root], cwd=root)
    check(p.returncode == 1 and "front-ends disagree" in p.stdout,
          "a one-front-end keyword removal is refused outright "
          "(the agreement law fires first)")
    write(boot_lexer, bl.replace('"__moved__",', '"where",', 1))
    # Now both: the boot table and the self-hosted chain.
    write(boot_lexer, bl.replace('"where",', '"__moved__",', 1))
    th2 = th.replace('|| w == "where"', '|| w == "__moved__"', 1)
    write(types_hls, th2)
    p = run(TOOL + ["verify", "--root", root], cwd=root)
    check(p.returncode == 1 and "DRIFTED" in p.stdout,
          "a keyword removed from both front-ends is drift")
    check("syntax.keywords" in p.stdout,
          "the keyword drift is named in the syntax section")
    write(boot_lexer, bl)
    write(types_hls, th)


# ===========================================================================
def _section_5():
    print("Section 5 — the mutation law")

    root = make_scratch("mutation")
    hex_path = os.path.join(root, "std", "hex.hls")
    src = read(hex_path)

    def expect_drift(tag, needle, restore=None):
        p = run(TOOL + ["verify", "--root", root], cwd=root)
        check(p.returncode == 1 and needle in p.stdout,
              "%s is named at its path" % tag)
        write(hex_path, restore if restore is not None else src)
        run(TOOL + ["verify", "--quiet", "--root", root], cwd=root)

    # (a) a parameter type change.
    write(hex_path, src.replace("fn hex_byte(b: int) -> str",
                                "fn hex_byte(b: str) -> str", 1))
    expect_drift("a parameter type change", "fns.hex_byte.params")

    # (b) a return type change.
    write(hex_path, src.replace("fn hex_byte(b: int) -> str",
                                "fn hex_byte(b: int) -> int", 1))
    expect_drift("a return type change", "fns.hex_byte.ret")

    # (c) an effect clause change (a pure fn grows uses IO — the
    # IO family expands into the effects row).
    i = src.find("fn hex_byte(b: int) -> str {")
    write(hex_path, src[:i] + "fn hex_byte(b: int) -> str uses IO {"
          + src[i + len("fn hex_byte(b: int) -> str {"):])
    expect_drift("an effect clause change", "fns.hex_byte.effects")

    # (d) a removed enum variant.
    opt_path = os.path.join(root, "core", "option.hls")
    opt = read(opt_path)
    m = re.search(r"enum Option[^{]*\{", opt)
    check(m is not None, "core.option carries the Option enum")
    if m:
        # the exact variant line: None carries no payload and closes
        # the enum body — remove its line, keep the trailing comma
        # grammar legal
        check("    None\n}" in opt, "the None variant's span is found")
        write(opt_path, opt.replace("    None\n}", "}", 1))
        p = run(TOOL + ["verify", "--root", root], cwd=root)
        check(p.returncode == 1
              and "stdlib.core.option.enums.Option.variants" in p.stdout,
              "a removed enum variant is named at its path")
        write(opt_path, opt)

    # (e) a removed struct field.
    io_path = os.path.join(root, "std", "io.hls")
    io_src = read(io_path)
    m2 = re.search(r"struct [A-Za-z_0-9]+[^{]*\{", io_src)
    check(m2 is not None, "std.io carries a struct")
    if m2:
        f_i = io_src.find(":", m2.end())
        f_name_start = io_src.rfind(" ", m2.end(), f_i)
        f_name = io_src[f_name_start + 1:f_i]
        line_end = io_src.find(",", f_i)
        if line_end == -1:
            line_end = io_src.find("}", f_i)
        write(io_path, io_src[:f_name_start + 1]
              + io_src[line_end + 1:])
        p = run(TOOL + ["verify", "--root", root], cwd=root)
        check(p.returncode == 1 and ".fields" in p.stdout,
              "a removed struct field is named at its path")
        write(io_path, io_src)

    # (f) a newly-deprecated fn: the record GAINS a key — named as
    # an addition at the record's path.
    src_hex = read(hex_path)
    i3 = src_hex.find("fn hex_byte")
    write(hex_path, src_hex[:i3] + '#[deprecated("use hex_encode")]\n'
          + src_hex[i3:])
    p = run(TOOL + ["verify", "--root", root], cwd=root)
    check(p.returncode == 1
          and "ADDED" in p.stdout and "fns.hex_byte.deprecated" in p.stdout,
          "a new deprecation is drift at the record's path")
    write(hex_path, src_hex)


# ===========================================================================
def _section_6():
    print("Section 6 — the addition law")

    root = make_scratch("addition")
    hex_path = os.path.join(root, "std", "hex.hls")
    src = read(hex_path)

    # (a) a new stdlib fn is ADDED drift — locked, not merely
    # backwards-compatible.
    write(hex_path, src + "\nfn hex_freeze_new(x: int) -> int "
          "{ return x + 1 }\n")
    p = run(TOOL + ["verify", "--root", root], cwd=root)
    check(p.returncode == 1 and "ADDED" in p.stdout
          and "stdlib.std.hex.fns.hex_freeze_new" in p.stdout,
          "a post-freeze addition is drift, named at its path")
    write(hex_path, src)

    # (b) a new builtin: the checker's own table grew, so the
    # syntax section moves — the freeze reads the live tables.
    helpers = os.path.join(root, "boot", "checking", "helpers.py")
    hsrc = read(helpers)
    write(helpers, hsrc.replace(
        '"print", "println", "panic", "exit", "str", "int", "len", '
        '"range",',
        '"print", "println", "panic", "exit", "str", "int", "len", '
        '"range", "zz_gate_new_builtin",', 1))
    p = run(TOOL + ["verify", "--root", root], cwd=root)
    check(p.returncode == 1 and "syntax.builtin_fns" in p.stdout,
          "a grown builtin table is syntax drift, named")
    write(helpers, hsrc)

    # (c) the contrast: a comment-only edit moves NO row.
    write(hex_path, "# a comment the freeze must not see\n" + src)
    p = run(TOOL + ["verify", "--quiet", "--root", root], cwd=root)
    check(p.returncode == 0,
          "a comment-only edit is NOT drift (the contract, not "
          "the bytes)")


# ===========================================================================
def _section_7():
    print("Section 7 — the amend law")

    root = make_scratch("amend")
    hex_path = os.path.join(root, "std", "hex.hls")
    src = read(hex_path)
    freeze_p = os.path.join(root, "tests", "api_freeze.json")
    chain_p = os.path.join(root, "tests", "api_freeze_amendments.jsonl")
    old_digest = json.loads(read(freeze_p))["digest"]

    # (a) the refusals, named.
    p = run(TOOL + ["amend", "--by", "tester", "--root", root],
            cwd=root)
    check(p.returncode == 2, "missing --reason is a usage error")
    p = run(TOOL + ["amend", "--reason", "   ", "--by", "tester",
                    "--root", root], cwd=root)
    check(p.returncode == 1 and "without a written reason" in p.stdout,
          "a whitespace-only reason is a named refusal")
    p = run(TOOL + ["amend", "--reason", "a real reason",
                    "--root", root], cwd=root)
    check(p.returncode == 2, "missing --by is a usage error")
    p = run(TOOL + ["amend", "--reason", "a real reason",
                    "--by", "tester", "--root", root], cwd=root)
    check(p.returncode == 1 and "nothing to amend" in p.stdout,
          "a no-op amendment is refused (noise is how real "
          "amendments stop being read)")

    # (b) the real amendment: the drift is absorbed, exactly one
    # record lands, the delta is the diff the tool computed.
    write(hex_path, src + "\nfn hex_freeze_new(x: int) -> int "
          "{ return x + 1 }\n")
    p = run(TOOL + ["amend", "--reason",
                    "std.hex grows a helper for the v0.160 formatter",
                    "--by", "mhieuhonda", "--root", root], cwd=root)
    check(p.returncode == 0 and "AMENDED" in p.stdout,
          "a real amendment succeeds")
    records = [json.loads(l) for l in read(chain_p).splitlines()
               if l.strip()]
    check(len(records) == 1, "exactly one record appended")
    rec = records[0]
    check(rec.get("seq") == 1, "the record is seq 1")
    check(rec.get("kind") == "amend", "the record is an amendment")
    check(rec.get("by") == "mhieuhonda", "the record names its author")
    check("v0.160 formatter" in rec.get("reason", ""),
          "the record carries the written reason")
    check(rec.get("delta", {}).get("added")
          == ["stdlib.std.hex.fns.hex_freeze_new"],
          "the recorded delta is the diff the tool computed")
    check(rec.get("from_digest") == old_digest,
          "the record's from_digest is the digest it amended away")

    # (c) the chain, re-derived through the family's arithmetic —
    # the gate computes chain_of itself, it does not trust the
    # tool's writer.
    check(rec.get("prev_hash") == GENESIS,
          "entry one chains from 64 zeros")
    want_hash = chain_of(rec["prev_hash"], rec)
    check(rec.get("chain_hash") == want_hash,
          "chain_hash recomputes through the imported chain_of")

    # (d) the document moved WITH the chain: the new surface carries
    # the added fn, the new digest is the record's to_digest, and
    # verify is green.
    new_doc = json.loads(read(freeze_p))
    check("hex_freeze_new"
          in new_doc["surface"]["stdlib"]["std.hex"]["fns"],
          "the amended document carries the added export")
    check(new_doc["digest"] == rec.get("to_digest"),
          "the new digest is the record's to_digest")
    check(sha(canon(new_doc["surface"])) == new_doc["digest"],
          "the amended document agrees with itself (gate's own "
          "canonicaliser)")
    p = run(TOOL + ["verify", "--quiet", "--root", root], cwd=root)
    check(p.returncode == 0, "verify is green after the amendment")

    # (e) a second amendment is refused — the drift is absorbed.
    p = run(TOOL + ["amend", "--reason", "another",
                    "--by", "tester", "--root", root], cwd=root)
    check(p.returncode == 1 and "nothing to amend" in p.stdout,
          "a second amendment with no drift is refused")

    # (f) a tampered record breaks the replay at its line.
    tampered = dict(rec)
    tampered["reason"] = "a different story"
    tampered["chain_hash"] = "0" * 64
    write(chain_p, canon(tampered) + "\n")
    p = run(TOOL + ["verify", "--root", root], cwd=root)
    check(p.returncode == 1 and "BROKEN" in p.stdout
          and "record 1" in p.stdout,
          "a tampered record breaks the replay at its line")
    write(chain_p, canon(rec) + "\n")
    p = run(TOOL + ["verify", "--quiet", "--root", root], cwd=root)
    check(p.returncode == 0, "the restored chain replays clean")


# ===========================================================================
def _section_8():
    print("Section 8 — the report + corpus showcase")

    root = make_scratch("report")
    out1 = os.path.join(GATE, "api_report_1.md")
    out2 = os.path.join(GATE, "api_report_2.md")
    p = run(TOOL + ["report", "--out", out1, "--root", root], cwd=root)
    check(p.returncode == 0, "the report renders from the freeze")
    if p.returncode != 0:
        print("    report said: " + p.stdout[:200])
    else:
        r1 = read(out1)
        doc = json.loads(read(os.path.join(
            root, "tests", "api_freeze.json")))
        check(doc["digest"] in r1, "the report embeds the digest")
        check("Stage 141" in r1 and "v0.159.0-alpha" in r1,
              "the report names the stage and the release")
        check("hex_byte" in r1 and "std.hex" in r1,
              "the report carries the stdlib census (std.hex row)")
        check("keywords" in r1 and "asm" in r1,
              "the report carries the syntax surface")
        p = run(TOOL + ["report", "--out", out2, "--root", root],
                cwd=root)
        check(p.returncode == 0 and read(out1) == read(out2),
              "two renders of one freeze are byte-identical")

    # The verify-first refusal: a drifted tree renders nothing.
    hex_path = os.path.join(root, "std", "hex.hls")
    src = read(hex_path)
    write(hex_path, src + "\nfn zz_drift(x: int) -> int { return x }\n")
    out3 = os.path.join(GATE, "api_report_3.md")
    p = run(TOOL + ["report", "--out", out3, "--root", root], cwd=root)
    check(p.returncode == 1 and not os.path.exists(out3),
          "a report is never rendered from a drifted tree, and "
          "writes no file")
    write(hex_path, src)

    # The corpus showcase.
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
