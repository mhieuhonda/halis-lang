"""emit_calls - verbatim segment of the original tools/llvm_emit.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from llvm_common import (
    Dict, List, Tuple, _is_list, _is_map, _list_elem, _map_val, _taint_inner,
    _unsupported, hls_type_to_llvm,
)

class LLVMEmitterCalls(object):
    def _lower_call_typed(self, e: Dict) -> Tuple[str, str]:
        """Lower a function call. Builtin calls map to the REAL runtime
        functions (same dispatch as the C backend's gen_call)."""
        name = e["name"]
        rc = e.get("rc")  # ("builtin", name) or ("user", name) — set by the checker
        # BUG (deep-scan-5): a never-typed argument (e.g. panic()) in a
        # non-final position closed the block; the remaining args were
        # still lowered, emitting instructions AFTER the terminator.
        # Stop lowering as soon as the block closes.
        arg_pairs = []
        for a in e["args"]:
            if self._cur_block is None:
                break
            arg_pairs.append(self._lower_expr_typed(a))
        if self._cur_block is None:
            return ("void", "0")

        # ---- builtins ----
        if rc is None or rc[0] == "builtin":
            return self._lower_builtin_call(name, e, arg_pairs)

        # ---- user-defined function call ----
        fn = self.program["fns"].get(name)
        ret_ty = hls_type_to_llvm(fn["ret"]) if fn else "ptr"
        param_tys = [hls_type_to_llvm(p[1]) for p in fn["params"]] if fn else (
            ["i64"] * len(arg_pairs))
        arg_str = ", ".join(
            "%s %s" % (pt, self._coerce(aty, aval, pt))
            for (aty, aval), pt in zip(arg_pairs, param_tys, strict=True))
        if ret_ty == "void":
            self._emit("  call void @%s(%s)" % (name, arg_str))
            return ("void", "0")
        tmp = self._fresh("call")
        self._emit("  %s = call %s @%s(%s)" % (tmp, ret_ty, name, arg_str))
        return (ret_ty, tmp)

    def _call1(self, ret_ty: str, sym: str, arg_tys: List[str],
               arg_pairs: List[Tuple[str, str]]) -> Tuple[str, str]:
        """Helper: emit `call ret_ty @sym(coerced args)` and return the
        (type, value) pair."""
        args = []
        for (aty, aval), want in zip(arg_pairs, arg_tys, strict=True):
            args.append("%s %s" % (want, self._coerce(aty, aval, want)))
        arg_str = ", ".join(args)
        if ret_ty == "void":
            self._emit("  call void %s(%s)" % (sym, arg_str))
            return ("void", "0")
        tmp = self._fresh("call")
        self._emit("  %s = call %s %s(%s)" % (tmp, ret_ty, sym, arg_str))
        return (ret_ty, tmp)

    def _lower_builtin_call(self, name, e, arg_pairs) -> Tuple[str, str]:
        # Dispatch table mirroring the C backend's gen_call (hlc.hls).
        if name in ("print", "println"):
            return self._call1("void", "@hl_print" if name == "print" else "@hl_println",
                               ["ptr"], arg_pairs)
        # ----- Stage 56 (v0.75.0-alpha): stderr + TTY-detection builtins.
        # eprint / eprintln consume their str argument (the same
        # lowering as print / println — _call1's call-void path passes
        # the raw pointer). isatty takes a primitive i64 and returns i1.
        if name in ("eprint", "eprintln"):
            return self._call1("void", "@hl_eprint" if name == "eprint" else "@hl_eprintln",
                               ["ptr"], arg_pairs)
        if name == "isatty":
            return self._call1("i1", "@hl_isatty", ["i64"], arg_pairs)
        # ----- Stage 57 (v0.76.0-alpha): process-identity builtins.
        # Both take no arguments (the empty arg_pairs call form).
        if name == "proc_pid":
            tmp = self._fresh("pid")
            self._emit("  %s = call i64 @hl_proc_pid()" % tmp)
            return ("i64", tmp)
        if name == "sys_hostname":
            tmp = self._fresh("hostname")
            self._emit("  %s = call ptr @hl_sys_hostname()" % tmp)
            return ("ptr", tmp)
        if name == "panic":
            # hl_panic never returns: emit call + unreachable so no
            # instructions follow in this block.
            self._call1("void", "@hl_panic", ["ptr"], arg_pairs)
            self._emit("  unreachable")
            return ("void", "0")
        if name == "exit":
            self._call1("void", "@hl_exit", ["i64"], arg_pairs)
            self._emit("  unreachable")
            return ("void", "0")
        if name == "str":
            at = e["args"][0].get("t", "int") if e["args"] else "int"
            if at == "int":
                return self._call1("ptr", "@hl_str_from_int64", ["i64"], arg_pairs)
            if at == "float":
                return self._call1("ptr", "@hl_str_from_double", ["double"], arg_pairs)
            if at == "bool":
                return self._call1("ptr", "@hl_str_from_bool", ["i1"], arg_pairs)
            return arg_pairs[0] if arg_pairs else ("ptr", "null")
        if name == "int":
            return self._call1("i64", "@hl_str_to_int", ["ptr"], arg_pairs)
        if name == "len":
            at = e["args"][0].get("t", "str") if e["args"] else "str"
            if at == "str":
                return self._call1("i64", "@hl_str_len", ["ptr"], arg_pairs)
            if _is_list(at):
                return self._call1("i64", "@hl_list_len", ["ptr"], arg_pairs)
            return self._call1("i64", "@hl_map_len", ["ptr"], arg_pairs)
        if name == "range":
            return self._call1("ptr", "@hl_range", ["i64", "i64"], arg_pairs)
        # Stage 19 (v0.35.0-alpha): O(n) join(list[str], sep) -> str.
        if name == "join":
            return self._call1("ptr", "@hl_str_join", ["ptr", "ptr"], arg_pairs)
        # Stage 21 (v0.37.0-alpha): has_feature — const-folded from the
        # --target-feature flag (boot.py annotates the program dict).
        if name == "has_feature":
            lit = e["args"][0].get("v", "") if e["args"] else ""
            if isinstance(lit, bytes):
                lit = lit.decode("utf-8", "replace")
            feat = self.program.get("target_feature") or ""
            return ("i1", "1" if lit == feat else "0")
        # Stage 21: simd_cpu_supports — runtime CPU probe.
        if name == "simd_cpu_supports":
            return self._call1("i1", "@hl_simd_cpu_supports", ["ptr"], arg_pairs)
        if name == "map_new":
            tmp = self._fresh("map")
            # Arena-mode contract: val_free = null (never release values).
            self._emit("  %s = call ptr @hl_map_new(ptr null)" % tmp)
            return ("ptr", tmp)
        if name == "read_file":
            return self._call1("ptr", "@hl_read_file", ["ptr"], arg_pairs)
        if name == "read_file_tainted":
            # Taint is compile-time only (same as the C backend).
            return self._call1("ptr", "@hl_read_file", ["ptr"], arg_pairs)
        if name == "write_file":
            return self._call1("void", "@hl_write_file", ["ptr", "ptr"], arg_pairs)
        if name == "args":
            tmp = self._fresh("call")
            self._emit("  %s = call ptr @hl_args()" % tmp)
            return ("ptr", tmp)
        if name == "tainted_args":
            # Runtime: same as args() (taint is compile-time only).
            tmp = self._fresh("call")
            self._emit("  %s = call ptr @hl_args()" % tmp)
            return ("ptr", tmp)
        if name == "taint_mark":
            # Identity at runtime.
            return arg_pairs[0]
        if name == "taint_unwrap":
            # Identity at runtime.
            return arg_pairs[0]
        if name == "chr":
            return self._call1("ptr", "@hl_chr", ["i64"], arg_pairs)
        if name == "clock_ms":
            tmp = self._fresh("call")
            self._emit("  %s = call i64 @hl_clock_ms()" % tmp)
            return ("i64", tmp)
        # Stage 46 (v0.65.0-alpha): thread / scheduling builtins.
        # thread_sleep_ms(ms) -> void: blocking sleep.
        if name == "thread_sleep_ms":
            return self._call1("void", "@hl_thread_sleep_ms", ["i64"], arg_pairs)
        # thread_yield() -> void: scheduler hint.
        if name == "thread_yield":
            return self._call1("void", "@hl_thread_yield", [], arg_pairs)
        # thread_current_id() -> int: non-zero thread identifier.
        if name == "thread_current_id":
            tmp = self._fresh("tid")
            self._emit("  %s = call i64 @hl_thread_current_id()" % tmp)
            return ("i64", tmp)
        if name == "file_exists":
            return self._call1("i1", "@hl_file_exists", ["ptr"], arg_pairs)
        # Stage 36 (v0.55.0-alpha): filesystem metadata builtins.
        # All consume the path argument; fs_set_perms also takes a mode int.
        if name == "fs_read_dir":
            return self._call1("ptr", "@hl_fs_read_dir", ["ptr"], arg_pairs)
        if name == "fs_size":
            return self._call1("i64", "@hl_fs_size", ["ptr"], arg_pairs)
        if name == "fs_is_dir":
            return self._call1("i1", "@hl_fs_is_dir", ["ptr"], arg_pairs)
        if name == "fs_set_perms":
            return self._call1("void", "@hl_fs_set_perms", ["ptr", "i64"],
                              arg_pairs)
        # Stage 37 (v0.56.0-alpha): TCP / UDP / TLS builtins. The host /
        # path / data arguments are owned ptr (one retain released inside
        # the helper); the fd / port / n / backlog arguments are i64.
        # net_read / net_udp_recv_from / net_tls_get return a fresh str.
        if name == "net_tcp_connect":
            return self._call1("i64", "@hl_net_tcp_connect",
                               ["ptr", "i64"], arg_pairs)
        if name == "net_tcp_listen":
            return self._call1("i64", "@hl_net_tcp_listen",
                               ["ptr", "i64", "i64"], arg_pairs)
        if name == "net_tcp_accept":
            return self._call1("i64", "@hl_net_tcp_accept", ["i64"],
                              arg_pairs)
        if name == "net_read":
            return self._call1("ptr", "@hl_net_read", ["i64", "i64"],
                              arg_pairs)
        if name == "net_write":
            return self._call1("i64", "@hl_net_write", ["i64", "ptr"],
                              arg_pairs)
        if name == "net_close":
            return self._call1("void", "@hl_net_close", ["i64"], arg_pairs)
        if name == "net_udp_open":
            tmp = self._fresh("udp_open")
            self._emit("  %s = call i64 @hl_net_udp_open()" % tmp)
            return ("i64", tmp)
        if name == "net_udp_send_to":
            return self._call1("i64", "@hl_net_udp_send_to",
                               ["i64", "ptr", "i64", "ptr"], arg_pairs)
        if name == "net_udp_recv_from":
            return self._call1("ptr", "@hl_net_udp_recv_from",
                               ["i64", "i64"], arg_pairs)
        if name == "net_tls_get":
            return self._call1("ptr", "@hl_net_tls_get",
                               ["ptr", "i64", "ptr"], arg_pairs)
        if name == "drop":
            # Compile-time annotation only; runtime no-op.
            return ("void", "0")
        if name == "take":
            # Compile-time move; runtime returns the value itself.
            return arg_pairs[0]
        if name == "clone":
            at = e["args"][0].get("t", "str") if e["args"] else "str"
            if at == "str":
                return self._call1("ptr", "@hl_clone_str", ["ptr"], arg_pairs)
            if _is_list(at):
                elem = _list_elem(at)
                suffix = {"int": "int", "float": "float", "bool": "bool",
                          "str": "str"}.get(elem)
                if suffix:
                    return self._call1("ptr", "@hl_clone_list_%s" % suffix,
                                       ["ptr"], arg_pairs)
                _unsupported("clone() on list[%s]" % elem, e)
            if _is_map(at):
                vt = _map_val(at)
                suffix = {"int": "int", "float": "float", "bool": "bool",
                          "str": "str"}.get(vt)
                if suffix:
                    return self._call1("ptr", "@hl_clone_map_%s" % suffix,
                                       ["ptr"], arg_pairs)
                _unsupported("clone() on map[str, %s]" % vt, e)
            _unsupported("clone() on type %s" % at, e)
        # ----- Stage 9 release: proc_exec + Stage 47 (v0.66.0-alpha):
        # process management builtins. proc_exec consumes its cmd
        # (str); proc_spawn consumes program (arg 0) and args (arg 1);
        # proc_child_write consumes data (arg 1). The other builtins
        # take only int arguments.
        if name == "proc_exec":
            return self._call1("i64", "@hl_proc_exec", ["ptr"], arg_pairs)
        if name == "proc_spawn":
            return self._call1("i64", "@hl_proc_spawn",
                               ["ptr", "ptr", "i64", "i64", "i64"], arg_pairs)
        if name == "proc_wait":
            return self._call1("i64", "@hl_proc_wait", ["i64"], arg_pairs)
        if name == "proc_kill":
            return self._call1("i64", "@hl_proc_kill", ["i64"], arg_pairs)
        if name == "proc_child_write":
            return self._call1("i64", "@hl_proc_child_write",
                               ["i64", "ptr"], arg_pairs)
        if name == "proc_child_read":
            return self._call1("ptr", "@hl_proc_child_read",
                               ["i64", "i64", "i64"], arg_pairs)
        if name == "proc_child_close":
            return self._call1("i64", "@hl_proc_child_close",
                               ["i64", "i64"], arg_pairs)
        # ----- Stage 48 (v0.67.0-alpha): environment + cwd builtins.
        # env_get / env_has / env_unset / cwd_set consume their str
        # argument; env_set consumes both; cwd_get / args_os take no
        # arguments.
        if name == "env_get":
            return self._call1("ptr", "@hl_env_get", ["ptr"], arg_pairs)
        if name == "env_has":
            return self._call1("i1", "@hl_env_has", ["ptr"], arg_pairs)
        if name == "env_set":
            return self._call1("void", "@hl_env_set", ["ptr", "ptr"],
                              arg_pairs)
        if name == "env_unset":
            return self._call1("void", "@hl_env_unset", ["ptr"], arg_pairs)
        if name == "cwd_get":
            tmp = self._fresh("cwd")
            self._emit("  %s = call ptr @hl_cwd_get()" % tmp)
            return ("ptr", tmp)
        if name == "cwd_set":
            return self._call1("i64", "@hl_cwd_set", ["ptr"], arg_pairs)
        if name == "args_os":
            # Same as args() at the LLVM level; the stdlib env_args_os
            # applies taint_mark per element.
            tmp = self._fresh("args_os")
            self._emit("  %s = call ptr @hl_args()" % tmp)
            return ("ptr", tmp)
        # ----- Stage 49 (v0.68.0-alpha): high-resolution time builtins.
        # Both take no args and return i64.
        if name == "instant_now_ns":
            tmp = self._fresh("instant")
            self._emit("  %s = call i64 @hl_instant_now_ns()" % tmp)
            return ("i64", tmp)
        if name == "system_time_now_ms":
            tmp = self._fresh("systime")
            self._emit("  %s = call i64 @hl_system_time_now_ms()" % tmp)
            return ("i64", tmp)
        # ----- Stage 50 (v0.69.0-alpha): libm-backed math builtins -----
        # Single-arg float -> float.
        if name in ("math_sin", "math_cos", "math_tan",
                    "math_asin", "math_acos", "math_atan",
                    "math_sinh", "math_cosh", "math_tanh",
                    "math_exp", "math_log", "math_log10", "math_log2",
                    "math_sqrt", "math_cbrt", "math_erf", "math_erfc",
                    "math_tgamma", "math_lgamma"):
            return self._call1("double", "@hl_" + name, ["double"], arg_pairs)
        # Two-arg float -> float.
        if name in ("math_atan2", "math_pow", "math_hypot",
                    "math_fmod", "math_copysign"):
            return self._call1("double", "@hl_" + name,
                               ["double", "double"], arg_pairs)
        # IEEE-754 predicates: float -> bool.
        if name in ("math_isnan", "math_isinf", "math_isfinite",
                    "math_signbit"):
            return self._call1("i1", "@hl_" + name, ["double"], arg_pairs)
        _unsupported("builtin function '%s'" % name, e)

    # ---------- method calls ----------
    def _lower_method_typed(self, e: Dict) -> Tuple[str, str]:
        """Lower a method call using the checker's `rm` annotation
        (("builtin", op) or ("user", key)). BUILTIN methods map 1:1 to the
        runtime functions the C backend uses (gen_method in hlc.hls);
        USER-defined struct methods are not yet supported (clean error)."""
        rm = e.get("rm")
        if rm is None or rm[0] != "builtin":
            _unsupported("call to user-defined method '%s'" % e.get("name"), e)
        op = rm[1]
        recv_ty, recv = self._lower_expr_typed(e["target"])
        if self._cur_block is None:
            return ("i64", "0")
        # BUG (deep-scan-5): stop lowering args once a never-typed
        # argument closes the block (see _lower_call_typed).
        arg_pairs = []
        for a in e["args"]:
            if self._cur_block is None:
                break
            arg_pairs.append(self._lower_expr_typed(a))
        if self._cur_block is None:
            return ("i64", "0")
        recv_t = e["target"].get("t", "int")

        # ---- str.* ----
        if op.startswith("str."):
            table = {
                "str.len":         ("i64", "@hl_str_len", ["ptr"]),
                "str.byte_at":     ("i64", "@hl_str_byte_at", ["ptr", "i64"]),
                "str.slice":       ("ptr", "@hl_str_slice", ["ptr", "i64", "i64"]),
                "str.find":        ("i64", "@hl_str_find", ["ptr", "ptr"]),
                "str.contains":    ("i1", "@hl_str_contains", ["ptr", "ptr"]),
                "str.starts_with": ("i1", "@hl_str_starts_with", ["ptr", "ptr"]),
                "str.ends_with":   ("i1", "@hl_str_ends_with", ["ptr", "ptr"]),
                "str.split":       ("ptr", "@hl_str_split", ["ptr", "ptr"]),
                "str.trim":        ("ptr", "@hl_str_trim", ["ptr"]),
                "str.to_int":      ("i64", "@hl_str_to_int", ["ptr"]),
                "str.to_float":    ("double", "@hl_str_to_double", ["ptr"]),
                "str.to_str":      (None, None, ["ptr"]),
            }
            entry = table.get(op)
            if entry is None:
                _unsupported("method '%s'" % op, e)
            ret_ty, sym, arg_tys = entry
            if op == "str.to_str":
                return (recv_ty, recv)
            full = [("ptr", recv)] + list(arg_pairs)
            return self._call1(ret_ty, sym, arg_tys, full)

        # ---- int.* ----
        if op.startswith("int."):
            if op == "int.to_str":
                return self._call1("ptr", "@hl_str_from_int64", ["i64"],
                                   [("i64", recv)] + arg_pairs)
            if op == "int.to_float":
                tmp = self._fresh("s2d")
                self._emit("  %s = sitofp i64 %s to double" % (tmp, recv))
                return ("double", tmp)
            if op == "int.abs":
                return self._call1("i64", "@hl_abs_i64", ["i64"],
                                   [("i64", recv)] + arg_pairs)
            _unsupported("method '%s'" % op, e)

        # ---- float.* ----
        if op.startswith("float."):
            if op == "float.to_str":
                return self._call1("ptr", "@hl_str_from_double", ["double"],
                                   [("double", recv)] + arg_pairs)
            if op == "float.to_int":
                return self._call1("i64", "@hl_float_to_int", ["double"],
                                   [("double", recv)] + arg_pairs)
            if op == "float.abs":
                tmp = self._fresh("fa")
                self._emit("  %s = call double @llvm.fabs.f64(double %s)" % (tmp, recv))
                return ("double", tmp)
            _unsupported("method '%s'" % op, e)

        # ---- bool.* ----
        if op.startswith("bool."):
            if op == "bool.to_str":
                return self._call1("ptr", "@hl_str_from_bool", ["i1"],
                                   [("i1", recv)] + arg_pairs)
            _unsupported("method '%s'" % op, e)

        # ---- list.* (boxed element ABI) ----
        if op.startswith("list."):
            elem_t = _list_elem(recv_t) if _is_list(recv_t) else "int"
            if op == "list.len":
                return self._call1("i64", "@hl_list_len", ["ptr"], [("ptr", recv)])
            if op == "list.push":
                # Box the pushed value by the element type.
                vty, v = arg_pairs[0]
                boxed = self._box_value(elem_t, vty, v)
                self._emit("  call void @hl_list_push(ptr %s, ptr %s)" % (recv, boxed))
                return ("void", "0")
            if op == "list.get":
                tmp = self._fresh("e")
                self._emit("  %s = call ptr @hl_list_get(ptr %s, i64 %s)" % (
                    tmp, recv, self._coerce(arg_pairs[0][0], arg_pairs[0][1], "i64")))
                ret_t = e.get("t", elem_t)
                unboxed = self._unbox_value(ret_t, tmp)
                return (hls_type_to_llvm(ret_t), unboxed)
            if op == "list.set":
                idx = self._coerce(arg_pairs[0][0], arg_pairs[0][1], "i64")
                vty, v = arg_pairs[1]
                boxed = self._box_value(elem_t, vty, v)
                self._emit("  call void @hl_list_set(ptr %s, i64 %s, ptr %s)" % (
                    recv, idx, boxed))
                return ("void", "0")
            if op == "list.pop":
                # SCAN-A fix: strip `tainted[...]` from the element type
                # before dispatching — `list[tainted[int]].pop()` was
                # falling through to the generic `hl_list_pop` (returns
                # ptr) instead of `hl_list_pop_i64` (returns i64 + frees
                # the box), producing invalid IR.
                base_elem_t = _taint_inner(elem_t) if elem_t.startswith("tainted[") else elem_t
                if base_elem_t == "int":
                    tmp = self._fresh("pop")
                    self._emit("  %s = call i64 @hl_list_pop_i64(ptr %s)" % (tmp, recv))
                    return ("i64", tmp)
                if base_elem_t == "float":
                    tmp = self._fresh("pop")
                    self._emit("  %s = call double @hl_list_pop_f64(ptr %s)" % (tmp, recv))
                    return ("double", tmp)
                if base_elem_t == "bool":
                    tmp = self._fresh("pop")
                    self._emit("  %s = call i1 @hl_list_pop_bool(ptr %s)" % (tmp, recv))
                    return ("i1", tmp)
                tmp = self._fresh("e")
                self._emit("  %s = call ptr @hl_list_pop(ptr %s)" % (tmp, recv))
                ret_t = e.get("t", elem_t)
                return (hls_type_to_llvm(ret_t), tmp)
            _unsupported("method '%s'" % op, e)

        # ---- map.* (boxed value ABI) ----
        if op.startswith("map."):
            val_t = _map_val(recv_t) if _is_map(recv_t) else "int"
            if op == "map.len":
                return self._call1("i64", "@hl_map_len", ["ptr"], [("ptr", recv)])
            if op == "map.set":
                k = self._coerce(arg_pairs[0][0], arg_pairs[0][1], "ptr")
                vty, v = arg_pairs[1]
                boxed = self._box_value(val_t, vty, v)
                self._emit("  call void @hl_map_set(ptr %s, ptr %s, ptr %s)" % (
                    recv, k, boxed))
                return ("void", "0")
            if op == "map.get_or":
                # BUG (deep-scan-5): the old code called hl_map_get(m, k,
                # boxed-default) — a runtime symbol that no longer exists
                # (the C runtime now has typed getters with defaults passed
                # BY VALUE). Dispatch to the typed getter, mirroring the C
                # backend. (The LLVM backend is arena-mode, so pointer-
                # valued maps use the typed getters' borrow semantics —
                # hl_map_get_own is refcount-specific.)
                k = self._coerce(arg_pairs[0][0], arg_pairs[0][1], "ptr")
                vty, d = arg_pairs[1]
                if val_t == "int":
                    dv = self._coerce(vty, d, "i64")
                    tmp = self._fresh("gv")
                    self._emit("  %s = call i64 @hl_map_get_i64(ptr %s, ptr %s, i64 %s)" % (
                        tmp, recv, k, dv))
                    return ("i64", tmp)
                if val_t == "float":
                    dv = self._coerce(vty, d, "double")
                    tmp = self._fresh("gv")
                    self._emit("  %s = call double @hl_map_get_f64(ptr %s, ptr %s, double %s)" % (
                        tmp, recv, k, dv))
                    return ("double", tmp)
                if val_t == "bool":
                    dv = self._coerce(vty, d, "i1")
                    tmp = self._fresh("gv")
                    self._emit("  %s = call i1 @hl_map_get_bool(ptr %s, ptr %s, i1 %s)" % (
                        tmp, recv, k, dv))
                    return ("i1", tmp)
                # Pointer-valued map: use the generic borrow lookup via
                # hl_map_has + hl_map_get_own is refcount-specific; the
                # arena contract lets us reuse hl_map_borrow.
                tmp = self._fresh("gv")
                self._emit("  %s = call ptr @hl_map_borrow(ptr %s, ptr %s)" % (tmp, recv, k))
                ret_t = e.get("t", val_t)
                cmp_tmp = self._fresh("gvnull")
                self._emit("  %s = icmp ne ptr %s, null" % (cmp_tmp, tmp))
                d2 = self._coerce(vty, d, "ptr")
                sel = self._fresh("gvsel")
                self._emit("  %s = select i1 %s, ptr %s, ptr %s" % (sel, cmp_tmp, tmp, d2))
                return (hls_type_to_llvm(ret_t), sel)
            if op == "map.has":
                k = self._coerce(arg_pairs[0][0], arg_pairs[0][1], "ptr")
                tmp = self._fresh("has")
                self._emit("  %s = call i1 @hl_map_has(ptr %s, ptr %s)" % (tmp, recv, k))
                return ("i1", tmp)
            if op == "map.keys":
                return self._call1("ptr", "@hl_map_keys", ["ptr"], [("ptr", recv)])
            _unsupported("method '%s'" % op, e)

        _unsupported("method '%s'" % op, e)

    # ---------- struct field access ----------
    def _lower_field_access_typed(self, e: Dict) -> Tuple[str, str]:
        """Stage 12 release: lower `expr.field` via the typed
        `hl_struct_get_*` runtime helpers. The checker annotates the
        field's declaration index as `field_idx` and the field's HLS
        type as `t`."""
        target_ty, target_val = self._lower_expr_typed(e["target"])
        if self._cur_block is None:
            return ("i64", "0")
        fidx = e.get("field_idx")
        if fidx is None:
            _unsupported("struct field access (checker did not annotate field_idx)", e)
        ftype = e.get("t", "int")
        if ftype == "int" or ftype.startswith("tainted[int]"):
            tmp = self._fresh("fld")
            self._emit("  %s = call i64 @hl_struct_get_i64(ptr %s, i64 %d)"
                       % (tmp, target_val, fidx))
            return ("i64", tmp)
        if ftype == "float" or ftype.startswith("tainted[float]"):
            tmp = self._fresh("fld")
            self._emit("  %s = call double @hl_struct_get_f64(ptr %s, i64 %d)"
                       % (tmp, target_val, fidx))
            return ("double", tmp)
        if ftype == "bool" or ftype.startswith("tainted[bool]"):
            tmp = self._fresh("fld")
            self._emit("  %s = call i1 @hl_struct_get_bool(ptr %s, i64 %d)"
                       % (tmp, target_val, fidx))
            return ("i1", tmp)
        tmp = self._fresh("fld")
        self._emit("  %s = call ptr @hl_struct_get_ptr(ptr %s, i64 %d)"
                   % (tmp, target_val, fidx))
        return ("ptr", tmp)

    def _lower_field_assign_typed(self, target: Dict, val_ty: str, val: str):
        """Stage 12 release: lower `expr.field = value` via the typed
        `hl_struct_set_*` runtime helpers."""
        target_ty, target_val = self._lower_expr_typed(target["target"])
        if self._cur_block is None:
            return
        fidx = target.get("field_idx")
        if fidx is None:
            _unsupported("struct field assignment (no field_idx annotation)", target)
        ftype = target.get("t", "int")
        if ftype == "int" or ftype.startswith("tainted[int]"):
            v = self._coerce(val_ty, val, "i64")
            self._emit("  call void @hl_struct_set_i64(ptr %s, i64 %d, i64 %s)"
                       % (target_val, fidx, v))
        elif ftype == "float" or ftype.startswith("tainted[float]"):
            v = self._coerce(val_ty, val, "double")
            self._emit("  call void @hl_struct_set_f64(ptr %s, i64 %d, double %s)"
                       % (target_val, fidx, v))
        elif ftype == "bool" or ftype.startswith("tainted[bool]"):
            v = self._coerce(val_ty, val, "i1")
            self._emit("  call void @hl_struct_set_bool(ptr %s, i64 %d, i1 %s)"
                       % (target_val, fidx, v))
        else:
            v = self._coerce(val_ty, val, "ptr")
            self._emit("  call void @hl_struct_set_ptr(ptr %s, i64 %d, ptr %s)"
                       % (target_val, fidx, v))

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




__all__ = [
]
