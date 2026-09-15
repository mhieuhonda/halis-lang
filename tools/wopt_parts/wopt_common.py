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
# Deep-scan-13: table.get / table.set (uleb table index immediate) —
# used by the strict body walker (_walk_instrs).
OP_TABLE_GET = 0x25
OP_TABLE_SET = 0x26
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



__all__ = [
    "Dict",
    "EXTERNREF",
    "F32",
    "F64",
    "FUNCREF",
    "I32",
    "I64",
    "List",
    "OP_BLOCK",
    "OP_BR",
    "OP_BR_IF",
    "OP_BR_TABLE",
    "OP_CALL",
    "OP_CALL_INDIRECT",
    "OP_DROP",
    "OP_ELSE",
    "OP_END",
    "OP_F32_CONST",
    "OP_F32_LOAD",
    "OP_F32_STORE",
    "OP_F64_CONST",
    "OP_F64_LOAD",
    "OP_F64_STORE",
    "OP_GLOBAL_GET",
    "OP_GLOBAL_SET",
    "OP_I32_CONST",
    "OP_I32_EQZ",
    "OP_I32_LOAD",
    "OP_I32_LOAD16_S",
    "OP_I32_LOAD16_U",
    "OP_I32_LOAD8_S",
    "OP_I32_LOAD8_U",
    "OP_I32_STORE",
    "OP_I32_STORE16",
    "OP_I32_STORE8",
    "OP_I64_CONST",
    "OP_I64_LOAD",
    "OP_I64_LOAD16_S",
    "OP_I64_LOAD16_U",
    "OP_I64_LOAD32_S",
    "OP_I64_LOAD32_U",
    "OP_I64_LOAD8_S",
    "OP_I64_LOAD8_U",
    "OP_I64_STORE",
    "OP_I64_STORE16",
    "OP_I64_STORE32",
    "OP_I64_STORE8",
    "OP_IF",
    "OP_LOCAL_GET",
    "OP_LOCAL_SET",
    "OP_LOCAL_TEE",
    "OP_LOOP",
    "OP_MEMORY_GROW",
    "OP_MEMORY_SIZE",
    "OP_NOP",
    "OP_RETURN",
    "OP_TABLE_GET",
    "OP_TABLE_SET",
    "OP_UNREACHABLE",
    "Optional",
    "SEC_CODE",
    "SEC_DATA",
    "SEC_DATA_COUNT",
    "SEC_ELEMENT",
    "SEC_EXPORT",
    "SEC_FUNCTION",
    "SEC_GLOBAL",
    "SEC_IMPORT",
    "SEC_MEMORY",
    "SEC_START",
    "SEC_TABLE",
    "SEC_TYPE",
    "Set",
    "Tuple",
    "_MEM_OPS",
    "annotations",
    "argparse",
    "os",
    "read_sleb",
    "read_uleb",
    "shutil",
    "sleb",
    "struct",
    "subprocess",
    "sys",
    "uleb",
]
