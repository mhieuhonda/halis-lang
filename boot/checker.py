"""Stage-0 type checker & effects analyzer for HLS. Conforms to SPEC.md sections 3-9.

Side effect: annotates the AST so the evaluator can run quickly:
  - every expression has e['t'] = type (or 'never')
  - e['rc'] = ('user', key) | ('builtin', name) for function calls
  - e['rm'] = ('user', key) | ('builtin', op) for method calls
  - program['edges'] = {fn_key: set(callees)} for effects analysis
"""
from .lexer import HLError

INT64_MAX = 9223372036854775807


def is_list(t):
    return t.startswith("list[")


def list_elem(t):
    return t[5:-1]


def is_map(t):
    return t.startswith("map[str, ")


def map_val(t):
    return t[9:-1]


BUILTIN_FNS = {
    "print", "println", "panic", "exit", "str", "int", "len", "range",
    "map_new", "read_file", "write_file", "args", "clock_ms", "chr",
    "file_exists",
}
IO_BUILTINS = {"print", "println", "read_file", "write_file", "exit", "args", "clock_ms", "file_exists"}

STR_M = {
    "len": ([], "int"), "byte_at": (["int"], "int"),
    "slice": (["int", "int"], "str"), "find": (["str"], "int"),
    "contains": (["str"], "bool"), "starts_with": (["str"], "bool"),
    "ends_with": (["str"], "bool"), "split": (["str"], "list[str]"),
    "trim": ([], "str"), "to_int": ([], "int"), "to_str": ([], "str"),
    "to_float": ([], "float"),
}
INT_M = {"to_str": "str", "to_float": "float", "abs": "int"}
FLOAT_M = {"to_str": "str", "to_int": "int", "abs": "float"}
BOOL_M = {"to_str": "str"}


