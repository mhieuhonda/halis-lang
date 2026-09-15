"""module - verbatim segment of the original tools/hlwasm.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hwasm_common import (
    I32, I64, List, OP_END, OP_I32_CONST, OP_I64_CONST, Optional, SEC_CODE,
    SEC_DATA, SEC_DATA_COUNT, SEC_EXPORT, SEC_FUNCTION, SEC_GLOBAL, SEC_IMPORT, SEC_MEMORY, SEC_START,
    SEC_TYPE, Set, Tuple, section, sleb, uleb,
)

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

def _called_fn_names(node) -> Set[str]:
    """Every plain-call NAME appearing anywhere in a boot-AST program
    value (dicts/lists, recursively). Deep-scan-23: the wasm emitter
    lowers every function in the program, so an extern "js" import must
    be declared iff SOME emitted call may reference it — i.e. iff a
    call node with that name exists anywhere in the same program dict
    the emitter walks (function bodies, struct defaults, everything).
    Sound by construction; independent of the checker's edge bookkeeping.
    """
    acc: Set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            k = cur.get("k")
            if k == "call" and isinstance(cur.get("name"), str):
                acc.add(cur["name"])
            elif k == "methodcall" and isinstance(cur.get("name"), str):
                acc.add(cur["name"])
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return acc




__all__ = [
    "WasmModule",
    "_called_fn_names",
]
