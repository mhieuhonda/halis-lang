"""Stage-0 type checker & effects analyzer for HLS. Conforms to SPEC.md
sections 3-12 (v0.3) — adds enum, match, generics monomorphisation, the `?`
operator, and struct default field values.

Side effect: annotates the AST so the evaluator can run quickly:
  - every expression has e['t'] = type (or 'never')
  - e['rc'] = ('user', key) | ('builtin', name) for function calls
  - e['rm'] = ('user', key) | ('builtin', op) for method calls
  - enum literals are rewritten in-place: e['k'] = 'enumlit',
    e['enum_name'], e['variant'], e['args'] (instantiated payloads)
  - match arms have arm['body_t'] = arm body type
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


def split_type_args(s):
    """Split a comma-separated list of type strings, respecting nested [ ]."""
    if s == "":
        return []
    parts = []
    depth = 0
    start = 0
    i = 0
    while i < len(s):
        c = s[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(s[start:i].strip())
            start = i + 1
        i += 1
    parts.append(s[start:].strip())
    return parts


def type_base(t):
    """For a type string, return the head identifier (before any `[`)."""
    if "[" in t:
        return t[: t.index("[")]
    return t


def type_args(t):
    """For an instantiated type, return the list of type-arg strings."""
    if "[" not in t:
        return []
    idx = t.index("[")
    return split_type_args(t[idx + 1: -1])


def instantiate_type(t, type_map):
    """Substitute type parameters in `t` according to `type_map`."""
    if t in type_map:
        return type_map[t]
    if t in ("int", "float", "bool", "str", "void"):
        return t
    if is_list(t):
        return "list[" + instantiate_type(list_elem(t), type_map) + "]"
    if is_map(t):
        return "map[str, " + instantiate_type(map_val(t), type_map) + "]"
    if "[" in t and t.endswith("]"):
        base = type_base(t)
        args = type_args(t)
        new_args = [instantiate_type(a, type_map) for a in args]
        return base + "[" + ", ".join(new_args) + "]"
    return t


def unify(pt, at, typeparams, type_map):
    """Match a parameter type `pt` against an argument type `at`, binding
    any type params in `typeparams` to concrete types in `type_map`."""
    if pt in typeparams:
        if pt in type_map and type_map[pt] != at:
            return False
        type_map[pt] = at
        return True
    if pt.startswith("list[") and at.startswith("list["):
        return unify(list_elem(pt), list_elem(at), typeparams, type_map)
    if pt.startswith("map[str, ") and at.startswith("map[str, "):
        return unify(map_val(pt), map_val(at), typeparams, type_map)
    if "[" in pt and "[" in at and type_base(pt) == type_base(at):
        pargs = type_args(pt)
        aargs = type_args(at)
        if len(pargs) != len(aargs):
            return False
        ok = True
        for p, a in zip(pargs, aargs):
            if not unify(p, a, typeparams, type_map):
                ok = False
        return ok
    return pt == at


BUILTIN_FNS = {
    "print", "println", "panic", "exit", "str", "int", "len", "range",
    "map_new", "read_file", "write_file", "args", "clock_ms", "chr",
    "file_exists",
    # Stage 8-alpha ownership primitives (v0.4.0-alpha)
    "drop", "clone", "take",
}
IO_BUILTINS = {"print", "println", "read_file", "write_file", "exit", "args", "clock_ms", "file_exists"}

# Types whose values are "owned" heap allocations — subject to move tracking.
# Primitives (int/float/bool) are Copy: passing them never moves.
def is_owned_type(t):
    """True if values of this type are heap-owned and subject to move tracking."""
    if t in ("int", "float", "bool", "void", "never"):
        return False
    return True  # str, list, map, struct, enum — all heap-allocated in v0.4

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
        self.enums = program.get("enums", {})
        self.fns = program["fns"]
        self.edges = {}
        self.methods = {}  # struct -> {meth: key}
        self.cur_fn = None
        self.cur_fn_ret = "void"  # for `?` propagation
        self.cur_typeparams = set()  # type params valid in the current context

    # ---------- utilities ----------
    def err(self, msg, node):
        raise HLError(msg, node.get("line", 0), 0)

    def type_exists(self, t, node):
        if t in ("int", "float", "bool", "str"):
            return True
        if t in self.cur_typeparams:
            return True
        if is_list(t):
            return self.type_exists(list_elem(t), node)
        if is_map(t):
            return self.type_exists(map_val(t), node)
        base = type_base(t)
        args = type_args(t)
        if base in self.structs or base in self.enums:
            tp = self.structs[base]["typeparams"] if base in self.structs \
                else self.enums[base]["typeparams"]
            if len(tp) != len(args):
                return False
            for a in args:
                if not self.type_exists(a, node):
                    return False
            return True
        return False

    def require_type(self, t, node, what):
        if t == "void":
            self.err("cannot use 'void' as %s" % what, node)
        if not self.type_exists(t, node):
            self.err("type does not exist: %s" % t, node)

    def resolve_struct(self, t):
        """If t is a (possibly generic) struct type, return (StructInfo, type_map).
        Otherwise return None."""
        base = type_base(t)
        if base not in self.structs:
            return None
        st = self.structs[base]
        tp = st["typeparams"]
        args = type_args(t)
        if len(tp) != len(args):
            return None
        type_map = dict(zip(tp, args))
        return (st, type_map)

    def resolve_enum(self, t):
        """If t is a (possibly generic) enum type, return (EnumInfo, type_map).
        Otherwise return None."""
        base = type_base(t)
        if base not in self.enums:
            return None
        en = self.enums[base]
        tp = en["typeparams"]
        args = type_args(t)
        if len(tp) != len(args):
            return None
        type_map = dict(zip(tp, args))
        return (en, type_map)

    # ---------- lifecycle ----------
    def check(self):
        # 1. group methods by struct
        for key, fn in self.fns.items():
            if fn["struct"] is not None:
                m = self.methods.setdefault(fn["struct"], {})
                m[fn["name"]] = key
        # 2. check declarations (struct/enum field types, function signatures)
        for name, st in self.structs.items():
            self.cur_typeparams = set(st["typeparams"])
            for fname, ftype, _ in st["fields"]:
                self.require_type(ftype, st, "struct field type")
            self.cur_typeparams = set()
        for ename, en in self.enums.items():
            self.cur_typeparams = set(en["typeparams"])
            for vname, payloads in en["variants"]:
                for pt in payloads:
                    self.require_type(pt, en, "variant payload type")
            self.cur_typeparams = set()
        for key, fn in self.fns.items():
            self.cur_typeparams = set(fn.get("typeparams", []))
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
            self.cur_typeparams = set()
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
    # Bindings are now [type, mut, moved] (3-tuple) — `moved` is True after
    # `drop(x)` or `take(x)`, which forbids subsequent use of `x` until it is
    # re-assigned. See sections 16 (Stage 8-alpha) in SPEC.md.
    def new_env(self, fn):
        env = [{}]
        if fn["struct"] is not None:
            sname, stype, smut = fn["params"][0]
            env[0][sname] = [stype, smut, False]
            params = fn["params"][1:]
        else:
            params = fn["params"]
        for pn, pt, pm in params:
            if pn in env[0]:
                self.err("duplicate parameter name: %s" % pn, fn)
            env[0][pn] = [pt, pm, False]
        return env

    def lookup(self, env, name):
        for scope in reversed(env):
            if name in scope:
                return scope[name]
        return None

    def mark_moved(self, env, name):
        """Mark a binding as moved (drop/take). Returns True on success."""
        for scope in reversed(env):
            if name in scope:
                scope[name][2] = True
                return True
        return False

    def revive_binding(self, env, name):
        """Re-clear moved status on reassignment (binding becomes 're-owned')."""
        for scope in reversed(env):
            if name in scope:
                scope[name][2] = False
                return True
        return False

    def snapshot_moved(self, env):
        """Take a snapshot of moved-status for all bindings (for scope restore)."""
        snap = []
        for scope in env:
            row = {}
            for name, b in scope.items():
                row[name] = b[2]
            snap.append(row)
        return snap

    def restore_moved(self, env, snap):
        """Restore moved-status from a snapshot (used when child scope exits)."""
        for scope, snap_row in zip(env, snap):
            for name, moved in snap_row.items():
                if name in scope:
                    scope[name][2] = moved

    def check_fn(self, key, fn):
        self.cur_fn = key
        self.cur_fn_ret = fn["ret"]
        saved_typeparams = self.cur_typeparams
        self.cur_typeparams = set(fn.get("typeparams", []))
        env = self.new_env(fn)
        self.check_stmts(fn["body"], env, fn, False)
        if fn["ret"] != "void" and not self.all_return(fn["body"]):
            self.err("function '%s' does not return on all paths" % fn["name"], fn)
        self.cur_typeparams = saved_typeparams

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
        if last["k"] == "expr" and last["e"]["k"] == "match" and \
           self.match_all_return(last["e"]):
            return True
        return False

    def match_all_return(self, e):
        for arm in e["arms"]:
            if not (arm["body"]["k"] == "return" or
                    (arm["body"]["k"] == "expr" and arm["body"].get("t") == "never")):
                return False
        return True

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
                env[-1][s["name"]] = [s["t"], s["mut"], False]
                return
            if vt != s["t"]:
                self.err("type mismatch: declared %s but got %s"
                         % (s["t"], vt), s)
            env[-1][s["name"]] = [s["t"], s["mut"], False]
        elif k == "assign":
            self.check_assign(s, env)
        elif k == "if":
            ct = self.check_expr(s["cond"], env, None)
            if ct != "bool":
                self.err("if condition must be bool, got %s" % ct, s)
            # Stage 8-alpha: take a snapshot of moved-status so moves done in
            # the then/else branches don't leak out (a binding moved inside an
            # if-arm should not be considered moved after the if completes).
            snap = self.snapshot_moved(env)
            self.child(env)
            self.check_stmts(s["then"], env, fn, in_loop)
            env.pop()
            if s["els"] is not None:
                self.child(env)
                self.check_stmts(s["els"], env, fn, in_loop)
                env.pop()
            self.restore_moved(env, snap)
        elif k == "while":
            ct = self.check_expr(s["cond"], env, None)
            if ct != "bool":
                self.err("while condition must be bool, got %s" % ct, s)
            # Moves inside the loop body don't leak out — the loop may execute
            # zero or many times, so any post-loop use must remain valid.
            snap = self.snapshot_moved(env)
            self.child(env)
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
            self.restore_moved(env, snap)
        elif k == "for":
            it = self.check_expr(s["iter"], env, None)
            if not is_list(it):
                self.err("for-in expression must be a list, got %s" % it, s)
            elem = list_elem(it)
            if s["vtype"] != elem:
                self.err("loop variable type %s does not match element %s"
                         % (s["vtype"], elem), s)
            snap = self.snapshot_moved(env)
            self.child(env)
            env[-1][s["var"]] = [elem, False, False]
            self.check_stmts(s["body"], env, fn, True)
            env.pop()
            self.restore_moved(env, snap)
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
        # Stage 8-alpha: use-after-move check on the LHS root.
        # Reading a moved binding (even to reassign) requires `mut` first.
        # For `mut x = ...`, the binding is "revived" — its moved flag is cleared.
        # For `x.field = ...` / `xs[i] = ...`, the binding itself is being read
        # (to obtain the reference), so moved bindings cannot be used.
        if len(binding) >= 3 and binding[2]:
            if tgt["k"] == "ident":
                # Whole-binding assignment: this is allowed on `let mut` only.
                # The binding will be revived below after the RHS is checked.
                if not binding[1]:
                    self.err("cannot reassign immutable variable: %s" % root["name"], s)
            else:
                # Field/index assignment — uses the binding's value (reference).
                self.err("use of moved value: %s" % root["name"], s)
        else:
            # 'mut' only governs REASSIGNMENT of the binding (name = v).
            # Field/index assignment mutates CONTENTS through a reference — no mut needed.
            if tgt["k"] == "ident" and not binding[1]:
                self.err("cannot reassign immutable variable: %s" % root["name"], s)
        tt = self.check_lvalue(tgt, env)
        vt = self.check_expr(s["value"], env, tt)
        if vt == "never":
            return
        if vt != tt:
            self.err("type mismatch on assignment: expected %s, got %s" % (tt, vt), s)
        # Stage 8-alpha: revive the binding (clear moved) on whole-binding
        # assignment. The binding now owns a fresh value.
        if tgt["k"] == "ident":
            self.revive_binding(env, root["name"])

    def check_lvalue(self, e, env):
        if e["k"] == "ident":
            b = self.lookup(env, e["name"])
            return b[0]
        if e["k"] == "field":
            bt = self.check_expr(e["target"], env, None)
            info = self.resolve_struct(bt)
            if info is None:
                self.err("cannot access field on type %s" % bt, e)
            st, type_map = info
            for fname, ftype, _ in st["fields"]:
                if fname == e["name"]:
                    return instantiate_type(ftype, type_map) if type_map else ftype
            self.err("struct %s has no field %s" % (type_base(bt), e["name"]), e)
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
            if b is not None:
                # Stage 8-alpha: use-after-move check
                if len(b) >= 3 and b[2]:
                    self.err("use of moved value: %s" % e["name"], e)
                e["t"] = b[0]
            elif e["name"] in self.enums:
                # bare enum name used as a value — only valid inside an enum
                # variant literal `Enum.Variant`; otherwise it's a type error.
                self.err("enum name '%s' cannot be used as a value" % e["name"], e)
            elif e["name"] in self.structs:
                self.err("struct name '%s' cannot be used as a value" % e["name"], e)
            else:
                self.err("variable does not exist: %s" % e["name"], e)
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
            # Disambiguate: if target is `ident` matching an enum name AND no
            # variable with that name exists in scope, this is an enum variant
            # with no payload.
            if e["target"]["k"] == "ident" and \
               e["target"]["name"] in self.enums and \
               self.lookup(env, e["target"]["name"]) is None:
                self.check_enum_variant(e, env, expected, with_args=False)
            else:
                tt = self.check_expr(e["target"], env, None)
                if tt == "never":
                    self.err("never value cannot be used in expression", e)
                info = self.resolve_struct(tt)
                if info is None:
                    self.err("cannot access field on type %s" % tt, e)
                st, type_map = info
                e["t"] = None
                for fname, ftype, _ in st["fields"]:
                    if fname == e["name"]:
                        e["t"] = instantiate_type(ftype, type_map) if type_map else ftype
                        break
                if e["t"] is None:
                    self.err("struct %s has no field %s" % (type_base(tt), e["name"]), e)
        elif k == "fieldcall":
            # Disambiguate: if target is `ident` matching an enum name AND no
            # variable with that name exists, this is an enum variant with
            # payload. Otherwise it's a method call.
            if e["target"]["k"] == "ident" and \
               e["target"]["name"] in self.enums and \
               self.lookup(env, e["target"]["name"]) is None:
                self.check_enum_variant(e, env, expected, with_args=True)
            else:
                # Rewrite as a method call.
                e["k"] = "method"
                e["t"] = self.check_method(e, env)
        elif k == "method":
            e["t"] = self.check_method(e, env)
        elif k == "call":
            e["t"] = self.check_call(e, env, expected)
        elif k == "listlit":
            e["t"] = self.check_listlit(e, env, expected)
        elif k == "structlit":
            e["t"] = self.check_structlit(e, env, expected)
        elif k == "match":
            e["t"] = self.check_match(e, env, expected)
        elif k == "qmark":
            e["t"] = self.check_qmark(e, env, expected)
        elif k == "mapnew":
            if expected is None or not is_map(expected):
                self.err("map_new() requires a 'map[str, T]' type in the surrounding context", e)
            e["t"] = expected
        elif k == "enumlit":
            # Already rewritten during a previous visit (e.g. nested); re-evaluate.
            self.check_enum_variant(e, env, expected, with_args=len(e.get("args", [])) > 0)
        else:
            self.err("unknown expression: %s" % k, e)
        return e["t"]

    def check_enum_variant(self, e, env, expected, with_args):
        """Check an enum variant literal. The node `e` is either:
          - {k: 'field', target: {k:'ident', name: EnumName}, name: VariantName}
            — no payload
          - {k: 'fieldcall', target: {...}, name: VariantName, args: [...]}
            — with payload
        The node is rewritten in-place to k='enumlit'.
        """
        ename = e["target"]["name"]
        vname = e["name"]
        if ename not in self.enums:
            self.err("enum does not exist: %s" % ename, e)
        edef = self.enums[ename]
        variant = None
        for v, payloads in edef["variants"]:
            if v == vname:
                variant = (v, payloads)
                break
        if variant is None:
            self.err("enum %s has no variant %s" % (ename, vname), e)
        v, payloads = variant
        args = e.get("args", []) if with_args else []
        if len(args) != len(payloads):
            self.err("variant %s of enum %s expects %d payloads, got %d"
                     % (vname, ename, len(payloads), len(args)), e)
        typeparams = edef["typeparams"]
        type_map = {}
        # First pass: infer type args from arguments.
        for a, pt in zip(args, payloads):
            at = self.check_expr(a, env, None)
            if at == "never":
                self.err("never value cannot be used as an enum payload", e)
            if typeparams:
                unify(pt, at, typeparams, type_map)
            else:
                if at != pt:
                    self.err("payload type mismatch: expected %s, got %s" % (pt, at), e)
        # Second pass: if any type params are still unbound, try the contextual
        # expected type.
        if typeparams and expected is not None and type_base(expected) == ename:
            eargs = type_args(expected)
            if len(eargs) == len(typeparams):
                for tp, ea in zip(typeparams, eargs):
                    if tp not in type_map:
                        type_map[tp] = ea
                    # If conflicting, trust the argument-inferred one
        # If any type param is still unbound, error.
        if typeparams:
            for tp in typeparams:
                if tp not in type_map:
                    self.err("cannot infer type argument for %s; provide a contextual type"
                             % tp, e)
            # Build the instantiated type.
            result_type = ename + "[" + ", ".join(type_map[tp] for tp in typeparams) + "]"
        else:
            result_type = ename
        # Type-check payloads against instantiated payload types.
        if typeparams:
            inst_payloads = [instantiate_type(pt, type_map) for pt in payloads]
        else:
            inst_payloads = list(payloads)
        for a, pt in zip(args, inst_payloads):
            at = self.check_expr(a, env, pt)
            if at == "never":
                continue
            if at != pt:
                self.err("payload type mismatch: expected %s, got %s" % (pt, at), e)
        # Rewrite the node in-place so the interpreter can dispatch on `enumlit`.
        e["k"] = "enumlit"
        e["enum_name"] = ename
        e["variant"] = vname
        e["payload_types"] = inst_payloads
        e["t"] = result_type

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

    def check_structlit(self, e, env, expected):
        name = type_base(e["name"]) if "[" in e["name"] else e["name"]
        if name not in self.structs:
            self.err("struct does not exist: %s" % e["name"], e)
        st = self.structs[name]
        typeparams = st["typeparams"]
        type_map = {}
        # If generic, infer type args from contextual expected type if available.
        if typeparams and expected is not None and type_base(expected) == name:
            eargs = type_args(expected)
            if len(eargs) == len(typeparams):
                for tp, ea in zip(typeparams, eargs):
                    type_map[tp] = ea
        # Determine the struct's effective field types (instantiate or not).
        fields_with_defaults = st["fields"]
        # Fields provided in the literal must come in declaration order, and
        # any fields with defaults may be omitted. We allow the user to omit
        # only trailing fields that have defaults.
        provided_names = [fname for fname, _ in e["fields"]]
        # Validate names against declared fields.
        decl_names = [fname for fname, _, _ in fields_with_defaults]
        for i, fname in enumerate(provided_names):
            if i >= len(decl_names) or decl_names[i] != fname:
                self.err("struct literal field order mismatch at position %d: expected '%s', got '%s'"
                         % (i, decl_names[i] if i < len(decl_names) else "<end>", fname), e)
        # Determine if all non-defaulted fields are present.
        defaulted = {fname for fname, _, d in fields_with_defaults if d is not None}
        required = [fname for fname, _, d in fields_with_defaults if d is None]
        for r in required:
            if r not in provided_names:
                self.err("struct literal missing required field '%s'" % r, e)
        # If we've provided fewer fields than declared, the rest must have
        # defaults — checked above. Any extra fields beyond declared?
        if len(provided_names) > len(decl_names):
            self.err("struct literal %s has too many fields" % name, e)
        # Now type-check each provided field.
        for i, (fname, fexpr) in enumerate(e["fields"]):
            decl_fname, decl_ftype, _ = fields_with_defaults[i]
            if typeparams and type_map:
                inst_ftype = instantiate_type(decl_ftype, type_map)
            else:
                inst_ftype = decl_ftype
            vt = self.check_expr(fexpr, env, inst_ftype)
            if vt != inst_ftype:
                self.err("field type %s mismatch: expected %s, got %s"
                         % (fname, inst_ftype, vt), e)
        # If generic and we couldn't infer all type params from context, try
        # to infer from provided field types.
        if typeparams:
            for i, (fname, fexpr) in enumerate(e["fields"]):
                decl_ftype = fields_with_defaults[i][1]
                ft = self.check_expr(fexpr, env, None)
                if ft == "never":
                    continue
                unify(decl_ftype, ft, typeparams, type_map)
            for tp in typeparams:
                if tp not in type_map:
                    self.err("cannot infer type argument for struct %s; provide a contextual type"
                             % name, e)
            result_type = name + "[" + ", ".join(type_map[tp] for tp in typeparams) + "]"
        else:
            result_type = name
        return result_type

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
            # Generic function: infer type args from argument types.
            typeparams = fn.get("typeparams", [])
            type_map = {}
            if typeparams:
                for a, (pn, pt, _) in zip(args, fn["params"]):
                    at = self.check_expr(a, env, None)
                    if at == "never":
                        continue
                    unify(pt, at, typeparams, type_map)
                for tp in typeparams:
                    if tp not in type_map:
                        self.err("cannot infer type argument for %s; provide explicit types"
                                 % tp, e)
            # Type-check arguments against instantiated parameter types.
            for a, (pn, pt, _) in zip(args, fn["params"]):
                if typeparams:
                    inst_pt = instantiate_type(pt, type_map)
                else:
                    inst_pt = pt
                at = self.check_expr(a, env, inst_pt)
                if at == "never":
                    continue
                if at != inst_pt:
                    self.err("argument '%s' of %s expects %s, got %s"
                             % (pn, name, inst_pt, at), a)
            if typeparams:
                return instantiate_type(fn["ret"], type_map)
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
        # ----- Stage 8-alpha: ownership primitives (drop / clone / take) -----
        if name == "drop":
            need(1)
            at = argt(0, None)
            if not is_owned_type(at):
                self.err("drop() requires an owned (heap) type, got %s" % at, e)
            # The argument must be a simple `ident` lvalue — we need a binding
            # to mark as moved. Complex expressions are not allowed.
            arg = args[0]
            if arg["k"] != "ident":
                self.err("drop() requires a variable name (not an expression)", e)
            # Mark the binding as moved.
            if not self.mark_moved(env, arg["name"]):
                self.err("drop() argument is not a binding: %s" % arg["name"], e)
            return "void"
        if name == "clone":
            need(1)
            at = argt(0, None)
            if not is_owned_type(at):
                self.err("clone() requires an owned (heap) type, got %s" % at, e)
            if not self.is_clone_supported(at):
                self.err("clone() on type %s is not supported in v0.4.0-alpha "
                         "(only str, list/map of int/float/bool/str are supported)"
                         % at, e)
            # Argument is consumed by value (read), not moved.
            return at
        if name == "take":
            need(1)
            at = argt(0, None)
            if not is_owned_type(at):
                self.err("take() requires an owned (heap) type, got %s" % at, e)
            arg = args[0]
            if arg["k"] != "ident":
                self.err("take() requires a variable name (not an expression)", e)
            if not self.mark_moved(env, arg["name"]):
                self.err("take() argument is not a binding: %s" % arg["name"], e)
            return at
        self.err("unknown builtin function: %s" % name, e)

    @staticmethod
    def is_clone_supported(t):
        """Types supported by clone() in v0.4.0-alpha. Stage 8-beta will
        expand this to all heap types via per-instantiation helpers."""
        if t == "str":
            return True
        if is_list(t):
            return list_elem(t) in ("int", "float", "bool", "str")
        if is_map(t):
            return map_val(t) in ("int", "float", "bool", "str")
        return False

    def check_method(self, e, env):
        tt = self.check_expr(e["target"], env, None)
        if tt == "never":
            self.err("never value cannot be used in expression", e)
        name = e["name"]
        args = e["args"]
        info = self.resolve_struct(tt)
        if info is not None:
            # User-defined struct method. The struct is the base name (e.g.,
            # "Box" for "Box[int]"). Methods are registered under the base
            # name in self.methods.
            tt_base = type_base(tt)
            m = self.methods.get(tt_base, {})
            if name not in m:
                self.err("struct %s has no method %s" % (tt_base, name), e)
            key = m[name]
            e["rm"] = ("user", key)
            fn = self.fns[key]
            params = fn["params"][1:]
            if len(args) != len(params):
                self.err("%s.%s expects %d arguments, got %d"
                         % (tt_base, name, len(params), len(args)), e)
            # Generic method: infer type args from arguments.
            typeparams = fn.get("typeparams", [])
            # If the struct is itself generic, propagate its type args.
            st, struct_type_map = info
            type_map = dict(struct_type_map)
            if typeparams:
                for a, (pn, pt, _) in zip(args, params):
                    at = self.check_expr(a, env, None)
                    if at == "never":
                        continue
                    unify(pt, at, typeparams, type_map)
                for tp in typeparams:
                    if tp not in type_map:
                        self.err("cannot infer type argument for %s.%s" % (tt_base, name), e)
            for a, (pn, pt, _) in zip(args, params):
                if type_map:
                    inst_pt = instantiate_type(pt, type_map)
                else:
                    inst_pt = pt
                at = self.check_expr(a, env, inst_pt)
                if at == "never":
                    continue
                if at != inst_pt:
                    self.err("argument '%s' of %s.%s expects %s, got %s"
                             % (pn, tt_base, name, inst_pt, at), a)
            if type_map:
                return instantiate_type(fn["ret"], type_map)
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
        elif "[" in tt and type_base(tt) in self.enums:
            # Method on a generic enum instantiation — currently we do not
            # support user-defined methods on enums. Future: enable `impl`.
            self.err("enum %s has no method %s (enum impl not supported yet)"
                     % (type_base(tt), name), e)
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

    # ---------- match ----------
    def check_match(self, e, env, expected):
        scrut_t = self.check_expr(e["scrut"], env, None)
        if scrut_t == "never":
            self.err("never value cannot be used in match", e)
        if "[" in scrut_t:
            ename = type_base(scrut_t)
        else:
            ename = scrut_t
        if ename not in self.enums:
            self.err("match scrutinee must be an enum, got %s" % scrut_t, e)
        edef = self.enums[ename]
        # For generic enums, instantiate variant payloads from scrut_t.
        typeparams = edef["typeparams"]
        type_map = {}
        if typeparams:
            sargs = type_args(scrut_t)
            if len(sargs) != len(typeparams):
                self.err("invalid enum instantiation: %s" % scrut_t, e)
            for tp, sa in zip(typeparams, sargs):
                type_map[tp] = sa
        # Check exhaustiveness and arm types.
        covered = set()
        has_wildcard = False
        arm_types = []
        for arm in e["arms"]:
            pat = arm["pattern"]
            if pat["k"] == "wildcard":
                has_wildcard = True
            else:
                pen = pat["enum"]
                pv = pat["variant"]
                if pen != ename:
                    self.err("match arm pattern belongs to enum %s, not %s"
                             % (pen, ename), arm)
                # find the variant
                variant = None
                for v, payloads in edef["variants"]:
                    if v == pv:
                        variant = (v, payloads)
                        break
                if variant is None:
                    self.err("enum %s has no variant %s" % (ename, pv), arm)
                v, payloads = variant
                covered.add(v)
                # Check binding count.
                if pat["has_paren"] and len(pat["bindings"]) != len(payloads):
                    self.err("variant %s has %d payloads, but pattern binds %d"
                             % (pv, len(payloads), len(pat["bindings"])), arm)
                if not pat["has_paren"] and len(payloads) != 0:
                    self.err("variant %s requires %d payload bindings"
                             % (pv, len(payloads)), arm)
            # Check arm body in a new scope with the bindings.
            self.child(env)
            if pat["k"] != "wildcard":
                # Bind payload values.
                if pat["has_paren"]:
                    # find variant payloads
                    for v, payloads in edef["variants"]:
                        if v == pat["variant"]:
                            inst_payloads = [instantiate_type(p, type_map) if typeparams
                                             else p for p in payloads]
                            for bname, btype in zip(pat["bindings"], inst_payloads):
                                if bname == "_":
                                    continue
                                if self.lookup(env, bname) is not None:
                                    self.err("shadowing not allowed in match arm: %s" % bname, arm)
                                env[-1][bname] = [btype, False, False]
                            break
            body_t = self.check_expr(arm["body"], env, expected)
            arm["body_t"] = body_t
            env.pop()
            if body_t == "never":
                arm_types.append(None)
            else:
                arm_types.append(body_t)
        # Exhaustiveness check.
        all_variants = {v for v, _ in edef["variants"]}
        if not has_wildcard:
            missing = all_variants - covered
            if missing:
                self.err("match is not exhaustive; missing: %s"
                         % ", ".join(sorted(missing)), e)
        # All arm types must agree.
        non_never = [t for t in arm_types if t is not None]
        if not non_never:
            # All arms are `never` — match type is never.
            e["t"] = "never"
            return "never"
        first = non_never[0]
        for t in non_never:
            if t != first:
                self.err("match arms have different types: %s and %s" % (first, t), e)
        e["t"] = first
        return first

    # ---------- `?` operator ----------
    def check_qmark(self, e, env, expected):
        inner_t = self.check_expr(e["e"], env, None)
        if inner_t == "never":
            self.err("never value cannot be used in expression", e)
        ename = type_base(inner_t) if "[" in inner_t else inner_t
        if ename not in self.enums:
            self.err("? operator requires an enum type, got %s" % inner_t, e)
        edef = self.enums[ename]
        # Find the "error" variant: Err (with one payload) or None (no payload).
        err_variant = None
        ok_variant = None
        for v, payloads in edef["variants"]:
            if v == "Err" and len(payloads) == 1:
                err_variant = (v, payloads)
            elif v == "None" and len(payloads) == 0:
                err_variant = (v, payloads)
            elif v == "Ok" and len(payloads) == 1:
                ok_variant = (v, payloads)
            elif v == "Some" and len(payloads) == 1:
                ok_variant = (v, payloads)
        if err_variant is None:
            self.err("? operator requires enum %s to have an 'Err' (1 payload) or 'None' variant"
                     % ename, e)
        if ok_variant is None:
            self.err("? operator requires enum %s to have an 'Ok' or 'Some' variant (1 payload)"
                     % ename, e)
        # The success value type is the (instantiated) payload type of the
        # success variant.
        typeparams = edef["typeparams"]
        if typeparams:
            type_map = {}
            iargs = type_args(inner_t)
            if len(iargs) != len(typeparams):
                self.err("invalid enum instantiation: %s" % inner_t, e)
            for tp, ia in zip(typeparams, iargs):
                type_map[tp] = ia
            ok_payload = ok_variant[1][0]
            success_t = instantiate_type(ok_payload, type_map)
        else:
            success_t = ok_variant[1][0]
        # Check that the enclosing function's return type is compatible.
        cur_ret = self.cur_fn_ret
        if cur_ret == "void":
            self.err("? operator cannot be used in a void function", e)
        cur_ret_base = type_base(cur_ret) if "[" in cur_ret else cur_ret
        if cur_ret_base != ename:
            self.err("? operator: enclosing function returns %s, cannot propagate %s"
                     % (cur_ret, ename), e)
        e["t"] = success_t
        e["err_variant"] = err_variant[0]
        e["ok_variant"] = ok_variant[0]
        return success_t

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
