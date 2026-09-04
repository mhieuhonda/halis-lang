"""Stage 17 (v0.28.0-alpha): the Halis proof engine.

Two capabilities, both driven by `requires` contracts:

1. **Interval analysis** (always on at check time): seed integer bounds
   from the requires conjuncts, propagate them through the function body
   (straight-line arithmetic, if/else joins, const-bound for loops), and
   decide for each checked operation whether it is PROVABLY safe:
     - `a + b` / `a - b` / `a * b` cannot overflow int64
     - `a / b` / `a % b` cannot divide by zero (or hit INT64_MIN/-1)
     - `xs[i]` / `s.byte_at(i)` / `s.slice(a, b)` cannot go out of bounds
   Only PROVEN operations are annotated; anything unknown keeps its
   runtime check (soundness: never elide a check you cannot prove dead).
   The annotations power `-O fast` check elision in hlc.hls codegen.

2. **SMT-LIB2 bridge** (hlprove --smt): generate a .smt2 encoding of
   each contract (satisfiability of `requires`, call-site violations,
   triviality of `ensures | requires`), runnable by external z3. The
   bridge is GENERATED FROM the HLS contracts (the roadmap's "z3 via a
   bridge generated from HLS"); z3 itself is optional.

Also here: the loop-invariant suggestion heuristics (for-loop bounds
and while-condition textual invariants).
"""
from .lexer import HLError

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

# Sentinel for "no bound known" and symbolic string/list lengths.
INF = None  # represented as (lo, hi) tuples with None = unbounded


class Interval:
    __slots__ = ("lo", "hi")

    def __init__(self, lo, hi):
        self.lo = lo  # int or None (-inf)
        self.hi = hi  # int or None (+inf)

    def is_const(self):
        return self.lo is not None and self.hi is not None and self.lo == self.hi

    def const(self):
        return self.lo

    def __repr__(self):
        return "[%s, %s]" % (self.lo if self.lo is not None else "-inf",
                             self.hi if self.hi is not None else "+inf")


TOP = Interval(None, None)


def iv_add(a, b):
    lo = None if a.lo is None or b.lo is None else a.lo + b.lo
    hi = None if a.hi is None or b.hi is None else a.hi + b.hi
    return Interval(lo, hi)


def iv_sub(a, b):
    lo = None if a.lo is None or b.hi is None else a.lo - b.hi
    hi = None if a.hi is None or b.lo is None else a.hi - b.lo
    return Interval(lo, hi)


def iv_mul(a, b):
    # Conservative product of the four corner combinations; unknown on
    # any None bound.
    corners = []
    for x in (a.lo, a.hi):
        for y in (b.lo, b.hi):
            if x is None or y is None:
                return TOP
            corners.append(x * y)
    return Interval(min(corners), max(corners))


def iv_neg(a):
    lo = None if a.hi is None else -a.hi
    hi = None if a.lo is None else -a.lo
    return Interval(lo, hi)


def iv_widen(a, b):
    """Join (union) of two intervals — used at if/else joins and loop
    back-edges. None bound wins (wider)."""
    lo = None if a.lo is None or b.lo is None else min(a.lo, b.lo)
    hi = None if a.hi is None or b.hi is None else max(a.hi, b.hi)
    return Interval(lo, hi)


def fits(iv):
    """True if every value in iv is within int64 (used for overflow)."""
    if iv.lo is not None and iv.lo < INT64_MIN:
        return False
    if iv.hi is not None and iv.hi > INT64_MAX:
        return False
    return True


def add_fits(a, b):
    return fits(iv_add(a, b))


def sub_fits(a, b):
    return fits(iv_sub(a, b))


def mul_fits(a, b):
    return fits(iv_mul(a, b))


def excludes_zero(iv):
    """True if 0 is provably NOT in iv (used for division)."""
    if iv.hi is not None and iv.hi < 0:
        return True
    if iv.lo is not None and iv.lo > 0:
        return True
    return False


# ----------------------------------------------------------------------------
# Seeding: walk a requires expression, binding variable bounds.
# ----------------------------------------------------------------------------

