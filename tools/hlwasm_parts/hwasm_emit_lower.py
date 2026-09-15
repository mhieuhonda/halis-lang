"""emit_lower - verbatim segment of the original tools/hlwasm.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from hwasm_common import (
    BLOCK_VOID, Dict, HLError, I32, List, OP_BLOCK, OP_BR, OP_BR_IF,
    OP_CALL, OP_DROP, OP_ELSE, OP_END, OP_F64_ADD, OP_F64_CONST, OP_F64_DIV, OP_F64_EQ,
    OP_F64_GE, OP_F64_GT, OP_F64_LE, OP_F64_LT, OP_F64_MUL, OP_F64_NE, OP_F64_SUB, OP_I32_CONST,
    OP_I32_EQ, OP_I32_EQZ, OP_I32_NE, OP_I64_ADD, OP_I64_CONST, OP_I64_DIV_S, OP_I64_EQ, OP_I64_GE_S,
    OP_I64_GT_S, OP_I64_LE_S, OP_I64_LT_S, OP_I64_NE, OP_IF, OP_LOCAL_GET, OP_LOCAL_SET, OP_LOOP,
    OP_RETURN, Tuple, _taint_inner, hls_to_wasm_result, hls_to_wasm_valtype, sleb, struct, uleb,
)

class WasmEmitterLower(object):
    def _emit_function(self, fname: str, fn: Dict):
        """Emit a user-defined HLS function."""
        ret_type = fn["ret"]
        self._current_ret_type = ret_type
        # Reset per-function state.
        self._locals = {}
        self._local_count = 0
        self._loop_stack = []
        # (name, hls_type) -> (local idx, wasm valtype); filled by
        # _emit_function (deep-scan-20 sibling-scope binding fix).
        self._binding_pool: Dict[Tuple[str, str], Tuple[int, int]] = {}
        # Deep-scan-20: absolute count of wasm block/loop/if frames
        # currently open inside the function body. br depths for
        # break/continue are computed from it at the branch SITE, so an
        # if (or any nested block) between the loop header and the
        # branch no longer breaks the depth arithmetic.
        self._frames = 0
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
        # Assign local indices to each binding.
        # Deep-scan-20 fix (MED): deduplicate pre-collected bindings by
        # (name, valtype), NOT name alone. The checker allows the same
        # name with DIFFERENT types in sibling scopes (if/else arms —
        # each arm pops its child scope); name-only dedup reused the
        # first binding's local with the wrong valtype ("local.set
        # expected type i64, found i32.const"). Each (name, type) pair
        # gets its own local; the let statement re-points _locals[name]
        # to the right entry when it is lowered (sibling scopes never
        # overlap at runtime).
        seen_keys = set()
        local_decls: List[Tuple[int, int]] = []  # (count, valtype)
        # (name, hls_type) -> (idx, wasm_valtype)
        self._binding_pool: Dict[Tuple[str, str], Tuple[int, int]] = {}
        for bname, btype in collected:
            key = (bname, btype)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            idx = self._local_count
            self._local_count += 1
            vty = hls_to_wasm_valtype(btype)
            self._binding_pool[key] = (idx, vty)
            local_decls.append((1, vty))
            # First sighting of this NAME becomes the default mapping;
            # `let` statements re-point it as they are lowered.
            if bname not in self._locals:
                self._locals[bname] = (idx, vty)
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
            # Deep-scan-20: re-point the name mapping at THIS binding's
            # (name, type) entry — same name with a different type in a
            # sibling scope must use its own local (see _emit_function).
            bkey = (stmt["name"], stmt["t"])
            if bkey in self._binding_pool:
                self._locals[stmt["name"]] = self._binding_pool[bkey]
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
            # Deep-scan-20 fix (HIGH): compute the br depth at the
            # branch site. The old code recorded fixed depths at the
            # loop header — a `break`/`continue` inside an `if` (an extra
            # open frame) emitted `br 0`/`br 1`, turning continue into
            # "exit the if" and break into a loop back-edge (the loop
            # never terminated). Entries on the loop stack are now
            # ABSOLUTE frame indices (break_frame, continue_frame);
            # depth = frames_open - 1 - target (0 = innermost).
            break_frame, _ = self._loop_stack[-1]
            out.append(OP_BR); out += uleb(self._frames - 1 - break_frame)
        elif k == "continue":
            if not self._loop_stack:
                raise HLError("continue outside loop", stmt.get("line", 0), 0)
            _, cont_frame = self._loop_stack[-1]
            out.append(OP_BR); out += uleb(self._frames - 1 - cont_frame)
        elif k == "expr":
            self._lower_expr(stmt["e"], out)
            # If the expression produced a value, drop it.
            # Deep-scan-20 fix (HIGH): panic()/exit() are typed `never`
            # but their wasm helpers return void — dropping on the empty
            # stack produced "not enough arguments on the stack for drop".
            # Never-typed statements leave nothing to drop.
            t = stmt["e"].get("t", "void")
            if t not in ("void", "never"):
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
        self._frames += 1
        # (Deep-scan-20: the if is an extra wasm frame — the frame
        # counter lets break/continue inside it recompute their br
        # depths correctly.)
        for s in stmt["then"]:
            self._lower_stmt(s, out)
        if stmt.get("els"):
            out.append(OP_ELSE)
            for s in stmt["els"]:
                self._lower_stmt(s, out)
        out.append(OP_END)
        self._frames -= 1

    def _lower_while(self, stmt: Dict, out: bytearray):
        # wasm pattern:
        #   block (void)
        #     loop (void)
        #       cond  i32.eqz  br_if 1   (break out of block)
        #       body
        #       br 0  (loop)
        #     end
        #   end
        # Deep-scan-20: loop-stack entries are ABSOLUTE frame indices;
        # break/continue recompute their br depth at the branch site so
        # intervening `if` frames (or nested loops) stay correct.
        out.append(OP_BLOCK); out.append(BLOCK_VOID)
        out.append(OP_LOOP); out.append(BLOCK_VOID)
        self._frames += 2
        # Deep-scan-20: loop-stack entries are now ABSOLUTE frame
        # indices: the block (break target) opened at _frames-2, the
        # loop (continue target) at _frames-1.
        block_frame = self._frames - 2
        loop_frame = self._frames - 1
        # cond
        self._lower_expr(stmt["cond"], out)
        out.append(OP_I32_EQZ)
        out.append(OP_BR_IF); out += uleb(1)  # break out of block
        # body
        self._loop_stack.append((block_frame, loop_frame))
        for s in stmt["body"]:
            self._lower_stmt(s, out)
        self._loop_stack.pop()
        out.append(OP_BR); out += uleb(0)  # loop back
        out.append(OP_END)  # loop
        out.append(OP_END)  # block
        self._frames -= 2

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
        self._frames += 2
        block_frame = self._frames - 2
        out.append(OP_LOCAL_GET); out += uleb(var_idx)
        out.append(OP_LOCAL_GET); out += uleb(end_idx)
        out.append(OP_I64_GE_S)
        out.append(OP_BR_IF); out += uleb(1)
        # Deep-scan-20 fix (HIGH): wrap the body in its own block so a
        # `continue` branches to the INCREMENT, not the loop header —
        # the old layout (continue = br to the loop frame) skipped
        # `var = var + 1`, so `for i in range(0, 3) { continue }` spun
        # forever. Layout:
        #   block (break target)
        #     loop
        #       (var >= end) br_if 1
        #       block (continue target)
        #         body
        #       end
        #       var = var + 1
        #       br 0
        #     end
        #   end
        out.append(OP_BLOCK); out.append(BLOCK_VOID)
        self._frames += 1
        cont_frame = self._frames - 1
        self._loop_stack.append((block_frame, cont_frame))
        for s in stmt["body"]:
            self._lower_stmt(s, out)
        self._loop_stack.pop()
        out.append(OP_END)  # inner continue-target block
        self._frames -= 1
        out.append(OP_LOCAL_GET); out += uleb(var_idx)
        out.append(OP_I64_CONST); out += sleb(1)
        out.append(OP_I64_ADD)
        out.append(OP_LOCAL_SET); out += uleb(var_idx)
        out.append(OP_BR); out += uleb(0)
        out.append(OP_END)  # loop
        out.append(OP_END)  # block
        self._frames -= 2

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
            self._frames += 1  # deep-scan-20: the if is an open frame
            self._lower_expr(e["r"], out)
            out.append(OP_ELSE)
            out.append(OP_I32_CONST); out += sleb(0)
            out.append(OP_END)
            self._frames -= 1
            return
        if op == "||":
            self._lower_expr(e["l"], out)
            out.append(OP_IF); out.append(I32)
            self._frames += 1  # deep-scan-20: the if is an open frame
            out.append(OP_I32_CONST); out += sleb(1)
            out.append(OP_ELSE)
            self._lower_expr(e["r"], out)
            out.append(OP_END)
            self._frames -= 1
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
                # Deep-scan-25 fix: overflow-checked (SPEC §7) — traps
                # instead of silently wrapping.
                out.append(OP_CALL)
                out += uleb(self.func_index["hl_checked_add"])
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_ADD)
            else:
                raise HLError("'+' on %s not supported by --emit wasm"
                              % lt, e.get("line", 0), 0)
        elif op == "-":
            if _taint_inner(lt) == "int":
                # Deep-scan-25 fix: overflow-checked (SPEC §7).
                out.append(OP_CALL)
                out += uleb(self.func_index["hl_checked_sub"])
            elif _taint_inner(lt) == "float":
                out.append(OP_F64_SUB)
            else:
                raise HLError("'-' on %s not supported" % lt, e.get("line", 0), 0)
        elif op == "*":
            if _taint_inner(lt) == "int":
                # Deep-scan-25 fix: overflow-checked (SPEC §7).
                out.append(OP_CALL)
                out += uleb(self.func_index["hl_checked_mul"])
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
                # Deep-scan-25 fix: INT64_MIN % -1 must halt (HLS
                # overflow panic), which wasm's rem_s defines as 0.
                out.append(OP_CALL)
                out += uleb(self.func_index["hl_checked_rem"])
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
                # Compute 0 - x via the CHECKED sub (deep-scan-25 fix:
                # 0 - INT64_MIN must halt, not wrap to INT64_MIN).
                # Push 0 FIRST (deeper), then x — i64.sub pops b then a
                # and computes a - b; a must be deeper.
                out.append(OP_I64_CONST); out += sleb(0)
                self._lower_expr(e["e"], out)
                out.append(OP_CALL)
                out += uleb(self.func_index["hl_checked_sub"])
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
            # Deep-scan-20 fix (HIGH): the checker allows `fn main()` with
            # NO return type (void) — an unconditional OP_DROP after the
            # call left the validation error "not enough arguments on the
            # stack for drop". Only drop when main actually returns int.
            if self.program["fns"]["main"]["ret"] != "void":
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
            # Deep-scan-15 fix: the previous format string had NO `%s`
            # placeholder but applied `% e.get("name", "")` — at runtime
            # this raised `TypeError: not enough arguments for format
            # string` (Python) instead of the clean HLError that callers
            # expect. Add the `%s` so the offending method name is named
            # in the message (which is the whole point of fetching it).
            raise HLError(
                "user-defined method '%s' is not yet supported by --emit wasm "
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
JS_GLUE_COMPACT = r"""/* Halis wasm32 glue (Stage 73, v0.92.0-alpha) -- compact build. */
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
else if(f.type==="str"){/* Stage 73 fix: the field holds a POINTER
to {i32 len, bytes} — dereference it before reading the string
(the Stage 24 version read the string AT the field address). */
var fp=dv.getInt32(v,true);o[f.name]=rs(m,fp);}
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
H.instantiate=async function(wb,ov){var inst;var env={hl_js_println:function(p){console.log(rs(inst.exports.memory,p));},
hl_js_print:function(p){var s=rs(inst.exports.memory,p);
if(typeof process!=="undefined"&&process.stdout)process.stdout.write(s);
else if(typeof document!=="undefined"){var o=document.getElementById("halis-out");
if(o)o.appendChild(document.createTextNode(s));}else console.log(s);},
hl_js_f64_to_str:function(f){var s=String(f);
if(s.indexOf(".")<0&&s.indexOf("e")<0&&s.indexOf("i")<0&&s.indexOf("N")<0)s+=".0";
return ws(inst.exports.memory,inst.exports.hl_alloc,s);},
/* std.jsffi defaults — injected per-import by generate_js_glue(). */
__EXTRA_ENV_STUBS__};
if(ov)for(var k in ov)env[k]=ov[k];
var mod;if(wb instanceof WebAssembly.Module)mod=wb;
else if(typeof wb==="string"){var r=await fetch(wb);var b=await r.arrayBuffer();
mod=await WebAssembly.compile(b);}
else if(wb instanceof ArrayBuffer||wb instanceof Uint8Array)mod=await WebAssembly.compile(wb);
else throw new Error("Halis.instantiate: expected bytes/URL");
inst=await WebAssembly.instantiate(mod,{env:env});H._mem=inst.exports.memory;H._inst=inst;
/* Stage 73: AUTO-REGISTER struct descriptors. If the module exports
hl_struct_descriptors(), every struct in the Halis program is
registered for marshalling with zero manual setup — "a Halis struct
becomes a JS object". */
if(typeof inst.exports.hl_struct_descriptors==="function"){
try{var d=JSON.parse(rs(inst.exports.memory,inst.exports.hl_struct_descriptors()));
for(var di=0;di<d.length;di++)H.registerStruct(d[di].name,d[di].fields);}catch(e){}}
return{instance:inst,module:mod};};
H.callHalis=async function(cbId,argJson){
if(!H._inst||typeof H._inst.exports.hl_call_halis!=="function")
throw new Error("hl_call_halis export missing: define fn jsffi_on_callback(cb_id: int, arg: str) -> str in the Halis program");
var m=H._inst.exports.memory;
var ap=ws(m,H._inst.exports.hl_alloc,String(argJson));
return rs(m,H._inst.exports.hl_call_halis(cbId,ap));};
H.run=async function(wb,ov){var r=await H.instantiate(wb,ov);
if(typeof r.instance.exports.hl_main==="function")return r.instance.exports.hl_main();
if(typeof r.instance.exports._start==="function")r.instance.exports._start();return 0;};
})(typeof globalThis!=="undefined"?globalThis:(typeof window!=="undefined"?window:global));
"""

# JS_GLUE_COMPACT_STUBS — the per-import default stubs removed from the
# compact template above (deep-scan-23 tree-shaking). generate_js_glue()
# re-injects ONLY the stubs the compiled module actually imports, so a
# program that never uses std.jsffi gets a ~3 KB glue instead of an
# 11 KB one; jsffi-heavy programs grow only as needed.
JS_GLUE_COMPACT_STUBS = {
    "js_console_log": r"""function(p){console.log(rs(inst.exports.memory,p));}""",
    "js_console_warn": r"""function(p){console.warn(rs(inst.exports.memory,p));}""",
    "js_console_error": r"""function(p){console.error(rs(inst.exports.memory,p));}""",
    "js_dom_set_text": r"""function(i,t){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));if(e)e.textContent=rs(m,t);}""",
    "js_dom_append": r"""function(i,h){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));if(e)e.insertAdjacentHTML("beforeend",rs(m,h));}""",
    "js_random": r"""function(){return Math.random();}""",
    "js_random_int": r"""function(mx){return BigInt(Math.floor(Math.random()*Number(mx)));}""",
    "js_fetch": r"""function(u){throw new Error("js_fetch: override via importOverrides");}""",
    "js_localstorage_get": r"""function(k){if(typeof localStorage==="undefined")return 0;
var m=inst.exports.memory;return ws(m,inst.exports.hl_alloc,localStorage.getItem(rs(m,k))||"");}""",
    "js_localstorage_set": r"""function(k,v){if(typeof localStorage==="undefined")return;
var m=inst.exports.memory;localStorage.setItem(rs(m,k),rs(m,v));}""",
    "js_now_ms": r"""function(){return BigInt(Date.now());}""",
    "js_set_timeout": r"""function(ms){return BigInt(0);}""",
    "js_struct_to_json": r"""function(p,n){var m=inst.exports.memory;var nm=rs(m,n);
if(H.structs&&H.structs[nm])return ws(m,inst.exports.hl_alloc,JSON.stringify(H.readStruct(p,nm)));
return ws(m,inst.exports.hl_alloc,"{}");}""",
    "js_json_to_struct": r"""function(j,n){var m=inst.exports.memory;var nm=rs(m,n);
if(H.structs&&H.structs[nm])try{return H.writeStruct(inst.exports.hl_alloc,JSON.parse(rs(m,j)),nm);}catch(e){}
return 0;}""",
    "js_call_with_struct": r"""function(f,p,n){return 0;}""",
    "js_console_table": r"""function(m){console.table(rs(inst.exports.memory,m));}""",
    "js_console_clear": r"""function(){console.clear();}""",
    "js_console_count": r"""function(l){var k=rs(inst.exports.memory,l);
H._counts=H._counts||{};H._counts[k]=(H._counts[k]||0)+1;
console.log(k+": "+H._counts[k]);return BigInt(H._counts[k]);}""",
    "js_console_count_reset": r"""function(l){var k=rs(inst.exports.memory,l);
if(H._counts)delete H._counts[k];}""",
    "js_console_group": r"""function(l){console.group(rs(inst.exports.memory,l));}""",
    "js_console_group_end": r"""function(){console.groupEnd();}""",
    "js_console_time": r"""function(l){H._timers=H._timers||{};
H._timers[rs(inst.exports.memory,l)]=Date.now();}""",
    "js_console_time_end": r"""function(l){var k=rs(inst.exports.memory,l);
H._timers=H._timers||{};var t0=H._timers[k]||Date.now();delete H._timers[k];
var ms=Date.now()-t0;console.log(k+": "+ms+"ms");return BigInt(ms);}""",
    "js_dom_set_attr": r"""function(i,n,v){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));
