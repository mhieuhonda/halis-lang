#!/usr/bin/env python3
"""hls-migrate — the Stage 129 migration tool (`hls migrate`).

Usage:
  python3 tools/hls-migrate.py <path>...                    # PLAN: what would change
  python3 tools/hls-migrate.py <path>... --diff             # the plan as unified diffs
  python3 tools/hls-migrate.py <path>... --apply            # rewrite the files
  python3 tools/hls-migrate.py <path>... --apply --verify   # + boot --check per rewritten file
  python3 tools/hls-migrate.py <path>... --json report.json # machine-readable plan report
  python3 tools/hls-migrate.py --rules                      # the rule registry
  python3 tools/hls-migrate.py <path> --from v0.34.0-alpha --to v1.0   # the roadmap phrase

Stage 128's suite measures compatibility: it replays every prior
release's ok corpus against today's toolchain and names every
sanctioned drift in a ledger. Stage 129 is the other half of the same
bargain. A ledger tells you WHAT changed; a migration tool UPDATES
the source that the change left behind. `hls migrate` is a codemod
engine whose rules are derived from the project's real, documented
breaking changes — the same events the Stage 128 ledger records —
each carrying the release that introduced it ("since"):

  html-escape-slash   v0.8.0-alpha    recompute html_escape golden
                                      expectations to today's exact
                                      rendering (the forward-slash
                                      hardening onwards)
  rename-project      v0.20.0-alpha   "Hieu Louis" -> "Halis", the
                                      project rename, in strings and
                                      comments alike
  import-guard        v0.51.1-alpha   absolute import paths rewritten
                                      to file-relative form (AUTO);
                                      `..` segments reported as MANUAL
                                      — the deep-scan-19 guard exists
                                      because `..` is a local-file-
                                      inclusion vector, and a codemod
                                      that silently rewrote one would
                                      change what the program loads
  xxh64-spec          v0.82.0-alpha   pinned XXH64 digest expectations
                                      recomputed with the spec-correct
                                      algorithm (verified against
                                      libxxhash 0.8.3 vectors); fires
                                      only where the pinned value and
                                      the computed value disagree
  asm-width           v0.102.0-alpha  narrow GPR names in asm!
                                      constraints rewritten to the
                                      full 64-bit spelling (Stage 83:
                                      a named register binds exactly
                                      the register the source spells)
  hex64-word          v0.103.0-alpha  hex64 golden expectations
                                      re-rendered as the full 16-digit
                                      unsigned word (Stage 85)

THE WALK. A file is migrated by walking it through history: the rules
run oldest-first, each seeing the previous rule's output, so a file
crosses every release boundary in order — the same journey its
author's tree took, compressed into one pass. Every rule is
individually idempotent (a second run finds nothing to do), so the
pipeline is idempotent: `--apply` twice leaves the second run CLEAN.

THE GATE. `--from` names the dialect the source is written in (a
release tag; default v0.1.0-alpha) and `--to` the target (default:
this tool's version). A rule fires only when `since > --from` — code
written at or after a breaking change already conforms to it, and
re-firing the rule would be noise. `hls migrate v0.34 -> v1.0` is
therefore a real statement, not a slogan: it fires exactly the four
rules born after v0.34 and leaves the two pre-v0.34 dialects alone.

THE HONESTY RULES. A plan never touches a file. `--apply` writes a
`.bak` beside every file it rewrites (the FIRST backup wins — the
oldest original is the valuable one; `--no-backup` declines) and
reports every edit with file:line precision. `--verify` runs
`boot.py --check` on every file the tool rewrote and fails loudly on
the first error — but SKIPS files that still carry unresolved MANUAL
findings, named: the tool refuses to certify a file it knows is still
broken for a reason it could not fix.

Exit codes: 0 = CLEAN (a plan with zero findings) or APPLIED
(apply succeeded; verification green when requested); 1 = the plan
has findings, or --verify failed; 2 = usage (a missing or non-.hls
path, a malformed version, an unknown rule id, --from > --to).
"""

import argparse
import difflib
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOOL_VERSION = "0.147.0-alpha"

BOOT = os.path.join(REPO_ROOT, "boot", "boot.py")

# Release tags: v<major>.<minor>[.<patch>][-suffix] — the same grammar
# the Stage 128 census uses, so the two tools agree on what a version IS.
_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)(?:\.(\d+))?(?:[.-][A-Za-z0-9.\-]+)?$")

_U64 = (1 << 64) - 1

