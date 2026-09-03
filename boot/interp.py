"""Stage-0 evaluator for HLS (v0.3) — adds enum, match, `?` operator.

Runtime values:
  int   -> int (Python, kept within int64 range by checked arithmetic)
  float -> float, bool -> bool, str -> bytes
  list[T]      -> list
  map[str,T]   -> dict (insertion-ordered), struct -> dict {field: value}
  enum         -> dict {"enum": <name>, "var": <variant>, "data": [payloads]}
"""
import ctypes
import math
import sys
import time

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
B_LOW = bytes(range(0x21))  # bytes <= 0x20 used by trim


class HLPanic(Exception):
    def __init__(self, msg, line):
        super().__init__(msg)
        self.msg = msg
        self.line = line


class ReturnSig(Exception):
    def __init__(self, value):
        self.value = value


class BreakSig(Exception):
    pass


class ContinueSig(Exception):
    pass


# ---------- int64 checked arithmetic ----------
def i64_add(a, b, line):
    r = a + b
    if r < INT64_MIN or r > INT64_MAX:
        raise HLPanic("integer overflow", line)
    return r


def i64_sub(a, b, line):
    r = a - b
    if r < INT64_MIN or r > INT64_MAX:
        raise HLPanic("integer overflow", line)
    return r


def i64_mul(a, b, line):
    r = a * b
    if r < INT64_MIN or r > INT64_MAX:
        raise HLPanic("integer overflow", line)
    return r


def i64_neg(a, line):
    if a == INT64_MIN:
        raise HLPanic("integer overflow", line)
    return -a


def i64_div(a, b, line):
    if b == 0:
        raise HLPanic("division by zero", line)
    if a == INT64_MIN and b == -1:
        raise HLPanic("integer overflow", line)
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def i64_mod(a, b, line):
    if b == 0:
        raise HLPanic("division by zero", line)
    if a == INT64_MIN and b == -1:
        raise HLPanic("integer overflow", line)
    r = abs(a) % abs(b)
    return r if a >= 0 else -r