def seed_from_requires(expr, params, env_len):
    """Extract (var -> Interval) facts from a requires expression.

    Recognised conjunct shapes (combined with &&):
      x >= k   x <= k   x > k   x < k   k <= x   k >= x   k > x   k < x
      x == k   x != 0
      x < s.len() (symbolic length bound: the var's hi becomes
      ("len", owner) — only the non-negative side is usable)
      s.len() >= k (MINIMUM LENGTH fact: minlen[s] = k — powers the
      numeric bounds elision: an index interval fully below k is
      provably in bounds for s)
    Only INTEGER params are tracked; everything else is ignored.
    """
    facts = {}
    _seed_walk(expr, params, facts)
    return facts


def _seed_walk(e, params, facts):
    if not isinstance(e, dict):
        return
    if e.get("k") == "bin" and e.get("op") == "&&":
        _seed_walk(e.get("l"), params, facts)
        _seed_walk(e.get("r"), params, facts)
        return
    if e.get("k") != "bin":
        return
    op = e.get("op")
    l = e.get("l")
    r = e.get("r")
    if not isinstance(l, dict) or not isinstance(r, dict):
        return
    # Determine (var, bound, direction) shapes.
    li = _int_var(l, params)
    ri = _int_var(r, params)
    if li is not None and r.get("k") == "int":
        _bound_var(facts, li, op, r["v"], True)
    elif ri is not None and l.get("k") == "int":
        _bound_var(facts, ri, op, l["v"], False)
    elif li is not None and ri is not None:
        # var vs var: e.g. x < y — only usable if the other has a bound
        # (deferred: keep simple, ignore)
        pass
    elif li is not None and _is_len_call(r):
        # x < s.len() / x <= s.len() — non-negative upper symbolic bound.
        owner = _len_owner(r)
        if op == "<" or op == "<=":
            _refine(facts, li, None, ("len", owner, 0 if op == "<" else None))
    elif _is_len_call(l) and ri is not None:
        owner = _len_owner(l)
        if op == ">" or op == ">=":
            _refine(facts, ri, None, ("len", owner, 0 if op == ">" else None))
    # Minimum-length facts: s.len() >= k (either order) -> minlen[s] = k.
    if op in (">=", ">", "<=", "<", "=="):
        for (len_side, other_side) in ((l, r), (r, l)):
            if _is_len_call(len_side) and isinstance(other_side, dict) \
                    and other_side.get("k") == "int":
                k = other_side["v"]
                owner = _len_owner(len_side)
                eff = op
                if len_side is r:  # flip to len-on-left
                    flip = {"<": ">", "<=": ">=", ">": "<", ">=": "<=",
                            "==": "=="}
                    eff = flip[op]
                if eff == ">=":
                    _set_minlen(facts, owner, k)
                elif eff == ">":
                    _set_minlen(facts, owner, k + 1)
                elif eff == "==":
                    _set_minlen(facts, owner, k)


def _set_minlen(facts, owner, k):
    cur = facts.get("__minlen__", {})
    old = cur.get(owner)
    if old is None or k > old:
        cur[owner] = k
    facts["__minlen__"] = cur


def _int_var(e, params):
    """If e is an ident naming an int param, return its name."""
    if isinstance(e, dict) and e.get("k") == "ident":
        n = e.get("name")
        for pn, pt, _ in params:
            if pn == n and pt == "int":
                return n
    return None


def _bound_var(facts, var, op, k, var_on_left):
    """Apply `var OP k` (var_on_left) or `k OP var` (not) to facts."""
    # Normalise to var-on-left semantics.
    if not var_on_left:
        flip = {"<": ">", "<=": ">=", ">": "<", ">=": "<=",
                "==": "==", "!=": "!="}
        op = flip[op]
    if op == ">=":
        _refine(facts, var, k, None)
    elif op == ">":
        _refine(facts, var, k + 1, None)
    elif op == "<=":
        _refine(facts, var, None, k)
    elif op == "<":
        _refine(facts, var, None, k - 1)
    elif op == "==":
        _refine(facts, var, k, k)
    elif op == "!=":
        if k == 0:
            _refine(facts, var, "nz", None)  # special: excludes zero


