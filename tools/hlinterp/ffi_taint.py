"""Stage 109 (v0.128.0-alpha): the FFI taint-flow report.

The checker already polices every boundary statically: tainted[T]
values cannot reach a sink (builtin or #[taint_sink(...)] extern
parameter), and #[taint_source] externs hand every call site a
tainted[ret] so untrusted data travels the ordinary taint lattice.
What the type system deliberately does NOT decide is the politics of
the escape hatch: `taint_unwrap()` is the user's "I accept the risk",
and nothing above the checker answers the auditor's question — WHICH
data escaped, FROM WHERE, and did it ever get near a sink?

This pass answers it. One fixpoint over the call graph, then one
flow-sensitive walk per fn with two environments:

  tainted[var]  -> provenance  the var holds a tainted[T] value
  escaped[var]  -> provenance  the var holds data that WAS tainted and
                               was unwrapped (the hatch has been used)

Provenance is a short label naming the origin: "ffi:getenv" for a
marked extern, "builtin:tainted_args" for argv, "builtin:read_line"
for stdin, "builtin:read_file_tainted" for file contents, "param:p"
for a parameter the fixpoint marked, "taint_mark" for a literal the
author marked by hand. A sanitizer call (std.sanitize.*) cuts the
chain — the escaped data is neutralised and reported as a defence,
not a flow. A `.len()`-style pure query reads the value but returns a
non-str, so it neither propagates nor flows.

The report prints, per fn:
  - every #[taint_source] call site (the entry points),
  - every taint_unwrap site with its provenance (the escapes),
  - every sanitizer call (the cut points),
  - every sink an escaped value reaches (the accepted-risk flows),
with the `FFI TAINT TOTAL:` line the acceptance gate pins.
"""

# The builtin taint sources (mirrors the checker's source builtins;
# taint_mark is the manual wrap — its provenance labels the value as
# author-marked rather than attacker-controlled).
SOURCE_BUILTINS = ("tainted_args", "read_file_tainted", "read_line")

# The builtin sinks and the argument positions that reject tainted
# values — the same table the checker enforces, spelled out for the
# report (a flow to ANY of these positions counts as sink-reaching).
SINK_BUILTINS = {
    "print": (0,), "println": (0,), "eprint": (0,), "eprintln": (0,),
    "exit": (0,),
    "read_file": (0,), "read_file_tainted": (0,), "file_exists": (0,),
    "write_file": (0, 1),
    "fs_read_dir": (0,), "fs_size": (0,), "fs_is_dir": (0,),
    "fs_set_perms": (0,),
    "net_lookup": (0,), "net_tcp_connect": (0,), "net_tcp_listen": (0,),
    "net_udp_send_to": (1,), "net_tls_get": (0, 2),
    "proc_exec": (0,), "proc_spawn": (0,),
    "env_get": (0,), "env_has": (0,), "env_set": (0,), "env_unset": (0,),
    "cwd_set": (0,),
}

# The std.sanitize helpers — each one cuts the taint chain (the value
# that comes back is clean by construction).
SANITIZERS = ("sanitize_html", "sanitize_path", "sanitize_sql_identifier",
              "sanitize_sql_string", "sanitize_command", "sanitize_filename")


class FfiBoundary:
    """One extern declaration's entry in the census."""
    __slots__ = ("name", "abi", "params", "ptypes", "ret",
                 "taint_source", "taint_sinks", "line")

    def __init__(self, name, abi, params, ptypes, ret,
                 taint_source, taint_sinks, line):
        self.name = name
        self.abi = abi
        self.params = params
        self.ptypes = ptypes
        self.ret = ret
        self.taint_source = taint_source
        self.taint_sinks = taint_sinks
        self.line = line

    @property
    def kind(self):
        if self.taint_source:
            return "source"
        if self.taint_sinks:
            return "sink"
        return "unmarked"


class FfiFinding:
    """One reported line inside one fn (a source site, an unwrap, a
    sanitizer cut or a sink-reaching flow)."""
    __slots__ = ("kind", "line", "text", "prov")

    def __init__(self, kind, line, text, prov):
        self.kind = kind
        self.line = line
        self.text = text
        self.prov = prov

    def key(self):
        return (self.kind, self.line, self.text)


