"""Interp mixin (builtin_method) - verbatim segment of the original
boot/interp.py Interp class (lines 2922..3096), split for
maintainability. The final Interp class assembles all mixins in
boot/interp_parts/interp.py - behavior is unchanged."""
from .rt import (
    B_LOW, HLPanic, INT64_MAX, INT64_MIN, fmt_float, i64_neg, parse_int,
)

class InterpBuiltin_method(object):
    def builtin_method(self, op, t, args, arg_nodes=None):
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
        # ----- Stage 16 (v0.27.0-alpha): Chan / Task methods -----
        if op == "chan.send" or op == "chan.try_send":
            # Boundary ownership rule: an owned message is deep-copied at
            # the send boundary unless it is syntactically clone(...)
            # (already a private deep copy) — the checker already
            # rejected bare variable reads. This mirrors the native
            # codegen's gen path for chan.send/chan.try_send exactly
            # (deep-scan-10: the interpreter previously deep-copied even
            # clone(...) results — harmless but asymmetric with the
            # native move semantics, and wasteful).
            v = args[0]
            is_clone = False
            if arg_nodes:
                a0 = arg_nodes[0]
                if a0.get("k") == "call" and a0.get("name") == "clone":
                    is_clone = True
            if not is_clone and isinstance(v, (list, dict)):
                v = self.deep_clone(v)
            if op == "chan.send":
                self.conc.send(t, v)
                return None
            return self.conc.try_send(t, v)
        if op == "chan.recv":
            return self.conc.recv(t, line)
        if op == "chan.recv_or":
            # Non-blocking recv: message if pending, else the caller's
            # default. The default never crosses a task boundary.
            return self.conc.recv_or(t, args[0])
        if op == "chan.len":
            with self.conc.cv:
                return len(t.q)
        if op == "task.join":
            return self.conc.join(t, line)
        raise HLPanic("unknown builtin method: %s" % op, line)