# --------------------------------------------------------------------------
# The rule registry, oldest first. `since` is the release whose behaviour
# the rule migrates TO; the gate fires the rule when --from is older.
# --------------------------------------------------------------------------
RULES = [
    {
        "id": "html-escape-slash",
        "since": "v0.8.0-alpha",
        "title": "html_escape golden expectations re-rendered",
        "desc": ("html_escape learned to escape the forward slash as "
                 "&#x2F; in v0.8.0-alpha (the \"</script>\" XSS vector). "
                 "A string literal compared (==/!=) against a direct "
                 "html_escape(<literal>) call is re-rendered to TODAY'S "
                 "exact output, so the golden pins the behaviour the "
                 "program will actually observe."),
    },
    {
        "id": "rename-project",
        "since": "v0.20.0-alpha",
        "title": "the project rename: Hieu Louis -> Halis",
        "desc": ("The v0.20.0-alpha rename changed data, comments and "
                 "string literals alike (two Stage 128 drift entries "
                 "record exactly this). Every occurrence of \"Hieu Louis\" "
                 "or \"Hieu-Louis\" becomes \"Halis\"."),
    },
    {
        "id": "import-guard",
        "since": "v0.51.1-alpha",
        "title": "the deep-scan-19 import-path guard",
        "desc": ("Absolute import paths are rewritten to file-relative "
                 "form (AUTO — with the reminder that the target file "
                 "must actually live at the new path). A path with a '..' "
                 "segment is reported MANUAL and never rewritten: the "
                 "guard exists because '..' is a local-file-inclusion "
                 "vector, and rewriting one would silently change what "
                 "the program loads. Move the file, import downward."),
    },
    {
        "id": "xxh64-spec",
        "since": "v0.82.0-alpha",
        "title": "XXH64 digests recomputed to the spec",
        "desc": ("The old std.hash xxHash64 was wrong for every input "
                 "class; v0.82.0-alpha shipped the spec-correct algorithm "
                 "(verified against libxxhash 0.8.3). A pinned digest "
                 "expectation on a LITERAL input is recomputed with the "
                 "spec algorithm and rewritten when it disagrees; "
                 "non-literal inputs are reported MANUAL."),
    },
    {
        "id": "asm-width",
        "since": "v0.102.0-alpha",
        "title": "narrow GPR names in asm! rewritten to 64-bit",
        "desc": ("Stage 83: a named register binds exactly the register "
                 "the source spells, so a 32-bit name on an int operand "
                 "was a silent miscompilation and is now a compile error. "
                 "Every 8/16/32-bit GPR spelling inside an asm! constraint "
                 "(in/out/inout/clobber) is rewritten to its full 64-bit "
                 "name — bool operands included, since Stage 83 carries a "
                 "bool through an int64 register variable. Width modifiers "
                 "in the template (%b0, %w1) are untouched."),
    },
    {
        "id": "hex64-word",
        "since": "v0.103.0-alpha",
        "title": "hex64 expectations re-rendered as the full word",
        "desc": ("Stage 85 renders a full 64-bit WORD — 16 lower-case "
                 "digits, unsigned two's complement — because boot "
                 "protocols carry values above 2^63 that arrive in an int "
                 "as negatives. A string literal compared against "
                 "hex64(...) is re-parsed (minimal-digit and negative "
                 "-0x forms both) and re-rendered in the Stage 85 form."),
    },
]

RULE_IDS = [r["id"] for r in RULES]
RULE_SINCE = {r["id"]: r["since"] for r in RULES}


def parse_version(v):
    """'v0.34.0-alpha' -> (0, 34, 0). None when the string is not a
    release tag (the error message names the grammar)."""
    m = _VERSION_RE.match(v)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


# --------------------------------------------------------------------------
# A spec-correct XXH64 (seed 0 default). Every constant and round is the
# algorithm libxxhash 0.8.3 implements; the vectors the acceptance pins
# are the same five tests/ok/feat_stage43_hash.hls has pinned since the
# v0.82.0-alpha fix.
# --------------------------------------------------------------------------
_P1 = 0x9E3779B185EBCA87
_P2 = 0xC2B2AE3D27D4EB4F
_P3 = 0x165667B19E3779F9
_P4 = 0x85EBCA77C2B2AE63
_P5 = 0x27D4EB2F165667C5


def _rotl(x, r):
    return ((x << r) | (x >> (64 - r))) & _U64


def _round(acc, val):
    acc = (acc + (val * _P2 & _U64)) & _U64
    acc = _rotl(acc, 31)
    return (acc * _P1) & _U64


def _merge_round(acc, val):
    val = _rotl((val * _P2) & _U64, 31)
    val = (val * _P1) & _U64
    acc = (acc ^ val) & _U64
    return (acc * _P1 + _P4) & _U64


def xxh64(data, seed=0):
    """data: bytes -> int (unsigned 64-bit). Spec XXH64."""
    n = len(data)
    i = 0
    if n >= 32:
        v1 = (seed + _P1 + _P2) & _U64
        v2 = (seed + _P2) & _U64
        v3 = seed & _U64
        v4 = (seed - _P1) & _U64
        while i + 32 <= n:
            v1 = _round(v1, int.from_bytes(data[i:i + 8], "little"))
            v2 = _round(v2, int.from_bytes(data[i + 8:i + 16], "little"))
            v3 = _round(v3, int.from_bytes(data[i + 16:i + 24], "little"))
            v4 = _round(v4, int.from_bytes(data[i + 24:i + 32], "little"))
            i += 32
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & _U64
        for v in (v1, v2, v3, v4):
            h = _merge_round(h, v)
    else:
        h = (seed + _P5) & _U64
    h = (h + n) & _U64
    while i + 8 <= n:
        h = (h ^ _round(0, int.from_bytes(data[i:i + 8], "little"))) & _U64
        h = (_rotl(h, 27) * _P1 + _P4) & _U64
        i += 8
    if i + 4 <= n:
        h = (h ^ ((int.from_bytes(data[i:i + 4], "little") * _P1) & _U64)) & _U64
        h = (_rotl(h, 23) * _P2 + _P3) & _U64
        i += 4
    while i < n:
        h = (h ^ ((data[i] * _P5) & _U64)) & _U64
        h = (_rotl(h, 11) * _P1) & _U64
        i += 1
    h ^= h >> 33
    h = (h * _P2) & _U64
    h ^= h >> 29
    h = (h * _P3) & _U64
    h ^= h >> 32
    return h


