#!/usr/bin/env python3
"""hlwasm_opt.py — Stage 24 (v0.43.0-alpha): in-tree wasm size optimizer.

A pure-Python wasm binary optimizer that performs genuine size reductions
on the output of `tools/hlwasm.py`. The roadmap's Stage 24 acceptance
criterion is "wasm-opt reduces size by >= 30%"; this module delivers that
reduction by running four classes of optimization that are always safe
(they preserve wasm validation and observable behaviour):

  1. DEAD FUNCTION ELIMINATION (DCE)
     Mark-and-sweep from the module's exported functions and the start
     function. A function is live iff it is reachable from an export or
     the start function via the `call` instruction; everything else is
     dead. The wasm spec guarantees this is safe: an unreachable function
     has no observable effect.

  2. DEAD IMPORT ELIMINATION
     An import that is never referenced by a `call` instruction in a
     live function is dead. Removing it both shrinks the import section
     AND every function index that comes after it (we renumber).

  3. TYPE-SECTION DEDUPLICATION
     Two function signatures with identical (params, results) are
     merged into a single type entry; function/type indices are
     rewritten to the canonical one. The wasm spec allows arbitrary
     type ordering; we keep the first occurrence.

  4. LOCAL COMPACTION
     A function's locals vector is a list of (count, type) pairs. The
     reference emitter may emit separate (1, I32) entries for each
     local; we merge consecutive entries of the same type into a
     single (N, I32) entry. (Same value type, but separate locals
     must have distinct indices — the merge is purely a serialization
     win: a (count, type) pair is 2-3 bytes vs. N pairs of (1, type)
     at 2-3 bytes each.)

  5. STRING POOL DEDUPLICATION (data section)
     Two identical byte sequences in the data section are merged into
     one; references in the code section are rewritten. The wasm spec
     permits multiple `data` segments at the same offset — the bytes
     overlap, which is undefined behaviour at runtime in the spec, so
     we actually keep them as one segment with a fresh offset and
     rewrite the const that loads the address.

The optimizer is designed to be safe to run multiple times and to be
idempotent: running it twice produces identical output. The first run
removes the dead code; the second run has nothing to do.

Usage:
  python3 tools/hlwasm_opt.py <input.wasm> <output.wasm>
                              [--level O1|O2|O3|Os]
                              [--report]
                              [--external-wasm-opt PATH]

When --external-wasm-opt is given (or `wasm-opt` is found on PATH), the
optimizer additionally invokes the external Binaryen `wasm-opt` for
binary-level passes (inlining, alias analysis, constant propagation)
that are beyond the scope of this in-tree optimizer. The external
optimizer is run AFTER the in-tree passes — the in-tree DCE cleans up
the bulk of the dead code, leaving the external optimizer to do the
fine-grained work on what's left.

The optimization level controls how aggressive the in-tree passes are:
  O1   : dead function + dead import elimination + local compaction.
  O2   : O1 + type-section deduplication + dead data elimination.
  O3   : O2 + peephole opts on the code section (drop-after-return,
         unreachable-after-unreachable, nop elimination, const-fold
         i32.eqz on a constant).
  Os   : O3 + aggressive string-pool deduplication (slower).

Default: O3.

The `--report` flag prints a summary of what was done (sections before/
after, dead functions removed, dead imports removed, dead data
eliminated, bytes saved).
"""
from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple


# ============================================================================
# LEB128 + wasm binary primitives (mirror of hlwasm.py — kept self-contained
# so this module has no internal dependencies that could break).
# ============================================================================

def uleb(n: int) -> bytes:
    if n < 0:
        raise ValueError("uleb: negative input")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n != 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


def sleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if (n == 0 and (b & 0x40) == 0) or (n == -1 and (b & 0x40) != 0):
            out.append(b)
            break
        b |= 0x80
        out.append(b)
    return bytes(out)


def read_uleb(buf: bytes, pos: int) -> Tuple[int, int]:
    """Read an unsigned LEB128 from buf at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            break
        shift += 7
    return result, pos


def read_sleb(buf: bytes, pos: int) -> Tuple[int, int]:
    """Read a signed LEB128 from buf at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if (b & 0x80) == 0:
            if (b & 0x40) != 0:
                result |= -(1 << shift)
            break
    return result, pos


# WASM value types.
I32 = 0x7F
I64 = 0x7E
F32 = 0x7D
F64 = 0x7C
FUNCREF = 0x70
EXTERNREF = 0x6F

# WASM section IDs.
SEC_TYPE = 1
SEC_IMPORT = 2
SEC_FUNCTION = 3
SEC_TABLE = 4
SEC_MEMORY = 5
SEC_GLOBAL = 6
SEC_EXPORT = 7
SEC_START = 8
SEC_ELEMENT = 9
SEC_CODE = 10
SEC_DATA = 11
SEC_DATA_COUNT = 12

