"""Interp mixin (exec) - verbatim segment of the original
boot/interp.py Interp class (lines 1014..1367), split for
maintainability. The final Interp class assembles all mixins in
boot/interp_parts/interp.py - behavior is unchanged."""
from .rt import (
    BreakSig, ContinueSig, HLPanic, ReturnSig, TailCallSig, f64_div, f64_mod, i64_add,
    i64_div, i64_mod, i64_mul, i64_neg, i64_sub,
    INT64_MAX, INT64_MIN,
)

class InterpExec(object):
    def exec_stmts(self, stmts, env):
        es = self.exec_stmt
        for s in stmts:
            es(s, env)

    def _lookup(self, env, name):
        # The fallback chain walk, factored out of the hot read sites:
        # called only when the pinned frame missed (or the node never
        # resolved), so the extra Python frame never touches the fast
        # path. Same search order and panic as the inline walk it
        # replaces.
        i = len(env) - 1
        while i >= 0:
            b = env[i].get(name)
            if b is not None:
                return b[0]
            i -= 1
        raise HLPanic("variable does not exist: %s" % name, self._tls.line)

    def exec_stmt(self, s, env):
        # Hot path: write the thread-local line slot directly. The
        # `line` property (a Python function call per access) is kept
        # for the cold read sites; this write runs once per executed
        # statement, and threading.local setattr is a C-level slot
        # write with per-thread isolation — identical semantics, no
        # property-call overhead. The resolver stamps "line" on every
        # statement (the parser already does), so the read is a direct
        # subscript.
        self._tls.line = s["line"]
        # Branch order follows the measured dynamic mix of a compiler-
        # source run (if / let / assign / return dominate; every test
        # is a unique interned string, so the chain costs one compare
        # per miss).
        k = s["k"]
        if k == "if":
            # The condition is a bin node on almost every hot if —
            # calling eval_bin directly skips one dispatch layer.
            c = s["cond"]
            if c["k"] == "bin":
                taken = self.eval_bin(c, env)
            else:
                taken = self.eval_expr(c, env)
            if taken:
                if s["nf"]:
                    env.append({})
                    try:
                        self.exec_stmts(s["then"], env)
                    finally:
                        env.pop()
                else:
                    # A binding-free body: the frame the block would
                    # push is empty, and an empty frame is invisible to
                    # every lookup (pinned reads and fallback walks
                    # both fall through it). The resolver numbers
                    # frames with the same skip, so indices stay in
                    # lockstep.
                    self.exec_stmts(s["then"], env)
            elif s["els"] is not None:
                if s["nf_else"]:
                    env.append({})
                    try:
                        self.exec_stmts(s["els"], env)
                    finally:
                        env.pop()
                else:
                    self.exec_stmts(s["els"], env)
        elif k == "let":
            # The value is an identifier or a literal on the great
            # majority of lets — the same leaf fusion eval_bin applies
            # to its operands, saving one eval_expr frame per binding.
            v = s["value"]
            kv = v["k"]
            if kv == "ident":
                fi = v["fi"]
                if fi is not None:
                    try:
                        val = env[fi][v["name"]][0]
                    except KeyError:
                        val = self._lookup(env, v["name"])
                else:
                    val = self._lookup(env, v["name"])
            elif kv == "int" or kv == "str" or kv == "float" or kv == "bool":
                val = v["v"]
            else:
                val = self.eval_expr(v, env)
            # Stage 98: runtime refinement guard on the binding (the
            # parser attached "rpred"; the checker validated the
            # predicate and already rejected constant violations).
            lref = s.get("rpred")
            if lref is not None:
                self.check_refine(lref, val, "variable '%s'" % s["name"])
            env[-1][s["name"]] = [val, s["mut"], False]
        elif k == "assign":
            self.exec_assign(s, env)
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
                fn_stack = self._tls.fn_stack
                if fn_stack:
                    cur = fn_stack[-1]
                    rc = v.get("rc")
                    # self._tail_fns: the precomputed set of fn keys with
                    # the #[tail_call] attribute (built in InterpCore);
                    # replaces the per-return attrs dict walk.
                    if (rc is not None and rc[0] == "user" and rc[1] == cur
                            and cur in self._tail_fns):
                        args = [self.eval_expr(a, env) for a in v.get("args", [])]
                        raise TailCallSig(cur, args, self._tls.line)
                raise ReturnSig(self.eval_expr(v, env))
            raise ReturnSig(None)
        elif k == "expr":
            self.eval_expr(s["e"], env)
        elif k == "while":
            cond = s["cond"]
            body = s["body"]
            cb = cond["k"] == "bin"
            if s["nf"]:
                while True:
                    if cb:
                        if not self.eval_bin(cond, env):
                            break
                    else:
                        if not self.eval_expr(cond, env):
                            break
                    env.append({})
                    try:
                        self.exec_stmts(body, env)
                    except BreakSig:
                        break
                    except ContinueSig:
                        continue
                    finally:
                        env.pop()
            else:
                # Binding-free loop body: no per-iteration frame (the
                # pushed dict was empty and unobservable), no finally.
                while True:
                    if cb:
                        if not self.eval_bin(cond, env):
                            break
                    else:
                        if not self.eval_expr(cond, env):
                            break
                    try:
                        self.exec_stmts(body, env)
                    except BreakSig:
                        break
                    except ContinueSig:
                        continue
        elif k == "for":
            lst = self.eval_expr(s["iter"], env)
            n = len(lst)  # snapshot length once (SPEC section 5)
            var = s["var"]
            sbody = s["body"]
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
                env.append({var: [lst[i], False, False]})
                try:
                    self.exec_stmts(sbody, env)
                except BreakSig:
                    break
                except ContinueSig:
                    pass
                finally:
                    env.pop()
                i += 1
        elif k == "break":
            raise BreakSig()
        elif k == "continue":
            raise ContinueSig()
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
        v = s["value"]
        kv = v["k"]
        # Leaf fusion, as on `let`: identifier and literal right-hand
        # sides skip the eval_expr frame.
        if kv == "ident":
            fi = v["fi"]
            if fi is not None:
                try:
                    val = env[fi][v["name"]][0]
                except KeyError:
                    val = self._lookup(env, v["name"])
            else:
                val = self._lookup(env, v["name"])
        elif kv == "int" or kv == "str" or kv == "float" or kv == "bool":
            val = v["v"]
        else:
            val = self.eval_expr(v, env)
        # Stage 98: runtime refinement guard on writes into refined
        # slots ("rpred" is attached by the checker — ident targets
        # read the binding's refinement, field targets the struct
        # field's). Elided sites (never attached) skip the guard.
        sref = s.get("rpred")
        if sref is not None:
            self.check_refine(sref, val,
                              s.get("rp_what") or "the assigned value")
        t = s["target"]
        tk = t["k"]
        if tk == "ident":
            name = t["name"]
            fi = t["fi"]
            if fi is not None:
                try:
                    b = env[fi][name]
                except KeyError:
                    b = None
                if b is not None:
                    b[0] = val
                    return
            i = len(env) - 1
            while i >= 0:
                b = env[i].get(name)
                if b is not None:
                    b[0] = val
                    return
                i -= 1
            raise HLPanic("variable does not exist: %s" % name, self._tls.line)
        base = self.eval_expr(t["target"], env)
        if tk == "field":
            base[t["name"]] = val
        elif tk == "index":
            i = self.eval_expr(t["idx"], env)
            if i < 0 or i >= len(base):
                raise HLPanic("array access out of bounds", self._tls.line)
            base[i] = val
        else:
            raise HLPanic("invalid lvalue", self._tls.line)

    # ---------- expressions ----------
    def eval_expr(self, e, env):
        # Dispatch order follows the measured dynamic mix of a
        # compiler-source run (bin > ident > field > method > call >
        # literals); every test is a unique interned string, so the
        # chain costs one compare per miss.
        k = e["k"]
        if k == "bin":
            return self.eval_bin(e, env)
        if k == "ident":
            name = e["name"]
            fi = e["fi"]
            if fi is not None:
                # Direct subscript: the resolver guarantees the frame
                # layout, and a pinned frame that somehow misses the
                # name falls to the same chain walk the .get form
                # used (the try costs nothing when it never raises).
                try:
                    return env[fi][name][0]
                except KeyError:
                    pass
            # Fallback (unannotated node, or the frame missed): the
            # original chain walk, byte-exact in behaviour.
            return self._lookup(env, name)
        if k == "field":
            t = e["target"]
            kt = t["k"]
            if kt == "ident":
                fi = t["fi"]
                if fi is not None:
                    try:
                        base = env[fi][t["name"]][0]
                    except KeyError:
                        base = self._lookup(env, t["name"])
                else:
                    base = self._lookup(env, t["name"])
            else:
                base = self.eval_expr(t, env)
            return base[e["name"]]
        if k == "method":
            t = e["target"]
            kt = t["k"]
            if kt == "ident":
                fi = t["fi"]
                if fi is not None:
                    try:
                        tgt = env[fi][t["name"]][0]
                    except KeyError:
                        tgt = self._lookup(env, t["name"])
                else:
                    tgt = self._lookup(env, t["name"])
            else:
                tgt = self.eval_expr(t, env)
            args = []
            for a in e["args"]:
                ka = a["k"]
                if ka == "ident":
                    fi = a["fi"]
                    if fi is not None:
                        try:
                            args.append(env[fi][a["name"]][0])
                            continue
                        except KeyError:
                            pass
                    args.append(self._lookup(env, a["name"]))
                elif ka == "int" or ka == "str" or ka == "float" or ka == "bool":
                    args.append(a["v"])
                else:
                    args.append(self.eval_expr(a, env))
            rm = e["rm"]
            if rm[0] == "user":
                return self.call_fn(rm[1], [tgt] + args)
            return self.builtin_method(rm[1], tgt, args, e["args"])
        if k == "call":
            rc = e["rc"]
            # Stage 16: spawn(f, args...) — the checker rewrote the node:
            # the fn-name argument was removed and recorded in e["spawn_fn"].
            # The target must NOT be evaluated as a value; do_spawn applies
            # the boundary ownership rule to each argument node.
            if rc[0] == "builtin" and rc[1] == "spawn":
                return self.do_spawn(e["spawn_fn"], e["args"], env)
            # Stage 33: async_spawn(f, args...) — like spawn, but creates a
            # cap-1 bounded channel and returns it as a Future. The
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
            args = []
            for a in e["args"]:
                ka = a["k"]
                if ka == "ident":
                    fi = a["fi"]
                    if fi is not None:
                        try:
                            args.append(env[fi][a["name"]][0])
                            continue
                        except KeyError:
                            pass
                    args.append(self._lookup(env, a["name"]))
                elif ka == "int" or ka == "str" or ka == "float" or ka == "bool":
                    args.append(a["v"])
                else:
                    args.append(self.eval_expr(a, env))
            if rc[0] == "user":
                return self.call_fn(rc[1], args)
            # Deep-scan-25: pass the argument NODES so boundary builtins
            # (stream_send / future_ready) can apply the syntactic
            # clone(...) exemption — the same rule builtin_method applies
            # for chan.send (mirrors the native gen_own_arg).
            return self.builtin(rc[1], args, e["args"])
        if k == "int" or k == "float" or k == "bool" or k == "str":
            return e["v"]
        if k == "index":
            t = e["target"]
            kt = t["k"]
            if kt == "ident":
                fi = t["fi"]
                if fi is not None:
                    try:
                        lst = env[fi][t["name"]][0]
                    except KeyError:
                        lst = self._lookup(env, t["name"])
                else:
                    lst = self._lookup(env, t["name"])
            else:
                lst = self.eval_expr(t, env)
            ix = e["idx"]
            kx = ix["k"]
            if kx == "ident":
                fi = ix["fi"]
                if fi is not None:
                    try:
                        i = env[fi][ix["name"]][0]
                    except KeyError:
                        i = self._lookup(env, ix["name"])
                else:
                    i = self._lookup(env, ix["name"])
            elif kx == "int" or kx == "str" or kx == "float" or kx == "bool":
                i = ix["v"]
            else:
                i = self.eval_expr(ix, env)
            if i < 0 or i >= len(lst):
                raise HLPanic("array access out of bounds", self._tls.line)
            return lst[i]
        if k == "un":
            ue = e["e"]
            ke = ue["k"]
            if ke == "ident":
                fi = ue["fi"]
                if fi is not None:
                    try:
                        v = env[fi][ue["name"]][0]
                    except KeyError:
                        v = self._lookup(env, ue["name"])
                else:
                    v = self._lookup(env, ue["name"])
            elif ke == "int" or ke == "str" or ke == "float" or ke == "bool":
                v = ue["v"]
            else:
                v = self.eval_expr(ue, env)
            if e["op"] == "!":
                return not v
            if type(v) is int:
                # i64_neg inlined: only INT64_MIN overflows; the panic
                # line and message are identical to the wrapper's.
                if v == INT64_MIN:
                    raise HLPanic("integer overflow", self._tls.line)
                return -v
            return -v
        if k == "listlit":
            out = []
            for it in e["items"]:
                ki = it["k"]
                if ki == "ident":
                    fi = it["fi"]
                    if fi is not None:
                        try:
                            out.append(env[fi][it["name"]][0])
                            continue
                        except KeyError:
                            pass
                    out.append(self._lookup(env, it["name"]))
                elif ki == "int" or ki == "str" or ki == "float" or ki == "bool":
                    out.append(it["v"])
                else:
                    out.append(self.eval_expr(it, env))
            return out
        if k == "structlit":
            return self.eval_structlit(e, env)
        if k == "enumlit":
            # e["enum_name"], e["variant"], e["args"]
            args = []
            for a in e.get("args", []):
                ka = a["k"]
                if ka == "ident":
                    fi = a["fi"]
                    if fi is not None:
                        try:
                            args.append(env[fi][a["name"]][0])
                            continue
                        except KeyError:
                            pass
                    args.append(self._lookup(env, a["name"]))
                elif ka == "int" or ka == "str" or ka == "float" or ka == "bool":
                    args.append(a["v"])
                else:
                    args.append(self.eval_expr(a, env))
            return {"enum": e["enum_name"], "var": e["variant"], "data": args}
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
        field_preds = st.get("field_preds") or {}
        result = {}
        # Map provided field names → values.
        provided = {}
        for fname, fe in e["fields"]:
            kf = fe["k"]
            if kf == "ident":
                fi = fe["fi"]
                if fi is not None:
                    try:
                        provided[fname] = env[fi][fe["name"]][0]
                        continue
                    except KeyError:
                        pass
                provided[fname] = self._lookup(env, fe["name"])
            elif kf == "int" or kf == "str" or kf == "float" or kf == "bool":
                provided[fname] = fe["v"]
            else:
                provided[fname] = self.eval_expr(fe, env)
        # Iterate declared fields in order; use provided value or default.
        for fname, ftype, fdefault in decl_fields:
            if fname in provided:
                val = provided[fname]
            elif fdefault is not None:
                # Evaluate default expression in the calling environment.
                val = self.eval_expr(fdefault, env)
            else:
                # Should have been caught by the checker.
                raise HLPanic("struct literal missing required field: %s" % fname,
                              self.line)
            # Stage 98: construction-time refinement guard — EVERY value
            # entering a refined field (provided or defaulted) passes
            # the predicate here; the native compiler emits the same
            # guard inside the generated constructor.
            fref = field_preds.get(fname)
            if fref is not None:
                self.check_refine(fref, val,
                                  "field '%s.%s'" % (name, fname))
            result[fname] = val
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
            # Bind payload values. An arm whose bindings are all "_"
            # skips the frame push: the pushed dict would be empty and
            # invisible to every lookup (the resolver numbers frames
            # with the same skip).
            if arm["nf"]:
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
            for i, bname in enumerate(pat["bindings"]):
                if bname == "_":
                    continue
                if i >= len(s_data):
                    raise HLPanic("match: binding count exceeds "
                                  "payload count (internal "
                                  "checker bug — please report)",
                                  self.line)
            return self.eval_expr(arm["body"], env)
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
        # Operand fetch is inlined here rather than delegated: bin nodes
        # are ~40% of the evaluated compound nodes and each carries two
        # operands, of which the majority are identifiers or constants
        # (loop counters, string literals, flags). Handling those two
        # kinds inline — with the exact same lookup order and panic
        # text as eval_expr — removes one Python call frame per leaf
        # operand without adding a dispatch layer; anything compound
        # falls through to eval_expr as before. The `fi` slot is
        # materialised on every ident by the resolver (index or None),
        # so both reads are direct subscripts.
        l = e["l"]
        kl = l["k"]
        if kl == "ident":
            fi = l["fi"]
            if fi is not None:
                try:
                    a = env[fi][l["name"]][0]
                except KeyError:
                    a = self._lookup(env, l["name"])
            else:
                a = self._lookup(env, l["name"])
        elif kl == "int" or kl == "str" or kl == "float" or kl == "bool":
            a = l["v"]
        else:
            a = self.eval_expr(l, env)
        if op == "||":
            if a:
                return True
            return self.eval_expr(e["r"], env)
        if op == "&&":
            if not a:
                return False
            return self.eval_expr(e["r"], env)
        r = e["r"]
        kr = r["k"]
        if kr == "ident":
            fi = r["fi"]
            if fi is not None:
                try:
                    b = env[fi][r["name"]][0]
                except KeyError:
                    b = self._lookup(env, r["name"])
            else:
                b = self._lookup(env, r["name"])
        elif kr == "int" or kr == "str" or kr == "float" or kr == "bool":
            b = r["v"]
        else:
            b = self.eval_expr(r, env)
        # Operator order follows the measured dynamic mix (== and +
        # dominate a compiler-source run).
        if op == "==":
            return a == b
        if op == "+":
            # Hot path: the checked int64 ops are inlined — the runtime
            # i64_add/i64_sub/i64_mul wrappers stay for the other
            # callers, but every compiler-source run pays a Python call
            # per arithmetic node, so the fast path does the same
            # bounds test inline and only the overflow raise reaches
            # out. Identical panics, identical lines.
            if type(a) is int:
                r = a + b
                if INT64_MIN <= r <= INT64_MAX:
                    return r
                raise HLPanic("integer overflow", self._tls.line)
            return a + b  # float or str
        if op == "<":
            return a < b
        if op == ">=":
            return a >= b
        if op == "!=":
            return a != b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == "-":
            if type(a) is int:
                r = a - b
                if INT64_MIN <= r <= INT64_MAX:
                    return r
                raise HLPanic("integer overflow", self._tls.line)
            return a - b
        if op == "*":
            if type(a) is int:
                r = a * b
                if INT64_MIN <= r <= INT64_MAX:
                    return r
                raise HLPanic("integer overflow", self._tls.line)
            return a * b
        if op == "/":
            if type(a) is int:
                return i64_div(a, b, self._tls.line)
            return f64_div(a, b)
        if op == "%":
            if type(a) is int:
                return i64_mod(a, b, self._tls.line)
            return f64_mod(a, b)
        raise HLPanic("unknown operator: %s" % op, self._tls.line)

    # ---------- builtins ----------
