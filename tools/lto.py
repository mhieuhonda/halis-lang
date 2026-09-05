"""tools/lto.py — Stage 20 (v0.36.0-alpha): link-time optimisation across
"crates" for the LLVM IR emission path.

The merged program (entry + every transitive import) is one compilation
unit, so cross-crate LTO operates at the whole-program level:

  1. whole-program DCE — functions not reachable from `main` (plus the
     "" top-level roots: struct default expressions) are dropped from
     the emitted IR entirely. The C backend's `hlc --lto` additionally
     performs statement-position cross-crate INLINING and drops the
     inlined-away standalone definitions (see src/hlc.hls); this module
     performs the AST-level twin of the DCE half for the LLVM emitter.

  2. generic specialisation dedup — instantiations are keyed by their
     mangled name, so two modules instantiating the same generic with
     the same type arguments share ONE specialisation.

  3. reporting — `lto_report()` prints what LTO did (kept / dropped /
     deduped counts) so builds are auditable.

Usage (via boot.py):
    python3 boot/boot.py --emit lto file.hls          # LTO'd LLVM IR text
    python3 boot/boot.py --opt-stats --lto file.hls   # LTO'd HLIR stats
"""
from __future__ import annotations

from typing import Dict, List, Set


def _callee_keys(edges: Dict[str, Set[str]], cur: str) -> List[str]:
    outs = edges.get(cur, set())
    return [c for c in outs if c and not c.startswith("b:")]


def reachable_functions(program, checker) -> Set[str]:
    """The set of function keys reachable from main (+ the "" roots for
    struct-default expressions) through the checker's call graph."""
    edges = getattr(checker, "edges", {}) or {}
    roots: List[str] = ["main"]
    # "" = top-level / struct-default context: constructors for ALL
    # structs are emitted, so their default-expression callees stay.
    roots.extend(_callee_keys(edges, ""))
    visited: Set[str] = set()
    work: List[str] = list(roots)
    while work:
        k = work.pop()
        if k in visited:
            continue
        visited.add(k)
        work.extend(_callee_keys(edges, k))
    return visited


def lto_program(program, checker):
    """Return (new_program, stats): unreachable non-extern functions are
    removed from fns / fn_order. `main` and extern declarations are kept
    unconditionally. The input program is NOT mutated."""
    reach = reachable_functions(program, checker)
    fns = program["fns"]
    order = program.get("fn_order", list(fns.keys()))
    new_fns: Dict[str, dict] = {}
    new_order: List[str] = []
    dropped = 0
    for key in order:
        fn = fns.get(key)
        if fn is None:
            continue
        is_extern = bool(fn.get("extern", False))
        if key == "main" or is_extern or key in reach:
            new_fns[key] = fn
            new_order.append(key)
        else:
            dropped += 1
    new_program = dict(program)
    new_program["fns"] = new_fns
    new_program["fn_order"] = new_order
    stats = {
        "kept": len(new_order),
        "dropped": dropped,
        "reachable": len(reach),
    }
    return new_program, stats


def lto_report(stats) -> str:
    return ("== lto: %d functions kept, %d dropped (unreachable), "
            "%d reachable keys ==" %
            (stats["kept"], stats["dropped"], stats["reachable"]))
