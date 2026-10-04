"""hls-reverify — the baseline harvester and the IR site harvest.

Stage 108. TWO harvests feed the replay:

1. `harvest_baseline(program)` — walk the CHECKED AST and collect every
   `-O fast` memory-safety check site with the interval prover's verdict
   (the annotations `boot/proof.py` wrote during the check — the same
   annotations `src/hlc/proof.hls` mirrors for the native codegen).
   This is the memory-safety EVIDENCE: proven sites are the claims
   `-O fast` may elide; checked sites keep their runtime check.

2. `harvest_ir_sites(mod)` — collect every check-site INSTRUCTION from
   an HLIR module (pre- and post-optimisation snapshots), keyed
   (function, kind, line). The for-loop lowering's generated
   `list_get` (dest `v_X`, index `v_X__i`) is NOT a user site — the
   discriminator is structural, and the baseline-vs-IR0 count
   cross-check in the replay law keeps the exclusion honest.
"""
from __future__ import annotations

from . import hlrev_common as com

# AST walk keys — the same child shapes propagate_stmt_exprs walks
# (boot/proof.py), so the baseline sees exactly what the prover saw.

_STMT_EXPR_KEYS = ("value", "cond", "iter", "e")
_STMT_BODY_KEYS = ("body", "then", "els")


def _walk_expr(e, out):
    """Collect (kind, line, proven) for every annotated check site in an
    expression. Only nodes the prover ANNOTATED carry a verdict — the
    harvest never invents one."""
    if not isinstance(e, dict):
        return
    k = e.get("k")
    if k == "bin":
        op = e.get("op")
        if e.get("t") == "int":
            if op in ("+", "-", "*") and "ovf_safe" in e:
                out.append((com.KIND_OVF, e.get("line", 0),
                            bool(e.get("ovf_safe"))))
            elif op in ("/", "%") and "div_safe" in e:
                out.append((com.KIND_DIV, e.get("line", 0),
                            bool(e.get("div_safe"))))
    elif k == "index":
        if "bnd_safe" in e:
            out.append((com.KIND_BND, e.get("line", 0),
                        bool(e.get("bnd_safe"))))
    elif k == "method":
        if e.get("name") in ("byte_at", "slice") and "bnd_safe" in e:
            out.append((com.KIND_BND, e.get("line", 0),
                        bool(e.get("bnd_safe"))))
    for key in ("l", "r", "e"):
        _walk_expr(e.get(key), out)
    for a in (e.get("args") or []):
        _walk_expr(a, out)
    if k == "method" or k == "field":
        _walk_expr(e.get("target"), out)
    if k == "index":
        _walk_expr(e.get("target"), out)
        _walk_expr(e.get("idx"), out)
    for it in (e.get("items") or []):
        _walk_expr(it, out)
    for _fname, fe in (e.get("fields") or []):
        _walk_expr(fe, out)
    if e.get("scrut") is not None:
        _walk_expr(e.get("scrut"), out)
    for arm in (e.get("arms") or []):
        _walk_expr(arm.get("body"), out)


def _walk_stmts(stmts, out):
    for s in stmts or []:
        if not isinstance(s, dict):
            continue
        for key in _STMT_EXPR_KEYS:
            _walk_expr(s.get(key), out)
        tgt = s.get("target")
        if isinstance(tgt, dict):
            # Assignment-target indexes are ALWAYS checked in every
            # backend (hl_list_set carries its own bounds check) — the
            # prover never annotates them; the walk below only reaches
            # the index EXPRESSION for completeness.
            _walk_expr(tgt.get("idx"), out)
        for bkey in _STMT_BODY_KEYS:
            _walk_stmts(s.get(bkey), out)


def harvest_baseline(program):
    """Return {fn_key: [(kind, line, proven), ...]} in source order —
    the memory-safety evidence the check+proof pass produced."""
    baseline = {}
    for key, fn in program["fns"].items():
        if fn.get("extern", False):
            continue
        sites = []
        _walk_stmts(fn.get("body"), sites)
        if sites:
            baseline[key] = sites
    return baseline


# ---------------------------------------------------------------------------
# IR-side harvest
# ---------------------------------------------------------------------------

def _arg_val(ins, i):
    """The ("var", name) / ("lit", v) operand at position i, else None."""
    if len(ins.args) > i:
        return ins.args[i]
    return None


def _binop_site(ins):
    """Classify a binop instruction into a check-site kind (or None)."""
    ty = (ins.attrs or {}).get("ty")
    if ty != "int":
        return None
    if not ins.args or ins.args[0][0] != "op":
        return None
    op = ins.args[0][1]
    if op in ("+", "-", "*"):
        return com.KIND_OVF
    if op in ("/", "%"):
        return com.KIND_DIV
    return None


def _method_site(ins):
    """byte_at / slice reads are bounds sites; `get` is the always-
    checked method spelling of a list read — advisory, like list_set."""
    if not ins.args or ins.args[0][0] != "name":
        return None
    name = ins.args[0][1]
    if name in ("byte_at", "slice"):
        return com.KIND_BND
    if name in ("get", "set"):
        return com.ADVISORY_KIND
    return None


def is_generated_for_get(ins):
    """The for-loop lowering's generated element read:
    `v_X = list_get <iter>, v_X__i`. Not a user site."""
    if ins.op != "list_get" or ins.dest is None:
        return False
    idx = _arg_val(ins, 1)
    return (idx is not None and idx[0] == "var"
            and idx[1] == ins.dest + "__i")


def site_kind(ins):
    """The check-site kind of an instruction (None = not a site)."""
    if ins.op == "binop":
        return _binop_site(ins)
    if ins.op == "list_get":
        return None if is_generated_for_get(ins) else com.KIND_BND
    if ins.op == "list_set":
        return com.ADVISORY_KIND
    if ins.op == "method":
        return _method_site(ins)
    return None


def harvest_ir_sites(mod):
    """Return {fn: [(kind, line, dest), ...]} for every check-site
    instruction in REACHABLE blocks, in block/instruction order
    (deterministic). Unreachable blocks (inherited from the lowering:
    an `if` whose branches both return leaves its endif shell) hold
    code the interpreter never executes — their sites are excluded
    here AND in the reconcile's unreachable class."""
    out = {}
    for fname, irf in mod.functions.items():
        if not irf.blocks:
            continue
        # reachability from the entry block
        name_set = {b.name for b in irf.blocks}
        succs = {}
        for b in irf.blocks:
            ss = []
            if b.terminator is not None and b.terminator.op in \
                    ("branch", "jump") and b.terminator.args:
                ss = [a[1] for a in b.terminator.args
                      if a[0] == "label" and a[1] in name_set]
            succs[b.name] = ss
        reach = set()
        work = [irf.blocks[0].name]
        while work:
            n = work.pop()
            if n in reach:
                continue
            reach.add(n)
            work.extend(succs.get(n, []))
        sites = []
        for block in irf.blocks:
            if block.name not in reach:
                continue
            for ins in block.instrs:
                kind = site_kind(ins)
                if kind is not None:
                    sites.append((kind, ins.line, ins.dest))
        if sites:
            out[fname] = sites
    return out