class FnFfiTaint:
    """The per-fn report."""
    __slots__ = ("fn_key", "tainted_params", "findings", "incoming")

    def __init__(self, fn_key):
        self.fn_key = fn_key
        self.tainted_params = []   # params marked by the fixpoint
        self.findings = []         # deduped FfiFinding list
        self.incoming = []         # (caller, param, line) taint flows


def _call_target(e):
    """The user-callee fn key of a call/method node (checker tag), or
    the extern fn name for an extern call (tag ("extern", name))."""
    tag = e.get("rc") or e.get("rm")
    if isinstance(tag, (list, tuple)) and len(tag) == 2:
        if tag[0] in ("user", "extern"):
            return tag[1]
    return None


def _builtin_op(e):
    tag = e.get("rc") or e.get("rm")
    if isinstance(tag, (list, tuple)) and len(tag) == 2 and tag[0] == "builtin":
        return tag[1]
    return None


def _text(e):
    """Short textual rendering of an expression (for the finding line)."""
    if not isinstance(e, dict):
        return "?"
    k = e.get("k")
    if k in ("int", "float", "bool"):
        return str(e.get("v"))
    if k == "str":
        return '"..."'
    if k == "ident":
        return e.get("name", "?")
    if k == "un":
        return "%s%s" % (e.get("op"), _text(e.get("e")))
    if k == "bin":
        return "(%s %s %s)" % (_text(e.get("l")), e.get("op"),
                               _text(e.get("r")))
    if k == "call":
        args = ", ".join(_text(a) for a in (e.get("args") or []))
        return "%s(%s)" % (e.get("name", "?"), args)
    if k == "method":
        args = ", ".join(_text(a) for a in (e.get("args") or []))
        return "%s.%s(%s)" % (_text(e.get("target")), e.get("name"), args)
    if k == "index":
        return "%s[%s]" % (_text(e.get("target")), _text(e.get("idx")))
    if k == "field":
        return "%s.%s" % (_text(e.get("target")), e.get("name", "?"))
    return "?"


def _root_name(e):
    """The variable at the base of an lvalue chain (xs[i].f -> xs)."""
    while isinstance(e, dict) and e.get("k") in ("index", "field"):
        e = e.get("target")
    if isinstance(e, dict) and e.get("k") == "ident":
        return e.get("name")
    return None


class _State:
    """The interprocedural fixpoint state: which fn parameters may
    carry tainted data, and which fns may return it. Mirrors the
    side-channel pass's state shape."""

    def __init__(self, program):
        self.fns = program["fns"]
        self.params = {key: set() for key in program["fns"]}
        self.rets = {key: False for key in program["fns"]}
        self.changed = False


