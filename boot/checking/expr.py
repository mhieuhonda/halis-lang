"""Checker mixin (expr) - verbatim segment of the original
boot/checker.py Checker class (lines 2231..2675), split for
maintainability. The final Checker class assembles all mixins in
boot/checking/checker.py - behavior is unchanged."""
from .helpers import (
    INT64_MAX, instantiate_type, is_list, list_elem, type_args, type_base, unify,
)
from ..compat import zip_strict

class CheckerExpr(object):
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
                # Stage 43 deep-scan fix: allow `never` as operand (e.g.
                # `-panic("...")`), propagating `never` as the result type.
                if vt == "never":
                    e["t"] = "never"
                elif vt not in ("int", "float"):
                    self.err("- operator requires int/float, got %s" % vt, e)
                else:
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
        # BUG-SC-10 fix: removed the dead `elif k == "mapnew":` branch.
        # The parser never produces a `mapnew` AST node — `map_new()` is
        # parsed as a `call` node with name="map_new" and dispatched via
        # check_call -> check_builtin_call. This branch was unreachable.
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

        SCAN-A fix: the old code re-ran `check_expr` on type disagreement,
        re-executing `drop`/`take` move-marking and producing spurious
        'use of moved value' errors. The first pass now uses the
        contextual expected payload type (inferred from the enum's
        declared payload types) so a re-check is unnecessary in nearly
        every case. The fallback re-check is restricted to the case
        where the first pass returned a placeholder type (containing '?')
        — for those, we re-check but with the contextual type passed in
        so it should produce the same answer (no second-pass divergence).
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
        # First pass: try to infer type args from arguments using the
        # contextual expected type (if available). Pass the declared
        # payload type as the contextual hint when possible — this lets
        # `[]` literals resolve to `list[T]` without a second check_expr
        # call (the source of the BUG-A spurious 'use of moved value').
        # If typeparams is non-empty, we don't yet know the instantiated
        # payload types, so the first pass uses `None`.
        if typeparams and expected is not None and type_base(expected) == ename:
            eargs = type_args(expected)
            if len(eargs) == len(typeparams):
                for tp, ea in zip_strict(typeparams, eargs):
                    type_map[tp] = ea
        first_at = []
        # Compute instantiated payload types for the first pass.
        if typeparams and type_map:
            inst_payloads_first = [instantiate_type(pt, type_map) for pt in payloads]
        elif not typeparams:
            inst_payloads_first = list(payloads)
        else:
            inst_payloads_first = [None] * len(payloads)
        for a, pt, hint in zip_strict(args, payloads, inst_payloads_first):
            at = self.check_expr(a, env, hint)
            if at == "never":
                self.err("never value cannot be used as an enum payload", e)
            first_at.append(at)
            if typeparams:
                unify(pt, at, typeparams, type_map)
            else:
                if at != pt:
                    self.err("payload type mismatch: expected %s, got %s" % (pt, at), e)
        # If any type params are still unbound, error.
        if typeparams:
            for tp in typeparams:
                if tp not in type_map:
                    self.err("cannot infer type argument for %s; provide a contextual type"
                             % tp, e)
            # Build the instantiated type.
            result_type = ename + "[" + ", ".join(type_map[tp] for tp in typeparams) + "]"
        else:
            result_type = ename
        # Compute the final instantiated payload types.
        if typeparams:
            inst_payloads = [instantiate_type(pt, type_map) for pt in payloads]
        else:
            inst_payloads = list(payloads)
        # SCAN-A fix: do NOT re-run check_expr on type disagreement.
        # The first pass already used the contextual hint, so a
        # disagreement is a real type error. If the first-pass type
        # contained a placeholder ('?'), the contextual hint wasn't
        # available — error out with a clearer message.
        for i, (a, pt) in enumerate(zip_strict(args, inst_payloads)):
            at = first_at[i]
            if at == "never":
                continue
            if at != pt:
                if "?" in at:
                    self.err("payload type mismatch: expected %s, got %s "
                             "(could not infer the contextual type)"
                             % (pt, at), e)
                else:
                    self.err("payload type mismatch: expected %s, got %s"
                             % (pt, at), e)
        # Rewrite the node in-place so the interpreter can dispatch on `enumlit`.
        e["k"] = "enumlit"
        e["enum_name"] = ename
        e["variant"] = vname
        e["payload_types"] = inst_payloads
        e["t"] = result_type
        # Stage 12 release: annotate the variant's declaration index and
        # payload type so the LLVM backend can lower the literal via
        # hl_enum_new_variant(idx) + hl_struct_set_* for the payload.
        for i, (vname2, _) in enumerate(edef["variants"]):
            if vname2 == vname:
                e["variant_idx"] = i
                break
        if inst_payloads:
            e["payload_type"] = inst_payloads[0]
        else:
            e["payload_type"] = None

    def check_bin(self, e, env):
        lt = self.check_expr(e["l"], env, None)
        rt = self.check_expr(e["r"], env, None)
        # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-06 fix.
        # `never` is the bottom type (the expression diverges — e.g.
        # `panic("...")` returns `never`). It should be compatible with
        # EVERY type (the rest of the expression is unreachable). The
        # previous code rejected `panic("...") + 1` with "never value
        # cannot be used in expression" — a false positive that broke
        # dead-code patterns like `let x: int = if c { 5 } else {
        # panic("...") }` (which works via the let-binding path) but
        # not `let x: int = panic("...") + 1` (which goes through
        # check_bin). The fix: propagate `never` instead of erroring.
        # The `+1` is dead code (panic diverges), but the checker
        # should not reject the whole expression — the codegen emits
        # the RHS as a no-op (it's unreachable).
        if lt == "never" or rt == "never":
            return "never"
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
                # Stage 77: float % float lowers to fmod (no
                # freestanding implementation).
                if op == "%" and self.is_freestanding():
                    self.err("float % is not available in "
                             "#![freestanding] mode (it lowers to fmod, "
                             "which has no freestanding implementation)", e)
                return "float"
            self.err("%s operator does not support %s and %s" % (op, lt, rt), e)
        self.err("unknown operator: %s" % op, e)

    def check_listlit(self, e, env, expected):
        elem = None
        first_vt = None  # BUG-SC-3 fix: cache first element's type
        if expected is not None and is_list(expected):
            elem = list_elem(expected)
        if elem is None:
            if not e["items"]:
                self.err("empty list literal requires a type in the surrounding context", e)
            # Check the first element ONCE to infer the element type, then
            # reuse its cached type below. Previously the loop re-checked
            # every item (including items[0]), which re-executed side
            # effects (drop/take move-marking) and produced spurious
            # "use of moved value" errors. Same class of bug as BUG-A/BUG-A2
            # in check_call/check_structlit.
            first_vt = self.check_expr(e["items"][0], env, None)
            elem = first_vt
            if elem in ("void", "never"):
                self.err("list element cannot have type %s" % elem, e)
        for i, it in enumerate(e["items"]):
            # Reuse the cached first-pass type for item 0 when available;
            # avoids re-running check_expr (which would re-execute side
            # effects like drop/take move-marking).
            if i == 0 and first_vt is not None:
                vt = first_vt
            else:
                vt = self.check_expr(it, env, elem)
            if vt == "never":
                continue
            if vt != elem:
                self.err("list element mismatch: expected %s, got %s"
                         % (elem, vt), it)
        return "list[%s]" % elem

    def check_structlit(self, e, env, expected):
        name = type_base(e["name"]) if "[" in e["name"] else e["name"]
        if name not in self.structs:
            self.err("struct does not exist: %s" % e["name"], e)
        st = self.structs[name]
        # BUG-DS4-2: constructing this struct may evaluate defaulted field
        # expressions (side effects live in the synthetic "@default.<S>"
        # call-graph node). Add the edge so the effects fixpoint propagates
        # the default's effects to the constructing function.
        #
        # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-04 fix.
        # The previous code added the @default.<S> edge UNCONDITIONALLY
        # whenever the struct had ANY defaulted field — even when the
        # literal provided ALL fields (no default is evaluated at this
        # call site). This was a false-positive in effect inference: a
        # pure function constructing a fully-specified struct whose
        # sibling default calls read_file would be rejected as "pure
        # but transitively uses Fs". The fix: only add the edge when at
        # least one defaulted field is ACTUALLY OMITTED from the literal.
        # We compute `provided_names` below (after the edge check) to
        # avoid duplicating the field-list walk; defer the edge add
        # until we know whether a default is being evaluated.
        typeparams = st["typeparams"]
        type_map = {}
        # If generic, infer type args from contextual expected type if available.
        if typeparams and expected is not None and type_base(expected) == name:
            eargs = type_args(expected)
            if len(eargs) == len(typeparams):
                for tp, ea in zip_strict(typeparams, eargs):
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
        # (F841 cleanup: the previous `defaulted` set was computed but never
        # used — the required-field check below subsumes it.)
        required = [fname for fname, _, d in fields_with_defaults if d is None]
        for r in required:
            if r not in provided_names:
                self.err("struct literal missing required field '%s'" % r, e)
        # If we've provided fewer fields than declared, the rest must have
        # defaults — checked above. Any extra fields beyond declared?
        if len(provided_names) > len(decl_names):
            self.err("struct literal %s has too many fields" % name, e)
        # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-04 fix
        # (deferred from the top of check_structlit). Now that we know
        # `provided_names` and `decl_names`, add the @default.<S> edge
        # ONLY if at least one defaulted field is actually OMITTED from
        # the literal (i.e. a default expression is being evaluated at
        # this call site). This prevents a pure function that constructs
        # a fully-specified struct from being falsely attributed the
        # sibling default's effects.
        if name in self._structs_with_defaults:
            if len(provided_names) < len(decl_names):
                # Only add the edge if at least one omitted field has a
                # default (the required-field check above guarantees
                # this, but we double-check defensively).
                omitted_have_default = False
                for i in range(len(provided_names), len(decl_names)):
                    if fields_with_defaults[i][2] is not None:
                        omitted_have_default = True
                        break
                if omitted_have_default:
                    self.edges[self.cur_fn].add("@default." + name)
        # Now type-check each provided field. We do a SINGLE pass that
        # records the resulting type for each field expression, then
        # optionally infers remaining type params from those types. This
        # avoids re-running check_expr (BUG-A: drop/take would re-execute
        # their move-marking side effects and spuriously fail with
        # "use of moved value" on the second pass).
        first_vt = []
        for i, (fname, fexpr) in enumerate(e["fields"]):
            decl_fname, decl_ftype, _ = fields_with_defaults[i]
            if typeparams and type_map:
                inst_ftype = instantiate_type(decl_ftype, type_map)
            else:
                inst_ftype = decl_ftype
            vt = self.check_expr(fexpr, env, inst_ftype)
            first_vt.append(vt)
            # If the expected type is a concrete (non-typeparam) type,
            # enforce the match. If it's an uninstantiated typeparam, defer
            # — the second pass below will infer.
            if vt == "never":
                continue
            if typeparams and not type_map:
                # Cannot enforce type when the type param is still unbound.
                continue
            if vt != inst_ftype:
                self.err("field type %s mismatch: expected %s, got %s"
                         % (fname, inst_ftype, vt), e)
        # If generic and we couldn't infer all type params from context, try
        # to infer from provided field types — reusing the first-pass types.
        if typeparams:
            for i, (fname, fexpr) in enumerate(e["fields"]):
                decl_ftype = fields_with_defaults[i][1]
                ft = first_vt[i] if i < len(first_vt) else None
                if ft is None:
                    # No first-pass result (shouldn't happen). Fall back to
                    # a single non-contextual check; this path is only
                    # reached when first_vt was not collected, e.g. if the
                    # field expression was added by the parser after the
                    # first pass.
                    ft = self.check_expr(fexpr, env, None)
                if ft == "never":
                    continue
                unify(decl_ftype, ft, typeparams, type_map)
            for tp in typeparams:
                if tp not in type_map:
                    self.err("cannot infer type argument for struct %s; provide a contextual type"
                             % name, e)
            result_type = name + "[" + ", ".join(type_map[tp] for tp in typeparams) + "]"
            # Now that we have the final type_map, re-verify the field types
            # against the INSTANTIATED types — but only on the cached
            # first-pass results, never by re-calling check_expr.
            for i, (fname, fexpr) in enumerate(e["fields"]):
                decl_ftype = fields_with_defaults[i][1]
                inst_ftype = instantiate_type(decl_ftype, type_map)
                ft = first_vt[i]
                if ft == "never":
                    continue
                if ft != inst_ftype:
                    self.err("field type %s mismatch: expected %s, got %s"
                             % (fname, inst_ftype, ft), e)
        else:
            result_type = name
        return result_type