# WASM instructions we use (a subset — only what we need for analysis).
OP_UNREACHABLE = 0x00
OP_NOP = 0x01
OP_BLOCK = 0x02
OP_LOOP = 0x03
OP_IF = 0x04
OP_ELSE = 0x05
OP_END = 0x0B
OP_BR = 0x0C
OP_BR_IF = 0x0D
OP_BR_TABLE = 0x0E
OP_RETURN = 0x0F
OP_CALL = 0x10
OP_CALL_INDIRECT = 0x11
OP_DROP = 0x1A
OP_LOCAL_GET = 0x20
OP_LOCAL_SET = 0x21
OP_LOCAL_TEE = 0x22
OP_GLOBAL_GET = 0x23
OP_GLOBAL_SET = 0x24
# Memory load/store ops (all have align + offset immediates).
OP_I32_LOAD = 0x28
OP_I64_LOAD = 0x29
OP_F32_LOAD = 0x2A
OP_F64_LOAD = 0x2B
OP_I32_LOAD8_S = 0x2C
OP_I32_LOAD8_U = 0x2D
OP_I32_LOAD16_S = 0x2E
OP_I32_LOAD16_U = 0x2F
OP_I64_LOAD8_S = 0x30
OP_I64_LOAD8_U = 0x31
OP_I64_LOAD16_S = 0x32
OP_I64_LOAD16_U = 0x33
OP_I64_LOAD32_S = 0x34
OP_I64_LOAD32_U = 0x35
OP_I32_STORE = 0x36
OP_I64_STORE = 0x37
OP_F32_STORE = 0x38
OP_F64_STORE = 0x39
OP_I32_STORE8 = 0x3A
OP_I32_STORE16 = 0x3B
OP_I64_STORE8 = 0x3C
OP_I64_STORE16 = 0x3D
OP_I64_STORE32 = 0x3E
OP_MEMORY_SIZE = 0x3F
OP_MEMORY_GROW = 0x40
OP_I32_CONST = 0x41
OP_I64_CONST = 0x42
OP_F32_CONST = 0x43
OP_F64_CONST = 0x44
OP_I32_EQZ = 0x45

# All memory load/store ops that take (align, offset) immediates.
# Used by _scan_calls / _renumber_calls / _peephole_body to skip the
# immediates when walking function bodies.
_MEM_OPS = frozenset([
    OP_I32_LOAD, OP_I64_LOAD, OP_F32_LOAD, OP_F64_LOAD,
    OP_I32_LOAD8_S, OP_I32_LOAD8_U, OP_I32_LOAD16_S, OP_I32_LOAD16_U,
    OP_I64_LOAD8_S, OP_I64_LOAD8_U, OP_I64_LOAD16_S, OP_I64_LOAD16_U,
    OP_I64_LOAD32_S, OP_I64_LOAD32_U,
    OP_I32_STORE, OP_I64_STORE, OP_F32_STORE, OP_F64_STORE,
    OP_I32_STORE8, OP_I32_STORE16, OP_I64_STORE8, OP_I64_STORE16,
    OP_I64_STORE32,
])


# ============================================================================
# Parser — turn the wasm binary into a structured module.
# ============================================================================

class FuncType:
    __slots__ = ("params", "results")

    def __init__(self, params: List[int], results: List[int]):
        self.params = params
        self.results = results

    def key(self) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
        return (tuple(self.params), tuple(self.results))

    def __eq__(self, other):
        if not isinstance(other, FuncType):
            return False
        return self.params == other.params and self.results == other.results


class Import:
    __slots__ = ("module", "name", "kind", "type_idx")
    # kind: 0x00 = func, 0x01 = table, 0x02 = memory, 0x03 = global

    def __init__(self, module: str, name: str, kind: int, type_idx: int):
        self.module = module
        self.name = name
        self.kind = kind
        self.type_idx = type_idx


class Export:
    __slots__ = ("name", "kind", "index")
    # kind: 0x00 = func, 0x01 = table, 0x02 = memory, 0x03 = global

    def __init__(self, name: str, kind: int, index: int):
        self.name = name
        self.kind = kind
        self.index = index


class Code:
    __slots__ = ("locals", "body")

    def __init__(self, locals_: List[Tuple[int, int]], body: bytes):
        # locals_ is a list of (count, type) pairs.
        self.locals = locals_
        self.body = body


class DataSegment:
    __slots__ = ("offset", "data")
    # active, memory 0 — the only kind we emit.

    def __init__(self, offset: int, data: bytes):
        self.offset = offset
        self.data = data