class _Walker:
    """One flow-sensitive pass over one fn body. The `tainted` and
    `escaped` predicates propagate the two environments; `flag`
    records the findings; the statement walk joins branches (union)
    and widens loops (two passes)."""

    def __init__(self, fn_key, fn, state, ffi_by_name):
        self.fn_key = fn_key
        self.fn = fn
        self.state = state
        self.ffi_by_name = ffi_by_name
        self.findings = {}
        self.flows = []       # (callee, param, line) taint edges
        self.ret_tainted = False

    # -- reporting ------------------------------------------------------

    def finding(self, kind, line, text, prov):
        f = FfiFinding(kind, line, text, prov)
        self.findings.setdefault(f.key(), f)

    def _flow(self, callee, param, line):
        """Mark the callee's parameter as tainted-carrying for the
        next fixpoint round and record the edge for the report."""
        if callee not in self.state.params:
            return
        if param not in self.state.params[callee]:
            self.state.params[callee].add(param)
            self.state.changed = True
        self.flows.append((callee, param, line))

    # -- the provenance predicates ----------------------------------------
    # Both return a provenance label (truthy) or None (clean). `tainted`
    # tracks tainted[T] values; `escaped` tracks unwrapped data that
    # CARRIED taint before the hatch.

    def _prop_of_method(self, e, env, which):
        """Shared method rule: len-style queries are clean (public by
        policy, the same note the side-channel pass makes); everything
        else derives from the receiver or the args."""
        op = _builtin_op(e)
        if op in ("list.len", "str.len", "map.len", "chan.len",
                  "str.starts_with", "str.ends_with", "str.contains",
                  "list.has", "map.has", "list.len"):
            return None
        prov = self._prop(e.get("target"), env, which)
        for a in (e.get("args") or []):
            p = self._prop(a, env, which)
            if p and not prov:
                prov = p
        return prov

    def _prop(self, e, env, which):
        """The provenance of `e` under environment `env` (env maps
        var -> label or None). `which` selects the environment table
        ('tainted' or 'escaped')."""
        if not isinstance(e, dict):
            return None
        k = e.get("k")
        if k in ("int", "float", "bool", "str"):
            return None
        if k == "ident":
            return env[which].get(e.get("name"))
        if k == "un":
            return self._prop(e.get("e"), env, which)
        if k == "bin":
            return (self._prop(e.get("l"), env, which)
                    or self._prop(e.get("r"), env, which))
        if k in ("index", "field"):
            return self._prop(e.get("target"), env, which)
        if k == "listlit":
            prov = None
            for i in (e.get("items") or []):
                p = self._prop(i, env, which)
                if p and not prov:
                    prov = p
            return prov
        if k in ("fieldcall", "enumlit"):
            prov = self._prop(e.get("target"), env, which)
            for a in (e.get("args") or []):
                p = self._prop(a, env, which)
                if p and not prov:
                    prov = p
            return prov
        if k == "method":
            return self._prop_of_method(e, env, which)
        if k == "call":
            name = e.get("name")
            args = e.get("args") or []
            # The taint builtins decide the environment switch. The
            # discipline: a TAINTED value never shows up in the
            # escaped env (it is still typed tainted[T]), and an
            # ESCAPED value never shows up in the tainted env (the
            # hatch has already been used on it) — only the unwrap
            # and mark builtins move data between the tables.
            if name == "taint_unwrap":
                # Unwrapping kills the taint: the result is
                # clean-typed, its ESCAPED provenance is the inner
                # value's origin (the data did not become neutral).
                if which == "tainted":
                    return None
                inner = args[0] if args else None
                return (self._prop(inner, env, "tainted")
                        or self._prop(inner, env, "escaped"))
            if name == "taint_mark":
                # Marking (re-)taints a value — the result is typed
                # tainted[T] even when the inner data was escaped.
                if which == "escaped":
                    return None
                inner = args[0] if args else None
                return (self._prop(inner, env, "escaped")
                        or self._prop(inner, env, "tainted")
                        or "taint_mark")
            if name in SANITIZERS:
                return None  # the cut point
            if name in SOURCE_BUILTINS:
                # A builtin source's result is tainted[T] — it lives
                # only in the tainted env.
                if which == "tainted":
                    return "builtin:" + name
                return None
            # A user/extern callee: tainted iff a param flows in, the
            # callee may return tainted data, or the callee is a
            # #[taint_source] extern.
            key = _call_target(e)
            fb = self.ffi_by_name.get(key) if key else None
            if fb is not None:
                # An extern's return semantics are the C side's:
                # untrusted ONLY when marked #[taint_source].
                # Argument provenance stops at the boundary (the
                # checker already rejected tainted arguments; the
                # return is not derived from them).
                if fb.taint_source and which == "tainted":
                    return "ffi:" + key
                return None
            if key:
                argprov = None
                for a in args:
                    p = self._prop(a, env, which)
                    if p and not argprov:
                        argprov = p
                if argprov:
                    return argprov
                return "param" if which == "tainted" \
                    and self.state.rets.get(key) else None
            if name == "range":
                return None
            argprov = None
            for a in args:
                p = self._prop(a, env, which)
                if p and not argprov:
                    argprov = p
            return argprov
        if k == "qmark":
            return self._prop(e.get("e"), env, which)
        if k == "match":
            scrut = self._prop(e.get("scrut"), env, which)
            if scrut:
                return scrut
            for arm in (e.get("arms") or []):
                binds = ((arm.get("pattern") or {}).get("bindings") or [])
                aenv = {w: dict(env[w]) for w in ("tainted", "escaped")}
                for b in binds:
                    if b != "_":
                        aenv[which][b] = None
                p = self._prop(arm.get("body"), aenv, which)
                if p:
                    return p
            return None
        return None

    def _either(self, e, env):
        """Provenance under EITHER environment — the general 'this
        value derives from untrusted data' predicate used for flows
        into user callees."""
        return (self._prop(e, env, "tainted")
                or self._prop(e, env, "escaped"))

    # -- the source-site flagger -------------------------------------------

    def flag_expr(self, e, env):
        """Visit every subexpression: record source call sites, unwrap
        sites, sanitizer cuts and sink-reaching flows."""
        if not isinstance(e, dict):
            return
        k = e.get("k")
        if k == "call":
            name = e.get("name")
            args = e.get("args") or []
            line = e.get("line", 0)
            key = _call_target(e)
            fb = self.ffi_by_name.get(key) if key else None
            if name in SOURCE_BUILTINS:
                self.finding("source", line, "%s()" % name,
                             "builtin:" + name)
            elif name == "taint_unwrap" and args:
                prov = (self._prop(args[0], env, "tainted")
                        or self._prop(args[0], env, "escaped"))
                if prov:
                    self.finding("unwrap", line, _text(e), prov)
            elif name in SANITIZERS and args:
                prov = self._either(args[0], env)
                if prov:
                    self.finding("sanitised", line, _text(e), prov)
            elif name in SINK_BUILTINS:
                for pos in SINK_BUILTINS[name]:
                    if pos < len(args):
                        prov = self._prop(args[pos], env, "escaped")
                        if prov:
                            self.finding("sink-reach", line,
                                         _text(e), prov)
            elif fb is not None and fb.taint_source:
                # The #[taint_source] extern call itself is an entry
                # point — the checker types its result tainted[ret],
                # and the report names the site.
                self.finding("source", line, _text(e), "ffi:" + key)
                for i, a in enumerate(args):
                    if i < len(fb.params) and fb.params[i] in fb.taint_sinks:
                        p = self._prop(a, env, "escaped")
                        if p:
                            self.finding("sink-reach", line,
                                         _text(e), p)
            elif fb is not None:
                # An unmarked extern: escaped data reaching a
                # #[taint_sink(...)] parameter is an accepted-risk
                # flow worth naming (the checker only rejects TAINTED
                # arguments; the hatch produced clean-typed data).
                for i, a in enumerate(args):
                    if i < len(fb.params) and fb.params[i] in fb.taint_sinks:
                        p = self._prop(a, env, "escaped")
                        if p:
                            self.finding("sink-reach", line,
                                         _text(e), p)
            elif key and key in self.state.params and fb is None:
                cf = self.state.fns.get(key) or {}
                params = [p[0] for p in cf.get("params", [])]
                for i, a in enumerate(args):
                    if i < len(params):
                        p = self._either(a, env)
                        if p:
                            self._flow(key, params[i], line)
            for a in args:
                self.flag_expr(a, env)
            return
        if k == "method":
            op = _builtin_op(e)
            args = e.get("args") or []
            line = e.get("line", 0)
            if op is None:
                # A user method: flows like a call (receiver = param 0).
                key = _call_target(e)
                if key and key in self.state.params:
                    cf = self.state.fns.get(key) or {}
                    params = [p[0] for p in cf.get("params", [])]
                    if params:
                        p = self._either(e.get("target"), env)
                        if p:
                            self._flow(key, params[0], line)
                    for i, a in enumerate(args):
                        if i + 1 < len(params):
                            pv = self._either(a, env)
                            if pv:
                                self._flow(key, params[i + 1], line)
            else:
                # Builtin method sinks on the ESCAPED env: byte_at /
                # slice are reads, but map.set / list.push with an
                # escaped value taint the container — handled by the
                # side-effects walk. Nothing to flag here.
                pass
            self.flag_expr(e.get("target"), env)
            for a in args:
                self.flag_expr(a, env)
            return
        if k == "bin":
            self.flag_expr(e.get("l"), env)
            self.flag_expr(e.get("r"), env)
            return
        if k == "un":
            self.flag_expr(e.get("e"), env)
            return
        if k == "index":
            self.flag_expr(e.get("target"), env)
            self.flag_expr(e.get("idx"), env)
            return
        if k in ("fieldcall", "enumlit"):
            self.flag_expr(e.get("target"), env)
            for a in (e.get("args") or []):
                self.flag_expr(a, env)
            return
        if k == "listlit":
            for i in (e.get("items") or []):
                self.flag_expr(i, env)
            return
        if k == "structlit":
            for _n, fe in (e.get("fields") or []):
                self.flag_expr(fe, env)
            return
        if k == "qmark":
            self.flag_expr(e.get("e"), env)
            return
        if k == "match":
            self.flag_expr(e.get("scrut"), env)
            for arm in (e.get("arms") or []):
                self.flag_expr(arm.get("body"), env)
            return
        # field / ident / literals: nothing to flag

    # -- the statement walk -------------------------------------------------

    def walk_stmts(self, stmts, env):
        for s in stmts or []:
            if not isinstance(s, dict):
                continue
            k = s.get("k")
            if k == "let":
                self.flag_expr(s.get("value"), env)
                tp = self._prop(s.get("value"), env, "tainted")
                ep = self._prop(s.get("value"), env, "escaped")
                env["tainted"][s["name"]] = tp
                env["escaped"][s["name"]] = ep
            elif k == "assign":
                tgt = s.get("target") or {}
                self.flag_expr(tgt, env)
                self.flag_expr(s.get("value"), env)
                if tgt.get("k") == "ident":
                    env["tainted"][tgt["name"]] = \
                        self._prop(s.get("value"), env, "tainted")
                    env["escaped"][tgt["name"]] = \
                        self._prop(s.get("value"), env, "escaped")
                elif tgt.get("k") in ("field", "index"):
                    base = _root_name(tgt)
                    if base is not None:
                        p = self._either(s.get("value"), env)
                        if p:
                            # Pushing untrusted data into a container
                            # taints the container.
                            env["tainted"][base] = \
                                env["tainted"].get(base) or p
            elif k == "expr":
                e = s.get("e")
                if isinstance(e, dict) and e.get("k") == "match":
                    self.walk_match(e, env)
                else:
                    self.flag_expr(e, env)
                    self.side_effects(e, env)
            elif k == "return":
                v = s.get("value")
                if v is not None:
                    self.flag_expr(v, env)
                    if self._prop(v, env, "tainted"):
                        self.ret_tainted = True
            elif k == "if":
                cond = s.get("cond")
                self.flag_expr(cond, env)
                then_env = self._branch_env(env)
                self.walk_stmts(s.get("then"), then_env)
                else_env = self._branch_env(env)
                self.walk_stmts(s.get("els"), else_env)
                self._join_into(env, then_env, else_env)
            elif k == "while":
                cond = s.get("cond")
                self.flag_expr(cond, env)
                # Widening: two passes over the body (a value assigned
                # inside the loop is untrusted on every later pass).
                self.walk_stmts(s.get("body"), env)
                self.walk_stmts(s.get("body"), env)
            elif k == "for":
                it = s.get("iter") or {}
                self.flag_expr(it, env)
                if it.get("k") == "call" and it.get("name") == "range":
                    env["tainted"][s["var"]] = None
                    env["escaped"][s["var"]] = None
                else:
                    env["tainted"][s["var"]] = \
                        self._prop(it, env, "tainted")
                    env["escaped"][s["var"]] = \
                        self._prop(it, env, "escaped")
                self.walk_stmts(s.get("body"), env)
                self.walk_stmts(s.get("body"), env)
            elif k == "match":
                m = s.get("e") or s
                self.walk_match(m, env)
            elif k in ("break", "continue", "asm"):
                pass
            else:
                for key in ("value", "cond", "iter", "e"):
                    self.flag_expr(s.get(key), env)

    def walk_match(self, m, env):
        if not isinstance(m, dict) or m.get("k") != "match":
            return
        scrut = m.get("scrut")
        self.flag_expr(scrut, env)
        for arm in (m.get("arms") or []):
            binds = ((arm.get("pattern") or {}).get("bindings") or [])
            aenv = self._branch_env(env)
            for b in binds:
                if b != "_":
                    aenv["tainted"][b] = self._prop(scrut, env, "tainted")
                    aenv["escaped"][b] = self._prop(scrut, env, "escaped")
            self.flag_expr(arm.get("body"), aenv)
            self.side_effects(arm.get("body"), aenv)

    def side_effects(self, e, env):
        """A statement-position method call can TAINT its receiver:
        `xs.push(untrusted)` makes every later read of xs untrusted."""
        if not isinstance(e, dict):
            return
        if e.get("k") == "method":
            op = _builtin_op(e)
            target = e.get("target")
            args = e.get("args") or []
            if op in ("list.push", "list.set", "map.set", "map.insert"):
                p = None
                for a in args:
                    p = p or self._either(a, env)
                if p:
                    base = _root_name(target)
                    if base is not None:
                        env["tainted"][base] = \
                            env["tainted"].get(base) or p
            self.flag_expr(target, env)
            for a in args:
                self.flag_expr(a, env)
        elif e.get("k") == "call":
            for a in (e.get("args") or []):
                self.flag_expr(a, env)
        elif e.get("k") == "qmark":
            self.side_effects(e.get("e"), env)

    # -- environment plumbing ------------------------------------------------

    @staticmethod
    def _branch_env(env):
        return {"tainted": dict(env["tainted"]),
                "escaped": dict(env["escaped"])}

    @staticmethod
    def _join_into(env, a, b):
        """Join: a variable untrusted on EITHER path is untrusted
        (union); the first provenance seen wins (labels are hints,
        not lattices)."""
        for table in ("tainted", "escaped"):
            for name, v in a[table].items():
                if v and not env[table].get(name):
                    env[table][name] = v
            for name, v in b[table].items():
                if v and not env[table].get(name):
                    env[table][name] = v