if(e)e.setAttribute(rs(m,n),rs(m,v));}""",
    "js_dom_get_attr": r"""function(i,n){var m=inst.exports.memory;
if(typeof document==="undefined")return ws(m,inst.exports.hl_alloc,"");
var e=document.getElementById(rs(m,i));
return ws(m,inst.exports.hl_alloc,e?String(e.getAttribute(rs(m,n))||""):"");}""",
    "js_dom_remove": r"""function(i){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));
if(e)e.remove();}""",
    "js_dom_add_class": r"""function(i,c){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));
if(e)e.classList.add(rs(m,c));}""",
    "js_dom_remove_class": r"""function(i,c){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));
if(e)e.classList.remove(rs(m,c));}""",
    "js_dom_toggle_class": r"""function(i,c){if(typeof document==="undefined")return false;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));
if(!e)return false;return e.classList.toggle(rs(m,c));}""",
    "js_dom_set_style": r"""function(i,p,v){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));
if(e)e.style.setProperty(rs(m,p),rs(m,v));}""",
    "js_dom_get_value": r"""function(i){var m=inst.exports.memory;
if(typeof document==="undefined")return ws(m,inst.exports.hl_alloc,"");
var e=document.getElementById(rs(m,i));
return ws(m,inst.exports.hl_alloc,e?String(e.value||""):"");}""",
    "js_dom_set_value": r"""function(i,v){if(typeof document==="undefined")return;
var m=inst.exports.memory;var e=document.getElementById(rs(m,i));
if(e)e.value=rs(m,v);}""",
    "js_dom_document_title": r"""function(){var m=inst.exports.memory;
return ws(m,inst.exports.hl_alloc,typeof document==="undefined"?"":String(document.title||""));}""",
    "js_dom_set_title": r"""function(t){if(typeof document==="undefined")return;
var m=inst.exports.memory;document.title=rs(m,t);}""",
    "js_json_canonical": r"""function(s){var m=inst.exports.memory;
try{return ws(m,inst.exports.hl_alloc,JSON.stringify(JSON.parse(rs(m,s))));}
catch(e){return ws(m,inst.exports.hl_alloc,"null");}}""",
    "js_json_valid": r"""function(s){var m=inst.exports.memory;
try{JSON.parse(rs(m,s));return true;}catch(e){return false;}}""",
    "js_url_encode": r"""function(s){var m=inst.exports.memory;
return ws(m,inst.exports.hl_alloc,encodeURIComponent(rs(m,s)));}""",
    "js_url_decode": r"""function(s){var m=inst.exports.memory;
try{return ws(m,inst.exports.hl_alloc,decodeURIComponent(rs(m,s)));}
catch(e){return ws(m,inst.exports.hl_alloc,"");}}""",
    "js_fetch_with_options": r"""function(u,o){throw new Error("js_fetch_with_options: override via importOverrides");}""",
    "js_localstorage_remove": r"""function(k){if(typeof localStorage==="undefined")return;
var m=inst.exports.memory;localStorage.removeItem(rs(m,k));}""",
    "js_localstorage_clear": r"""function(){if(typeof localStorage!=="undefined")localStorage.clear();}""",
    "js_localstorage_key_count": r"""function(){if(typeof localStorage==="undefined")return BigInt(0);
return BigInt(localStorage.length);}""",
    "js_platform_name": r"""function(){var m=inst.exports.memory;
var n=typeof navigator!=="undefined"?String(navigator.platform||"node"):"unknown";
return ws(m,inst.exports.hl_alloc,n);}""",
    "js_language": r"""function(){var m=inst.exports.memory;
var n=typeof navigator!=="undefined"?String(navigator.language||"en"):"en";
return ws(m,inst.exports.hl_alloc,n);}""",
    "js_online": r"""function(){return typeof navigator!=="undefined"?navigator.onLine!==false:true;}""",
    "js_user_agent": r"""function(){var m=inst.exports.memory;
var n=typeof navigator!=="undefined"?String(navigator.userAgent||""):"node";
return ws(m,inst.exports.hl_alloc,n);}""",
    "js_screen_width": r"""function(){if(typeof screen==="undefined")return BigInt(0);
return BigInt(screen.width|0);}""",
    "js_screen_height": r"""function(){if(typeof screen==="undefined")return BigInt(0);
return BigInt(screen.height|0);}""",
    "js_alert": r"""function(m){if(typeof alert!=="undefined")alert(rs(inst.exports.memory,m));}""",
    "js_performance_now": r"""function(){return (typeof performance!=="undefined"?performance.now():Date.now());}""",
    "js_date_now_iso": r"""function(){var m=inst.exports.memory;
return ws(m,inst.exports.hl_alloc,new Date().toISOString());}""",
}




__all__ = [
    "HTML_RUNNER",
    "JS_GLUE",
    "JS_GLUE_COMPACT",
    "JS_GLUE_COMPACT_STUBS",
]
