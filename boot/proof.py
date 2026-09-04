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


def iv_mul(a, b):
    # Conservative product of the four corner combinations; unknown on
    # any None bound. Deep-scan-10 soundness fix: a SYMBOLIC bound
    # (tuple, from `x < s.len()` seeds) must never reach arithmetic —
    # tuple + int used to raise a raw TypeError that crashed the whole
    # compiler (any contracted fn that seeded a len bound and then did
    # arithmetic on the variable). Any tuple bound now collapses to
    # the numeric TOP (sound: we lose precision, never soundness).
    corners = []
    for x in (a.lo, a.hi):
        for y in (b.lo, b.hi):
            if x is None or y is None or isinstance(x, tuple) or isinstance(y, tuple):
                return TOP
            corners.append(x * y)
    return Interval(min(corners), max(corners))


def _numeric_bound(x):
    """A bound usable in numeric arithmetic: tuples (symbolic len
    bounds) become None (unknown). Deep-scan-10."""
    return None if isinstance(x, tuple) else x


def iv_add(a, b):
    lo = _numeric_bound(a.lo)
    hi = _numeric_bound(a.hi)
    blo = _numeric_bound(b.lo)
    bhi = _numeric_bound(b.hi)
    lo = None if lo is None or blo is None else lo + blo
    hi = None if hi is None or bhi is None else hi + bhi
    return Interval(lo, hi)


def iv_sub(a, b):
    lo = _numeric_bound(a.lo)
    hi = _numeric_bound(a.hi)
    blo = _numeric_bound(b.lo)
    bhi = _numeric_bound(b.hi)
    lo = None if lo is None or bhi is None else lo - bhi
    hi = None if hi is None or blo is None else hi - blo
    return Interval(lo, hi)


def iv_neg(a):
    lo = _numeric_bound(a.hi)
    hi = _numeric_bound(a.lo)
    lo = None if lo is None else -lo
    hi = None if hi is None else -hi
    return Interval(lo, hi)


def iv_widen(a, b):
    """Join (union) of two intervals — used at if/else joins and loop
    back-edges. None bound wins (wider). Deep-scan-10: symbolic (tuple)
    bounds join to the numeric unknown — sound and simple."""
    alo = _numeric_bound(a.lo)
    ahi = _numeric_bound(a.hi)
    blo = _numeric_bound(b.lo)
    bhi = _numeric_bound(b.hi)
    lo = None if alo is None or blo is None else min(alo, blo)
    hi = None if ahi is None or bhi is None else max(ahi, bhi)
    return Interval(lo, hi)


def fits(iv):
    """True if every value in iv is within int64 (used for overflow).
    Deep-scan-10 CRITICAL soundness fix: an UNKNOWN bound means the
    true value may be anywhere up to +/-infinity — TOP used to 'fit'
    int64, so `fn add(x, y) requires x >= 0 { return x + y }` was
    annotated ovf_safe and the -O fast native build emitted a raw C
    `+` that silently WRAPPED (signed-overflow UB) while the checked
    builds panicked. Both bounds must now be KNOWN and inside int64
    for a fit verdict; symbolic (tuple) bounds never fit."""
    if iv.lo is None or iv.hi is None:
        return False
    if isinstance(iv.lo, tuple) or isinstance(iv.hi, tuple):
        return False
    return iv.lo >= INT64_MIN and iv.hi <= INT64_MAX


def add_fits(a, b):
    return fits(iv_add(a, b))


def sub_fits(a, b):
    return fits(iv_sub(a, b))


def mul_fits(a, b):
    return fits(iv_mul(a, b))


def excludes_zero(iv):
    """True if 0 is provably NOT in iv (used for division).
    Deep-scan-10: symbolic (tuple) bounds carry no numeric order —
    they must simply answer 'unknown' (the old code compared a tuple
    with 0 and crashed)."""
    if isinstance(iv.hi, tuple) or isinstance(iv.lo, tuple):
        return False
    if iv.hi is not None and iv.hi < 0:
        return True
    if iv.lo is not None and iv.lo > 0:
        return True
    return False


