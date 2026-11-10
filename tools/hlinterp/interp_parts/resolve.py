"""Static frame-index resolution for the Stage-0 interpreter.

The eval loop resolves every identifier by walking the live env chain
(`i = len(env) - 1; while i >= 0: env[i].get(name)`). Scoping in HLS is
fully static — no closures, no first-class functions — so the frame a
name binds in is a per-occurrence constant: for a given program point
the live env is always `[env0]` plus the block frames that construct
pushes, in the same order, on every execution.

This pass walks each fn body exactly the way exec_stmt/eval_expr push
and pop frames (if/else, while, for, non-wildcard match arms, let
bindings, call parameters) and writes the absolute frame index into
every resolvable ident node as `e["fi"]`. The eval sites keep the old
chain walk as the fallback: a node whose `fi` is None behaves exactly
as before, and an annotated node whose frame somehow misses the name
falls through to the same walk — the annotation can only ever change
speed, never the lookup result.

Two further annotations fall out of the same walk, both pure speed:

* Every ident node leaves the pass carrying an `fi` key (the resolved
  index, or None) — including the deliberately-unresolved families
  walked with empty frames below — so the hot eval paths can read the
  slot with a direct subscript instead of a `.get` method call.
* Every if/else/while body and every non-wildcard match arm carries an
  `nf` flag ("needs frame"): True when the block binds a name directly
  (a `let` at this depth — nested constructs push their own frames, so
  only a direct `let` writes this block's frame). exec skips pushing
  the empty dict for nf-False blocks; the pushed-but-empty frame was
  invisible to every lookup (both the pinned reads and the fallback
  walk), so skipping it cannot change any result — and this pass skips
  the same frames when numbering, keeping the absolute indices in
  lockstep with the runtime.

Deliberately NOT resolved (their binding env is not the occurrence's
own — they are walked with NO frames so their idents get `fi = None`
and keep the fallback walk): struct-literal default expressions and
refinement predicates (requires/ensures/rpred — evaluated against call
frames or synthetic `self` frames from runtime data). Those keep the
fallback walk.
"""

def _resolve(name, frames):
    # Top-down, mirroring the eval loop's search order.
    for fi in range(len(frames) - 1, -1, -1):
        if name in frames[fi]:
            return fi
    return None


def _walk_expr(e, frames):
    k = e["k"]
    if k == "ident":
        # Always materialise the slot: resolved index or None. The hot
        # eval paths subscript `e["fi"]` directly.
        e["fi"] = _resolve(e["name"], frames)
        return
    if k == "bin":
        _walk_expr(e["l"], frames)
        # || and && evaluate the right side in the same env (short-
        # circuit or not, no frame changes between the operands).
        _walk_expr(e["r"], frames)
        return
    if k == "call" or k == "enumlit":
        for a in e.get("args", []):
            _walk_expr(a, frames)
        return
    if k == "method":
        _walk_expr(e["target"], frames)
        for a in e.get("args", []):
            _walk_expr(a, frames)
        return
    if k == "field":
        _walk_expr(e["target"], frames)
        return
    if k == "index":
        _walk_expr(e["target"], frames)
        _walk_expr(e["idx"], frames)
        return
    if k == "un" or k == "qmark":
        _walk_expr(e["e"], frames)
        return
    if k == "listlit":
        for it in e.get("items", []):
            _walk_expr(it, frames)
        return
    if k == "structlit":
        # Field values come from the occurrence's env; the struct's
        # own default expressions do NOT (see module docstring).
        for _fname, fe in e["fields"]:
            _walk_expr(fe, frames)
        return
    if k == "match":
        _walk_expr(e["scrut"], frames)
        for arm in e["arms"]:
            pat = arm["pattern"]
            if pat["k"] == "wildcard":
                # eval_match runs a wildcard body in the CURRENT env
                # (no frame push).
                _walk_expr(arm["body"], frames)
                continue
            # A matched arm with at least one real (non-"_") binding
            # pushes one frame holding the bindings; an all-wildcard
            # arm's pushed frame would be empty and unobservable, so
            # eval_match skips it and this pass numbers accordingly.
            arm["nf"] = any(b != "_" for b in pat.get("bindings", []))
            if arm["nf"]:
                frames.append({})
                idx = len(frames) - 1
                for bname in pat.get("bindings", []):
                    if bname != "_":
                        frames[-1][bname] = idx
                _walk_expr(arm["body"], frames)
                frames.pop()
            else:
                _walk_expr(arm["body"], frames)
        return


