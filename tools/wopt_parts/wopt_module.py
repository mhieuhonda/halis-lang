"""module - verbatim segment of the original tools/hlwasm_opt.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from wopt_common import (
    List, OP_BLOCK, OP_END, OP_F32_CONST, OP_F64_CONST, OP_GLOBAL_GET, OP_I32_CONST, OP_I64_CONST,
    OP_IF, OP_LOOP, Optional, SEC_CODE, SEC_DATA, SEC_DATA_COUNT, SEC_EXPORT, SEC_FUNCTION,
    SEC_GLOBAL, SEC_IMPORT, SEC_MEMORY, SEC_START, SEC_TYPE, Tuple, read_sleb, read_uleb,
    struct,
)

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



__all__ = [
    "Code",
    "DataSegment",
    "Export",
    "FuncType",
    "Import",
    "WasmModule",
]
