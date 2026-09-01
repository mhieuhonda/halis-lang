"""Stage-0 evaluator for HLS (v0.3) — adds enum, match, `?` operator.

Runtime values:
  int   -> int (Python, kept within int64 range by checked arithmetic)
  float -> float, bool -> bool, str -> bytes
  list[T]      -> list
  map[str,T]   -> dict (insertion-ordered), struct -> dict {field: value}
  enum         -> dict {"enum": <name>, "var": <variant>, "data": [payloads]}
"""
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
        if a == 0 or a != a:
            return float("nan")
        return float("inf") if (a > 0) == (b >= 0) else float("-inf")


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
        env = [{}]
        if fn["struct"] is not None:
            sn, _, sm = fn["params"][0]
            env[0][sn] = [args[0], sm]
            params = fn["params"][1:]
            args = args[1:]
        else:
            params = fn["params"]
        for (pn, _, _), v in zip(params, args):
            env[0][pn] = [v, False]
        try:
            self.exec_stmts(fn["body"], env)
        except ReturnSig as r:
            return r.value
        return None

    # ---------- statements ----------
    def exec_stmts(self, stmts, env):
        for s in stmts:
            self.exec_stmt(s, env)

    def exec_stmt(self, s, env):
        self.line = s.get("line", 0)
        k = s["k"]
        if k == "let":
            env[-1][s["name"]] = [self.eval_expr(s["value"], env), s["mut"]]
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
                env.append({s["var"]: [lst[i], False]})
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
        if k == "mapnew":
            return {}
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
                    env[-1][bname] = [s_data[i] if i < len(s_data) else None, False]
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
        if name == "write_file":
            try:
                with open(args[0], "wb") as f:
                    f.write(args[1])
                return None
            except OSError:
                raise HLPanic("cannot write file: %s" % to_display(args[0]), line)
        if name == "args":
            return list(self.argv)
        if name == "chr":
            if args[0] < 0 or args[0] > 255:
                raise HLPanic("chr out of range 0..255", line)
            return bytes([args[0]])
        if name == "clock_ms":
            return int(time.monotonic() * 1000)
        if name == "file_exists":
            import os
            return os.path.isfile(args[0].decode("utf-8", "replace"))
        raise HLPanic("unknown builtin function: %s" % name, line)

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
            # strict: ^-?[0-9]+(\.[0-9]+)?$ — matches the C version exactly
            i = 0
            if t[0:1] == b"-":
                i = 1
            if i >= len(t):
                raise HLPanic("cannot convert string to float", line)
            dots = 0
            while i < len(t):
                c = t[i]
                if c == 46:
                    dots += 1
                elif not (48 <= c <= 57):
                    raise HLPanic("cannot convert string to float", line)
                i += 1
            if dots > 1:
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
            return int(t)
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
                raise HLPanic("array access out of bounds", line)
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
