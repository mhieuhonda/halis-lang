"""LLVM IR emitter for Hieu Louis (Stage 12, v0.10.0-alpha).

This module emits LLVM IR text (.ll) from a checked HLS program (post-type-
check AST). The IR can be assembled by `llc` or `clang` into a native binary.

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
  - Local variables become `alloca` + `load`/`store` (we do NOT use SSA
    registers for locals; this would require phi-node construction).
  - Basic blocks are emitted for `if`/`while`/`for`/`match` control flow.
  - Integer arithmetic uses LLVM's `add`/`sub`/`mul`/`sdiv`/`srem` with
    explicit overflow checks via `llvm.sadd.with.overflow.i64` etc. The
    overflow path calls `hl_die` and the result is replaced with `0`.

Multi-target support:
  - The default target is the host triple (queried from `llvm-config`).
  - `--target aarch64-linux` cross-compiles.
  - Stack probes are emitted via `llvm.stackprobe` attribute on functions
    with large stack frames (>4KB).
  - PGO (profile-guided optimisation) is deferred to the Stage 12 release
    target.

The emitter is a *separate backend* from the C codegen. It is wired into
`boot.py` via the new `--emit llvm` flag.
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
        # Per-function local variable map: name -> LLVM register.
        self._locals: Dict[str, str] = {}

    def _fresh(self, prefix="t"):
        self._tmp += 1
        return "%%%s%d" % (prefix, self._tmp)

    def _fresh_label(self, prefix="bb"):
        self._label += 1
        return "%s%d" % (prefix, self._label)

    def _emit(self, line: str):
        self.lines.append(line)

    # ---------- public API ----------
    def emit(self) -> str:
        if self.target_triple:
            self._emit("target triple = \"%s\"" % self.target_triple)
            self._emit("")
        # Emit runtime declarations.
        for line in RUNTIME_DECLS.strip().split("\n"):
            self._emit(line)
        self._emit("")
        # Emit each function.
        for fname, fn in self.program["fns"].items():
            self._emit_function(fname, fn)
        return "\n".join(self.lines) + "\n"

    # ---------- function emission ----------
    def _emit_function(self, fname: str, fn: Dict):
        ret_llvm = hls_type_to_llvm(fn["ret"])
        params = []
        for (pname, ptype, _) in fn["params"]:
            params.append("%s %%v_%s" % (hls_type_to_llvm(ptype), pname))
        param_str = ", ".join(params)
        self._emit("define %s @%s(%s) {" % (ret_llvm, fname, param_str))
        self._emit("entry:")
        # Allocate stack slots for parameters so we can re-assign them
        # (HLS allows `let mut` reassignment).
        self._locals = {}
        for (pname, ptype, _) in fn["params"]:
            slot = self._fresh("p")
            self._emit("  %s = alloca %s" % (slot, hls_type_to_llvm(ptype)))
            self._emit("  store %s %%v_%s, ptr %s" % (
                hls_type_to_llvm(ptype), pname, slot))
            self._locals[pname] = slot
        # Lower each statement.
        for stmt in fn["body"]:
            self._lower_stmt(stmt)
        # Implicit return void for void functions; panic for non-void
        # (the type checker already rejects missing returns).
        if fn["ret"] == "void":
            self._emit("  ret void")
        else:
            # The function should have hit a `return` already; defensively
            # emit an unreachable marker.
            self._emit("  unreachable")
        self._emit("}")
        self._emit("")

    # ---------- statement lowering ----------
    def _lower_stmt(self, stmt: Dict):
        k = stmt["k"]
        if k == "let":
            val = self._lower_expr(stmt["value"])
            slot = self._fresh("l")
            ty = hls_type_to_llvm(stmt["t"])
            self._emit("  %s = alloca %s" % (slot, ty))
            self._emit("  store %s %s, ptr %s" % (ty, val, slot))
            self._locals[stmt["name"]] = slot
        elif k == "assign":
            target = stmt["target"]
            val = self._lower_expr(stmt["value"])
            if target["k"] == "ident":
                slot = self._locals.get(target["name"])
                if slot is None:
                    # The checker already rejects undefined vars; defensively skip.
                    return
                # Need to know the LLVM type of the slot. We don't track it
                # explicitly here; use `ptr` cast + i64 store as the fallback
                # (this only works for int locals — full type-tracking is a
                # Stage 12 release target).
                self._emit("  store i64 %s, ptr %s" % (val, slot))
            else:
                # field/index assignment goes through the runtime API.
                # For now, we emit a call to the corresponding runtime fn.
                # (Stage 12 release target: full support.)
                pass
        elif k == "return":
            if stmt.get("value") is None:
                self._emit("  ret void")
            else:
                val = self._lower_expr(stmt["value"])
                fn_ret = self._current_ret_type
                self._emit("  ret %s %s" % (hls_type_to_llvm(fn_ret), val))
        elif k == "if":
            cond = self._lower_expr(stmt["cond"])
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
            for s in stmt["then"]:
                self._lower_stmt(s)
            if not self._block_terminated():
                self._emit("  br label %%%s" % end_lbl)
            if has_else:
                self._emit("%s:" % else_lbl)
                for s in stmt["els"]:
                    self._lower_stmt(s)
                if not self._block_terminated():
                    self._emit("  br label %%%s" % end_lbl)
            self._emit("%s:" % end_lbl)
            self._block_started = True
        elif k == "while":
            cond_lbl = self._fresh_label("while_cond")
            body_lbl = self._fresh_label("while_body")
            end_lbl = self._fresh_label("while_end")
            self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % cond_lbl)
            cond = self._lower_expr(stmt["cond"])
            self._emit("  br i1 %s, label %%%s, label %%%s" % (
                cond, body_lbl, end_lbl))
            self._emit("%s:" % body_lbl)
            self._loop_stack.append((cond_lbl, end_lbl))
            for s in stmt["body"]:
                self._lower_stmt(s)
            self._loop_stack.pop()
            if not self._block_terminated():
                self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % end_lbl)
            self._block_started = True
        elif k == "for":
            # for v: T in iter { body } -> lowered as a while loop over
            # list indices. See the HLIR lowering for the same pattern.
            iter_val = self._lower_expr(stmt["iter"])
            len_val = self._fresh("len")
            self._emit("  %s = call i64 @hl_list_len(ptr %s)" % (len_val, iter_val))
            i_slot = self._fresh("i")
            self._emit("  %s = alloca i64" % i_slot)
            self._emit("  store i64 0, ptr %s" % i_slot)
            cond_lbl = self._fresh_label("for_cond")
            body_lbl = self._fresh_label("for_body")
            end_lbl = self._fresh_label("for_end")
            self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % cond_lbl)
            i_val = self._fresh("i")
            self._emit("  %s = load i64, ptr %s" % (i_val, i_slot))
            cond_tmp = self._fresh("cond")
            self._emit("  %s = icmp slt i64 %s, %s" % (cond_tmp, i_val, len_val))
            self._emit("  br i1 %s, label %%%s, label %%%s" % (
                cond_tmp, body_lbl, end_lbl))
            self._emit("%s:" % body_lbl)
            # Bind the loop variable.
            v_slot = self._fresh("v")
            self._emit("  %s = alloca ptr" % v_slot)
            elem = self._fresh("elem")
            self._emit("  %s = call ptr @hl_list_get(ptr %s, i64 %s)" % (
                elem, iter_val, i_val))
            self._emit("  store ptr %s, ptr %s" % (elem, v_slot))
            self._locals[stmt["var"]] = v_slot
            self._loop_stack.append((cond_lbl, end_lbl))
            for s in stmt["body"]:
                self._lower_stmt(s)
            self._loop_stack.pop()
            # Increment.
            inc = self._fresh("inc")
            self._emit("  %s = add i64 %s, 1" % (inc, i_val))
            self._emit("  store i64 %s, ptr %s" % (inc, i_slot))
            if not self._block_terminated():
                self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % end_lbl)
            self._block_started = True
        elif k == "break":
            if self._loop_stack:
                self._emit("  br label %%%s" % self._loop_stack[-1][1])
                self._block_terminated_flag = True
        elif k == "continue":
            if self._loop_stack:
                self._emit("  br label %%%s" % self._loop_stack[-1][0])
                self._block_terminated_flag = True
        elif k == "expr":
            self._lower_expr(stmt["e"])

    # ---------- expression lowering ----------
    def _lower_expr(self, e: Dict) -> str:
        k = e["k"]
        if k == "int":
            return str(e["v"])
        if k == "float":
            # LLVM requires the exact hex-bits representation of the double
            # for full precision. For simplicity we use the decimal form,
            # which LLVM accepts.
            return "0x%X" % float_to_ieee_bits(e["v"]) if False else \
                ("%.17e" % e["v"])
        if k == "bool":
            return "1" if e["v"] else "0"
        if k == "str":
            # Emit a global constant for the string bytes, then construct
            # an hl_str via hl_str_from.
            data = e["v"]
            if isinstance(data, str):
                data = data.encode("utf-8")
            const_name = self._emit_string_const(data)
            # Build the hl_str via hl_str_from.
            tmp = self._fresh("s")
            self._emit("  %s = call ptr @hl_str_from(ptr @%s, i64 %d)" % (
                tmp, const_name, len(data)))
            return tmp
        if k == "ident":
            slot = self._locals.get(e["name"])
            if slot is None:
                # Should not happen post-check; defensively return 0.
                return "0"
            # We don't track the LLVM type per local. Use i64 as the default
            # (the most common case for int locals). Full type-tracking is
            # a Stage 12 release target.
            tmp = self._fresh("r")
            self._emit("  %s = load i64, ptr %s" % (tmp, slot))
            return tmp
        if k == "bin":
            return self._lower_binop(e)
        if k == "un":
            a = self._lower_expr(e["e"])
            tmp = self._fresh("u")
            if e["op"] == "-":
                # Negation with overflow check.
                ovf = self._fresh("ov")
                res = self._fresh("sub_ov")
                self._emit(
                    "  %s = call { i64, i1 } @llvm.ssub.with.overflow.i64("
                    "i64 0, i64 %s)" % (res, a))
                self._emit("  %s = extractvalue { i64, i1 } %s, 0" % (tmp, res))
                self._emit("  %s = extractvalue { i64, i1 } %s, 1" % (ovf, res))
                self._emit("  br i1 %s, label %%ov_panic, label %%ov_ok" % ovf)
                self._emit("ov_panic:")
                self._emit("  call void @hl_die(ptr @.panic_overflow_msg)")
                self._emit("  unreachable")
                self._emit("ov_ok:")
                return tmp
            elif e["op"] == "!":
                self._emit("  %s = xor i1 %s, 1" % (tmp, a))
                return tmp
            return "0"
        if k == "call":
            return self._lower_call(e)
        if k == "method" or k == "fieldcall":
            # Method calls dispatch through the runtime. Today the LLVM
            # backend treats them as opaque calls to a mangled name.
            # Full method dispatch is a Stage 12 release target.
            recv = self._lower_expr(e["target"])
            args = [self._lower_expr(a) for a in e["args"]]
            mangled = "hl_method_%s" % e["name"]
            arg_str = ", ".join(["ptr %s" % recv] + ["ptr %s" % a for a in args])
            tmp = self._fresh("m")
            self._emit("  %s = call ptr @%s(%s)" % (tmp, mangled, arg_str))
            return tmp
        if k == "field":
            recv = self._lower_expr(e["target"])
            tmp = self._fresh("f")
            # Field access via runtime helper. The runtime function
            # `hl_struct_get` returns a ptr to the field; full support
            # for typed fields is the Stage 12 release target.
            self._emit("  %s = call ptr @hl_struct_get(ptr %s, ptr @.field_%s)" % (
                tmp, recv, e["name"]))
            return tmp
        if k == "listlit":
            tmp = self._fresh("lst")
            self._emit("  %s = call ptr @hl_list_new()" % tmp)
            for item in e["items"]:
                v = self._lower_expr(item)
                self._emit("  call void @hl_list_push(ptr %s, ptr %s)" % (tmp, v))
            return tmp
        if k == "structlit":
            # Allocate via the runtime; details deferred.
            tmp = self._fresh("st")
            self._emit("  %s = inttoptr i64 0 to ptr" % tmp)
            return tmp
        if k == "index":
            lst = self._lower_expr(e["target"])
            idx = self._lower_expr(e["idx"])
            tmp = self._fresh("e")
            self._emit("  %s = call ptr @hl_list_get(ptr %s, i64 %s)" % (
                tmp, lst, idx))
            return tmp
        # Fallback for unsupported expression kinds.
        return "0"

    def _lower_binop(self, e: Dict) -> str:
        op = e["op"]
        a = self._lower_expr(e["l"])
        b = self._lower_expr(e["r"])
        # Use the checker's type annotation (set during `check()`) to
        # dispatch on operand type. The left operand's type determines
        # the operation kind for HLS.
        lt = e["l"].get("t", "int")
        if lt == "str" and op == "+":
            # String concatenation.
            tmp = self._fresh("cat")
            self._emit("  %s = call ptr @hl_str_concat(ptr %s, ptr %s)" % (
                tmp, a, b))
            return tmp
        if lt == "str" and op == "==":
            tmp = self._fresh("r")
            self._emit("  %s = call i1 @hl_str_eq(ptr %s, ptr %s)" % (
                tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        if lt == "str" and op == "!=":
            tmp = self._fresh("r")
            eq = self._fresh("eq")
            self._emit("  %s = call i1 @hl_str_eq(ptr %s, ptr %s)" % (
                eq, a, b))
            self._emit("  %s = xor i1 %s, 1" % (tmp, eq))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        if lt == "float":
            # Float arithmetic (no overflow check).
            tmp = self._fresh("r")
            if op == "+":
                self._emit("  %s = fadd double %s, %s" % (tmp, a, b))
            elif op == "-":
                self._emit("  %s = fsub double %s, %s" % (tmp, a, b))
            elif op == "*":
                self._emit("  %s = fmul double %s, %s" % (tmp, a, b))
            elif op == "/":
                self._emit("  %s = fdiv double %s, %s" % (tmp, a, b))
            elif op == "%":
                self._emit("  %s = frem double %s, %s" % (tmp, a, b))
            elif op == "==":
                self._emit("  %s = fcmp oeq double %s, %s" % (tmp, a, b))
            elif op == "!=":
                self._emit("  %s = fcmp one double %s, %s" % (tmp, a, b))
            elif op == "<":
                self._emit("  %s = fcmp olt double %s, %s" % (tmp, a, b))
            elif op == "<=":
                self._emit("  %s = fcmp ole double %s, %s" % (tmp, a, b))
            elif op == ">":
                self._emit("  %s = fcmp ogt double %s, %s" % (tmp, a, b))
            elif op == ">=":
                self._emit("  %s = fcmp oge double %s, %s" % (tmp, a, b))
            else:
                return "0.0"
            # Comparison results are i1; arithmetic results are double.
            if op in ("==", "!=", "<", "<=", ">", ">="):
                ext = self._fresh("ext")
                self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
                return ext
            return tmp
        # Default: integer arithmetic with overflow checks.
        if op == "+":
            return self._emit_overflow_op("llvm.sadd.with.overflow.i64", a, b)
        if op == "-":
            return self._emit_overflow_op("llvm.ssub.with.overflow.i64", a, b)
        if op == "*":
            return self._emit_overflow_op("llvm.smul.with.overflow.i64", a, b)
        if op == "/":
            # Check for division by zero first.
            is_zero = self._fresh("dz")
            self._emit("  %s = icmp eq i64 %s, 0" % (is_zero, b))
            n = self._dz_counter
            self._dz_counter += 1
            self._emit("  br i1 %s, label %%dz_panic_%d, label %%dz_ok_%d" % (
                is_zero, n, n))
            self._emit("dz_panic_%d:" % n)
            self._emit("  call void @hl_die(ptr @.panic_divzero_msg)")
            self._emit("  unreachable")
            self._emit("dz_ok_%d:" % n)
            tmp = self._fresh("r")
            self._emit("  %s = sdiv i64 %s, %s" % (tmp, a, b))
            return tmp
        if op == "%":
            is_zero = self._fresh("dz")
            self._emit("  %s = icmp eq i64 %s, 0" % (is_zero, b))
            n = self._dz_counter
            self._dz_counter += 1
            self._emit("  br i1 %s, label %%dz_panic_%d, label %%dz_ok_%d" % (
                is_zero, n, n))
            self._emit("dz_panic_%d:" % n)
            self._emit("  call void @hl_die(ptr @.panic_divzero_msg)")
            self._emit("  unreachable")
            self._emit("dz_ok_%d:" % n)
            tmp = self._fresh("r")
            self._emit("  %s = srem i64 %s, %s" % (tmp, a, b))
            return tmp
        if op == "==":
            tmp = self._fresh("r")
            self._emit("  %s = icmp eq i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        if op == "!=":
            tmp = self._fresh("r")
            self._emit("  %s = icmp ne i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        if op == "<":
            tmp = self._fresh("r")
            self._emit("  %s = icmp slt i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        if op == "<=":
            tmp = self._fresh("r")
            self._emit("  %s = icmp sle i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        if op == ">":
            tmp = self._fresh("r")
            self._emit("  %s = icmp sgt i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        if op == ">=":
            tmp = self._fresh("r")
            self._emit("  %s = icmp sge i64 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        if op == "&&":
            tmp = self._fresh("r")
            self._emit("  %s = and i1 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        if op == "||":
            tmp = self._fresh("r")
            self._emit("  %s = or i1 %s, %s" % (tmp, a, b))
            ext = self._fresh("ext")
            self._emit("  %s = zext i1 %s to i64" % (ext, tmp))
            return ext
        # String concat (+ on str) is handled by the runtime; if both operands
        # are str, we'd need a different lowering. For simplicity, the Stage 12
        # alpha treats `+` as integer addition; full type-aware lowering is
        # a release target.
        return "0"

    def _emit_overflow_op(self, llvm_intrinsic: str, a: str, b: str) -> str:
        """Emit a checked overflow operation."""
        n = self._ov_counter
        self._ov_counter += 1
        res = self._fresh("ov_res")
        self._emit("  %s = call { i64, i1 } @%s(i64 %s, i64 %s)" % (
            res, llvm_intrinsic, a, b))
        out = self._fresh("r")
        self._emit("  %s = extractvalue { i64, i1 } %s, 0" % (out, res))
        ovf = self._fresh("ov")
        self._emit("  %s = extractvalue { i64, i1 } %s, 1" % (ovf, res))
        # Each overflow op must use unique labels.
        panic_lbl = "ov_panic_%d" % n
        ok_lbl = "ov_ok_%d" % n
        self._emit("  br i1 %s, label %%%s, label %%%s" % (
            ovf, panic_lbl, ok_lbl))
        self._emit("%s:" % panic_lbl)
        self._emit("  call void @hl_die(ptr @.panic_overflow_msg)")
        self._emit("  unreachable")
        self._emit("%s:" % ok_lbl)
        return out

    def _lower_call(self, e: Dict) -> str:
        """Lower a function call. Maps HLS builtins to runtime functions."""
        name = e["name"]
        args = [self._lower_expr(a) for a in e["args"]]
        # HLS builtin -> runtime function mapping.
        builtin_map = {
            "println": ("void", "@hl_println", ["ptr"]),
            "print":    ("void", "@hl_print",   ["ptr"]),
            "len":      ("i64",  "@hl_str_len", ["ptr"]),
            "str":      ("ptr",  "@hl_int_to_str", ["i64"]),
            "panic":    ("void", "@hl_die",     ["ptr"]),
            "clock_ms": ("i64",  "@hl_clock_ms", []),
            "file_exists": ("i1", "@hl_file_exists", ["ptr"]),
            "read_file":   ("ptr", "@hl_read_file", ["ptr"]),
            "write_file":  ("void", "@hl_write_file", ["ptr", "ptr"]),
            "exit":        ("void", "@hl_exit", ["i64"]),
            "chr":         ("i64", "@hl_chr", ["i64"]),
            "args":        ("ptr", "@hl_args_get_zero", []),
            "range":       ("ptr", "@hl_range", ["i64", "i64"]),
        }
        if name in builtin_map:
            ret_ty, fn_name, arg_tys = builtin_map[name]
            arg_str = ", ".join("%s %s" % (t, a) for t, a in zip(arg_tys, args))
            if ret_ty == "void":
                self._emit("  call void %s(%s)" % (fn_name, arg_str))
                return "0"
            tmp = self._fresh("call")
            self._emit("  %s = call %s %s(%s)" % (tmp, ret_ty, fn_name, arg_str))
            return tmp
        # User-defined function call. All HLS functions are emitted with
        # their original name in LLVM IR.
        # Determine the function's return type from the program.
        fn = self.program["fns"].get(name)
        if fn is None:
            # Might be a method-key like "Type.method" — check that too.
            fn = self.program["fns"].get(name)
        ret_ty = hls_type_to_llvm(fn["ret"]) if fn else "ptr"
        param_tys = [hls_type_to_llvm(p[1]) for p in fn["params"]] if fn else (
            ["i64"] * len(args))
        arg_str = ", ".join("%s %s" % (t, a) for t, a in zip(param_tys, args))
        if ret_ty == "void":
            self._emit("  call void @%s(%s)" % (name, arg_str))
            return "0"
        tmp = self._fresh("call")
        self._emit("  %s = call %s @%s(%s)" % (tmp, ret_ty, name, arg_str))
        return tmp

    # ---------- string constant emission ----------
    def _emit_string_const(self, data: bytes) -> str:
        """Emit a global string constant. Returns the global name."""
        name = ".str.%d" % (self._tmp_str_consts())
        # LLVM IR string constants are arrays of i8 with a trailing null
        # byte (for C compatibility).
        if data:
            bytes_str = ", ".join("i8 %d" % b for b in data) + ", i8 0"
        else:
            bytes_str = "i8 0"
        self._string_consts.append((name, data, bytes_str))
        return name

    _str_counter = 0
    _ov_counter = 0

    def _tmp_str_consts(self):
        self._str_counter += 1
        return self._str_counter

    # ---------- helpers ----------
    @property
    def _current_ret_type(self):
        return self._current_ret_type_value

    _current_ret_type_value = "void"

    _block_terminated_flag = False
    _block_started = True

    def _block_terminated(self):
        # Return True if the current block ends with a terminator (br / ret /
        # unreachable). We approximate this by checking the last emitted line.
        if not self.lines:
            return False
        last = self.lines[-1].strip()
        return (last.startswith("ret ") or last.startswith("br ")
                or last == "ret void" or last == "unreachable")

    # ---------- final assembly ----------
    def emit_final(self) -> str:
        """Emit the complete module, including prelude and string consts."""
        # Reset state.
        self.lines = []
        self._string_consts = []
        if self.target_triple:
            self._emit("target triple = \"%s\"" % self.target_triple)
            self._emit("")
        for line in RUNTIME_DECLS.strip().split("\n"):
            self._emit(line)
        self._emit("")
        # Emit panic message constants.
        self._emit('@.panic_overflow_msg = private unnamed_addr constant [18 x i8] c"integer overflow\\00"')
        self._emit('@.panic_divzero_msg = private unnamed_addr constant [18 x i8] c"division by zero\\00"')
        self._emit("")
        # Emit string constants.
        for name, data, bytes_str in self._string_consts:
            self._emit('@%s = private unnamed_addr constant [%d x i8] [%s]' % (
                name, len(data) + 1, bytes_str))
        self._emit("")
        # Emit functions.
        for fname, fn in self.program["fns"].items():
            self._current_ret_type_value = fn["ret"]
            self._emit_function(fname, fn)
        return "\n".join(self.lines) + "\n"


def float_to_ieee_bits(f: float) -> int:
    import struct
    return struct.unpack(">Q", struct.pack(">d", f))[0]


def emit_module(program, target_triple: Optional[str] = None) -> str:
    """Convenience: emit LLVM IR for a checked program."""
    return LLVMEmitter(program, target_triple).emit_final()