def _refine(facts, var, lo, hi):
    """Refine facts[var] with a new bound. lo/hi may be int, None (no
    info), ("len", owner, delta) symbolic, or the string "nz" (the
    interval excludes zero)."""
    old = facts.get(var, TOP)
    if lo == "nz":
        # Excludes zero: if the interval is entirely one side of 0, we
        # can tighten; otherwise only record non-zero-ness.
        if old.hi is not None and old.hi < 0:
            pass  # already negative-only
        elif old.lo is not None and old.lo > 0:
            pass  # already positive-only
        else:
            # Split into two intervals; represent conservatively as
            # (min_int, -1) U (1, max_int) — we cannot express unions,
            # so record the WIDER fact "excludes zero" via hi<0/lo>0
            # only when one side is impossible. Keep TOP but tag nz.
            facts[var] = old
            facts.setdefault("__nz__", set()).add(var)
        return
    if isinstance(lo, tuple):
        # Symbolic len bound: keep hi as the tuple; numeric lo stays.
        new_hi = hi if hi is not None else old.hi
        if isinstance(new_hi, tuple) or isinstance(old.hi, tuple):
            # Merge symbolic: prefer the symbolic (more precise for
            # in-bounds); numeric beats nothing.
            if isinstance(old.hi, tuple) and isinstance(new_hi, tuple):
                new_hi = new_hi  # keep latest
            elif isinstance(old.hi, tuple):
                new_hi = old.hi
        facts[var] = Interval(old.lo if lo is None else lo, new_hi)
        return
    new_lo = old.lo if lo is None else (lo if old.lo is None else max(old.lo, lo))
    new_hi_ = hi
    if new_hi_ is None:
        new_hi_ = old.hi
    elif old.hi is not None:
        if isinstance(old.hi, tuple) or isinstance(new_hi_, tuple):
            pass  # symbolic handled above
        else:
            new_hi_ = min(old.hi, new_hi_)
    facts[var] = Interval(new_lo, new_hi_)


def _is_len_call(e):
    """True for `len(x)` or `x.len()` shapes."""
    if not isinstance(e, dict):
        return False
    if e.get("k") == "call" and e.get("name") == "len":
        return True
    if e.get("k") == "method" and e.get("name") == "len":
        return True
    return False


def _len_owner(e):
    """Best-effort owner name for a len() call (for symbolic bounds)."""
    if not isinstance(e, dict):
        return "?"
    if e.get("k") == "call" and e.get("name") == "len":
        a = e.get("args") or []
        if a and isinstance(a[0], dict):
            return a[0].get("name", "?")
    if e.get("k") == "method" and e.get("name") == "len":
        t = e.get("target")
        if isinstance(t, dict):
            return t.get("name", "?")
    return "?"


# ----------------------------------------------------------------------------
# The proof pass: annotate the body with safety facts.
# ----------------------------------------------------------------------------

class ProofReport:
    def __init__(self):
        self.elisions = []   # (kind, line, description)
        self.facts = {}      # fn key -> seeded facts summary

    def add(self, kind, line, desc):
        self.elisions.append((kind, line, desc))


def expr_interval(e, env, params, facts):
    """Best-effort interval of an int-typed expression node."""
    if not isinstance(e, dict):
        return TOP
    k = e.get("k")
    if k == "int":
        return Interval(e["v"], e["v"])
    if k == "ident":
        n = e.get("name")
        if n in facts and not isinstance(facts[n], str):
            return facts[n]
        # loop variable / params tracked in `facts` only; env not tracked
        # here (the checker-level pass threads facts through statements).
        return TOP
    if k == "un" and e.get("op") == "-":
        return iv_neg(expr_interval(e.get("e"), env, params, facts))
    if k == "bin":
        op = e.get("op")
        li = expr_interval(e.get("l"), env, params, facts)
        ri = expr_interval(e.get("r"), env, params, facts)
        if op == "+":
            return iv_add(li, ri)
        if op == "-":
            return iv_sub(li, ri)
        if op == "*":
            return iv_mul(li, ri)
        return TOP
    return TOP