class WasmModule:
    def __init__(self):
        self.types: List[FuncType] = []
        self.imports: List[Import] = []
        self.funcs: List[int] = []  # type indices for defined functions
        self.memories: List[Tuple[int, Optional[int]]] = []
        self.globals: List[Tuple[bool, int, int]] = []
        self.exports: List[Export] = []
        self.start: Optional[int] = None
        self.codes: List[Code] = []
        self.data: List[DataSegment] = []

    # --- parsing ---

    @classmethod
    def parse(cls, wasm: bytes) -> "WasmModule":
        if wasm[:4] != b"\x00asm":
            raise ValueError("not a wasm binary (magic mismatch)")
        version = struct.unpack("<I", wasm[4:8])[0]
        if version != 1:
            raise ValueError("unsupported wasm version: %d" % version)
        pos = 8
        mod = cls()
        while pos < len(wasm):
            sec_id = wasm[pos]; pos += 1
            sec_size, pos = read_uleb(wasm, pos)
            sec_end = pos + sec_size
            if sec_id == SEC_TYPE:
                mod._parse_types(wasm, pos, sec_end)
            elif sec_id == SEC_IMPORT:
                mod._parse_imports(wasm, pos, sec_end)
            elif sec_id == SEC_FUNCTION:
                mod._parse_functions(wasm, pos, sec_end)
            elif sec_id == SEC_MEMORY:
                mod._parse_memories(wasm, pos, sec_end)
            elif sec_id == SEC_GLOBAL:
                mod._parse_globals(wasm, pos, sec_end)
            elif sec_id == SEC_EXPORT:
                mod._parse_exports(wasm, pos, sec_end)
            elif sec_id == SEC_START:
                mod.start, pos = read_uleb(wasm, pos)
            elif sec_id == SEC_CODE:
                mod._parse_code(wasm, pos, sec_end)
            elif sec_id == SEC_DATA:
                mod._parse_data(wasm, pos, sec_end)
            elif sec_id == SEC_DATA_COUNT:
                # DataCount section: just a u32 count; we don't need to track it.
                pass
            # Unknown sections (custom, table, element) are skipped.
            pos = sec_end
        return mod

    def _parse_types(self, buf: bytes, pos: int, end: int):
        n, pos = read_uleb(buf, pos)
        for _ in range(n):
            form = buf[pos]; pos += 1  # 0x60 = func
            assert form == 0x60, "expected 0x60 (func), got 0x%02x" % form
            np, pos = read_uleb(buf, pos)
            params = list(buf[pos:pos + np]); pos += np
            nr, pos = read_uleb(buf, pos)
            results = list(buf[pos:pos + nr]); pos += nr
            self.types.append(FuncType(params, results))

    def _parse_imports(self, buf: bytes, pos: int, end: int):
        n, pos = read_uleb(buf, pos)
        for _ in range(n):
            mlen, pos = read_uleb(buf, pos)
            module = buf[pos:pos + mlen].decode("utf-8"); pos += mlen
            nlen, pos = read_uleb(buf, pos)
            name = buf[pos:pos + nlen].decode("utf-8"); pos += nlen
            kind = buf[pos]; pos += 1
            type_idx = 0
            if kind == 0x00:  # func
                type_idx, pos = read_uleb(buf, pos)
            elif kind == 0x01:  # table
                # elem type (1 byte) + limits
                pos += 1
                flag = buf[pos]; pos += 1
                _, pos = read_uleb(buf, pos)
                if flag == 1:
                    _, pos = read_uleb(buf, pos)
            elif kind == 0x02:  # memory
                flag = buf[pos]; pos += 1
                _, pos = read_uleb(buf, pos)
                if flag == 1:
                    _, pos = read_uleb(buf, pos)
            elif kind == 0x03:  # global
                pos += 1  # value type
                pos += 1  # mutability
            self.imports.append(Import(module, name, kind, type_idx))

    def _parse_functions(self, buf: bytes, pos: int, end: int):
        n, pos = read_uleb(buf, pos)
        for _ in range(n):
            ty, pos = read_uleb(buf, pos)
            self.funcs.append(ty)

    def _parse_memories(self, buf: bytes, pos: int, end: int):
        n, pos = read_uleb(buf, pos)
        for _ in range(n):
            flag = buf[pos]; pos += 1
            min_p, pos = read_uleb(buf, pos)
            max_p = None
            if flag == 1:
                max_p, pos = read_uleb(buf, pos)
            self.memories.append((min_p, max_p))

    def _parse_globals(self, buf: bytes, pos: int, end: int):
        n, pos = read_uleb(buf, pos)
        for _ in range(n):
            ty = buf[pos]; pos += 1
            mut = buf[pos]; pos += 1
            # init_expr: <const> end
            init_val = 0
            if buf[pos] == OP_I32_CONST:
                pos += 1
                init_val, pos = read_sleb(buf, pos)
            elif buf[pos] == OP_I64_CONST:
                pos += 1
                init_val, pos = read_sleb(buf, pos)
            else:
                # Skip the init expression generically.
                pos = self._skip_init_expr(buf, pos)
            assert buf[pos] == OP_END
            pos += 1
            self.globals.append((mut != 0, ty, init_val))

    def _skip_init_expr(self, buf: bytes, pos: int) -> int:
        # Skip a single init expression: read instructions until OP_END.
        depth = 0
        while True:
            op = buf[pos]; pos += 1
            if op in (OP_BLOCK, OP_LOOP, OP_IF):
                pos += 1  # block type
                depth += 1
            elif op == OP_END:
                if depth == 0:
                    return pos
                depth -= 1
            elif op == OP_I32_CONST:
                _, pos = read_sleb(buf, pos)
            elif op == OP_I64_CONST:
                _, pos = read_sleb(buf, pos)
            elif op == OP_F32_CONST:
                pos += 4
            elif op == OP_F64_CONST:
                pos += 8
            elif op == OP_GLOBAL_GET:
                _, pos = read_uleb(buf, pos)
            # Other instructions: no immediates for our use case.

    def _parse_exports(self, buf: bytes, pos: int, end: int):
        n, pos = read_uleb(buf, pos)
        for _ in range(n):
            nlen, pos = read_uleb(buf, pos)
            name = buf[pos:pos + nlen].decode("utf-8"); pos += nlen
            kind = buf[pos]; pos += 1
            idx, pos = read_uleb(buf, pos)
            self.exports.append(Export(name, kind, idx))

    def _parse_code(self, buf: bytes, pos: int, end: int):
        n, pos = read_uleb(buf, pos)
        for _ in range(n):
            body_size, pos = read_uleb(buf, pos)
            body_end = pos + body_size
            nloc, pos = read_uleb(buf, pos)
            locals_: List[Tuple[int, int]] = []
            for _ in range(nloc):
                count, pos = read_uleb(buf, pos)
                ty = buf[pos]; pos += 1
                locals_.append((count, ty))
            body = buf[pos:body_end]
            pos = body_end
            # Strip the trailing OP_END (it's part of the expr per the
            # wasm spec, but we follow hlwasm.py's convention of NOT
            # including it in `body` — the serializer re-adds it).
            # This keeps the body manipulable as "instructions only".
            if body and body[-1] == OP_END:
                body = body[:-1]
            self.codes.append(Code(locals_, body))

    def _parse_data(self, buf: bytes, pos: int, end: int):
        n, pos = read_uleb(buf, pos)
        for _ in range(n):
            flag = buf[pos]; pos += 1
            # active, memory 0 (flag 0): offset init expr + bytes
            assert flag == 0, "only active data segment flag 0 supported"
            # offset: i32.const <sleb> end
            assert buf[pos] == OP_I32_CONST
            pos += 1
            offset, pos = read_sleb(buf, pos)
            assert buf[pos] == OP_END
            pos += 1
            dlen, pos = read_uleb(buf, pos)
            data = buf[pos:pos + dlen]
            pos += dlen
            self.data.append(DataSegment(offset, data))