# ----------------------------------------------------------------------------
# Seeding: walk a requires expression, binding variable bounds.
# ----------------------------------------------------------------------------

# Deep-scan-10 fix: internal fact keys are NUL-prefixed so NO legal
# HLS identifier can collide with them. A parameter literally named
# `__nz__` or `__minlen__` used to crash the engine with a raw
# AttributeError ('Interval' object has no attribute 'add').
_NZ = "\x00nz"         # set of vars provably != 0
_MINLEN = "\x00minlen"  # {owner -> known minimum length}


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
        # x < s.len()  ->  x <= len-1  (delta -1)  [a VALID index bound]
        # x <= s.len() ->  x <= len    (delta  0)  [NOT a valid index
        #     bound — x == len is out of bounds; valid only as a slice
        #     END bound]. Deep-scan-10 soundness fix: the old code
        #     stored delta 0 for `<` (and None for `<=`) and the
        #     consumers IGNORED the delta entirely, so `i <= xs.len()`
        #     proved `xs[i]` — a native OOB read under -O fast.
        owner = _len_owner(r)
        if op == "<":
            _refine(facts, li, None, ("len", owner, -1))
        elif op == "<=":
            _refine(facts, li, None, ("len", owner, 0))
    elif _is_len_call(l) and ri is not None:
        owner = _len_owner(l)
        if op == ">":
            _refine(facts, ri, None, ("len", owner, -1))
        elif op == ">=":
            _refine(facts, ri, None, ("len", owner, 0))
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
    cur = facts.get(_MINLEN, {})
    old = cur.get(owner)
    if old is None or k > old:
        cur[owner] = k
    facts[_MINLEN] = cur


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
    info), ("len", owner, delta) symbolic (semantics: var <=
    len(owner) + delta), or the string "nz" (the interval excludes
    zero)."""
    old = facts.get(var, TOP)
    if not isinstance(old, Interval):
        old = TOP
    if lo == "nz":
        # Excludes zero: if the interval is entirely one side of 0, we
        # can tighten; otherwise only record non-zero-ness.
        if old.hi is not None and not isinstance(old.hi, tuple) and old.hi < 0:
            pass  # already negative-only
        elif old.lo is not None and not isinstance(old.lo, tuple) and old.lo > 0:
            pass  # already positive-only
        else:
            # Split into two intervals; represent conservatively as
            # (min_int, -1) U (1, max_int) — we cannot express unions,
            # so record the WIDER fact "excludes zero" via the nz set.
            facts[var] = old
            nzset = facts.get(_NZ)
            if not isinstance(nzset, set):
                nzset = set()
                facts[_NZ] = nzset
            nzset.add(var)
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
    new_lo = old.lo if lo is None else (lo if old.lo is None or isinstance(old.lo, tuple) else max(old.lo, lo))
    new_hi_ = hi
    if new_hi_ is None:
        new_hi_ = old.hi
    elif old.hi is not None and not isinstance(old.hi, tuple) \
            and not isinstance(new_hi_, tuple):
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
        f = facts.get(n)
        if isinstance(f, Interval):
            return f
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
    # Deep-scan-10: CLEAR the annotation when not proven — the loop
    # analysis runs multiple passes (Kleene rounds + the final
    # invariant pass), and a stale True from a too-precise intermediate
    # pass must never survive into the final verdict.
    e["ovf_safe"] = ok
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
    # "\x00nz" facts (x != 0 seeds) — check the ident specially.
    if not nz:
        r = e.get("r")
        if isinstance(r, dict) and r.get("k") == "ident":
            nz_set = facts.get(_NZ)
            if isinstance(nz_set, set) and r.get("name") in nz_set:
                nz = True
    if not nz:
        e["div_safe"] = False
        return False
    li = expr_interval(e.get("l"), None, None, facts)
    # INT64_MIN / -1 overflow corner (deep-scan-10 fix: an UNBOUNDED
    # dividend (lo is None) may BE INT64_MIN — the old check only fired
    # when lo was exactly INT64_MIN, so `x / y requires y < 0` was
    # proven div_safe and the native -O fast build hit UB on
    # INT64_MIN / -1).
    can_be_min = li.lo is None or (not isinstance(li.lo, tuple) and li.lo <= INT64_MIN)
    if can_be_min:
        if ri.lo is None or (not isinstance(ri.lo, tuple) and ri.lo <= -1) \
                and (ri.hi is None or (not isinstance(ri.hi, tuple) and ri.hi >= -1)):
            e["div_safe"] = False
            return False
    e["div_safe"] = True
    return True


def _bound_for_index(e, facts):
    return expr_interval(e, None, None, facts)


def check_index_safe(e, facts):
    """If e is an index `xs[i]` with i provably in [0, len-1], annotate
    e['bnd_safe']. Two proof routes:
      - symbolic: the index var carries a ("len", owner, delta) upper
        bound with delta == -1 (i <= len-1 - the ONLY delta that makes
        the access valid; deep-scan-10: the delta used to be ignored,
        so `i <= xs.len()` proved `xs[i]`) AND the container is that
        owner;
      - numeric: the index interval is [lo, hi] with lo >= 0 and hi <
        minlen[owner] (seeded from `requires xs.len() > hi`).

    Deep-scan-10: the annotation is (re)set on every evaluation - the
    loop analysis visits expressions in multiple passes and a stale
    True from a too-precise intermediate pass must never survive.
    """
    if not isinstance(e, dict) or e.get("k") != "index":
        return False  # wrong node kind: never touch the flag
    e["bnd_safe"] = False  # verdict for THIS pass (reset any stale True)
    tgt = e.get("target")
    idx = e.get("idx")
    if not isinstance(tgt, dict) or not isinstance(idx, dict):
        return False
    if tgt.get("k") != "ident":
        return False
    ii = _bound_for_index(idx, facts)
    if ii.lo is None or isinstance(ii.lo, tuple) or ii.lo < 0:
        return False
    owner = tgt.get("name")
    hi = ii.hi
    if isinstance(hi, tuple) and hi[0] == "len" and hi[1] == owner \
            and hi[2] == -1:
        e["bnd_safe"] = True
        return True
    minlen = facts.get(_MINLEN)
    if isinstance(minlen, dict) and owner in minlen \
            and isinstance(hi, int) and hi < minlen[owner]:
        e["bnd_safe"] = True
        return True
    return False



def check_byte_at_safe(e, facts):
    """s.byte_at(i) with i in [0, s.len()-1] - symbolic or minlen rule.
    The flag is reset each pass (see check_index_safe)."""
    if not isinstance(e, dict) or e.get("k") != "method":
        return False  # wrong node kind: never touch the flag
    if e.get("name") != "byte_at":
        return False
    e["bnd_safe"] = False  # verdict for THIS pass (reset any stale True)
    tgt = e.get("target")
    args = e.get("args") or []
    if not isinstance(tgt, dict) or tgt.get("k") != "ident" or not args:
        return False
    ii = _bound_for_index(args[0], facts)
    if ii.lo is None or isinstance(ii.lo, tuple) or ii.lo < 0:
        return False
    owner = tgt.get("name")
    hi = ii.hi
    if isinstance(hi, tuple) and hi[0] == "len" and hi[1] == owner \
            and hi[2] == -1:
        e["bnd_safe"] = True
        return True
    minlen = facts.get(_MINLEN)
    if isinstance(minlen, dict) and owner in minlen \
            and isinstance(hi, int) and hi < minlen[owner]:
        e["bnd_safe"] = True
        return True
    return False



def check_slice_safe(e, facts):
    """s.slice(a, b) with 0 <= a <= b <= len(s) - both bounds symbolic.
    Deep-scan-10 fix: `a <= b` is now a PROVEN obligation, not a
    best-effort guess - the old code granted bnd_safe whenever a's
    upper bound was unknown, so slice(5, 2) was elided to
    hl_str_slice_unchecked and the runtime panicked on a negative
    length (or worse). The flag is reset each pass (see
    check_index_safe)."""
    if not isinstance(e, dict) or e.get("k") != "method":
        return False  # wrong node kind: never touch the flag
    if e.get("name") != "slice":
        return False
    e["bnd_safe"] = False  # verdict for THIS pass (reset any stale True)
    tgt = e.get("target")
    args = e.get("args") or []
    if not isinstance(tgt, dict) or tgt.get("k") != "ident" or len(args) != 2:
        return False
    owner = tgt.get("name")
    ai = _bound_for_index(args[0], facts)
    bi = _bound_for_index(args[1], facts)
    if ai.lo is None or isinstance(ai.lo, tuple) or ai.lo < 0:
        return False
    # a <= b must be PROVEN (see the docstring).
    if not (isinstance(ai.hi, int) and isinstance(bi.lo, int)
            and ai.hi <= bi.lo):
        return False
    ok_b = False
    if isinstance(bi.hi, tuple) and bi.hi[0] == "len" and bi.hi[1] == owner \
            and bi.hi[2] in (-1, 0):
        # b <= len (delta 0) or b < len (delta -1): both are valid slice
        # ends.
        ok_b = True
    minlen = facts.get(_MINLEN)
    if isinstance(minlen, dict) and owner in minlen \
            and isinstance(bi.hi, int) and bi.hi < minlen[owner]:
        ok_b = True
    if not ok_b:
        return False
    e["bnd_safe"] = True
    return True



def _drop_facts_for_owner(facts, tname):
    """Deep-scan-10 soundness fix: reassigning `tname` invalidates every
    fact derived from its VALUE — the minimum-length fact for tname and
    any other variable's symbolic `("len", tname, ...)` upper bound
    (assigning a shorter list/str is always possible: `requires
    xs.len() >= 3` then `xs = [1]` used to keep the stale minlen and
    prove an out-of-bounds index)."""
    ml = facts.get(_MINLEN)
    if isinstance(ml, dict) and tname in ml:
        ml = dict(ml)
        ml.pop(tname, None)
        facts[_MINLEN] = ml
    for var in list(facts.keys()):
        f = facts.get(var)
        if isinstance(f, Interval) and isinstance(f.hi, tuple) \
                and f.hi[1] == tname:
            # The symbolic bound on this var pointed at the reassigned
            # owner: collapse to numeric-unknown (keep any numeric lo).
            facts[var] = Interval(f.lo if not isinstance(f.lo, tuple) else None, None)


def _copy_facts(facts):
    """Deep copy of a facts dict (the __nz set and __minlen dict are
    mutable containers shared between branches otherwise)."""
    out = {}
    for k, v in facts.items():
        if isinstance(v, Interval):
            out[k] = Interval(v.lo, v.hi)
        else:
            out[k] = v.copy() if isinstance(v, (set, dict)) else v
    return out


def _widen_growth(a, b):
    """The standard interval WIDENING operator: keep the bounds that did
    not grow, send bounds that grew to infinity. Applied after a couple
    of Kleene rounds this is what makes the loop analysis converge to a
    SOUND over-approximation without iterating to the true fixpoint
    (which may need unboundedly many rounds)."""
    if not isinstance(a, Interval) or not isinstance(b, Interval):
        return TOP
    alo = a.lo if not isinstance(a.lo, tuple) else None
    ahi = a.hi if not isinstance(a.hi, tuple) else None
    blo = b.lo if not isinstance(b.lo, tuple) else None
    bhi = b.hi if not isinstance(b.hi, tuple) else None
    lo = alo if (alo is not None and blo is not None and blo >= alo) else None
    hi = ahi if (ahi is not None and bhi is not None and bhi <= ahi) else None
    return Interval(lo, hi)


def _facts_only_int(facts):
    """The {var: Interval} view of a facts dict (internal keys dropped)."""
    return {k: v for k, v in facts.items() if isinstance(v, Interval)}


def _join_facts(a, b):
    """Join two full facts dicts (branch join / loop back-edge): int
    intervals widen; a var missing on one side goes TOP; nz sets
    intersect (both branches must prove non-zero); minlen takes the
    min (both branches must bound it below)."""
    out = {}
    keys = set(a.keys()) | set(b.keys())
    for k in keys:
        va = a.get(k)
        vb = b.get(k)
        if k == _NZ:
            sa = va if isinstance(va, set) else set()
            sb = vb if isinstance(vb, set) else set()
            inter = sa & sb
            if inter:
                out[k] = inter
        elif k == _MINLEN:
            ma = va if isinstance(va, dict) else {}
            mb = vb if isinstance(vb, dict) else {}
            both = {o: min(ma[o], mb[o]) for o in ma.keys() & mb.keys()}
            if both:
                out[k] = both
        else:
            if isinstance(va, Interval) and isinstance(vb, Interval):
                out[k] = iv_widen(va, vb)
            elif va is not None and vb is None:
                # Defined on one side only: unknown after the join.
                out[k] = TOP if not isinstance(va, Interval) else TOP
            elif vb is not None and va is None:
                out[k] = TOP
            else:
                out[k] = va  # identical non-interval values (rare)
    return out


def _iv_contains(a, b):
    """True if interval a contains interval b (None = +/-inf; symbolic
    tuples are never 'contained' — conservative)."""
    if not isinstance(a, Interval) or not isinstance(b, Interval):
        return False
    if isinstance(a.lo, tuple) or isinstance(a.hi, tuple) \
            or isinstance(b.lo, tuple) or isinstance(b.hi, tuple):
        return False
    lo_ok = a.lo is None or (b.lo is not None and a.lo <= b.lo)
    hi_ok = a.hi is None or (b.hi is not None and a.hi >= b.hi)
    return lo_ok and hi_ok


def propagate_stmts(stmts, facts, depth=0):
    """Walk statements, updating facts for int let/assign/if/for/while,
    and annotate every expression node with safety verdicts.

    v0.30.0-alpha (Stage-17 perfection) — soundness overhauls:
      * let/assign invalidates nz / minlen / symbolic-len facts derived
        from the reassigned variable's value (deep-scan-10);
      * while conditions are annotated with the LOOP-INVARIANT facts
        (entry facts widened over body outcomes), not the entry facts —
        the condition is re-evaluated on EVERY iteration with
        loop-modified values (deep-scan-10: the old entry-fact
        annotation produced false bnd_safe -> native OOB reads);
      * while loops run two Kleene rounds + the standard widening
        operator (growth -> infinity), which is strictly more precise
        than the old blanket TOP for modified variables AND sound;
      * `for i in range(a, b)` seeds i in [a, b-1] (the old code seeded
        [0, count-1] — wrong on BOTH bounds whenever a != 0);
      * non-const-range for loops seed TOP (list elements can be
        negative — the old [0, None] claimed non-negativity);
      * facts are widened AFTER a for/while loop too (the old code kept
        stale entry facts for variables modified in the body — a false
        div_safe for `x = 0` inside the loop).
    """
    if depth > 32 or not isinstance(stmts, list):
        return
    for s in stmts:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k == "while":
            # ---- while: two Kleene rounds + the standard widening
            # operator + a POST-FIXPOINT VERIFICATION pass (the
            # verification is what makes a bounded number of rounds
            # sound: any variable whose body outcome still escapes the
            # invariant is sent to TOP, the top element, which by
            # definition cannot grow further). ----
            body = s.get("body") or []
            # Round 1: exact body outcome from the entry facts.
            w1 = _copy_facts(facts)
            propagate_stmts(body, w1, depth + 1)
            f1 = _join_facts(_copy_facts(facts), w1)
            # Round 2: body outcome from the joined facts.
            w2 = _copy_facts(f1)
            propagate_stmts(body, w2, depth + 1)
            f2 = _join_facts(f1, w2)
            # Widen growth to infinity (the classic widening operator).
            finv = {}
            for key, v in f2.items():
                if key in (_NZ, _MINLEN) or not isinstance(v, Interval):
                    finv[key] = v
                    continue
                v1 = f1.get(key) if isinstance(f1.get(key), Interval) else None
                finv[key] = _widen_growth(v1, v) if v1 is not None else v
            # POST-FIXPOINT VERIFICATION.
            for _round in range(4):
                wchk = _copy_facts(finv)
                propagate_stmts(body, wchk, depth + 1)
                grew = []
                for key, v in wchk.items():
                    if key in (_NZ, _MINLEN) or not isinstance(v, Interval):
                        continue
                    cur = finv.get(key)
                    if not isinstance(cur, Interval):
                        continue
                    if not _iv_contains(cur, v):
                        grew.append(key)
                if not grew:
                    break
                for key in grew:
                    finv[key] = TOP
            # Annotate the CONDITION with the invariant: it is evaluated
            # before every iteration, with loop-modified values.
            propagate_stmt_exprs({"cond": s.get("cond")}, finv)
            # Final annotation pass for the body, under the invariant.
            wf = _copy_facts(finv)
            propagate_stmts(body, wf, depth + 1)
            # Post-loop state: the invariant over-approximates every
            # reachable state, including the exit state.
            facts.clear()
            facts.update(finv)
            continue
        if k == "for":
            # ---- for: exact const-range bounds; TOP otherwise; post-loop
            # widening. ----
            var = s.get("var")
            it = s.get("iter")
            rng = _const_range(it)
            body = s.get("body") or []
            bf = _copy_facts(facts)
            # SOUNDNESS: any int var modified inside the loop body can
            # change across iterations — its entry fact may not hold at
            # iteration 2+. Widen such vars to TOP before annotating the
            # body (the loop variable itself is immutable and gets the
            # const-range bound).
            for mv in _assigned_int_vars(body):
                if mv != var:
                    bf[mv] = TOP
            if var:
                if rng is not None:
                    # deep-scan-10: range(a, b) iterates a, a+1, ..., b-1.
                    a, b = rng
                    bf[var] = Interval(a, b - 1)
                else:
                    # deep-scan-10: iterating a list/map yields arbitrary
                    # element values — TOP, not [0, None].
                    bf[var] = TOP
            propagate_stmts(body, bf, depth + 1)
            # Post-loop: join the entry facts with the body outcome and
            # drop the loop variable (out of scope / stale).
            after = _join_facts(_copy_facts(facts), bf)
            if isinstance(var, str):
                after.pop(var, None)
            facts.clear()
            facts.update(after)
            continue
        propagate_stmt_exprs(s, facts)
        if k == "let" or k == "assign":
            t = s.get("t") or s.get("vtype")
            tgt = s.get("target")
            tname = None
            if isinstance(tgt, dict) and tgt.get("k") == "ident":
                tname = tgt.get("name")
            elif k == "let":
                tname = s.get("name")
            if tname and (t == "int"):
                # deep-scan-10: an assignment invalidates every nz / len
                # fact about the value (see _drop_facts_for_owner).
                nz = facts.get(_NZ)
                if isinstance(nz, set) and tname in nz:
                    nz = set(nz)
                    nz.discard(tname)
                    facts[_NZ] = nz
                _drop_facts_for_owner(facts, tname)
                facts[tname] = expr_interval(s.get("value"), None, None, facts)
            elif tname:
                # Deep-scan-10 (Stage-17 perfection): invalidate the
                # value-derived facts (nz / minlen / symbolic len bounds)
                # for EVERY non-int binding write — a reassigned list's
                # stale minimum-length fact proved out-of-bounds indices.
                if tname in facts:
                    del facts[tname]
                _drop_facts_for_owner(facts, tname)
        elif k == "if":
            sf = _copy_facts(facts)
            propagate_stmts(s.get("then") or [], sf, depth + 1)
            ef = _copy_facts(facts)
            propagate_stmts(s.get("els") or [], ef, depth + 1)
            # Join: union of branch outcomes (conservative).
            joined = _join_facts(sf, ef)
            facts.clear()
            facts.update(joined)


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
    """If it is range(a, b) with int literals, return (a, b) — the
    loop variable iterates a, a+1, ..., b-1 (deep-scan-10 fix: the old
    code returned the COUNT b-a, which the caller then seeded as
    [0, count-1] — wrong on both bounds whenever a != 0). Returns None
    when the bounds are not both int literals."""
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
        return (a, b)
    return (a, a)  # empty loop: any sound interval works




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
                # Deep-scan-10 fix: int / int must use C-style TRUNCATED
                # division (the HLS runtime semantics), not Python's
                # float division — `int(9223372036854775806 / 2)` used
                # to round to ...904, spurring a FALSE "contract
                # violation at call site" compile error for a contract
                # that is actually true.
                if isinstance(l, int) and isinstance(r, int):
                    if r == 0:
                        return None
                    q = abs(l) // abs(r)
                    return q if (l >= 0) == (r >= 0) else -q
                return l / r
            if op == "%":
                # C-style remainder (sign of the dividend) — matches the
                # interpreter's i64_mod and the native runtime.
                if isinstance(l, int) and isinstance(r, int):
                    if r == 0:
                        return None
                    q = abs(l) // abs(r)
                    q = q if (l >= 0) == (r >= 0) else -q
                    return l - q * r
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

# Deep-scan-10 fix: HLS `/` and `%` use C-style TRUNCATED semantics
# (remainder takes the dividend's sign), while SMT-LIB `div` / `mod` are
# EUCLIDEAN (mod result >= 0). A contract like `requires a % 2 >= 0` is
# false in HLS for a < 0 but valid under SMT `mod` — wrong z3 verdicts.
# The bridge now emits helper definitions with the exact HLS semantics.
_SMT_OPS = {"+": "+", "-": "-", "*": "*", "/": "cdiv", "%": "cmod",
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
    """The SMT-LIB2 prelude. `result_int` may be True (result: Int),
    False (result: Bool), or None (no result declaration — e.g. void or
    non-scalar returns). Deep-scan-10: `result` used to be declared only
    for int-returning fns, so any bool-returning contract referencing
    `result` emitted an assertion over an UNDECLARED constant (a z3
    error). The cdiv/cmod helpers encode the HLS (C-truncated) division
    semantics (see _SMT_OPS)."""
    # No (set-logic ...) declaration: contracts containing `/` or `%`
    # encode C-truncated division via div/mod with a VARIABLE divisor,
    # which is nonlinear (QF_NIA); z3 infers the logic automatically and
    # the files stay runnable. Plain contracts remain pure QF_LIA.
    lines = ["; Halis SMT bridge (integer arithmetic; cdiv/cmod encode"
             " the C-truncated / and %)"]
    lines.append("(define-fun cdiv ((a Int) (b Int)) Int")
    lines.append("  (ite (= b 0) 0")
    lines.append("       (ite (or (and (>= a 0) (>= b 0)) (and (< a 0) (< b 0)))")
    lines.append("            (div (abs a) (abs b))")
    lines.append("            (- (div (abs a) (abs b))))))")
    lines.append("(define-fun cmod ((a Int) (b Int)) Int")
    lines.append("  (ite (= b 0) 0")
    lines.append("       (ite (>= a 0) (mod a (abs b))")
    lines.append("            (- (mod (abs a) (abs b))))))")
    for v in vars_int:
        lines.append("(declare-const %s Int)" % v)
    if vars_str:
        # String lengths need QF_S — emit them as Int proxies with a note.
        lines.append("; strings are abstracted to their lengths (QF_LIA)")
        for v in vars_str:
            lines.append("(declare-const %s_len Int)" % v)
    if result_int is True:
        lines.append("(declare-const result Int)")
    elif result_int is False:
        lines.append("(declare-const result Bool)")
    return lines