def check_bin_overflow(e, facts):
    """If e is an int binop whose overflow is PROVABLY impossible,
    annotate e['ovf_safe'] = True and return True."""
    if not isinstance(e, dict) or e.get("k") != "bin":
        return False
    op = e.get("op")
    if op not in ("+", "-", "*"):
        return False
    # Only int-typed operands (the checker already set e['t']).
    if e.get("t") != "int":
        return False
    li = expr_interval(e.get("l"), None, None, facts)
    ri = expr_interval(e.get("r"), None, None, facts)
    ok = False
    if op == "+":
        ok = add_fits(li, ri)
    elif op == "-":
        ok = sub_fits(li, ri)
    elif op == "*":
        ok = mul_fits(li, ri)
    if ok:
        e["ovf_safe"] = True
    return ok


def check_div_safe(e, facts):
    """If e is an int `/` or `%` whose divisor is provably non-zero
    (and not the INT64_MIN/-1 overflow case), annotate e['div_safe']."""
    if not isinstance(e, dict) or e.get("k") != "bin":
        return False
    op = e.get("op")
    if op not in ("/", "%"):
        return False
    if e.get("t") != "int":
        return False
    ri = expr_interval(e.get("r"), None, None, facts)
    nz = excludes_zero(ri)
    # "__nz__" facts (x != 0 seeds) — check the ident specially.
    if not nz:
        r = e.get("r")
        if isinstance(r, dict) and r.get("k") == "ident":
            nz_set = facts.get("__nz__", set())
            if r.get("name") in nz_set:
                nz = True
    if not nz:
        return False
    li = expr_interval(e.get("l"), None, None, facts)
    # INT64_MIN / -1 overflow: only if li can be INT64_MIN and ri can
    # be -1.
    if li.lo is not None and li.lo == INT64_MIN:
        if ri.lo is not None and ri.lo <= -1 and ri.hi is not None and ri.hi >= -1:
            return False
    e["div_safe"] = True
    return True


def _bound_for_index(e, facts):
    return expr_interval(e, None, None, facts)


def check_index_safe(e, facts):
    """If e is an index `xs[i]` with i provably in [0, len-1], annotate
    e['bnd_safe']. Two proof routes:
      - symbolic: the index var carries a ("len", owner) upper bound
        seeded from `i < xs.len()` AND the container is that owner;
      - numeric: the index interval is [lo, hi] with lo >= 0 and hi <
        minlen[owner] (seeded from `requires xs.len() > hi`).
    """
    if not isinstance(e, dict) or e.get("k") != "index":
        return False
    tgt = e.get("target")
    idx = e.get("idx")
    if not isinstance(tgt, dict) or not isinstance(idx, dict):
        return False
    if tgt.get("k") != "ident":
        return False
    ii = _bound_for_index(idx, facts)
    if ii.lo is None or ii.lo < 0:
        return False
    owner = tgt.get("name")
    hi = ii.hi
    if isinstance(hi, tuple) and hi[0] == "len" and hi[1] == owner:
        e["bnd_safe"] = True
        return True
    minlen = facts.get("__minlen__", {})
    if owner in minlen and isinstance(hi, int) and hi < minlen[owner]:
        e["bnd_safe"] = True
        return True
    return False


def check_byte_at_safe(e, facts):
    """s.byte_at(i) with i in [0, s.len()-1] — symbolic or minlen rule."""
    if not isinstance(e, dict) or e.get("k") != "method":
        return False
    if e.get("name") != "byte_at":
        return False
    tgt = e.get("target")
    args = e.get("args") or []
    if not isinstance(tgt, dict) or tgt.get("k") != "ident" or not args:
        return False
    ii = _bound_for_index(args[0], facts)
    if ii.lo is None or ii.lo < 0:
        return False
    owner = tgt.get("name")
    hi = ii.hi
    if isinstance(hi, tuple) and hi[0] == "len" and hi[1] == owner:
        e["bnd_safe"] = True
        return True
    minlen = facts.get("__minlen__", {})
    if owner in minlen and isinstance(hi, int) and hi < minlen[owner]:
        e["bnd_safe"] = True
        return True
    return False