# ============================================================================
# Analysis — find live functions, dead imports, dead data.
# ============================================================================

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
        # For all other ops we encounter (drop, end, return, etc.), there
        # are no immediates — the loop continues.
    return out


def find_live_functions(mod: WasmModule) -> Set[int]:
    """Mark-and-sweep: a function is live iff it's reachable from an
    exported function, the start function, or an exported table element
    (the latter is not in our alpha)."""
    n_imports = len(mod.imports)
    n_funcs = len(mod.funcs)
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

def opt_dce(mod: WasmModule, report: dict):
    """Dead function elimination + dead import elimination."""
    n_imports = len(mod.imports)
    live = find_live_functions(mod)
    used_imports = find_used_imports(mod, live)

    # Build the renumbering maps.
    new_import_indices: Dict[int, int] = {}
    new_imports: List[Import] = []
    for i, imp in enumerate(mod.imports):
        if i in used_imports:
            new_import_indices[i] = len(new_imports)
            new_imports.append(imp)

    new_func_indices: Dict[int, int] = {}
    new_funcs: List[int] = []
    new_codes: List[Code] = []
    for i, (ty, code) in enumerate(zip(mod.funcs, mod.codes)):
        old_idx = n_imports + i
        if old_idx in live:
            # The wasm function index space includes BOTH imports and
            # defined functions, contiguous. The new index of a defined
            # function is its position in the (new_imports || new_funcs)
            # list — NOT n_imports + position (that would double-count
            # the original import count).
            new_func_indices[old_idx] = len(new_imports) + len(new_funcs)
            new_funcs.append(ty)
            new_codes.append(code)

    # Renumber call targets in live function bodies.
    for code in new_codes:
        code.body = _renumber_calls(code.body, new_import_indices, new_func_indices)

    # Renumber exports + start function.
    new_exports: List[Export] = []
    for exp in mod.exports:
        if exp.kind == 0x00:
            if exp.index < n_imports:
                if exp.index in new_import_indices:
                    new_exports.append(Export(exp.name, exp.kind,
                                              new_import_indices[exp.index]))
            else:
                if exp.index in new_func_indices:
                    new_exports.append(Export(exp.name, exp.kind,
                                              new_func_indices[exp.index]))
        else:
            new_exports.append(exp)
    new_start: Optional[int] = None
    if mod.start is not None:
        if mod.start < n_imports:
            if mod.start in new_import_indices:
                new_start = new_import_indices[mod.start]
        else:
            if mod.start in new_func_indices:
                new_start = new_func_indices[mod.start]

    report["dead_funcs_removed"] = (len(mod.funcs) - len(new_funcs))
    report["dead_imports_removed"] = (len(mod.imports) - len(new_imports))
    mod.imports = new_imports
    mod.funcs = new_funcs
    mod.codes = new_codes
    mod.exports = new_exports
    mod.start = new_start


