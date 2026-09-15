"""analysis - verbatim segment of the original tools/hlwasm_opt.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from wopt_common import (
    Dict, List, OP_BLOCK, OP_BR, OP_BR_IF, OP_BR_TABLE, OP_CALL, OP_CALL_INDIRECT,
    OP_F32_CONST, OP_F64_CONST, OP_GLOBAL_GET, OP_GLOBAL_SET, OP_I32_CONST, OP_I64_CONST, OP_IF, OP_LOCAL_GET,
    OP_LOCAL_SET, OP_LOCAL_TEE, OP_LOOP, Set, _MEM_OPS, read_sleb, read_uleb,
)
from wopt_module import (
    WasmModule,
)

def _scan_calls(body: bytes) -> List[int]:
    """Return the list of function indices referenced by `call` instructions
    in this function body. Does NOT descend into nested control structures
    (the indices are absolute function indices in the global space, so we
    just scan linearly)."""
    out: List[int] = []
    pos = 0
    n = len(body)
    while pos < n:
        op = body[pos]; pos += 1
        if op == OP_CALL:
            idx, pos = read_uleb(body, pos)
            out.append(idx)
        elif op == OP_CALL_INDIRECT:
            # type_idx, table_idx
            _, pos = read_uleb(body, pos)
            _, pos = read_uleb(body, pos)
        elif op in (OP_BLOCK, OP_LOOP, OP_IF):
            pos += 1  # block type (1 byte; could be value type or sleb, but
                       # for our emitter it's always 0x40 = void)
        elif op == OP_BR_TABLE:
            n_targets, pos = read_uleb(body, pos)
            for _ in range(n_targets + 1):
                _, pos = read_uleb(body, pos)
        elif op == OP_LOCAL_GET or op == OP_LOCAL_SET or op == OP_LOCAL_TEE:
            _, pos = read_uleb(body, pos)
        elif op == OP_GLOBAL_GET or op == OP_GLOBAL_SET:
            _, pos = read_uleb(body, pos)
        elif op == OP_BR or op == OP_BR_IF:
            _, pos = read_uleb(body, pos)
        elif op == OP_I32_CONST:
            _, pos = read_sleb(body, pos)
        elif op == OP_I64_CONST:
            _, pos = read_sleb(body, pos)
        elif op == OP_F32_CONST:
            pos += 4
        elif op == OP_F64_CONST:
            pos += 8
        elif op in _MEM_OPS:
            _, pos = read_uleb(body, pos)  # align
            _, pos = read_uleb(body, pos)  # offset
        elif op == 0xFC:
            # Deep-scan-13 fix: bulk-memory / table instructions carry a
            # sub-opcode + immediates (memory.copy = 0xFC 0x0A dst, src).
            # The old walker fell through and misparsed the sub-opcode
            # bytes as instructions — a latent parser bug that could
            # mark arbitrary function indices as called (conservative
            # over-retention, but still wrong).
            sub, pos = read_uleb(body, pos)
            if sub == 0x08:      # memory.init dst, src
                _, pos = read_uleb(body, pos)
                _, pos = read_uleb(body, pos)
            elif sub in (0x09, 0x0B, 0x0D, 0x10, 0x12, 0x14, 0x15,
                         0x16, 0x17):
                # data.drop / memory.fill / table.copy / elem.drop etc.
                _, pos = read_uleb(body, pos)
            else:
                # memory.copy (0x0A) and the table.init variants take
                # TWO index immediates.
                _, pos = read_uleb(body, pos)
                _, pos = read_uleb(body, pos)
        # For all other ops we encounter (drop, end, return, etc.), there
        # are no immediates — the loop continues.
    return out


def find_live_functions(mod: WasmModule) -> Set[int]:
    """Mark-and-sweep: a function is live iff it's reachable from an
    exported function, the start function, or an exported table element
    (the latter is not in our alpha)."""
    n_imports = len(mod.imports)
    live: Set[int] = set()

    # Seeds: exported functions + start function. Note: import indices
    # (0..n_imports-1) are always "live" in the sense that we can't
    # eliminate them — they're declared by the user, and removing an
    # import that's never called is a separate pass (dead import elim).
    # For the function-DCE pass, we only consider DEFINED functions
    # (indices n_imports..n_imports+n_funcs-1).
    seeds: List[int] = []
    for exp in mod.exports:
        if exp.kind == 0x00 and exp.index >= n_imports:
            seeds.append(exp.index)
    if mod.start is not None and mod.start >= n_imports:
        seeds.append(mod.start)

    worklist: List[int] = list(seeds)
    while worklist:
        idx = worklist.pop()
        if idx in live:
            continue
        live.add(idx)
        if idx < n_imports:
            continue  # import — no body to scan
        # Defined function index -> code entry index.
        code_idx = idx - n_imports
        if code_idx < 0 or code_idx >= len(mod.codes):
            continue
        for callee in _scan_calls(mod.codes[code_idx].body):
            if callee not in live:
                worklist.append(callee)
    return live


def find_used_imports(mod: WasmModule, live_funcs: Set[int]) -> Set[int]:
    """Return the set of import indices (0..n_imports-1) referenced by
    any live function (or by exports)."""
    n_imports = len(mod.imports)
    used: Set[int] = set()
    # Direct exports: an import can be exported directly.
    for exp in mod.exports:
        if exp.kind == 0x00 and exp.index < n_imports:
            used.add(exp.index)
    # Calls from live functions.
    for idx in live_funcs:
        if idx < n_imports:
            used.add(idx)
            continue
        code_idx = idx - n_imports
        if code_idx < 0 or code_idx >= len(mod.codes):
            continue
        for callee in _scan_calls(mod.codes[code_idx].body):
            if callee < n_imports:
                used.add(callee)
    return used


def find_used_data_offsets(mod: WasmModule, live_funcs: Set[int],
                           data_offset_to_idx: Dict[int, int]) -> Set[int]:
    """Return the set of data-segment indices referenced by live functions.

    The reference emitter uses `i32.const <offset>` to push the address of
    a string literal. We scan each live function body for `i32.const`
    instructions whose value matches a known data offset, and mark that
    data segment as used.

    This is conservative: an i32.const that happens to equal a data
    offset but is not used as a pointer will mark the segment as used
    unnecessarily. That's safe (we just miss a DCE opportunity); the
    reverse — dropping a used segment — would corrupt the program.
    """
    n_imports = len(mod.imports)
    used: Set[int] = set()
    if not data_offset_to_idx:
        return used
    for idx in live_funcs:
        if idx < n_imports:
            continue
        code_idx = idx - n_imports
        if code_idx < 0 or code_idx >= len(mod.codes):
            continue
        body = mod.codes[code_idx].body
        pos = 0
        n = len(body)
        while pos < n:
            op = body[pos]; pos += 1
            if op == OP_I32_CONST:
                val, pos = read_sleb(body, pos)
                if val in data_offset_to_idx:
                    used.add(data_offset_to_idx[val])
            elif op == OP_I64_CONST:
                _, pos = read_sleb(body, pos)
            elif op == OP_F32_CONST:
                pos += 4
            elif op == OP_F64_CONST:
                pos += 8
            elif op in (OP_LOCAL_GET, OP_LOCAL_SET, OP_LOCAL_TEE,
                       OP_GLOBAL_GET, OP_GLOBAL_SET, OP_BR, OP_BR_IF,
                       OP_CALL):
                _, pos = read_uleb(body, pos)
            elif op == OP_CALL_INDIRECT:
                _, pos = read_uleb(body, pos)
                _, pos = read_uleb(body, pos)
            elif op in (OP_BLOCK, OP_LOOP, OP_IF):
                pos += 1
            elif op == OP_BR_TABLE:
                n_targets, pos = read_uleb(body, pos)
                for _ in range(n_targets + 1):
                    _, pos = read_uleb(body, pos)
            elif op in _MEM_OPS:
                _, pos = read_uleb(body, pos)
                _, pos = read_uleb(body, pos)
    return used


# ============================================================================
# Optimizations.
# ============================================================================



__all__ = [
    "_scan_calls",
    "find_live_functions",
    "find_used_data_offsets",
    "find_used_imports",
]
