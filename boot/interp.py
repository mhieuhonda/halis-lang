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
import os
import sys
import time

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
B_LOW = bytes(range(0x21))  # bytes <= 0x20 used by trim

# Stage 10 release: process-global sandbox root. When non-None, all
# filesystem builtins (read_file, read_file_tainted, write_file,
# file_exists) reject any path that does not resolve INSIDE this
# directory. The sandbox is set via boot.py --sandbox DIR. The C
# runtime mirrors this with hl_set_sandbox_root().
SANDBOX_ROOT = None


def _set_sandbox_root(path):
    """Set the process-global sandbox root. Called by boot.py --sandbox."""
    global SANDBOX_ROOT
    if path is None:
        SANDBOX_ROOT = None
        return
    # Canonicalise to an absolute, symlink-resolved path so that
    # ../escape attempts are caught after resolution (NOT before,
    # which would still allow the symlink to point outside).
    SANDBOX_ROOT = os.path.realpath(path)


def _sandbox_check(path_bytes):
    """If SANDBOX_ROOT is set, verify that `path_bytes` resolves inside
    the sandbox. Raises HLPanic with a clean message otherwise.

    The check is performed AFTER realpath resolution, so paths like
    "../etc/passwd" or symlinks pointing outside the sandbox are
    rejected. We DO allow the path to NOT exist (e.g. file_exists
    probing) — we just check that IF it existed, it would be inside
    the sandbox.

    SCAN-A fix: decode bytes as latin-1 (a 1:1 byte->str mapping) so
    the realpath comparison runs on the SAME byte sequence as `open()`
    will use. The previous `errors="replace"` decoded non-UTF-8 bytes
    to U+FFFD, so the realpath check ran on a different path than the
    actual open() call — a non-UTF-8-named symlink inside the sandbox
    pointing outside wasn't caught by the check, but `open()` would
    still follow it.
    """
    if SANDBOX_ROOT is None:
        return
    # bytes -> str using latin-1 (1:1 byte->str mapping) so the realpath
    # check uses the EXACT same byte sequence as `open()` will.
    if isinstance(path_bytes, bytes):
        p = path_bytes.decode("latin-1")
    else:
        p = str(path_bytes)
    if not os.path.isabs(p):
        p = os.path.join(os.getcwd(), p)
    # realpath resolves symlinks; if the path does not exist, it
    # resolves as far as possible (the existing prefix) and leaves
    # the rest verbatim. That is enough: if any component of the
    # existing prefix points outside the sandbox, we reject.
    resolved = os.path.realpath(p)
    # Common-prefix check: SANDBOX_ROOT must be a prefix of `resolved`,
    # AND the character after the prefix must be a separator (or end
    # of string) — otherwise "/sandbox_evil" would be allowed inside
    # "/sandbox".
    sb = SANDBOX_ROOT
    if resolved == sb:
        return
    if not resolved.startswith(sb + os.sep):
        raise HLPanic("sandbox violation: path '%s' resolves outside the sandbox"
                      % to_display(path_bytes), 0)


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


# Stage 9 release (v0.20.0-alpha): HalisRNG — a 64-bit LCG shared between
# the Stage-0 interpreter and the native C runtime. Same constants, same
# bit-mask → same sequence for the same seed. Critical for differential
# testing (a test using rand_seed + rand_int / rand_float must produce
# identical output in both backends; otherwise the suite would fail).
#
# Algorithm: Knuth's LCG with the glibc/MMIX Taussian-Lewis constants.
#   state = state * 6364136223846793005 + 1442695040888963407   (mod 2^64)
#   rand_int(max) = state % max   (max > 0)
#   rand_float() = (state >> 11) / 2^53   (53 bits of randomness)
# The state is masked to 64 bits with & 0xFFFFFFFFFFFFFFFF to mirror C's
# uint64_t overflow. Seed 0 is normalised to 1 because xorshift-style
# alternatives would not — but the LCG actually accepts 0 (it just stays
# at 0x...407 forever); we normalise anyway so the seed "0" does not
# produce a degenerate sequence.
class HalisRNG:
    MASK = (1 << 64) - 1
    A = 6364136223846793005
    C = 1442695040888963407

    def __init__(self):
        self.state = 1  # nonzero default; same as native runtime

    def seed(self, s):
        # HLS ints are 64-bit signed; mask to 64 bits to mirror C uint64.
        self.state = s & self.MASK
        if self.state == 0:
            self.state = 1

    def _next(self):
        self.state = (self.state * self.A + self.C) & self.MASK
        return self.state

    def randrange(self, max):
        # Caller guarantees max > 0 (the checker raises otherwise).
        return self._next() % max

    def random(self):
        # 53 bits of randomness — full precision of an IEEE double's
        # significand. Matches the native runtime's calculation.
        return (self._next() >> 11) / (1 << 53)