def _ffi_boundaries(program):
    """The census rows: one FfiBoundary per extern declaration, in
    declaration order."""
    out = []
    for ext in program.get("externs", []):
        for fn in ext.get("decls", []):
            params = [p[0] for p in fn.get("params", [])]
            ptypes = [p[1] for p in fn.get("params", [])]
            out.append(FfiBoundary(
                fn["name"], ext.get("abi", "C"), params, ptypes,
                fn.get("ret", "void"),
                bool(fn.get("taint_source")),
                list(fn.get("taint_sinks") or []),
                fn.get("line", 0)))
    return out


def _ffi_round(program, state, ffi_by_name):
    """One fixpoint round: walk every fn with the current state. The
    walkers report the findings with the CURRENT state and mutate the
    state with the flows they observe (monotone growth)."""
    walkers = {}
    for key, fn in program["fns"].items():
        w = _Walker(key, fn, state, ffi_by_name)
        env = {"tainted": {}, "escaped": {}}
        params = [p[0] for p in fn.get("params", [])]
        for pn in params:
            env["tainted"][pn] = "param:%s" % pn \
                if pn in state.params.get(key, ()) else None
            env["escaped"][pn] = None
        w.walk_stmts(fn.get("body"), env)
        walkers[key] = w
        if w.ret_tainted and not state.rets.get(key, False):
            state.rets[key] = True
            state.changed = True
    return walkers


