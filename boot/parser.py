"""Stage-0 parser for HLS. Conforms to SPEC.md sections 4-6."""
from .lexer import HLError

INT64_MAX = 9223372036854775807
INT64_MIN = -9223372036854775808

PRIM_TYPES = ("int", "float", "bool", "str", "void")

BIN_LEVELS = [
    ("||",),
    ("&&",),
    ("==", "!="),
    ("<", "<=", ">", ">="),
    ("+", "-"),
    ("*", "/", "%"),
]


class Parser:
    def __init__(self, toks):
        self.toks = toks
        self.pos = 0

    # ---------- utilities ----------
    def peek(self):
        return self.toks[self.pos]

    def next(self):
        t = self.toks[self.pos]
        self.pos += 1
        return t

    def err(self, msg, tok=None):
        t = tok or self.peek()
        raise HLError(msg, t["line"], t["col"])

    def at_sym(self, v):
        t = self.peek()
        return t["k"] == "sym" and t["v"] == v

    def at_kw(self, v):
        t = self.peek()
        return t["k"] == "kw" and t["v"] == v

    def eat_sym(self, v):
        if not self.at_sym(v):
            self.err("expected symbol '%s' but got %s" % (v, self._desc()))
        return self.next()

    def eat_kw(self, v):
        if not self.at_kw(v):
            self.err("expected keyword '%s' but got %s" % (v, self._desc()))
        return self.next()

    def eat_ident(self):
        t = self.peek()
        if t["k"] != "ident":
            self.err("expected identifier but got %s" % self._desc())
        return self.next()

    def _desc(self):
        t = self.peek()
        if t["k"] == "eof":
            return "end of file"
        return "'%s'" % (t["v"] if not isinstance(t["v"], bytes) else t["v"].decode("latin-1"))

    # ---------- program ----------
    def parse_program(self):
        structs = {}   # name -> struct
        fns = {}       # key -> fn  (key = function name or "Struct.method")
        imports = []   # list of import paths (Phase 6)
        while self.peek()["k"] != "eof":
            if self.at_kw("struct"):
                st = self.parse_struct()
                if st["name"] in structs:
                    self.err("duplicate struct name: %s" % st["name"])
                structs[st["name"]] = st
            elif self.at_kw("impl"):
                self.parse_impl(fns)
            elif self.at_kw("fn"):
                f = self.parse_fn(None)
                if f["name"] in fns:
                    self.err("duplicate function name: %s" % f["name"])
                fns[f["name"]] = f
            elif self.at_kw("import"):
                imp = self.parse_import()
                imports.append(imp)
            else:
                self.err("only struct/impl/fn/import declarations allowed at top level")
        return {"structs": structs, "fns": fns, "imports": imports}

    def parse_import(self):
        t0 = self.eat_kw("import")
        # import "path/to/mod.hls"
        # or: import "std.str"
        path_tok = self.peek()
        if path_tok["k"] != "str":
            self.err("import expects a string path", path_tok)
        self.next()
        path = path_tok["v"].decode("latin-1")
        return {"path": path, "line": t0["line"]}

    def parse_struct(self):
        t0 = self.eat_kw("struct")
        name = self.eat_ident()["v"]
        self.eat_sym("{")
        fields = []
        while not self.at_sym("}"):
            ft = self.eat_ident()
            self.eat_sym(":")
            ty = self.parse_type()
            fields.append((ft["v"], ty))
            if self.at_sym(","):
                self.next()
            elif not self.at_sym("}"):
                self.err("expected ',' or '}' in struct definition")
        self.eat_sym("}")
        if not fields:
            self.err("struct must have at least one field", t0)
        return {"name": name, "fields": fields, "line": t0["line"]}

    def parse_impl(self, fns):
        t0 = self.eat_kw("impl")
        sname = self.eat_ident()["v"]
        self.eat_sym("{")
        while not self.at_sym("}"):
            if self.at_kw("fn"):
                f = self.parse_fn(sname)
                key = "%s.%s" % (sname, f["name"])
                if key in fns:
                    self.err("duplicate method name: %s" % key, t0)
                fns[key] = f
            else:
                self.err("only fn definitions allowed inside impl")
        self.eat_sym("}")

    def parse_fn(self, impl_struct):
        t0 = self.eat_kw("fn")
        name = self.eat_ident()["v"]
        self.eat_sym("(")
        params = []  # (name, type, mut)
        while not self.at_sym(")"):
            is_mut = False
            if self.at_kw("mut"):
                self.next()
                is_mut = True
            pn = self.eat_ident()
            self.eat_sym(":")
            pt = self.parse_type()
            params.append((pn["v"], pt, is_mut))
            if self.at_sym(","):
                self.next()
            elif not self.at_sym(")"):
                self.err("expected ',' or ')' in parameters")
        self.eat_sym(")")
        ret = "void"
        if self.at_sym("->"):
            self.next()
            ret = self.parse_type(allow_void=True)
        uses_io = False
        if self.at_kw("uses"):
            self.next()
            io = self.eat_ident()
            if io["v"] != "IO":
                self.err("the only effect in v0.2 is 'uses IO'", io)
            uses_io = True
        body = self.parse_block()
        return {
            "name": name, "params": params, "ret": ret, "uses_io": uses_io,
            "body": body, "line": t0["line"], "struct": impl_struct,
        }

    # ---------- types ----------
    def parse_type(self, allow_void=False):
        t = self.peek()
        if t["k"] != "ident":
            self.err("expected type but got %s" % self._desc())
        base = self.next()["v"]
        if base == "list":
            self.eat_sym("[")
            inner = self.parse_type()
            self.eat_sym("]")
            return "list[%s]" % inner
        if base == "map":
            self.eat_sym("[")
            kt = self.eat_ident()
            if kt["v"] != "str":
                self.err("map key in v0.2 must be 'str'", kt)
            self.eat_sym(",")
            vt = self.parse_type()
            self.eat_sym("]")
            return "map[str, %s]" % vt
        if base in PRIM_TYPES:
            if base == "void" and not allow_void:
                self.err("void can only be used as a return type", t)
            return base
        return base  # struct name — checker will verify

    # ---------- statements ----------
    def parse_block(self):
        self.eat_sym("{")
        stmts = []
        while not self.at_sym("}"):
            stmts.append(self.parse_stmt())
        self.eat_sym("}")
        return stmts

    def parse_stmt(self):
        t = self.peek()
        if t["k"] == "kw":
            v = t["v"]
            if v == "let":
                return self.parse_let()
            if v == "return":
                self.next()
                if self.at_sym("}"):
                    return {"k": "return", "value": None, "line": t["line"]}
                val = self.parse_expr()
                return {"k": "return", "value": val, "line": t["line"]}
            if v == "if":
                return self.parse_if()
            if v == "while":
                self.next()
                cond = self.parse_expr(allow_struct=False)
                body = self.parse_block()
                return {"k": "while", "cond": cond, "body": body, "line": t["line"]}
            if v == "for":
                self.next()
                vn = self.eat_ident()
                self.eat_sym(":")
                vt = self.parse_type()
                self.eat_kw("in")
                it = self.parse_expr(allow_struct=False)
                body = self.parse_block()
                return {"k": "for", "var": vn["v"], "vtype": vt, "iter": it,
                        "body": body, "line": t["line"]}
            if v == "break":
                self.next()
                return {"k": "break", "line": t["line"]}
            if v == "continue":
                self.next()
                return {"k": "continue", "line": t["line"]}
            if v in ("struct", "impl", "fn"):
                self.err("%s declaration only allowed at top level" % v)
            self.err("keyword cannot start a statement: %s" % v)
        # expression / assignment
        e = self.parse_expr()
        if self.at_sym("="):
            eq = self.next()
            if e["k"] not in ("ident", "field", "index"):
                self.err("left side of assignment must be variable/field/index", eq)
            if e["k"] == "field" or e["k"] == "index":
                self._require_ident_root(e)
            val = self.parse_expr()
            return {"k": "assign", "target": e, "value": val, "line": eq["line"]}
        if e["k"] not in ("call", "method"):
            self.err("expression as statement must be a function/method call")
        return {"k": "expr", "e": e, "line": e["line"]}

    def _require_ident_root(self, e):
        if e["k"] in ("field", "index"):
            self._require_ident_root(e["target"])
        elif e["k"] != "ident":
            self.err("left side of assignment must start from a variable")

    def parse_let(self):
        t0 = self.eat_kw("let")
        is_mut = False
        if self.at_kw("mut"):
            self.next()
            is_mut = True
        name = self.eat_ident()
        self.eat_sym(":")
        ty = self.parse_type()
        self.eat_sym("=")
        val = self.parse_expr()
        return {"k": "let", "name": name["v"], "t": ty, "mut": is_mut,
                "value": val, "line": t0["line"]}

    def parse_if(self):
        t0 = self.eat_kw("if")
        cond = self.parse_expr(allow_struct=False)
        then = self.parse_block()
        els = None
        if self.at_kw("else"):
            self.next()
            if self.at_kw("if"):
                els = [self.parse_if()]
            else:
                els = self.parse_block()
        return {"k": "if", "cond": cond, "then": then, "els": els, "line": t0["line"]}

    # ---------- expressions ----------
    def parse_expr(self, allow_struct=True):
        return self.parse_binary(0, allow_struct)

    def parse_binary(self, level, allow_struct):
        if level >= len(BIN_LEVELS):
            return self.parse_unary(allow_struct)
        ops = BIN_LEVELS[level]
        left = self.parse_binary(level + 1, allow_struct)
        while True:
            t = self.peek()
            if t["k"] == "sym" and t["v"] in ops:
                self.next()
                right = self.parse_binary(level + 1, allow_struct)
                left = {"k": "bin", "op": t["v"], "l": left, "r": right,
                        "line": t["line"]}
            else:
                return left

    def parse_unary(self, allow_struct):
        t = self.peek()
        if t["k"] == "sym" and t["v"] in ("!", "-"):
            self.next()
            e = self.parse_unary(allow_struct)
            if t["v"] == "-" and e["k"] == "int" and e["v"] == 2 ** 63:
                return {"k": "int", "v": INT64_MIN, "line": t["line"]}
            return {"k": "un", "op": t["v"], "e": e, "line": t["line"]}
        return self.parse_postfix(allow_struct)

    def parse_postfix(self, allow_struct):
        e = self.parse_primary(allow_struct)
        while True:
            if self.at_sym("."):
                dot = self.next()
                name = self.eat_ident()
                if self.at_sym("("):
                    args = self.parse_args()
                    e = {"k": "method", "target": e, "name": name["v"],
                         "args": args, "line": dot["line"]}
                else:
                    e = {"k": "field", "target": e, "name": name["v"],
                         "line": dot["line"]}
            elif self.at_sym("[") and e["k"] in ("ident", "field", "index", "call", "method"):
                lb = self.next()
                idx = self.parse_expr()
                self.eat_sym("]")
                e = {"k": "index", "target": e, "idx": idx, "line": lb["line"]}
            elif self.at_sym("(") and e["k"] == "ident":
                args = self.parse_args()
                e = {"k": "call", "name": e["name"], "args": args,
                     "line": e["line"]}
            else:
                return e

    def parse_args(self):
        self.eat_sym("(")
        args = []
        while not self.at_sym(")"):
            args.append(self.parse_expr())
            if self.at_sym(","):
                self.next()
            elif not self.at_sym(")"):
                self.err("expected ',' or ')' in arguments")
        self.eat_sym(")")
        return args

    def parse_primary(self, allow_struct):
        t = self.peek()
        if t["k"] == "int":
            if t["v"] > 2 ** 63:
                # 2^63 is only valid when combined with minus sign to form INT64_MIN
                self.err("integer literal too large (exceeds int64)")
            self.next()
            return {"k": "int", "v": t["v"], "line": t["line"]}
        if t["k"] == "float":
            self.next()
            return {"k": "float", "v": t["v"], "line": t["line"]}
        if t["k"] == "str":
            self.next()
            return {"k": "str", "v": t["v"], "line": t["line"]}
        if t["k"] == "kw" and t["v"] in ("true", "false"):
            self.next()
            return {"k": "bool", "v": t["v"] == "true", "line": t["line"]}
        if t["k"] == "ident":
            name = t["v"]
            nxt = self.toks[self.pos + 1] if self.pos + 1 < len(self.toks) else None
            if allow_struct and nxt and nxt["k"] == "sym" and nxt["v"] == "{":
                return self.parse_struct_lit()
            self.next()
            return {"k": "ident", "name": name, "line": t["line"]}
        if self.at_sym("("):
            self.next()
            e = self.parse_expr()
            self.eat_sym(")")
            return e
        if self.at_sym("["):
            lb = self.next()
            items = []
            while not self.at_sym("]"):
                items.append(self.parse_expr())
                if self.at_sym(","):
                    self.next()
                elif not self.at_sym("]"):
                    self.err("expected ',' or ']' in list literal")
            self.eat_sym("]")
            return {"k": "listlit", "items": items, "line": lb["line"]}
        self.err("expected expression but got %s" % self._desc())

    def parse_struct_lit(self):
        t = self.peek()
        name = self.next()["v"]
        self.eat_sym("{")
        fields = []  # (field_name, expr)
        while not self.at_sym("}"):
            ft = self.eat_ident()
            self.eat_sym(":")
            fields.append((ft["v"], self.parse_expr()))
            if self.at_sym(","):
                self.next()
            elif not self.at_sym("}"):
                self.err("expected ',' or '}' in struct literal")
        self.eat_sym("}")
        return {"k": "structlit", "name": name, "fields": fields, "line": t["line"]}


def parse(src):
    from .lexer import tokenize
    return Parser(tokenize(src)).parse_program()