def f64_div(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        # BUG-SC-6 fix: handle -0.0 divisor correctly. Previously the
        # sign test `(a > 0) == (b >= 0)` treated +0.0 and -0.0 the same
        # (both pass `>= 0`), producing the wrong sign of infinity when
        # the divisor was -0.0. Use math.copysign to distinguish them.
        if a == 0 or a != a:
            return float("nan")
        # copysign(1.0, x) returns 1.0 for +x (incl. +0.0) and -1.0 for -x
        # (incl. -0.0). Result is +inf iff signs of a and b agree.
        same_sign = math.copysign(1.0, a) == math.copysign(1.0, b)
        return float("inf") if same_sign else float("-inf")


def f64_mod(a, b):
    try:
        return math.fmod(a, b)
    except ValueError:
        return float("nan")


def parse_int(s, line):
    """Convert bytes -> int, matching the C semantics of builtin int()/to_int()."""
    n = len(s)
    i = 0
    neg = False
    if n > 0 and s[0:1] == b"-":
        neg = True
        i = 1
    if i >= n:
        raise HLPanic("cannot convert string to int", line)
    v = 0
    while i < n:
        c = s[i]
        if c < 48 or c > 57:
            raise HLPanic("cannot convert string to int", line)
        v = v * 10 + (c - 48)
        if v > 2 ** 63:
            raise HLPanic("integer too large when converting string", line)
        i += 1
    if neg:
        return -v
    if v > INT64_MAX:
        raise HLPanic("integer too large when converting string", line)
    return v


def fmt_float(v):
    return ("%.6f" % v).encode("ascii")


def to_display(b):
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return str(b)


class Interp:
    def __init__(self, program, argv, out):
        self.p = program
        self.fns = program["fns"]
        self.structs = program["structs"]
        self.enums = program.get("enums", {})
        self.argv = argv  # list[bytes]
        self.out = out
        self.line = 0

    # ---------- lifecycle ----------
    def run(self):
        """Run main(); return exit code."""
        try:
            r = self.call_fn("main", [])
        except HLPanic as ex:
            self.out.flush()
            sys.stderr.write("panic: %s (at line %d)\n" % (to_display(ex.msg), ex.line))
            return 101
        if r is None:
            return 0
        return int(r) & 0xFF

    def call_fn(self, key, args):
        fn = self.fns[key]
        # Stage 15 (v0.13.0-alpha): extern fn — call via ctypes.
        if fn.get("extern", False):
            return self.call_extern(fn, args)
        env = [{}]
        if fn["struct"] is not None:
            sn, _, sm = fn["params"][0]
            env[0][sn] = [args[0], sm, False]
            params = fn["params"][1:]
            args = args[1:]
        else:
            params = fn["params"]
        for (pn, _, _), v in zip(params, args):
            env[0][pn] = [v, False, False]
        try:
            self.exec_stmts(fn["body"], env)
        except ReturnSig as r:
            return r.value
        return None

    # ---------- extern (Stage 15) ----------
    _libc = None

    def _get_libc(self):
        """Lazily load libc for extern calls."""
        if self._libc is None:
            try:
                # `None` loads the default C library (libc on Linux,
                # msvcrt on Windows, libSystem on macOS).
                Interp._libc = ctypes.CDLL(None)
            except OSError as ex:
                raise HLPanic("cannot load libc for extern call: %s" % ex,
                              getattr(self, "line", 0))
        return self._libc

    def call_extern(self, fn, args):
        """Call a C function via ctypes (Stage 15-alpha).

        The function signature is taken from the HLS declaration:
          - int -> c_int64
          - float -> c_double
          - bool -> c_bool
          - str -> c_char_p (passed as a null-terminated C string;
            HLS bytes are passed as-is; the caller is responsible for
            ensuring no embedded NUL bytes)
          - void -> no return
          - any other type (list/map/struct/enum/tainted/ptr) -> ptr
            (treated as an opaque pointer; the caller must ensure
            ABI compatibility)

        For the alpha, only int/str args are fully supported. Float
        and bool work via automatic ctypes conversion. Opaque pointers
        are NOT derefenced by the interpreter — they're passed as
        raw addresses.
        """
        libc = self._get_libc()
        name = fn["name"]
        try:
            c_fn = getattr(libc, name)
        except AttributeError:
            raise HLPanic("extern function not found in libc: %s" % name,
                          getattr(self, "line", 0))
        # Set up the argument types.
        c_argtypes = []
        c_args = []
        for (pn, pt, _), v in zip(fn["params"], args):
            if pt == "int":
                c_argtypes.append(ctypes.c_int64)
                c_args.append(int(v))
            elif pt == "float":
                c_argtypes.append(ctypes.c_double)
                c_args.append(float(v))
            elif pt == "bool":
                c_argtypes.append(ctypes.c_bool)
                c_args.append(bool(v))
            elif pt == "str":
                # HLS str is bytes. Pass as a null-terminated C string.
                c_argtypes.append(ctypes.c_char_p)
                if isinstance(v, bytes):
                    c_args.append(v)
                else:
                    c_args.append(str(v).encode("utf-8"))
            else:
                # Opaque pointer. The caller is responsible for ABI
                # compatibility; we treat the value as a raw address.
                c_argtypes.append(ctypes.c_void_p)
                if isinstance(v, int):
                    c_args.append(v)
                elif isinstance(v, bytes):
                    c_args.append(ctypes.cast(v, ctypes.c_void_p).value)
                else:
                    c_args.append(id(v))
        c_fn.argtypes = c_argtypes
        # Set up the return type.
        ret = fn["ret"]
        if ret == "int":
            c_fn.restype = ctypes.c_int64
        elif ret == "float":
            c_fn.restype = ctypes.c_double
        elif ret == "bool":
            c_fn.restype = ctypes.c_bool
        elif ret == "str":
            c_fn.restype = ctypes.c_char_p
        elif ret == "void":
            c_fn.restype = None
        else:
            c_fn.restype = ctypes.c_void_p
        # Call.
        try:
            result = c_fn(*c_args)
        except Exception as ex:
            raise HLPanic("extern call to '%s' failed: %s" % (name, ex),
                          getattr(self, "line", 0))
        # Convert the return value back to HLS runtime values.
        if ret == "int":
            return int(result) if result is not None else 0
        if ret == "float":
            return float(result) if result is not None else 0.0
        if ret == "bool":
            return bool(result) if result is not None else False
        if ret == "str":
            # c_char_p returns bytes (null-terminated).
            if result is None:
                return b""
            if isinstance(result, bytes):
                return result
            return bytes(result)
        if ret == "void":
            return None
        # Opaque pointer -> int (the raw address).
        return int(result) if result is not None else 0

    # ---------- statements ----------
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
            raise ReturnSig(self.eval_expr(s["value"], env) if s["value"] is not None else None)
        elif k == "break":
            raise BreakSig()
        elif k == "continue":
            raise ContinueSig()
        elif k == "expr":
            self.eval_expr(s["e"], env)
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
            args = [self.eval_expr(a, env) for a in e["args"]]
            if rc[0] == "user":
                return self.call_fn(rc[1], args)
            return self.builtin(rc[1], args)
        if k == "field":
            return self.eval_expr(e["target"], env)[e["name"]]
        if k == "method":
            tgt = self.eval_expr(e["target"], env)
            args = [self.eval_expr(a, env) for a in e["args"]]
            rm = e["rm"]
            if rm[0] == "user":
                return self.call_fn(rm[1], [tgt] + args)
            return self.builtin_method(rm[1], tgt, args)
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
                    env[-1][bname] = [s_data[i] if i < len(s_data) else None, False, False]
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
    def builtin(self, name, args):
        line = self.line
        if name == "print":
            self.out.write(args[0])
            return None
        if name == "println":
            self.out.write(args[0] + b"\n")
            return None
        if name == "panic":
            raise HLPanic(args[0], line)
        if name == "exit":
            self.out.flush()
            raise SystemExit(int(args[0]) & 0xFF)
        if name == "str":
            v = args[0]
            if type(v) is bool:
                return b"true" if v else b"false"
            if type(v) is int:
                return str(v).encode("ascii")
            if type(v) is float:
                return fmt_float(v)
            return v
        if name == "int":
            return parse_int(args[0], line)
        if name == "len":
            return len(args[0])
        if name == "range":
            return list(range(args[0], args[1]))
        if name == "map_new":
            return {}
        if name == "read_file":
            try:
                with open(args[0], "rb") as f:
                    return f.read()
            except OSError:
                raise HLPanic("cannot open file: %s" % to_display(args[0]), line)
        # Stage 10-beta: read_file_tainted(path) — same as read_file but
        # the returned str is wrapped as tainted[str]. The wrapper dict
        # format is identical to taint_mark's output.
        if name == "read_file_tainted":
            try:
                with open(args[0], "rb") as f:
                    content = f.read()
                return {"tainted": True, "value": content}
            except OSError:
                raise HLPanic("cannot open file: %s" % to_display(args[0]), line)
        if name == "write_file":
            try:
                with open(args[0], "wb") as f:
                    f.write(args[1])
                return None
            except OSError:
                raise HLPanic("cannot write file: %s" % to_display(args[0]), line)
        if name == "args":
            # BUG (deep-scan-5): this returned a fresh list COPY on every
            # call, but the native runtime returns THE process-global list
            # — mutating the result is observable in native code but
            # not under Stage-0 (a differential divergence). Return the
            # actual list so both implementations alias identically.
            return self.argv
        if name == "chr":
            if args[0] < 0 or args[0] > 255:
                raise HLPanic("chr out of range 0..255", line)
            return bytes([args[0]])
        if name == "clock_ms":
            return int(time.monotonic() * 1000)
        if name == "file_exists":
            import os
            return os.path.isfile(args[0].decode("utf-8", "replace"))
        # ----- Stage 8-alpha: ownership primitives -----
        # drop(x): semantically releases x. In Stage-0 (Python), the underlying
        # value is left for Python's GC. The binding is marked moved at compile
        # time, so this runtime path just needs to be a no-op that returns None.
        if name == "drop":
            return None
        # clone(x): deep-copy a heap value.
        if name == "clone":
            return self.deep_clone(args[0])
        # take(x): returns x's value (binding is marked moved at compile time).
        if name == "take":
            return args[0]
        # ----- Stage 10-alpha: taint tracking -----
        # tainted_args() — like args() but each element is wrapped in the
        # `tainted[str]` runtime representation: a dict {"tainted": True,
        # "value": <bytes>}. The wrapper is created here; the std.taint /
        # std.sanitize helpers consume it. See std/taint.hls.
        if name == "tainted_args":
            return [{"tainted": True, "value": a} for a in self.argv]
        # taint_mark(x) — wrap any value as tainted. The wrapper is a dict
        # so it's distinguishable from raw values (especially strings,
        # which are bytes).
        if name == "taint_mark":
            return {"tainted": True, "value": args[0]}
        # taint_unwrap(x) — extract the inner value, dropping taint.
        # The checker rejects taint_unwrap on non-tainted values, so by
        # the time we get here, args[0] is guaranteed to be a tainted
        # wrapper dict.
        if name == "taint_unwrap":
            v = args[0]
            if isinstance(v, dict) and "tainted" in v:
                return v["value"]
            # BUG-23 fix: the previous defensive fallback returned the
            # "value" field of ANY dict (including user structs that
            # happen to have a field named "value"). Since the checker
            # guarantees args[0] is a tainted[T], we panic if we get here
            # without the taint wrapper — that indicates a checker bug.
            raise HLPanic("taint_unwrap: expected tainted[T] wrapper, "
                          "got a non-tainted value", line)
        raise HLPanic("unknown builtin function: %s" % name, line)

    def deep_clone(self, v):
        """Deep-copy an HLS runtime value (Stage-0 / Python)."""
        if isinstance(v, bytes):
            return bytes(v)  # strings are immutable, shallow copy is fine
        if isinstance(v, list):
            return [self.deep_clone(x) for x in v]
        if isinstance(v, dict) and "enum" not in v:
            # map[str, T] — copy insertion-ordered dict
            new = {}
            for k in v:
                new[k] = self.deep_clone(v[k])
            return new
        if isinstance(v, dict) and "enum" in v:
            # BUG-018: this branch is currently dead because the checker's
            # `is_clone_supported` rejects enums at check time. It is
            # INTENTIONALLY KEPT for forward-compatibility — when Stage 8-beta
            # expands clone() to support structs/enums (per ROADMAP), the
            # runtime path will be exercised. Removing it now would risk a
            # silent regression later.
            return {"enum": v["enum"], "var": v["var"],
                    "data": [self.deep_clone(x) for x in v["data"]]}
        # primitives (int, float, bool, None)
        return v

    def builtin_method(self, op, t, args):
        line = self.line
        if op == "str.len":
            return len(t)
        if op == "str.byte_at":
            if args[0] < 0 or args[0] >= len(t):
                raise HLPanic("string access out of bounds", line)
            return t[args[0]]
        if op == "str.slice":
            a, b = args
            if a < 0 or b < a or b > len(t):
                raise HLPanic("invalid string slice", line)
            return t[a:b]
        if op == "str.find":
            return t.find(args[0])
        if op == "str.contains":
            return t.find(args[0]) >= 0
        if op == "str.starts_with":
            return t.startswith(args[0])
        if op == "str.ends_with":
            return t.endswith(args[0])
        if op == "str.split":
            if len(args[0]) == 0:
                raise HLPanic("empty separator not allowed", line)
            return t.split(args[0])
        if op == "str.trim":
            return t.strip(B_LOW)
        if op == "str.to_int":
            return parse_int(t, line)
        if op == "str.to_float":
            # strict: ^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$ — matches the C version
            # BUG-007 fix: must require at least one digit; "." alone is invalid.
            # BUG-4 fix (Stage 10-beta): accept optional scientific notation
            # exponent so JSON parsers can produce floats like "1e5", "1.5e-3",
            # etc. Previously the function rejected any non-digit/non-dot char
            # (including 'e'/'E'), which made json_parse("1e5") panic.
            i = 0
            if t[0:1] == b"-":
                i = 1
            if i >= len(t):
                raise HLPanic("cannot convert string to float", line)
            dots = 0
            digits = 0
            saw_exp = False
            exp_digits = 0
            while i < len(t):
                c = t[i]
                if c == 46:  # '.'
                    if saw_exp:
                        raise HLPanic("cannot convert string to float", line)
                    dots += 1
                elif 48 <= c <= 57:
                    if saw_exp:
                        exp_digits += 1
                    digits += 1
                elif c == 101 or c == 69:  # 'e' or 'E'
                    if saw_exp or digits == 0:
                        raise HLPanic("cannot convert string to float", line)
                    saw_exp = True
                    # Optional sign after e/E.
                    if i + 1 < len(t) and (t[i + 1] == 43 or t[i + 1] == 45):
                        i += 1
                    exp_digits = 0
                else:
                    raise HLPanic("cannot convert string to float", line)
                i += 1
            if dots > 1 or digits == 0:
                raise HLPanic("cannot convert string to float", line)
            if saw_exp and exp_digits == 0:
                raise HLPanic("cannot convert string to float", line)
            return float(t)
        if op == "str.to_str":
            return t
        if op == "int.to_str":
            return str(t).encode("ascii")
        if op == "int.to_float":
            return float(t)
        if op == "int.abs":
            return i64_neg(t, line) if t < 0 else t
        if op == "float.to_str":
            return fmt_float(t)
        if op == "float.to_int":
            # BUG-15 fix: range-check the conversion. Python's int() on a
            # large float (e.g. 1e20) returns a Python int exceeding int64
            # range, which would then propagate as a "valid" int and only
            # trip the next arithmetic op. Panic early here so the error
            # points to the actual source.
            # BUG (deep-scan-5): int() raises OverflowError on inf and
            # ValueError on NaN BEFORE the range check runs — the
            # interpreter crashed with a raw Python traceback while the
            # native runtime panicked cleanly. Check non-finiteness first.
            if t != t or t in (float("inf"), float("-inf")):
                raise HLPanic("float.to_int out of int64 range", line)
            r = int(t)
            if r < INT64_MIN or r > INT64_MAX:
                raise HLPanic("float.to_int out of int64 range", line)
            return r
        if op == "float.abs":
            return abs(t)
        if op == "bool.to_str":
            return b"true" if t else b"false"
        if op == "list.len":
            return len(t)
        if op == "list.push":
            t.append(args[0])
            return None
        if op == "list.get":
            if args[0] < 0 or args[0] >= len(t):
                raise HLPanic("array access out of bounds", line)
            return t[args[0]]
        if op == "list.pop":
            if len(t) == 0:
                # BUG-SC-9 fix: "array access out of bounds" is misleading
                # for pop() — the user called pop() on an empty list, not
                # an index operation. Report the actual problem.
                raise HLPanic("pop from empty list", line)
            return t.pop()
        if op == "list.set":
            if args[0] < 0 or args[0] >= len(t):
                raise HLPanic("array access out of bounds", line)
            t[args[0]] = args[1]
            return None
        if op == "map.len":
            return len(t)
        if op == "map.set":
            t[args[0]] = args[1]
            return None
        if op == "map.get_or":
            v = t.get(args[0])
            return args[1] if v is None else v
        if op == "map.has":
            return args[0] in t
        if op == "map.keys":
            return list(t.keys())
        raise HLPanic("unknown builtin method: %s" % op, line)