def analyze_ffi_taint(program):
    """The Stage 109 pass. Returns (boundaries, {fn_key: FnFfiTaint}).
    The fixpoint is monotone (parameter sets only grow, bounded by
    the param names), so it terminates; the findings grow with the
    state, so the LAST round carries the fullest report."""
    boundaries = _ffi_boundaries(program)
    ffi_by_name = {b.name: b for b in boundaries}
    state = _State(program)
    # Roots: the fns whose #[taint_source] call sites appear — the
    # first round's walks record the edges; the fixpoint grows from
    # them. (#[secrets]-style annotation is not needed: the SOURCE
    # call sites are the roots, and they are found by the walk.)
    reports = {}
    flows = {}
    for _round in range(64):
        state.changed = False
        walkers = _ffi_round(program, state, ffi_by_name)
        for key, w in walkers.items():
            rep = reports.setdefault(key, FnFfiTaint(key))
            rep.findings = list(w.findings.values())
            flows.setdefault(key, []).extend(w.flows)
        if not state.changed:
            break
    # Assemble the incoming flows per callee (dedup per caller+param).
    incoming = {}
    for caller, edges in flows.items():
        for callee, param, line in edges:
            incoming.setdefault(callee, []).append((caller, param, line))
    out = {}
    for key in program["fns"]:
        rep = reports.get(key) or FnFfiTaint(key)
        rep.tainted_params = sorted(state.params.get(key, ()))
        ins = []
        seen = set()
        for caller, param, line in sorted(incoming.get(key, []),
                                          key=lambda t: (t[2], t[0], t[1])):
            k = (caller, param)
            if k not in seen:
                seen.add(k)
                ins.append((caller, param, line))
        rep.incoming = ins
        if rep.tainted_params or rep.incoming or rep.findings:
            out[key] = rep
    return boundaries, out
