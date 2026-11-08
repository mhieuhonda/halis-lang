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
chain walk as the fallback: a node without "fi" behaves exactly as
before, and an annotated node whose frame somehow misses the name
falls through to the same walk — the annotation can only ever change
speed, never the lookup result. HL_BOOT_RESOLVE_CHECK=1 cross-checks
every annotated hit against the walk result (suite-corpus-wide debug
arm, zero cost when unset).

Deliberately NOT annotated (their binding env is not the occurrence's
own): struct-literal default expressions (evaluated from the caller's
env through the shared struct table) and refinement predicates
(requires/ensures/rpred — evaluated against call/let frames from
runtime data). Those keep the fallback walk.
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
        fi = _resolve(e["name"], frames)
        if fi is not None:
            e["fi"] = fi
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
            # A matched arm pushes one frame holding the bindings.
            frames.append({})
            idx = len(frames) - 1
            for bname in pat.get("bindings", []):
                if bname != "_":
                    frames[-1][bname] = idx
            _walk_expr(arm["body"], frames)
            frames.pop()
        return


def _walk_target(t, frames):
    # exec_assign's lvalue: an ident target is a name write; field and
    # index targets walk their base/idx expressions like reads.
    if t["k"] == "ident":
        fi = _resolve(t["name"], frames)
        if fi is not None:
            t["fi"] = fi
        return
    if t["k"] == "field":
        _walk_expr(t["target"], frames)
        return
    if t["k"] == "index":
        _walk_expr(t["target"], frames)
        _walk_expr(t["idx"], frames)


def _walk_stmt(s, frames):
    k = s["k"]
    if k == "expr":
        _walk_expr(s["e"], frames)
    elif k == "let":
        _walk_expr(s["value"], frames)
        # exec_stmt binds into env[-1]: the top frame's index.
        frames[-1][s["name"]] = len(frames) - 1
    elif k == "assign":
        _walk_expr(s["value"], frames)
        _walk_target(s["target"], frames)
    elif k == "if":
        _walk_expr(s["cond"], frames)
        frames.append({})
        _walk_stmts(s["then"], frames)
        frames.pop()
        if s["els"] is not None:
            frames.append({})
            _walk_stmts(s["els"], frames)
            frames.pop()
    elif k == "while":
        _walk_expr(s["cond"], frames)
        frames.append({})
        _walk_stmts(s["body"], frames)
        frames.pop()
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


def annotate_program(program):
    for fn in program["fns"].values():
        annotate_fn(fn)
