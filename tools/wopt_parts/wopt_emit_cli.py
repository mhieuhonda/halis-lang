"""emit_cli - verbatim segment of the original tools/hlwasm_opt.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from wopt_common import (
    I32, I64, OP_END, OP_I32_CONST, OP_I64_CONST, Optional, SEC_CODE, SEC_DATA,
    SEC_DATA_COUNT, SEC_EXPORT, SEC_FUNCTION, SEC_GLOBAL, SEC_IMPORT, SEC_MEMORY, SEC_START, SEC_TYPE,
    argparse, os, shutil, sleb, subprocess, sys, uleb,
)
from wopt_module import (
    WasmModule,
)
from wopt_passes import (
    _uses_bulk_data_ops, opt_code_clean, opt_data_merge, opt_dce, opt_dead_data, opt_dead_types, opt_local_compact, opt_peephole,
    opt_type_dedup,
)

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
    # DataCount (must precede Code section when Data is present; only
    # REQUIRED when memory.init / data.drop are used — see the
    # bulk-memory proposal. optimize() sets mod.emit_datacount).
    if getattr(mod, "emit_datacount", True):
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
    # O2: type dedup + dead data + data-segment merging.
    if level in ("O2", "O3", "Os"):
        opt_type_dedup(mod, report)
        opt_dead_data(mod, report)
        # Deep-scan-13: merge contiguous data segments (one active
        # segment per string constant is ~6 bytes of header each; the
        # Stage 24 web app paid ~950 bytes for a single contiguous
        # region).
        opt_data_merge(mod, report)
    # O3: peephole + structure-aware code cleanup.
    if level in ("O3", "Os"):
        opt_peephole(mod, report)
        # Deep-scan-13: local.set/get -> tee, redundant br-0-into-end,
        # trailing returns.
        opt_code_clean(mod, report)
        # Deep-scan-13: dead type-section entries after DCE (with full
        # index remapping across imports / funcs / call_indirect).
        opt_dead_types(mod, report)
    # Deep-scan-13: the DataCount section is only REQUIRED for
    # memory.init / data.drop (bulk-memory proposal). Our emitter's
    # memory.copy does not need it; drop the 3-byte section when the
    # bodies provably never use those two instructions. Conservative:
    # any unparseable body keeps the section. (With no data segments at
    # all there is nothing to count either.)
    mod.emit_datacount = bool(mod.data) and (_uses_bulk_data_ops(mod) is not False)
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
    # Deep-scan-25 fix: malformed input used to escape as a raw Python
    # traceback (e.g. ValueError "not a wasm binary (magic mismatch)").
    # Every sibling tool prints a one-line clean error for bad input —
    # mirror that (the output file is only written on success, so no
    # corruption was possible; this is purely an error-reporting fix).
    try:
        optimized = optimize(wasm_bytes, level=args.level,
                             external_wasm_opt=args.external_wasm_opt,
                             report=report)
    except (ValueError, IndexError, KeyError, TypeError, NotImplementedError,
            OSError) as ex:
        sys.stderr.write("error: %s: not a valid wasm module (%s)\n"
                         % (args.input, ex))
        return 1
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




__all__ = [
    "_run_external_wasm_opt",
    "main",
    "optimize",
    "section",
    "serialize",
]