class Interp:
    def __init__(self, program, argv, out):
        self.p = program
        self.fns = program["fns"]
        self.structs = program["structs"]
        self.enums = program.get("enums", {})
        self.argv = argv  # list[bytes]
        self.out = out
        self.line = 0
        # Stage 9 release (v0.20.0-alpha): process-wide PRNG state for the
        # Rand effect. Uses a 64-bit LCG with the same constants as the
        # native runtime (Knuth LCG: state = state * 6364136223846793005
        # + 1442695040888963407, masked to 64 bits). This makes the
        # sequence DETERMINISTIC across implementations: the same seed
        # produces the same sequence of ints and floats in both Stage-0
        # (Python) and the native binary (C). Crucial for differential
        # testing — tests using rand_seed + rand_int/rand_float produce
        # identical output in both backends.
        self.rand_state = HalisRNG()

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
                # Deep-scan fix (C8): the previous code passed `id(v)` for
                # list/map/struct args. That's a raw CPython heap address,
                # which the C function would dereference as garbage — a
                # soundness hole. Now we panic with a clean error: opaque
                # pointer args are NOT supported (they require a real
                # ABI/marshalling layer that Stage 15-alpha doesn't have).
                # The user must declare extern fns with primitive types only
                # (int, float, bool, str) and marshal complex types via str.
                raise HLPanic(
                    "extern call to '%s': argument of type %s is not "
                    "supported (only int, float, bool, str args are "
                    "allowed in extern FFI; use a string-encoded form "
                    "for complex data)" % (name, pt),
                    getattr(self, "line", 0))
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
            # Deep-scan-7 fix: a malicious or buggy program calling
            # `range(0, INT64_MAX)` would attempt to materialise a
            # 9-quintillion-element list, exhausting memory. Cap at
            # a reasonable limit (1M elements) and panic with a clear
            # message otherwise. boot.py's main thread catches
            # MemoryError, but list(range(...)) raises MemoryError
            # AT THE PYTHON LEVEL — we want a clean HLPanic instead.
            a, b = int(args[0]), int(args[1])
            count = b - a if b > a else 0
            RANGE_MAX = 1_000_000
            if count > RANGE_MAX:
                raise HLPanic(
                    "range(%d, %d) would produce %d elements (limit %d) — "
                    "use an explicit counter loop for large ranges"
                    % (a, b, count, RANGE_MAX), line)
            return list(range(a, b))
        if name == "map_new":
            return {}
        if name == "read_file":
            _sandbox_check(args[0])
            try:
                with open(args[0], "rb") as f:
                    return f.read()
            except OSError:
                raise HLPanic("cannot open file: %s" % to_display(args[0]), line)
        # Stage 10-beta: read_file_tainted(path) — same as read_file but
        # the returned str is wrapped as tainted[str]. The wrapper dict
        # format is identical to taint_mark's output.
        if name == "read_file_tainted":
            _sandbox_check(args[0])
            try:
                with open(args[0], "rb") as f:
                    content = f.read()
                return {"tainted": True, "value": content}
            except OSError:
                raise HLPanic("cannot open file: %s" % to_display(args[0]), line)
        # Stage 10 release: read_line() -> tainted[str] — third taint source.
        # Reads one line from stdin (newline stripped). The result is always
        # tainted because stdin is untrusted input. EOF returns an empty
        # tainted string (mirrors fgets() semantics in the C runtime).
        if name == "read_line":
            raw = sys.stdin.buffer.readline()
            # Strip a trailing newline (matches the C runtime's hl_read_line).
            if raw.endswith(b"\n"):
                raw = raw[:-1]
                # Also strip a trailing \r if present (CRLF line endings).
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
            return {"tainted": True, "value": raw}
        if name == "write_file":
            _sandbox_check(args[0])
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
            _sandbox_check(args[0])
            # Deep-scan fix (H2): pass `args[0]` (bytes) directly to
            # os.path.isfile — Python's os.path.isfile accepts bytes.
            # The old code decoded with errors="replace", which substituted
            # U+FFFD for non-UTF-8 bytes, so the interpreter checked a
            # DIFFERENT path than the native runtime (which passes raw
            # bytes to stat()). Files with non-UTF-8 names diverged.
            return os.path.isfile(args[0])
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
            # Deep-scan-7 fix: the previous check `"tainted" in v` matched
            # any dict with a field literally named "tainted" — including
            # user structs with a `tainted: int` field. Tighten the check:
            # require BOTH "tainted" AND "value" keys, AND "tainted" must
            # be the boolean True (the taint-mark builtin sets it to True).
            if (isinstance(v, dict) and "tainted" in v and "value" in v
                    and v["tainted"] is True):
                return v["value"]
            # BUG-23 fix: the previous defensive fallback returned the
            # "value" field of ANY dict (including user structs that
            # happen to have a field named "value"). Since the checker
            # guarantees args[0] is a tainted[T], we panic if we get here
            # without the taint wrapper — that indicates a checker bug.
            raise HLPanic("taint_unwrap: expected tainted[T] wrapper, "
                          "got a non-tainted value (got %s)"
                          % type(v).__name__, line)
        # ----- Stage 9 release (v0.20.0-alpha): Net / Rand / Proc builtins -----
        # The interpreter implementations mirror the native runtime in
        # src/hlc.hls exactly, so differential testing passes.
        # rand_int(max: int) -> int — uniform random int in [0, max).
        # Panics on max <= 0 to keep the bound well-defined. Uses the
        # shared HalisRNG LCG so the sequence is identical to the native
        # runtime for the same seed.
        if name == "rand_int":
            if args[0] <= 0:
                raise HLPanic("rand_int() requires a positive max (got %d)"
                              % args[0], line)
            return self.rand_state.randrange(args[0])
        # rand_float() -> float — uniform random float in [0.0, 1.0).
        # Uses the same PRNG state as rand_int; 53 bits of randomness.
        if name == "rand_float":
            return self.rand_state.random()
        # rand_seed(s: int) -> void — seed the PRNG. Same seed produces
        # the same sequence in both the interpreter and the native
        # runtime (the constants and bit-masking are identical).
        if name == "rand_seed":
            self.rand_state.seed(args[0])
            return None
        # net_lookup(host: str) -> str — DNS resolution. Returns the
        # first IPv4 address as a string. Panics on failure (DNS error
        # or no A records). The interpreter uses Python's socket module
        # — the native runtime uses getaddrinfo directly.
        if name == "net_lookup":
            import socket
            host = args[0].decode("utf-8", "replace")
            try:
                infos = socket.getaddrinfo(host, None, socket.AF_INET)
                for fam, _, _, _, sa in infos:
                    if fam == socket.AF_INET:
                        return sa[0].encode("ascii")
                raise HLPanic("net_lookup: no A records for %s"
                              % to_display(args[0]), line)
            except socket.gaierror as ex:
                raise HLPanic("net_lookup: DNS resolution failed for %s: %s"
                              % (to_display(args[0]), str(ex)), line)
            except OSError as ex:
                # Deep-scan fix (C2): connection timeouts, refused
                # connections, and other non-gaierror OSErrors used to
                # propagate as raw Python tracebacks while the native
                # runtime panicked cleanly. Catch the broader OSError
                # family for differential parity.
                raise HLPanic("net_lookup: network error for %s: %s"
                              % (to_display(args[0]), str(ex)), line)
        # proc_exec(cmd: str) -> int — run a shell command. Returns the
        # exit code (0 on success, 1..255 on failure). Uses os.system()
        # so the command runs in a subshell, matching the C runtime's
        # system() call. Tainted command strings are rejected at
        # check time (proc_exec is a taint sink for argument 0).
        if name == "proc_exec":
            import os
            cmd = args[0].decode("utf-8", "replace")
            rc = os.system(cmd)
            # Deep-scan-7 fix: os.WIFEXITED / WEXITSTATUS / WTERMSIG
            # are POSIX-only macros. On Windows, os.system returns the
            # exit code directly (not a status word). Detect the
            # platform and handle both cases so the interpreter
            # produces the same result as the C runtime on every OS.
            if sys.platform == "win32":
                # Windows: rc is already the exit code (0..255).
                # Encode signal-like values as 128 + signum for parity.
                if rc < 0:
                    return 128 + (-rc)
                return rc & 0xFF
            # POSIX: os.system returns a status word; the exit code is
            # the high byte (WEXITSTATUS).
            if os.WIFEXITED(rc):
                return os.WEXITSTATUS(rc)
            # Killed by signal — encode as 128 + signum, like shells.
            return 128 + os.WTERMSIG(rc)
        raise HLPanic("unknown builtin function: %s" % name, line)

    def deep_clone(self, v):
        """Deep-copy an HLS runtime value (Stage-0 / Python)."""
        if isinstance(v, bytes):
            return bytes(v)  # strings are immutable, shallow copy is fine
        if isinstance(v, list):
            return [self.deep_clone(x) for x in v]
        if isinstance(v, dict):
            # SCAN-A fix: distinguish enum values from struct values. An
            # enum value is `{"enum": name, "var": variant, "data": [...]}` —
            # check for ALL THREE keys. A struct value with a field literally
            # named "enum" would be `{"enum": value}` — missing "var" and
            # "data" — so it must be treated as a struct (a plain dict).
            if "enum" in v and "var" in v and "data" in v:
                return {"enum": v["enum"], "var": v["var"],
                        "data": [self.deep_clone(x) for x in v["data"]]}
            # map[str, T] — copy insertion-ordered dict. Also covers
            # struct values (which are dicts of field_name -> value).
            new = {}
            for k in v:
                new[k] = self.deep_clone(v[k])
            return new
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
            # Deep-scan-7 fix: also accept leading `+` for parity with C's
            # strtod() and Python's float() — both accept "+1.5". The old
            # code only stripped a leading `-`, so "+1.5".to_float() panicked
            # on the `+`. This caused a differential testing divergence
            # between the interpreter and the C runtime.
            i = 0
            if t[0:1] == b"-":
                i = 1
            elif t[0:1] == b"+":
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