class Checker:
    def __init__(self, program):
        self.p = program
        self.structs = program["structs"]
        self.fns = program["fns"]
        self.edges = {}
        self.methods = {}  # struct -> {meth: key}
        self.cur_fn = None

    # ---------- utilities ----------
    def err(self, msg, node):
        raise HLError(msg, node.get("line", 0), 0)

    def type_exists(self, t, node):
        if t in ("int", "float", "bool", "str"):
            return True
        if is_list(t):
            return self.type_exists(list_elem(t), node)
        if is_map(t):
            return self.type_exists(map_val(t), node)
        return t in self.structs

    def require_type(self, t, node, what):
        if t == "void":
            self.err("cannot use 'void' as %s" % what, node)
        if not self.type_exists(t, node):
            self.err("type does not exist: %s" % t, node)

    # ---------- lifecycle ----------
    def check(self):
        # 1. group methods by struct
        for key, fn in self.fns.items():
            if fn["struct"] is not None:
                m = self.methods.setdefault(fn["struct"], {})
                m[fn["name"]] = key
        # 2. check declarations
        for name, st in self.structs.items():
            for fname, ftype in st["fields"]:
                self.require_type(ftype, st, "struct field type")
        for key, fn in self.fns.items():
            if fn["struct"] is None:
                if fn["name"] in BUILTIN_FNS:
                    self.err("cannot redefine builtin function: %s" % fn["name"], fn)
            else:
                if fn["struct"] not in self.structs:
                    self.err("impl for non-existent struct: %s" % fn["struct"], fn)
                if not fn["params"]:
                    self.err("method must have 'self' as first parameter", fn)
                sname, stype, _ = fn["params"][0]
                if sname != "self" or stype != fn["struct"]:
                    self.err("method must have first parameter 'self: %s'"
                             % fn["struct"], fn)
            for pn, pt, _ in fn["params"]:
                self.require_type(pt, fn, "parameter type")
            if fn["ret"] != "void" and not self.type_exists(fn["ret"], fn):
                self.err("return type does not exist: %s" % fn["ret"], fn)
            self.edges[key] = set()
        if "main" not in self.fns:
            self.err("missing main function", {"line": 1})
        mainf = self.fns["main"]
        if mainf["struct"] is not None:
            self.err("main cannot be a method", mainf)
        if mainf["params"]:
            self.err("main cannot have parameters", mainf)
        if mainf["ret"] not in ("int", "void"):
            self.err("main must return 'int' or have no return type", mainf)
        # 3. check function bodies
        for key, fn in self.fns.items():
            self.check_fn(key, fn)
        # 4. effects analysis (fixpoint on the call graph)
        self.check_effects()

    # ---------- environment ----------
    def new_env(self, fn):
        env = [{}]
        if fn["struct"] is not None:
            sname, stype, smut = fn["params"][0]
            env[0][sname] = [stype, smut]
            params = fn["params"][1:]
        else:
            params = fn["params"]
        for pn, pt, pm in params:
            if pn in env[0]:
                self.err("duplicate parameter name: %s" % pn, fn)
            env[0][pn] = [pt, pm]
        return env

    def lookup(self, env, name):
        for scope in reversed(env):
            if name in scope:
                return scope[name]
        return None

    def check_fn(self, key, fn):
        self.cur_fn = key
        env = self.new_env(fn)
        self.check_stmts(fn["body"], env, fn, False)
        if fn["ret"] != "void" and not self.all_return(fn["body"]):
            self.err("function '%s' does not return on all paths" % fn["name"], fn)

    def all_return(self, stmts):
        if not stmts:
            return False
        last = stmts[-1]
        if last["k"] == "return":
            return True
        if last["k"] == "expr" and last["e"].get("t") == "never":
            return True
        if last["k"] == "if" and last["els"] is not None:
            return self.all_return(last["then"]) and self.all_return(last["els"])
        return False

    def check_stmts(self, stmts, env, fn, in_loop):
        for s in stmts:
            self.check_stmt(s, env, fn, in_loop)

    def child(self, env):
        env.append({})
        return env

    # ---------- statements ----------
    def check_stmt(self, s, env, fn, in_loop):
        k = s["k"]
        if k == "let":
            self.require_type(s["t"], s, "variable type")
            if self.lookup(env, s["name"]) is not None:
                self.err("shadowing not allowed: %s" % s["name"], s)
            vt = self.check_expr(s["value"], env, s["t"])
            if vt == "never":
                # `let x: T = panic()` is sound: x is unreachable.
                # Mark binding with the declared type so later references type-check.
                env[-1][s["name"]] = [s["t"], s["mut"]]
                return
            if vt != s["t"]:
                self.err("type mismatch: declared %s but got %s"
                         % (s["t"], vt), s)
            env[-1][s["name"]] = [s["t"], s["mut"]]
        elif k == "assign":
            self.check_assign(s, env)
        elif k == "if":
            ct = self.check_expr(s["cond"], env, None)
            if ct != "bool":
                self.err("if condition must be bool, got %s" % ct, s)
            self.child(env)
            self.check_stmts(s["then"], env, fn, in_loop)
            env.pop()
            if s["els"] is not None:
                self.child(env)
                self.check_stmts(s["els"], env, fn, in_loop)
                env.pop()
        elif k == "while":
            ct = self.check_expr(s["cond"], env, None)
            if ct != "bool":
                self.err("while condition must be bool, got %s" % ct, s)
            self.child(env)
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
        elif k == "for":
            it = self.check_expr(s["iter"], env, None)
            if not is_list(it):
                self.err("for-in expression must be a list, got %s" % it, s)
            elem = list_elem(it)
            if s["vtype"] != elem:
                self.err("loop variable type %s does not match element %s"
                         % (s["vtype"], elem), s)
            self.child(env)
            env[-1][s["var"]] = [elem, False]
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
        elif k == "return":
            if fn["ret"] == "void":
                if s["value"] is not None:
                    self.err("void function cannot return a value", s)
            else:
                if s["value"] is None:
                    self.err("function returning %s must return a value" % fn["ret"], s)
                vt = self.check_expr(s["value"], env, fn["ret"])
                if vt != fn["ret"] and vt != "never":
                    self.err("return type mismatch: expected %s, got %s"
                             % (fn["ret"], vt), s)
        elif k == "break":
            if not in_loop:
                self.err("break only allowed inside a loop", s)
        elif k == "continue":
            if not in_loop:
                self.err("continue only allowed inside a loop", s)
        elif k == "expr":
            self.check_expr(s["e"], env, None)
        else:
            self.err("unknown statement: %s" % k, s)

    def check_assign(self, s, env):
        tgt = s["target"]
        # find root binding
        root = tgt
        while root["k"] in ("field", "index"):
            root = root["target"]
        binding = self.lookup(env, root["name"])
        if binding is None:
            self.err("variable does not exist: %s" % root["name"], s)
        # 'mut' only governs REASSIGNMENT of the binding (name = v).
        # Field/index assignment mutates CONTENTS through a reference — no mut needed.
        if tgt["k"] == "ident" and not binding[1]:
            self.err("cannot reassign immutable variable: %s" % root["name"], s)
        tt = self.check_lvalue(tgt, env)
        vt = self.check_expr(s["value"], env, tt)
        if vt == "never":
            # `x = panic()` is sound: unreachable. Treat as no-op for type checking.
            return
        if vt != tt:
            self.err("type mismatch on assignment: expected %s, got %s" % (tt, vt), s)

    def check_lvalue(self, e, env):
        if e["k"] == "ident":
            b = self.lookup(env, e["name"])
            return b[0]
        if e["k"] == "field":
            bt = self.check_expr(e["target"], env, None)
            if bt not in self.structs:
                self.err("cannot access field on type %s" % bt, e)
            for fname, ftype in self.structs[bt]["fields"]:
                if fname == e["name"]:
                    return ftype
            self.err("struct %s has no field %s" % (bt, e["name"]), e)
        if e["k"] == "index":
            tt = self.check_expr(e["target"], env, None)
            if not is_list(tt):
                self.err("cannot use index on type %s" % tt, e)
            it = self.check_expr(e["idx"], env, None)
            if it != "int":
                self.err("index must be int, got %s" % it, e)
            return list_elem(tt)
        self.err("invalid lvalue", e)

    # ---------- expressions ----------
    def check_expr(self, e, env, expected):
        k = e["k"]
        if k == "int":
            if e["v"] > INT64_MAX:
                self.err("integer literal too large (exceeds int64)", e)
            e["t"] = "int"
        elif k == "float":
            e["t"] = "float"
        elif k == "bool":
            e["t"] = "bool"
        elif k == "str":
            e["t"] = "str"
        elif k == "ident":
            b = self.lookup(env, e["name"])
            if b is None:
                self.err("variable does not exist: %s" % e["name"], e)
            e["t"] = b[0]
        elif k == "bin":
            e["t"] = self.check_bin(e, env)
        elif k == "un":
            vt = self.check_expr(e["e"], env, None)
            if e["op"] == "!":
                if vt != "bool":
                    self.err("! operator requires bool, got %s" % vt, e)
                e["t"] = "bool"
            else:
                if vt not in ("int", "float"):
                    self.err("- operator requires int/float, got %s" % vt, e)
                e["t"] = vt
        elif k == "index":
            tt = self.check_expr(e["target"], env, None)
            if tt == "never":
                self.err("never value cannot be used in expression", e)
            if not is_list(tt):
                self.err("cannot use index on type %s" % tt, e)
            it = self.check_expr(e["idx"], env, None)
            if it != "int":
                self.err("index must be int, got %s" % it, e)
            e["t"] = list_elem(tt)
        elif k == "field":
            tt = self.check_expr(e["target"], env, None)
            if tt == "never":
                self.err("never value cannot be used in expression", e)
            if tt not in self.structs:
                self.err("cannot access field on type %s" % tt, e)
            e["t"] = None
            for fname, ftype in self.structs[tt]["fields"]:
                if fname == e["name"]:
                    e["t"] = ftype
                    break
            if e["t"] is None:
                self.err("struct %s has no field %s" % (tt, e["name"]), e)
        elif k == "call":
            e["t"] = self.check_call(e, env, expected)
        elif k == "method":
            e["t"] = self.check_method(e, env)
        elif k == "listlit":
            e["t"] = self.check_listlit(e, env, expected)
        elif k == "structlit":
            e["t"] = self.check_structlit(e, env)
        elif k == "mapnew":
            if expected is None or not is_map(expected):
                self.err("map_new() requires a 'map[str, T]' type in the surrounding context", e)
            e["t"] = expected
        else:
            self.err("unknown expression: %s" % k, e)
        return e["t"]

    def check_bin(self, e, env):
        lt = self.check_expr(e["l"], env, None)
        rt = self.check_expr(e["r"], env, None)
        if lt == "never" or rt == "never":
            self.err("never value cannot be used in expression", e)
        op = e["op"]
        if op in ("||", "&&"):
            if lt != "bool" or rt != "bool":
                self.err("%s operator requires bool, got %s and %s" % (op, lt, rt), e)
            return "bool"
        if op in ("==", "!="):
            if lt != rt or lt not in ("int", "float", "bool", "str"):
                self.err("cannot compare == between %s and %s" % (lt, rt), e)
            return "bool"
        if op in ("<", "<=", ">", ">="):
            if lt != rt or lt not in ("int", "float", "str"):
                self.err("cannot compare ordering between %s and %s" % (lt, rt), e)
            return "bool"
        if op == "+":
            if lt == "int" and rt == "int":
                return "int"
            if lt == "float" and rt == "float":
                return "float"
            if lt == "str" and rt == "str":
                return "str"
            self.err("+ operator does not support %s and %s" % (lt, rt), e)
        if op in ("-", "*", "/", "%"):
            if lt == "int" and rt == "int":
                return "int"
            if lt == "float" and rt == "float":
                return "float"
            self.err("%s operator does not support %s and %s" % (op, lt, rt), e)
        self.err("unknown operator: %s" % op, e)

    def check_listlit(self, e, env, expected):
        elem = None
        if expected is not None and is_list(expected):
            elem = list_elem(expected)
        if elem is None:
            if not e["items"]:
                self.err("empty list literal requires a type in the surrounding context", e)
            elem = self.check_expr(e["items"][0], env, None)
            if elem in ("void", "never"):
                self.err("list element cannot have type %s" % elem, e)
        for it in e["items"]:
            vt = self.check_expr(it, env, elem)
            if vt != elem:
                self.err("list element mismatch: expected %s, got %s"
                         % (elem, vt), it)
        return "list[%s]" % elem

    def check_structlit(self, e, env):
        name = e["name"]
        if name not in self.structs:
            self.err("struct does not exist: %s" % name, e)
        fields = self.structs[name]["fields"]
        if len(e["fields"]) != len(fields):
            self.err("struct literal %s requires exactly %d fields, got %d"
                     % (name, len(fields), len(e["fields"])), e)
        for (lfname, lfe), (fname, ftype) in zip(e["fields"], fields):
            if lfname != fname:
                self.err("field order mismatch: expected '%s', got '%s'"
                         % (fname, lfname), e)
            vt = self.check_expr(lfe, env, ftype)
            if vt != ftype:
                self.err("field type %s mismatch: expected %s, got %s"
                         % (fname, ftype, vt), e)
        return name

    def check_call(self, e, env, expected):
        name = e["name"]
        args = e["args"]
        if name in BUILTIN_FNS:
            e["rc"] = ("builtin", name)
            return self.check_builtin_call(name, e, env, expected)
        if name in self.fns:
            fn = self.fns[name]
            if fn["struct"] is not None:
                self.err("method must be called through an object: %s" % name, e)
            e["rc"] = ("user", name)
            self.edges[self.cur_fn].add(name)
            if len(args) != len(fn["params"]):
                self.err("function %s expects %d arguments, got %d"
                         % (name, len(fn["params"]), len(args)), e)
            for a, (pn, pt, _) in zip(args, fn["params"]):
                at = self.check_expr(a, env, pt)
                if at == "never":
                    # `foo(panic())` is sound: argument is unreachable.
                    continue
                if at != pt:
                    self.err("argument '%s' of %s expects %s, got %s"
                             % (pn, name, pt, at), a)
            return fn["ret"]
        self.err("function does not exist: %s" % name, e)

    def check_builtin_call(self, name, e, env, expected):
        args = e["args"]

        def need(n):
            if len(args) != n:
                self.err("%s expects %d arguments, got %d" % (name, n, len(args)), e)

        def argt(i, want):
            at = self.check_expr(args[i], env, want if want is not None else None)
            if at == "never":
                self.err("never value cannot be used as an argument", e)
            if want is not None and at != want:
                self.err("%s expects argument %s, got %s" % (name, want, at), e)
            return at

        if name in ("print", "println"):
            need(1)
            argt(0, "str")
            self.edges[self.cur_fn].add("b:print")
            return "void"
        if name == "panic":
            need(1)
            argt(0, "str")
            return "never"
        if name == "exit":
            need(1)
            argt(0, "int")
            self.edges[self.cur_fn].add("b:exit")
            return "never"
        if name == "str":
            need(1)
            at = argt(0, None)
            if at not in ("int", "float", "bool", "str"):
                self.err("str() does not support type %s" % at, e)
            return "str"
        if name == "int":
            need(1)
            argt(0, "str")
            return "int"
        if name == "len":
            need(1)
            at = argt(0, None)
            if at not in ("str",) and not is_list(at) and not is_map(at):
                self.err("len() does not support type %s" % at, e)
            return "int"
        if name == "range":
            need(2)
            argt(0, "int")
            argt(1, "int")
            return "list[int]"
        if name == "map_new":
            need(0)
            if expected is None or not is_map(expected):
                self.err("map_new() requires a 'map[str, T]' type in the surrounding context", e)
            return expected
        if name == "read_file":
            need(1)
            argt(0, "str")
            self.edges[self.cur_fn].add("b:read_file")
            return "str"
        if name == "write_file":
            need(2)
            argt(0, "str")
            argt(1, "str")
            self.edges[self.cur_fn].add("b:write_file")
            return "void"
        if name == "args":
            need(0)
            self.edges[self.cur_fn].add("b:args")
            return "list[str]"
        if name == "chr":
            need(1)
            argt(0, "int")
            return "str"
        if name == "clock_ms":
            need(0)
            self.edges[self.cur_fn].add("b:clock_ms")
            return "int"
        if name == "file_exists":
            need(1)
            argt(0, "str")
            self.edges[self.cur_fn].add("b:file_exists")
            return "bool"
        self.err("unknown builtin function: %s" % name, e)

    def check_method(self, e, env):
        tt = self.check_expr(e["target"], env, None)
        if tt == "never":
            self.err("never value cannot be used in expression", e)
        name = e["name"]
        args = e["args"]
        if tt in self.structs:
            m = self.methods.get(tt, {})
            if name not in m:
                self.err("struct %s has no method %s" % (tt, name), e)
            key = m[name]
            e["rm"] = ("user", key)
            fn = self.fns[key]
            params = fn["params"][1:]
            if len(args) != len(params):
                self.err("%s.%s expects %d arguments, got %d"
                         % (tt, name, len(params), len(args)), e)
            for a, (pn, pt, _) in zip(args, params):
                at = self.check_expr(a, env, pt)
                if at == "never":
                    continue
                if at != pt:
                    self.err("argument '%s' of %s.%s expects %s, got %s"
                             % (pn, tt, name, pt, at), a)
            return fn["ret"]
        if tt == "str":
            if name not in STR_M:
                self.err("str has no method %s" % name, e)
            ptypes, ret = STR_M[name]
            e["rm"] = ("builtin", "str." + name)
        elif tt == "int":
            if name not in INT_M:
                self.err("int has no method %s" % name, e)
            ptypes, ret = ([], INT_M[name]) if isinstance(INT_M[name], str) else INT_M[name]
            e["rm"] = ("builtin", "int." + name)
        elif tt == "float":
            if name not in FLOAT_M:
                self.err("float has no method %s" % name, e)
            ptypes, ret = ([], FLOAT_M[name])
            e["rm"] = ("builtin", "float." + name)
        elif tt == "bool":
            if name not in BOOL_M:
                self.err("bool has no method %s" % name, e)
            ptypes, ret = ([], BOOL_M[name])
            e["rm"] = ("builtin", "bool." + name)
        elif is_list(tt):
            elem = list_elem(tt)
            tbl = {
                "len": ([], "int"),
                "push": ([elem], "void"),
                "get": (["int"], elem),
                "set": (["int", elem], "void"),
                "pop": ([], elem),
            }
            if name not in tbl:
                self.err("list has no method %s" % name, e)
            ptypes, ret = tbl[name]
            e["rm"] = ("builtin", "list." + name)
        elif is_map(tt):
            vt = map_val(tt)
            tbl = {
                "len": ([], "int"),
                "set": (["str", vt], "void"),
                "get_or": (["str", vt], vt),
                "has": (["str"], "bool"),
                "keys": ([], "list[str]"),
            }
            if name not in tbl:
                self.err("map has no method %s" % name, e)
            ptypes, ret = tbl[name]
            e["rm"] = ("builtin", "map." + name)
        else:
            self.err("cannot call method on type %s" % tt, e)
        if len(args) != len(ptypes):
            self.err("%s.%s expects %d arguments, got %d"
                     % (tt, name, len(ptypes), len(args)), e)
        for a, pt in zip(args, ptypes):
            at = self.check_expr(a, env, pt)
            if at == "never":
                continue
            if at != pt:
                self.err("argument of %s.%s expects %s, got %s" % (tt, name, pt, at), a)
        return ret

    # ---------- effects ----------
    def check_effects(self):
        eff = {}
        for key, fn in self.fns.items():
            eff[key] = fn["uses_io"]
        changed = True
        while changed:
            changed = False
            for key, callees in self.edges.items():
                if eff[key]:
                    continue
                for c in callees:
                    if c.startswith("b:"):
                        eff[key] = True
                        changed = True
                        break
                    if eff.get(c):
                        eff[key] = True
                        changed = True
                        break
        for key, fn in self.fns.items():
            if eff[key] and not fn["uses_io"]:
                for c in self.edges.get(key, ()):  # find a violating edge to report
                    if c.startswith("b:") or eff.get(c):
                        self.err("function '%s' calls '%s' (IO) but does not declare 'uses IO'"
                                 % (fn["name"], c[2:] if c.startswith("b:") else c), fn)


def check(program):
    Checker(program).check()