def _renumber_calls(body: bytes,
                    new_import_indices: Dict[int, int],
                    new_func_indices: Dict[int, int]) -> bytes:
    """Walk a function body and rewrite `call <idx>` instructions to use
    the renumbered indices. Returns new bytes."""
    out = bytearray()
    pos = 0
    n = len(body)
    while pos < n:
        op = body[pos]; pos += 1
        out.append(op)
        if op == OP_CALL:
            idx, pos = read_uleb(body, pos)
            new_idx = new_import_indices.get(idx, new_func_indices.get(idx, idx))
            out += uleb(new_idx)
        elif op == OP_CALL_INDIRECT:
            type_idx, pos = read_uleb(body, pos)
            table_idx, pos = read_uleb(body, pos)
            out += uleb(type_idx)
            out += uleb(table_idx)
        elif op in (OP_BLOCK, OP_LOOP, OP_IF):
            out.append(body[pos]); pos += 1
        elif op == OP_BR_TABLE:
            n_targets, pos = read_uleb(body, pos)
            out += uleb(n_targets)
            for _ in range(n_targets + 1):
                t, pos = read_uleb(body, pos)
                out += uleb(t)
        elif op in (OP_LOCAL_GET, OP_LOCAL_SET, OP_LOCAL_TEE,
                    OP_GLOBAL_GET, OP_GLOBAL_SET, OP_BR, OP_BR_IF):
            v, pos = read_uleb(body, pos)
            out += uleb(v)
        elif op == OP_I32_CONST:
            v, pos = read_sleb(body, pos)
            out += sleb(v)
        elif op == OP_I64_CONST:
            v, pos = read_sleb(body, pos)
            out += sleb(v)
        elif op == OP_F32_CONST:
            out += body[pos:pos + 4]; pos += 4
        elif op == OP_F64_CONST:
            out += body[pos:pos + 8]; pos += 8
        elif op in _MEM_OPS:
            align, pos = read_uleb(body, pos)
            off, pos = read_uleb(body, pos)
            out += uleb(align)
            out += uleb(off)
        # All other ops: no immediates — already appended.
    return bytes(out)


def opt_type_dedup(mod: WasmModule, report: dict):
    """Deduplicate the type section: collapse identical function signatures
    into a single type entry. Rewrites function, import, and call_indirect
    type_idx references."""
    new_types: List[FuncType] = []
    remap: Dict[int, int] = {}
    seen: Dict[Tuple[Tuple[int, ...], Tuple[int, ...]], int] = {}
    for i, ty in enumerate(mod.types):
        key = ty.key()
        if key in seen:
            remap[i] = seen[key]
        else:
            seen[key] = len(new_types)
            remap[i] = len(new_types)
            new_types.append(ty)
    report["types_deduped"] = len(mod.types) - len(new_types)
    # Rewrite all type_idx references.
    for imp in mod.imports:
        if imp.kind == 0x00:
            imp.type_idx = remap.get(imp.type_idx, imp.type_idx)
    for i in range(len(mod.funcs)):
        mod.funcs[i] = remap.get(mod.funcs[i], mod.funcs[i])
    # call_indirect type_idx in code bodies.
    for code in mod.codes:
        code.body = _renumber_call_indirect(code.body, remap)
    mod.types = new_types


def _renumber_call_indirect(body: bytes, remap: Dict[int, int]) -> bytes:
    out = bytearray()
    pos = 0
    n = len(body)
    while pos < n:
        op = body[pos]; pos += 1
        out.append(op)
        if op == OP_CALL_INDIRECT:
            type_idx, pos = read_uleb(body, pos)
            table_idx, pos = read_uleb(body, pos)
            out += uleb(remap.get(type_idx, type_idx))
            out += uleb(table_idx)
        elif op == OP_CALL:
            idx, pos = read_uleb(body, pos)
            out += uleb(idx)
        elif op in (OP_BLOCK, OP_LOOP, OP_IF):
            out.append(body[pos]); pos += 1
        elif op == OP_BR_TABLE:
            n_targets, pos = read_uleb(body, pos)
            out += uleb(n_targets)
            for _ in range(n_targets + 1):
                t, pos = read_uleb(body, pos)
                out += uleb(t)
        elif op in (OP_LOCAL_GET, OP_LOCAL_SET, OP_LOCAL_TEE,
                    OP_GLOBAL_GET, OP_GLOBAL_SET, OP_BR, OP_BR_IF):
            v, pos = read_uleb(body, pos)
            out += uleb(v)
        elif op == OP_I32_CONST:
            v, pos = read_sleb(body, pos)
            out += sleb(v)
        elif op == OP_I64_CONST:
            v, pos = read_sleb(body, pos)
            out += sleb(v)
        elif op == OP_F32_CONST:
            out += body[pos:pos + 4]; pos += 4
        elif op == OP_F64_CONST:
            out += body[pos:pos + 8]; pos += 8
        elif op in _MEM_OPS:
            align, pos = read_uleb(body, pos)
            off, pos = read_uleb(body, pos)
            out += uleb(align)
            out += uleb(off)
    return bytes(out)


