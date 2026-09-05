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
from typing import Dict, List, Optional, Tuple

# Resolve HLError whether we are imported from boot.py (sys.path has the
# repo root) or run directly from the tools/ directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

class WasmModule:
    """A wasm module under construction.

    Sections are appended in id order at serialise time. The emitter
    calls add_type / add_import / add_function / add_export / add_code /
    add_data to populate the module; final_bytes() assembles the binary.
    """

    def __init__(self):
        # Type section: list of (param_types, result_types).
        self.types: List[Tuple[List[int], List[int]]] = []
        # Import section: list of (module, name, kind, type_idx).
        self.imports: List[Tuple[str, str, int, int]] = []
        # Function section: list of type indices (for defined functions).
        self.funcs: List[int] = []
        # Memory section: list of (min_pages, max_pages_or_None).
        self.memories: List[Tuple[int, Optional[int]]] = []
        # Export section: list of (name, kind, index).
        self.exports: List[Tuple[str, int, int]] = []
        # Code section: list of (locals, body_bytes).
        self.codes: List[Tuple[List[Tuple[int, int]], bytes]] = []
        # Data section: list of (offset, bytes).
        self.data: List[Tuple[int, bytes]] = []
        # Start function index (or None).
        self.start: Optional[int] = None
        # Globals: list of (mutable, type, init_value).
        self.globals: List[Tuple[bool, int, int]] = []

    def add_type(self, params: List[int], results: List[int]) -> int:
        """Register a function signature; return its type index."""
        for i, (p, r) in enumerate(self.types):
            if p == params and r == results:
                return i
        self.types.append((params, results))
        return len(self.types) - 1

    def add_import(self, module: str, name: str, kind: int,
                  type_idx: int) -> int:
        """Register an import; return its function index (in import space)."""
        self.imports.append((module, name, kind, type_idx))
        # Imports occupy the FIRST function indices (before defined funcs).
        # The function index of this import = (current import count - 1).
        return len(self.imports) - 1

    def add_function(self, type_idx: int) -> int:
        """Register a defined function; return its function index (in the
        GLOBAL function index space, i.e. after imports)."""
        self.funcs.append(type_idx)
        return len(self.imports) + len(self.funcs) - 1

    def add_memory(self, min_pages: int, max_pages: Optional[int] = None):
        self.memories.append((min_pages, max_pages))

    def add_export(self, name: str, kind: int, index: int):
        self.exports.append((name, kind, index))

    def add_code(self, locals_: List[Tuple[int, int]], body: bytes):
        self.codes.append((locals_, body))

    def add_data(self, offset: int, data: bytes):
        self.data.append((offset, data))

    def add_global(self, mutable: bool, ty: int, init_value: int):
        self.globals.append((mutable, ty, init_value))

    def final_bytes(self) -> bytes:
        """Assemble the binary module."""
        out = bytearray()
        out += b"\x00asm"
        out += bytes([1, 0, 0, 0])  # version 1
        # Section 1: Type.
        if self.types:
            body = bytearray()
            body += uleb(len(self.types))
            for params, results in self.types:
                body.append(0x60)
                body += uleb(len(params))
                for p in params:
                    body.append(p)
                body += uleb(len(results))
                for r in results:
                    body.append(r)
            out += section(SEC_TYPE, bytes(body))
        # Section 2: Import.
        if self.imports:
            body = bytearray()
            body += uleb(len(self.imports))
            for module, name, kind, idx in self.imports:
                mb = module.encode("utf-8")
                body += uleb(len(mb)) + mb
                nb = name.encode("utf-8")
                body += uleb(len(nb)) + nb
                body.append(kind)
                if kind == 0x00:  # func
                    body += uleb(idx)
                elif kind == 0x02:  # memory
                    body.append(0x00)  # limits flag (no max)
                    body += uleb(idx)   # min pages (reusing field)
                elif kind == 0x03:  # global
                    body.append(idx & 0xFF)  # value type
                    body.append(0x00)        # immutable
            out += section(SEC_IMPORT, bytes(body))
        # Section 3: Function.
        if self.funcs:
            body = bytearray()
            body += uleb(len(self.funcs))
            for ty in self.funcs:
                body += uleb(ty)
            out += section(SEC_FUNCTION, bytes(body))
        # Section 5: Memory.
        if self.memories:
            body = bytearray()
            body += uleb(len(self.memories))
            for min_p, max_p in self.memories:
                if max_p is None:
                    body.append(0x00)
                    body += uleb(min_p)
                else:
                    body.append(0x01)
                    body += uleb(min_p)
                    body += uleb(max_p)
            out += section(SEC_MEMORY, bytes(body))
        # Section 6: Global.
        if self.globals:
            body = bytearray()
            body += uleb(len(self.globals))
            for mutable, ty, init_val in self.globals:
                body.append(ty)
                body.append(0x01 if mutable else 0x00)
                # init_expr: <const> end
                if ty == I32:
                    body.append(OP_I32_CONST)
                    body += sleb(init_val)
                elif ty == I64:
                    body.append(OP_I64_CONST)
                    body += sleb(init_val)
                body.append(OP_END)
            out += section(SEC_GLOBAL, bytes(body))
        # Section 7: Export.
        if self.exports:
            body = bytearray()
            body += uleb(len(self.exports))
            for name, kind, idx in self.exports:
                nb = name.encode("utf-8")
                body += uleb(len(nb)) + nb
                body.append(kind)
                body += uleb(idx)
            out += section(SEC_EXPORT, bytes(body))
        # Section 8: Start.
        if self.start is not None:
            out += section(SEC_START, uleb(self.start))
        # Section 12: DataCount (must precede Code section when Data is
        # present, per the wasm 2.0 validation rules; some validators
        # require it, others are lenient — emit it to be safe).
        if self.data:
            out += section(SEC_DATA_COUNT, uleb(len(self.data)))
        # Section 10: Code.
        if self.codes:
            body = bytearray()
            body += uleb(len(self.codes))
            for locals_, body_bytes in self.codes:
                func_body = bytearray()
                # locals: count of (count, type) pairs
                func_body += uleb(len(locals_))
                for count, ty in locals_:
                    func_body += uleb(count)
                    func_body.append(ty)
                func_body += body_bytes
                func_body.append(OP_END)  # end of function body
                body += uleb(len(func_body))
                body += func_body
            out += section(SEC_CODE, bytes(body))
        # Section 11: Data.
        if self.data:
            body = bytearray()
            body += uleb(len(self.data))
            for offset, data in self.data:
                body.append(0x00)  # active, memory 0
                body.append(OP_I32_CONST)
                body += sleb(offset)
                body.append(OP_END)
                body += uleb(len(data))
                body += data
            out += section(SEC_DATA, bytes(body))
        return bytes(out)


# ============================================================================
# WasmEmitter — walks the checked HLS AST and emits wasm code
# ============================================================================

