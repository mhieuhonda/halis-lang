"""Stage-0 parser for HLS. Conforms to SPEC.md sections 4-6, 11b-12 (v0.3)."""
from .lexer import HLError

INT64_MAX = 9223372036854775807
INT64_MIN = -9223372036854775808

PRIM_TYPES = ("int", "float", "bool", "str", "void")

# Stage 9-alpha: fine-grained effects & capabilities (v0.5.0-alpha)
# Recognized effect names. `IO` is a blanket alias for the entire IO family
# (IO + Fs + Clock + Args + Exit) — expanded at parse time so the fixpoint
# is a trivial subset test.
KNOWN_EFFECTS = {"IO", "Fs", "Clock", "Args", "Exit"}
RESERVED_EFFECTS = {"Net", "Rand", "Proc"}  # recognized but no builtins yet
IO_FAMILY = {"IO", "Fs", "Clock", "Args", "Exit"}

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
        enums = {}      # name -> enum
        fns = {}       # key -> fn  (key = function name or "Struct.method")
        imports = []   # list of import paths
        while self.peek()["k"] != "eof":
            if self.at_kw("struct"):
                st = self.parse_struct()
                if st["name"] in structs or st["name"] in enums:
                    self.err("duplicate type name: %s" % st["name"])
                structs[st["name"]] = st
            elif self.at_kw("enum"):
                en = self.parse_enum()
                if en["name"] in enums or en["name"] in structs:
                    self.err("duplicate type name: %s" % en["name"])
                enums[en["name"]] = en
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
                self.err("only struct/enum/impl/fn/import declarations allowed at top level")
        return {"structs": structs, "enums": enums, "fns": fns, "imports": imports}

    def parse_import(self):
        t0 = self.eat_kw("import")
        path_tok = self.peek()
        if path_tok["k"] != "str":
            self.err("import expects a string path", path_tok)
        self.next()
        path = path_tok["v"].decode("latin-1")
        return {"path": path, "line": t0["line"]}

    def parse_typeparams(self):
        """Parse `[T, U, ...]` — list of type parameter names."""
        tps = []
        if not self.at_sym("["):
            return tps
        self.next()
        while not self.at_sym("]"):
            t = self.eat_ident()
            tps.append(t["v"])
            if self.at_sym(","):
                self.next()
            elif not self.at_sym("]"):
                self.err("expected ',' or ']' in type parameters")
        self.eat_sym("]")
        return tps

    def parse_struct(self):
        t0 = self.eat_kw("struct")
        name = self.eat_ident()["v"]
        typeparams = self.parse_typeparams()
        self.eat_sym("{")
        fields = []  # list of (name, type, default_expr_or_None)
        while not self.at_sym("}"):
            ft = self.eat_ident()
            self.eat_sym(":")
            ty = self.parse_type()
            default = None
            if self.at_sym("="):
                self.next()
                default = self.parse_expr()
            fields.append((ft["v"], ty, default))
            if self.at_sym(","):
                self.next()
            elif not self.at_sym("}"):
                self.err("expected ',' or '}' in struct definition")
        self.eat_sym("}")
        if not fields:
            self.err("struct must have at least one field", t0)
        # Enforce: defaulted fields must come after non-defaulted ones.
        seen_default = False
        for fname, ftype, fdefault in fields:
            if fdefault is not None:
                seen_default = True
            elif seen_default:
                self.err("struct field '%s' without default cannot follow a defaulted field"
                         % fname, t0)
        return {"name": name, "typeparams": typeparams, "fields": fields,
                "line": t0["line"]}

    def parse_enum(self):
        t0 = self.eat_kw("enum")
        name = self.eat_ident()["v"]
        typeparams = self.parse_typeparams()
        self.eat_sym("{")
        variants = []  # list of (variant_name, [payload_types])
        seen = set()
        while not self.at_sym("}"):
            vt = self.eat_ident()
            if vt["v"] in seen:
                self.err("duplicate variant name in enum: %s" % vt["v"], vt)
            seen.add(vt["v"])
            payloads = []
            if self.at_sym("("):
                self.next()
                while not self.at_sym(")"):
                    payloads.append(self.parse_type())
                    if self.at_sym(","):
                        self.next()
                    elif not self.at_sym(")"):
                        self.err("expected ',' or ')' in variant payloads")
                self.eat_sym(")")
            variants.append((vt["v"], payloads))
            if self.at_sym(","):
                self.next()
            elif not self.at_sym("}"):
                self.err("expected ',' or '}' in enum definition")
        self.eat_sym("}")
        if not variants:
            self.err("enum must have at least one variant", t0)
        return {"name": name, "typeparams": typeparams, "variants": variants,
                "line": t0["line"]}

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
        typeparams = self.parse_typeparams()
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
        # Stage 9-beta: explicit `pure` keyword for documentation/linting.
        # A function declared `pure` must have NO `uses` clause, and the
        # checker will reject it if its body transitively calls anything
        # effectful. `pure` and `uses` are mutually exclusive.
        is_pure = False
        if self.at_kw("pure"):
            self.next()
            is_pure = True
        # Stage 9-alpha: fine-grained effects. `uses IO` is a blanket alias
        # for the entire IO family (expanded at parse time). Other recognized
        # effects: Fs, Clock, Args, Exit. Reserved: Net, Rand, Proc.
        effects = set()
        if self.at_kw("uses"):
            if is_pure:
                self.err("'pure' and 'uses' are mutually exclusive (a pure "
                         "function declares no effects)", t0)
            self.next()
            while True:
                tok = self.eat_ident()
                eff = tok["v"]
                if eff in RESERVED_EFFECTS:
                    self.err("effect '%s' is reserved for a future stage "
                             "(no builtins implemented yet)" % eff, tok)
                if eff not in KNOWN_EFFECTS:
                    self.err("unknown effect '%s'; known effects: IO, Fs, "
                             "Clock, Args, Exit" % eff, tok)
                if eff in effects:
                    self.err("duplicate effect declaration: %s" % eff, tok)
                effects.add(eff)
                # `IO` is the blanket alias — expand to the full IO family.
                if eff == "IO":
                    effects |= IO_FAMILY
                if self.at_sym(","):
                    self.next()
                    continue
                break
        body = self.parse_block()
        return {
            "name": name, "typeparams": typeparams, "params": params, "ret": ret,
            "effects": effects, "pure": is_pure, "body": body, "line": t0["line"],
            "struct": impl_struct,
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
                self.err("map key in v0.3 must be 'str'", kt)
            self.eat_sym(",")
            vt = self.parse_type()
            self.eat_sym("]")
            return "map[str, %s]" % vt
        # Stage 10-alpha: `tainted[T]` — a built-in generic wrapper for
        # taint tracking. The wrapper is recognised by the checker as a
        # built-in (no struct/enum definition needed). At runtime it is
        # represented as a Python dict {"tainted": True, "value": <T>}.
        if base == "tainted":
            self.eat_sym("[")
            inner = self.parse_type()
            self.eat_sym("]")
            return "tainted[%s]" % inner
        if base in PRIM_TYPES:
            if base == "void" and not allow_void:
                self.err("void can only be used as a return type", t)
            return base
        # User-defined type — could be generic instantiation: Name[T1, T2, ...]
        if self.at_sym("["):
            self.next()
            args = []
            while not self.at_sym("]"):
                args.append(self.parse_type())
                if self.at_sym(","):
                    self.next()
                elif not self.at_sym("]"):
                    self.err("expected ',' or ']' in type arguments")
            self.eat_sym("]")
            return "%s[%s]" % (base, ", ".join(args))
        return base  # struct/enum name — checker will verify

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
            if v == "match":
                # match can be a statement (expression-statement)
                e = self.parse_match()
                # In statement position, a match is just an expression.
                # The checker will allow it as a call-equivalent (it has side effects
                # only through its arms; we permit it as a statement).
                return {"k": "expr", "e": e, "line": e["line"]}
            if v in ("struct", "impl", "enum", "fn"):
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
        if e["k"] not in ("call", "method", "fieldcall", "match"):
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
                    # `target.name(args)` — either a method call or an enum
                    # variant constructor. Defer disambiguation to the checker.
                    args = self.parse_args()
                    e = {"k": "fieldcall", "target": e, "name": name["v"],
                         "args": args, "line": dot["line"]}
                else:
                    e = {"k": "field", "target": e, "name": name["v"],
                         "line": dot["line"]}
            elif self.at_sym("[") and e["k"] in ("ident", "field", "index", "call", "method", "fieldcall"):
                lb = self.next()
                idx = self.parse_expr()
                self.eat_sym("]")
                e = {"k": "index", "target": e, "idx": idx, "line": lb["line"]}
            elif self.at_sym("(") and e["k"] == "ident":
                args = self.parse_args()
                e = {"k": "call", "name": e["name"], "args": args,
                     "line": e["line"]}
            elif self.at_sym("?"):
                q = self.next()
                e = {"k": "qmark", "e": e, "line": q["line"]}
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

    def parse_match(self):
        t0 = self.eat_kw("match")
        scrut = self.parse_expr(allow_struct=False)
        self.eat_sym("{")
        arms = []
        while not self.at_sym("}"):
            arm = self.parse_arm()
            arms.append(arm)
            if self.at_sym(","):
                self.next()
            elif not self.at_sym("}"):
                self.err("expected ',' or '}' in match arms")
        self.eat_sym("}")
        return {"k": "match", "scrut": scrut, "arms": arms, "line": t0["line"]}

    def parse_arm(self):
        # pattern: _ | _Ident (wildcard with optional bind, only `_` here)
        #        | Name.Variant
        #        | Name.Variant(Ident, Ident, ...)   (with payload bindings)
        #        | Name.Variant(_)                    (payload ignored)
        # NOTE (BUG-001 fix): `_` is tokenized by the lexer as an `ident`
        # token (because `_is_ident_start` returns True for byte 95), NOT
        # as a `sym`. So we must test for both kinds here.
        if self.at_sym("_") or (self.peek()["k"] == "ident" and self.peek()["v"] == "_"):
            self.next()
            pattern = {"k": "wildcard"}
        else:
            # `Name.Variant` or `Name.Variant(bindings)` — Name is the enum
            # type name. (We require the enum name to be present — no bare
            # `Variant` patterns, for auditability.)
            ename_tok = self.eat_ident()
            ename = ename_tok["v"]
            self.eat_sym(".")
            vname = self.eat_ident()["v"]
            bindings = []
            has_paren = False
            if self.at_sym("("):
                has_paren = True
                self.next()
                while not self.at_sym(")"):
                    # NOTE (BUG-001 fix): `_` is an ident token, not a sym.
                    if self.at_sym("_") or (self.peek()["k"] == "ident" and self.peek()["v"] == "_"):
                        self.next()
                        bindings.append("_")  # wildcard — payload ignored
                    else:
                        b = self.eat_ident()
                        bindings.append(b["v"])
                    if self.at_sym(","):
                        self.next()
                    elif not self.at_sym(")"):
                        self.err("expected ',' or ')' in variant pattern")
                self.eat_sym(")")
            pattern = {"k": "variant", "enum": ename, "variant": vname,
                       "bindings": bindings, "has_paren": has_paren}
        self.eat_sym("=>")
        body = self.parse_expr()
        return {"pattern": pattern, "body": body, "line": self.peek()["line"]}

    def parse_primary(self, allow_struct):
        t = self.peek()
        if t["k"] == "int":
            if t["v"] > 2 ** 63:
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
        if t["k"] == "kw" and t["v"] == "match":
            return self.parse_match()
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