def to_signed64(u):
    """unsigned 64-bit -> the signed int64 a Halis `int` compares as."""
    return u - (1 << 64) if u >= (1 << 63) else u


# --------------------------------------------------------------------------
# Halis string-literal helpers: the lexer's escape set is exactly
# \n \t \\ \" — anything else is an invalid escape, so a literal the
# decoder cannot handle is skipped (never guessed at).
# --------------------------------------------------------------------------
_STR_RE = r'"(?:[^"\\]|\\.)*"'
_HEXDIGITS_RE = re.compile(r"^-?0[xX][0-9a-fA-F]+$")

_DECODE_ESCAPES = {"n": 10, "t": 9, "\\": 92, '"': 34}


def decode_str_literal(lit):
    """'a\\n b' -> bytes, or None when the literal carries an escape the
    lexer itself would reject (or malformed quoting)."""
    if len(lit) < 2 or not (lit.startswith('"') and lit.endswith('"')):
        return None
    body = lit[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            if i + 1 >= len(body):
                return None
            e = body[i + 1]
            if e not in _DECODE_ESCAPES:
                return None
            out.append(_DECODE_ESCAPES[e])
            i += 2
            continue
        if ord(c) < 32:
            return None
        out.append(ord(c) & 0xFF)
        i += 1
    return bytes(out)


def encode_str_literal(data):
    """bytes -> a Halis literal, escaping exactly the lexer's set."""
    out = ['"']
    for b in data:
        c = chr(b)
        if b == 10:
            out.append("\\n")
        elif b == 9:
            out.append("\\t")
        elif b == 92:
            out.append("\\\\")
        elif b == 34:
            out.append('\\"')
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def html_escape_today(data):
    """bytes -> bytes: what std.html's html_escape renders today
    (& < > " ' /, byte-wise — see std/html.hls)."""
    table = {38: b"&amp;", 60: b"&lt;", 62: b"&gt;", 34: b"&quot;",
             39: b"&#39;", 47: b"&#x2F;"}
    out = bytearray()
    for b in data:
        out.extend(table.get(b, bytes([b])))
    return bytes(out)


# The Stage 83 register facts: a narrow GPR spelling binds a register
# the operand cannot occupy (an int operand occupies all 64 bits; a
# bool is carried through an int64 register variable), so every narrow
# spelling migrates to its 64-bit name.
_REG64 = ["rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp"] + \
         ["r%d" % n for n in range(8, 16)]
_REG32 = ["eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp"] + \
         ["r%dd" % n for n in range(8, 16)]
_REG16 = ["ax", "bx", "cx", "dx", "si", "di", "bp", "sp"] + \
         ["r%dw" % n for n in range(8, 16)]
_REG8H = ["ah", "bh", "ch", "dh"]
_REG8L = ["al", "bl", "cl", "dl", "sil", "dil", "bpl", "spl"] + \
         ["r%db" % n for n in range(8, 16)]

_NARROW_TO_64 = {}
for _fam, _base in ((_REG32, 0), (_REG16, 0), (_REG8H, 0), (_REG8L, 0)):
    for _n in _fam:
        _NARROW_TO_64[_n] = None  # filled below
for _i, _n in enumerate(_REG32):
    _NARROW_TO_64[_n] = _REG64[_i]
for _i, _n in enumerate(_REG16):
    _NARROW_TO_64[_n] = _REG64[_i]
for _i, _n in enumerate(_REG8H):
    _NARROW_TO_64[_n] = _REG64[_i]
for _i, _n in enumerate(_REG8L):
    _NARROW_TO_64[_n] = _REG64[_i]


def hex64_render(v):
    """int -> the Stage 85 rendering: '0x' + 16 lower-case digits of the
    unsigned two's-complement word (core/section.hls:hex64)."""
    return "0x" + format(v & _U64, "016x")
import bisect


# --------------------------------------------------------------------------
# Line/column arithmetic + the edit record. An edit is a dict:
#   {rule, start, end, new, kind, note}  — [start, end) in the CURRENT
# text; new == None for a MANUAL finding (advice, never a rewrite).
# --------------------------------------------------------------------------

def _line_starts(text):
    starts = [0]
    for i, c in enumerate(text):
        if c == "\n":
            starts.append(i + 1)
    return starts


def _line_col(starts, off):
    ln = bisect.bisect_right(starts, off)
    return ln, off - starts[ln - 1] + 1


class Migrator:
    """One file's migration walk: rules oldest-first, each seeing the
    previous rule's output."""

    def __init__(self, path, text, only=None, skip=None):
        self.path = path
        self.text = text
        self.only = only
        self.skip = skip or set()
        self.findings = []
        self.changed = False
        self.rules_fired = []

    def gated(self, rule_id, from_v):
        """The gate: a rule fires when the source's dialect (--from)
        predates the rule's breaking change (since > --from)."""
        if self.only is not None and rule_id not in self.only:
            return False
        if rule_id in self.skip:
            return False
        since = parse_version(RULE_SINCE[rule_id])
        return from_v < since

    def run(self, from_v):
        for rule in RULES:
            rid = rule["id"]
            if not self.gated(rid, from_v):
                continue
            edits = RULE_SCAN[rid](self.text)
            if not edits:
                continue
            self.rules_fired.append(rid)
            starts = _line_starts(self.text)
            # Apply bottom-up so offsets stay valid; AUTO edits rewrite,
            # MANUAL edits only record.
            edits.sort(key=lambda e: (e["start"], e["end"]))
            for e in reversed(edits):
                ln, col = _line_col(starts, e["start"])
                old = self.text[e["start"]:e["end"]]
                rec = {"rule": rid, "line": ln, "col": col,
                       "kind": e["kind"], "old": old, "new": e["new"],
                       "note": e["note"]}
                self.findings.append(rec)
                if e["kind"] == "auto":
                    self.text = self.text[:e["start"]] + e["new"] + \
                        self.text[e["end"]:]
                    self.changed = True
        self.findings.sort(key=lambda f: (f["line"], f["col"]))
        return self

    def auto_count(self):
        return sum(1 for f in self.findings if f["kind"] == "auto")

    def manual_count(self):
        return sum(1 for f in self.findings if f["kind"] == "manual")

    def manual_rules(self):
        return sorted({f["rule"] for f in self.findings
                       if f["kind"] == "manual"})


# --------------------------------------------------------------------------
# Rule: html-escape-slash. A golden literal compared against a direct
# html_escape(<literal>) call is re-rendered to today's exact output.
# Both operand orders; the INPUT literal is never touched.
# --------------------------------------------------------------------------
_HTML_CALL_RE = re.compile(
    r'html_escape\s*\(\s*("(?:[^"\\]|\\.)*")\s*\)\s*(==|!=)\s*("(?:[^"\\]|\\.)*")')
_HTML_CALL_REV_RE = re.compile(
    r'("(?:[^"\\]|\\.)*")\s*(==|!=)\s*html_escape\s*\(\s*("(?:[^"\\]|\\.)*")\s*\)')


def _scan_html_escape(text):
    edits = []
    for m in _HTML_CALL_RE.finditer(text):
        e = _html_edit(text, m.start(3), m.group(3), m.group(1),
                       "html-escape-slash")
        if e:
            edits.append(e)
    for m in _HTML_CALL_REV_RE.finditer(text):
        e = _html_edit(text, m.start(1), m.group(1), m.group(3),
                       "html-escape-slash")
        if e:
            edits.append(e)
    return edits


def _html_edit(text, exp_start, exp_lit, inp_lit, rid):
    inp = decode_str_literal(inp_lit)
    cur = decode_str_literal(exp_lit)
    if inp is None or cur is None:
        # Not decodable (a lexer-invalid escape): report, never guess.
        return {"rule": rid, "start": exp_start,
                "end": exp_start + len(exp_lit), "new": None,
                "kind": "manual",
                "note": "golden expectation not decodable as a Halis "
                        "string literal; recompute html_escape output "
                        "by hand"}
    want = encode_str_literal(html_escape_today(inp))
    if want == exp_lit:
        return []  # the golden already pins today's output
    return {"rule": rid, "start": exp_start,
            "end": exp_start + len(exp_lit), "new": want,
            "kind": "auto",
            "note": "html_escape output changed (forward-slash "
                    "escaping, v0.8.0-alpha); golden re-rendered"}


# --------------------------------------------------------------------------
# Rule: rename-project. Every "Hieu Louis"/"Hieu-Louis" -> "Halis",
# comments and strings alike (the v0.20.0-alpha rename was total).
# --------------------------------------------------------------------------
_RENAME_RE = re.compile(r"Hieu[ -]Louis")


def _scan_rename(text):
    edits = []
    for m in _RENAME_RE.finditer(text):
        edits.append({"rule": "rename-project", "start": m.start(),
                      "end": m.end(), "new": "Halis", "kind": "auto",
                      "note": "the project renamed to Halis "
                              "(v0.20.0-alpha)"})
    return edits


# --------------------------------------------------------------------------
# Rule: import-guard. `import "..."` lines only.
#   starts with /  -> AUTO: file-relative ("./..." — the target must
#                     actually live beside the importer)
#   has a .. seg   -> MANUAL: the guard's whole point; move the file
# --------------------------------------------------------------------------
_IMPORT_RE = re.compile(r'^(\s*import\s+)("([^"]*)")', re.M)


def _scan_import(text):
    edits = []
    for m in _IMPORT_RE.finditer(text):
        lit, path = m.group(2), m.group(3)
        if path.startswith("/") or path.startswith("\\"):
            new_lit = '"./' + path.lstrip("/\\") + '"'
            new_parts = ("./" + path.lstrip("/\\")).replace("\\", "/").split("/")
            if ".." in new_parts:
                # The absolute form smuggled a '..' inside: rewriting it
                # would still be rejected, and honestly rewriting it
                # means changing what the program loads — MANUAL.
                edits.append({"rule": "import-guard",
                              "start": m.start(2),
                              "end": m.start(2) + len(lit),
                              "new": None, "kind": "manual",
                              "note": "absolute import path carrying a "
                                      "'..' segment — rejected by the "
                                      "deep-scan-19 guard; move the "
                                      "module below the importer and "
                                      "import downward"})
                continue
            edits.append({"rule": "import-guard",
                          "start": m.start(2),
                          "end": m.start(2) + len(lit),
                          "new": new_lit, "kind": "auto",
                          "note": "absolute import paths are rejected "
                                  "(deep-scan-19); rewritten relative to "
                                  "the importing file — the target must "
                                  "live at the new path"})
            continue
        parts = path.replace("\\", "/").split("/")
        if ".." in parts:
            edits.append({"rule": "import-guard",
                          "start": m.start(2),
                          "end": m.start(2) + len(lit),
                          "new": None, "kind": "manual",
                          "note": "import path contains a '..' segment, "
                                  "rejected by the deep-scan-19 "
                                  "local-file-inclusion guard; move the "
                                  "module below the importer and import "
                                  "downward (never auto-rewritten)"})
            continue
        for prefix in ("std.", "core."):
            if path.startswith(prefix):
                mod = path[len(prefix):]
                mparts = mod.replace("\\", "/").split("/")
                if mod.startswith("/") or mod.startswith("\\"):
                    new_path = prefix + mod.lstrip("/\\")
                    edits.append({"rule": "import-guard",
                                  "start": m.start(2),
                                  "end": m.start(2) + len(lit),
                                  "new": '"' + new_path + '"',
                                  "kind": "auto",
                                  "note": "module path must be relative "
                                          "after '%s' (deep-scan-19)" %
                                          prefix})
                elif ".." in mparts:
                    edits.append({"rule": "import-guard",
                                  "start": m.start(2),
                                  "end": m.start(2) + len(lit),
                                  "new": None, "kind": "manual",
                                  "note": "module path contains a '..' "
                                          "segment, rejected by the "
                                          "deep-scan-19 guard"})
                break
    return edits


# --------------------------------------------------------------------------
# Rule: xxh64-spec. Pinned digest expectations on literal inputs are
# recomputed with the spec algorithm; a disagreement is rewritten.
# --------------------------------------------------------------------------
_XXH_RE = re.compile(
    r'(xxhash_str(?:_seeded)?)\s*\(\s*("(?:[^"\\]|\\.)*")\s*(?:,\s*(-?\d+)\s*)?\)'
    r'\s*(==|!=)\s*(-?\d+)')
_XXH_REV_RE = re.compile(
    r'(-?\d+)\s*(==|!=)\s*(xxhash_str(?:_seeded)?)\s*\(\s*("(?:[^"\\]|\\.)*")\s*(?:,\s*(-?\d+)\s*)?\)')


def _xxh_digest(inp_lit, seed_tok, fname):
    inp = decode_str_literal(inp_lit)
    if inp is None:
        return None, None
    if fname == "xxhash_str_seeded" and seed_tok is None:
        return None, None
    if fname == "xxhash_str" and seed_tok is not None:
        return None, None
    seed = int(seed_tok) if seed_tok is not None else 0
    return inp, to_signed64(xxh64(inp, seed))


def _scan_xxh64(text):
    edits = []
    for m in _XXH_RE.finditer(text):
        fname, inp_lit, seed_tok, expect = m.group(1), m.group(2), \
            m.group(3), m.group(5)
        edits.extend(_xxh_edit(text, m.start(5), expect, inp_lit,
                               seed_tok, fname))
    for m in _XXH_REV_RE.finditer(text):
        expect, fname, inp_lit, seed_tok = m.group(1), m.group(3), \
            m.group(4), m.group(5)
        edits.extend(_xxh_edit(text, m.start(1), expect, inp_lit,
                               seed_tok, fname))
    return [e for e in edits if e is not None]


def _xxh_edit(text, exp_start, expect, inp_lit, seed_tok, fname):
    inp, want = _xxh_digest(inp_lit, seed_tok, fname)
    if want is None:
        if decode_str_literal(inp_lit) is None:
            return [{"rule": "xxh64-spec", "start": exp_start,
                     "end": exp_start + len(expect), "new": None,
                     "kind": "manual",
                     "note": "digest input is not a decodable literal; "
                             "recompute by hand"}]
        return []
    if int(expect) == want:
        return []
    return [{"rule": "xxh64-spec", "start": exp_start,
             "end": exp_start + len(expect), "new": str(want),
             "kind": "auto",
             "note": "pinned XXH64 digest disagrees with the spec "
                     "algorithm (v0.82.0-alpha, libxxhash 0.8.3 "
                     "vectors); recomputed"}]


# --------------------------------------------------------------------------
# Rule: asm-width. Inside asm!( ... ) spans only (balanced-paren scan,
# strings and comments skipped), every narrow GPR spelling in an
# in/out/inout/clobber constraint becomes its 64-bit name.
# --------------------------------------------------------------------------
_ASM_CONSTRAINT_RE = re.compile(
    r'\b(in|out|inout|clobber)\s*\(\s*"([A-Za-z0-9]+)"\s*\)')


def _asm_spans(text):
    """The [start, end) of every asm!( ... ) argument list — the opening
    paren's offset and its matching close, strings/comments respected."""
    spans = []
    for m in re.finditer(r"asm!\s*\(", text):
        i = m.end()          # first char after '('
        depth = 1
        n = len(text)
        while i < n and depth > 0:
            c = text[i]
            if c == '"':
                i += 1
                while i < n:
                    if text[i] == "\\":
                        i += 2
                        continue
                    if text[i] == '"':
                        break
                    i += 1
            elif c == "#":
                while i < n and text[i] != "\n":
                    i += 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    spans.append((m.end() - 1, i + 1))
            i += 1
    return spans


def _scan_asm_width(text):
    edits = []
    for lo, hi in _asm_spans(text):
        span = text[lo:hi]
        for m in _ASM_CONSTRAINT_RE.finditer(span):
            name = m.group(2)
            wide = _NARROW_TO_64.get(name)
            if wide is None:
                continue
            edits.append({"rule": "asm-width",
                          "start": lo + m.start(2),
                          "end": lo + m.end(2),
                          "new": wide, "kind": "auto",
                          "note": "'%s' binds a %d-bit register; Stage 83 "
                                  "rewrites narrow names to the full "
                                  "64-bit spelling ('%s')" %
                                  (name, 8 if name in _REG8H or name in
                                   _REG8L else 16 if name in _REG16 else 32,
                                   wide)})
    return edits


# --------------------------------------------------------------------------
# Rule: hex64-word. String literals compared against hex64(...) calls,
# re-rendered as the Stage 85 full word.
# --------------------------------------------------------------------------
_PAREN1 = r'\((?:[^()]|\([^()]*\))*\)'
_HEX_CALL_RE = re.compile(
    r'hex64\s*' + _PAREN1 + r'\s*(==|!=)\s*("(?:[^"\\]|\\.)*")')
_HEX_CALL_REV_RE = re.compile(
    r'("(?:[^"\\]|\\.)*")\s*(==|!=)\s*hex64\s*' + _PAREN1)


def _scan_hex64(text):
    edits = []
    for m in _HEX_CALL_RE.finditer(text):
        edits.extend(_hex_edit(m.group(2), m.start(2), "hex64-word"))
    for m in _HEX_CALL_REV_RE.finditer(text):
        edits.extend(_hex_edit(m.group(1), m.start(1), "hex64-word"))
    return [e for e in edits if e is not None]


def _hex_edit(lit, lit_start, rid):
    s = lit[1:-1]
    if not _HEXDIGITS_RE.match(s):
        return []
    neg = s.startswith("-")
    body = s[3:] if neg else s[2:]
    if not body:
        return []
    val = int(body, 16)
    if neg:
        val = -val
    want = hex64_render(val)
    if want == s:
        return []
    return [{"rule": rid, "start": lit_start,
             "end": lit_start + len(lit), "new": '"' + want + '"',
             "kind": "auto",
             "note": "hex64 renders the full 16-digit unsigned word "
                     "(Stage 85); '%s' -> '%s'" % (s, want)}]


RULE_SCAN = {
    "html-escape-slash": _scan_html_escape,
    "rename-project": _scan_rename,
    "import-guard": _scan_import,
    "xxh64-spec": _scan_xxh64,
    "asm-width": _scan_asm_width,
    "hex64-word": _scan_hex64,
}


# --------------------------------------------------------------------------
# File discovery, apply, verify.
# --------------------------------------------------------------------------

def collect_hls_files(paths):
    """Files to migrate: a path may be a single .hls file or a directory
    (walked recursively; hidden dirs and *.bak skipped; results sorted
    for a deterministic report)."""
    files = []
    for p in paths:
        if os.path.isfile(p):
            if not p.endswith(".hls"):
                raise Usage("not a .hls file: %s" % p)
            files.append(os.path.abspath(p))
        elif os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(names):
                    if name.endswith(".hls") and not name.endswith(".bak"):
                        files.append(os.path.abspath(os.path.join(root, name)))
        else:
            raise Usage("no such file or directory: %s" % p)
    return files


def apply_edits(text, edits):
    """Apply the AUTO edits of one rule to the text (bottom-up; the
    scanner produced them non-overlapping)."""
    out = text
    for e in sorted((e for e in edits if e["kind"] == "auto"),
                    key=lambda e: e["start"], reverse=True):
        out = out[:e["start"]] + e["new"] + out[e["end"]:]
    return out


def boot_check(path):
    """boot.py --check on a migrated file: (ok, first_error_line)."""
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, BOOT, "--check", path],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=120)
    except subprocess.TimeoutExpired:
        return False, "boot --check timed out after 120s"
    if proc.returncode == 0:
        return True, ""
    lines = (proc.stdout.decode("utf-8", "replace") or "").strip().splitlines()
    first = lines[0].strip() if lines else "exit %d" % proc.returncode
    return False, first


