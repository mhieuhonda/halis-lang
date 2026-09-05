"""Stage-0 parser for HLS. Conforms to SPEC.md sections 4-6, 11b-12 (v0.3)."""
from .lexer import HLError, RESERVED_IDENTIFIERS

INT64_MAX = 9223372036854775807
INT64_MIN = -9223372036854775808

PRIM_TYPES = ("int", "float", "bool", "str", "void")

# Stage 9 (v0.20.0-alpha — Stage 9 release): fine-grained effects &
# capabilities COMPLETE. All eight effects are now active; the reserved
# set is empty.
#
# Recognized effect names. `IO` is a blanket alias for the entire IO family
# (IO + Fs + Clock + Args + Exit) — expanded at parse time so the fixpoint
# is a trivial subset test. `Net`, `Rand`, `Proc` are independent effects
# (not part of the IO family) — a program must declare them explicitly to
# use network, random, or subprocess builtins.
#
# Stage 16 (v0.27.0-alpha): `Conc` — the concurrency effect. Carried by
# chan_new / spawn / select builtins and the Chan.send / Chan.recv /
# Chan.len / Task.join methods. NOT part of the IO family: a program must
# declare `uses Conc` explicitly to use task/channel operations. This keeps
# every function without a `uses` clause pure AND deterministic (spawn
# introduces observable scheduling nondeterminism).
KNOWN_EFFECTS = {"IO", "Fs", "Clock", "Args", "Exit", "Net", "Rand", "Proc", "Conc"}
RESERVED_EFFECTS = set()  # no reserved effects as of v0.20.0-alpha
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
        # BUG-29 fix: reject reserved identifiers (secure / trait) — they
        # are reserved for future keywords per SPEC section 2.5.
        if t["v"] in RESERVED_IDENTIFIERS:
            self.err("'%s' is a reserved identifier and cannot be used as "
                     "a name (it will become a keyword in a future stage)" % t["v"], t)
        return self.next()

    def _desc(self):
        t = self.peek()
        if t["k"] == "eof":
            return "end of file"
        return "'%s'" % (t["v"] if not isinstance(t["v"], bytes) else t["v"].decode("latin-1"))

    # ---------- program ----------
    # Deep-scan-7 fix: a struct/enum named after a primitive type
    # (int, float, bool, str) or after void is a SOUNDNESS HOLE —
    # `type_exists` short-circuits on the primitive name without
    # consulting self.structs, so `let x: int = int { x: 5 }` would
    # compile and the runtime would crash at access time. Reject
    # any struct/enum declaration with a primitive name at parse time.
    RESERVED_TYPE_NAMES = {
        "int", "float", "bool", "str", "void",
        "list", "map", "tainted",
        "true", "false",  # boolean literals are reserved too
        # Stage 16 (v0.27.0-alpha): built-in concurrency wrappers.
        "Chan", "Task",
    }

    # Stage 28+29 (v0.45.0-alpha): attribute parser state. The boot
    # interpreter does NOT honour these attributes (they only affect C
    # codegen in the self-hosted compiler), but it MUST parse them so
    # that running `boot/boot.py file_with_attrs.hls` does not error.
    # parse_attributes populates these; parse_fn reads them and stores
    # them on the fn dict (the checker / interp ignore them).
    def reset_cur_attrs(self):
        self.cur_attrs = {
            "stack_size": -1,
            "no_red_zone": False,
            "irq_handler": False,
            "inline": "",
            "hot": False,
            "cold": False,
        }

    def parse_attributes(self):
        """Stage 28+29 (v0.45.0-alpha): parse one or more `#[...]`
        attribute lists. Each list is `#` `[` attr (`,` attr)* `]`
        where each `attr` is one of:
            inline(always)   - force inline at every call site
            inline(never)    - the function is never inlined
            hot              - mark the function hot
            cold             - mark the function cold
            no_red_zone      - disable the x86-64 red zone
            irq_handler      - emit an IRET-compatible frame
            stack_size(N)    - assert the fn's frame is <= N bytes
        Multiple `#[...]` lists may precede a single fn (each
        accumulates). `hot` and `cold` are mutually exclusive; likewise
        `inline(always)` and `inline(never)`. The boot parser
        validates these constraints and stores the result in
        self.cur_attrs; the boot interpreter ignores them.
        """
        while self.at_sym("#"):
            t0 = self.next()  # consume '#'
            self.eat_sym("[")
            while not self.at_sym("]"):
                attr_name = self.eat_ident()["v"]
                if attr_name == "inline":
                    self.eat_sym("(")
                    mode = self.eat_ident()["v"]
                    self.eat_sym(")")
                    if mode not in ("always", "never"):
                        self.err("unknown inline mode '%s' (expected "
                                 "'always' or 'never')" % mode, t0)
                    if (self.cur_attrs["inline"] != ""
                            and self.cur_attrs["inline"] != mode):
                        self.err("conflicting inline attributes (was '%s', "
                                 "now '%s')" % (self.cur_attrs["inline"], mode), t0)
                    self.cur_attrs["inline"] = mode
                elif attr_name == "hot":
                    if self.cur_attrs["cold"]:
                        self.err("'hot' and 'cold' are mutually exclusive", t0)
                    self.cur_attrs["hot"] = True
                elif attr_name == "cold":
                    if self.cur_attrs["hot"]:
                        self.err("'hot' and 'cold' are mutually exclusive", t0)
                    self.cur_attrs["cold"] = True
                elif attr_name == "no_red_zone":
                    self.cur_attrs["no_red_zone"] = True
                elif attr_name == "irq_handler":
                    self.cur_attrs["irq_handler"] = True
                elif attr_name == "stack_size":
                    self.eat_sym("(")
                    nt = self.peek()
                    if nt["k"] != "int":
                        self.err("stack_size expects an integer byte count "
                                 "but got %s" % self._desc(), nt)
                    self.next()
                    n = int(nt["v"])
                    if n < 0:
                        self.err("stack_size must be non-negative", nt)
                    self.eat_sym(")")
                    self.cur_attrs["stack_size"] = n
                elif attr_name == "stack" or attr_name == "boxed":
                    # Stage 30 (v0.47.0-alpha): #[stack] / #[boxed] are
                    # LET-BINDING attributes (they control the layout of a
                    # list[T] value), not function attributes. Placing one
                    # before a fn is a clear user error — point them at the
                    # let statement form.
                    self.err("'%s' is a let-binding attribute — place it "
                             "inside a function, directly before a 'let' "
                             "statement (e.g. #[%s] let xs: list[int] = "
                             "[1, 2, 3])" % (attr_name, attr_name), t0)
                else:
                    self.err("unknown attribute '%s' (known: inline(always), "
                             "inline(never), hot, cold, no_red_zone, "
                             "irq_handler, stack_size(N))" % attr_name, t0)
                if self.at_sym(","):
                    self.next()
                elif not self.at_sym("]"):
                    self.err("expected ',' or ']' in attribute list")
            self.eat_sym("]")

    def parse_program(self):
        structs = {}   # name -> struct
        enums = {}      # name -> enum
        fns = {}       # key -> fn  (key = function name or "Struct.method")
        imports = []   # list of import paths
        externs = []   # Stage 15 (v0.13.0-alpha): list of extern blocks
        # Stage 28+29: attribute cache, reset before every declaration.
        self.reset_cur_attrs()
        while self.peek()["k"] != "eof":
            # Stage 28+29: a leading `#[...]` applies to the next fn.
            if self.at_sym("#"):
                self.parse_attributes()
                continue
            if self.at_kw("struct"):
                self.reset_cur_attrs()
                st = self.parse_struct()
                if st["name"] in self.RESERVED_TYPE_NAMES:
                    self.err("type name '%s' is reserved (collides with a "
                             "primitive type)" % st["name"])
                if st["name"] in structs or st["name"] in enums:
                    self.err("duplicate type name: %s" % st["name"])
                structs[st["name"]] = st
            elif self.at_kw("enum"):
                self.reset_cur_attrs()
                en = self.parse_enum()
                if en["name"] in self.RESERVED_TYPE_NAMES:
                    self.err("type name '%s' is reserved (collides with a "
                             "primitive type)" % en["name"])
                if en["name"] in enums or en["name"] in structs:
                    self.err("duplicate type name: %s" % en["name"])
                enums[en["name"]] = en
            elif self.at_kw("impl"):
                self.reset_cur_attrs()
                self.parse_impl(fns)
            elif self.at_kw("fn"):
                f = self.parse_fn(None)
                if f["name"] in fns:
                    self.err("duplicate function name: %s" % f["name"])
                fns[f["name"]] = f
            elif self.at_kw("import"):
                self.reset_cur_attrs()
                imp = self.parse_import()
                imports.append(imp)
            elif self.at_kw("extern"):
                self.reset_cur_attrs()
                # Stage 15 (v0.13.0-alpha): extern "C" { ... } block.
                ext = self.parse_extern_block()
                for fn_decl in ext["decls"]:
                    if fn_decl["name"] in fns:
                        self.err("duplicate function name: %s" % fn_decl["name"])
                    fns[fn_decl["name"]] = fn_decl
                externs.append(ext)
            else:
                self.err("only struct/enum/impl/fn/import/extern declarations allowed at top level")
            self.reset_cur_attrs()
        return {"structs": structs, "enums": enums, "fns": fns,
                "imports": imports, "externs": externs}

    def parse_extern_block(self):
        """Stage 15 (v0.13.0-alpha): parse `extern "C" { fn decls }` block.
        Stage 23 (v0.42.0-alpha): also accept `extern "js" { fn decls }`
        for the WebAssembly JS-FFI (the wasm module imports these from
        the JS host).

        Syntax:
            extern "C" {
                fn puts(s: str) -> int uses IO
                fn malloc(size: int) -> ptr uses IO
                ...
            }

            extern "js" {
                fn console.log(s: str) -> void uses IO
                fn fetch(url: str) -> str uses IO
                ...
            }

        Each `fn` declaration has NO body (just a signature). The
        `uses IO` clause is REQUIRED unless `pure` is declared — the
        safe default for FFI is to assume side effects.
        """
        t0 = self.eat_kw("extern")
        abi_tok = self.peek()
        if abi_tok["k"] != "str":
            self.err("expected ABI string (e.g. \"C\" or \"js\") after 'extern'", abi_tok)
        self.next()
        abi = abi_tok["v"]
        if isinstance(abi, bytes):
            abi = abi.decode("latin-1")
        if abi not in ("C", "js"):
            self.err("unsupported ABI '%s'; only \"C\" and \"js\" are supported" % abi, abi_tok)
        self.eat_sym("{")
        decls = []
        while not self.at_sym("}"):
            if not self.at_kw("fn"):
                self.err("extern block can only contain fn declarations")
            fn_decl = self.parse_fn(None, extern=True)
            if not fn_decl["effects"] and not fn_decl["pure"]:
                self.err("extern fn '%s' must declare `uses IO` (or `pure`) "
                         "— FFI is unsafe by default" % fn_decl["name"], t0)
            decls.append(fn_decl)
        self.eat_sym("}")
        return {"abi": abi, "decls": decls, "line": t0["line"]}

    def parse_import(self):
        t0 = self.eat_kw("import")
        path_tok = self.peek()
        if path_tok["k"] != "str":
            self.err("import expects a string path", path_tok)
        self.next()
        path = path_tok["v"].decode("latin-1")
        return {"path": path, "line": t0["line"]}

    def parse_typeparams(self):
        """Parse `[T, U, ...]` — list of type parameter names.

        Deep-scan-10 soundness fix: a type parameter may NOT be named
        after a primitive / wrapper / builtin generic base (`int`,
        `float`, `bool`, `str`, `void`, `list`, `map`, `tainted`,
        `Chan`, `Task`, `Option`, `Result` — the latter two are stdlib
        enums, the rest are language builtins). `fn f[int](x: int)`
        used to parse fine, and `_instantiate_type` / `unify` consulted
        the type_map BEFORE the primitive table, so a `str` argument
        silently bound `int -> str` — a type-soundness hole (the
        parameter declared `int` accepted a `str`)."""
        tps = []
        if not self.at_sym("["):
            return tps
        self.next()
        while not self.at_sym("]"):
            t = self.eat_ident()
            name = t["v"]
            if name in ("int", "float", "bool", "str", "void", "list",
                        "map", "tainted", "Chan", "Task", "Option",
                        "Result"):
                self.err("type parameter cannot be named '%s' (it is a "
                         "builtin type name and would shadow it)" % name, t)
            tps.append(name)
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
        # BUG-SC-8 fix: reject duplicate field names. Previously
        # `struct Foo { x: int, x: int }` was silently accepted; at
        # runtime `eval_structlit` builds a dict, so `Foo { x: 1, x: 2 }`
        # would silently produce `{"x": 2}` (the first value lost).
        seen_fields = set()
        while not self.at_sym("}"):
            ft = self.eat_ident()
            if ft["v"] in seen_fields:
                self.err("duplicate field name in struct: %s" % ft["v"], ft)
            seen_fields.add(ft["v"])
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
            # Stage 28+29: methods may carry a `#[...]` attribute list
            # (applies to the following fn).
            if self.at_sym("#"):
                self.parse_attributes()
                continue
            if self.at_kw("fn"):
                f = self.parse_fn(sname)
                key = "%s.%s" % (sname, f["name"])
                if key in fns:
                    self.err("duplicate method name: %s" % key, t0)
                fns[key] = f
            else:
                self.err("only fn definitions allowed inside impl")
            self.reset_cur_attrs()
        self.eat_sym("}")

    def parse_fn(self, impl_struct, extern=False):
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
        # Stage 9 (v0.20.0-alpha — release): fine-grained effects &
        # capabilities. `uses IO` is a blanket alias for the entire IO
        # family (expanded at parse time). Active effects: Fs, Clock,
        # Args, Exit, Net, Rand, Proc. No reserved effects remain.
        effects = set()
        # Deep-scan-8 fix: track EXPLICITLY-declared effects separately
        # from the expanded set. `uses IO, Fs` used to error with
        # "duplicate effect declaration: Fs" because IO is expanded to
        # the full IO family (which includes Fs) at parse time, and the
        # duplicate check ran against the expanded set. Now we only
        # check for duplicates against the explicitly-declared names,
        # so `uses IO, Fs` is accepted (redundant but legal), while
        # `uses IO, IO` or `uses Fs, Fs` still errors.
        explicit_effects = set()
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
                             "Clock, Args, Exit, Net, Rand, Proc, Conc" % eff, tok)
                if eff in explicit_effects:
                    self.err("duplicate effect declaration: %s" % eff, tok)
                explicit_effects.add(eff)
                effects.add(eff)
                # `IO` is the blanket alias — expand to the full IO family.
                if eff == "IO":
                    effects |= IO_FAMILY
                if self.at_sym(","):
                    self.next()
                    continue
                break
        if extern:
            # Stage 15 (v0.13.0-alpha): extern fn declarations have NO
            # body. They are forward declarations for C functions.
            # Stage 17: contracts on extern fns (requires only; an
            # ensures without a body would be an unverifiable claim —
            # allow it as a documentation claim but it is only checked
            # at call sites, not verified).
            contracts = self.parse_contracts(t0)
            return {
                "name": name, "typeparams": typeparams, "params": params,
                "ret": ret, "effects": effects, "pure": is_pure,
                "body": [], "line": t0["line"], "struct": impl_struct,
                "extern": True,
                "requires": contracts[0], "ensures": contracts[1],
                # Stage 28+29: extern fns are not allowed to carry HLS
                # attributes (the FFI signature is the C ABI; inlining,
                # interrupt frame layout etc. belong to the C side).
                "attrs": {"stack_size": -1, "no_red_zone": False,
                          "irq_handler": False, "inline": "",
                          "hot": False, "cold": False},
            }
        # Stage 17 (v0.28.0-alpha): optional contract clauses —
        # `requires <bool-expr>` then/and `ensures <bool-expr>`, parsed
        # after the effects clause, before the body block.
        contracts = self.parse_contracts(t0)
        body = self.parse_block()
        # Stage 28+29 (v0.45.0-alpha): snapshot the per-fn attribute
        # cache into the fn dict. The boot interpreter does NOT honour
        # these attributes (they only affect C codegen in the
        # self-hosted compiler) but storing them keeps the AST shape
        # consistent with the self-hosted FnInfo struct (the hlmodel /
        # hlprove / hlbindgen tools that consume boot's AST see the
        # fields and can reason about them).
        return {
            "name": name, "typeparams": typeparams, "params": params, "ret": ret,
            "effects": effects, "pure": is_pure, "body": body, "line": t0["line"],
            "struct": impl_struct, "extern": False,
            "requires": contracts[0], "ensures": contracts[1],
            "attrs": dict(self.cur_attrs),
        }

    def parse_contracts(self, fn_tok):
        """Stage 17: parse the contract clause list — any number of
        `requires <expr>` clauses followed by any number of
        `ensures <expr>` clauses (multiple clauses of the same kind are
        combined with && into a single expression). Returns the
        (requires_expr_or_None, ensures_expr_or_None) pair. The
        expressions are parsed with allow_struct=False so a struct
        literal cannot be confused with the function body's `{`."""
        reqs = []
        enss = []
        while True:
            if self.at_kw("requires") and not enss:
                self.next()
                reqs.append(self.parse_expr(allow_struct=False))
            elif self.at_kw("ensures"):
                self.next()
                enss.append(self.parse_expr(allow_struct=False))
            else:
                break
        req_expr = None
        for x in reqs:
            if req_expr is None:
                req_expr = x
            else:
                req_expr = {"k": "bin", "op": "&&", "l": req_expr, "r": x,
                            "line": x.get("line", 0)}
        ens_expr = None
        for x in enss:
            if ens_expr is None:
                ens_expr = x
            else:
                ens_expr = {"k": "bin", "op": "&&", "l": ens_expr, "r": x,
                            "line": x.get("line", 0)}
        return (req_expr, ens_expr)

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
        # Stage 16 (v0.27.0-alpha): `Chan[T]` — a built-in generic
        # message-passing channel (MPMC, unbounded, blocking recv).
        # No struct/enum definition needed; the checker recognises it.
        if base == "Chan":
            self.eat_sym("[")
            inner = self.parse_type()
            self.eat_sym("]")
            if inner == "void":
                self.err("Chan[T] element type cannot be void", t)
            return "Chan[%s]" % inner
        # Stage 16 (v0.27.0-alpha): `Task[R]` — a spawned task's join
        # handle. R is the spawned function's return type (void allowed:
        # join() returns nothing). Not Send: a Task handle cannot itself
        # cross a task boundary.
        if base == "Task":
            self.eat_sym("[")
            inner = self.parse_type(allow_void=True)
            self.eat_sym("]")
            return "Task[%s]" % inner
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

    def parse_let_attrs(self):
        """Stage 30 (v0.47.0-alpha): parse a `#[stack]` / `#[boxed]`
        attribute list that applies to the FOLLOWING `let` binding.
        Only the two layout attributes are accepted here (the fn-level
        attributes like #[inline] must not appear on a let). Returns the
        (stack, boxed) flags. The caller must verify the next token is
        `let` — these attributes only apply to let bindings.
        """
        stack = False
        boxed = False
        while self.at_sym("#"):
            t0 = self.next()  # consume '#'
            self.eat_sym("[")
            while not self.at_sym("]"):
                attr_name = self.eat_ident()["v"]
                if attr_name == "stack":
                    if boxed:
                        self.err("'stack' and 'boxed' are mutually exclusive", t0)
                    stack = True
                elif attr_name == "boxed":
                    if stack:
                        self.err("'stack' and 'boxed' are mutually exclusive", t0)
                    boxed = True
                else:
                    self.err("unknown let-binding attribute '%s' (known: "
                             "stack, boxed; function attributes go before "
                             "a fn declaration)" % attr_name, t0)
                if self.at_sym(","):
                    self.next()
                elif not self.at_sym("]"):
                    self.err("expected ',' or ']' in attribute list")
            self.eat_sym("]")
        return stack, boxed

    def parse_stmt(self):
        t = self.peek()
        # Stage 30 (v0.47.0-alpha): a `#[stack]` / `#[boxed]` attribute
        # list directly before a statement applies to a `let` binding
        # (the only statement that accepts attributes). The lexer only
        # emits a bare `#` token when it is followed by `[` (anything
        # else is a line comment), so seeing sym `#` here is unambiguous.
        if t["k"] == "sym" and t["v"] == "#":
            stack, boxed = self.parse_let_attrs()
            if not self.at_kw("let"):
                self.err("#[stack]/#[boxed] attributes apply to a 'let' "
                         "binding", t)
            s = self.parse_let()
            s["stack"] = stack
            s["boxed"] = boxed
            return s
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
        # Deep-scan-10 fix: `foo()?` as an expression-statement is the
        # idiomatic "propagate the error, discard the value" form —
        # `match` already had an exemption for exactly this reason. The
        # qmark node is validated by the checker (its operand must be a
        # call/fieldcall anyway).
        if e["k"] not in ("call", "method", "fieldcall", "match", "qmark"):
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
        # Stage 30 (v0.47.0-alpha): let-binding layout attributes,
        # populated by parse_stmt when a `#[stack]` / `#[boxed]` list
        # precedes the let. Defaults are False (the automatic escape
        # analysis decides the layout; the attrs only force a side).
        return {"k": "let", "name": name["v"], "t": ty, "mut": is_mut,
                "value": val, "line": t0["line"],
                "stack": False, "boxed": False}

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
            elif self.at_sym("[") and e["k"] in ("ident", "field", "index", "call", "method", "fieldcall", "qmark"):
                # Deep-scan-10 fix: `qmark` joins the indexable forms —
                # `g()?[0]` used to detach the `[0]` into a stray
                # statement (inconsistent with `g()?.x`, which worked).
                # `listlit` (indexing a fresh list literal) and `match`
                # results are also indexable values.
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
        # BUG-32 fix: capture the arm's line from the FIRST token of the
        # pattern (not the token after the body, which is the next arm or
        # the closing `}`). Error messages for arm issues now point to
        # the right line.
        arm_line = self.peek()["line"]
        # pattern: _ | _Ident (wildcard with optional bind, only `_` here)
        #        | Name.Variant
        #        | Name.Variant(Ident, Ident, ...)   (with payload bindings)
        #        | Name.Variant(_)                    (payload ignored)
        # NOTE (BUG-001 fix, BUG-18 cleanup): `_` is tokenized by the lexer
        # as an `ident` token (because `_is_ident_start` returns True for
        # byte 95). It is NEVER a `sym` token, so we only need to test for
        # the ident form here — the at_sym("_") check is dead code and has
        # been removed.
        if self.peek()["k"] == "ident" and self.peek()["v"] == "_":
            self.next()
            pattern = {"k": "wildcard"}
        else:
            # `Name.Variant` or `Name.Variant(bindings)` — Name is the enum
            # type name. Deep-scan-10 fix: a BARE `Variant` pattern (no
            # `Name.` prefix) is also accepted, per the SPEC §5 grammar
            # (`pattern := (Ident ".")? Ident ...`) — the checker resolves
            # the enum from the scrutinee's type (a bare name that matches
            # no variant of the scrutinee enum is a clean error there).
            ename_tok = self.eat_ident()
            ename = ename_tok["v"]
            vname = None
            bare = True
            if self.at_sym("."):
                self.next()
                vname = self.eat_ident()["v"]
                bare = False
            if bare:
                vname = ename
                ename = ""  # resolved by the checker from the scrutinee type
            bindings = []
            has_paren = False
            if self.at_sym("("):
                has_paren = True
                self.next()
                while not self.at_sym(")"):
                    # NOTE (BUG-001 fix, BUG-18 cleanup): `_` is an ident token.
                    if self.peek()["k"] == "ident" and self.peek()["v"] == "_":
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
        return {"pattern": pattern, "body": body, "line": arm_line}

    def parse_primary(self, allow_struct):
        t = self.peek()
        if t["k"] == "int":
            # BUG-37 analysis: the parser INTENTIONALLY allows literals up
            # to (and including) 2^63 as an intermediate value, so that the
            # unary-minus handler in parse_unary() can fold the literal
            # `-9223372036854775808` (= INT64_MIN) into a single int node
            # instead of evaluating `-` then `9223372036854775808` and
            # panicking on the i64_neg of INT64_MIN. The checker then
            # rejects POSITIVE literals exceeding INT64_MAX (2^63 - 1) at
            # check time. So the parser's `2**63` boundary here is correct;
            # any tighter bound would break the INT64_MIN literal.
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