def check_slice_safe(e, facts):
    """s.slice(a, b) with 0 <= a <= b <= len(s) — both bounds symbolic."""
    if not isinstance(e, dict) or e.get("k") != "method":
        return False
    if e.get("name") != "slice":
        return False
    tgt = e.get("target")
    args = e.get("args") or []
    if not isinstance(tgt, dict) or tgt.get("k") != "ident" or len(args) != 2:
        return False
    owner = tgt.get("name")
    ai = _bound_for_index(args[0], facts)
    bi = _bound_for_index(args[1], facts)
    if ai.lo is None or ai.lo < 0:
        return False
    ok_b = False
    if isinstance(bi.hi, tuple) and bi.hi[0] == "len" and bi.hi[1] == owner:
        ok_b = True
    minlen = facts.get("__minlen__", {})
    if owner in minlen and isinstance(bi.hi, int) and bi.hi < minlen[owner]:
        ok_b = True
    if not ok_b:
        return False
    # a <= b: compare best-effort numeric bounds
    if ai.hi is not None and bi.lo is not None and not isinstance(ai.hi, tuple):
        if ai.hi > bi.lo:
            return False
    e["bnd_safe"] = True
    return True


# ----------------------------------------------------------------------------
# Statement-level propagation (int vars only).
# ----------------------------------------------------------------------------

def propagate_stmts(stmts, facts, depth=0):
    """Walk statements, updating facts for int let/assign/if/for/while,
    and annotate every expression node with safety verdicts."""
    if depth > 32 or not isinstance(stmts, list):
        return
    for s in stmts:
        if not isinstance(s, dict):
            continue
        propagate_stmt_exprs(s, facts)
        k = s.get("k")
        if k == "let" or k == "assign":
            t = s.get("t") or s.get("vtype")
            tgt = s.get("target")
            tname = None
            if isinstance(tgt, dict) and tgt.get("k") == "ident":
                tname = tgt.get("name")
            elif k == "let":
                tname = s.get("name")
            if tname and (t == "int"):
                facts[tname] = expr_interval(s.get("value"), None, None, facts)
            elif tname and tname in facts and t != "int":
                del facts[tname]
        elif k == "if":
            sf = dict(facts)
            propagate_stmts(s.get("then") or [], sf, depth + 1)
            ef = dict(facts)
            propagate_stmts(s.get("els") or [], ef, depth + 1)
            # Join: union of branch outcomes (conservative).
            for var in set(sf) | set(ef):
                if var == "__nz__" or var == "__minlen__":
                    continue
                a = sf.get(var)
                b = ef.get(var)
                if a is None or b is None:
                    facts[var] = TOP
                else:
                    facts[var] = iv_widen(a, b)
        elif k == "while":
            # Unknown iterations: widen every int fact touched in the
            # body to TOP-ish (conservative: bounds that could grow).
            wf = dict(facts)
            # SOUNDNESS: modified vars -> TOP for the body annotation
            # (see the for-branch note).
            for mv in _assigned_int_vars(s.get("body") or []):
                wf[mv] = TOP
            propagate_stmts(s.get("body") or [], wf, depth + 1)
            for var, iv in wf.items():
                if var == "__nz__" or var == "__minlen__":
                    continue
                if var in facts:
                    old = facts[var]
                    # If the body may change it, take the union.
                    facts[var] = iv_widen(old, iv)
        elif k == "for":
            # for i: int in range(0, K) with K const -> i in [0, K-1].
            var = s.get("var")
            it = s.get("iter")
            rng = _const_range(it)
            bf = dict(facts)
            # SOUNDNESS: any int var modified inside the loop body can
            # change across iterations — its entry fact may not hold at
            # iteration 2+. Widen such vars to TOP before annotating the
            # body (the loop variable itself is immutable and gets the
            # const-range bound).
            for mv in _assigned_int_vars(s.get("body") or []):
                if mv != var:
                    bf[mv] = TOP
            if var and rng is not None:
                bf[var] = Interval(0, rng - 1)
            elif var:
                bf[var] = Interval(0, None)
            propagate_stmts(s.get("body") or [], bf, depth + 1)


