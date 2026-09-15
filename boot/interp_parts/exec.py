"""Interp mixin (exec) - verbatim segment of the original
boot/interp.py Interp class (lines 1014..1367), split for
maintainability. The final Interp class assembles all mixins in
boot/interp_parts/interp.py - behavior is unchanged."""
from .rt import (
    BreakSig, ContinueSig, HLPanic, ReturnSig, TailCallSig, f64_div, f64_mod, i64_add,
    i64_div, i64_mod, i64_mul, i64_neg, i64_sub,
)

class InterpExec(object):
    def exec_stmts(self, stmts, env):
        for s in stmts:
            self.exec_stmt(s, env)

    def exec_stmt(self, s, env):
        self.line = s.get("line", 0)
        k = s["k"]
        if k == "let":
            env[-1][s["name"]] = [self.eval_expr(s["value"], env), s["mut"], False]
        elif k == "assign":
            self.exec_assign(s, env)
        elif k == "if":
            if self.eval_expr(s["cond"], env):
                env.append({})
                try:
                    self.exec_stmts(s["then"], env)
                finally:
                    env.pop()
            elif s["els"] is not None:
                env.append({})
                try:
                    self.exec_stmts(s["els"], env)
                finally:
                    env.pop()
        elif k == "while":
            while self.eval_expr(s["cond"], env):
                env.append({})
                try:
                    self.exec_stmts(s["body"], env)
                except BreakSig:
                    break
                except ContinueSig:
                    continue
                finally:
                    env.pop()
        elif k == "for":
            lst = self.eval_expr(s["iter"], env)
            n = len(lst)  # snapshot length once (SPEC section 5)
            i = 0
            while i < n:
                # BUG-SC-4 fix: if the loop body shrinks the list (e.g.
                # `xs.pop()`), `lst[i]` would raise a Python IndexError,
                # crashing the interpreter with a traceback instead of a
                # clean HLPanic. Bounds-check before access and stop
                # iterating once the list is shorter than the snapshot.
                # The SPEC only guarantees that appended elements are not
                # visited; shrinking during iteration is undefined, so we
                # stop cleanly rather than crash.
                if i >= len(lst):
                    break
                # BUG-22 fix: use a 3-element binding [value, mut, moved]
                # to match all other bindings in the interpreter. The
                # previous 2-element form was internally inconsistent and
                # would have crashed any future code that indexed [2].
                env.append({s["var"]: [lst[i], False, False]})
                try:
                    self.exec_stmts(s["body"], env)
                except BreakSig:
                    break
                except ContinueSig:
                    pass
                finally:
                    env.pop()
                i += 1
        elif k == "return":
            v = s["value"]
            if v is not None:
                # Stage 31 (v0.48.0-alpha): a VERIFIED tail self-call in
                # a #[tail_call] fn raises TailCallSig instead — call_fn
                # rebinds the parameters and re-runs the body in the
                # SAME Python frame (the trampoline mirror of the native
                # parameter-rebinding goto). The checker guarantees the
                # call target is the CURRENT fn and the dataflow is
                # primitive-only, so evaluating the argument list here
                # has no refcount side effects.
                fn_stack = getattr(self._tls, "fn_stack", None)
                if fn_stack:
                    cur = fn_stack[-1] if fn_stack else None
                    rc = v.get("rc")
                    if (rc is not None and rc[0] == "user" and rc[1] == cur
                            and cur in self.fns
                            and self.fns[cur].get("attrs", {}).get("tail_call", False)):
                        args = [self.eval_expr(a, env) for a in v.get("args", [])]
                        raise TailCallSig(cur, args, self.line)
            raise ReturnSig(self.eval_expr(v, env) if v is not None else None)
        elif k == "break":
            raise BreakSig()
        elif k == "continue":
            raise ContinueSig()
        elif k == "expr":
            self.eval_expr(s["e"], env)
        elif k == "asm":
            # Stage 27 (v0.50.0-alpha): inline-assembly statement.
            # The boot interpreter is a pure-Python reference and
            # cannot execute native asm. Raise a clean error rather
            # than silently no-op'ing (a silent no-op would mislead
            # users — `asm!("mov $0, {0}", out(reg) x); print(x)`
            # would print the original x and look like the asm ran).
            # Programs that declare `asm!` blocks but do NOT execute
            # them at interpreter runtime (e.g. an `asm!` inside a
            # kernel IRQ handler never called from `main`) run fine.
            raise HLPanic(
                "asm! cannot be executed by the boot interpreter — "
                "use the native compiler `hlc` (which lowers asm! to "
                "GCC extended asm) or wrap the asm! call in a path "
                "the interpreter does not take (e.g. an extern \"C\" "
                "fn or a kernel-only entrypoint)",
                self.line)
        else:
            raise HLPanic("unknown statement: %s" % k, self.line)

    def exec_assign(self, s, env):
        val = self.eval_expr(s["value"], env)
        t = s["target"]
        if t["k"] == "ident":
            for scope in reversed(env):
                if t["name"] in scope:
                    scope[t["name"]][0] = val
                    return
            raise HLPanic("variable does not exist: %s" % t["name"], self.line)
        base = self.eval_expr(t["target"], env)
        if t["k"] == "field":
            base[t["name"]] = val
        elif t["k"] == "index":
            i = self.eval_expr(t["idx"], env)
            if i < 0 or i >= len(base):
                raise HLPanic("array access out of bounds", self.line)
            base[i] = val
        else:
            raise HLPanic("invalid lvalue", self.line)

    # ---------- expressions ----------
    def eval_expr(self, e, env):
        k = e["k"]
        if k == "ident":
            name = e["name"]
            for scope in reversed(env):
                if name in scope:
                    return scope[name][0]
            raise HLPanic("variable does not exist: %s" % name, self.line)
        if k == "bin":
            return self.eval_bin(e, env)
        if k == "int" or k == "float" or k == "bool" or k == "str":
            return e["v"]
        if k == "call":
            rc = e["rc"]
            # Stage 16: spawn(f, args...) — the checker rewrote the node:
            # the fn-name argument was removed and recorded in e["spawn_fn"].
            # The target must NOT be evaluated as a value; do_spawn applies
            # the boundary ownership rule to each argument node.
            if rc[0] == "builtin" and rc[1] == "spawn":
                return self.do_spawn(e["spawn_fn"], e["args"], env)
            # Stage 33: async_spawn(f, args...) — like spawn, but creates
            # a cap-1 bounded channel and returns it as a Future. The
            # spawned task calls f(args...) and sends the result on the
            # channel.
            if rc[0] == "builtin" and rc[1] == "async_spawn":
                return self.do_async_spawn(e["spawn_fn"], e["args"], env)
            # Stage 34: gen_spawn(f, args...) -> Stream[T] — creates a
            # bounded stream, spawns f(stream, args...), returns the stream.
            if rc[0] == "builtin" and rc[1] == "gen_spawn":
                return self.do_gen_spawn(e["spawn_fn"], e["args"], env)
            # Stage 34: stream_map_int / stream_filter_int / stream_fold_int /
            # stream_flat_map_int — each takes a Stream and a function name
            # (recorded in spawn_fn by the checker). The worker is spawned
            # with (in_stream, out_stream [, extra]) and calls the function.
            if rc[0] == "builtin" and rc[1] in (
                    "stream_map_int", "stream_filter_int",
                    "stream_fold_int", "stream_flat_map_int"):
                return self.do_stream_combinator(rc[1], e["spawn_fn"],
                                                  e["args"], env)
            args = [self.eval_expr(a, env) for a in e["args"]]
            if rc[0] == "user":
                return self.call_fn(rc[1], args)
            # Deep-scan-25: pass the argument NODES so boundary builtins
            # (stream_send / future_ready) can apply the syntactic
            # clone(...) exemption — the same rule builtin_method applies
            # for chan.send (mirrors the native gen_own_arg).
            return self.builtin(rc[1], args, e["args"])
        if k == "field":
            return self.eval_expr(e["target"], env)[e["name"]]
        if k == "method":
            tgt = self.eval_expr(e["target"], env)
            args = [self.eval_expr(a, env) for a in e["args"]]
            rm = e["rm"]
            if rm[0] == "user":
                return self.call_fn(rm[1], [tgt] + args)
            return self.builtin_method(rm[1], tgt, args, e["args"])
        if k == "index":
            lst = self.eval_expr(e["target"], env)
            i = self.eval_expr(e["idx"], env)
            if i < 0 or i >= len(lst):
                raise HLPanic("array access out of bounds", self.line)
            return lst[i]
        if k == "un":
            v = self.eval_expr(e["e"], env)
            if e["op"] == "!":
                return not v
            if type(v) is int:
                return i64_neg(v, self.line)
            return -v
        if k == "listlit":
            return [self.eval_expr(it, env) for it in e["items"]]
        if k == "structlit":
            return self.eval_structlit(e, env)
        if k == "enumlit":
            return self.eval_enumlit(e, env)
        if k == "match":
            return self.eval_match(e, env)
        if k == "qmark":
            return self.eval_qmark(e, env)
        # BUG-SC-10 fix: removed the dead `if k == "mapnew": return {}`
        # branch. The parser never produces a `mapnew` AST node; `map_new()`
        # is a `call` node handled by the `builtin` method.
        raise HLPanic("unknown expression: %s" % k, self.line)

    def eval_structlit(self, e, env):
        name = e["name"]
        # In case of a generic struct, the parser keeps the base name; we use
        # the type from `e["t"]` which has the instantiation. But the field
        # values are determined by `e["fields"]` (in declaration order, may
        # omit defaulted trailing fields). We fill defaults from the struct
        # definition.
        st = self.structs[name]
        decl_fields = st["fields"]  # [(name, type, default_expr_or_None)]
        result = {}
        # Map provided field names → values.
        provided = {}
        for fname, fe in e["fields"]:
            provided[fname] = self.eval_expr(fe, env)
        # Iterate declared fields in order; use provided value or default.
        for fname, ftype, fdefault in decl_fields:
            if fname in provided:
                result[fname] = provided[fname]
            elif fdefault is not None:
                # Evaluate default expression in the calling environment.
                result[fname] = self.eval_expr(fdefault, env)
            else:
                # Should have been caught by the checker.
                raise HLPanic("struct literal missing required field: %s" % fname,
                              self.line)
        return result

    def eval_enumlit(self, e, env):
        # e["enum_name"], e["variant"], e["args"]
        args = [self.eval_expr(a, env) for a in e.get("args", [])]
        return {"enum": e["enum_name"], "var": e["variant"], "data": args}

    def eval_match(self, e, env):
        scrut = self.eval_expr(e["scrut"], env)
        if not isinstance(scrut, dict) or "enum" not in scrut:
            raise HLPanic("match on non-enum value", self.line)
        s_enum = scrut["enum"]
        s_var = scrut["var"]
        s_data = scrut["data"]
        for arm in e["arms"]:
            pat = arm["pattern"]
            if pat["k"] == "wildcard":
                return self.eval_expr(arm["body"], env)
            if pat["enum"] != s_enum:
                continue
            if pat["variant"] != s_var:
                continue
            # Bind payload values.
            env.append({})
            try:
                for i, bname in enumerate(pat["bindings"]):
                    if bname == "_":
                        continue
                    # Stage 27 perfection (v0.50.3-alpha) deep-scan-18:
                    # BUG-13 fix. The previous `else None` fallback
                    # silently set the binding to Python `None` when the
                    # pattern had more bindings than the enum variant's
                    # data (i >= len(s_data)). The checker validates
                    # len(bindings) == len(payloads), so this branch
                    # should be unreachable — but if a checker bug ever
                    # allowed a mismatch, the runtime would set a
                    # binding to None and the next use would raise a
                    # confusing Python TypeError (e.g. `None + 5`).
                    # Now: a clean HLPanic surfaces the bug.
                    if i >= len(s_data):
                        raise HLPanic("match: binding count exceeds "
                                      "payload count (internal "
                                      "checker bug — please report)",
                                      self.line)
                    env[-1][bname] = [s_data[i], False, False]
                return self.eval_expr(arm["body"], env)
            finally:
                env.pop()
        # No arm matched (shouldn't happen if exhaustive).
        raise HLPanic("match: no arm matched (non-exhaustive?)", self.line)

    def eval_qmark(self, e, env):
        v = self.eval_expr(e["e"], env)
        if not isinstance(v, dict) or "enum" not in v:
            raise HLPanic("? on non-enum value", self.line)
        if v["var"] == e["ok_variant"]:
            # Success — yield the single payload value.
            if len(v["data"]) != 1:
                raise HLPanic("? operator: success variant must have exactly one payload",
                              self.line)
            return v["data"][0]
        if v["var"] == e["err_variant"]:
            # Propagate the error: re-wrap and return from the enclosing fn.
            raise ReturnSig(v)
        raise HLPanic("? operator: enum value matched neither ok nor err variant", self.line)

    def eval_bin(self, e, env):
        op = e["op"]
        if op == "||":
            if self.eval_expr(e["l"], env):
                return True
            return self.eval_expr(e["r"], env)
        if op == "&&":
            if not self.eval_expr(e["l"], env):
                return False
            return self.eval_expr(e["r"], env)
        a = self.eval_expr(e["l"], env)
        b = self.eval_expr(e["r"], env)
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "+":
            if type(a) is int:
                return i64_add(a, b, self.line)
            return a + b  # float or str
        if op == "-":
            if type(a) is int:
                return i64_sub(a, b, self.line)
            return a - b
        if op == "*":
            if type(a) is int:
                return i64_mul(a, b, self.line)
            return a * b
        if op == "/":
            if type(a) is int:
                return i64_div(a, b, self.line)
            return f64_div(a, b)
        if op == "%":
            if type(a) is int:
                return i64_mod(a, b, self.line)
            return f64_mod(a, b)
        raise HLPanic("unknown operator: %s" % op, self.line)

    # ---------- builtins ----------
