#!/usr/bin/env python3
"""hlwasm.py — Stage 23 (v0.42.0-alpha): WebAssembly backend.

Emits a `.wasm` binary DIRECTLY from a checked HLS program — no LLVM
toolchain needed. The roadmap calls for "emit .wasm directly from the
HLIR via a new backend (bypass LLVM for the smallest binaries; use
LLVM for the fastest)"; this is the direct emitter (smallest binaries,
zero external dependencies, always available).

Supported targets (the roadmap's Stage 23 set):

  wasm32-unknown-unknown      Freestanding wasm32. The module imports a
                              small set of JS functions (print, println,
                              float-to-str) from module "env" and exports
                              `_start` + `hl_alloc` + `memory`. Runs in
                              any Wasm host (browser, Node.js, wasmtime).
  wasm32-unknown-emscripten   Stage 24 will add full emscripten libc
                              access; for the alpha this target falls
                              back to the freestanding backend and prints
                              a note (no libc access beyond the small JS
                              import set).

Type mapping (wasm32):
  int    -> i64   (HLS int is 64-bit; wasm32 supports i64 natively)
  float  -> f64
  bool   -> i32
  void   -> (no result)
  str    -> i32   (pointer to {i32 len, i8 data[len]} in linear memory)
  list[T]/map/struct/enum -> i32 (pointer; alpha raises a clean error
                                 for these — full support lands with
                                 the HLIR-based emitter in Stage 24).

`extern "js"` blocks:
  extern "js" {
      fn console.log(s: str) -> void uses IO
      fn fetch(url: str) -> str uses IO
  }
  Each declared fn becomes a wasm IMPORT from module "env" with the
  SAME name (the JS glue must provide a function of that name in the
  import object). This is the Stage 23 `std.jsffi` mechanism.

Acceptance (Stage 23): `examples/hello.hls` compiles to a < 10 KB wasm
binary that prints "Hello, World!" in a browser.

Usage:
  python3 tools/hlwasm.py <input.hls> <output_base>
                         [--target wasm32-unknown-unknown]
                         [--wasm] [--js] [--html] [--run]
                         [--list-targets] [--show-size]

Examples:
  python3 tools/hlwasm.py examples/hello.hls /tmp/hello
       writes /tmp/hello.wasm + /tmp/hello.js + /tmp/hello.html
  python3 tools/hlwasm.py examples/hello.hls /tmp/hello --run
       compiles + runs the wasm in Node.js (if available), printing
       the program's stdout.
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple

# Resolve HLError whether we are imported from boot.py (sys.path has the
# repo root) or run directly from the tools/ directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from boot.lexer import HLError  # type: ignore
except ImportError:  # pragma: no cover
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from boot.lexer import HLError  # type: ignore


# ============================================================================
# LEB128 + wasm binary primitives
# ============================================================================

def uleb(n: int) -> bytes:
    """Encode a non-negative integer as unsigned LEB128."""
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
    """Encode an integer as signed LEB128."""
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


# WASM value types.
I32 = 0x7F
I64 = 0x7E
F32 = 0x7D
F64 = 0x7C

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

# WASM instructions we use. (Single-byte opcodes unless noted.)
OP_UNREACHABLE = 0x00
OP_NOP = 0x01
OP_BLOCK = 0x02
OP_LOOP = 0x03
OP_IF = 0x04
OP_ELSE = 0x05
OP_END = 0x0B
OP_BR = 0x0C
OP_BR_IF = 0x0D
OP_RETURN = 0x0F
OP_CALL = 0x10
OP_DROP = 0x1A
OP_LOCAL_GET = 0x20
OP_LOCAL_SET = 0x21
OP_LOCAL_TEE = 0x22
OP_GLOBAL_GET = 0x23
OP_GLOBAL_SET = 0x24
OP_I32_LOAD = 0x28
OP_I64_LOAD = 0x29
OP_I32_LOAD8_U = 0x2D
OP_I32_STORE = 0x36
OP_I64_STORE = 0x37
OP_I32_STORE8 = 0x3A
OP_MEMORY_SIZE = 0x3F
OP_MEMORY_GROW = 0x40
OP_I32_CONST = 0x41
OP_I64_CONST = 0x42
OP_F64_CONST = 0x44
OP_I32_EQZ = 0x45
OP_I32_EQ = 0x46
OP_I32_NE = 0x47
OP_I32_LT_S = 0x48
OP_I32_GT_S = 0x4A
OP_I32_LE_S = 0x4C
OP_I32_GE_S = 0x4E
OP_I64_EQZ = 0x50
OP_I64_EQ = 0x51
OP_I64_NE = 0x52
OP_I64_LT_S = 0x53
OP_I64_LT_U = 0x54
OP_I64_GT_S = 0x55
OP_I64_GT_U = 0x56
OP_I64_LE_S = 0x57
OP_I64_LE_U = 0x58
OP_I64_GE_S = 0x59
OP_I64_GE_U = 0x5A
OP_I32_ADD = 0x6A
OP_I32_SUB = 0x6B
OP_I32_MUL = 0x6C
OP_I32_AND = 0x71
OP_I32_OR = 0x72
OP_I64_ADD = 0x7C
OP_I64_SUB = 0x7D
OP_I64_MUL = 0x7E
OP_I64_DIV_S = 0x7F
OP_I64_DIV_U = 0x80
OP_I64_REM_S = 0x81
OP_I64_REM_U = 0x82
OP_I64_AND = 0x83
OP_I64_OR = 0x84
OP_I64_XOR = 0x85
OP_F64_ADD = 0xA0
OP_F64_SUB = 0xA1
OP_F64_MUL = 0xA2
OP_F64_DIV = 0xA3
OP_F64_EQ = 0x61
OP_F64_NE = 0x62
OP_F64_LT = 0x63
OP_F64_LE = 0x64
OP_F64_GT = 0x65
OP_F64_GE = 0x66
OP_I32_WRAP_I64 = 0xA7
OP_I64_EXTEND_I32_S = 0xAC
# Stage 24 fix: correct wasm conversion opcodes per the spec.
#   i32.trunc_f64_s   = 0xAA
#   i64.trunc_f64_s   = 0xB0
#   f64.convert_i32_s = 0xB7
#   f64.convert_i64_s = 0xB9
#   f64.reinterpret_i64 = 0xBE  (NOT used here; for reference)
#   i64.reinterpret_f64  = 0xBC  (NOT used here; for reference)
OP_I64_TRUNC_F64_S = 0xB0
OP_F64_CONVERT_I64_S = 0xB9

# Block type bytes (used after OP_BLOCK / OP_LOOP / OP_IF).
BLOCK_VOID = 0x40


def section(sec_id: int, content: bytes) -> bytes:
    """Wrap a section payload in its (id, size, content) framing."""
    return bytes([sec_id]) + uleb(len(content)) + content


# ============================================================================
# HLS type -> wasm value-type mapping
# ============================================================================

def _taint_inner(t: str) -> str:
    if t.startswith("tainted["):
        return t[8:-1]
    return t


def hls_to_wasm_valtype(t: str) -> int:
    """Map an HLS type to a single wasm value type byte.

    Returns I32 for str/list/struct/etc. (they are all pointers in the
    wasm32 ABI), and I64 for int, F64 for float, I32 for bool.
    """
    t = _taint_inner(t)
    if t == "int":
        return I64
    if t == "float":
        return F64
    if t == "bool":
        return I32
    if t == "str":
        return I32  # pointer
    if t.startswith("list[") or t.startswith("map["):
        return I32  # pointer
    if t in ("void",):
        raise ValueError("void has no value type")
    # struct / enum / generic instantiated -> pointer
    return I32


def hls_to_wasm_result(t: str) -> List[int]:
    """Return the list of result value types for an HLS function."""
    if t == "void":
        return []
    return [hls_to_wasm_valtype(t)]


# ============================================================================
# WasmModule — accumulates sections and serialises to bytes
# ============================================================================



__all__ = [
    "BLOCK_VOID",
    "Dict",
    "F32",
    "F64",
    "HLError",
    "I32",
    "I64",
    "List",
    "OP_BLOCK",
    "OP_BR",
    "OP_BR_IF",
    "OP_CALL",
    "OP_DROP",
    "OP_ELSE",
    "OP_END",
    "OP_F64_ADD",
    "OP_F64_CONST",
    "OP_F64_CONVERT_I64_S",
    "OP_F64_DIV",
    "OP_F64_EQ",
    "OP_F64_GE",
    "OP_F64_GT",
    "OP_F64_LE",
    "OP_F64_LT",
    "OP_F64_MUL",
    "OP_F64_NE",
    "OP_F64_SUB",
    "OP_GLOBAL_GET",
    "OP_GLOBAL_SET",
    "OP_I32_ADD",
    "OP_I32_AND",
    "OP_I32_CONST",
    "OP_I32_EQ",
    "OP_I32_EQZ",
    "OP_I32_GE_S",
    "OP_I32_GT_S",
    "OP_I32_LE_S",
    "OP_I32_LOAD",
    "OP_I32_LOAD8_U",
    "OP_I32_LT_S",
    "OP_I32_MUL",
    "OP_I32_NE",
    "OP_I32_OR",
    "OP_I32_STORE",
    "OP_I32_STORE8",
    "OP_I32_SUB",
    "OP_I32_WRAP_I64",
    "OP_I64_ADD",
    "OP_I64_AND",
    "OP_I64_CONST",
    "OP_I64_DIV_S",
    "OP_I64_DIV_U",
    "OP_I64_EQ",
    "OP_I64_EQZ",
    "OP_I64_EXTEND_I32_S",
    "OP_I64_GE_S",
    "OP_I64_GE_U",
    "OP_I64_GT_S",
    "OP_I64_GT_U",
    "OP_I64_LE_S",
    "OP_I64_LE_U",
    "OP_I64_LOAD",
    "OP_I64_LT_S",
    "OP_I64_LT_U",
    "OP_I64_MUL",
    "OP_I64_NE",
    "OP_I64_OR",
    "OP_I64_REM_S",
    "OP_I64_REM_U",
    "OP_I64_STORE",
    "OP_I64_SUB",
    "OP_I64_TRUNC_F64_S",
    "OP_I64_XOR",
    "OP_IF",
    "OP_LOCAL_GET",
    "OP_LOCAL_SET",
    "OP_LOCAL_TEE",
    "OP_LOOP",
    "OP_MEMORY_GROW",
    "OP_MEMORY_SIZE",
    "OP_NOP",
    "OP_RETURN",
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
    "_REPO_ROOT",
    "_taint_inner",
    "annotations",
    "argparse",
    "hls_to_wasm_result",
    "hls_to_wasm_valtype",
    "os",
    "section",
    "sleb",
    "struct",
    "subprocess",
    "sys",
    "uleb",
]