def propagate_stmt_exprs(s, facts):
    """Annotate every expression reachable from one statement with the
    safety verdicts given the current facts."""
    stack = [s.get("value"), s.get("cond"), s.get("iter"), s.get("e")]
    tgt = s.get("target")
    if isinstance(tgt, dict):
        stack.append(tgt.get("idx"))
    while stack:
        e = stack.pop()
        if not isinstance(e, dict):
            continue
        check_bin_overflow(e, facts)
        check_div_safe(e, facts)
        check_index_safe(e, facts)
        check_byte_at_safe(e, facts)
        check_slice_safe(e, facts)
        stack.append(e.get("l"))
        stack.append(e.get("r"))
        stack.append(e.get("e"))
        if e.get("k") == "method":
            stack.append(e.get("target"))
            for a in (e.get("args") or []):
                stack.append(a)
        if e.get("k") == "call":
            for a in (e.get("args") or []):
                stack.append(a)
        if e.get("k") == "index":
            stack.append(e.get("target"))
            stack.append(e.get("idx"))
        if e.get("k") == "field":
            stack.append(e.get("target"))
        if e.get("k") == "match":
            stack.append(e.get("scrut"))
            for arm in (e.get("arms") or []):
                stack.append(arm.get("body"))
        if e.get("k") == "qmark":
            stack.append(e.get("e"))
        if e.get("k") in ("listlit", "structlit", "enumlit"):
            for a in (e.get("items") or e.get("args") or []):
                stack.append(a)
            for _, fe in (e.get("fields") or []):
                stack.append(fe)


def _const_range(it):
    """If it is range(0, K) / range(A, B) with int literals, return
    (B - A) when positive... we need the loop variable range: return
    the iteration count for `range(a, b)` with constant a, b."""
    if not isinstance(it, dict) or it.get("k") != "call":
        return None
    if it.get("name") != "range":
        return None
    args = it.get("args") or []
    if len(args) != 2:
        return None
    if args[0].get("k") != "int" or args[1].get("k") != "int":
        return None
    a = args[0]["v"]
    b = args[1]["v"]
    if b > a:
        return b - a
    return 0




def _assigned_int_vars(stmts, acc=None, depth=0):
    """Every int variable assigned (let/assign target) anywhere in the
    statement tree — the loop-carried-fact soundness set."""
    if acc is None:
        acc = set()
    if depth > 64:
        return acc
    for s in stmts or []:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k == "let":
            if s.get("vtype") == "int":
                acc.add(s.get("name"))
        elif k == "assign":
            tgt = s.get("target")
            if isinstance(tgt, dict) and tgt.get("k") == "ident":
                # the checker annotates tgt['t']; before checking we do
                # not know the type — add it (sound to overapproximate).
                acc.add(tgt.get("name"))
        for bkey in ("body", "then", "els"):
            _assigned_int_vars(s.get(bkey), acc, depth + 1)
    return acc

# ----------------------------------------------------------------------------
# Call-site constant evaluation of requires.
# ----------------------------------------------------------------------------

def const_eval(e, consts):
    """Evaluate a contract expression under a constant environment
    {param_name: python value}. Returns True/False or None (unknown).
    Supported: literals, idents (from consts), +,-,*,/,%, comparisons,
    &&, ||, !, len(str-const), str ==/!= str. Everything else -> None."""
    if not isinstance(e, dict):
        return None
    k = e.get("k")
    if k == "int":
        return e["v"]
    if k == "float":
        return e["v"]
    if k == "bool":
        return e["v"]
    if k == "str":
        return e["v"]
    if k == "ident":
        return consts.get(e.get("name"))
    if k == "un":
        v = const_eval(e.get("e"), consts)
        if v is None:
            return None
        if e.get("op") == "!":
            return not v
        if e.get("op") == "-":
            return -v
        return None
    if k == "bin":
        op = e.get("op")
        if op == "&&":
            l = const_eval(e.get("l"), consts)
            if l is False:
                return False
            r = const_eval(e.get("r"), consts)
            if r is False:
                return False
            if l is True and r is True:
                return True
            return None
        if op == "||":
            l = const_eval(e.get("l"), consts)
            if l is True:
                return True
            r = const_eval(e.get("r"), consts)
            if r is True:
                return True
            if l is False and r is False:
                return False
            return None
        l = const_eval(e.get("l"), consts)
        r = const_eval(e.get("r"), consts)
        if l is None or r is None:
            return None
        try:
            if op == "+":
                return l + r
            if op == "-":
                return l - r
            if op == "*":
                return l * r
            if op == "/":
                return int(l / r) if isinstance(l, int) else l / r
            if op == "%":
                return l % r
            if op == "==":
                return l == r
            if op == "!=":
                return l != r
            if op == "<":
                return l < r
            if op == "<=":
                return l <= r
            if op == ">":
                return l > r
            if op == ">=":
                return l >= r
        except (ZeroDivisionError, TypeError):
            return None
    if k == "call" and e.get("name") == "len":
        a = const_eval((e.get("args") or [None])[0], consts)
        if isinstance(a, (str, bytes, list)):
            return len(a)
        return None
    if k == "method" and e.get("name") == "len":
        t = const_eval(e.get("target"), consts)
        if isinstance(t, (str, bytes, list)):
            return len(t)
        return None
    return None