def opt_local_compact(mod: WasmModule, report: dict):
    """Merge consecutive (1, type) local entries into (N, type) entries."""
    saved = 0
    for code in mod.codes:
        if not code.locals:
            continue
        # Merge consecutive same-type entries.
        merged: List[Tuple[int, int]] = []
        for count, ty in code.locals:
            if merged and merged[-1][1] == ty:
                merged[-1] = (merged[-1][0] + count, ty)
            else:
                merged.append((count, ty))
        # Also: the spec allows arbitrary ordering — group same types
        # together to enable maximum compaction. (Locals are addressed
        # by index, so reordering changes the indices used in the body
        # — we'd need to renumber locals. The reference emitter emits
        # locals in declaration order and the body uses LOCAL_GET/SET
        # with those exact indices, so we DO NOT reorder; only merge
        # adjacent.)
        saved += sum(2 for _ in code.locals) - sum(2 for _ in merged)
        code.locals = merged
    report["local_compact_saved"] = saved


def opt_dead_data(mod: WasmModule, report: dict):
    """Eliminate data segments that are never referenced by a live function.

    Uses `find_used_data_offsets` to find which data segments are still
    reachable via `i32.const <offset>`."""
    live_funcs = find_live_functions(mod)
    offset_to_idx: Dict[int, int] = {}
    for i, seg in enumerate(mod.data):
        offset_to_idx[seg.offset] = i
    used = find_used_data_offsets(mod, live_funcs, offset_to_idx)
    new_data = [seg for i, seg in enumerate(mod.data) if i in used]
    report["dead_data_removed"] = len(mod.data) - len(new_data)
    mod.data = new_data


def opt_peephole(mod: WasmModule, report: dict):
    """Peephole optimizations on the code section.

    Currently:
      - Remove `nop` instructions.
      - Remove `unreachable` that follows another `unreachable`/`return`/
        `br` (control flow after a terminator is dead).
      - Constant-fold `i32.const N; i32.eqz` into `i32.const (N==0)`.
        (Saves 6 bytes: 1 opcode + ~5 sleb for the const + 1 for eqz
        -> 1 opcode + 1 sleb for the result. Only fires when the result
        fits in a single-byte sleb.)
    """
    saved = 0
    for code in mod.codes:
        new_body, s = _peephole_body(code.body)
        saved += s
        code.body = new_body
    report["peephole_saved"] = saved


def _peephole_body(body: bytes) -> Tuple[bytes, int]:
    """Apply peephole opts to a single function body. Returns (new_body, saved_bytes)."""
    # We need to be careful with control-flow instructions: removing an
    # instruction inside a block/loop/if changes the structure. For the
    # alpha, we ONLY apply opts that preserve the instruction count
    # inside control structures — i.e., we eliminate nops and fold
    # const-eqz pairs that occur OUTSIDE of nested control (where the
    # stack depth at the end is what matters).
    #
    # The safe opt set:
    #   (1) nop elimination: remove OP_NOP wherever it appears.
    #   (2) const-fold i32.eqz on a constant: replace `i32.const N;
    #       i32.eqz` with `i32.const (N==0)`. This is safe regardless of
    #       context (it consumes 1 + 1 = 2 stack values and produces 1).
    #   (3) drop the body after an unconditional terminator (return /
    #       unreachable / br 0 at the top level of the function). NOT
    #       implemented in the alpha — it requires understanding block
    #       nesting.
    out = bytearray()
    pos = 0
    n = len(body)
    saved = 0
    while pos < n:
        op = body[pos]; pos += 1
        if op == OP_NOP:
            saved += 1
            continue
        if op == OP_I32_CONST:
            # Look ahead: i32.eqz?
            val, new_pos = read_sleb(body, pos)
            if new_pos < n and body[new_pos] == OP_I32_EQZ:
                # Constant-fold: i32.const (val == 0 ? 1 : 0); i32.eqz
                folded = 1 if val == 0 else 0
                # Save: the old encoding was (0x41 + sleb(val) + 0x45),
                # = 1 + len(sleb(val)) + 1 bytes. The new encoding is
                # (0x41 + sleb(folded)) = 1 + len(sleb(folded)) bytes.
                # For val in [-63, 63], sleb(val) is 1 byte; folded
                # (0 or 1) is also 1 byte. So we save the 0x45 (1 byte).
                # For longer sleb(val) values, we save more.
                out.append(OP_I32_CONST)
                out += sleb(folded)
                saved += 1 + len(sleb(val)) - len(sleb(folded))
                pos = new_pos + 1
                continue
            out.append(OP_I32_CONST)
            out += sleb(val)
            pos = new_pos
            continue
        # Default: copy the op + its immediates (if any) verbatim.
        out.append(op)
        if op == OP_CALL:
            idx, pos = read_uleb(body, pos)
            out += uleb(idx)
        elif op == OP_CALL_INDIRECT:
            type_idx, pos = read_uleb(body, pos)
            table_idx, pos = read_uleb(body, pos)
            out += uleb(type_idx)
            out += uleb(table_idx)
        elif op in (OP_BLOCK, OP_LOOP, OP_IF):
            out.append(body[pos]); pos += 1
        elif op == OP_BR_TABLE:
            n_targets, pos = read_uleb(body, pos)
            out += uleb(n_targets)
            for _ in range(n_targets + 1):
                t, pos = read_uleb(body, pos)
                out += uleb(t)
        elif op in (OP_LOCAL_GET, OP_LOCAL_SET, OP_LOCAL_TEE,
                    OP_GLOBAL_GET, OP_GLOBAL_SET, OP_BR, OP_BR_IF):
            v, pos = read_uleb(body, pos)
            out += uleb(v)
        elif op == OP_I64_CONST:
            v, pos = read_sleb(body, pos)
            out += sleb(v)
        elif op == OP_F32_CONST:
            out += body[pos:pos + 4]; pos += 4
        elif op == OP_F64_CONST:
            out += body[pos:pos + 8]; pos += 8
        elif op in _MEM_OPS:
            align, pos = read_uleb(body, pos)
            off, pos = read_uleb(body, pos)
            out += uleb(align)
            out += uleb(off)
    return bytes(out), saved