def make_diff(path, before, after):
    body = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                fromfile="%s (before)" % path,
                                tofile="%s (after)" % path,
                                lineterm="", n=2)
    return list(body)


# --------------------------------------------------------------------------
# Reporting.
# --------------------------------------------------------------------------

def finding_line(f, path):
    tag = "AUTO" if f["kind"] == "auto" else "MANUAL"
    arrow = ""
    if f["kind"] == "auto":
        shown_new = f["new"]
        if len(shown_new) > 60:
            shown_new = shown_new[:57] + "..."
        arrow = "  %r -> %r" % (f["old"], shown_new) if len(f["old"]) <= 40 \
            else ""
    return "%s:%d:%d: [%s] %s: %s%s" % (
        path, f["line"], f["col"], f["rule"], tag, f["note"], arrow)


def report_json(files_rec, from_v, to_v, only, skip):
    gated = [r for r in RULE_IDS
             if (only is None or r in only) and r not in skip
             and from_v < parse_version(RULE_SINCE[r])]
    rec = {
        "tool": "hls-migrate",
        "version": TOOL_VERSION,
        "from": "v%d.%d.%d" % from_v,
        "to": "v%d.%d.%d" % to_v,
        "rules_registered": [r["id"] for r in RULES],
        "rules_selected": gated,
        "files": files_rec,
        "summary": {
            "files_scanned": len(files_rec),
            "files_changed": sum(1 for f in files_rec if f["changed"]),
            "findings_auto": sum(f["auto"] for f in files_rec),
            "findings_manual": sum(f["manual"] for f in files_rec),
        },
    }
    return rec


