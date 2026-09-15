"""emit_lower - verbatim segment of the original tools/llvm_emit.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from llvm_common import (
    Dict, Tuple, _is_list, _list_elem, _unsupported, hls_type_to_llvm,
)

class LLVMEmitterLower(object):
    def _lower_stmt(self, stmt: Dict):
        # Guard: if the current block is already terminated (return /
        # break / continue / panic / exit happened), the rest of this block
        # is unreachable dead code — emitting instructions here would
        # produce invalid IR (BUG-DS4-5: e.g. the `for` increment used to
        # be emitted after a `continue`'s branch).
        if self._cur_block is None:
            return
        k = stmt["k"]
        if k == "let":
            val_ty, val = self._lower_expr_typed(stmt["value"])
            slot, ty = self._get_slot(stmt["name"], stmt["t"])
            self._locals[stmt["name"]] = (slot, ty)
            if self._cur_block is None:
                # RHS was `never` (panic/exit) — nothing left to store.
                return
            self._emit("  store %s %s, ptr %s" % (ty, self._coerce(val_ty, val, ty), slot))
        elif k == "assign":
            self._lower_assign(stmt)
        elif k == "return":
            if stmt.get("value") is None:
                self._emit("  ret void")
            else:
                val_ty, val = self._lower_expr_typed(stmt["value"])
                if self._cur_block is None:
                    # Value was `never` (panic(...)/exit(...)) — the call
                    # itself already diverged; do not emit a ret after the
                    # unreachable (mirrors the C backend's BUG-004 handling).
                    return
                fn_ret = self._current_ret_type_value
                ret_ty = hls_type_to_llvm(fn_ret)
                self._emit("  ret %s %s" % (ret_ty, self._coerce(val_ty, val, ret_ty)))
        elif k == "if":
            cond_ty, cond = self._lower_expr_typed(stmt["cond"])
            if self._cur_block is None:
                return  # cond was `never` — unreachable
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
            for s in stmt["then"]:
                self._lower_stmt(s)
            if self._cur_block is not None:
                self._emit("  br label %%%s" % end_lbl)
            if has_else:
                self._emit("%s:" % else_lbl)
                for s in stmt["els"]:
                    self._lower_stmt(s)
                if self._cur_block is not None:
                    self._emit("  br label %%%s" % end_lbl)
            self._emit("%s:" % end_lbl)
        elif k == "while":
            cond_lbl = self._fresh_label("while_cond")
            body_lbl = self._fresh_label("while_body")
            end_lbl = self._fresh_label("while_end")
            self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % cond_lbl)
            cond_ty, cond = self._lower_expr_typed(stmt["cond"])
            if self._cur_block is None:
                return  # cond was `never`
            cond = self._to_i1(cond_ty, cond)
            self._emit("  br i1 %s, label %%%s, label %%%s" % (
                cond, body_lbl, end_lbl))
            self._emit("%s:" % body_lbl)
            self._loop_stack.append((cond_lbl, end_lbl))
            for s in stmt["body"]:
                self._lower_stmt(s)
            self._loop_stack.pop()
            if self._cur_block is not None:
                self._emit("  br label %%%s" % cond_lbl)
            self._emit("%s:" % end_lbl)
        elif k == "for":
            self._lower_for(stmt)
        elif k == "break":
            if self._loop_stack:
                self._emit("  br label %%%s" % self._loop_stack[-1][1])
        elif k == "continue":
            if self._loop_stack:
                self._emit("  br label %%%s" % self._loop_stack[-1][0])
        elif k == "expr":
            self._lower_expr_typed(stmt["e"])
        else:
            # Unknown statement kind — the checker rejects these; skip.
            return

    def _lower_for(self, stmt: Dict):
        """Lower `for v: T in iter { body }`.

        Mirrors the C backend exactly (BUG-SC-4 semantics):
          - the element count is snapshotted ONCE (appended elements are
            not visited);
          - each iteration re-checks the CURRENT list length and breaks
            when the list has shrunk below the index;
          - the loop variable slot is hoisted to the function entry;
          - the increment lives in its own block, so `continue` jumps to
            the increment (BUG-DS4-5: the increment used to be emitted
            after the continue branch, producing invalid IR).
        """
        iter_ty, iter_val = self._lower_expr_typed(stmt["iter"])
        if self._cur_block is None:
            return
        len_val = self._fresh("len")
        self._emit("  %s = call i64 @hl_list_len(ptr %s)" % (len_val, iter_val))
        i_slot, _ = self._get_slot(stmt["var"] + "#idx", "int")
        self._emit("  store i64 0, ptr %s" % i_slot)
        # Loop variable slot (hoisted; vtype is the element type).
        v_slot, v_ty = self._get_slot(stmt["var"], stmt["vtype"])
        self._locals[stmt["var"]] = (v_slot, v_ty)
        cond_lbl = self._fresh_label("for_cond")
        body_lbl = self._fresh_label("for_body")
        elem_lbl = self._fresh_label("for_elem")
        inc_lbl = self._fresh_label("for_inc")
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
        # Shrink re-check (BUG-SC-4 semantics, same as the C backend).
        cur_len = self._fresh("curlen")
        self._emit("  %s = call i64 @hl_list_len(ptr %s)" % (cur_len, iter_val))
        ok_tmp = self._fresh("ok")
        self._emit("  %s = icmp slt i64 %s, %s" % (ok_tmp, i_val, cur_len))
        self._emit("  br i1 %s, label %%%s, label %%%s" % (
            ok_tmp, elem_lbl, end_lbl))
        self._emit("%s:" % elem_lbl)
        # Fetch + unbox the element by the declared element type.
        elem = self._fresh("elem")
        self._emit("  %s = call ptr @hl_list_get(ptr %s, i64 %s)" % (
            elem, iter_val, i_val))
        unboxed = self._unbox_value(stmt["vtype"], elem)
        self._emit("  store %s %s, ptr %s" % (v_ty, self._coerce(
            hls_type_to_llvm(stmt["vtype"]), unboxed, v_ty), v_slot))
        self._loop_stack.append((inc_lbl, end_lbl))
        for s in stmt["body"]:
            self._lower_stmt(s)
        self._loop_stack.pop()
        if self._cur_block is not None:
            self._emit("  br label %%%s" % inc_lbl)
        # Increment block — also the `continue` target.
        self._emit("%s:" % inc_lbl)
        i_val2 = self._fresh("i")
        self._emit("  %s = load i64, ptr %s" % (i_val2, i_slot))
        inc = self._fresh("inc")
        self._emit("  %s = add i64 %s, 1" % (inc, i_val2))
        self._emit("  store i64 %s, ptr %s" % (inc, i_slot))
        self._emit("  br label %%%s" % cond_lbl)
        self._emit("%s:" % end_lbl)

    def _lower_assign(self, stmt: Dict):
        target = stmt["target"]
        val_ty, val = self._lower_expr_typed(stmt["value"])
        if self._cur_block is None:
            return  # RHS was `never`
        if target["k"] == "ident":
            entry = self._locals.get(target["name"])
            if entry is None:
                # The checker already rejects undefined vars; defensively skip.
                return
            slot, slot_ty = entry
            self._emit("  store %s %s, ptr %s" % (
                slot_ty, self._coerce(val_ty, val, slot_ty), slot))
        elif target["k"] == "field":
            self._lower_field_assign_typed(target, val_ty, val)
        elif target["k"] == "index":
            # Index assignment: box the value by the element type of the
            # target list (BUG-DS4-8: the runtime stores boxed elements).
            lst_ty, lst = self._lower_expr_typed(target["target"])
            idx_ty, idx = self._lower_expr_typed(target["idx"])
            list_t = target["target"].get("t", "list[int]")
            elem_t = _list_elem(list_t) if _is_list(list_t) else "int"
            boxed = self._box_value(elem_t, val_ty, val)
            self._emit("  call void @hl_list_set(ptr %s, i64 %s, ptr %s)" % (
                lst, idx, boxed))

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
            return self._lower_unop_typed(e)
        if k == "call":
            return self._lower_call_typed(e)
        if k == "method" or k == "fieldcall":
            return self._lower_method_typed(e)
        if k == "field":
            return self._lower_field_access_typed(e)
        if k == "index":
            lst_ty, lst = self._lower_expr_typed(e["target"])
            idx_ty, idx = self._lower_expr_typed(e["idx"])
            if self._cur_block is None:
                return ("i64", "0")
            tmp = self._fresh("e")
            self._emit("  %s = call ptr @hl_list_get(ptr %s, i64 %s)" % (
                tmp, lst, idx))
            # Unbox the element according to the (checker-annotated) type.
            et = e.get("t", "int")
            unboxed = self._unbox_value(et, tmp)
            return (hls_type_to_llvm(et), unboxed)
        if k == "listlit":
            tmp = self._fresh("lst")
            # Arena-mode contract: elem_free = null (never release items).
            self._emit("  %s = call ptr @hl_list_new(ptr null)" % tmp)
            # Box each element by the (checker-annotated) element type.
            list_t = e.get("t", "list[int]")
            elem_t = _list_elem(list_t) if _is_list(list_t) else "int"
            for item in e["items"]:
                ity, iv = self._lower_expr_typed(item)
                if self._cur_block is None:
                    break
                boxed = self._box_value(elem_t, ity, iv)
                self._emit("  call void @hl_list_push(ptr %s, ptr %s)" % (tmp, boxed))
            return ("ptr", tmp)
        if k == "structlit":
            return self._lower_structlit_typed(e)
        if k == "match":
            return self._lower_match_typed(e)
        if k == "qmark":
            return self._lower_qmark_typed(e)
        if k == "enumlit":
            return self._lower_enumlit_typed(e)
        # Fallback for unsupported expression kinds.
        _unsupported("expression kind '%s'" % k, e)

    def _lower_structlit_typed(self, e: Dict) -> Tuple[str, str]:
        """Stage 12 release: lower a struct literal via the runtime
        constructor helpers.

        Allocates an opaque struct via `hl_struct_alloc(<size>)`, then
        stores each field via the typed `hl_struct_set_*` helper. The
        field index comes from the checker's `sfields` annotation (a
        list of (name, type) tuples in declaration order). If `sfields`
        is missing (older checker), we fall back to positional indexing.
        """
        name = e.get("name", "?")
        # The checker annotates the resolved struct's field list as `sfields`.
        sfields = e.get("sfields") or []
        if not sfields:
            # Without the field list, we can't dispatch to the right setter.
            _unsupported("struct literal %s (checker did not annotate sfields)" % name, e)
        # Estimate size: 16 bytes per field (covers i64/double/ptr alignment).
        size_bytes = max(16, 16 * len(sfields))
        tmp = self._fresh("st")
        self._emit("  %s = call ptr @hl_struct_alloc(i64 %d)" % (tmp, size_bytes))
        # Set each provided field by its declaration index.
        decl_names = [fname for fname, _ in sfields]
        for fname, fexpr in e.get("fields", []):
            if fname not in decl_names:
                _unsupported("struct literal %s: unknown field '%s'" % (name, fname), e)
            fidx = decl_names.index(fname)
            ftype = sfields[fidx][1]
            fty, fval = self._lower_expr_typed(fexpr)
            if self._cur_block is None:
                return ("ptr", tmp)
            if ftype == "int" or ftype.startswith("tainted[int]"):
                v = self._coerce(fty, fval, "i64")
                self._emit("  call void @hl_struct_set_i64(ptr %s, i64 %d, i64 %s)"
                           % (tmp, fidx, v))
            elif ftype == "float" or ftype.startswith("tainted[float]"):
                v = self._coerce(fty, fval, "double")
                self._emit("  call void @hl_struct_set_f64(ptr %s, i64 %d, double %s)"
                           % (tmp, fidx, v))
            elif ftype == "bool" or ftype.startswith("tainted[bool]"):
                v = self._coerce(fty, fval, "i1")
                self._emit("  call void @hl_struct_set_bool(ptr %s, i64 %d, i1 %s)"
                           % (tmp, fidx, v))
            else:
                v = self._coerce(fty, fval, "ptr")
                self._emit("  call void @hl_struct_set_ptr(ptr %s, i64 %d, ptr %s)"
                           % (tmp, fidx, v))
        return ("ptr", tmp)

    def _lower_enumlit_typed(self, e: Dict) -> Tuple[str, str]:
        """Stage 12 release: lower an enum literal `Variant(...)` via
        `hl_enum_new_variant(idx)`. The checker annotates the variant's
        declaration index as `variant_idx`."""
        idx = e.get("variant_idx")
        if idx is None:
            _unsupported("enum literal (checker did not annotate variant_idx)", e)
        tmp = self._fresh("en")
        self._emit("  %s = call ptr @hl_enum_new_variant(i64 %d)" % (tmp, idx))
        # If the variant has a payload, store it via hl_struct_set_* on the
        # payload pointer. The checker annotates the payload type as
        # `payload_type` (or None for unit variants).
        payload_type = e.get("payload_type")
        if payload_type and e.get("args"):
            payload_ptr = self._fresh("ep")
            self._emit("  %s = call ptr @hl_enum_payload(ptr %s)" % (payload_ptr, tmp))
            pty, pval = self._lower_expr_typed(e["args"][0])
            if self._cur_block is None:
                return ("ptr", tmp)
            if payload_type == "int" or payload_type.startswith("tainted[int]"):
                v = self._coerce(pty, pval, "i64")
                self._emit("  call void @hl_struct_set_i64(ptr %s, i64 0, i64 %s)"
                           % (payload_ptr, v))
            elif payload_type == "float" or payload_type.startswith("tainted[float]"):
                v = self._coerce(pty, pval, "double")
                self._emit("  call void @hl_struct_set_f64(ptr %s, i64 0, double %s)"
                           % (payload_ptr, v))
            elif payload_type == "bool" or payload_type.startswith("tainted[bool]"):
                v = self._coerce(pty, pval, "i1")
                self._emit("  call void @hl_struct_set_bool(ptr %s, i64 0, i1 %s)"
                           % (payload_ptr, v))
            else:
                v = self._coerce(pty, pval, "ptr")
                self._emit("  call void @hl_struct_set_ptr(ptr %s, i64 0, ptr %s)"
                           % (payload_ptr, v))
        return ("ptr", tmp)

    def _lower_match_typed(self, e: Dict) -> Tuple[str, str]:
        """Stage 12 release + deep-scan-7 fix: lower a match expression
        on an enum.

        Lowers to a `switch` on the enum's variant tag (loaded via
        `hl_enum_tag`). Each arm becomes a basic block. The arm body's
        result is stored to a per-match alloca; the result is loaded
        after the end label.

        Deep-scan-7 fix: the previous implementation always returned
        0/null for every match (the comment at the old line 965-968
        explicitly said "return 0/i64 — this matches the existing
        alpha fallback"). This was silent miscompilation — every
        `match` in --emit llvm mode produced 0 regardless of the arm
        body's actual value. The fix uses a per-match alloca: each
        arm stores its result value, and the end block loads it.

        Only matches on enums are supported today (matching on ints/
        strs with full literal patterns is future work).
        """
        scrut_ty, scrut = self._lower_expr_typed(e["scrut"])
        if self._cur_block is None:
            return ("i64", "0")
        # Load the variant tag.
        tag = self._fresh("tag")
        self._emit("  %s = call i64 @hl_enum_tag(ptr %s)" % (tag, scrut))
        # Build a switch over the arms.
        end_lbl = self._fresh_label("match_end")
        arms = e.get("arms", [])
        # Result type is the match expression's annotated type.
        result_t = e.get("t", "int")
        llvm_ret_ty = hls_type_to_llvm(result_t)
        # Deep-scan-7 fix: allocate a slot for the match result. Each
        # arm stores its body's result here; the end block loads it.
        # This is the simplest correct lowering — phi nodes would be
        # more efficient but require tracking every predecessor block.
        # Deep-scan-12 fix (DSS-T-02): emit the alloca in the ENTRY block
        # (the function header at line 430), not in the current basic
        # block. A `match` inside a `while` body would otherwise re-emit
        # the alloca on every iteration; LLVM allocas are not released
        # until function return, so the stack grew without bound. We
        # allocate a per-match slot ONCE in the entry block (keyed by the
        # match's source line so nested matches in the same function get
        # distinct slots), and use it from the current block.
        result_slot = self._fresh("match_result")
        llvm_ret_ty_full = llvm_ret_ty
        self._entry_allocas.append((result_slot, llvm_ret_ty_full))
        # The slot is allocated in the entry block — we just reference it
        # from here on out.
        # Pre-emit each arm as a basic block. Each arm records:
        #   (variant_idx_or_None_for_default, label, body_result_value, body_end_block)
        arm_records = []
        # First pass: emit labels + bodies.
        for arm in arms:
            arm_lbl = self._fresh_label("match_arm")
            var_idx = arm.get("variant_idx")
            arm_records.append([var_idx, arm_lbl, None, None])
        # Default arm (wildcard).
        default_lbl = None
        default_rec = None
        for r in arm_records:
            if r[0] is None:
                default_lbl = r[1]
                default_rec = r
                break
        if default_lbl is None:
            # No wildcard — emit a panic block as the default.
            default_lbl = self._fresh_label("match_panic")
        # Emit the switch.
        cases = [(r[0], r[1]) for r in arm_records if r[0] is not None]
        switch_cases = ", ".join("i64 %d label %%%s" % (idx, lbl)
                                 for idx, lbl in cases)
        if switch_cases:
            self._emit("  switch i64 %s, label %%%s [%s]" % (
                tag, default_lbl, switch_cases))
        else:
            self._emit("  br label %%%s" % default_lbl)
        # Emit the default (panic) block if no wildcard.
        if default_rec is None:
            self._emit("%s:" % default_lbl)
            self._emit('  call void @hl_die(ptr @.panic_overflow_msg)')
            self._emit("  unreachable")
        # Emit each arm body.
        for i, arm in enumerate(arms):
            lbl = arm_records[i][1]
            self._emit("%s:" % lbl)
            # Bind the payload (if any) — the checker annotates the
            # pattern's binding names and the variant's instantiated
            # payload types (deep-scan-20: 'binds' covers multi-payload
            # variants like Op.Add(a, b); the legacy single-payload
            # 'bind_name'/'payload_type' pair is kept as a fallback).
            binds = arm.get("binds")
            if not binds and arm.get("bind_name") and arm.get("payload_type"):
                binds = [(arm["bind_name"], arm["payload_type"])]
            if binds:
                payload_ptr = self._fresh("ap")
                self._emit("  %s = call ptr @hl_enum_payload(ptr %s)" % (payload_ptr, scrut))
                for fi, (bname, ptype) in enumerate(binds):
                    slot, slot_ty = self._get_slot(bname, ptype)
                    self._locals[bname] = (slot, slot_ty)
                    if ptype == "int" or ptype.startswith("tainted[int]"):
                        ptmp = self._fresh("pv")
                        self._emit("  %s = call i64 @hl_struct_get_i64(ptr %s, i64 %d)"
                                   % (ptmp, payload_ptr, fi))
                        self._emit("  store i64 %s, ptr %s" % (ptmp, slot))
                    elif ptype == "float" or ptype.startswith("tainted[float]"):
                        ptmp = self._fresh("pv")
                        self._emit("  %s = call double @hl_struct_get_f64(ptr %s, i64 %d)"
                                   % (ptmp, payload_ptr, fi))
                        self._emit("  store double %s, ptr %s" % (ptmp, slot))
                    elif ptype == "bool" or ptype.startswith("tainted[bool]"):
                        ptmp = self._fresh("pv")
                        self._emit("  %s = call i1 @hl_struct_get_bool(ptr %s, i64 %d)"
                                   % (ptmp, payload_ptr, fi))
                        self._emit("  store i1 %s, ptr %s" % (ptmp, slot))
                    else:
                        ptmp = self._fresh("pv")
                        self._emit("  %s = call ptr @hl_struct_get_ptr(ptr %s, i64 %d)"
                                   % (ptmp, payload_ptr, fi))
                        self._emit("  store ptr %s, ptr %s" % (ptmp, slot))
            # Lower the arm body. The HLS match arm body is a single
            # EXPRESSION (the parser's `parse_expr`), not a list of
            # statements. Deep-scan-7 fix: the old code iterated
            # `arm["body"]` as if it were a list of statements — but
            # since it's actually a single expression node, the
            # iteration walked the dict's KEYS (strings), causing
            # `AttributeError: 'str' object has no attribute 'get'`.
            body = arm.get("body")
            if isinstance(body, dict):
                # Expression form (the only form parse_arm produces today).
                arm_result_ty, arm_result_val = self._lower_expr_typed(body)
            elif isinstance(body, list):
                # Statement-list form (future-proofing; matches the
                # statement-list lowering used elsewhere). The last
                # `expr` statement's value is the arm's result.
                arm_result_ty = llvm_ret_ty
                arm_result_val = "0" if llvm_ret_ty != "ptr" else "null"
                for s in body:
                    if isinstance(s, dict) and s.get("k") == "expr":
                        arm_result_ty, arm_result_val = self._lower_expr_typed(s["e"])
                    else:
                        self._lower_stmt(s)
            else:
                # No body? Use a zero default.
                arm_result_ty = llvm_ret_ty
                arm_result_val = "0" if llvm_ret_ty != "ptr" else "null"
            # Deep-scan-7 fix: store the arm result to the per-match slot.
            # Cast the value to the match's result type if needed.
            # Deep-scan-20 fix: an arm whose body DIVERGES (panic ->
            # unreachable) leaves no current block — the old code still
            # emitted `store` + `br` AFTER the unreachable terminator,
            # producing invalid IR (llvm-as rejects it).
            if arm_result_ty != llvm_ret_ty:
                # Coerce — same logic as the assignment path.
                # Deep-scan-15 fix: the previous call had the first two
                # arguments swapped (`_coerce(arm_result_val, arm_result_ty, ...)`).
                # _coerce's signature is `(val_ty, val, want_ty)` — passing
                # the value string as `val_ty` made every defensive
                # coercion silently misbehave (the i1/i64/double/ptr
                # dispatch never matched). All other call sites in this
                # file use the correct order; this one diverged.
                arm_result_val = self._coerce(
                    arm_result_ty, arm_result_val, llvm_ret_ty)
            if self._cur_block is not None:
                self._emit("  store %s %s, ptr %s"
                           % (llvm_ret_ty, arm_result_val, result_slot))
                self._emit("  br label %%%s" % end_lbl)
            arm_records[i][2] = arm_result_ty
            arm_records[i][3] = self._cur_block  # may be None if arm diverged
        # End block — load the match result from the alloca.
        self._emit("%s:" % end_lbl)
        result_val = self._fresh("match_val")
        self._emit("  %s = load %s, ptr %s"
                   % (result_val, llvm_ret_ty, result_slot))
        return (llvm_ret_ty, result_val)



__all__ = [
]
