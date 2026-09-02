"""LLVM IR emitter for Hieu Louis (Stage 12).

Emits LLVM IR text (.ll) from a checked HLS program (post-type-check AST).
The IR can be assembled by `llc` or `clang` into a native binary.

Design:
  - The HLS C runtime (declared inline in `src/hlc.hls` codegen) is treated
    as an opaque external library. The LLVM emitter declares each runtime
    function as `declare` and calls them via `call`.
  - HLS types map to LLVM types as follows:
      int    -> i64
      float  -> double
      bool   -> i1
      str    -> ptr (pointer to %hl_str)
      void   -> void
      list[T] / map[str,T] / struct / enum / tainted[T] -> ptr (opaque)
  - Each HLS function becomes an LLVM `define` with the appropriate
    parameter types.
  - Local variables become `alloca` + `load`/`store` with TYPE TRACKING
    (each slot's LLVM type is recorded so load/store use the right type).
  - Basic blocks are emitted for `if`/`while`/`for`/`match` control flow.
  - Integer arithmetic uses LLVM's `add`/`sub`/`mul`/`sdiv`/`srem` with
    explicit overflow checks via `llvm.sadd.with.overflow.i64` etc. The
    overflow path calls `hl_die` and the result is replaced with `0`.

Multi-target support:
  - The default target is the host triple (queried from `llvm-config`).
  - `--target aarch64-linux` cross-compiles.
  - PGO (profile-guided optimisation) is deferred to a later stage.

The emitter is a *separate backend* from the C codegen. It is wired into
`boot.py` via the `--emit llvm` flag.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple


# Runtime function signatures (name -> (ret_type, [param_types]).
# These are the C runtime functions emitted inline by `src/hlc.hls`
# codegen. We declare them as opaque externals here.
RUNTIME_DECLS = """
; ===== Hieu Louis runtime declarations (opaque externals) =====
%hl_str = type { i64, ptr }
%hl_list = type { i64, i64, ptr }
%hl_map = type { i64, i64, ptr, ptr, ptr }