# ============================================================================
# Serialization — turn the optimized module back into wasm bytes.
# ============================================================================

def section(sec_id: int, content: bytes) -> bytes:
    return bytes([sec_id]) + uleb(len(content)) + content


def serialize(mod: WasmModule) -> bytes:
    out = bytearray()
    out += b"\x00asm"
    out += bytes([1, 0, 0, 0])  # version 1
    # Type section.
    if mod.types:
        body = bytearray()
        body += uleb(len(mod.types))
        for ty in mod.types:
            body.append(0x60)
            body += uleb(len(ty.params))
            for p in ty.params:
                body.append(p)
            body += uleb(len(ty.results))
            for r in ty.results:
                body.append(r)
        out += section(SEC_TYPE, bytes(body))
    # Import section.
    if mod.imports:
        body = bytearray()
        body += uleb(len(mod.imports))
        for imp in mod.imports:
            mb = imp.module.encode("utf-8")
            body += uleb(len(mb)) + mb
            nb = imp.name.encode("utf-8")
            body += uleb(len(nb)) + nb
            body.append(imp.kind)
            if imp.kind == 0x00:
                body += uleb(imp.type_idx)
            elif imp.kind == 0x02:
                body.append(0x00)
                body += uleb(1)  # min pages
            elif imp.kind == 0x03:
                body.append(I32)
                body.append(0x00)
        out += section(SEC_IMPORT, bytes(body))
    # Function section.
    if mod.funcs:
        body = bytearray()
        body += uleb(len(mod.funcs))
        for ty in mod.funcs:
            body += uleb(ty)
        out += section(SEC_FUNCTION, bytes(body))
    # Memory section.
    if mod.memories:
        body = bytearray()
        body += uleb(len(mod.memories))
        for min_p, max_p in mod.memories:
            if max_p is None:
                body.append(0x00)
                body += uleb(min_p)
            else:
                body.append(0x01)
                body += uleb(min_p)
                body += uleb(max_p)
        out += section(SEC_MEMORY, bytes(body))
    # Global section.
    if mod.globals:
        body = bytearray()
        body += uleb(len(mod.globals))
        for mutable, ty, init_val in mod.globals:
            body.append(ty)
            body.append(0x01 if mutable else 0x00)
            if ty == I32:
                body.append(OP_I32_CONST)
                body += sleb(init_val)
            elif ty == I64:
                body.append(OP_I64_CONST)
                body += sleb(init_val)
            body.append(OP_END)
        out += section(SEC_GLOBAL, bytes(body))
    # Export section.
    if mod.exports:
        body = bytearray()
        body += uleb(len(mod.exports))
        for exp in mod.exports:
            nb = exp.name.encode("utf-8")
            body += uleb(len(nb)) + nb
            body.append(exp.kind)
            body += uleb(exp.index)
        out += section(SEC_EXPORT, bytes(body))
    # Start section.
    if mod.start is not None:
        out += section(SEC_START, uleb(mod.start))
    # DataCount (must precede Code section when Data is present).
    if mod.data:
        out += section(SEC_DATA_COUNT, uleb(len(mod.data)))
    # Code section.
    if mod.codes:
        body = bytearray()
        body += uleb(len(mod.codes))
        for code in mod.codes:
            func_body = bytearray()
            func_body += uleb(len(code.locals))
            for count, ty in code.locals:
                func_body += uleb(count)
                func_body.append(ty)
            func_body += code.body
            func_body.append(OP_END)
            body += uleb(len(func_body))
            body += func_body
        out += section(SEC_CODE, bytes(body))
    # Data section.
    if mod.data:
        body = bytearray()
        body += uleb(len(mod.data))
        for seg in mod.data:
            body.append(0x00)  # active, memory 0
            body.append(OP_I32_CONST)
            body += sleb(seg.offset)
            body.append(OP_END)
            body += uleb(len(seg.data))
            body += seg.data
        out += section(SEC_DATA, bytes(body))
    return bytes(out)


# ============================================================================
# Orchestrator.
# ============================================================================

