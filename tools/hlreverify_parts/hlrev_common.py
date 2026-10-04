"""hls-reverify parts — shared shapes, constants and the report schema.

Stage 108 (v0.127.0-alpha): memory-safety re-verification under
`-O fast` (proof replay).

The module split follows the house layout (hltlog_parts, hlsign_parts,
hpkg_parts): the CLI entry stays thin in tools/hls-reverify.py and every
definition lives in exactly one part module.
"""
from __future__ import annotations

TOOL = "hls-reverify"
VERSION = "0.127.0-alpha"          # the toolchain release this stage stamps
REPORT_SCHEMA = "hls-reverify-report/v1"

# ---------------------------------------------------------------------------
# Check-site kinds — the exact elision surface of `-O fast`:
#   ovf — int `+ - *` (gen_expr emits hl_add_i64/sub/mul unchecked)
#   div — int `/ %`   (the divisor cannot be 0 nor INT64_MIN/-1)
#   bnd — `xs[i]` reads, `s.byte_at(i)`, `s.slice(a, b)`
# (list.set — index ASSIGNMENT targets — is always checked in every
# backend; those sites are advisory only and never part of the law.)
# ---------------------------------------------------------------------------
KIND_OVF = "ovf"
KIND_DIV = "div"
KIND_BND = "bnd"
SITE_KINDS = (KIND_OVF, KIND_DIV, KIND_BND)
ADVISORY_KIND = "set"          # list_set — replayed, reported, not judged

# The replay-law verdicts (per baseline claim class).
C_REPLAYED = "replayed"                # proven before, re-proven after
C_REPLAYED_CHECKED = "replayed-checked"  # checked before, checked after
C_LOST_PROOF = "lost-proof"            # proven before, not re-provable -> FAIL
C_ELIMINATED = "eliminated"            # site folded/DCE'd away (vacuous)
C_INLINE_DERIVED = "inline-derived"    # callee body copied into a caller
C_NEW_PROOF = "new-proof"              # post-opt proof the baseline lacked
C_UNREACHABLE = "unreachable"          # never lowered (after return/break)
C_NO_CLAIM = "no-claim"                # advisory site (no elision class)

# Failure classes (any one flips the verdict to REPLAY-FAILED).
F_LOST_PROOF = "lost-proof"
F_FORGED_ANNOTATION = "forged-annotation"
F_BROKEN_IR = "broken-ir"
F_DUPLICATED_SITE = "duplicated-site"   # lowering invented a check site

# The algebraic identity certificates the replay accepts for
# `safe_overflow` annotations (on top of an interval proof). Adding
# zero / multiplying by zero or one cannot overflow REGARDLESS of the
# operand's bounds — the result is the other operand, which is already
# a legal int64. These shapes are pinned here, by name; any annotation
# outside (interval proof | this whitelist) is FORGED.
ALGEBRAIC_IDENTITIES = (
    "alg:add-zero-right",   # x + 0
    "alg:add-zero-left",    # 0 + x
    "alg:sub-zero-right",   # x - 0
    "alg:mul-zero-right",   # x * 0
    "alg:mul-zero-left",    # 0 * x
    "alg:mul-one-right",    # x * 1
    "alg:mul-one-left",     # 1 * x
)


def canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, no whitespace — the same
    discipline every Stage 103+ document follows (the report is
    byte-identical over an unchanged tree)."""
    import json
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def new_report(entry: str, lto: bool) -> dict:
    """The report skeleton (schema hls-reverify-report/v1)."""
    return {
        "schema": REPORT_SCHEMA,
        "tool": TOOL,
        "tool_version": VERSION,
        "entry": entry,
        "lto": bool(lto),
        "verdict": None,
        "baseline": {"sites": 0, "proven": 0, "checked": 0},
        "replay": {"sites": 0, "proven": 0, "functions": 0, "blocks": 0,
                   "widened_loops": 0},
        "counts": {},
        "findings": [],       # every classified finding (text + class)
        "failures": [],       # the failure classes that fired
        "functions": {},      # per-function site tables (deterministic order)
    }