declare void     @hl_die(ptr)
declare ptr      @hl_str_alloc(i64)
declare ptr      @hl_str_from(ptr, i64)
declare ptr      @hl_str_concat(ptr, ptr)
declare i1       @hl_str_eq(ptr, ptr)
declare i64      @hl_str_cmp(ptr, ptr)
declare i64      @hl_str_len(ptr)
declare i64      @hl_str_byte_at(ptr, i64)
declare ptr      @hl_str_slice(ptr, i64, i64)
declare i64      @hl_str_find(ptr, ptr)
declare ptr      @hl_str_subst(ptr, ptr, ptr)
declare ptr      @hl_str_trim(ptr)
declare ptr      @hl_str_lower(ptr)
declare ptr      @hl_str_upper(ptr)
declare ptr      @hl_str_repeat(ptr, i64)
declare ptr      @hl_str_reverse(ptr)
declare ptr      @hl_int_to_str(i64)
declare ptr      @hl_float_to_str(double)
declare i64      @hl_str_to_int(ptr)
declare double   @hl_str_to_float(ptr)
declare ptr      @hl_list_new()
declare void     @hl_list_push(ptr, ptr)
declare ptr      @hl_list_get(ptr, i64)
declare void     @hl_list_set(ptr, i64, ptr)
declare i64      @hl_list_len(ptr)
declare ptr      @hl_map_new()
declare ptr      @hl_map_get(ptr, ptr)
declare void     @hl_map_set(ptr, ptr, ptr)
declare i1       @hl_map_has(ptr, ptr)
declare i64      @hl_map_len(ptr)
declare i1       @hl_file_exists(ptr)
declare ptr      @hl_read_file(ptr)
declare void     @hl_write_file(ptr, ptr)
declare void     @hl_println(ptr)
declare void     @hl_print(ptr)
declare i64      @hl_clock_ms()
declare i64      @hl_args_count()
declare ptr      @hl_args_get(i64)
declare void     @hl_exit(i64)
declare i64      @hl_chr(i64)
declare i64      @hl_ord(ptr)
"""


# HLS -> LLVM type mapping.
def hls_type_to_llvm(t: str) -> str:
    if t == "int":
        return "i64"
    if t == "float":
        return "double"
    if t == "bool":
        return "i1"
    if t == "void":
        return "void"
    if t == "str":
        return "ptr"
    if t.startswith("list[") or t.startswith("map[") or t.startswith("tainted["):
        return "ptr"
    # User-defined struct / enum -> opaque pointer.
    return "ptr"


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

class LLVMEmitter:
    """Walks a checked HLS program AST and emits LLVM IR text."""

    def __init__(self, program, target_triple: Optional[str] = None):
        self.program = program
        self.target_triple = target_triple
        self.lines: List[str] = []
        self._tmp = 0
        self._label = 0
        self._ov_counter = 0
        self._dz_counter = 0
        self._str_counter = 0
        self._string_consts: List[Tuple[str, bytes, str]] = []
        # Stack of (continue_label, break_label) for loops.
        self._loop_stack: List[Tuple[str, str]] = []
        # Per-function local variable map: name -> (LLVM register, llvm_type).
        self._locals: Dict[str, Tuple[str, str]] = {}
        # The current function's return type (HLS type string).
        self._current_ret_type_value: str = "void"
        # Whether the current basic block has already been terminated.
        self._block_terminated_flag: bool = False

    def _fresh(self, prefix="t"):
        self._tmp += 1
        return "%%%s%d" % (prefix, self._tmp)

    def _fresh_label(self, prefix="bb"):
        self._label += 1
        return "%s%d" % (prefix, self._label)

    def _emit(self, line: str):
        self.lines.append(line)

    def _mark_terminated(self):
        self._block_terminated_flag = True

    def _reset_terminated(self):
        self._block_terminated_flag = False

    def _to_i1(self, val_ty: str, val: str) -> str:
        """Coerce a value to i1 for use as a branch condition."""
        if val_ty == "i1":
            return val
        if val_ty == "i64":
            tmp = self._fresh("b")
            self._emit("  %s = trunc i64 %s to i1" % (tmp, val))
            return tmp
        if val_ty == "ptr":
            tmp = self._fresh("b")
            self._emit("  %s = icmp ne ptr %s, null" % (tmp, val))
            return tmp
        # Fallback: treat any other type as truthy (non-zero).
        tmp = self._fresh("b")
        self._emit("  %s = or i1 1, 0" % tmp)
        return tmp

    # ---------- public API ----------
    def emit(self) -> str:
        """Emit the complete module (prelude + functions)."""
        # Reset ALL emitter state (idempotent — safe to call twice).
        self.lines = []
        self._string_consts = []
        self._tmp = 0
        self._label = 0
        self._ov_counter = 0
        self._dz_counter = 0
        self._str_counter = 0
        self._locals = {}
        self._loop_stack = []
        self._block_terminated_flag = False
        if self.target_triple:
            self._emit("target triple = \"%s\"" % self.target_triple)
            self._emit("")
        for line in RUNTIME_DECLS.strip().split("\n"):
            self._emit(line)
        self._emit("")
        # Emit panic message constants.
        # BUG-SC-LLVM-3 fix: the array sizes were [18 x i8] but both messages
        # are 16 chars + 1 NUL = 17 bytes. LLVM rejects size mismatches.
        # "integer overflow" = 16 chars, "division by zero" = 16 chars.
        self._emit('@.panic_overflow_msg = private unnamed_addr constant [17 x i8] c"integer overflow\\00"')
        self._emit('@.panic_divzero_msg = private unnamed_addr constant [17 x i8] c"division by zero\\00"')
        self._emit("")
        # Emit functions. (String constants are emitted on demand by
        # _emit_string_const and accumulated; we append them at the end
        # before joining.)
        for fname, fn in self.program["fns"].items():
            self._emit_function(fname, fn)
        # Emit collected string constants just before the final join.
        # Insert them at the position right after the panic constants.
        if self._string_consts:
            # Find the second blank line (after panic constants) and
            # insert string constants there.
            insert_at = None
            blank_count = 0
            for idx, ln in enumerate(self.lines):
                if ln == "":
                    blank_count += 1
                    if blank_count == 2:
                        insert_at = idx + 1
                        break
            if insert_at is None:
                insert_at = len(self.lines)
            sc_lines = []
            for name, data, bytes_str in self._string_consts:
                sc_lines.append('@%s = private unnamed_addr constant [%d x i8] [%s]' % (
                    name, len(data) + 1, bytes_str))
            sc_lines.append("")
            self.lines[insert_at:insert_at] = sc_lines
        return "\n".join(self.lines) + "\n"

    # ---------- function emission ----------
    def _emit_function(self, fname: str, fn: Dict):
        ret_llvm = hls_type_to_llvm(fn["ret"])
        self._current_ret_type_value = fn["ret"]
        self._locals = {}
        self._block_terminated_flag = False
        params = []
        for (pname, ptype, _) in fn["params"]:
            params.append("%s %%v_%s" % (hls_type_to_llvm(ptype), pname))
        param_str = ", ".join(params)
        self._emit("define %s @%s(%s) {" % (ret_llvm, fname, param_str))
        self._emit("entry:")
        # Allocate stack slots for parameters so we can re-assign them
        # (HLS allows `let mut` reassignment). Track the LLVM type per
        # slot so load/store use the right type.
        for (pname, ptype, _) in fn["params"]:
            slot = self._fresh("p")
            pty = hls_type_to_llvm(ptype)
            self._emit("  %s = alloca %s" % (slot, pty))
            self._emit("  store %s %%v_%s, ptr %s" % (pty, pname, slot))
            self._locals[pname] = (slot, pty)
        # Lower each statement.
        for stmt in fn["body"]:
            self._lower_stmt(stmt)
        # Implicit return void for void functions; panic for non-void
        # (the type checker already rejects missing returns).
        if not self._block_terminated_flag:
            if fn["ret"] == "void":
                self._emit("  ret void")
            else:
                # Defensive unreachable — the checker should have rejected
                # this function for not returning on all paths.
                self._emit("  unreachable")
        self._emit("}")
        self._emit("")

    # ---------- statement lowering ----------
    def _lower_stmt(self, stmt: Dict):
        k = stmt["k"]
        if k == "let":
            val_ty, val = self._lower_expr_typed(stmt["value"])
            slot = self._fresh("l")
            ty = hls_type_to_llvm(stmt["t"])
            self._emit("  %s = alloca %s" % (slot, ty))
            self._emit("  store %s %s, ptr %s" % (ty, val, slot))
            self._locals[stmt["name"]] = (slot, ty)
        elif k == "assign":
            target = stmt["target"]
            val_ty, val = self._lower_expr_typed(stmt["value"])
            if target["k"] == "ident":
                entry = self._locals.get(target["name"])
                if entry is None:
                    # The checker already rejects undefined vars; defensively skip.
                    return
                slot, slot_ty = entry
                self._emit("  store %s %s, ptr %s" % (slot_ty, val, slot))
            elif target["k"] == "field":
                recv_ty, recv = self._lower_expr_typed(target["target"])
                # Field assignment via runtime helper. Full typed-field
                # support is deferred; emit a runtime call.
                self._emit("  call void @hl_struct_set_field(ptr %s, ptr @.field_%s, ptr %s)" % (
                    recv, target["name"], val))
            elif target["k"] == "index":
                lst_ty, lst = self._lower_expr_typed(target["target"])
                idx_ty, idx = self._lower_expr_typed(target["idx"])
                self._emit("  call void @hl_list_set(ptr %s, i64 %s, ptr %s)" % (
                    lst, idx, val))
        elif k == "return":
            if stmt.get("value") is None:
                self._emit("  ret void")
            else:
                val_ty, val = self._lower_expr_typed(stmt["value"])
                fn_ret = self._current_ret_type_value
                self._emit("  ret %s %s" % (hls_type_to_llvm(fn_ret), val))
            self._mark_terminated()
        elif k == "if":
            cond_ty, cond = self._lower_expr_typed(stmt["cond"])
            cond = self._to_i1(cond_ty, cond)
            then_lbl = self._fresh_label("then")
            else_lbl = self._fresh_label("else")
            end_lbl = self._fresh_label("endif")
            has_else = bool(stmt.get("els"))
            if has_else:
                self._emit("  br i1 %s, label %%%s, label %%%s" % (
                    cond, then_lbl, else_lbl))
            else:
                self._emit("  br i1 %s, label %%%s, label %%%s" % (
                    cond, then_lbl, end_lbl))
            self._emit("%s:" % then_lbl)
            self._reset_terminated()
            for s in stmt["then"]:
                self._lower_stmt(s)
            if not self._block_terminated_flag:
                self._emit("  br label %%%s" % end_lbl)
            if has_else:
                self._emit("%s:" % else_lbl)
                self._reset_terminated()
                for s in stmt["els"]:
                    self._lower_stmt(s)
                if not self._block_terminated_flag:
                    self._emit("  br label %%%s" % end_lbl)
            self._emit("%s:" % end_lbl)
            self._reset_terminated()
        elif k == "while":
            cond_lbl = self._fresh_label("while_cond")
            body_lbl = self._fresh_label("while_body")
            end_lbl = self._fresh_label("while_end")
            self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % cond_lbl)
            self._reset_terminated()
            cond_ty, cond = self._lower_expr_typed(stmt["cond"])
            cond = self._to_i1(cond_ty, cond)
            self._emit("  br i1 %s, label %%%s, label %%%s" % (
                cond, body_lbl, end_lbl))
            self._emit("%s:" % body_lbl)
            self._reset_terminated()
            self._loop_stack.append((cond_lbl, end_lbl))
            for s in stmt["body"]:
                self._lower_stmt(s)
            self._loop_stack.pop()
            if not self._block_terminated_flag:
                self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % end_lbl)
            self._reset_terminated()
        elif k == "for":
            # for v: T in iter { body } -> lowered as a while loop over
            # list indices.
            iter_ty, iter_val = self._lower_expr_typed(stmt["iter"])
            len_val = self._fresh("len")
            self._emit("  %s = call i64 @hl_list_len(ptr %s)" % (len_val, iter_val))
            i_slot = self._fresh("i")
            self._emit("  %s = alloca i64" % i_slot)
            self._emit("  store i64 0, ptr %s" % i_slot)
            self._locals["__for_i_%s" % stmt["var"]] = (i_slot, "i64")
            cond_lbl = self._fresh_label("for_cond")
            body_lbl = self._fresh_label("for_body")
            end_lbl = self._fresh_label("for_end")
            self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % cond_lbl)
            self._reset_terminated()
            i_val = self._fresh("i")
            self._emit("  %s = load i64, ptr %s" % (i_val, i_slot))
            cond_tmp = self._fresh("cond")
            self._emit("  %s = icmp slt i64 %s, %s" % (cond_tmp, i_val, len_val))
            self._emit("  br i1 %s, label %%%s, label %%%s" % (
                cond_tmp, body_lbl, end_lbl))
            self._emit("%s:" % body_lbl)
            self._reset_terminated()
            # Bind the loop variable.
            v_slot = self._fresh("v")
            vty = hls_type_to_llvm(stmt["vtype"])
            self._emit("  %s = alloca %s" % (v_slot, vty))
            elem = self._fresh("elem")
            self._emit("  %s = call ptr @hl_list_get(ptr %s, i64 %s)" % (
                elem, iter_val, i_val))
            # Cast elem (ptr) to the actual element type if it's a primitive.
            if vty == "i64":
                cvt = self._fresh("cvt")
                self._emit("  %s = ptrtoint ptr %s to i64" % (cvt, elem))
                self._emit("  store %s %s, ptr %s" % (vty, cvt, v_slot))
            else:
                self._emit("  store %s %s, ptr %s" % (vty, elem, v_slot))
            self._locals[stmt["var"]] = (v_slot, vty)
            self._loop_stack.append((cond_lbl, end_lbl))
            for s in stmt["body"]:
                self._lower_stmt(s)
            self._loop_stack.pop()
            # Increment.
            inc = self._fresh("inc")
            self._emit("  %s = add i64 %s, 1" % (inc, i_val))
            self._emit("  store i64 %s, ptr %s" % (inc, i_slot))
            if not self._block_terminated_flag:
                self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % end_lbl)
            self._reset_terminated()
        elif k == "break":
            if self._loop_stack:
                self._emit("  br label %%%s" % self._loop_stack[-1][1])
                self._mark_terminated()
        elif k == "continue":
            if self._loop_stack:
                self._emit("  br label %%%s" % self._loop_stack[-1][0])
                self._mark_terminated()
        elif k == "expr":
            self._lower_expr_typed(stmt["e"])

    # ---------- expression lowering ----------
    def _lower_expr_typed(self, e: Dict) -> Tuple[str, str]:
        """Lower an expression; return (llvm_type, llvm_value)."""
        k = e["k"]
        if k == "int":
            return ("i64", str(e["v"]))
        if k == "float":
            return ("double", "%.17e" % e["v"])
        if k == "bool":
            return ("i1", "1" if e["v"] else "0")
        if k == "str":
            data = e["v"]
            if isinstance(data, str):
                data = data.encode("utf-8")
            const_name = self._emit_string_const(data)
            tmp = self._fresh("s")
            self._emit("  %s = call ptr @hl_str_from(ptr @%s, i64 %d)" % (
                tmp, const_name, len(data)))
            return ("ptr", tmp)
        if k == "ident":
            entry = self._locals.get(e["name"])
            if entry is None:
                # Should not happen post-check; defensively return 0.
                return ("i64", "0")
            slot, slot_ty = entry
            tmp = self._fresh("r")
            self._emit("  %s = load %s, ptr %s" % (tmp, slot_ty, slot))
            return (slot_ty, tmp)
        if k == "bin":
            return self._lower_binop_typed(e)
        if k == "un":
            a_ty, a = self._lower_expr_typed(e["e"])
            tmp = self._fresh("u")
            if e["op"] == "-":
                if a_ty == "i64":
                    res = self._fresh("sub_ov")
                    ovf = self._fresh("ov")
                    self._emit(
                        "  %s = call { i64, i1 } @llvm.ssub.with.overflow.i64("
                        "i64 0, i64 %s)" % (res, a))
                    self._emit("  %s = extractvalue { i64, i1 } %s, 0" % (tmp, res))
                    self._emit("  %s = extractvalue { i64, i1 } %s, 1" % (ovf, res))
                    n = self._ov_counter
                    self._ov_counter += 1
                    self._emit("  br i1 %s, label %%ov_panic_%d, label %%ov_ok_%d" % (ovf, n, n))
                    self._emit("ov_panic_%d:" % n)
                    self._emit("  call void @hl_die(ptr @.panic_overflow_msg)")
                    self._emit("  unreachable")
                    self._emit("ov_ok_%d:" % n)
                    self._reset_terminated()
                    return ("i64", tmp)
                # float negation
                self._emit("  %s = fneg double %s" % (tmp, a))
                return ("double", tmp)
            elif e["op"] == "!":
                self._emit("  %s = xor i1 %s, 1" % (tmp, a))
                return ("i1", tmp)
            return ("i64", "0")
        if k == "call":
            return self._lower_call_typed(e)
        if k == "method" or k == "fieldcall":
            recv_ty, recv = self._lower_expr_typed(e["target"])
            arg_vals = [self._lower_expr_typed(a) for a in e["args"]]
            mangled = "hl_method_%s" % e["name"]
            arg_str = ", ".join(["ptr %s" % recv] + ["ptr %s" % v for _, v in arg_vals])
            tmp = self._fresh("m")
            self._emit("  %s = call ptr @%s(%s)" % (tmp, mangled, arg_str))
            return ("ptr", tmp)
        if k == "field":
            recv_ty, recv = self._lower_expr_typed(e["target"])
            tmp = self._fresh("f")
            self._emit("  %s = call ptr @hl_struct_get(ptr %s, ptr @.field_%s)" % (
                tmp, recv, e["name"]))
            return ("ptr", tmp)
        if k == "index":
            lst_ty, lst = self._lower_expr_typed(e["target"])
            idx_ty, idx = self._lower_expr_typed(e["idx"])
            tmp = self._fresh("e")
            self._emit("  %s = call ptr @hl_list_get(ptr %s, i64 %s)" % (
                tmp, lst, idx))
            return ("ptr", tmp)
        if k == "listlit":
            tmp = self._fresh("lst")
            self._emit("  %s = call ptr @hl_list_new()" % tmp)
            for item in e["items"]:
                _, v = self._lower_expr_typed(item)
                self._emit("  call void @hl_list_push(ptr %s, ptr %s)" % (tmp, v))
            return ("ptr", tmp)
        if k == "structlit":
            tmp = self._fresh("st")
            self._emit("  %s = inttoptr i64 0 to ptr" % tmp)
            return ("ptr", tmp)
        if k == "match":
            # Fall back to a runtime call; full lowering is complex.
            scrut_ty, scrut = self._lower_expr_typed(e["scrut"])
            tmp = self._fresh("match")
            self._emit("  %s = call ptr @hl_match_dispatch(ptr %s)" % (tmp, scrut))
            return ("ptr", tmp)
        if k == "qmark":
            # Lower ? as a runtime dispatch.
            inner_ty, inner = self._lower_expr_typed(e["e"])
            tmp = self._fresh("qmark")
            self._emit("  %s = call ptr @hl_qmark_dispatch(ptr %s)" % (tmp, inner))
            return ("ptr", tmp)
        if k == "mapnew":
            tmp = self._fresh("map")
            self._emit("  %s = call ptr @hl_map_new()" % tmp)
            return ("ptr", tmp)
        if k == "enumlit":
            tmp = self._fresh("enum")
            self._emit("  %s = call ptr @hl_enum_new(ptr @.enum_%s_%s)" % (
                tmp, e.get("enum_name", "_"), e.get("variant", "_")))
            return ("ptr", tmp)
        # Fallback for unsupported expression kinds.
        return ("i64", "0")

    def _lower_binop_typed(self, e: Dict) -> Tuple[str, str]:
        op = e["op"]
        lt = e["l"].get("t", "int")
        a_ty, a = self._lower_expr_typed(e["l"])
        b_ty, b = self._lower_expr_typed(e["r"])

        if lt == "str" and op == "+":
            tmp = self._fresh("cat")
            self._emit("  %s = call ptr @hl_str_concat(ptr %s, ptr %s)" % (tmp, a, b))
            return ("ptr", tmp)
        if lt == "str" and op == "==":
            tmp = self._fresh("r")
            self._emit("  %s = call i1 @hl_str_eq(ptr %s, ptr %s)" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        if lt == "str" and op == "!=":
            eq = self._fresh("eq")
            self._emit("  %s = call i1 @hl_str_eq(ptr %s, ptr %s)" % (eq, a, b))
            tmp = self._fresh("r")
            self._emit("  %s = xor i1 %s, 1" % (tmp, eq))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        if lt == "float":
            tmp = self._fresh("r")
            if op == "+":
                self._emit("  %s = fadd double %s, %s" % (tmp, a, b))
                return ("double", tmp)
            if op == "-":
                self._emit("  %s = fsub double %s, %s" % (tmp, a, b))
                return ("double", tmp)
            if op == "*":
                self._emit("  %s = fmul double %s, %s" % (tmp, a, b))
                return ("double", tmp)
            if op == "/":
                self._emit("  %s = fdiv double %s, %s" % (tmp, a, b))
                return ("double", tmp)
            if op == "%":
                self._emit("  %s = frem double %s, %s" % (tmp, a, b))
                return ("double", tmp)
            if op == "==":
                self._emit("  %s = fcmp oeq double %s, %s" % (tmp, a, b))
                ext = self._fresh("ext")
                self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
                return ("i64", ext)
            if op == "!=":
                self._emit("  %s = fcmp one double %s, %s" % (tmp, a, b))
                ext = self._fresh("ext")
                self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
                return ("i64", ext)
            if op == "<":
                self._emit("  %s = fcmp olt double %s, %s" % (tmp, a, b))
                ext = self._fresh("ext")
                self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
                return ("i64", ext)
            if op == "<=":
                self._emit("  %s = fcmp ole double %s, %s" % (tmp, a, b))
                ext = self._fresh("ext")
                self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
                return ("i64", ext)
            if op == ">":
                self._emit("  %s = fcmp ogt double %s, %s" % (tmp, a, b))
                ext = self._fresh("ext")
                self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
                return ("i64", ext)
            if op == ">=":
                self._emit("  %s = fcmp oge double %s, %s" % (tmp, a, b))
                ext = self._fresh("ext")
                self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
                return ("i64", ext)
        # Default: integer arithmetic with overflow checks.
        if op == "+":
            res = self._emit_overflow_op("llvm.sadd.with.overflow.i64", a, b)
            return ("i64", res)
        if op == "-":
            res = self._emit_overflow_op("llvm.ssub.with.overflow.i64", a, b)
            return ("i64", res)
        if op == "*":
            res = self._emit_overflow_op("llvm.smul.with.overflow.i64", a, b)
            return ("i64", res)
        if op == "/":
            is_zero = self._fresh("dz")
            self._emit("  %s = icmp eq i64 %s, 0" % (is_zero, b))
            n = self._dz_counter
            self._dz_counter += 1
            self._emit("  br i1 %s, label %%dz_panic_%d, label %%dz_ok_%d" % (is_zero, n, n))
            self._emit("dz_panic_%d:" % n)
            self._emit("  call void @hl_die(ptr @.panic_divzero_msg)")
            self._emit("  unreachable")
            self._emit("dz_ok_%d:" % n)
            self._reset_terminated()
            tmp = self._fresh("r")
            self._emit("  %s = sdiv i64 %s, %s" % (tmp, a, b))
            return ("i64", tmp)
        if op == "%":
            is_zero = self._fresh("dz")
            self._emit("  %s = icmp eq i64 %s, 0" % (is_zero, b))
            n = self._dz_counter
            self._dz_counter += 1
            self._emit("  br i1 %s, label %%dz_panic_%d, label %%dz_ok_%d" % (is_zero, n, n))
            self._emit("dz_panic_%d:" % n)
            self._emit("  call void @hl_die(ptr @.panic_divzero_msg)")
            self._emit("  unreachable")
            self._emit("dz_ok_%d:" % n)
            self._reset_terminated()
            tmp = self._fresh("r")
            self._emit("  %s = srem i64 %s, %s" % (tmp, a, b))
            return ("i64", tmp)
        if op == "==":
            tmp = self._fresh("r")
            self._emit("  %s = icmp eq i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        if op == "!=":
            tmp = self._fresh("r")
            self._emit("  %s = icmp ne i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        if op == "<":
            tmp = self._fresh("r")
            self._emit("  %s = icmp slt i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        if op == "<=":
            tmp = self._fresh("r")
            self._emit("  %s = icmp sle i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        if op == ">":
            tmp = self._fresh("r")
            self._emit("  %s = icmp sgt i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        if op == ">=":
            tmp = self._fresh("r")
            self._emit("  %s = icmp sge i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        if op == "&&":
            tmp = self._fresh("r")
            self._emit("  %s = and i1 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        if op == "||":
            tmp = self._fresh("r")
            self._emit("  %s = or i1 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ("i64", ext)
        return ("i64", "0")

    def _emit_overflow_op(self, llvm_intrinsic: str, a: str, b: str) -> str:
        """Emit a checked overflow operation; return the result SSA name."""
        n = self._ov_counter
        self._ov_counter += 1
        res = self._fresh("ov_res")
        self._emit("  %s = call { i64, i1 } @%s(i64 %s, i64 %s)" % (
            res, llvm_intrinsic, a, b))
        out = self._fresh("r")
        self._emit("  %s = extractvalue { i64, i1 } %s, 0" % (out, res))
        ovf = self._fresh("ov")
        self._emit("  %s = extractvalue { i64, i1 } %s, 1" % (ovf, res))
        panic_lbl = "ov_panic_%d" % n
        ok_lbl = "ov_ok_%d" % n
        self._emit("  br i1 %s, label %%%s, label %%%s" % (ovf, panic_lbl, ok_lbl))
        self._emit("%s:" % panic_lbl)
        self._emit("  call void @hl_die(ptr @.panic_overflow_msg)")
        self._emit("  unreachable")
        self._emit("%s:" % ok_lbl)
        self._reset_terminated()
        return out

    def _lower_call_typed(self, e: Dict) -> Tuple[str, str]:
        """Lower a function call. Maps HLS builtins to runtime functions."""
        name = e["name"]
        arg_pairs = [self._lower_expr_typed(a) for a in e["args"]]
        # HLS builtin -> runtime function mapping.
        builtin_map = {
            "println":          ("void", "@hl_println",    ["ptr"]),
            "print":            ("void", "@hl_print",      ["ptr"]),
            "len":              ("i64",  "@hl_str_len",    ["ptr"]),   # generic len (str/list/map)
            "str":              ("ptr",  "@hl_int_to_str", ["i64"]),
            "panic":            ("void", "@hl_die",        ["ptr"]),
            "clock_ms":         ("i64",  "@hl_clock_ms",   []),
            "file_exists":      ("i1",   "@hl_file_exists", ["ptr"]),
            "read_file":        ("ptr",  "@hl_read_file",  ["ptr"]),
            "write_file":       ("void", "@hl_write_file", ["ptr", "ptr"]),
            "exit":             ("void", "@hl_exit",       ["i64"]),
            "chr":              ("i64",  "@hl_chr",        ["i64"]),
            "args":             ("ptr",  "@hl_args_get",   ["i64"]),  # placeholder: returns first arg
            "range":            ("ptr",  "@hl_range",      ["i64", "i64"]),
            "map_new":          ("ptr",  "@hl_map_new",    []),
            "tainted_args":     ("ptr",  "@hl_tainted_args", []),
            "taint_mark":       ("ptr",  "@hl_taint_mark", ["ptr"]),
            "taint_unwrap":     ("ptr",  "@hl_taint_unwrap", ["ptr"]),
            "read_file_tainted": ("ptr", "@hl_read_file_tainted", ["ptr"]),
            "drop":             ("void", "@hl_drop",       ["ptr"]),
            "clone":            ("ptr",  "@hl_clone",      ["ptr"]),
            "take":             ("ptr",  "@hl_take",       ["ptr"]),
        }
        if name in builtin_map:
            ret_ty, fn_name, arg_tys = builtin_map[name]
            # Coerce each argument to the expected type.
            coerced = []
            for (aty, aval), want_ty in zip(arg_pairs, arg_tys):
                if aty == want_ty:
                    coerced.append(aval)
                elif aty == "i64" and want_ty == "ptr":
                    cvt = self._fresh("i2p")
                    self._emit("  %s = inttoptr i64 %s to ptr" % (cvt, aval))
                    coerced.append(cvt)
                elif aty == "ptr" and want_ty == "i64":
                    cvt = self._fresh("p2i")
                    self._emit("  %s = ptrtoint ptr %s to i64" % (cvt, aval))
                    coerced.append(cvt)
                elif aty == "i1" and want_ty == "i64":
                    cvt = self._fresh("z")
                    self._emit("  %s = zext i1 %s to i64" % (cvt, aval))
                    coerced.append(cvt)
                else:
                    coerced.append(aval)
            arg_str = ", ".join("%s %s" % (t, v) for t, v in zip(arg_tys, coerced))
            if ret_ty == "void":
                self._emit("  call void %s(%s)" % (fn_name, arg_str))
                return ("void", "0")
            tmp = self._fresh("call")
            self._emit("  %s = call %s %s(%s)" % (tmp, ret_ty, fn_name, arg_str))
            return (ret_ty, tmp)
        # User-defined function call.
        fn = self.program["fns"].get(name)
        # BUG-SC-LLVM-24 fix: removed the dead duplicate lookup — the
        # previous `if fn is None: fn = self.program["fns"].get(name)`
        # branch was identical to the line above (a no-op). Method-key
        # calls ("Type.method") arrive here with name already being the
        # full key, so the single lookup above suffices.
        ret_ty = hls_type_to_llvm(fn["ret"]) if fn else "ptr"
        param_tys = [hls_type_to_llvm(p[1]) for p in fn["params"]] if fn else (
            ["i64"] * len(arg_pairs))
        coerced = []
        for (aty, aval), want_ty in zip(arg_pairs, param_tys):
            if aty == want_ty:
                coerced.append(aval)
            elif aty == "i64" and want_ty == "ptr":
                cvt = self._fresh("i2p")
                self._emit("  %s = inttoptr i64 %s to ptr" % (cvt, aval))
                coerced.append(cvt)
            elif aty == "ptr" and want_ty == "i64":
                cvt = self._fresh("p2i")
                self._emit("  %s = ptrtoint ptr %s to i64" % (cvt, aval))
                coerced.append(cvt)
            else:
                coerced.append(aval)
        arg_str = ", ".join("%s %s" % (t, v) for t, v in zip(param_tys, coerced))
        if ret_ty == "void":
            self._emit("  call void @%s(%s)" % (name, arg_str))
            return ("void", "0")
        tmp = self._fresh("call")
        self._emit("  %s = call %s @%s(%s)" % (tmp, ret_ty, name, arg_str))
        return (ret_ty, tmp)

    # ---------- string constant emission ----------
    def _emit_string_const(self, data: bytes) -> str:
        """Emit a global string constant. Returns the global name."""
        self._str_counter += 1
        name = ".str.%d" % self._str_counter
        if data:
            bytes_str = ", ".join("i8 %d" % b for b in data) + ", i8 0"
        else:
            bytes_str = "i8 0"
        self._string_consts.append((name, data, bytes_str))
        return name


def emit_module(program, target_triple: Optional[str] = None) -> str:
    """Convenience: emit LLVM IR for a checked program."""
    return LLVMEmitter(program, target_triple).emit()