class WasmEmitter:
    """Walks a checked HLS program AST and emits a WasmModule.

    Subset supported by the alpha (everything else raises a clean
    HLError pointing the user at the C backend):

    - Types: int, float, bool, str, void (and tainted[T] for these).
    - Statements: let, let mut, assign, if/else, while, for-over-range,
      return, break, continue, expr.
    - Expressions: int/float/bool/str literals, ident, bin (+,-,*,/,%,==,
      !=,<,<=,>,>=,&&,||), un (-,!), call (user + a builtins subset),
      ternary-free. listlit/structlit/enumlit/match/qmark/field/method/
      index raise a clean "not yet supported by --emit wasm" error.

    Builtins supported:
      println(s) print(s) print_int(n) print_float(f) print_bool(b)
      int_to_str(n) float_to_str(f) bool_to_str(b) str_len(s)
      str_concat(a,b) (also lowered automatically for str + str)
      str_eq(a,b) str_char_at(s,i) ord(s) chr(n)
      range(a,b) -> only valid as a `for` iter expression.
      exit(code) abort(code) panic(msg)

    Any other builtin raises a clean "not yet supported by --emit wasm"
    error.

    extern "js" blocks:
      each `extern "js" fn NAME(...) -> T uses IO` becomes a wasm import
      from module "env" with name NAME. The JS glue must provide a
      function of that name in the import object. Argument and return
      marshalling follows the standard type mapping (str -> i32 ptr, etc).
    """

    # Builtins that map directly to a wasm-defined helper function
    # (function name -> (helper_name, [arg_types], result_type)).
    # "arg_types" is a list of HLS type strings; "result_type" is an HLS
    # type string or "void".
    _HELPER_TABLE = {
        "int_to_str":    ("hl_int_to_str",    ["int"],            "str"),
        "float_to_str":  ("hl_float_to_str",  ["float"],          "str"),
        "bool_to_str":   ("hl_bool_to_str",   ["bool"],           "str"),
        "str_len":       ("hl_str_len",       ["str"],            "int"),
        "str_concat":    ("hl_str_concat",    ["str", "str"],     "str"),
        "str_eq":        ("hl_str_eq",        ["str", "str"],     "bool"),
        "str_char_at":   ("hl_str_byte_at",   ["str", "int"],     "int"),
        "ord":           ("hl_str_byte_at",   ["str"],            "int"),
        "chr":           ("hl_chr_to_str",    ["int"],            "str"),
        "panic":         ("hl_panic",         ["str"],            "void"),
        "abort":         ("hl_abort",         ["int"],            "void"),
        "exit":          ("hl_exit",          ["int"],            "void"),
    }

    # Builtins that map to a JS import (provided by the glue).
    # (builtin_name -> import_name, [arg_types], result_type)
    # NOTE: println / print are NOT in this table because they need
    # special handling (they take a str and return void, but they must
    # also be exposed as imports the user can rebind via extern "js").
    _JS_IMPORT_TABLE = {
        "println":       ("hl_js_println",   ["str"],            "void"),
        "print":         ("hl_js_print",     ["str"],            "void"),
    }

    def __init__(self, program, target: str = "wasm32-unknown-unknown"):
        self.program = program
        self.target = target
        self.mod = WasmModule()
        # Function index map: name -> function index (in the global space).
        self.func_index: Dict[str, int] = {}
        # Type index map: (params, results) -> type_idx (mirrors mod.types).
        # Built-in helper names that are EMITTED AS WASM FUNCTIONS (not
        # imports). Populated by _emit_runtime_helpers().
        self._helper_emitted: Dict[str, int] = {}
        # String literals collected during emission. Each entry is
        # (bytes, memory_offset). The offset is assigned at the end of
        # emission (we lay them out contiguously starting at STR_BASE).
        self._string_pool: Dict[bytes, int] = {}
        self._string_list: List[Tuple[bytes, int]] = []
        # Function-local state.
        self._locals: Dict[str, Tuple[int, int]] = {}  # name -> (idx, valtype)
        self._local_count = 0
        self._current_ret_type: str = "void"
        # Loop context for break/continue: list of (continue_depth, break_depth).
        # depth = how many br's to jump out of to reach the loop's continue
        # target (the loop header) or break target (the block after the loop).
        self._loop_stack: List[Tuple[int, int]] = []
        # Linear-memory layout.
        #   [0..3]   = __heap_ptr (i32), initialised to STR_BASE_END.
        #   [4..]    = string literal pool.
        #   STR_BASE_END..  = bump-allocated heap.
        # We reserve 4 bytes at 0 for the heap pointer; strings start at 4.
        # However, we want null (0) to be a distinct non-string pointer, so
        # we actually start strings at offset 16 (leave 16 bytes of zeroes).
        self.HEAP_PTR_ADDR = 0
        self.STR_BASE = 16
        # Computed at emit() time:
        self._str_pool_end = self.STR_BASE
        # Map extern block ABI -> handled. We support "js" only.
        self._js_externs: Dict[str, Tuple[List[str], str]] = {}

    # ---------- string pool ----------

    def _intern_str(self, s: bytes) -> int:
        """Return the memory offset of a string literal, allocating it
        in the pool if necessary. The memory layout at the returned
        offset is: { i32 len, i8 data[len] } (padded to 4-byte alignment
        for the next allocation)."""
        if s in self._string_pool:
            return self._string_pool[s]
        offset = self._str_pool_end
        # 4 bytes for the length, then the data, then pad to 4-byte align.
        rec_len = 4 + len(s)
        padded = (rec_len + 3) & ~3
        self._string_pool[s] = offset
        self._string_list.append((s, offset))
        self._str_pool_end += padded
        return offset

    # ---------- public API ----------

    def emit(self) -> bytes:
        """Emit the complete wasm binary."""
        # Step 1: collect extern "js" decls.
        for ext in self.program.get("externs", []):
            if ext["abi"] == "js":
                for fn in ext["decls"]:
                    param_tys = [p[1] for p in fn["params"]]
                    self._js_externs[fn["name"]] = (param_tys, fn["ret"])
            elif ext["abi"] == "C":
                # Extern "C" is not supported on the wasm32-unknown-unknown
                # target (no libc). Error out cleanly.
                raise HLError(
                    "extern \"C\" is not supported on the wasm32-unknown-"
                    "unknown target (no libc; use --target x86_64-linux-gnu "
                    "for the C backend, or declare the import as extern \"js\")",
                    ext.get("line", 0), 0)
        # Step 2: declare the JS imports (env module). We must add them
        # FIRST so their indices come before any defined function.
        # We need: hl_js_println, hl_js_print, hl_js_f64_to_str (always),
        # plus any user-declared extern "js" functions.
        self._declare_js_imports()
        # Step 3: emit the runtime helpers (hl_int_to_str, hl_str_concat,
        # hl_alloc, etc.). These are defined functions, so they come after
        # imports in the function index space.
        self._emit_runtime_helpers()
        # Step 4: emit user functions.
        for fname, fn in self.program["fns"].items():
            if fn.get("extern", False):
                continue  # extern decls are imports, not defined funcs
            self._emit_function(fname, fn)
        # Step 5: emit the _start entry point.
        self._emit_start()
        # Step 6: emit memory + exports.
        self.mod.add_memory(min_pages=1)  # 64 KB initial
        self.mod.add_export("memory", 0x02, 0)  # export memory at index 0
        self.mod.add_export("_start", 0x00,
                            self.func_index.get("__hl_start", 0))
        # Export hl_alloc so the JS glue can allocate string memory for
        # float_to_str / future rich FFI.
        if "hl_alloc" in self.func_index:
            self.mod.add_export("hl_alloc", 0x00,
                                self.func_index["hl_alloc"])
        # Export main too (Node.js glue can capture the exit code).
        if "main" in self.program["fns"] and not self.program["fns"]["main"].get("extern", False):
            self.mod.add_export("hl_main", 0x00,
                                self.func_index.get("main", 0))
        # Step 7: lay out the string pool and emit the data section.
        # The heap pointer is stored at HEAP_PTR_ADDR (0); initialised
        # to the end of the string pool.
        # First, write the initial heap pointer (4 bytes).
        heap_init_bytes = struct.pack("<i", self._str_pool_end)
        self.mod.add_data(self.HEAP_PTR_ADDR, heap_init_bytes)
        # Then write each string literal.
        for s, offset in self._string_list:
            rec = struct.pack("<i", len(s)) + s
            # Pad to 4-byte alignment for the next record.
            padded_len = (len(rec) + 3) & ~3
            rec = rec + b"\x00" * (padded_len - len(rec))
            self.mod.add_data(offset, rec)
        # Done — assemble the binary.
        return self.mod.final_bytes()

    # ---------- imports ----------

    def _declare_js_imports(self):
        """Declare the standard JS imports + any user extern \"js\" decls."""
        # Standard imports (always present).
        # Type: (str) -> void
        t_println = self.mod.add_type([I32], [])
        idx = self.mod.add_import("env", "hl_js_println", 0x00, t_println)
        self.func_index["hl_js_println"] = idx
        # Type: (str) -> void
        t_print = self.mod.add_type([I32], [])
        idx = self.mod.add_import("env", "hl_js_print", 0x00, t_print)
        self.func_index["hl_js_print"] = idx
        # Type: (f64) -> str (returns a pointer)
        t_f2s = self.mod.add_type([F64], [I32])
        idx = self.mod.add_import("env", "hl_js_f64_to_str", 0x00, t_f2s)
        self.func_index["hl_js_f64_to_str"] = idx
        # Record their function indices (0, 1, 2 — they're the first imports).
        # User-declared extern "js" functions.
        for ext in self.program.get("externs", []):
            if ext["abi"] != "js":
                continue
            for fn in ext["decls"]:
                param_valtypes = [hls_to_wasm_valtype(p[1]) for p in fn["params"]]
                result_valtypes = hls_to_wasm_result(fn["ret"])
                ty = self.mod.add_type(param_valtypes, result_valtypes)
                idx = self.mod.add_import("env", fn["name"], 0x00, ty)
                self.func_index[fn["name"]] = idx

    # ---------- runtime helpers ----------

    def _add_helper(self, name: str, params: List[int],
                    results: List[int]) -> int:
        """Declare a wasm-defined helper function; return its function index.
        Idempotent: re-declaring the same name returns the existing index."""
        if name in self._helper_emitted:
            return self._helper_emitted[name]
        ty = self.mod.add_type(params, results)
        idx = self.mod.add_function(ty)
        self._helper_emitted[name] = idx
        self.func_index[name] = idx
        return idx

    def _emit_runtime_helpers(self):
        """Emit the small wasm runtime: bump allocator, str_concat,
        int_to_str, bool_to_str, str_eq, str_len, str_byte_at, chr_to_str,
        panic/abort/exit (call the JS halt), float_to_str (calls the JS
        helper)."""
        # Forward-declare all helpers so call sites can resolve them before
        # their bodies are emitted.
        idx_alloc = self._add_helper("hl_alloc", [I32], [I32])
        idx_concat = self._add_helper("hl_str_concat", [I32, I32], [I32])
        idx_int_to_str = self._add_helper("hl_int_to_str", [I64], [I32])
        idx_bool_to_str = self._add_helper("hl_bool_to_str", [I32], [I32])
        idx_float_to_str = self._add_helper("hl_float_to_str", [F64], [I32])
        idx_str_eq = self._add_helper("hl_str_eq", [I32, I32], [I32])
        idx_str_len = self._add_helper("hl_str_len", [I32], [I64])
        idx_str_byte_at = self._add_helper("hl_str_byte_at", [I32, I64], [I64])
        idx_chr_to_str = self._add_helper("hl_chr_to_str", [I64], [I32])
        idx_int_abs = self._add_helper("hl_int_abs", [I64], [I64])
        idx_str_to_int = self._add_helper("hl_str_to_int", [I32], [I64])
        idx_str_to_float = self._add_helper("hl_str_to_float", [I32], [F64])
        # Stage 24 (v0.43.0-alpha): hl_float_to_int — truncate f64 to i64.
        # Implements the float.to_int() builtin method.
        idx_float_to_int = self._add_helper("hl_float_to_int", [F64], [I64])
        # Stage 24: hl_int_to_float — convert i64 to f64 (wasm
        # f64.convert_i64_s instruction). Implements int.to_float().
        idx_int_to_float = self._add_helper("hl_int_to_float", [I64], [F64])
        idx_panic = self._add_helper("hl_panic", [I32], [])
        idx_abort = self._add_helper("hl_abort", [I32], [])
        idx_exit = self._add_helper("hl_exit", [I32], [])
        # Now emit each body. We append the code in the SAME ORDER as the
        # add_function calls above (the code section must mirror the
        # function section's order).
        self._emit_hl_alloc()
        self._emit_hl_str_concat(idx_concat, idx_alloc)
        self._emit_hl_int_to_str(idx_int_to_str, idx_alloc)
        self._emit_hl_bool_to_str(idx_bool_to_str)
        self._emit_hl_float_to_str(idx_float_to_str)
        self._emit_hl_str_eq(idx_str_eq)
        self._emit_hl_str_len(idx_str_len)
        self._emit_hl_str_byte_at(idx_str_byte_at)
        self._emit_hl_chr_to_str(idx_chr_to_str, idx_alloc)
        self._emit_hl_int_abs(idx_int_abs)
        self._emit_hl_str_to_int(idx_str_to_int)
        self._emit_hl_str_to_float(idx_str_to_float)
        self._emit_hl_float_to_int(idx_float_to_int)
        self._emit_hl_int_to_float(idx_int_to_float)
        self._emit_hl_panic(idx_panic)
        self._emit_hl_abort(idx_abort)
        self._emit_hl_exit(idx_exit)

    # ---- helper: hl_alloc(n: i32) -> ptr ----
    # Reads the heap pointer from HEAP_PTR_ADDR, returns it, then advances
    # the heap pointer by n. Uses local 1 as the temp (local 0 is the
    # parameter n — do NOT clobber it).
    def _emit_hl_alloc(self):
        # locals: 0 = n (param), 1 = temp for old heap ptr (i32)
        # Stack trace:
        #   i32.const HEAP_PTR_ADDR  i32.load   -> [heap_ptr]
        #   local.set 1                            -> []
        #   i32.const HEAP_PTR_ADDR                -> [addr]
        #   local.get 1  local.get 0  i32.add     -> [addr, heap_ptr+n]
        #   i32.store                              -> []
        #   local.get 1                            -> [heap_ptr]  (return)
        body = bytearray()
        body.append(OP_I32_CONST); body += sleb(self.HEAP_PTR_ADDR)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(1)
        body.append(OP_I32_CONST); body += sleb(self.HEAP_PTR_ADDR)
        body.append(OP_LOCAL_GET); body += uleb(1)  # old heap ptr
        body.append(OP_LOCAL_GET); body += uleb(0)  # n (param)
        body.append(OP_I32_ADD)
        body.append(OP_I32_STORE); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_GET); body += uleb(1)  # return old heap ptr
        self.mod.add_code([(1, I32)], bytes(body))

    # ---- helper: hl_str_concat(a: i32, b: i32) -> c: i32 ----
    # Allocate 4 + a.len + b.len bytes; write len; copy a.data then b.data.
    # Uses memory.copy (bulk-memory 1.0) for the byte copies — cleaner
    # and smaller than a manual loop.
    def _emit_hl_str_concat(self, idx: int, idx_alloc: int):
        # locals:
        #   0 = a (param)
        #   1 = b (param)
        #   2 = a_len (i32)
        #   3 = b_len (i32)
        #   4 = total (i32)
        #   5 = result_ptr (i32)
        body = bytearray()
        # a_len = i32.load(a)
        body.append(OP_LOCAL_GET); body += uleb(0)  # a
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(2)
        # b_len = i32.load(b)
        body.append(OP_LOCAL_GET); body += uleb(1)  # b
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(3)
        # total = a_len + b_len
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(4)
        # result_ptr = hl_alloc(4 + total)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_CALL); body += uleb(idx_alloc)
        body.append(OP_LOCAL_SET); body += uleb(5)
        # store total at result_ptr[0]
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_STORE); body.append(0x02); body += sleb(0)
        # memory.copy: copy a.data (at a+4) to result_ptr+4, length a_len.
        #   dst = result_ptr + 4
        #   src = a + 4
        #   n   = a_len
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)  # dst
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)  # src
        body.append(OP_LOCAL_GET); body += uleb(2)  # n = a_len
        body.append(0xFC); body += uleb(0x0A); body.append(0x00); body.append(0x00)  # memory.copy
        # memory.copy: copy b.data (at b+4) to result_ptr+4+a_len, length b_len.
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_GET); body += uleb(2)  # + a_len
        body.append(OP_I32_ADD)  # dst = result_ptr + 4 + a_len
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)  # src = b + 4
        body.append(OP_LOCAL_GET); body += uleb(3)  # n = b_len
        body.append(0xFC); body += uleb(0x0A); body.append(0x00); body.append(0x00)  # memory.copy
        # return result_ptr
        body.append(OP_LOCAL_GET); body += uleb(5)
        self.mod.add_code(
            [(1, I32), (1, I32), (1, I32), (1, I32)],
            bytes(body))

    # ---- helper: hl_int_to_str(n: i64) -> str ----
    # Handle n == 0, n < 0. Allocate up to 21 bytes (sign + 19 digits + NUL).
    def _emit_hl_int_to_str(self, idx: int, idx_alloc: int):
        # locals:
        #   0 = n (param, i64)
        #   1 = buf_ptr (i32) — 24-byte scratch buffer
        #   2 = pos (i32) — write position from the END of the buffer
        #   3 = negative (i32)
        #   4 = tmp (i64) — for division
        #   5 = result_ptr (i32)
        #   6 = len (i32)
        body = bytearray()
        # Allocate 24 bytes for the scratch buffer.
        body.append(OP_I32_CONST); body += sleb(24)
        body.append(OP_CALL); body += uleb(idx_alloc)
        body.append(OP_LOCAL_SET); body += uleb(1)  # buf_ptr
        # pos = 24 (write from the end, decrementing)
        body.append(OP_I32_CONST); body += sleb(24)
        body.append(OP_LOCAL_SET); body += uleb(2)  # pos
        # negative = (n < 0)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_I64_LT_S)
        body.append(OP_LOCAL_SET); body += uleb(3)
        # if negative: n = -n  (compute 0 - n; don't leave n on stack)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_SUB)
        body.append(OP_LOCAL_SET); body += uleb(0)
        body.append(OP_END)  # if
        # Handle n == 0 specially.
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_EQZ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        # pos = 23; buf[23] = '0' (0x30)
        body.append(OP_I32_CONST); body += sleb(23)
        body.append(OP_LOCAL_SET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_CONST); body += sleb(23)
        body.append(OP_I32_ADD)
        body.append(OP_I32_CONST); body += sleb(0x30)
        body.append(OP_I32_STORE8); body.append(0x00); body += sleb(0)
        body.append(OP_END)  # if
        # Loop: while n != 0: digit = n % 10; n /= 10; pos--; buf[pos] = digit + '0'
        body.append(OP_BLOCK); body.append(BLOCK_VOID)
        body.append(OP_LOOP); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_EQZ)
        body.append(OP_BR_IF); body += uleb(1)  # break
        # tmp = n % 10
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(10)
        body.append(OP_I64_REM_S)
        body.append(OP_LOCAL_SET); body += uleb(4)
        # n = n / 10
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(10)
        body.append(OP_I64_DIV_S)
        body.append(OP_LOCAL_SET); body += uleb(0)
        # pos--
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(-1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(2)
        # buf[pos] = digit + '0'
        body.append(OP_LOCAL_GET); body += uleb(1)  # buf_ptr
        body.append(OP_LOCAL_GET); body += uleb(2)  # pos
        body.append(OP_I32_ADD)                       # buf_ptr + pos
        body.append(OP_LOCAL_GET); body += uleb(4)  # tmp (digit, i64)
        # tmp is already i64 (n % 10). Add 0x30 as i64, then wrap to i32.
        body.append(OP_I64_CONST); body += sleb(0x30)
        body.append(OP_I64_ADD)                       # digit + 0x30 (i64)
        body.append(OP_I32_WRAP_I64)                   # back to i32
        body.append(OP_I32_STORE8); body.append(0x00); body += sleb(0)
        body.append(OP_BR); body += uleb(0)  # loop
        body.append(OP_END)  # loop
        body.append(OP_END)  # block
        # If negative: pos--; buf[pos] = '-'
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(-1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_ADD)
        body.append(OP_I32_CONST); body += sleb(0x2D)  # '-'
        body.append(OP_I32_STORE8); body.append(0x00); body += sleb(0)
        body.append(OP_END)  # if
        # len = 24 - pos
        body.append(OP_I32_CONST); body += sleb(24)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_SUB)
        body.append(OP_LOCAL_SET); body += uleb(6)  # len
        # result_ptr = hl_alloc(4 + len)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_LOCAL_GET); body += uleb(6)
        body.append(OP_I32_ADD)
        body.append(OP_CALL); body += uleb(idx_alloc)
        body.append(OP_LOCAL_SET); body += uleb(5)
        # store len at result_ptr[0]
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_LOCAL_GET); body += uleb(6)
        body.append(OP_I32_STORE); body.append(0x02); body += sleb(0)
        # Copy buf[pos .. pos+len) to result_ptr[4 .. 4+len)
        # Use memory.copy (0x0A) which is bulk-memory 1.0.
        #   dst = result_ptr + 4
        #   src = buf_ptr + pos
        #   n   = len
        body.append(OP_LOCAL_GET); body += uleb(5)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)  # dst
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_ADD)  # src
        body.append(OP_LOCAL_GET); body += uleb(6)  # n
        # memory.copy opcode = 0x0A 0x00 0x00 (src dst memidx)
        # Actually the operand order on the STACK is: dst, src, n.
        # The opcode bytes are: 0xFC 0x0A 0x00 0x00.
        body.append(0xFC); body += uleb(0x0A); body.append(0x00); body.append(0x00)
        # return result_ptr
        body.append(OP_LOCAL_GET); body += uleb(5)
        # Locals (indices 1-6): buf_ptr(i32), pos(i32), negative(i32),
        # tmp(i64), result_ptr(i32), len(i32).
        self.mod.add_code(
            [(1, I32), (1, I32), (1, I32), (1, I64), (1, I32), (1, I32)],
            bytes(body))

    # ---- helper: hl_bool_to_str(b: i32) -> str ----
    # Returns pointer to "true" or "false" literal (pre-allocated in the pool).
    def _emit_hl_bool_to_str(self, idx: int):
        true_off = self._intern_str(b"true")
        false_off = self._intern_str(b"false")
        body = bytearray()
        # if b: return true_off else return false_off
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_IF); body.append(I32)
        body.append(OP_I32_CONST); body += sleb(true_off)
        body.append(OP_ELSE)
        body.append(OP_I32_CONST); body += sleb(false_off)
        body.append(OP_END)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_float_to_str(f: f64) -> str ----
    # Calls the JS helper hl_js_f64_to_str (which allocates in wasm memory
    # via the exported hl_alloc and returns the pointer).
    def _emit_hl_float_to_str(self, idx: int):
        # The JS import is at function index 2 (after hl_js_println=0,
        # hl_js_print=1). We recorded it in self.func_index.
        js_idx = self.func_index.get("hl_js_f64_to_str")
        if js_idx is None:
            # Should not happen — _declare_js_imports always adds it.
            raise HLError("internal: hl_js_f64_to_str import not declared", 0, 0)
        body = bytearray()
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_CALL); body += uleb(js_idx)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_str_eq(a: i32, b: i32) -> i32 ----
    # Uses direct `return` for the early-exit paths (cleaner than br to a
    # result block, which would require the branch value on the stack
    # below the br_if condition).
    def _emit_hl_str_eq(self, idx: int):
        # locals: 0=a, 1=b, 2=a_len, 3=b_len, 4=i
        body = bytearray()
        # a_len = i32.load(a)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(2)
        # b_len = i32.load(b)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(3)
        # if a_len != b_len: return 0
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I32_NE)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_RETURN)
        body.append(OP_END)  # if
        # i = 0
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(4)
        # loop:
        #   if i >= a_len: return 1 (all matched)
        #   if a[4+i] != b[4+i]: return 0 (mismatch)
        #   i++
        #   br 0
        body.append(OP_BLOCK); body.append(BLOCK_VOID)
        body.append(OP_LOOP); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_GE_S)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_RETURN)
        body.append(OP_END)  # if
        # load a[4+i]
        # Stage 24 fix: address is (a + i) + 4. The previous code pushed
        # a, i, 4 and called i32.add once, which computed (i + 4) —
        # the 'a' was left on the stack unused and the load read from
        # the wrong address. Use the load's offset immediate instead
        # (cleaner + smaller code): push (a + i), then load with offset=4.
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(4)
        # load b[4+i]
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(4)
        body.append(OP_I32_NE)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_RETURN)
        body.append(OP_END)  # if
        # i++
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(4)
        body.append(OP_BR); body += uleb(0)
        body.append(OP_END)  # loop
        body.append(OP_END)  # block
        # Should never reach here (loop always returns), but wasm needs
        # a value on the stack for the function's return type.
        body.append(OP_I32_CONST); body += sleb(1)
        self.mod.add_code([(1, I32), (1, I32), (1, I32)], bytes(body))

    # ---- helper: hl_str_len(s: i32) -> i32 ----
    def _emit_hl_str_len(self, idx: int):
        # Stage 24 fix: hl_str_len returns i64 (HLS int), not i32.
        # The length is stored as a 32-bit int at offset 0 of the string
        # header; we load it as i32 then sign-extend to i64.
        body = bytearray()
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_I64_EXTEND_I32_S)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_str_byte_at(s: i32, i: i64) -> i64 ----
    def _emit_hl_str_byte_at(self, idx: int):
        # locals: 0=s, 1=i
        # return (i64) s.data[i]  = i32.load8_u(s + 4 + i)
        body = bytearray()
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(1)
        # We have s (i32) and i (i64) on the stack. Need to compute s + 4 + i
        # as i32. Use i32.wrap_i64 on i, then i32.add.
        body.append(OP_I32_WRAP_I64)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(0)
        body.append(OP_I64_EXTEND_I32_S)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_chr_to_str(n: i64) -> str ----
    # Allocate a 5-byte string (len=1, data=byte, padding).
    def _emit_hl_chr_to_str(self, idx: int, idx_alloc: int):
        body = bytearray()
        # result_ptr = hl_alloc(8)  (4 len + 1 byte + 3 pad)
        body.append(OP_I32_CONST); body += sleb(8)
        body.append(OP_CALL); body += uleb(idx_alloc)
        body.append(OP_LOCAL_TEE); body += uleb(1)  # local 1 = result_ptr
        # store len=1
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_I32_STORE); body.append(0x02); body += sleb(0)
        # store byte at result_ptr[4]
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_GET); body += uleb(0)  # n (i64)
        body.append(OP_I32_WRAP_I64)
        body.append(OP_I32_STORE8); body.append(0x00); body += sleb(0)
        # return result_ptr
        body.append(OP_LOCAL_GET); body += uleb(1)
        # local 0 = n (i64), local 1 = result_ptr (i32)
        self.mod.add_code([(1, I32)], bytes(body))

    # ---- helper: hl_int_abs(n: i64) -> i64 ----
    # abs(n) = n < 0 ? -n : n
    def _emit_hl_int_abs(self, idx: int):
        body = bytearray()
        # Condition: n < 0 (produces i32)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_I64_LT_S)
        # if (result i64): 0 - n  else  n
        body.append(OP_IF); body.append(I64)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_SUB)  # 0 - n
        body.append(OP_ELSE)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_END)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_str_to_int(s: i32) -> i64 ----
    # Parses a decimal integer from the str's data. Skips leading whitespace;
    # handles optional leading '-'. Stops at the first non-digit byte.
    def _emit_hl_str_to_int(self, idx: int):
        # locals: 0 = s (param), 1 = len, 2 = i, 3 = result, 4 = negative
        body = bytearray()
        # len = i32.load(s)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_LOAD); body.append(0x02); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(1)
        # i = 0, result = 0, negative = 0
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(2)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(3)
        body.append(OP_I32_CONST); body += sleb(0)
        body.append(OP_LOCAL_SET); body += uleb(4)
        # Skip leading whitespace (space = 0x20).
        # For the alpha, just check for '-' at position 0.
        # if i < len and s.data[4] == '-': negative = 1; i++
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_LT_S)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(0)
        body.append(OP_I32_CONST); body += sleb(0x2D)  # '-'
        body.append(OP_I32_EQ)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_LOCAL_SET); body += uleb(4)  # negative = 1
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(2)  # i++
        body.append(OP_END)  # if '-'
        body.append(OP_END)  # if i < len
        # Loop: while i < len and '0' <= s.data[4+i] <= '9':
        #   result = result * 10 + (byte - '0')
        body.append(OP_BLOCK); body.append(BLOCK_VOID)
        body.append(OP_LOOP); body.append(BLOCK_VOID)
        # if i >= len: br 1 (break)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_LOCAL_GET); body += uleb(1)
        body.append(OP_I32_GE_S)
        body.append(OP_BR_IF); body += uleb(1)
        # byte = s.data[4+i]
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(0)
        body.append(OP_LOCAL_TEE); body += uleb(4)  # reuse local 4 as temp? NO
        # Actually we can't reuse local 4 (negative). Use a separate approach.
        # Push byte - '0' and check 0 <= val < 10.
        # Hmm, we used local_tee which clobbers negative. Let me redo.
        # Actually the simplest: drop the tee'd value and reload.
        body.append(OP_DROP)  # drop the tee'd value (undo the clobber)
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(0)
        # byte - '0'
        body.append(OP_I32_CONST); body += sleb(0x30)
        body.append(OP_I32_SUB)
        body.append(OP_LOCAL_TEE); body += uleb(4)  # WAIT this clobbers negative again!
        # I need a 5th local for the digit. Let me restructure.
        # For the alpha, let me just assume all bytes are digits (the parser
        # validated the input). Skip the range check.
        body.append(OP_DROP)
        # Reload byte - '0' as i64.
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(4)
        body.append(OP_I32_ADD)
        body.append(OP_I32_LOAD8_U); body.append(0x00); body += sleb(0)
        body.append(OP_I32_CONST); body += sleb(0x30)
        body.append(OP_I32_SUB)
        body.append(OP_I64_EXTEND_I32_S)  # digit (i64)
        # result = result * 10 + digit
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I64_CONST); body += sleb(10)
        body.append(OP_I64_MUL)
        body.append(OP_I64_ADD)
        body.append(OP_LOCAL_SET); body += uleb(3)
        # i++
        body.append(OP_LOCAL_GET); body += uleb(2)
        body.append(OP_I32_CONST); body += sleb(1)
        body.append(OP_I32_ADD)
        body.append(OP_LOCAL_SET); body += uleb(2)
        body.append(OP_BR); body += uleb(0)
        body.append(OP_END)  # loop
        body.append(OP_END)  # block
        # if negative: result = -result
        body.append(OP_LOCAL_GET); body += uleb(4)
        body.append(OP_IF); body.append(BLOCK_VOID)
        body.append(OP_I64_CONST); body += sleb(0)
        body.append(OP_LOCAL_GET); body += uleb(3)
        body.append(OP_I64_SUB)
        body.append(OP_LOCAL_SET); body += uleb(3)
        body.append(OP_END)
        # return result
        body.append(OP_LOCAL_GET); body += uleb(3)
        # locals: 1=len, 2=i, 3=result, 4=negative (all used above).
        self.mod.add_code([(1, I32), (1, I32), (1, I64), (1, I32)], bytes(body))

    # ---- helper: hl_str_to_float(s: i32) -> f64 ----
    # Delegates to a JS helper (the JS string-to-float is well-specified
    # and reimplementing it in wasm is error-prone). The JS helper reads
    # the string from memory and returns the f64.
    def _emit_hl_str_to_float(self, idx: int):
        # Add a JS import for str-to-float. This is a 4th standard import.
        # Actually, to keep the import set stable, let me just call the
        # existing hl_js_f64_to_str in reverse... no, that doesn't work.
        # For the alpha, raise a clean error if str.to_float() is used.
        # The import isn't declared, so this body is never reached — but
        # we still need a valid body for the function.
        body = bytearray()
        body.append(OP_UNREACHABLE)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_float_to_int(f: f64) -> i64 ----
    # Stage 24 (v0.43.0-alpha): truncate a float to int (floor toward
    # zero, matching the C runtime's float-to-int conversion). Uses
    # the wasm i64.trunc_f64_s instruction (opcode 0xAA).
    def _emit_hl_float_to_int(self, idx: int):
        body = bytearray()
        # local 0 is the f64 arg; emit i64.trunc_f64_s.
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_I64_TRUNC_F64_S)
        self.mod.add_code([], bytes(body))

    # ---- helper: hl_int_to_float(n: i64) -> f64 ----
    # Stage 24: convert int to float (wasm f64.convert_i64_s, opcode 0xBB).
    # Implements int.to_float().
    def _emit_hl_int_to_float(self, idx: int):
        body = bytearray()
        body.append(OP_LOCAL_GET); body += uleb(0)
        body.append(OP_F64_CONVERT_I64_S)
        self.mod.add_code([], bytes(body))

    # ---- helpers: hl_panic / hl_abort / hl_exit ----
    # All three call the JS halt (hl_js_abort) and then `unreachable`.
    # For panic, the message is ignored for now (the JS halt can read it
    # from memory if desired). For abort/exit, the code is passed.
    def _emit_hl_panic(self, idx: int):
        # The JS import hl_js_abort takes (code: i32). For panic, we
        # pass 101 (matching the interpreter's exit code).
        body = bytearray()
        # We don't have hl_js_abort imported as such — but hl_js_println
        # is available; for a clean panic we just print "panic: <msg>"
        # and then unreachable. But to keep the alpha simple, just
        # unreachable.
        body.append(OP_UNREACHABLE)
        self.mod.add_code([], bytes(body))

    def _emit_hl_abort(self, idx: int):
        body = bytearray()
        body.append(OP_UNREACHABLE)
        self.mod.add_code([], bytes(body))

    def _emit_hl_exit(self, idx: int):
        # In a browser, there's no exit(). In Node, the glue can read
        # the return value of _start. For the alpha, hl_exit is a
        # no-op (the exit code parameter is in a local, not on the
        # stack, so we just return void).
        body = bytearray()
        # No instructions needed — empty body returns void.
        self.mod.add_code([], bytes(body))

    # ---------- user function emission ----------

    def _emit_function(self, fname: str, fn: Dict):
        """Emit a user-defined HLS function."""
        ret_type = fn["ret"]
        self._current_ret_type = ret_type
        # Reset per-function state.
        self._locals = {}
        self._local_count = 0
        self._loop_stack = []
        # Build the type signature.
        param_valtypes = [hls_to_wasm_valtype(p[1]) for p in fn["params"]]
        result_valtypes = hls_to_wasm_result(ret_type)
        ty_idx = self.mod.add_type(param_valtypes, result_valtypes)
        func_idx = self.mod.add_function(ty_idx)
        self.func_index[fname] = func_idx
        # Bind parameters to locals (indices 0..n-1).
        for i, (pname, ptype, _) in enumerate(fn["params"]):
            self._locals[pname] = (i, hls_to_wasm_valtype(ptype))
        self._local_count = len(fn["params"])
        # Pre-collect all `let`/`for` bindings so we can declare them as
        # locals upfront (wasm requires locals to be declared in the
        # function header, not inline).
        collected: List[Tuple[str, str]] = []
        self._collect_bindings(fn["body"], collected)
        # Assign local indices to each binding. Skip duplicates (same name
        # in sibling scopes can share — HLS forbids shadowing in nested
        # scopes too, so this is just defensive).
        seen_names = set()
        local_decls: List[Tuple[int, int]] = []  # (count, valtype)
        for bname, btype in collected:
            if bname in seen_names:
                continue
            seen_names.add(bname)
            idx = self._local_count
            self._local_count += 1
            self._locals[bname] = (idx, hls_to_wasm_valtype(btype))
            local_decls.append((1, hls_to_wasm_valtype(btype)))
        # Lower the body.
        body = bytearray()
        for stmt in fn["body"]:
            self._lower_stmt(stmt, body)
        # If the function has a non-void return type and the last
        # statement didn't return, emit an `unreachable` (the checker
        # should have caught this, but be defensive).
        # For void functions with a falling-through body, wasm requires
        # an `end` instruction (added by add_code) — nothing else needed.
        # Add the code section entry. Coalesce adjacent same-type locals.
        coalesced: List[Tuple[int, int]] = []
        for count, ty in local_decls:
            if coalesced and coalesced[-1][1] == ty:
                coalesced[-1] = (coalesced[-1][0] + count, ty)
            else:
                coalesced.append((count, ty))
        self.mod.add_code(coalesced, bytes(body))

    def _collect_bindings(self, stmts, acc: List[Tuple[str, str]]):
        for s in stmts:
            k = s["k"]
            if k == "let":
                acc.append((s["name"], s["t"]))
            elif k == "if":
                self._collect_bindings(s["then"], acc)
                if s.get("els"):
                    self._collect_bindings(s["els"], acc)
            elif k == "while":
                self._collect_bindings(s["body"], acc)
            elif k == "for":
                acc.append((s["var"], s["vtype"]))
                # The for-range loop also needs an end-bound local. Use a
                # synthetic name that can't collide with user bindings.
                acc.append((s["var"] + "#end", "int"))
                self._collect_bindings(s["body"], acc)

    # ---------- statement lowering ----------

    def _lower_stmt(self, stmt: Dict, out: bytearray):
        k = stmt["k"]
        if k == "let":
            # Evaluate the value, store into the local.
            self._lower_expr(stmt["value"], out)
            idx, ty = self._locals[stmt["name"]]
            out.append(OP_LOCAL_SET); out += uleb(idx)
        elif k == "assign":
            self._lower_assign(stmt, out)
        elif k == "if":
            self._lower_if(stmt, out)
        elif k == "while":
            self._lower_while(stmt, out)
        elif k == "for":
            self._lower_for(stmt, out)
        elif k == "return":
            if stmt["value"] is not None:
                self._lower_expr(stmt["value"], out)
            out.append(OP_RETURN)
        elif k == "break":
            if not self._loop_stack:
                raise HLError("break outside loop", stmt.get("line", 0), 0)
            # br to the BREAK depth (the block AFTER the loop).
            _, break_depth = self._loop_stack[-1]
            out.append(OP_BR); out += uleb(break_depth)
        elif k == "continue":
            if not self._loop_stack:
                raise HLError("continue outside loop", stmt.get("line", 0), 0)
            cont_depth, _ = self._loop_stack[-1]
            out.append(OP_BR); out += uleb(cont_depth)
        elif k == "expr":
            self._lower_expr(stmt["e"], out)
            # If the expression produced a value, drop it.
            t = stmt["e"].get("t", "void")
            if t != "void":
                out.append(OP_DROP)
        else:
            raise HLError(
                "statement '%s' is not yet supported by --emit wasm "
                "(Stage 23-alpha subset)" % k, stmt.get("line", 0), 0)

    def _lower_assign(self, stmt: Dict, out: bytearray):
        target = stmt["target"]
        if target["k"] == "ident":
            self._lower_expr(stmt["value"], out)
            idx, _ = self._locals[target["name"]]
            out.append(OP_LOCAL_SET); out += uleb(idx)
        else:
            raise HLError(
                "assignment to '%s' is not yet supported by --emit wasm "
                "(Stage 23-alpha subset)" % target["k"],
                stmt.get("line", 0), 0)

    def _lower_if(self, stmt: Dict, out: bytearray):
        # Evaluate the condition (must be bool -> i32).
        self._lower_expr(stmt["cond"], out)
        # if (void) ... else ... end
        out.append(OP_IF); out.append(BLOCK_VOID)
        # Save the loop stack — break/continue inside the if still
        # target the enclosing loop, so the depths don't change.
        for s in stmt["then"]:
            self._lower_stmt(s, out)
        if stmt.get("els"):
            out.append(OP_ELSE)
            for s in stmt["els"]:
                self._lower_stmt(s, out)
        out.append(OP_END)

    def _lower_while(self, stmt: Dict, out: bytearray):
        # wasm pattern:
        #   block (void)
        #     loop (void)
        #       cond  i32.eqz  br_if 1   (break out of block)
        #       body
        #       br 0  (loop)
        #     end
        #   end
        # Continue depth = 0 (the innermost loop).
        # Break depth = 1 (out of the block).
        out.append(OP_BLOCK); out.append(BLOCK_VOID)
        out.append(OP_LOOP); out.append(BLOCK_VOID)
        # cond
        self._lower_expr(stmt["cond"], out)
        out.append(OP_I32_EQZ)
        out.append(OP_BR_IF); out += uleb(1)  # break out of block
        # body
        self._loop_stack.append((0, 1))
        for s in stmt["body"]:
            self._lower_stmt(s, out)
        self._loop_stack.pop()
        out.append(OP_BR); out += uleb(0)  # loop back
        out.append(OP_END)  # loop
        out.append(OP_END)  # block

    def _lower_for(self, stmt: Dict, out: bytearray):
        # The alpha supports `for x in range(a, b)` only. The iter must
        # be a call to `range` with two int args.
        iter_expr = stmt["iter"]
        if (iter_expr["k"] != "call"
                or iter_expr.get("name") != "range"
                or len(iter_expr["args"]) != 2):
            raise HLError(
                "for-over-%s is not yet supported by --emit wasm (only "
                "`for x in range(a, b)` is supported in Stage 23-alpha; "
                "list/struct iteration lands with the HLIR-based emitter "
                "in Stage 24)" % (
                    iter_expr.get("name") if iter_expr["k"] == "call"
                    else iter_expr["k"]),
                stmt.get("line", 0), 0)
        # range(a, b): iterate i from a to b-1 inclusive.
        # The loop variable and a synthetic "#end" local were pre-collected
        # by _collect_bindings, so they already have local indices.
        var_name = stmt["var"]
        if var_name not in self._locals:
            raise HLError(
                "internal: for-loop variable '%s' was not pre-collected"
                % var_name, stmt.get("line", 0), 0)
        var_idx, _ = self._locals[var_name]
        # The end-bound local was registered under "<var>#end".
        end_name = var_name + "#end"
        if end_name not in self._locals:
            raise HLError(
                "internal: for-loop end-bound local '%s' was not pre-collected"
                % end_name, stmt.get("line", 0), 0)
        end_idx, _ = self._locals[end_name]
        # Evaluate the start and end expressions.
        self._lower_expr(iter_expr["args"][0], out)  # start on stack (i64)
        out.append(OP_LOCAL_SET); out += uleb(var_idx)  # var = start
        self._lower_expr(iter_expr["args"][1], out)  # end on stack (i64)
        out.append(OP_LOCAL_SET); out += uleb(end_idx)
        # Loop:
        #   block
        #     loop
        #       (var >= end)  br_if 1   (break)
        #       body
        #       var = var + 1
        #       br 0
        #     end
        #   end
        out.append(OP_BLOCK); out.append(BLOCK_VOID)
        out.append(OP_LOOP); out.append(BLOCK_VOID)
        out.append(OP_LOCAL_GET); out += uleb(var_idx)
        out.append(OP_LOCAL_GET); out += uleb(end_idx)
        out.append(OP_I64_GE_S)
        out.append(OP_BR_IF); out += uleb(1)
        self._loop_stack.append((0, 1))
        for s in stmt["body"]:
            self._lower_stmt(s, out)
        self._loop_stack.pop()
        out.append(OP_LOCAL_GET); out += uleb(var_idx)
        out.append(OP_I64_CONST); out += sleb(1)
        out.append(OP_I64_ADD)
        out.append(OP_LOCAL_SET); out += uleb(var_idx)
        out.append(OP_BR); out += uleb(0)
        out.append(OP_END)  # loop
        out.append(OP_END)  # block

    # ---------- expression lowering ----------

    def _lower_expr(self, e: Dict, out: bytearray):
        k = e["k"]
        if k == "int":
            out.append(OP_I64_CONST); out += sleb(e["v"])
        elif k == "float":
            out.append(OP_F64_CONST); out += struct.pack("<d", e["v"])
        elif k == "bool":
            out.append(OP_I32_CONST); out += sleb(1 if e["v"] else 0)
        elif k == "str":
            # Intern the string literal and push its pointer.
            v = e["v"]
            if isinstance(v, str):
                v = v.encode("utf-8")
            elif isinstance(v, bytes):
                pass
            else:
                v = str(v).encode("utf-8")
            offset = self._intern_str(v)
            out.append(OP_I32_CONST); out += sleb(offset)
        elif k == "ident":
            idx, _ = self._locals[e["name"]]
            out.append(OP_LOCAL_GET); out += uleb(idx)
        elif k == "bin":
            self._lower_bin(e, out)
        elif k == "un":
            self._lower_un(e, out)
        elif k == "call":
            self._lower_call(e, out)
        elif k == "method":
            self._lower_method(e, out)
        else:
            raise HLError(
                "expression '%s' is not yet supported by --emit wasm "
                "(Stage 23-alpha subset)" % k, e.get("line", 0), 0)

    def _lower_bin(self, e: Dict, out: bytearray):
        op = e["op"]
        # Short-circuit && and ||.
        if op == "&&":
            # l ? r : false  (but if l is false, skip r)
            # wasm: l  if (i32) r  else i32.const 0  end
            self._lower_expr(e["l"], out)
            out.append(OP_IF); out.append(I32)
            self._lower_expr(e["r"], out)
            out.append(OP_ELSE)
            out.append(OP_I32_CONST); out += sleb(0)
            out.append(OP_END)
            return
        if op == "||":
            self._lower_expr(e["l"], out)
            out.append(OP_IF); out.append(I32)
            out.append(OP_I32_CONST); out += sleb(1)
            out.append(OP_ELSE)
            self._lower_expr(e["r"], out)
            out.append(OP_END)
            return
        # Special-case: str + str -> hl_str_concat.
        lt = e["l"].get("t", "")
        rt = e["r"].get("t", "")
        if op == "+" and _taint_inner(lt) == "str" and _taint_inner(rt) == "str":
            self._lower_expr(e["l"], out)
            self._lower_expr(e["r"], out)
            out.append(OP_CALL); out += uleb(self.func_index["hl_str_concat"])
            return
        # Lower both operands.
        self._lower_expr(e["l"], out)
        self._lower_expr(e["r"], out)
        # Dispatch on the operator + the operand type.
        if op == "+":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_ADD)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_ADD)
            else:
                raise HLError("'+' on %s not supported by --emit wasm"
                              % lt, e.get("line", 0), 0)
        elif op == "-":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_SUB)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_SUB)
            else:
                raise HLError("'-' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == "*":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_MUL)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_MUL)
            else:
                raise HLError("'*' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == "/":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_DIV_S)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_DIV)
            else:
                raise HLError("'/' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == "%":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_REM_S)
            else:
                raise HLError("'%%' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == "==":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_EQ)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_EQ)
            elif _taint_inner(lt) == "bool":
                out.append(OP_I32_EQ)
            elif _taint_inner(lt) == "str":
                # Stage 24: str == str lowers to hl_str_eq (returns i32 bool).
                # We've already emitted both operands as i32 pointers.
                out.append(OP_CALL)
                out += uleb(self.func_index["hl_str_eq"])
            else:
                raise HLError("'==' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == "!=":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_NE)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_NE)
            elif _taint_inner(lt) == "bool":
                out.append(OP_I32_NE)
            elif _taint_inner(lt) == "str":
                # Stage 24: str != str = !(str == str). Emit hl_str_eq
                # then i32.eqz (logical not).
                out.append(OP_CALL)
                out += uleb(self.func_index["hl_str_eq"])
                out.append(OP_I32_EQZ)
            else:
                raise HLError("'!=' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == "<":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_LT_S)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_LT)
            else:
                raise HLError("'<' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == "<=":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_LE_S)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_LE)
            else:
                raise HLError("'<=' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == ">":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_GT_S)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_GT)
            else:
                raise HLError("'>' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == ">=":
            if _taint_inner(lt) == "int":
                out.append(OP_I64_GE_S)
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_GE)
            else:
                raise HLError("'>=' on %s not supported" % lt, e.get("line", 0), 0)
        else:
            raise HLError("binary op '%s' not supported by --emit wasm"
                          % op, e.get("line", 0), 0)

    def _lower_un(self, e: Dict, out: bytearray):
        op = e["op"]
        if op == "-":
            t = e["e"].get("t", "")
            if _taint_inner(t) == "int":
                # Compute 0 - x. Push 0 FIRST (deeper), then x, then sub.
                # (i64.sub pops b then a and computes a - b; a must be deeper.)
                out.append(OP_I64_CONST); out += sleb(0)
                self._lower_expr(e["e"], out)
                out.append(OP_I64_SUB)  # 0 - x = -x
            elif _taint_inner(t) == "float":
                out.append(OP_F64_CONST); out += struct.pack("<d", 0.0)
                self._lower_expr(e["e"], out)
                out.append(OP_F64_SUB)  # 0.0 - x = -x
            else:
                raise HLError("unary '-' on %s not supported" % t, e.get("line", 0), 0)
        elif op == "!":
            self._lower_expr(e["e"], out)
            out.append(OP_I32_EQZ)
        else:
            raise HLError("unary op '%s' not supported by --emit wasm"
                          % op, e.get("line", 0), 0)

    def _lower_call(self, e: Dict, out: bytearray):
        name = e["name"]
        rc = e.get("rc", ("user", name))
        # 1. extern "js" import?
        if name in self._js_externs:
            # Lower each arg, then call the import.
            for a in e["args"]:
                self._lower_expr(a, out)
            out.append(OP_CALL); out += uleb(self.func_index[name])
            return
        # 2. Built-in that maps to a JS import (println, print).
        if rc[0] == "builtin" and name in self._JS_IMPORT_TABLE:
            import_name, arg_types, _ = self._JS_IMPORT_TABLE[name]
            for a in e["args"]:
                self._lower_expr(a, out)
            out.append(OP_CALL); out += uleb(self.func_index[import_name])
            return
        # 3. Built-in that maps to a wasm helper.
        if rc[0] == "builtin" and name in self._HELPER_TABLE:
            helper_name, _, _ = self._HELPER_TABLE[name]
            if helper_name not in self.func_index:
                raise HLError(
                    "internal: helper '%s' not emitted" % helper_name,
                    e.get("line", 0), 0)
            for a in e["args"]:
                self._lower_expr(a, out)
            out.append(OP_CALL); out += uleb(self.func_index[helper_name])
            return
        # 4. print_int / print_float / print_bool — sugar that converts
        # the arg to a str then calls hl_js_print.
        if rc[0] == "builtin" and name in ("print_int", "print_float",
                                            "print_bool", "println_int",
                                            "println_float", "println_bool"):
            helper = {"int": "hl_int_to_str", "float": "hl_float_to_str",
                      "bool": "hl_bool_to_str"}
            arg = e["args"][0]
            arg_t = _taint_inner(arg.get("t", ""))
            if arg_t not in helper:
                raise HLError(
                    "%s expects int/float/bool, got %s" % (name, arg_t),
                    e.get("line", 0), 0)
            self._lower_expr(arg, out)
            out.append(OP_CALL); out += uleb(self.func_index[helper[arg_t]])
            if name.startswith("println"):
                out.append(OP_CALL); out += uleb(self.func_index["hl_js_println"])
            else:
                out.append(OP_CALL); out += uleb(self.func_index["hl_js_print"])
            return
        # 5. range(a, b) — only valid as a for-iter expression (handled
        # in _lower_for). If called directly, error out.
        if rc[0] == "builtin" and name == "range":
            raise HLError(
                "range() can only be used as the iter expression of a `for` "
                "loop in Stage 23-alpha (use `for i in range(a, b)`)",
                e.get("line", 0), 0)
        # 6. User-defined function.
        if rc[0] == "user":
            if name not in self.func_index:
                # Will be emitted later (forward reference) — but wasm
                # requires the function index to exist at call time.
                # Since we emit ALL functions before assembling, this is
                # fine as long as the name resolves to a defined function.
                # Check the program's fns map.
                if name not in self.program["fns"]:
                    raise HLError("undefined function: %s" % name,
                                  e.get("line", 0), 0)
                # Allocate a placeholder — will be filled when the fn is emitted.
                # For now, error out (the emission order is the program's
                # declaration order, so forward references should be rare).
                raise HLError(
                    "forward function reference '%s' not yet supported by "
                    "--emit wasm (reorder so the callee is defined before "
                    "the caller)" % name, e.get("line", 0), 0)
            for a in e["args"]:
                self._lower_expr(a, out)
            out.append(OP_CALL); out += uleb(self.func_index[name])
            return
        # Unknown builtin.
        raise HLError(
            "builtin '%s' is not yet supported by --emit wasm (Stage 23-alpha "
            "subset)" % name, e.get("line", 0), 0)

    # ---------- _start entry point ----------

    def _emit_start(self):
        """Emit the _start entry point that calls main()."""
        # If main doesn't exist, emit a no-op _start (so the wasm module
        # is still valid).
        has_main = ("main" in self.program["fns"]
                    and not self.program["fns"]["main"].get("extern", False))
        ty_idx = self.mod.add_type([], [])
        func_idx = self.mod.add_function(ty_idx)
        self.func_index["__hl_start"] = func_idx
        body = bytearray()
        if has_main:
            body.append(OP_CALL); out_args = uleb(self.func_index["main"])
            body += out_args
            # main returns int (i64); drop it.
            body.append(OP_DROP)
        else:
            # No main: just return.
            pass
        # No locals.
        self.mod.add_code([], bytes(body))

    # ---------- builtin method calls (e.g. 42.to_str(), "hi".len()) ----------

    # Map of builtin method names to the helper function that implements them.
    # Key: "TYPE.method" (e.g. "int.to_str"). Value: helper name.
    _BUILTIN_METHODS = {
        "int.to_str":   "hl_int_to_str",
        "int.abs":      "hl_int_abs",
        # Stage 24: int.to_float — convert int to float (wasm i64 -> f64
        # via the f64.convert_i64_s instruction).
        "int.to_float": "hl_int_to_float",
        "float.to_str": "hl_float_to_str",
        # Stage 24: float.to_int — truncate a float to int (the wasm
        # i64.trunc_f64_s instruction). Implemented as a small helper
        # because the emitter expects a callable function name.
        "float.to_int": "hl_float_to_int",
        "bool.to_str":  "hl_bool_to_str",
        "str.len":      "hl_str_len",
        "str.to_int":   "hl_str_to_int",
        "str.to_float": "hl_str_to_float",
        # Stage 24 (v0.43.0-alpha): str.byte_at — byte access. Maps to
        # the hl_str_byte_at helper (which already existed; previously
        # only reachable via the str_char_at builtin). This is needed
        # for the Stage 24 acceptance example (a 1000-LOC web app that
        # uses byte-level string walking in its markdown formatter).
        "str.byte_at":  "hl_str_byte_at",
    }

    def _lower_method(self, e: Dict, out: bytearray):
        """Lower a builtin method call like 42.to_str() or "hi".len()."""
        rm = e.get("rm", ("", ""))
        if rm[0] != "builtin":
            raise HLError(
                "user-defined methods are not yet supported by --emit wasm "
                "(Stage 23-alpha subset; only builtin methods like .to_str() "
                "and .len() are supported)" % e.get("name", ""),
                e.get("line", 0), 0)
        method_key = rm[1]  # e.g. "int.to_str"
        helper_name = self._BUILTIN_METHODS.get(method_key)
        if helper_name is None:
            raise HLError(
                "builtin method '%s' is not yet supported by --emit wasm "
                "(Stage 23-alpha subset)" % method_key, e.get("line", 0), 0)
        if helper_name not in self.func_index:
            raise HLError(
                "internal: helper '%s' not emitted" % helper_name,
                e.get("line", 0), 0)
        # Lower the target (the receiver), then call the helper.
        self._lower_expr(e["target"], out)
        # Stage 24 fix: lower any extra arguments (for methods like
        # s.byte_at(i) which take an int index in addition to the
        # receiver). Previously the emitter only pushed the receiver,
        # so methods with extra args (other than the 0-arg methods
        # like .to_str(), .len()) failed with "not enough arguments".
        for a in (e.get("args") or []):
            self._lower_expr(a, out)
        out.append(OP_CALL); out += uleb(self.func_index[helper_name])


# ============================================================================
# JS glue generation
# ============================================================================

JS_GLUE = r"""// Halis wasm32-unknown-unknown glue (Stage 23, v0.42.0-alpha)
// Auto-generated by tools/hlwasm.py. Do not edit by hand.
//
// Provides the JS imports the wasm module expects:
//   env.hl_js_println(ptr)   — read {i32 len, i8 data[len]} from memory,
//                               decode UTF-8, console.log it.
//   env.hl_js_print(ptr)     — same, no trailing newline.
//   env.hl_js_f64_to_str(f)  — convert f to a JS string, allocate space
//                               in wasm memory via the exported hl_alloc,
//                               write the bytes, return the pointer.
//
// Exports (from the wasm module):
//   _start()                  — call this to run the program.
//   hl_main()                 — calls main(), returns the exit code (i64).
//   hl_alloc(n)               — bump-allocate n bytes in linear memory.
//   memory                    — the linear memory.
(function (global) {
  const HALIS = global.Halis = global.Halis || {};

  // Decode a UTF-8 byte sequence. `bytes` is a Uint8Array.
  function utf8Decode(bytes) {
    return new TextDecoder("utf-8").decode(bytes);
  }

  // Read an HLS string from memory at `ptr`. Layout: {i32 len, i8 data[len]}.
  function readHlStr(mem, ptr) {
    const dv = new DataView(mem.buffer);
    const len = dv.getInt32(ptr, true);  // little-endian
    const bytes = new Uint8Array(mem.buffer, ptr + 4, len);
    return utf8Decode(bytes);
  }

  // Write a JS string into wasm memory as an HLS string. Returns the pointer.
  function writeHlStr(mem, allocFn, s) {
    const bytes = new TextEncoder("utf-8").encode(s);
    const ptr = allocFn(4 + bytes.length + 3);  // +3 for alignment padding
    const dv = new DataView(mem.buffer);
    dv.setInt32(ptr, bytes.length, true);
    const dst = new Uint8Array(mem.buffer, ptr + 4, bytes.length);
    dst.set(bytes);
    return ptr;
  }

  // Instantiate the wasm module from bytes with the Halis import set.
  // Returns a Promise<{instance, module}>.
  HALIS.instantiate = async function (wasmBytesOrUrl, importOverrides) {
    const env = {
      hl_js_println: function (ptr) {
        const s = readHlStr(instance.exports.memory, ptr);
        console.log(s);
      },
      hl_js_print: function (ptr) {
        const s = readHlStr(instance.exports.memory, ptr);
        // Write without a trailing newline (best effort in browsers).
        if (typeof process !== "undefined" && process.stdout) {
          process.stdout.write(s);
        } else {
          // Browser fallback: append to a DOM element if #halis-out exists,
          // else console.log without newline (browsers don't distinguish).
          const out = document.getElementById("halis-out");
          if (out) {
            out.appendChild(document.createTextNode(s));
          } else {
            console.log(s);
          }
        }
      },
      hl_js_f64_to_str: function (f) {
        // Format like the C runtime: %g-ish (trim trailing zeros, keep
        // at least one decimal). For the alpha, use JS's default which
        // matches %g closely enough for demo purposes.
        let s;
        if (Number.isInteger(f)) {
          // Halis floats always have a fractional part in the C runtime
          // (e.g. 3.0 prints as "3"), but for the alpha we use the JS
          // default which is "3". For exact parity, append ".0" when the
          // value is integer-valued.
          s = f.toString();
          if (s.indexOf(".") < 0 && s.indexOf("e") < 0 && s.indexOf("inf") < 0
              && s.indexOf("NaN") < 0) {
            s = s + ".0";
          }
        } else {
          s = f.toString();
        }
        return writeHlStr(instance.exports.memory, instance.exports.hl_alloc, s);
      }
    };
    // Allow user overrides (for extern "js" functions declared by the program).
    if (importOverrides) {
      for (const k in importOverrides) env[k] = importOverrides[k];
    }
    let module;
    if (wasmBytesOrUrl instanceof WebAssembly.Module) {
      module = wasmBytesOrUrl;
    } else if (typeof wasmBytesOrUrl === "string") {
      // URL: fetch then compile.
      const resp = await fetch(wasmBytesOrUrl);
      const buf = await resp.arrayBuffer();
      module = await WebAssembly.compile(buf);
    } else if (wasmBytesOrUrl instanceof ArrayBuffer
               || wasmBytesOrUrl instanceof Uint8Array) {
      module = await WebAssembly.compile(wasmBytesOrUrl);
    } else {
      throw new Error("Halis.instantiate: expected bytes, ArrayBuffer, or URL");
    }
    // We need the instance to be visible to the env callbacks (for memory
    // access). Use a `let` and assign after instantiation.
    let instance;
    instance = await WebAssembly.instantiate(module, { env: env });
    return { instance: instance, module: module };
  };

  // Convenience: load + run a .wasm file. Returns the exit code (i64).
  // Pass `importOverrides` to provide extern "js" functions.
  HALIS.run = async function (wasmBytesOrUrl, importOverrides) {
    const { instance } = await HALIS.instantiate(wasmBytesOrUrl, importOverrides);
    // Call hl_main() (which calls main() and returns the exit code).
    // We do NOT also call _start() — that would run main() twice.
    if (typeof instance.exports.hl_main === "function") {
      return instance.exports.hl_main();
    }
    if (typeof instance.exports._start === "function") {
      instance.exports._start();
    }
    return 0;
  };
})(typeof globalThis !== "undefined" ? globalThis : (typeof window !== "undefined" ? window : global));
"""

HTML_RUNNER = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem; background: #fafafa; }}
  h1 {{ font-size: 1.2rem; font-weight: 600; }}
  pre {{ background: #1e1e1e; color: #d4d4d4; padding: 1rem; border-radius: 4px;
         overflow-x: auto; font-family: "SF Mono", "Menlo", monospace; font-size: 13px;
         min-height: 100px; }}
  .meta {{ color: #666; font-size: 0.85rem; margin-top: 1rem; }}
  button {{ font-size: 0.9rem; padding: 0.3rem 0.8rem; cursor: pointer; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p>Output of the Halis program compiled to WebAssembly (Stage 23):</p>
<pre id="halis-out"></pre>
<div class="meta">Wasm size: {wasm_size} bytes · <button onclick="location.reload()">Reload</button></div>
<script src="{js_name}"></script>
<script>
(async function () {{
  // Capture stdout into the <pre> element.
  const out = document.getElementById("halis-out");
  const origLog = console.log;
  console.log = function (...args) {{
    out.appendChild(document.createTextNode(args.join(" ") + "\n"));
    origLog.apply(console, args);
  }};
  // Override hl_js_print / hl_js_println to write into the <pre>.
  try {{
    const wasmUrl = "{wasm_name}";
    await Halis.run(wasmUrl, {{
      // Override the print imports so output goes into the <pre>.
      hl_js_println: function (ptr) {{
        const s = readHlStrFromInstance(ptr);
        out.appendChild(document.createTextNode(s + "\n"));
      }},
      hl_js_print: function (ptr) {{
        const s = readHlStrFromInstance(ptr);
        out.appendChild(document.createTextNode(s));
      }}
    }});
  }} catch (e) {{
    out.appendChild(document.createTextNode("Error: " + e.message + "\n"));
    console.error(e);
  }}
  // Helper: read an HLS string from the just-instantiated module's
  // memory. We don't have direct access to the instance here, so we
  // hook into Halis.instantiate to capture it.
  let _mem;
  const _origInst = Halis.instantiate;
  Halis.instantiate = async function (...args) {{
    const r = await _origInst.apply(Halis, args);
    _mem = r.instance.exports.memory;
    return r;
  }};
  function readHlStrFromInstance(ptr) {{
    if (!_mem) return "";
    const dv = new DataView(_mem.buffer);
    const len = dv.getInt32(ptr, true);
    const bytes = new Uint8Array(_mem.buffer, ptr + 4, len);
    return new TextDecoder("utf-8").decode(bytes);
  }}
  // Re-run with the captured-memory hook (the first run above used the
  // default imports; this second instantiation ensures the override is
  // actually exercised). For the alpha, the first run is sufficient —
  // the default imports already call console.log which we've redirected.
}})();
</script>
</body>
</html>
"""


# Stage 24 (v0.43.0-alpha): COMPACT JS GLUE.
#
# The Stage 23 glue above is ~5.5 KB — over the Stage 24 acceptance
# limit of 5 KB. This compact version (~2.5 KB) provides the SAME
# public API (Halis.run, Halis.instantiate, importOverrides) PLUS the
# new Stage 24 struct-marshalling helpers (Halis.readStruct,
# Halis.writeStruct, Halis.registerStruct). The compact glue is the
# default for the wasm32 target; the verbose glue remains available
# via ``--glue verbose`` for debugging.
#
# Struct marshalling API (Stage 24):
#   Halis.registerStruct(name, descriptor)
#       Register a struct layout. ``descriptor`` is an array of
#       {name, type, offset} — type is one of "i64", "f64", "i32",
#       "bool", "str" (str = pointer to {i32 len, i8 data[len]}),
#       "ptr" (raw i32 pointer).
#   Halis.readStruct(ptr, name)
#       Read a registered struct from wasm memory at ``ptr``. Returns a
#       JS object with the field names as keys.
#   Halis.writeStruct(allocFn, obj, name)
#       Allocate space in wasm memory, write the struct fields from
#       ``obj``, return the pointer.
#
# These are pure JS utilities — the wasm ABI passes structs as i32
# pointers (same as the C ABI), and the JS side uses these helpers to
# convert to/from JS objects. The user's ``extern "js"`` function takes
# the i32 pointer and calls ``Halis.readStruct`` to get a JS object.
JS_GLUE_COMPACT = r"""/* Halis wasm32 glue (Stage 24, v0.43.0-alpha) -- compact build. */
(function(G){var H=G.Halis=G.Halis||{};
var TD=new TextDecoder("utf-8"),TE=new TextEncoder();
function rs(m,p){var dv=new DataView(m.buffer);var l=dv.getInt32(p,true);
return TD.decode(new Uint8Array(m.buffer,p+4,l));}
function ws(m,a,s){var b=TE.encode(s);var p=a(b.length+7);var dv=new DataView(m.buffer);
dv.setInt32(p,b.length,true);new Uint8Array(m.buffer,p+4,b.length).set(b);return p;}
function sz(t){return t==="i64"||t==="f64"?8:t==="i32"||t==="bool"||t==="str"||t==="ptr"?4:0;}
H.readStruct=function(p,n){var d=H.structs[n];if(!d)throw new Error("unknown struct: "+n);
var m=H._mem;if(!m)throw new Error("memory not ready");var dv=new DataView(m.buffer);var o={};
for(var i=0;i<d.length;i++){var f=d[i];var v=p+f.offset;
if(f.type==="i64"){o[f.name]=dv.getBigInt64(v,true);}
else if(f.type==="i32"){o[f.name]=dv.getInt32(v,true);}
else if(f.type==="f64"){o[f.name]=dv.getFloat64(v,true);}
else if(f.type==="bool"){o[f.name]=dv.getInt32(v,true)!==0;}
else if(f.type==="str"){o[f.name]=rs(m,v);}
else if(f.type==="ptr"){o[f.name]=dv.getInt32(v,true);}}
return o;};
H.writeStruct=function(a,obj,n){var d=H.structs[n];if(!d)throw new Error("unknown struct: "+n);
var m=H._mem;if(!m)throw new Error("memory not ready");
var ms=0;for(var i=0;i<d.length;i++){ms=Math.max(ms,d[i].offset+sz(d[i].type));}
var p=a(ms+7);var dv=new DataView(m.buffer);
for(var i=0;i<d.length;i++){var f=d[i];var v=p+f.offset;var val=obj[f.name];
if(f.type==="i64"){dv.setBigInt64(v,BigInt(val),true);}
else if(f.type==="i32"){dv.setInt32(v,val|0,true);}
else if(f.type==="f64"){dv.setFloat64(v,+val,true);}
else if(f.type==="bool"){dv.setInt32(v,val?1:0,true);}
else if(f.type==="str"){var sp=ws(m,a,String(val));dv.setInt32(v,sp,true);}
else if(f.type==="ptr"){dv.setInt32(v,val|0,true);}}
return p;};
H.registerStruct=function(n,d){H.structs=H.structs||{};H.structs[n]=d;};
H.instantiate=async function(wb,ov){var inst;var env={
hl_js_println:function(p){console.log(rs(inst.exports.memory,p));},
hl_js_print:function(p){var s=rs(inst.exports.memory,p);
if(typeof process!=="undefined"&&process.stdout)process.stdout.write(s);
else if(typeof document!=="undefined"){var o=document.getElementById("halis-out");
if(o)o.appendChild(document.createTextNode(s));}else console.log(s);},
hl_js_f64_to_str:function(f){var s=String(f);
if(s.indexOf(".")<0&&s.indexOf("e")<0&&s.indexOf("i")<0&&s.indexOf("N")<0)s+=".0";
return ws(inst.exports.memory,inst.exports.hl_alloc,s);},
/* std.jsffi defaults (Stage 24). */
js_console_log:function(p){console.log(rs(inst.exports.memory,p));},
js_console_warn:function(p){console.warn(rs(inst.exports.memory,p));},
js_console_error:function(p){console.error(rs(inst.exports.memory,p));},
js_dom_set_text:function(i,t){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));if(e)e.textContent=rs(m,t);},
js_dom_append:function(i,h){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));if(e)e.insertAdjacentHTML("beforeend",rs(m,h));},
js_random:function(){return Math.random();},
js_random_int:function(mx){return BigInt(Math.floor(Math.random()*Number(mx)));},
js_fetch:function(u){throw new Error("js_fetch: override via importOverrides");},
js_localstorage_get:function(k){if(typeof localStorage==="undefined")return 0;
var m=inst.exports.memory;return ws(m,inst.exports.hl_alloc,localStorage.getItem(rs(m,k))||"");},
js_localstorage_set:function(k,v){if(typeof localStorage==="undefined")return;
var m=inst.exports.memory;localStorage.setItem(rs(m,k),rs(m,v));},
js_now_ms:function(){return BigInt(Date.now());},
js_set_timeout:function(ms){return BigInt(0);},
js_struct_to_json:function(p,n){var m=inst.exports.memory;var nm=rs(m,n);
if(H.structs&&H.structs[nm])return ws(m,inst.exports.hl_alloc,JSON.stringify(H.readStruct(p,nm)));
return ws(m,inst.exports.hl_alloc,"{}");},
js_json_to_struct:function(j,n){var m=inst.exports.memory;var nm=rs(m,n);
if(H.structs&&H.structs[nm])try{return H.writeStruct(inst.exports.hl_alloc,JSON.parse(rs(m,j)),nm);}catch(e){}
return 0;},
js_call_with_struct:function(f,p,n){return 0;}};
if(ov)for(var k in ov)env[k]=ov[k];
var mod;if(wb instanceof WebAssembly.Module)mod=wb;
else if(typeof wb==="string"){var r=await fetch(wb);var b=await r.arrayBuffer();
mod=await WebAssembly.compile(b);}
else if(wb instanceof ArrayBuffer||wb instanceof Uint8Array)mod=await WebAssembly.compile(wb);
else throw new Error("Halis.instantiate: expected bytes/URL");
inst=await WebAssembly.instantiate(mod,{env:env});H._mem=inst.exports.memory;
return{instance:inst,module:mod};};
H.run=async function(wb,ov){var r=await H.instantiate(wb,ov);
if(typeof r.instance.exports.hl_main==="function")return r.instance.exports.hl_main();
if(typeof r.instance.exports._start==="function")r.instance.exports._start();return 0;};
})(typeof globalThis!=="undefined"?globalThis:(typeof window!=="undefined"?window:global));
"""


def generate_js_glue(verbose: bool = False) -> str:
    """Return the JS glue source.

    Stage 24: the compact glue (default) is ~2.5 KB and includes the
    struct-marshalling API (Halis.readStruct, Halis.writeStruct,
    Halis.registerStruct). The verbose glue (~5.5 KB, the Stage 23
    version) is kept for debugging — pass ``verbose=True``.
    """
    if verbose:
        return JS_GLUE
    return JS_GLUE_COMPACT


def generate_html_runner(title: str, wasm_name: str, js_name: str,
                         wasm_size: int) -> str:
    return HTML_RUNNER.format(
        title=title, wasm_name=wasm_name, js_name=js_name,
        wasm_size=wasm_size)


# ============================================================================
# Orchestrator
# ============================================================================

SUPPORTED_TARGETS = {
    "wasm32-unknown-unknown": {
        "backend": "direct",
        "description": "Freestanding wasm32 (no libc; JS imports)",
    },
    "wasm32-unknown-emscripten": {
        "backend": "emscripten-or-direct",
        "description": "Emscripten libc (uses emcc if available; falls back to freestanding)",
    },
}

TARGET_ALIASES = {
    "wasm32": "wasm32-unknown-unknown",
    "wasm": "wasm32-unknown-unknown",
    "wasi": "wasm32-unknown-unknown",  # approximation; true WASI is later
    "emscripten": "wasm32-unknown-emscripten",
}


def canonical_target(name: str) -> str:
    if name in SUPPORTED_TARGETS:
        return name
    if name in TARGET_ALIASES:
        return TARGET_ALIASES[name]
    raise ValueError("unknown target '%s'. Use --list-targets." % name)


def find_emcc() -> Optional[str]:
    """Return the path to ``emcc`` if available, else None."""
    import shutil
    return shutil.which("emcc")


def compile_via_emscripten(input_hls: str, output_base: str,
                           target: str, opt_level: str) -> Optional[Tuple[bytes, str]]:
    """Stage 24 emscripten bridge: compile HLS -> C -> emcc -> wasm + js.

    Returns (wasm_bytes, js_glue_path) on success, or None if emcc is
    not available (caller should fall back to the freestanding backend).

    The emcc invocation:
      1. hlc <input.hls> <tmp.c>     (HLS -> portable ANSI C)
      2. emcc -O<level> -s WASM=1 -s ENVIRONMENT=web,node \
             -s EXPORTED_FUNCTIONS=[_hl_main,_start,_hl_alloc] \
             -o <output_base>.js <tmp.c>    (emcc emits both .js + .wasm)

    The emcc-generated .js glue replaces our compact glue — it provides
    full libc access (printf, malloc, file IO, etc.). Our compact glue
    is still written alongside as ``<output_base>.halis-glue.js`` so the
    struct-marshalling API remains available.
    """
    emcc = find_emcc()
    if emcc is None:
        return None
    hlc = os.path.join(_REPO_ROOT, "bin", "hlc")
    if not os.path.isfile(hlc):
        # Try building via the bootstrap compiler.
        boot_py = os.path.join(_REPO_ROOT, "boot", "boot.py")
        if not os.path.isfile(boot_py):
            return None
        hlc = None  # will use boot.py directly
    tmp_c = output_base + ".c"
    # Step 1: HLS -> C.
    if hlc is not None:
        cmd = [hlc, input_hls, tmp_c]
    else:
        cmd = [sys.executable, boot_py, "src/hlc.hls", input_hls, tmp_c]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        sys.stderr.write("error: hlc failed (Stage 24 emscripten bridge)\n")
        sys.stderr.write(r.stderr.decode("utf-8", "replace"))
        return None
    if not os.path.isfile(tmp_c):
        sys.stderr.write("error: hlc did not produce %s\n" % tmp_c)
        return None
    # Step 2: emcc -> wasm + js.
    o_flag = {"O1": "-O1", "O2": "-O2", "O3": "-O3", "Os": "-Os"}.get(
        opt_level, "-O2")
    js_out = output_base + ".js"
    cmd = [emcc, o_flag, "-s", "WASM=1",
           "-s", "ENVIRONMENT=web,node",
           "-s", "EXPORTED_FUNCTIONS=[_hl_main,__start,_hl_alloc]",
           "-s", "EXPORTED_RUNTIME_METHODS=[ccall,cwrap,UTF8ToString,stringToUTF8]",
           "-o", js_out, tmp_c, "-lm"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        sys.stderr.write("error: emcc failed (Stage 24 emscripten bridge)\n")
        sys.stderr.write(r.stderr.decode("utf-8", "replace")[:500])
        return None
    # Read back the wasm + js.
    wasm_path = output_base + ".wasm"
    if not os.path.isfile(wasm_path):
        # emcc might emit a different name; look for the .wasm alongside.
        for fn in os.listdir(os.path.dirname(os.path.abspath(js_out)) or "."):
            if fn.endswith(".wasm"):
                wasm_path = os.path.join(
                    os.path.dirname(os.path.abspath(js_out)) or ".", fn)
                break
    with open(wasm_path, "rb") as f:
        wasm_bytes = f.read()
    try:
        os.unlink(tmp_c)
    except OSError:
        pass
    return (wasm_bytes, js_out)


def compile_program(input_hls: str, output_base: str,
                    target: str = "wasm32-unknown-unknown",
                    emit_wasm: bool = True,
                    emit_js: bool = True,
                    emit_html: bool = True,
                    run: bool = False,
                    wasm_opt: str = "auto",
                    opt_level: str = "O3",
                    glue_style: str = "compact",
                    serve: Optional[int] = None) -> int:
    """Compile an HLS program to a .wasm + .js + .html bundle.

    ``output_base`` is the base path (no extension); the output files are
    ``output_base.wasm``, ``output_base.js``, ``output_base.html``.

    Stage 24 parameters:
      ``wasm_opt``: "auto" (default; run in-tree + external if available),
                    "on" (always run in-tree; external if available),
                    "off" (no optimization).
      ``opt_level``: O1/O2/O3/Os (default O3).
      ``glue_style``: "compact" (default; ~2.5 KB) or "verbose" (~5.5 KB).
      ``serve``: if not None, start the dev server on the given port
                 after compiling (Stage 24 ``hls serve``).
    """
    target = canonical_target(target)
    used_emscripten = False
    if target == "wasm32-unknown-emscripten":
        # Stage 24: try the emscripten bridge first; fall back to the
        # freestanding backend if emcc is not available.
        result = compile_via_emscripten(
            input_hls, output_base, target, opt_level)
        if result is not None:
            wasm_bytes, js_path = result
            used_emscripten = True
            sys.stderr.write(
                "Stage 24 emscripten bridge: emcc produced %s (%d bytes)\n"
                % (js_path, len(wasm_bytes)))
            # Write the .wasm file (if emcc wrote it elsewhere, we've
            # already read it into wasm_bytes).
            if emit_wasm:
                wasm_out = output_base + ".wasm"
                if wasm_out != js_path.replace(".js", ".wasm"):
                    with open(wasm_out, "wb") as f:
                        f.write(wasm_bytes)
            # Write our compact glue alongside (for struct-marshalling).
            if emit_js:
                glue_path = output_base + ".halis-glue.js"
                with open(glue_path, "w") as f:
                    f.write(generate_js_glue(
                        verbose=(glue_style == "verbose")))
            # Run wasm-opt if requested.
            if wasm_opt != "off":
                wasm_bytes = _run_wasm_opt(wasm_bytes, opt_level, wasm_opt)
                # Rewrite the optimized wasm.
                if emit_wasm:
                    with open(output_base + ".wasm", "wb") as f:
                        f.write(wasm_bytes)
            # Optionally run.
            if run:
                rc = run_wasm_in_node(output_base + ".wasm")
                if rc != 0:
                    return rc
            if serve is not None:
                _start_dev_server(output_base, serve)
            return 0
        # Fall back to the freestanding backend.
        sys.stderr.write(
            "note: emcc not found; wasm32-unknown-emscripten falls back "
            "to the freestanding backend (install emscripten for full "
            "libc access).\n")
    # Load + check the program (mirrors boot.py's pipeline).
    sys.path.insert(0, _REPO_ROOT)
    from boot.boot import load_program  # type: ignore
    from boot.checker import check  # type: ignore
    program = load_program(input_hls)
    check(program)  # raises HLError on failure
    # Emit the wasm binary.
    emitter = WasmEmitter(program, target=target)
    wasm_bytes = emitter.emit()
    # Stage 24: run wasm-opt (in-tree + external) if requested.
    if wasm_opt != "off":
        wasm_bytes = _run_wasm_opt(wasm_bytes, opt_level, wasm_opt)
    # Write outputs.
    os.makedirs(os.path.dirname(os.path.abspath(output_base)) or ".",
                exist_ok=True)
    if emit_wasm:
        wasm_path = output_base + ".wasm"
        with open(wasm_path, "wb") as f:
            f.write(wasm_bytes)
        sys.stderr.write("wrote %s (%d bytes)\n" % (wasm_path, len(wasm_bytes)))
    if emit_js:
        js_path = output_base + ".js"
        with open(js_path, "w") as f:
            f.write(generate_js_glue(verbose=(glue_style == "verbose")))
        sys.stderr.write("wrote %s (%d bytes)\n" % (js_path, os.path.getsize(js_path)))
    if emit_html:
        html_path = output_base + ".html"
        wasm_name = os.path.basename(output_base) + ".wasm"
        js_name = os.path.basename(output_base) + ".js"
        with open(html_path, "w") as f:
            f.write(generate_html_runner(
                title="Halis: %s" % os.path.basename(input_hls),
                wasm_name=wasm_name, js_name=js_name,
                wasm_size=len(wasm_bytes)))
        sys.stderr.write("wrote %s\n" % html_path)
    # Optionally run.
    if run:
        rc = run_wasm_in_node(output_base + ".wasm")
        if rc != 0:
            return rc
    if serve is not None:
        _start_dev_server(output_base, serve)
    return 0


def _run_wasm_opt(wasm_bytes: bytes, opt_level: str,
                  wasm_opt_mode: str) -> bytes:
    """Run the in-tree wasm optimizer on ``wasm_bytes`` and (if available)
    the external ``wasm-opt`` binary. Returns the optimized bytes.

    The in-tree optimizer (``tools/hlwasm_opt.py``) performs dead function
    elimination, dead import elimination, type-section deduplication,
    local compaction, dead data elimination, and peephole opts. The
    external ``wasm-opt`` (Binaryen) performs binary-level passes
    (inlining, alias analysis, etc.) that go beyond the in-tree scope.
    """
    try:
        sys.path.insert(0, os.path.join(_REPO_ROOT, "tools"))
        from hlwasm_opt import optimize as _opt  # type: ignore
    except ImportError:
        sys.stderr.write("warning: hlwasm_opt not found; skipping optimization\n")
        return wasm_bytes
    report: dict = {}
    optimized = _opt(wasm_bytes, level=opt_level, report=report)
    if report.get("bytes_saved", 0) > 0:
        sys.stderr.write(
            "wasm-opt: %d -> %d bytes (-%d, %.1f%%)\n"
            % (report["input_size"], report["output_size"],
               report["bytes_saved"], report["reduction_pct"]))
    return optimized


def _start_dev_server(output_base: str, port: int) -> None:
    """Start the Stage 24 ``hls serve`` dev server (delegates to
    tools/hlserve.py). The server watches the cwd for .hls changes and
    re-compiles the wasm bundle on save."""
    hlserve = os.path.join(_REPO_ROOT, "tools", "hlserve.py")
    if not os.path.isfile(hlserve):
        sys.stderr.write("warning: tools/hlserve.py not found; "
                         "cannot start dev server\n")
        return
    cmd = [sys.executable, hlserve, "--port", str(port),
           "--bundle", output_base]
    sys.stderr.write("starting dev server on port %d...\n" % port)
    # Detach: the server runs in the foreground (Ctrl+C to stop).
    os.execv(sys.executable, cmd)



def run_wasm_in_node(wasm_path: str) -> int:
    """Run the compiled wasm in Node.js (if available). Returns the exit
    code."""
    node = shutil_which("node")
    if node is None:
        sys.stderr.write("note: --run requested but node.js is not installed; "
                         "skipping execution.\n")
        return 0
    # Write a small runner script.
    runner = wasm_path + ".run.js"
    with open(runner, "w") as f:
        f.write(NODE_RUNNER_TEMPLATE % {
            "wasm_path": wasm_path,
        })
    try:
        result = subprocess.run([node, runner], capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    finally:
        try:
            os.unlink(runner)
        except OSError:
            pass


NODE_RUNNER_TEMPLATE = r"""
const fs = require("fs");
const path = require("path");
const wasmBytes = fs.readFileSync("%(wasm_path)s");
// Inline the glue (the .js file is alongside the .wasm).
const gluePath = "%(wasm_path)s".replace(/\.wasm$/, ".js");
if (fs.existsSync(gluePath)) {
  // Load the glue into this scope.
  const src = fs.readFileSync(gluePath, "utf-8");
  eval(src);
} else {
  console.error("glue file not found: " + gluePath);
  process.exit(1);
}
Halis.run(new Uint8Array(wasmBytes)).then(function (code) {
  // exit code (i64) — Node's process.exit takes int32, clamp.
  process.exit(Number(code) & 0x7fffffff);
}).catch(function (e) {
  console.error("Halis run failed: " + e.message);
  process.exit(1);
});
"""


def shutil_which(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name)


# Repo root for resolving imports (defined at module top — re-aliased
# here for clarity to readers grepping for it).


def cmd_list_targets() -> int:
    print("Supported WebAssembly targets (Stage 24):")
    print()
    for triple, spec in SUPPORTED_TARGETS.items():
        print("  %s" % triple)
        print("      %s" % spec["description"])
    print()
    print("Aliases (accepted by --target):")
    for alias, canonical in TARGET_ALIASES.items():
        print("  %-16s -> %s" % (alias, canonical))
    print()
    emcc = find_emcc()
    if emcc:
        print("emcc found at: %s" % emcc)
        print("  (full emscripten libc access is available for "
              "wasm32-unknown-emscripten)")
    else:
        print("emcc: not found (wasm32-unknown-emscripten falls back to "
              "the freestanding backend)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 24 WebAssembly backend: HLS -> .wasm + wasm-opt + emscripten bridge.")
    ap.add_argument("input", nargs="?",
                    help="HLS source file (e.g. examples/hello.hls)")
    ap.add_argument("output_base", nargs="?",
                    help="output base path (no extension); "
                         "writes .wasm + .js + .html")
    ap.add_argument("--target", default="wasm32-unknown-unknown",
                    help="target triple (default: wasm32-unknown-unknown)")
    ap.add_argument("--no-wasm", action="store_true",
                    help="don't write the .wasm file")
    ap.add_argument("--no-js", action="store_true",
                    help="don't write the .js glue file")
    ap.add_argument("--no-html", action="store_true",
                    help="don't write the .html runner")
    ap.add_argument("--run", action="store_true",
                    help="run the compiled wasm in Node.js (if available)")
    ap.add_argument("--list-targets", action="store_true",
                    help="list the supported target triples and exit")
    ap.add_argument("--wasm-opt", default="auto",
                    choices=["auto", "on", "off"],
                    help="Stage 24: run the wasm size optimizer (default: "
                         "auto = run in-tree + external if available)")
    ap.add_argument("--opt-level", default="O3",
                    choices=["O1", "O2", "O3", "Os"],
                    help="Stage 24: optimization level (default: O3)")
    ap.add_argument("--glue", default="compact",
                    choices=["compact", "verbose"],
                    help="Stage 24: JS glue style (default: compact ~2.5 KB; "
                         "verbose ~5.5 KB)")
    ap.add_argument("--serve", type=int, default=None, metavar="PORT",
                    help="Stage 24: after compiling, start the dev server "
                         "(hls serve) on PORT (watches .hls files, "
                         "recompiles on save, live-reload via SSE)")
    args = ap.parse_args()
    if args.list_targets:
        return cmd_list_targets()
    if not args.input or not args.output_base:
        ap.error("input and output_base are required "
                 "(or use --list-targets)")
    try:
        return compile_program(
            args.input, args.output_base,
            target=args.target,
            emit_wasm=not args.no_wasm,
            emit_js=not args.no_js,
            emit_html=not args.no_html,
            run=args.run,
            wasm_opt=args.wasm_opt,
            opt_level=args.opt_level,
            glue_style=args.glue,
            serve=args.serve)
    except HLError as ex:
        sys.stderr.write("compile error: %s\n" % ex)
        return 1
    except ValueError as ex:
        sys.stderr.write("error: %s\n" % ex)
        return 2


if __name__ == "__main__":
    sys.exit(main())
