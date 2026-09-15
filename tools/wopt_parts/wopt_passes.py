"""passes - verbatim segment of the original tools/hlwasm_opt.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from wopt_analysis import (
    find_live_functions, find_used_data_offsets, find_used_imports,
)
from wopt_common import (
    Dict, List, OP_BLOCK, OP_BR, OP_BR_IF, OP_BR_TABLE, OP_CALL, OP_CALL_INDIRECT,
    OP_ELSE, OP_END, OP_F32_CONST, OP_F64_CONST, OP_GLOBAL_GET, OP_GLOBAL_SET, OP_I32_CONST, OP_I32_EQZ,
    OP_I64_CONST, OP_IF, OP_LOCAL_GET, OP_LOCAL_SET, OP_LOCAL_TEE, OP_LOOP, OP_NOP, OP_RETURN,
    OP_TABLE_GET, OP_TABLE_SET, Optional, Set, Tuple, _MEM_OPS, read_sleb, read_uleb,
    sleb, uleb,
)
from wopt_module import (
    Code, DataSegment, Export, FuncType, Import, WasmModule,
)

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
    for i, (ty, code) in enumerate(zip(mod.funcs, mod.codes, strict=False)):
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
# Deep-scan-13 (Stage 24 rework): structure-aware code + data passes.
# ============================================================================
# The old optimizer removed whole functions / imports / data segments
# (DCE) but never looked INSIDE the surviving bodies or at the data
# SEGMENT layout. Two new O3 passes close that gap:
#
#   opt_code_clean — a strict, structure-aware walk of each body:
#     (1) `local.set X; local.get X` -> `local.tee X` (the get re-reads
#         exactly what the set stored; tee stores and keeps the value);
#     (2) `br 0` immediately before the `end` of a NON-loop frame -> drop
#         (the branch targets exactly where control falls through);
#     (3) a trailing top-level `return` -> drop (the implicit function
#         `end` returns the stack top / void identically).
#
#   opt_data_merge — merge data segments that are contiguous in memory
#     (or separated by a tiny gap) into ONE segment. The reference
#     emitter lays out one segment per string constant, which costs
#     ~6 bytes of segment header per string (159 strings in the Stage
#     24 web app = ~950 bytes of pure overhead). Gap bytes are written
#     as zeros — identical to the memory's initial state, so the
#     observable content is unchanged.
#
# The walker is STRICT: any opcode it does not know aborts the pass
# for that body (the original is kept). It can therefore never corrupt
# a construct it does not understand — including the 0xFC (bulk
# memory / reference-types) prefix instructions the emitter uses for
# memory.copy, which the older _scan_calls walker silently misparsed
# (a latent parser bug — fixed below).

class _WalkError(Exception):
    """Raised by _walk_instrs on an opcode the walker cannot parse."""


_PLAIN_OPS = frozenset(
    [0x00, 0x01, 0x0B, 0x0F, 0x1A, 0x1B, 0x05] +   # ctrl/plain
    list(range(0x45, 0xC5)) +                      # numeric/comparison
    [0xD0, 0xD1, 0xD2])                            # ref.* (not emitted)
_VALTYPES = frozenset([0x7F, 0x7E, 0x7D, 0x7C, 0x7B, 0x70, 0x6F])


def _walk_instrs(body: bytes):
    """Yield (start, op, end, frames) for each instruction in `body`.

    `start`/`end` are byte offsets (op at start, immediates up to end,
    end-exclusive). `frames` is the tuple of control-frame kinds OPEN at
    the point AFTER this instruction's opcode is consumed — used to
    distinguish loop back-edges from fall-through branches.

    Raises _WalkError on any opcode the walker cannot fully parse —
    callers must treat that as "leave this body alone".
    """
    pos = 0
    n = len(body)
    frames: List[str] = []
    while pos < n:
        start = pos
        op = body[pos]; pos += 1
        if op in (OP_BLOCK, OP_LOOP, OP_IF):
            # Block type: 0x40 (void) or a value type byte, or a
            # POSITIVE sleb type index (multi-value blocks).
            if pos >= n:
                raise _WalkError("truncated blocktype")
            bt = body[pos]
            if bt == 0x40 or bt in _VALTYPES:
                pos += 1
            else:
                while pos < n and (body[pos] & 0x80):
                    pos += 1
                pos += 1
            frames.append("loop" if op == OP_LOOP else "block")
            yield start, op, pos, tuple(frames)
            continue
        if op == OP_END:
            if frames:
                frames.pop()
            yield start, op, pos, tuple(frames)
            continue
        if op == OP_ELSE:
            # The else belongs to the innermost IF frame.
            if not frames or frames[-1] != "block":
                # An `if` also pushes a "block" frame; an `else` with no
                # open block frame is malformed.
                raise _WalkError("else without open if frame")
            yield start, op, pos, tuple(frames)
            continue
        if op in _PLAIN_OPS:
            yield start, op, pos, tuple(frames)
            continue
        if op == OP_CALL or op == OP_BR or op == OP_BR_IF \
                or op == OP_LOCAL_GET or op == OP_LOCAL_SET \
                or op == OP_LOCAL_TEE or op == OP_GLOBAL_GET \
                or op == OP_GLOBAL_SET or op == OP_TABLE_GET \
                or op == OP_TABLE_SET:
            _, pos = read_uleb(body, pos)
            yield start, op, pos, tuple(frames)
            continue
        if op == OP_CALL_INDIRECT:
            _, pos = read_uleb(body, pos)
            _, pos = read_uleb(body, pos)
            yield start, op, pos, tuple(frames)
            continue
        if op == OP_BR_TABLE:
            cnt, pos = read_uleb(body, pos)
            for _ in range(cnt + 1):
                _, pos = read_uleb(body, pos)
            yield start, op, pos, tuple(frames)
            continue
        if op == OP_I32_CONST or op == OP_I64_CONST:
            _, pos = read_sleb(body, pos)
            yield start, op, pos, tuple(frames)
            continue
        if op == OP_F32_CONST:
            pos += 4
            yield start, op, pos, tuple(frames)
            continue
        if op == OP_F64_CONST:
            pos += 8
            yield start, op, pos, tuple(frames)
            continue
        if op in _MEM_OPS:
            _, pos = read_uleb(body, pos)
            _, pos = read_uleb(body, pos)
            yield start, op, pos, tuple(frames)
            continue
        if op == 0x1C:  # select_t
            cnt, pos = read_uleb(body, pos)
            for _ in range(cnt):
                pos += 1
            yield start, op, pos, tuple(frames)
            continue
        if op == 0xFC:  # misc prefix (bulk memory / tables)
            if pos >= n:
                raise _WalkError("truncated 0xFC")
            sub, pos = read_uleb(body, pos)
            if sub == 0x08:      # memory.init dst, src
                _, pos = read_uleb(body, pos)
                _, pos = read_uleb(body, pos)
            elif sub == 0x09:    # data.drop
                _, pos = read_uleb(body, pos)
            elif sub == 0x0A:    # memory.copy dst, src
                _, pos = read_uleb(body, pos)
                _, pos = read_uleb(body, pos)
            elif sub == 0x0B:    # memory.fill
                _, pos = read_uleb(body, pos)
            elif sub in (0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13,
                         0x14, 0x15, 0x16, 0x17):
                # table.init / table.copy / elem.drop variants
                if sub in (0x0C, 0x0E, 0x0F, 0x11, 0x13, 0x15):
                    _, pos = read_uleb(body, pos)
                    _, pos = read_uleb(body, pos)
                else:
                    _, pos = read_uleb(body, pos)
            else:
                raise _WalkError("unknown 0xFC sub-opcode %d" % sub)
            yield start, op, pos, tuple(frames)
            continue
        raise _WalkError("unknown opcode 0x%02x at %d" % (op, start))


def opt_code_clean(mod: WasmModule, report: dict):
    """Structure-aware body cleanup (see the module comment above)."""
    saved = 0
    for code in mod.codes:
        try:
            new_body, s = _clean_body(code.body)
        except _WalkError:
            # Unknown construct: leave the body untouched (never corrupt
            # what we do not fully understand).
            continue
        saved += s
        code.body = new_body
    report["code_clean_saved"] = saved


def _clean_body(body: bytes) -> Tuple[bytes, int]:
    instrs = list(_walk_instrs(body))
    if not instrs:
        return body, 0
    drop = set()          # instruction start offsets to remove
    tee_set = set()       # local.set starts that become local.tee
    for i, (start, op, end, frames) in enumerate(instrs):
        # (1) local.set X; local.get X -> local.tee X
        if op == OP_LOCAL_SET and i + 1 < len(instrs):
            nstart, nop, nend, _ = instrs[i + 1]
            if nop == OP_LOCAL_GET and body[start + 1:end] == body[nstart + 1:nend]:
                # Replace the set with a tee and drop the get.
                drop.add(nstart)
                tee_set.add(start)
                continue
        if op == OP_BR:
            depth, _ = read_uleb(body, start + 1)
            if depth == 0 and i + 1 < len(instrs):
                nstart, nop, nend, _ = instrs[i + 1]
                if nop == OP_END and frames and frames[-1] != "loop":
                    # (2) br 0 into the immediately following end of a
                    # block/if frame: control falls through to exactly
                    # the same place. (A loop frame would make br 0 a
                    # BACK EDGE — never removed.)
                    drop.add(start)
    # (3) trailing top-level return: the last instruction of the body
    # is a `return` with no open frames — the serializer's final `end`
    # returns the stack top / void identically.
    if instrs:
        lstart, lop, lend, lframes = instrs[-1]
        if lop == OP_RETURN and not lframes:
            drop.add(lstart)
    if not drop and not tee_set:
        return body, 0
    out = bytearray()
    saved = 0
    for start, op, end, _frames in instrs:
        if start in drop:
            saved += end - start
            continue
        if start in tee_set:
            # local.set X -> local.tee X (0x21 -> 0x22, same immediates)
            out.append(OP_LOCAL_TEE)
            out += body[start + 1:end]
            continue
        out += body[start:end]
    return bytes(out), saved


def opt_data_merge(mod: WasmModule, report: dict):
    """Merge data segments that are contiguous (or nearly so) in memory.

    The reference emitter emits one active segment per string constant;
    each costs a ~6-byte header (flags + i32.const offset + end + uleb
    length). A 159-string program pays ~950 bytes of pure overhead for
    what is semantically ONE contiguous data region. Merging keeps the
    byte-for-byte memory content identical (small gaps are filled with
    zeros, which is the memory's initial state — untouched gap bytes
    are zeros too) while collapsing the headers.
    """
    if len(mod.data) <= 1:
        report["data_segments_merged"] = 0
        report["data_merge_saved"] = 0
        return
    segs = sorted(mod.data, key=lambda s: s.offset)
    # Reject overlapping segments outright (never emitted; defensively
    # keep the original if seen).
    for i in range(len(segs) - 1):
        if segs[i].offset + len(segs[i].data) > segs[i + 1].offset:
            report["data_segments_merged"] = 0
            report["data_merge_saved"] = 0
            return
    # Merge a gap only when filling it with zeros is cheaper than the
    # ~7-byte header (offset sleb + length uleb + flags) a separate
    # segment would cost.
    GAP_LIMIT = 6
    merged: List[DataSegment] = []
    cur_off = segs[0].offset
    cur_data = bytearray(segs[0].data)
    for seg in segs[1:]:
        gap = seg.offset - (cur_off + len(cur_data))
        if gap <= GAP_LIMIT:
            cur_data += b"\x00" * gap
            cur_data += seg.data
        else:
            merged.append(DataSegment(cur_off, bytes(cur_data)))
            cur_off = seg.offset
            cur_data = bytearray(seg.data)
    merged.append(DataSegment(cur_off, bytes(cur_data)))
    # Exact byte accounting from the serialized form.
    def _sec_bytes(segs):
        n = 1  # count uleb
        for s in segs:
            n += 1 + 1 + len(sleb(s.offset)) + 1 + len(uleb(len(s.data))) + len(s.data)
        return n
    before_bytes = _sec_bytes(segs)
    after_bytes = _sec_bytes(merged)
    report["data_segments_merged"] = len(segs) - len(merged)
    report["data_merge_saved"] = max(0, before_bytes - after_bytes)
    mod.data = merged


def _uses_bulk_data_ops(mod: WasmModule) -> Optional[bool]:
    """True if any body uses memory.init (0xFC 0x08) or data.drop
    (0xFC 0x09) — the two instructions that REQUIRE a DataCount section.
    False if definitively not. None if a body cannot be parsed (the
    caller keeps the DataCount section — conservative)."""
    for code in mod.codes:
        try:
            instrs = list(_walk_instrs(code.body))
        except _WalkError:
            return None
        for start, op, end, _frames in instrs:
            if op == 0xFC:
                sub, _ = read_uleb(code.body, start + 1)
                if sub == 0x08 or sub == 0x09:
                    return True
    return False


def opt_dead_types(mod: WasmModule, report: dict):
    """Remove type-section entries no longer referenced after DCE.

    DCE may remove every function of a signature, leaving the type in
    the type section. Removing it requires renumbering every type
    reference: imports (kind 0), the function section, and the
    call_indirect immediates inside bodies. Two-phase: build the full
    plan first, apply only if every body parses (an unknown construct
    aborts the whole pass — indices must stay consistent everywhere).
    """
    referenced: Set[int] = set()
    for imp in mod.imports:
        if imp.kind == 0:
            referenced.add(imp.type_idx)
    for tidx in mod.funcs:
        referenced.add(tidx)
    # call_indirect type indices from every body.
    for ci, code in enumerate(mod.codes):
        try:
            instrs = list(_walk_instrs(code.body))
        except _WalkError:
            report["dead_types_removed"] = 0
            return
        for start, op, end, _frames in instrs:
            if op == OP_CALL_INDIRECT:
                tidx, _ = read_uleb(code.body, start + 1)
                referenced.add(tidx)
    live = sorted(t for t in referenced if 0 <= t < len(mod.types))
    if len(live) == len(mod.types):
        report["dead_types_removed"] = 0
        return
    remap = {old: new for new, old in enumerate(live)}
    # Phase 2: apply. Body rewrites need the walker again (same result).
    for ci, code in enumerate(mod.codes):
        try:
            instrs = list(_walk_instrs(code.body))
        except _WalkError:
            # Should not happen (phase 1 walked the same bodies).
            report["dead_types_removed"] = 0
            return
        out = bytearray()
        for start, op, end, _frames in instrs:
            if op == OP_CALL_INDIRECT:
                tidx, rest = read_uleb(code.body, start + 1)
                tbl, _ = read_uleb(code.body, rest)
                out.append(OP_CALL_INDIRECT)
                out += uleb(remap.get(tidx, tidx))
                out += uleb(tbl)
            else:
                out += code.body[start:end]
        code.body = bytes(out)
    for imp in mod.imports:
        if imp.kind == 0:
            imp.type_idx = remap.get(imp.type_idx, imp.type_idx)
    n_before = len(mod.types)
    mod.funcs = [remap.get(t, t) for t in mod.funcs]
    mod.types = [mod.types[t] for t in live]
    report["dead_types_removed"] = n_before - len(mod.types)


# ============================================================================
# Serialization — turn the optimized module back into wasm bytes.
# ============================================================================



__all__ = [
    "_PLAIN_OPS",
    "_VALTYPES",
    "_WalkError",
    "_clean_body",
    "_peephole_body",
    "_renumber_call_indirect",
    "_renumber_calls",
    "_uses_bulk_data_ops",
    "_walk_instrs",
    "opt_code_clean",
    "opt_data_merge",
    "opt_dce",
    "opt_dead_data",
    "opt_dead_types",
    "opt_local_compact",
    "opt_peephole",
    "opt_type_dedup",
]