def args_are_const(fn, arg_exprs):
    """If every argument expression is a literal (int/float/bool/str),
    return {param_name: value}; else None."""
    consts = {}
    for (pn, pt, _), a in zip(fn["params"], arg_exprs):
        if not isinstance(a, dict) or a.get("k") not in ("int", "float", "bool", "str"):
            return None
        consts[pn] = a["v"]
    return consts


# ----------------------------------------------------------------------------
# SMT-LIB2 generation (the z3 bridge).
# ----------------------------------------------------------------------------

_SMT_OPS = {"+": "+", "-": "-", "*": "*", "/": "div", "%": "mod",
            "==": "=", "!=": "distinct", "<": "<", "<=": "<=",
            ">": ">", ">=": ">=", "&&": "and", "||": "or"}


def smt_of_expr(e, vars_int, vars_str):
    """Render a contract expression as an SMT-LIB2 term. `result` maps
    to a declared constant. Raises ValueError on unsupported shapes."""
    if not isinstance(e, dict):
        raise ValueError("unsupported contract node")
    k = e.get("k")
    if k == "int":
        return str(e["v"])
    if k == "bool":
        return "true" if e["v"] else "false"
    if k == "ident":
        n = e.get("name")
        if n == "result":
            return "result"
        if n in vars_int:
            return n
        raise ValueError("unknown identifier in contract: %s" % n)
    if k == "un":
        if e.get("op") == "!":
            return "(not %s)" % smt_of_expr(e.get("e"), vars_int, vars_str)
        if e.get("op") == "-":
            return "(- %s)" % smt_of_expr(e.get("e"), vars_int, vars_str)
        raise ValueError("unsupported unary op")
    if k == "bin":
        op = _SMT_OPS.get(e.get("op"))
        if op is None:
            raise ValueError("unsupported binary op %s" % e.get("op"))
        return "(%s %s %s)" % (op, smt_of_expr(e.get("l"), vars_int, vars_str),
                               smt_of_expr(e.get("r"), vars_int, vars_str))
    if k == "call" and e.get("name") == "len":
        a = (e.get("args") or [None])[0]
        n = a.get("name") if isinstance(a, dict) else None
        if n in vars_str:
            return "%s_len" % n
        raise ValueError("len() of a non-parameter")
    if k == "method" and e.get("name") == "len":
        t = e.get("target")
        n = t.get("name") if isinstance(t, dict) else None
        if n in vars_str:
            return "%s_len" % n
        raise ValueError(".len() of a non-parameter")
    raise ValueError("unsupported contract expression kind: %s" % k)


def smt_prelude(vars_int, vars_str, result_int=True):
    lines = ["(set-logic QF_LIA)"]
    for v in vars_int:
        lines.append("(declare-const %s Int)" % v)
    if vars_str:
        # String lengths need QF_S — emit them as Int proxies with a note.
        lines.append("; strings are abstracted to their lengths (QF_LIA)")
        for v in vars_str:
            lines.append("(declare-const %s_len Int)" % v)
    if result_int:
        lines.append("(declare-const result Int)")
    return lines
