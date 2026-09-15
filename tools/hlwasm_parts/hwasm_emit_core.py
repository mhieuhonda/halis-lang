"""emit_core - verbatim segment of the original tools/hlwasm.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hwasm_common import (
    Dict, F64, HLError, I32, I64, List, OP_CALL, OP_I32_CONST,
    OP_I64_EXTEND_I32_S, OP_LOCAL_GET, Tuple, hls_to_wasm_result, hls_to_wasm_valtype, sleb, struct, uleb,
)
from hwasm_module import (
    WasmModule, _called_fn_names,
)

class WasmEmitterCore(object):
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
        # Deep-scan-20: wasm frame counter for br-depth computation
        # (reset per function in _emit_function; see break/continue).
        self._frames = 0
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
        # Stage 73 (v0.92.0-alpha): struct field layouts (name ->
        # [(field_name, wasm_abi_type, offset)]) and total sizes,
        # computed by _compute_struct_layouts() for every non-generic
        # struct. They feed the hl_struct_descriptors export so the JS
        # glue can AUTO-register marshalling descriptors ("a Halis
        # struct becomes a JS object", the roadmap's Stage 73 promise).
        self._struct_layouts: Dict[str, List[Tuple[str, str, int]]] = {}
        self._struct_sizes: Dict[str, int] = {}

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
        # Step 0 (Stage 73): compute struct field layouts for the
        # auto-marshalling descriptors.
        self._compute_struct_layouts()
        # Step 4: emit user functions.
        for fname, fn in self.program["fns"].items():
            if fn.get("extern", False):
                continue  # extern decls are imports, not defined funcs
            self._emit_function(fname, fn)
        # Step 4b (Stage 73): the JS interop exports —
        #   hl_struct_descriptors() -> str ptr  (auto-registered on the
        #                                     JS side at instantiation)
        #   hl_call_halis(cb_id, arg_ptr) -> ret ptr (JS->HLS callbacks;
        #                                     only when the program
        #                                     defines jsffi_on_callback)
        self._emit_stage73_helpers()
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

    # ---------- Stage 73: struct layouts + JS interop exports ----------

    def _compute_struct_layouts(self):
        """Stage 73: compute field offsets for every non-generic struct.

        The wasm ABI passes structs as i32 pointers (the same convention
        as the C backend), so the emitter never needed a layout before.
        Stage 73 introduces AUTO-MARSHALLING: the JSON descriptor
        emitted by hl_struct_descriptors() must agree with the JS glue's
        reader (Halis.readStruct), so the layout is computed HERE and
        shipped to the JS side at instantiation time.

        Layout rules (mirroring the glue's sz()/readStruct):
          int    -> i64, size 8, align 8
          float  -> f64, size 8, align 8
          bool   -> i32, size 4, align 4
          str    -> i32 pointer, size 4, align 4
          other  -> i32 pointer (nested struct / list / map / enum),
                    size 4, align 4
        The struct size is rounded up to its alignment (max field
        align). Generic structs are skipped (they monomorphise at use
        sites, which this emitter does not lower anyway).
        """
        for name in sorted(self.program.get("structs", {}) or {}):
            st = self.program["structs"][name]
            if st.get("typeparams"):
                continue
            off = 0
            max_align = 4
            fields: List[Tuple[str, str, int]] = []
            for (fname, ftype, _default) in st["fields"]:
                if ftype == "int":
                    wty, size = "i64", 8
                elif ftype == "float":
                    wty, size = "f64", 8
                elif ftype == "bool":
                    wty, size = "bool", 4
                elif ftype == "str":
                    wty, size = "str", 4
                else:
                    # Nested struct / list / map / enum / generic: the
                    # wasm ABI passes them as raw i32 pointers.
                    wty, size = "ptr", 4
                align = size
                off = (off + align - 1) & ~(align - 1)
                fields.append((fname, wty, off))
                off += size
                if align > max_align:
                    max_align = align
            size = (off + max_align - 1) & ~(max_align - 1)
            self._struct_layouts[name] = fields
            self._struct_sizes[name] = size

    def _struct_descriptors_json(self) -> bytes:
        """Render the struct-layout table as compact JSON (the body of
        the hl_struct_descriptors() return string). Field order follows
        the struct definition order; structs are sorted by name so the
        output is deterministic."""
        import json as _json
        out = []
        for name in sorted(self._struct_layouts.keys()):
            fields = [
                {"name": fn, "type": wt, "offset": off}
                for (fn, wt, off) in self._struct_layouts[name]
            ]
            out.append({
                "name": name,
                "size": self._struct_sizes[name],
                "fields": fields,
            })
        return _json.dumps(out, separators=(",", ":")).encode("utf-8")

    def _emit_stage73_helpers(self):
        """Stage 73: emit the JS interop exports.

        hl_struct_descriptors() -> str ptr
            Returns the JSON layout table for every non-generic struct
            in the program. The JS glue reads it right after
            instantiation and calls Halis.registerStruct for each entry
            — "a Halis struct becomes a JS object" with ZERO manual
            registration.

        hl_call_halis(cb_id: i32, arg_ptr: i32) -> ret ptr
            The JS->HLS callback entry. Emitted ONLY when the program
            defines ``fn jsffi_on_callback(cb_id: int, arg: str) -> str``
            (a plain HLS function — the user's dispatch table, the
            std.http_router handler_id pattern applied to FFI). The JS
            side reaches it through Halis.callHalis(cbId, json).
        """
        # --- hl_struct_descriptors ---
        if self._struct_layouts:
            json_bytes = self._struct_descriptors_json()
            off = self._intern_str(json_bytes)
            idx = self._add_helper("hl_struct_descriptors", [], [I32])
            body = bytearray()
            body.append(OP_I32_CONST); body += sleb(off)
            # NOTE: WasmModule.final_bytes appends the function's
            # closing OP_END itself — do NOT add one here.
            self.mod.add_code([], bytes(body))
            self.mod.add_export("hl_struct_descriptors", 0x00, idx)
        # --- hl_call_halis ---
        fns = self.program["fns"]
        cb = fns.get("jsffi_on_callback")
        if cb is not None and not cb.get("extern", False):
            params = cb.get("params", [])
            ok_sig = (len(params) == 2 and params[0][1] == "int"
                      and params[1][1] == "str"
                      and cb.get("ret") == "str")
            if not ok_sig:
                raise HLError(
                    "jsffi_on_callback must have the exact signature "
                    "fn jsffi_on_callback(cb_id: int, arg: str) -> str "
                    "(found a different shape)", cb.get("line", 0), 0)
            idx_cb = self.func_index["jsffi_on_callback"]
            idx = self._add_helper("hl_call_halis", [I32, I32], [I32])
            body = bytearray()
            # cb_id arrives as i32 (JS numbers); the HLS fn takes int
            # (i64) — sign-extend on the way through.
            body.append(OP_LOCAL_GET); body += uleb(0)
            body.append(OP_I64_EXTEND_I32_S)
            body.append(OP_LOCAL_GET); body += uleb(1)
            body.append(OP_CALL); body += uleb(idx_cb)
            # final_bytes appends the closing OP_END.
            self.mod.add_code([], bytes(body))
            self.mod.add_export("hl_call_halis", 0x00, idx)

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
        # Deep-scan-23: declare only the externs the program ACTUALLY
        # calls somewhere (a static walk of the same AST the emitter
        # lowers). Importing std.jsffi wholesale no longer drags its
        # whole FFI surface into every wasm module and its JS glue —
        # only the imports that can truly be referenced are emitted.
        called = _called_fn_names(self.program)
        for ext in self.program.get("externs", []):
            if ext["abi"] != "js":
                continue
            for fn in ext["decls"]:
                if fn["name"] not in called:
                    continue
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
        # Deep-scan-20 fix: abort(code) takes an HLS int (i64) — the
        # helper was declared with an i32 param (same i64/i32 mismatch
        # as hl_exit; the call site pushes i64).
        idx_abort = self._add_helper("hl_abort", [I64], [])
        # Deep-scan-20 fix: exit(code) takes an HLS int (i64) — the
        # helper was declared with an i32 param, so the call site pushed
        # i64 and the module failed validation ("call[0] expected type
        # i32, found i64"). Take the code as i64 (ignored by the trap).
        idx_exit = self._add_helper("hl_exit", [I64], [])
        # Deep-scan-25 fix (soundness): CHECKED ARITHMETIC. The SPEC
        # (§7 "every operation is checked") and the C runtime panic on
        # int64 overflow, but the wasm backend emitted bare i64.add /
        # i64.sub / i64.mul, which WRAP silently — a wasm binary printed
        # INT64_MAX+1 as INT64_MIN where the interpreter and the native
        # binary panic. Add overflow-checking helpers (same convention
        # as _emit_hl_int_abs: trap on the overflow event, never wrap).
        # i64.div_s / i64.rem_s already trap on /0 and INT64_MIN/-1
        # (div), matching the HLS panics; rem is checked explicitly
        # because wasm DEFINES INT64_MIN % -1 == 0 where HLS panics.
        idx_add = self._add_helper("hl_checked_add", [I64, I64], [I64])
        idx_sub = self._add_helper("hl_checked_sub", [I64, I64], [I64])
        idx_mul = self._add_helper("hl_checked_mul", [I64, I64], [I64])
        idx_rem = self._add_helper("hl_checked_rem", [I64, I64], [I64])
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
        self._emit_hl_checked_add(idx_add)
        self._emit_hl_checked_sub(idx_sub)
        self._emit_hl_checked_mul(idx_mul)
        self._emit_hl_checked_rem(idx_rem)

    # ---- helper: hl_alloc(n: i32) -> ptr ----
    # Reads the heap pointer from HEAP_PTR_ADDR, returns it, then advances
    # the heap pointer by n. Uses local 1 as the temp (local 0 is the
    # parameter n — do NOT clobber it).


__all__ = [
]