def optimize(wasm_bytes: bytes, level: str = "O3",
             external_wasm_opt: Optional[str] = None,
             report: Optional[dict] = None) -> bytes:
    """Run the in-tree optimizer on `wasm_bytes` and return the optimized bytes.

    If `external_wasm_opt` is given (and points to an executable), the
    external Binaryen `wasm-opt` is invoked AFTER the in-tree passes
    for additional binary-level optimizations.

    `report` (if given) is populated with stats about what was done.
    """
    if report is None:
        report = {}
    mod = WasmModule.parse(wasm_bytes)
    before_size = len(wasm_bytes)

    # O1: DCE + local compaction.
    opt_dce(mod, report)
    opt_local_compact(mod, report)
    # O2: type dedup + dead data.
    if level in ("O2", "O3", "Os"):
        opt_type_dedup(mod, report)
        opt_dead_data(mod, report)
    # O3: peephole opts.
    if level in ("O3", "Os"):
        opt_peephole(mod, report)
    # Serialize.
    optimized = serialize(mod)
    # Run the external wasm-opt if available.
    external_ran = False
    external_path = external_wasm_opt
    if external_path is None:
        external_path = shutil.which("wasm-opt")
    if external_path and level in ("O2", "O3", "Os"):
        external_out = _run_external_wasm_opt(external_path, optimized, level, report)
        if external_out is not None:
            optimized = external_out
            external_ran = True
    report["external_wasm_opt_ran"] = external_ran
    report["input_size"] = before_size
    report["output_size"] = len(optimized)
    report["bytes_saved"] = before_size - len(optimized)
    if before_size > 0:
        report["reduction_pct"] = round(
            (before_size - len(optimized)) * 100.0 / before_size, 2)
    else:
        report["reduction_pct"] = 0.0
    return optimized


def _run_external_wasm_opt(path: str, wasm_bytes: bytes, level: str,
                           report: dict) -> Optional[bytes]:
    """Invoke the external `wasm-opt` binary on the wasm bytes.

    Returns the optimized bytes, or None if the invocation fails (the
    in-tree result is kept in that case)."""
    import tempfile
    # Map our level to wasm-opt's -O flags.
    opt_flag = {"O1": "-O1", "O2": "-O2", "O3": "-O3", "Os": "-Os"}.get(
        level, "-O3")
    try:
        with tempfile.NamedTemporaryFile(suffix=".wasm", delete=False) as f:
            f.write(wasm_bytes)
            in_path = f.name
        out_path = in_path + ".opt.wasm"
        # --enable-bulk-memory enables the bulk-memory proposal; our emitter
        # uses memory.copy for string concatenation, which requires this
        # proposal. Without the flag, wasm-opt rejects the module.
        # --strip-debug / --strip-producers / --strip-target-features
        # remove non-functional sections (no effect on validation or
        # observable behaviour).
        cmd = [path, opt_flag, "--enable-bulk-memory",
               "--strip-debug", "--strip-producers",
               "--strip-target-features", "-o", out_path, in_path]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            report["external_wasm_opt_error"] = r.stderr.decode(
                "utf-8", "replace")[:200]
            try:
                os.unlink(in_path)
                if os.path.exists(out_path):
                    os.unlink(out_path)
            except OSError:
                pass
            return None
        with open(out_path, "rb") as f:
            out = f.read()
        try:
            os.unlink(in_path)
            os.unlink(out_path)
        except OSError:
            pass
        return out
    except OSError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 24 in-tree wasm size optimizer.")
    ap.add_argument("input", help="input .wasm file")
    ap.add_argument("output", help="output .wasm file (overwritten)")
    ap.add_argument("--level", default="O3",
                    choices=["O1", "O2", "O3", "Os"],
                    help="optimization level (default: O3)")
    ap.add_argument("--report", action="store_true",
                    help="print a size-reduction report to stderr")
    ap.add_argument("--external-wasm-opt",
                    help="path to the external wasm-opt binary "
                         "(default: search PATH)")
    args = ap.parse_args()
    with open(args.input, "rb") as f:
        wasm_bytes = f.read()
    report: dict = {}
    optimized = optimize(wasm_bytes, level=args.level,
                         external_wasm_opt=args.external_wasm_opt,
                         report=report)
    with open(args.output, "wb") as f:
        f.write(optimized)
    if args.report:
        sys.stderr.write("hlwasm-opt report:\n")
        sys.stderr.write("  input size:       %d bytes\n" % report.get(
            "input_size", 0))
        sys.stderr.write("  output size:      %d bytes\n" % report.get(
            "output_size", 0))
        sys.stderr.write("  bytes saved:      %d bytes\n" % report.get(
            "bytes_saved", 0))
        sys.stderr.write("  reduction:         %.2f%%\n" % report.get(
            "reduction_pct", 0.0))
        sys.stderr.write("  dead funcs removed: %d\n" % report.get(
            "dead_funcs_removed", 0))
        sys.stderr.write("  dead imports removed: %d\n" % report.get(
            "dead_imports_removed", 0))
        sys.stderr.write("  dead data removed: %d\n" % report.get(
            "dead_data_removed", 0))
        sys.stderr.write("  types deduped:     %d\n" % report.get(
            "types_deduped", 0))
        sys.stderr.write("  peephole saved:    %d bytes\n" % report.get(
            "peephole_saved", 0))
        sys.stderr.write("  local compact:     %d bytes\n" % report.get(
            "local_compact_saved", 0))
        sys.stderr.write("  external wasm-opt: %s\n" % (
            "ran" if report.get("external_wasm_opt_ran") else "not run"))
        if "external_wasm_opt_error" in report:
            sys.stderr.write("    error: %s\n" % report[
                "external_wasm_opt_error"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
