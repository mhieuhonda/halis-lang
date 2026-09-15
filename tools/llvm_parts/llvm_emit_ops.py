"""emit_ops - verbatim segment of the original tools/llvm_emit.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from llvm_common import (
    Dict, Tuple, _unsupported, hls_type_to_llvm,
)

class LLVMEmitterOps(object):
    def _lower_qmark_typed(self, e: Dict) -> Tuple[str, str]:
        """Stage 12 release: lower the `?` operator.

        `expr?` on a `Result[T, E]` enum:
          - if Ok, unwrap to T and continue.
          - if Err, propagate the error to the caller.

        Lowers to: extract the tag, branch on Ok-vs-Err, extract the
        payload, return on Err. The Ok payload is the unwrapped value.
        The checker annotates `qmark_ok_idx` (variant index for Ok) and
        `qmark_err_idx` (variant index for Err).
        """
        scrut_ty, scrut = self._lower_expr_typed(e["e"])
        if self._cur_block is None:
            return ("i64", "0")
        tag = self._fresh("qtag")
        self._emit("  %s = call i64 @hl_enum_tag(ptr %s)" % (tag, scrut))
        ok_idx = e.get("qmark_ok_idx", 0)
        # qmark_err_idx is not needed here: the branch tests `tag ==
        # ok_idx` and EVERYTHING else falls through to the err path (the
        # enum tag space is closed), so the err index is implicit.
        ok_lbl = self._fresh_label("qmark_ok")
        err_lbl = self._fresh_label("qmark_err")
        end_lbl = self._fresh_label("qmark_end")
        cond = self._fresh("qcond")
        self._emit("  %s = icmp eq i64 %s, %d" % (cond, tag, ok_idx))
        self._emit("  br i1 %s, label %%%s, label %%%s" % (cond, ok_lbl, err_lbl))
        # Err branch: extract the Err payload and return it from the
        # current function (propagation).
        self._emit("%s:" % err_lbl)
        # Deep-scan-12 fix (DSS-T-15): use the ENCLOSING function's actual
        # return type for the `ret` instruction, not a hardcoded `ret ptr`.
        # The hardcoded `ret ptr` produced invalid IR for any function
        # returning int / float / bool / void — LLVM rejects `ret ptr` in
        # an `i64`-returning function. The Err value is the scrut enum,
        # which is already a ptr; we return it as the function's actual
        # LLVM return type (the enum ptr matches a `ptr` return; for any
        # other return type the function was mis-declared, but that is a
        # checker concern, not a codegen concern — we emit the declared
        # type and let LLVM validate the call chain).
        fn_ret_llvm = hls_type_to_llvm(self._current_ret_type_value) \
            if self._current_ret_type_value else "ptr"
        if fn_ret_llvm == "void":
            # A void function with `?` should never exist (the checker
            # rejects it), but defensive: don't emit a value return.
            self._emit("  ret void")
        else:
            self._emit("  ret %s %s" % (fn_ret_llvm, scrut))
        # Ok branch: extract the Ok payload.
        self._emit("%s:" % ok_lbl)
        payload_ptr = self._fresh("qpayload")
        self._emit("  %s = call ptr @hl_enum_payload(ptr %s)" % (payload_ptr, scrut))
        ok_t = e.get("qmark_ok_type", "int")
        if ok_t == "int" or ok_t.startswith("tainted[int]"):
            tmp = self._fresh("qv")
            self._emit("  %s = call i64 @hl_struct_get_i64(ptr %s, i64 0)"
                       % (tmp, payload_ptr))
            self._emit("  br label %%%s" % end_lbl)
            self._emit("%s:" % end_lbl)
            return ("i64", tmp)
        if ok_t == "float" or ok_t.startswith("tainted[float]"):
            tmp = self._fresh("qv")
            self._emit("  %s = call double @hl_struct_get_f64(ptr %s, i64 0)"
                       % (tmp, payload_ptr))
            self._emit("  br label %%%s" % end_lbl)
            self._emit("%s:" % end_lbl)
            return ("double", tmp)
        if ok_t == "bool" or ok_t.startswith("tainted[bool]"):
            tmp = self._fresh("qv")
            self._emit("  %s = call i1 @hl_struct_get_bool(ptr %s, i64 0)"
                       % (tmp, payload_ptr))
            self._emit("  br label %%%s" % end_lbl)
            self._emit("%s:" % end_lbl)
            return ("i1", tmp)
        # ptr-valued payload.
        tmp = self._fresh("qv")
        self._emit("  %s = call ptr @hl_struct_get_ptr(ptr %s, i64 0)"
                   % (tmp, payload_ptr))
        self._emit("  br label %%%s" % end_lbl)
        self._emit("%s:" % end_lbl)
        return ("ptr", tmp)

    def _lower_unop_typed(self, e: Dict) -> Tuple[str, str]:
        a_ty, a = self._lower_expr_typed(e["e"])
        if self._cur_block is None:
            return ("i64", "0")
        if e["op"] == "-":
            if a_ty == "i64":
                # Checked negation via the runtime (identical panic to the
                # interpreter: -INT64_MIN overflows).
                tmp = self._fresh("u")
                self._emit("  %s = call i64 @hl_neg_i64(i64 %s)" % (tmp, a))
                return ("i64", tmp)
            # float negation
            tmp = self._fresh("u")
            self._emit("  %s = fneg double %s" % (tmp, a))
            return ("double", tmp)
        if e["op"] == "!":
            tmp = self._fresh("u")
            self._emit("  %s = xor i1 %s, 1" % (tmp, a))
            return ("i1", tmp)
        return ("i64", "0")

    # ---------- binary operators ----------
    def _lower_binop_typed(self, e: Dict) -> Tuple[str, str]:
        op = e["op"]
        lt = e["l"].get("t", "int")
        # BUG-DS4-9: && and || must SHORT-CIRCUIT (the interpreter does).
        # Lowering them as eager `and`/`or` evaluated the RHS even when the
        # LHS already decided the result — `x != 0 && 10 / x > 0` panicked
        # on division by zero under the LLVM backend but not in the
        # interpreter. Now lowered as control flow + phi.
        if op == "&&" or op == "||":
            return self._lower_shortcircuit_typed(e)
        a_ty, a = self._lower_expr_typed(e["l"])
        if self._cur_block is None:
            return ("i1", "0")
        b_ty, b = self._lower_expr_typed(e["r"])
        if self._cur_block is None:
            return ("i1", "0")
        # String operations.
        if lt == "str":
            return self._lower_str_binop(op, a, b)
        # Float operations (comparisons produce i1 — no widening).
        if lt == "float":
            return self._lower_float_binop(op, a, b)
        # Bool equality.
        if lt == "bool":
            if op == "==":
                tmp = self._fresh("r")
                self._emit("  %s = icmp eq i1 %s, %s" % (tmp, a, b))
                return ("i1", tmp)
            if op == "!=":
                tmp = self._fresh("r")
                self._emit("  %s = icmp ne i1 %s, %s" % (tmp, a, b))
                return ("i1", tmp)
            # Checker rejects ordering / arithmetic on bool.
            _unsupported("operator '%s' on bool" % op, e)
        # Integer arithmetic with overflow checks; division/modulo via the
        # runtime (zero + INT64_MIN/-1 panics identical to the interpreter
        # and the C backend).
        if op == "+":
            return ("i64", self._emit_overflow_op("llvm.sadd.with.overflow.i64", a, b))
        if op == "-":
            return ("i64", self._emit_overflow_op("llvm.ssub.with.overflow.i64", a, b))
        if op == "*":
            return ("i64", self._emit_overflow_op("llvm.smul.with.overflow.i64", a, b))
        if op == "/":
            tmp = self._fresh("r")
            self._emit("  %s = call i64 @hl_div_i64(i64 %s, i64 %s)" % (tmp, a, b))
            return ("i64", tmp)
        if op == "%":
            tmp = self._fresh("r")
            self._emit("  %s = call i64 @hl_mod_i64(i64 %s, i64 %s)" % (tmp, a, b))
            return ("i64", tmp)
        # Integer comparisons (i1 results — BUG-DS4-4: previously every
        # comparison was zext'ed to i64, so storing into a bool slot or
        # branching on it produced invalid IR).
        icmp_map = {"==": "eq", "!=": "ne",
                    "<": "slt", "<=": "sle", ">": "sgt", ">=": "sge"}
        if op in icmp_map:
            tmp = self._fresh("r")
            self._emit("  %s = icmp %s i64 %s, %s" % (tmp, icmp_map[op], a, b))
            return ("i1", tmp)
        _unsupported("operator '%s'" % op, e)

    def _lower_shortcircuit_typed(self, e: Dict) -> Tuple[str, str]:
        """Lower `a && b` / `a || b` with REAL short-circuit semantics:
        the RHS is only evaluated when the LHS leaves the result undecided.
        Uses two extra blocks and a phi node (no temp alloca, so loops
        don't grow the stack)."""
        op = e["op"]
        la_ty, la = self._lower_expr_typed(e["l"])
        if self._cur_block is None:
            return ("i1", "0")
        la = self._to_i1(la_ty, la)
        lhs_block = self._cur_block  # block the LHS ended in (open)
        n = self._sc_counter
        self._sc_counter += 1
        rhs_lbl = "sc_rhs_%d" % n
        end_lbl = "sc_end_%d" % n
        if op == "&&":
            # LHS false -> result is false; skip the RHS entirely.
            self._emit("  br i1 %s, label %%%s, label %%%s" % (la, rhs_lbl, end_lbl))
        else:  # "||"
            # LHS true -> result is true; skip the RHS entirely.
            self._emit("  br i1 %s, label %%%s, label %%%s" % (la, end_lbl, rhs_lbl))
        self._emit("%s:" % rhs_lbl)
        rb_ty, rb = self._lower_expr_typed(e["r"])
        if self._cur_block is None:
            # RHS was `never` (panic/exit): the block is closed. The value
            # along that edge is unreachable — use 0 as a placeholder.
            rhs_block = None
            rb_i1 = "0"
        else:
            rhs_block = self._cur_block
            rb_i1 = self._to_i1(rb_ty, rb)
            self._emit("  br label %%%s" % end_lbl)
        self._emit("%s:" % end_lbl)
        # phi: value along the short-circuit edge is 0 for && (false),
        # 1 for || (true); value along the RHS edge is the RHS result.
        short_val = "0" if op == "&&" else "1"
        tmp = self._fresh("sc")
        if rhs_block is not None:
            self._emit("  %s = phi i1 [ %s, %%%s ], [ %s, %%%s ]" % (
                tmp, short_val, lhs_block, rb_i1, rhs_block))
        else:
            # Degenerate case: RHS diverges — the phi has one real edge.
            self._emit("  %s = phi i1 [ %s, %%%s ]" % (tmp, short_val, lhs_block))
        return ("i1", tmp)

    def _lower_str_binop(self, op, a, b) -> Tuple[str, str]:
        if op == "+":
            tmp = self._fresh("cat")
            self._emit("  %s = call ptr @hl_str_concat(ptr %s, ptr %s)" % (tmp, a, b))
            return ("ptr", tmp)
        if op == "==":
            tmp = self._fresh("r")
            self._emit("  %s = call i1 @hl_str_eq(ptr %s, ptr %s)" % (tmp, a, b))
            return ("i1", tmp)
        if op == "!=":
            eq = self._fresh("eq")
            self._emit("  %s = call i1 @hl_str_eq(ptr %s, ptr %s)" % (eq, a, b))
            tmp = self._fresh("r")
            self._emit("  %s = xor i1 %s, 1" % (tmp, eq))
            return ("i1", tmp)
        # BUG-DS4-10: string relational comparisons used to fall into the
        # INTEGER path (icmp on pointers — invalid IR and wrong semantics).
        # Route them through hl_str_cmp like the C backend.
        cmp_map = {"<": "slt", "<=": "sle", ">": "sgt", ">=": "sge"}
        if op in cmp_map:
            c = self._fresh("c")
            self._emit("  %s = call i64 @hl_str_cmp(ptr %s, ptr %s)" % (c, a, b))
            tmp = self._fresh("r")
            self._emit("  %s = icmp %s i64 %s, 0" % (tmp, cmp_map[op], c))
            return ("i1", tmp)
        _unsupported("operator '%s' on str" % op, {"line": 0})

    def _lower_float_binop(self, op, a, b) -> Tuple[str, str]:
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
        fcmp_map = {"==": "oeq", "!=": "one",
                    "<": "olt", "<=": "ole", ">": "ogt", ">=": "oge"}
        if op in fcmp_map:
            self._emit("  %s = fcmp %s double %s, %s" % (tmp, fcmp_map[op], a, b))
            return ("i1", tmp)
        _unsupported("operator '%s' on float" % op, {"line": 0})

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
        return out

    # ---------- boxing helpers (list/map element ABI) ----------
    def _box_value(self, hls_ty: str, val_ty: str, val: str) -> str:
        """Box a primitive value for storage in a dynamic list/map.
        Heap types (str, list, map, ...) are stored as raw pointers."""
        if hls_ty == "int" or hls_ty.startswith("tainted[int]"):
            v = self._coerce(val_ty, val, "i64")
            tmp = self._fresh("box")
            self._emit("  %s = call ptr @hl_box_i64(i64 %s)" % (tmp, v))
            return tmp
        if hls_ty == "float" or hls_ty.startswith("tainted[float]"):
            v = self._coerce(val_ty, val, "double")
            tmp = self._fresh("box")
            self._emit("  %s = call ptr @hl_box_f64(double %s)" % (tmp, v))
            return tmp
        if hls_ty == "bool" or hls_ty.startswith("tainted[bool]"):
            v = self._coerce(val_ty, val, "i1")
            tmp = self._fresh("box")
            self._emit("  %s = call ptr @hl_box_bool(i1 %s)" % (tmp, v))
            return tmp
        # ptr-valued: use as-is (matches the C backend's box_ptr).
        return val

    def _unbox_value(self, hls_ty: str, box: str) -> str:
        """Unbox a dynamic list/map element (a box pointer) to the element's
        LLVM value. Mirrors the C backend's gen_unbox: primitives load
        through the box pointer; heap types pass through unchanged."""
        if hls_ty == "int" or hls_ty.startswith("tainted[int]"):
            tmp = self._fresh("ub")
            self._emit("  %s = load i64, ptr %s" % (tmp, box))
            return tmp
        if hls_ty == "float" or hls_ty.startswith("tainted[float]"):
            tmp = self._fresh("ub")
            self._emit("  %s = load double, ptr %s" % (tmp, box))
            return tmp
        if hls_ty == "bool" or hls_ty.startswith("tainted[bool]"):
            tmp = self._fresh("ub")
            self._emit("  %s = load i1, ptr %s" % (tmp, box))
            return tmp
        return box

    def _coerce(self, val_ty: str, val: str, want_ty: str) -> str:
        """Coerce a value between compatible LLVM types (defensive).

        SCAN-A fix: added i1→double and ptr→double paths (used when a
        bool/str is passed to a float parameter — defensive coercion
        that the checker should reject, but the LLVM backend must not
        emit invalid IR for)."""
        if val_ty == want_ty:
            return val
        if val_ty == "i1" and want_ty == "i64":
            tmp = self._fresh("z")
            self._emit("  %s = zext i1 %s to i64" % (tmp, val))
            return tmp
        if val_ty == "i64" and want_ty == "i1":
            tmp = self._fresh("t")
            self._emit("  %s = trunc i64 %s to i1" % (tmp, val))
            return tmp
        if val_ty == "i64" and want_ty == "ptr":
            tmp = self._fresh("i2p")
            self._emit("  %s = inttoptr i64 %s to ptr" % (tmp, val))
            return tmp
        if val_ty == "ptr" and want_ty == "i64":
            tmp = self._fresh("p2i")
            self._emit("  %s = ptrtoint ptr %s to i64" % (tmp, val))
            return tmp
        if val_ty == "i64" and want_ty == "double":
            tmp = self._fresh("s2d")
            self._emit("  %s = sitofp i64 %s to double" % (tmp, val))
            return tmp
        # SCAN-A fix: i1 → double (via i64 widening, then signed-to-float).
        if val_ty == "i1" and want_ty == "double":
            tmp_i64 = self._fresh("z2d")
            self._emit("  %s = zext i1 %s to i64" % (tmp_i64, val))
            tmp = self._fresh("b2d")
            self._emit("  %s = sitofp i64 %s to double" % (tmp, tmp_i64))
            return tmp
        # SCAN-A fix: ptr → double (ptrtoint to i64, then sitofp).
        if val_ty == "ptr" and want_ty == "double":
            tmp_i64 = self._fresh("p2i_d")
            self._emit("  %s = ptrtoint ptr %s to i64" % (tmp_i64, val))
            tmp = self._fresh("p2d")
            self._emit("  %s = sitofp i64 %s to double" % (tmp, tmp_i64))
            return tmp
        # Deep-scan-7 fix: double -> i64. The C backend's
        # `hl_float_to_int` (declared at line 130) range-checks the
        # input and panics on NaN / out-of-range values (mirroring the
        # interpreter's behavior). The LLVM backend's previous direct
        # `fptosi` was a SILENT MISCOMPILATION: NaN produced 0 in LLVM
        # but panicked in C; values > INT64_MAX wrapped to INT64_MIN in
        # LLVM (UB) but panicked in C. The fix delegates to the same
        # range-checked runtime helper the C backend uses, restoring
        # differential parity.
        if val_ty == "double" and want_ty == "i64":
            tmp = self._fresh("d2s")
            self._emit("  %s = call i64 @hl_float_to_int(double %s)"
                       % (tmp, val))
            return tmp
        return val

    # ---------- calls ----------


__all__ = [
]