def print_human(files_rec):
    for fr in files_rec:
        for f in fr["findings"]:
            print(finding_line(f, fr["path"]))
    n_files = sum(1 for fr in files_rec if fr["findings"])
    auto = sum(fr["auto"] for fr in files_rec)
    manual = sum(fr["manual"] for fr in files_rec)
    total = auto + manual
    if total == 0:
        print("hls-migrate: CLEAN — %d files, nothing to migrate" %
              len(files_rec))
    else:
        print("hls-migrate: %d finding%s across %d file%s "
              "(%d auto, %d manual)"
              % (total, "" if total == 1 else "s", n_files,
                 "" if n_files == 1 else "s", auto, manual))


def print_rules():
    print("hls-migrate %s — %d rules, oldest first (a rule fires when "
          "the source dialect --from predates its breaking change):"
          % (TOOL_VERSION, len(RULES)))
    for r in RULES:
        print("  %-18s since %-14s %s" % (r["id"], r["since"], r["title"]))
        print("      %s" % r["desc"])


# --------------------------------------------------------------------------
# The CLI.
# --------------------------------------------------------------------------

class Usage(Exception):
    pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hls-migrate",
        description="Stage 129 migration tool: upgrade Halis sources "
                    "through the language's real, documented breaking "
                    "changes (hls migrate v0.34 -> v1.0).")
    ap.add_argument("paths", nargs="*", metavar="PATH",
                    help=".hls files or directories to migrate")
    ap.add_argument("--from", dest="from_ver", default="v0.1.0-alpha",
                    metavar="VER", help="the dialect the source is "
                    "written in (a release tag; default v0.1.0-alpha)")
    ap.add_argument("--to", dest="to_ver", default="v" + TOOL_VERSION,
                    metavar="VER", help="the target release (default: "
                    "this tool's version)")
    ap.add_argument("--apply", action="store_true",
                    help="write the rewrites (a .bak beside every file "
                    "rewritten, unless --no-backup)")
    ap.add_argument("--no-backup", action="store_true",
                    help="decline the .bak backups")
    ap.add_argument("--verify", action="store_true",
                    help="after --apply, run boot.py --check on every "
                    "rewritten file (files with unresolved MANUAL "
                    "findings are skipped, named)")
    ap.add_argument("--diff", action="store_true",
                    help="print the plan as unified diffs")
    ap.add_argument("--json", dest="json_path", nargs="?", const="-",
                    default=None, metavar="FILE",
                    help="machine-readable plan report (default stdout "
                    "when FILE omitted)")
    ap.add_argument("--rules", action="store_true",
                    help="list the rule registry and exit")
    ap.add_argument("--only", default=None, metavar="ID,ID",
                    help="restrict to these rule ids")
    ap.add_argument("--skip", default="", metavar="ID,ID",
                    help="exclude these rule ids")
    ap.add_argument("--version", action="store_true",
                    help="print the tool version and exit")
    args = ap.parse_args(argv)

    if args.version:
        print("hls-migrate %s (Stage 129)" % TOOL_VERSION)
        return 0
    if args.rules:
        print_rules()
        return 0

    from_v = parse_version(args.from_ver)
    if from_v is None:
        sys.stderr.write(
            "error: --from must be a release tag like v0.34.0-alpha, "
            "got: %s\n" % args.from_ver)
        return 2
    to_v = parse_version(args.to_ver)
    if to_v is None:
        sys.stderr.write(
            "error: --to must be a release tag like v1.0, got: %s\n"
            % args.to_ver)
        return 2
    if from_v > to_v:
        sys.stderr.write("error: --from (%s) is newer than --to (%s); "
                         "migration walks forward\n"
                         % (args.from_ver, args.to_ver))
        return 2
    only = None
    if args.only is not None:
        only = [x for x in args.only.split(",") if x]
        for rid in only:
            if rid not in RULE_IDS:
                sys.stderr.write("error: unknown rule id: %s (--rules "
                                 "lists the registry)\n" % rid)
                return 2
    skip = set(x for x in args.skip.split(",") if x)
    for rid in skip:
        if rid not in RULE_IDS:
            sys.stderr.write("error: unknown rule id: %s\n" % rid)
            return 2
    if not args.paths:
        sys.stderr.write("error: no PATH given (a .hls file or a "
                         "directory)\n")
        return 2

    try:
        files = collect_hls_files(args.paths)
    except Usage as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    if not files:
        sys.stderr.write("error: no .hls files under the given path(s)\n")
        return 2

    # The walk: every file crosses every gated release boundary, oldest
    # first; each rule sees the previous rule's output.
    files_rec = []
    for path in files:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            original = fh.read()
        mig = Migrator(path, original, only=only, skip=skip).run(from_v)
        files_rec.append({
            "path": os.path.relpath(path, REPO_ROOT),
            "abs_path": path,
            "changed": mig.changed,
            "before": original,
            "after": mig.text,
            "findings": mig.findings,
            "rules_fired": mig.rules_fired,
            "auto": mig.auto_count(),
            "manual": mig.manual_count(),
            "manual_rules": mig.manual_rules(),
        })

    if args.json_path is not None:
        rec = report_json(
            [{k: v for k, v in fr.items()
              if k in ("path", "changed", "findings", "rules_fired",
                       "auto", "manual")} for fr in files_rec],
            from_v, to_v, only, skip)
        text = json.dumps(rec, indent=2)
        if args.json_path == "-":
            print(text)
        else:
            with open(args.json_path, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        return 1 if rec["summary"]["findings_auto"] + \
            rec["summary"]["findings_manual"] > 0 else 0

    if args.diff:
        any_diff = False
        for fr in files_rec:
            if fr["before"] != fr["after"]:
                any_diff = True
                for line in make_diff(fr["path"], fr["before"], fr["after"]):
                    print(line)
        return 1 if any_diff else 0

    if not args.apply:
        print_human(files_rec)
        total = sum(fr["auto"] + fr["manual"] for fr in files_rec)
        return 1 if total > 0 else 0

    # --apply: rewrite, back up, verify.
    print_human(files_rec)  # the findings, so the MANUAL pointers read
    rewritten = []
    for fr in files_rec:
        if not fr["changed"]:
            continue
        if not args.no_backup:
            bak = fr["abs_path"] + ".bak"
            if not os.path.exists(bak):  # the FIRST backup wins
                with open(bak, "w", encoding="utf-8", newline="") as fh:
                    fh.write(fr["before"])
        with open(fr["abs_path"], "w", encoding="utf-8", newline="") as fh:
            fh.write(fr["after"])
        rewritten.append(fr)

    print("hls-migrate: applied %d edit%s across %d file%s"
          % (sum(fr["auto"] for fr in files_rec),
             "" if sum(fr["auto"] for fr in files_rec) == 1 else "s",
             len(rewritten), "" if len(rewritten) == 1 else "s"))
    for fr in files_rec:
        if fr["manual"]:
            print("hls-migrate: MANUAL work remains in %s (%s) — see the "
                  "findings above or --json" %
                  (fr["path"], ", ".join(
                      "line %d" % f["line"]
                      for f in fr["findings"] if f["kind"] == "manual")))

    if args.verify:
        failures = 0
        for fr in rewritten:
            if fr["manual"]:
                print("hls-migrate: verify skipped %s (unresolved MANUAL "
                      "findings: %s)" % (fr["path"], fr["manual_rules"]))
                continue
            ok, err = boot_check(fr["abs_path"])
            if ok:
                print("hls-migrate: verify OK  %s" % fr["path"])
            else:
                failures += 1
                print("hls-migrate: verify FAIL %s — %s" %
                      (fr["path"], err))
        if failures:
            print("hls-migrate: verification FAILED for %d file(s)" %
                  failures)
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