def _walk_target(t, frames):
    # exec_assign's lvalue: an ident target is a name write; field and
    # index targets walk their base/idx expressions like reads.
    if t["k"] == "ident":
        t["fi"] = _resolve(t["name"], frames)
        return
    if t["k"] == "field":
        _walk_expr(t["target"], frames)
        return
    if t["k"] == "index":
        _walk_expr(t["target"], frames)
        _walk_expr(t["idx"], frames)


def _binds_directly(stmts):
    # Does this block need its own frame? Only a `let` directly in the
    # list writes env[-1] at this depth — every nested construct that
    # binds (if/while bodies, for loops, match arms) pushes a frame of
    # its own before any of its bindings happen.
    for s in stmts:
        if s["k"] == "let":
            return True
    return False


def _walk_stmt(s, frames):
    k = s["k"]
    # Line-slot completeness: the parser stamps every statement, this
    # is the belt-and-braces that lets exec_stmt subscript directly.
    if "line" not in s:
        s["line"] = 0
    if k == "expr":
        _walk_expr(s["e"], frames)
    elif k == "let":
        _walk_expr(s["value"], frames)
        # Runtime refinement guards evaluate their predicate in a
        # synthetic `self`-only frame — no static frames apply.
        lref = s.get("rpred")
        if lref is not None:
            _walk_expr(lref["ast"], [])
        # exec_stmt binds into env[-1]: the top frame's index.
        frames[-1][s["name"]] = len(frames) - 1
    elif k == "assign":
        _walk_expr(s["value"], frames)
        sref = s.get("rpred")
        if sref is not None:
            _walk_expr(sref["ast"], [])
        _walk_target(s["target"], frames)
    elif k == "if":
        _walk_expr(s["cond"], frames)
        # Number the frames EXACTLY as exec_stmt pushes them: a
        # binding-free body gets no runtime frame, so it gets no
        # numbering frame either — the absolute indices stay in
        # lockstep with the live env.
        if _binds_directly(s["then"]):
            s["nf"] = True
            frames.append({})
            _walk_stmts(s["then"], frames)
            frames.pop()
        else:
            s["nf"] = False
            _walk_stmts(s["then"], frames)
        if s["els"] is not None:
            if _binds_directly(s["els"]):
                s["nf_else"] = True
                frames.append({})
                _walk_stmts(s["els"], frames)
                frames.pop()
            else:
                s["nf_else"] = False
                _walk_stmts(s["els"], frames)
    elif k == "while":
        _walk_expr(s["cond"], frames)
        if _binds_directly(s["body"]):
            s["nf"] = True
            frames.append({})
            _walk_stmts(s["body"], frames)
            frames.pop()
        else:
            s["nf"] = False
            _walk_stmts(s["body"], frames)
    elif k == "for":
        _walk_expr(s["iter"], frames)
        frames.append({s["var"]: len(frames)})
        _walk_stmts(s["body"], frames)
        frames.pop()
    elif k == "return":
        if s["value"] is not None:
            _walk_expr(s["value"], frames)


def _walk_stmts(stmts, frames):
    for s in stmts:
        _walk_stmt(s, frames)


def annotate_fn(fn):
    """Mirror call_fn's env: one frame holding the receiver (if any)
    and the parameters, then the body."""
    body = fn.get("body")
    if fn.get("extern", False) or not body:
        # extern "C" fns have no body to walk; call_extern never builds
        # an env for them.
        return
    frames = [{}]
    params = fn["params"]
    if fn.get("struct") is not None:
        # Struct method: params[0] is the receiver, bound into env0
        # exactly like the rest (call_fn keeps one env0 for both).
        frames[0][params[0][0]] = 0
        named = params[1:]
    else:
        named = params
    for p in named:
        frames[0][p[0]] = 0
    _walk_stmts(body, frames)
    # Contract / refinement predicates evaluate against the runtime
    # call env (or a synthetic `self` frame), never against static
    # frames: walk them with no frames so their idents carry fi=None
    # and keep the chain-walk fallback at eval time.
    for key in ("requires", "ensures"):
        if fn.get(key) is not None:
            _walk_expr(fn[key], [])
    rp = fn.get("ret_pred")
    if rp is not None:
        _walk_expr(rp["ast"], [])
    pp = fn.get("param_preds")
    if pp:
        for ref in pp.values():
            _walk_expr(ref["ast"], [])


def annotate_program(program):
    for fn in program["fns"].values():
        annotate_fn(fn)
    # Struct-table expression families: field defaults evaluate in the
    # literal's caller env, field predicates in a synthetic self frame
    # — both keep the chain-walk fallback (fi=None on every ident).
    for st in program.get("structs", {}).values():
        for _fname, _ftype, fdefault in st["fields"]:
            if fdefault is not None:
                _walk_expr(fdefault, [])
        for ref in (st.get("field_preds") or {}).values():
            _walk_expr(ref["ast"], [])
