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

from .compat import zip_strict  # noqa: E402

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
            # Deep-scan-15 cleanup: the previous branch
            # `if isinstance(old.hi, tuple) and isinstance(new_hi, tuple):
            #    new_hi = new_hi` was a no-op self-assignment (the
            # `else if` only fires when `old.hi` is tuple and
            # `new_hi` is not — i.e. use old.hi). The both-tuple case
            # already leaves new_hi unchanged (it IS a tuple), so the
            # explicit self-assignment was dead code that confused
            # pylint (W0127) and obscured the intent.
            if isinstance(old.hi, tuple):
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
    """Best-effort owner name for a len() call (for symbolic bounds).

    Deep-scan-20 soundness fix: only plain identifiers are valid
    owners. The old code returned e.get("name") for ANY node — a
    field access `p.items.len()` keyed its minlen fact on the bare
    field name "items", which then collided with an unrelated
    parameter named `items` and proved out-of-bounds accesses on it
    (the native len_owner correctly returns "?" for field targets;
    this restores boot/native parity)."""
    if not isinstance(e, dict):
        return "?"
    if e.get("k") == "call" and e.get("name") == "len":
        a = e.get("args") or []
        if a and isinstance(a[0], dict) and a[0].get("k") == "ident":
            return a[0].get("name", "?")
    if e.get("k") == "method" and e.get("name") == "len":
        t = e.get("target")
        if isinstance(t, dict) and t.get("k") == "ident":
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


def _collect_len_mutators(e, out):
    """Deep-scan-20 soundness fix: collect the ident names whose length
    is MUTATED by a list.pop()/list.push() method call anywhere inside
    expression `e`. A minlen fact seeded from `requires xs.len() >= N`
    used to survive `let _ = xs.pop()` (an expression statement never
    ran the assignment invalidation path), so the prover kept proving
    bounds checks dead on a list that had just shrunk — the native -O
    fast build then emitted an unchecked out-of-bounds access."""
    if not isinstance(e, dict):
        return
    if e.get("k") == "method" and e.get("name") in ("pop", "push"):
        t = e.get("target")
        if isinstance(t, dict) and t.get("k") == "ident":
            out.append(t.get("name"))
    for key in ("l", "r", "e", "cond", "idx", "scrut"):
        _collect_len_mutators(e.get(key), out)
    for a in (e.get("args") or []):
        _collect_len_mutators(a, out)
    for it in (e.get("items") or []):
        _collect_len_mutators(it, out)
    # Deep-scan-30 fix: struct-literal fields are (name, expr) tuples
    # (parser.py parse_struct_lit) — the old isinstance(fld, dict)
    # test never matched, so a pop()/push() nested inside a struct
    # field value left a stale minlen fact alive (unsound under -O
    # fast: a bounds check on the shrunk list could be elided).
    for _fname, fe in (e.get("fields") or []):
        _collect_len_mutators(fe, out)
    for arm in (e.get("arms") or []):
        if isinstance(arm, dict):
            _collect_len_mutators(arm.get("body") if not isinstance(arm.get("body"), list) else None, out)
            _collect_len_mutators(arm.get("guard"), out)
    _collect_len_mutators(e.get("target"), out)


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


def propagate_stmts(stmts, facts, depth=0, loop_hook=None):
    """Walk statements, updating facts for int let/assign/if/for/while,
    and annotate every expression node with safety verdicts.

    Stage 97: `loop_hook(loop_stmt, kind, facts_snapshot)` — when not
    None, it is invoked once per while/for loop BEFORE the loop is
    processed, with a deep copy of the loop-ENTRY facts. The checker
    never passes it (default None — zero behaviour change); the Stage 97
    invariant inference uses it to snapshot the state at every loop
    head. Hooks raised during the while fixpoint's internal passes are
    the caller's business (the inference dedups by node identity and
    keeps the first — the entry-facts pass, the most precise one).

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
            if loop_hook is not None:
                loop_hook(s, "while", _copy_facts(facts))
            # Round 1: exact body outcome from the entry facts.
            w1 = _copy_facts(facts)
            propagate_stmts(body, w1, depth + 1, loop_hook)
            f1 = _join_facts(_copy_facts(facts), w1)
            # Round 2: body outcome from the joined facts.
            w2 = _copy_facts(f1)
            propagate_stmts(body, w2, depth + 1, loop_hook)
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
                propagate_stmts(body, wchk, depth + 1, loop_hook)
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
            propagate_stmts(body, wf, depth + 1, loop_hook)
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
            if loop_hook is not None:
                # Snapshot BEFORE the loop variable's range fact is
                # seeded — the hook wants the ENTRY state (the loop
                # variable has no prior existence at the head).
                loop_hook(s, "for", _copy_facts(facts))
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
            propagate_stmts(body, bf, depth + 1, loop_hook)
            # Post-loop: join the entry facts with the body outcome and
            # drop the loop variable (out of scope / stale).
            after = _join_facts(_copy_facts(facts), bf)
            if isinstance(var, str):
                after.pop(var, None)
            facts.clear()
            facts.update(after)
            continue
        propagate_stmt_exprs(s, facts)
        # Deep-scan-20 soundness fix: list.pop()/push() inside this
        # statement (expr statement, let value, condition...) mutates
        # the receiver's length — invalidate its facts exactly like a
        # reassignment would (see _collect_len_mutators).
        _muts = []
        for key in ("value", "cond", "iter", "e"):
            _collect_len_mutators(s.get(key), _muts)
        tgt = s.get("target")
        if isinstance(tgt, dict):
            _collect_len_mutators(tgt.get("idx"), _muts)
        for tname in _muts:
            _drop_facts_for_owner(facts, tname)
        if k == "let" or k == "assign":
            t = s.get("t") or s.get("vtype")
            tgt = s.get("target")
            tname = None
            if isinstance(tgt, dict) and tgt.get("k") == "ident":
                tname = tgt.get("name")
                # Deep-scan-24 fix: an `assign` statement carries no
                # type of its own (s["t"] / s["vtype"] are let-only);
                # the lvalue's static type is annotated on the TARGET
                # by check_lvalue. The old lookup therefore always saw
                # t=None for assigns, so the int branch below was DEAD
                # for `y = 5`-style assignments: facts were invalidated
                # but never recomputed — the same program proved dead
                # checks via `let y: int = 5` but kept every runtime
                # check via `y = 5` (verified; sound-but-imprecise).
                # Read the type off the target so assigns recompute too.
                if t is None and k == "assign":
                    t = tgt.get("t")
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
            propagate_stmts(s.get("then") or [], sf, depth + 1, loop_hook)
            ef = _copy_facts(facts)
            propagate_stmts(s.get("els") or [], ef, depth + 1, loop_hook)
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
            # Deep-scan-30 fix: a `let` node carries its declared type
            # under "t" (parser.py) — "vtype" only exists on `for` nodes,
            # so let-bound int variables were never collected here. They
            # are currently unreachable as loop-carried facts (no
            # shadowing), but keeping the key honest protects the set if
            # shadowing ever lands.
            if s.get("t") == "int":
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
                    # Stage 27 perfection (v0.50.3-alpha) deep-scan-18:
                    # BUG-02 fix. INT64_MIN / -1 is the unique division
                    # corner that overflows int64: abs(INT64_MIN) is
                    # 9223372036854775808 (one past INT64_MAX), so
                    # abs(l) // abs(r) returns 9223372036854775808,
                    # which exceeds INT64_MAX. The HLS runtime (i64_div)
                    # PANICS on this corner; the const-eval must NOT
                    # return a value the runtime can never produce
                    # (returning 9223372036854775808 here would let
                    # `requires INT64_MIN / -1 > 0` const-eval to True,
                    # but the runtime would panic — a soundness gap).
                    # Treat as "unknown" (return None) so the runtime
                    # check handles it.
                    if l == -9223372036854775808 and r == -1:
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
                    # Stage 27 perfection (v0.50.3-alpha) deep-scan-18:
                    # BUG-02 fix. INT64_MIN % -1 is the unique modulo
                    # corner that overflows: the mathematical result is
                    # 0, but the C `%` operator is UB for INT64_MIN % -1
                    # (and GCC may fold it to 0 OR raise SIGFPE on
                    # x86-64). The HLS runtime (i64_mod) PANICS on this
                    # corner; the const-eval must NOT return a value
                    # the runtime can never produce. Treat as "unknown".
                    if l == -9223372036854775808 and r == -1:
                        return None
                    q = abs(l) // abs(r)
                    q = q if (l >= 0) == (r >= 0) else -q
                    return l - q * r
                # Stage 53 deep-scan-22 fix (MEDIUM severity): float %
                # must use math.fmod (truncated modulo — sign of the
                # dividend), matching f64_mod in interp.py and the C
                # runtime's hl_fmod (which calls libm fmod). Python's
                # built-in % operator on floats uses FLOORED modulo
                # (sign of the divisor), so -7.0 % 3.0 = 2.0 in Python
                # but math.fmod(-7.0, 3.0) = -1.0. A contract using
                # float % would const-eval to a different value than
                # the runtime produces — a soundness gap (const-eval
                # says "satisfied" but runtime panics with --contracts).
                # Note: after the args_are_const fix above, float
                # params no longer trigger const_eval via the checker,
                # but this path is still reachable for tools that call
                # const_eval directly.
                try:
                    import math
                    return math.fmod(l, r)
                except (ValueError, TypeError):
                    return None
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
    """If every argument expression is a literal (int/bool/str),
    return {param_name: value}; else None.

    Stage 53 deep-scan-22 fix (HIGH severity): differential mismatch
    with the native const-eval (src/hlc.hls args_const_hlc). The
    native rejects FLOAT params (returns false for `pt == "float"`,
    falling through to the `else { return false }` branch), so a
    contracted fn called with float literals is NOT constant-evaluated
    by the native — the precondition defers to runtime. The boot
    previously accepted float literals and const-evaluated the
    contract, so a provably-false float contract (e.g.
    `fn f(x: float) requires x > 5.0` called with `f(1.0)`) produced
    a compile error in the boot but compiled cleanly in the native
    (deferred to runtime, which then ran "ok" without --contracts).
    Removing "float" from the accepted literal kinds makes the boot
    defer float contracts to runtime, matching the native byte-for-
    byte in differential testing."""
    consts = {}
    for (pn, pt, _), a in zip_strict(fn["params"], arg_exprs):
        if not isinstance(a, dict) or a.get("k") not in ("int", "bool", "str"):
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


# ----------------------------------------------------------------------------
# Stage 97 (v0.116.0-alpha): SMT-based loop-invariant inference.
#
# The interval engine proves BOUNDS facts by over-approximating the loop
# with widening; Stage 97 discovers invariants EXACTLY: summarise each
# loop (entry facts, the transition relation of every loop-carried int
# variable, the loop condition), generate candidate invariants from
# templates, and let an SMT solver decide initiation and preservation.
# Only candidates whose obligations come back UNSAT are reported as
# inferred — anything the solver cannot discharge is dropped (the same
# soundness discipline as the interval engine: never claim a proof you
# do not have).
#
# Transition language (per loop iteration, per carried variable):
#   x' = a*x + sum(±y or ±y') + c   with at most two variable terms,
#   x' = literal, or UNMODELED. An unmodeled variable is encoded as a
#   FREE primed constant — the standard over-approximation — so any
#   body shape the summariser does not understand (calls, non-affine
#   arithmetic, assignment through control flow it cannot order, a
#   nested loop writing the variable, return/break/continue escapes)
#   simply loses precision, never soundness: a free x' can refute any
#   candidate that is not genuinely inductive.
#
# Encoding: initiation is  entry-facts ∧ ¬cand  (for-range loops add
# the "the loop runs" guard a < b — a for-variable is only bound when
# an iteration happens, so an empty range vacuously satisfies every
# candidate). Preservation is  cand ∧ accepted ∧ cond ∧ transition ∧
# ¬cand'  — already-accepted candidates join the hypothesis (the
# classic strengthening loop: `s >= 0` for `s' = s + i` is inductive
# only together with `i >= 0`), which is why candidates on variables
# READ by other variables' transitions are verified first.
# ----------------------------------------------------------------------------

_IDENTITY = (1, (), 0)  # x' = x


class Cand:
    """One candidate invariant over a single carried variable."""

    __slots__ = ("var", "text", "smt", "smt_primed", "tag")

    def __init__(self, var, text, smt, smt_primed, tag):
        self.var = var
        self.text = text      # human-readable: "i <= n + 1"
        self.smt = smt        # unprimed SMT term: "(<= i (+ n 1))"
        self.smt_primed = smt_primed
        self.tag = tag        # "entry-bound" | "cond-bound" | "range" | "modular"


class LoopSummary:
    """Everything the inference knows about one loop."""

    __slots__ = ("fn_key", "kind", "line", "header", "header_var",
                 "carried", "unmodeled", "inv_vars", "entry_facts",
                 "cond_shape", "entry_eq", "runs_guard", "cands",
                 "accepted", "rejected", "note")

    def __init__(self, fn_key, kind, line, header):
        self.fn_key = fn_key
        self.kind = kind            # "while" | "for"
        self.line = line
        self.header = header
        self.header_var = None      # the for-range variable, if any
        self.carried = {}           # var -> [disjunct]  (disjunct = (self, terms, const))
        self.unmodeled = {}         # var -> reason
        self.inv_vars = set()       # int vars read but not carried
        self.entry_facts = {}       # loop-entry interval snapshot
        self.cond_shape = None      # (op, var, bkind, bval) var-on-left
        self.entry_eq = None        # ("var"| "lit", v) — for-range variable start
        self.runs_guard = None      # (a, b) encodings — a < b, for-loops only
        self.cands = []             # [Cand], verification order
        self.accepted = []          # [Cand]
        self.rejected = []          # [(Cand, "initiation" | "preservation")]
        self.note = None            # extra report line (e.g. cond unmodeled)

    def transition_text(self, var):
        if var in self.unmodeled:
            return "%s' = <unmodeled: %s>" % (var, self.unmodeled[var])
        ds = self.carried.get(var)
        if not ds:
            return "%s' = <unmodeled: no affine update>" % var
        parts = []
        for d in ds:
            parts.append(_disjunct_text(var, d))
        return "%s' = %s" % (var, " | ".join(parts))


def _disjunct_text(var, d):
    self_c, terms, const = d
    parts = []
    if self_c == 1:
        parts.append(var)
    elif self_c == -1:
        parts.append("-" + var)
    elif self_c != 0:
        parts.append("%d*%s" % (self_c, var))
    for name, primed, coeff in terms:
        nm = name + ("'" if primed else "")
        if coeff == 1:
            parts.append(nm)
        elif coeff == -1:
            parts.append("-" + nm)
        else:
            parts.append("%d*%s" % (coeff, nm))
    if const != 0 or not parts:
        parts.append(str(const))
    out = parts[0]
    for p in parts[1:]:
        out += " + " + p if not p.startswith("-") else " - " + p[1:]
    return out


def _disjunct_smt(var, d, prime=False):
    self_c, terms, const = d
    v = var + "_p" if prime else var
    parts = []
    if self_c == 1:
        parts.append(v)
    elif self_c == -1:
        parts.append("(- %s)" % v)
    elif self_c != 0:
        parts.append("(* %d %s)" % (self_c, v))
    for name, pr, coeff in terms:
        nm = name + "_p" if pr else name
        if coeff == 1:
            parts.append(nm)
        elif coeff == -1:
            parts.append("(- %s)" % nm)
        else:
            parts.append("(* %d %s)" % (coeff, nm))
    if const != 0 or not parts:
        parts.append(str(const))
    if len(parts) == 1:
        return parts[0]
    return "(+ %s)" % " ".join(parts)


def _expr_text(e):
    """Minimal textual rendering of an int expression (reports only)."""
    if not isinstance(e, dict):
        return "?"
    k = e.get("k")
    if k == "int":
        return str(e.get("v"))
    if k == "ident":
        return e.get("name", "?")
    if k == "un" and e.get("op") == "-":
        inner = _expr_text(e.get("e"))
        return "-" + inner if not inner.startswith("-") else inner[1:]
    if k == "bin":
        return "%s %s %s" % (_expr_text(e.get("l")), e.get("op"),
                             _expr_text(e.get("r")))
    if k == "call":
        args = ", ".join(_expr_text(a) for a in (e.get("args") or []))
        return "%s(%s)" % (e.get("name", "?"), args)
    return "?"


def _smt_bump(base_kind, base_val, delta):
    """base + delta as an SMT term / plain literal when delta == 0."""
    if delta == 0:
        return str(base_val) if base_kind == "lit" else base_val
    if base_kind == "lit":
        return str(base_val + delta)
    op = "+" if delta > 0 else "-"
    return "(%s %s %d)" % (op, base_val, abs(delta))


def _smt_cmp(op, lhs, rhs):
    smt_op = {"==": "=", "!=": "distinct"}.get(op, op)
    if smt_op == "distinct":
        return "(distinct %s %s)" % (lhs, rhs)
    return "(%s %s %s)" % (smt_op, lhs, rhs)


def _has_escape(stmts, depth=0):
    """True if any return/break/continue is reachable in the statement
    tree. Escaping paths are excluded wholesale (see the transition-
    language note): every assigned variable goes unmodeled."""
    if depth > 32:
        return True
    for s in stmts or []:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k in ("return", "break", "continue"):
            return True
        if k == "if" and (_has_escape(s.get("then"), depth + 1)
                          or _has_escape(s.get("els"), depth + 1)):
            return True
        if k in ("while", "for") and _has_escape(s.get("body"), depth + 1):
            return True
    return False


def _assigned_names(stmts, acc=None, depth=0):
    """Every name assigned (let/assign) anywhere in the statement tree,
    in first-appearance order. Names of unknown type are included — the
    type filter happens at transition extraction."""
    if acc is None:
        acc = []
        seen = set()
    else:
        seen = set(acc)
    if depth > 32:
        return acc
    for s in stmts or []:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k == "let":
            n = s.get("name")
        elif k == "assign":
            t = s.get("target")
            n = t.get("name") if isinstance(t, dict) else None
        else:
            n = None
        if isinstance(n, str) and n not in seen:
            seen.add(n)
            acc.append(n)
        for bkey in ("body", "then", "els"):
            sub = s.get(bkey)
            if sub:
                before = len(acc)
                _assigned_names(sub, acc, depth + 1)
                seen.update(acc[before:])
    return acc


def _expr_idents(e, out, depth=0):
    """Collect every identifier read inside an expression."""
    if not isinstance(e, dict) or depth > 48:
        return
    if e.get("k") == "ident":
        n = e.get("name")
        if isinstance(n, str):
            out.add(n)
    for key in ("l", "r", "e", "cond", "idx", "scrut", "target"):
        _expr_idents(e.get(key), out, depth + 1)
    for a in (e.get("args") or []):
        _expr_idents(a, out, depth + 1)
    for it in (e.get("items") or []):
        _expr_idents(it, out, depth + 1)
    for _fn, fe in (e.get("fields") or []):
        _expr_idents(fe, out, depth + 1)
    for arm in (e.get("arms") or []):
        if isinstance(arm, dict):
            _expr_idents(arm.get("body"), out, depth + 1)
            _expr_idents(arm.get("guard"), out, depth + 1)


def _stmt_idents(s, out):
    """Every identifier read by one statement (its expressions)."""
    if not isinstance(s, dict):
        return
    for key in ("value", "cond", "iter", "e"):
        _expr_idents(s.get(key), out)
    t = s.get("target")
    if isinstance(t, dict):
        _expr_idents(t.get("idx"), out)


def _int_target(s):
    """(name, is_int) for a let/assign target — is_int reads the type
    the checker annotated (inference always runs post-check)."""
    k = s.get("k")
    if k == "let":
        return s.get("name"), s.get("t") == "int"
    if k == "assign":
        t = s.get("target")
        if isinstance(t, dict) and t.get("k") == "ident":
            return t.get("name"), t.get("t") == "int"
    return None, False


def _affine_of(e, lhs, assigned_before, carried):
    """Parse e as  x' = self*lhs + Σ(±y or ±y') + const  with at most
    two variable terms in play. Returns (self, terms, const) or None.
    `terms` is a tuple of (name, primed, coeff); primed means the
    variable was already assigned earlier on the same path (statement
    order), coeff is the literal multiplier."""
    if not isinstance(e, dict):
        return None
    k = e.get("k")
    if k == "int":
        v = e.get("v")
        return (0, (), v) if isinstance(v, int) else None
    if k == "un" and e.get("op") == "-":
        inner = e.get("e")
        if isinstance(inner, dict) and inner.get("k") == "int" \
                and isinstance(inner.get("v"), int):
            return (0, (), -inner["v"])
        return None
    if k == "ident":
        n = e.get("name")
        if n == lhs:
            return (1, (), 0)
        return (0, ((n, n in assigned_before, 1),), 0)
    if k == "bin":
        op = e.get("op")
        if op not in ("+", "-", "*"):
            return None
        if op == "*":
            # literal * form (constant coefficient) — either side.
            for const_side, form_side in ((e.get("l"), e.get("r")),
                                          (e.get("r"), e.get("l"))):
                if isinstance(const_side, dict) and const_side.get("k") == "int" \
                        and isinstance(const_side.get("v"), int):
                    f = _affine_of(form_side, lhs, assigned_before, carried)
                    if f is not None:
                        kc = const_side["v"]
                        return (f[0] * kc,
                                tuple((n, p, c * kc) for n, p, c in f[1]),
                                f[2] * kc)
            return None
        a = _affine_of(e.get("l"), lhs, assigned_before, carried)
        b = _affine_of(e.get("r"), lhs, assigned_before, carried)
        if a is None or b is None:
            return None
        if op == "-":
            b = (-b[0], tuple((n, p, -c) for n, p, c in b[1]), -b[2])
        self_c = a[0] + b[0]
        merged = {}
        for n, p, c in a[1] + b[1]:
            key = (n, p)
            merged[key] = merged.get(key, 0) + c
        terms = tuple((n, p, c) for (n, p), c in merged.items() if c != 0)
        const = a[2] + b[2]
        nvars = (1 if self_c != 0 else 0) + len(terms)
        if nvars > 2:
            return None
        return (self_c, terms, const)
    return None


class _BodySummary:
    """Result of walking one loop body (or one if-branch)."""

    __slots__ = ("trans", "unmodeled", "reads", "straight")

    def __init__(self):
        self.trans = {}      # var -> [disjunct]
        self.unmodeled = {}  # var -> reason
        self.reads = set()
        self.straight = True  # no if / nested loop seen


def _walk_body_stmts(stmts, assigned_before, carried_names, summary):
    """Accumulate one block's assignments into `summary`, in statement
    order. `assigned_before` is the ORDER set of the CURRENT PATH: a
    right-hand side referencing a variable assigned earlier on the same
    path reads the NEW value (primed); branches walk their own copy (a
    variable assigned on the sibling branch keeps its head value on
    this one)."""
    written = set()
    for s in stmts or []:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k in ("let", "assign"):
            name, is_int = _int_target(s)
            _stmt_idents(s, summary.reads)
            if not isinstance(name, str) or not is_int:
                continue
            if name in written or name in summary.trans \
                    or name in summary.unmodeled:
                # A second assignment to the same variable inside one
                # iteration would need affine composition — unmodeled.
                summary.unmodeled[name] = "assigned more than once"
                summary.trans.pop(name, None)
                continue
            d = _affine_of(s.get("value"), name, assigned_before,
                           carried_names)
            if d is None:
                summary.unmodeled[name] = "non-affine update"
            else:
                summary.trans[name] = [d]
            written.add(name)
            assigned_before.add(name)
        elif k == "if":
            summary.straight = False
            _stmt_idents(s, summary.reads)
            _walk_if(s, assigned_before, carried_names, summary)
        elif k in ("while", "for"):
            # A nested loop's updates happen an unknown number of times
            # per outer iteration — every variable it writes (including
            # its own loop variable) is unmodeled for THIS loop.
            summary.straight = False
            _stmt_idents(s, summary.reads)
            for inner in _assigned_names(s.get("body") or []):
                if inner not in summary.trans and inner not in summary.unmodeled:
                    summary.unmodeled[inner] = "written inside a nested loop"
        else:
            _stmt_idents(s, summary.reads)


def _walk_if(s, assigned_before, carried_names, summary):
    """Merge the then/els branch summaries of one if. Each branch walks
    its own copy of the path-order set (branches are mutually
    exclusive). A variable written on one branch only gains the branch
    disjuncts plus the identity step for the fall-through path."""
    sub_summaries = []
    for bkey in ("then", "els"):
        branch = s.get(bkey)
        if not branch:
            sub_summaries.append(None)
            continue
        bsum = _BodySummary()
        bsum.reads = summary.reads  # shared accumulator
        _walk_body_stmts(branch, set(assigned_before), carried_names, bsum)
        sub_summaries.append(bsum)
    then_s, else_s = sub_summaries
    all_vars = set()
    for bs in (then_s, else_s):
        if bs:
            all_vars |= set(bs.trans) | set(bs.unmodeled)
    for v in all_vars:
        if v in (then_s.unmodeled if then_s else {}) \
                or v in (else_s.unmodeled if else_s else {}):
            summary.unmodeled[v] = "assigned under control flow"
            summary.trans.pop(v, None)
            continue
        disjs = []
        for bs, other in ((then_s, else_s), (else_s, then_s)):
            if bs is None:
                # Absent/empty branch: it falls through keeping every
                # value — it contributes an identity for a variable the
                # OTHER branch wrote.
                if other is not None and (other.trans.get(v)
                                          or v in other.unmodeled):
                    disjs.append(_IDENTITY)
                continue
            d = bs.trans.get(v)
            if d is not None:
                disjs.extend(d)
            elif other is not None and (v in other.trans
                                        or v in other.unmodeled):
                # Written on the OTHER branch only — this path keeps
                # the value.
                disjs.append(_IDENTITY)
        if disjs:
            summary.trans[v] = disjs
        else:
            summary.unmodeled[v] = "assigned under control flow"
    for v in list(summary.trans.keys()):
        if v in summary.unmodeled:
            del summary.trans[v]
    # After the if, every merged variable's value is branch-dependent —
    # later right-hand sides on this path read the merged (primed) one.
    assigned_before.update(all_vars)


def _summarize_body(body, carried_names):
    """Transition relation of one loop body (callers exclude escapes)."""
    summary = _BodySummary()
    _walk_body_stmts(body, set(), carried_names, summary)
    return summary


def _parse_cond(cond):
    """(op, var, bkind, bval) with the variable on the left, or None.
    Recognised: var op (int literal | ident) and the flipped shapes."""
    if not isinstance(cond, dict) or cond.get("k") != "bin":
        return None
    op = cond.get("op")
    if op not in ("<", "<=", ">", ">=", "!=", "=="):
        return None
    l = cond.get("l")
    r = cond.get("r")
    if not isinstance(l, dict) or not isinstance(r, dict):
        return None

    def side(e):
        if e.get("k") == "int" and isinstance(e.get("v"), int):
            return ("lit", e["v"])
        if e.get("k") == "ident" and isinstance(e.get("name"), str):
            return ("var", e["name"])
        return None

    ls = side(l)
    rs = side(r)
    if ls is None and rs is None:
        return None
    if ls is not None and ls[0] == "var" and rs is not None:
        return (op, ls[1], rs[0], rs[1])
    if rs is not None and rs[0] == "var" and ls is not None:
        flip = {"<": ">", "<=": ">=", ">": "<", ">=": "<=",
                "==": "==", "!=": "!="}
        return (flip[op], rs[1], ls[0], ls[1])
    return None


def _parse_range(it):
    """range(a, b) bounds as ("lit", v) | ("var", name) | None."""
    if not isinstance(it, dict) or it.get("k") != "call" \
            or it.get("name") != "range":
        return None
    args = it.get("args") or []
    if len(args) != 2:
        return None

    def side(e):
        if isinstance(e, dict):
            if e.get("k") == "int" and isinstance(e.get("v"), int):
                return ("lit", e["v"])
            if e.get("k") == "ident" and isinstance(e.get("name"), str):
                return ("var", e["name"])
        return None

    a = side(args[0])
    b = side(args[1])
    if a is None or b is None:
        return None
    return (a, b)


def _mk_bound_cand(var, op, base_kind, base_val, delta, tag):
    """Candidate  var op (base + delta)  with both text and SMT form."""
    if base_kind == "lit":
        bound = base_val + delta
        text = "%s %s %d" % (var, op, bound)
        smt_rhs = str(bound)
    else:
        text = "%s %s %s" % (var, op, _bump_text(base_val, delta))
        smt_rhs = _smt_bump("var", base_val, delta)
    smt = _smt_cmp(op, var, smt_rhs)
    smt_p = _smt_cmp(op, var + "_p", smt_rhs)
    return Cand(var, text, smt, smt_p, tag)


def _bump_text(name, delta):
    if delta == 0:
        return name
    if delta > 0:
        return "%s + %d" % (name, delta)
    return "%s - %d" % (name, -delta)


def _step_const(disjuncts):
    """The common constant step c of x' = x + c — None when the
    disjuncts are not all pure equal self-steps."""
    c0 = None
    for self_c, terms, const in disjuncts:
        if self_c != 1 or terms or (c0 is not None and const != c0):
            return None
        c0 = const
    return c0


def _generate_candidates(loop):
    """Template instantiation. Every candidate is CHECKED by the solver
    before it is reported — the templates only decide what to TRY."""
    facts = loop.entry_facts
    cands = []
    for var, disjs in loop.carried.items():
        iv = facts.get(var)
        lo = iv.lo if isinstance(iv, Interval) and iv.lo is not None \
            and not isinstance(iv.lo, tuple) else None
        hi = iv.hi if isinstance(iv, Interval) and iv.hi is not None \
            and not isinstance(iv.hi, tuple) else None
        # T1 — entry bounds carried across iterations.
        if lo is not None:
            cands.append(_mk_bound_cand(var, ">=", "lit", lo, 0, "entry-bound"))
        if hi is not None:
            cands.append(_mk_bound_cand(var, "<=", "lit", hi, 0, "entry-bound"))
        # T2 — the loop condition, weakened by the step's overshoot.
        cs = loop.cond_shape
        if cs is not None and cs[1] == var:
            _op, _cv, bkind, bval = cs
            step = _step_const(disjs)
            if step is not None and step > 0 and bval != var:
                if _op == "<":
                    cands.append(_mk_bound_cand(var, "<=", bkind, bval,
                                                step - 1, "cond-bound"))
                elif _op == "<=":
                    cands.append(_mk_bound_cand(var, "<=", bkind, bval,
                                                step, "cond-bound"))
            if step is not None and step < 0 and bval != var:
                if _op == ">":
                    cands.append(_mk_bound_cand(var, ">=", bkind, bval,
                                                step + 1, "cond-bound"))
                elif _op == ">=":
                    cands.append(_mk_bound_cand(var, ">=", bkind, bval,
                                                step, "cond-bound"))
        # T4 — congruence: a constant step preserves the residue class
        # of an exactly-known start (the non-interval family the
        # interval engine cannot express).
        step = _step_const(disjs)
        if step not in (None, 0, 1, -1) and iv is not None and iv.is_const():
            m = abs(step)
            start = iv.lo
            text = "%s ≡ %d (mod %d)" % (var, start % m, m)
            smt = "(= (mod %s %d) (mod %d %d))" % (var, m, start, m)
            smt_p = "(= (mod %s_p %d) (mod %d %d))" % (var, m, start, m)
            cands.append(Cand(var, text, smt, smt_p, "modular"))
    # for-range: the loop variable's start anchors a lower bound (both
    # literal and loop-invariant-variable starts).
    if loop.kind == "for" and loop.entry_eq is not None:
        var = loop.header_var
        kind, aval = loop.entry_eq
        lo_known = var not in loop.unmodeled and var in loop.carried
        if lo_known:
            tag_txt = ("%s >= %d" % (var, aval)) if kind == "lit" \
                else ("%s >= %s" % (var, aval))
            if not any(c.var == var and c.tag == "entry-bound"
                       and c.text == tag_txt for c in cands):
                cands.append(_mk_bound_cand(var, ">=", kind, aval, 0,
                                            "range"))
    # Verification order: candidates on variables that OTHER variables'
    # transitions read go first (the strengthening loop needs `i >= 0`
    # accepted before `s >= 0` can be).
    read_by = {v: set() for v in loop.carried}
    for v, disjs in loop.carried.items():
        for self_c, terms, _c in disjs:
            for n, _p, _coeff in terms:
                if n in read_by and n != v:
                    read_by[n].add(v)
    def order(c):
        return (0 if read_by.get(c.var) else 1, c.var, c.tag)
    cands.sort(key=order)
    seen = set()
    out = []
    for c in cands:
        key = (c.var, c.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _build_summary(fn_key, fn, stmt, kind, entry_facts):
    """Summarise one loop node: header, transition relation, condition
    shape, candidate templates."""
    if kind == "for":
        var = stmt.get("var")
        header = "for %s in %s" % (var, _expr_text(stmt.get("iter")))
    else:
        var = None
        header = "while %s" % _expr_text(stmt.get("cond"))
    loop = LoopSummary(fn_key, kind, stmt.get("line", 0), header)
    loop.header_var = var
    loop.entry_facts = entry_facts
    body = stmt.get("body") or []

    body_assigned = [n for n in _assigned_names(body)]
    carried_names = set(body_assigned)
    if kind == "for" and isinstance(var, str):
        carried_names.add(var)

    if _has_escape(body):
        # Escaping paths (return/break/continue) are excluded wholesale:
        # a free primed value over-approximates every path outcome.
        for n in carried_names:
            loop.unmodeled[n] = "body escapes through return/break/continue"
        loop.note = ("body has return/break/continue — every assignment "
                     "goes unmodeled (sound, less precise)")
    else:
        bsum = _summarize_body(body, carried_names)
        loop.carried = dict(bsum.trans)
        loop.unmodeled = dict(bsum.unmodeled)
        reads = set(bsum.reads)
        reads.discard(var)
        reads -= set(loop.carried)
        reads -= set(loop.unmodeled)
        loop.inv_vars = {n for n in reads if isinstance(n, str)}

    if kind == "while":
        loop.cond_shape = _parse_cond(stmt.get("cond"))
        if loop.cond_shape is None and loop.note is None:
            loop.note = ("condition not in var/literal-or-var form — no "
                         "condition-derived candidates")
    else:
        rng = _parse_range(stmt.get("iter"))
        if rng is None:
            loop.note = ("iterable not a range(literal|var, literal|var) "
                         "— the loop variable is unmodeled")
            if isinstance(var, str) and var not in loop.unmodeled:
                loop.unmodeled[var] = "non-range iterable"
        else:
            (akind, aval), (bkind, bval) = rng
            ok_a = akind == "lit" or (akind == "var" and aval not in carried_names)
            ok_b = bkind == "lit" or (bkind == "var" and bval not in carried_names)
            var_clean = isinstance(var, str) and var not in loop.unmodeled
            if ok_a and ok_b and var_clean:
                # The loop variable: fresh-bound each iteration from the
                # range start, stepping by 1 — model it as carried.
                loop.carried[var] = [(1, (), 1)]
                loop.entry_eq = (akind, aval)
                loop.cond_shape = ("<", var, bkind, bval)
                loop.runs_guard = ((akind, aval), (bkind, bval))
            elif not var_clean:
                pass  # already unmodeled with its reason
            else:
                loop.note = ("range bounds reference a loop-carried "
                             "variable — the loop variable is unmodeled")
                loop.unmodeled[var] = "range bounds not loop-invariant"
        # An unmodeled loop variable must not keep a transition.
        if loop.header_var in loop.unmodeled:
            loop.carried.pop(loop.header_var, None)

    loop.cands = _generate_candidates(loop)
    return loop


def infer_loops(program):
    """Collect and summarise every loop of every function. Runs the
    interval engine with a loop hook to snapshot the ENTRY facts at
    each loop head (dedup by node identity keeps the first — the most
    precise — snapshot)."""
    loops = []
    for key, fn in program["fns"].items():
        if fn.get("extern", False):
            continue
        req = fn.get("requires")
        if req is not None:
            facts = seed_from_requires(req, fn["params"], 0)
        else:
            facts = {}
        seen = set()

        def hook(stmt, kind, snap, key=key, seen=seen, loops=loops):
            nid = id(stmt)
            if nid in seen:
                return
            seen.add(nid)
            loops.append(_build_summary(key, fn, stmt, kind, snap))

        propagate_stmts(fn["body"], facts, 0, hook)
    return loops


def _loop_decl_names(loop):
    """Every int variable the obligations mention (declaration set)."""
    decls = set(loop.carried) | set(loop.unmodeled)
    cs = loop.cond_shape
    if cs is not None:
        decls.add(cs[1])
        if cs[2] == "var":
            decls.add(cs[3])
    if loop.entry_eq is not None and loop.header_var:
        decls.add(loop.header_var)
    if loop.runs_guard is not None:
        for kind, val in loop.runs_guard:
            if kind == "var":
                decls.add(val)
    for disjs in loop.carried.values():
        for _s, terms, _c in disjs:
            for n, _p, _coeff in terms:
                decls.add(n)
    return decls


def _entry_fact_asserts(loop, decls, skip_carried=False):
    """Numeric interval facts at the loop head, for declared names only.
    Symbolic (tuple) bounds have no QF_LIA encoding — skipped.
    `skip_carried` drops facts about carried/unmodeled variables (their
    head value changes across iterations) and keeps the loop-invariant
    environment — a parameter's requires-bound holds at EVERY head
    state, so preservation may assume it."""
    skip = set(loop.carried) | set(loop.unmodeled) if skip_carried else set()
    out = []
    for name, iv in loop.entry_facts.items():
        if name not in decls or not isinstance(iv, Interval) or name in skip:
            continue
        lo, hi = iv.lo, iv.hi
        if isinstance(lo, tuple) or isinstance(hi, tuple):
            continue
        if lo is not None and hi is not None and lo == hi:
            out.append("(assert (= %s %d))" % (name, lo))
        else:
            if lo is not None:
                out.append("(assert (>= %s %d))" % (name, lo))
            if hi is not None:
                out.append("(assert (<= %s %d))" % (name, hi))
    return out


def _transition_conjuncts(loop):
    """(= v_p rhs) per modeled carried variable (or-ed over disjuncts).
    Unmodeled variables assert nothing — their primed value is free."""
    out = []
    for v in sorted(loop.carried):
        eqs = ["(= %s_p %s)" % (v, _disjunct_smt(v, d)) for d in loop.carried[v]]
        out.append(eqs[0] if len(eqs) == 1 else "(or %s)" % " ".join(eqs))
    return out


def _segment(prelude_lines, comment, asserts):
    lines = list(prelude_lines)
    lines.append(comment)
    lines.extend(asserts)
    lines.append("(check-sat)")
    return lines


def _obligation_segments(loop, cand, accepted_prefix):
    """The two SMT segments for ONE candidate at the greedy state
    `accepted_prefix` (already-accepted candidates join the
    preservation hypothesis). Returns [(label, lines)]."""
    decls = _loop_decl_names(loop)
    fact_asserts = _entry_fact_asserts(loop, decls)

    def prelude(with_primed):
        lines = []
        for v in sorted(decls):
            lines.append("(declare-const %s Int)" % v)
        if with_primed:
            for v in sorted(decls):
                lines.append("(declare-const %s_p Int)" % v)
        return lines

    entry_eq_smt = None
    if loop.entry_eq is not None and loop.header_var:
        kind, aval = loop.entry_eq
        entry_eq_smt = "(= %s %s)" % (loop.header_var,
                                      str(aval) if kind == "lit" else aval)
    guard_smt = None
    if loop.runs_guard is not None:
        (ak, av), (bk, bv) = loop.runs_guard
        a_s = str(av) if ak == "lit" else av
        b_s = str(bv) if bk == "lit" else bv
        guard_smt = "(< %s %s)" % (a_s, b_s)
    cond_smt = None
    if loop.cond_shape is not None:
        op, cv, bkind, bval = loop.cond_shape
        b_s = str(bval) if bkind == "lit" else bval
        cond_smt = _smt_cmp(op, cv, b_s)

    # -- initiation: the entry state satisfies the candidate ------------
    init_asserts = list(fact_asserts)
    if entry_eq_smt:
        init_asserts.append("(assert %s)" % entry_eq_smt)
    if guard_smt:
        init_asserts.append("(assert %s)" % guard_smt)
    init_asserts.append("(assert (not %s))" % cand.smt)
    init = _segment(prelude(False),
                    "; initiation of [%s] (%s): unsat => holds on entry"
                    % (cand.text, cand.tag),
                    init_asserts)

    # -- preservation: cand ∧ accepted ∧ env-facts ∧ cond ∧ transition
    #    ⟹ cand' ---------------------------------------------------------
    keep_asserts = ["(assert %s)" % cand.smt]
    for acc in accepted_prefix:
        keep_asserts.append("(assert %s)" % acc.smt)
    keep_asserts.extend(_entry_fact_asserts(loop, decls, skip_carried=True))
    if cond_smt:
        keep_asserts.append("(assert %s)" % cond_smt)
    for t in _transition_conjuncts(loop):
        keep_asserts.append("(assert %s)" % t)
    keep_asserts.append("(assert (not %s))" % cand.smt_primed)
    hyp = ", ".join([cand.text] + [a.text for a in accepted_prefix])
    keep = _segment(prelude(True),
                    "; preservation of [%s] given [%s]: unsat => inductive"
                    % (cand.text, hyp),
                    keep_asserts)
    return [("init: %s" % cand.text, init),
            ("keep: %s" % cand.text, keep)]


def verify_loop(loop, decide):
    """Greedy strengthening: verify candidates in order; each candidate
    may assume every candidate accepted before it. `decide(text)` runs
    one segment through an SMT solver and returns "sat" / "unsat" /
    "unknown" / "?" — or None when no solver exists (nothing is then
    claimed). Fills loop.accepted / loop.rejected. Returns the segment
    list [(label, lines)] for --smt dumps."""
    loop.accepted = []
    loop.rejected = []
    segments = []
    if decide is None:
        return segments
    for cand in loop.cands:
        segs = _obligation_segments(loop, cand, list(loop.accepted))
        init_v = decide(segs[0][1])
        if init_v != "unsat":
            loop.rejected.append((cand, "initiation"))
            continue
        keep_v = decide(segs[1][1])
        if keep_v != "unsat":
            loop.rejected.append((cand, "preservation"))
            continue
        loop.accepted.append(cand)
        segments.extend(segs)
    return segments


def smt_program_text(loop_header_comment, segments):
    """Render verified obligation segments as one .smt2 file
    (reset-separated, the same convention as the contract bridge)."""
    lines = [loop_header_comment]
    for _label, seg_lines in segments:
        lines.extend(seg_lines)
        lines.append("(reset)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------------
# Stage 100 (v0.119.0-alpha): the separation-logic fragment — heap shapes.
#
# Stages 17/97 reason about SCALARS: intervals and affine transitions over
# Ints. Neither can say anything about the HEAP — the lists a program
# allocates, aliases and writes through. This stage adds the fragment that
# can, deliberately small and every verdict checked:
#
# 1. **Footprints and alias classes.** Every list-typed binding gets an
#    alias class: a fresh class (bound to a list literal or `clone(..)`),
#    a caller-owned class (a parameter, or an alias chain into one), or
#    the wildcard class (unknown provenance — `grid.get(0)`, a call
#    result). Assignment aliases (`let ys = xs`) merge classes — because
#    Halis values ARE references (SPEC 16: assignment creates a
#    reference; mutation is visible through all references). The
#    experiment that pins the design: `f(xs, xs)` is LEGAL and the two
#    parameters alias the same list — so "distinct parameters never
#    alias" is FALSE in Halis and separation is never assumed.
#
# 2. **Separation verdicts.** For every pair of written classes the
#    fragment decides `own(a) * own(b)` — writes are disjoint — or
#    REFUSES with the concrete reason. Two fresh classes are disjoint
#    (two allocations); fresh vs anything pre-existing is disjoint (the
#    allocation happened after the other object was bound); caller-owned
#    vs caller-owned is refused (the f(xs, xs) aliasing); anything
#    touching the wildcard class is refused.
#
# 3. **Cursor-loop shape triples.** The classic separation-logic proof
#    for a list walk — the loop at head state owns
#      lseg(xs, 0, i) * cell(xs, i) * lseg(xs, i+1, xs.len())
#    and the body's write stays inside `cell(xs, i)`. The fragment
#    recognises the anchored walks: a cursor `i` compared DIRECTLY
#    against `xs.len()` (while) or `range(a, xs.len())` (for), an affine
#    self-step (i' = i + c, conditionally identity), and a FRAME check
#    with teeth: every write through xs's alias class anywhere in the
#    body must be exactly `xs[i]`, and no push/pop may touch the class —
#    which is precisely the justification for the encoding's one honest
#    lie, treating `xs_len` as a loop-invariant Int.
#
# 4. **The obligations.** Initiation: the entry state satisfies
#    `0 <= i /\ i <= xs_len` (cursor interval facts at the loop head;
#    for-ranges add the runs-guard `a < xs_len`). Preservation:
#    shape ∧ cond ∧ i' = i + c ⊫ shape' — decided by the same decider
#    abstraction as Stage 97 (z3 by default, cvc5 under --cvc5), the
#    same honesty rules: no solver installed → candidates are reported
#    and NOTHING is claimed.
#
# The fragment's deliberate refusals are its content: descending steps
# fail `0 <= i'` (one iteration from i == 0 lands at -1 — the solver
# finds it), writes off the cursor cell (or through an alias, or a
# push/pop) fail the frame, a captured `let n = xs.len()` anchor is
# refused (an Int copy has no heap link), escaping bodies are refused
# wholesale. Where the fragment refuses, Stage 97's arithmetic
# invariants may still apply — the two are complementary, not
# overlapping.
# ----------------------------------------------------------------------------

_SHAPE_WILD = -1  # the wildcard alias class (unknown provenance)


class _AliasState:
    """Alias classes over list-typed bindings, one function's worth.
    Class ids: 0..n are concrete (fresh or caller-owned), _SHAPE_WILD is
    the wildcard. `fresh[cid]` — the class was allocated INSIDE the
    function (list literal / clone(..)) and therefore provably disjoint
    from every object that existed before it."""

    def __init__(self):
        self.owner = {}       # binding -> cid | WILD
        self.members = {}     # cid -> set(bindings)
        self.fresh = {}       # cid -> bool
        self.next_cid = 0

    def new_class(self, name, fresh):
        cid = self.next_cid
        self.next_cid += 1
        self.members[cid] = {name}
        self.fresh[cid] = fresh
        self.owner[name] = cid
        return cid

    def join(self, name, cid):
        if cid == _SHAPE_WILD:
            self.owner[name] = _SHAPE_WILD
            return
        self.owner[name] = cid
        self.members[cid].add(name)

    def drop(self, name):
        cid = self.owner.get(name)
        if cid is not None and cid != _SHAPE_WILD and name in \
                self.members.get(cid, ()):
            self.members[cid].discard(name)
        self.owner.pop(name, None)

    def of(self, name):
        return self.owner.get(name, _SHAPE_WILD)

    def repr_of(self, cid):
        """A stable printable representative (sorted first member)."""
        if cid == _SHAPE_WILD:
            return "?"
        mem = self.members.get(cid) or set()
        return sorted(mem)[0] if mem else "?"

    def snapshot(self):
        cp = _AliasState()
        cp.owner = dict(self.owner)
        cp.members = {c: set(m) for c, m in self.members.items()}
        cp.fresh = dict(self.fresh)
        cp.next_cid = self.next_cid
        return cp


def _is_list_type(t):
    return isinstance(t, str) and t.startswith("list[")


def _ident_of(e):
    """The ident name of an expression node, or None."""
    if isinstance(e, dict) and e.get("k") == "ident" \
            and isinstance(e.get("name"), str):
        return e["name"]
    return None


def _len_anchor(e):
    """(root, cursor, op) when `e` is  cursor OP root.len()  (or the
    flipped/`len(root)` shapes) — the DIRECT heap anchor the fragment
    requires. A captured `let n = xs.len()` is an Int copy with no heap
    link and never matches."""
    if not isinstance(e, dict) or e.get("k") != "bin":
        return None
    op = e.get("op")
    if op not in ("<", "<=", ">", ">="):
        return None
    l, r = e.get("l"), e.get("r")

    def len_root(x):
        """xs.len() — method form — or len(xs) — call form."""
        if isinstance(x, dict) and x.get("k") == "method" \
                and x.get("name") == "len":
            return _ident_of(x.get("target"))
        if isinstance(x, dict) and x.get("k") == "call" \
                and x.get("name") == "len":
            args = x.get("args") or []
            if len(args) == 1:
                return _ident_of(args[0])
        return None

    cur = _ident_of(l)
    if cur is not None:
        root = len_root(r)
        if root is not None:
            return (root, cur, op)
    cur = _ident_of(r)
    if cur is not None:
        root = len_root(l)
        if root is not None:
            flip = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
            return (root, cur, flip[op])
    return None


class _WriteSite:
    """One heap write through a binding: xs[i] = v (idx node kept for
    the cursor comparison), xs.set(i, v), or a len mutator
    (push/pop)."""

    __slots__ = ("binding", "kind", "idx", "line")

    def __init__(self, binding, kind, idx, line):
        self.binding = binding
        self.kind = kind      # "index" | "set" | "push" | "pop"
        self.idx = idx        # idx expr node (index writes), else None
        self.line = line


class ShapeLoop:
    """One loop's shape candidate and verdict."""

    __slots__ = ("fn_key", "kind", "line", "header", "root", "cursor",
                 "cond_op", "steps", "cond_ok", "cond_reason", "frame_ok",
                 "frame_reasons", "range_lo", "entry_facts", "status",
                 "shape_text", "segments", "access_total", "access_safe",
                 "access_blocked", "minlen", "solver_reason")

    def __init__(self, fn_key, kind, line, header):
        self.fn_key = fn_key
        self.kind = kind          # "while" | "for"
        self.line = line
        self.header = header
        self.root = None          # anchor list binding
        self.cursor = None        # cursor binding
        self.cond_op = None       # "<" | "<=" (cursor on the left)
        self.steps = []           # literal self-steps [c] of i' = i + c
        self.cond_ok = False
        self.cond_reason = None
        self.frame_ok = False
        self.frame_reasons = []
        self.range_lo = None      # ("lit"|"var", val) for for-loops
        self.entry_facts = {}
        self.status = "refused"   # "proven" | "refused" | "unverified"
        self.shape_text = None
        self.segments = []
        self.access_total = 0
        self.access_safe = 0
        self.access_blocked = 0
        self.minlen = None        # entry lower bound on len(xs), if any
        self.solver_reason = None  # why the solver refused, if it did

    def refused(self, why):
        self.frame_ok = False
        self.frame_reasons.append(why)


class FnShapes:
    """One function's heap analysis: footprints, separation verdicts,
    loop-shape candidates."""

    __slots__ = ("fn_key", "fn", "classes", "writes", "reads",
                 "separation", "loops", "notes")

    def __init__(self, fn_key, fn):
        self.fn_key = fn_key
        self.fn = fn
        self.classes = _AliasState()
        self.writes = []          # [(cid, _WriteSite)]
        self.reads = set()        # cids read
        self.separation = []      # [(a_repr, b_repr, proven, reason)]
        self.loops = []           # [ShapeLoop]
        self.notes = []


def _shape_lit(e):
    """int literal node -> value, else None."""
    if isinstance(e, dict) and e.get("k") == "int" \
            and isinstance(e.get("v"), int):
        return e["v"]
    return None


def _rebind_list(state, name, value):
    """(re)bind `name` according to its value's provenance."""
    state.drop(name)
    if isinstance(value, dict):
        k = value.get("k")
        if k == "listlit":
            state.new_class(name, True)
            return
        if k == "call" and value.get("name") == "clone":
            state.new_class(name, True)
            return
        if k == "ident" and _is_list_type(value.get("t")):
            src = value.get("name")
            state.join(name, state.of(src))
            return
    state.join(name, _SHAPE_WILD)


def _iter_writes_reads(e, out_w, out_r):
    """Every heap write and heap read inside one expression tree."""
    if not isinstance(e, dict):
        return
    k = e.get("k")
    if k == "method":
        tgt = e.get("target")
        root = _ident_of(tgt)
        name = e.get("name")
        if root is not None and _is_list_type(tgt.get("t")):
            if name in ("push", "pop"):
                out_w.append(_WriteSite(root, name, None, e.get("line", 0)))
            elif name == "set":
                args = e.get("args") or []
                out_w.append(_WriteSite(root, "set",
                                        args[0] if len(args) == 2 else None,
                                        e.get("line", 0)))
            elif name == "get":
                out_r.add(root)
            elif name == "len":
                out_r.add(root)
    if k == "index":
        tgt = e.get("target")
        root = _ident_of(tgt)
        if root is not None and _is_list_type(tgt.get("t")):
            out_r.add(root)
    for key in ("l", "r", "e", "cond", "idx", "scrut", "target"):
        _iter_writes_reads(e.get(key), out_w, out_r)
    for a in (e.get("args") or []):
        _iter_writes_reads(a, out_w, out_r)
    for it in (e.get("items") or []):
        _iter_writes_reads(it, out_w, out_r)
    for _fn, fe in (e.get("fields") or []):
        _iter_writes_reads(fe, out_w, out_r)
    for arm in (e.get("arms") or []):
        if isinstance(arm, dict):
            _iter_writes_reads(arm.get("guard"), out_w, out_r)
            ab = arm.get("body")
            if isinstance(ab, list):
                for sub in ab:
                    _iter_writes_reads(sub, out_w, out_r)
            else:
                _iter_writes_reads(ab, out_w, out_r)


def _stmt_writes_reads(s, out_w, out_r):
    """Writes/reads of ONE statement: the assigned target (a heap write
    when it is an index into a list binding) plus every read in the
    value/condition expressions."""
    if not isinstance(s, dict):
        return
    k = s.get("k")
    if k == "assign":
        t = s.get("target")
        if isinstance(t, dict) and t.get("k") == "index":
            root = _ident_of(t.get("target"))
            if root is not None and _is_list_type(
                    (t.get("target") or {}).get("t")):
                out_w.append(_WriteSite(root, "index", t.get("idx"),
                                        t.get("line", 0)))
            # the index expression itself is evaluated (and read)
            _iter_writes_reads(t.get("idx"), out_w, out_r)
        _iter_writes_reads(s.get("value"), out_w, out_r)
        if isinstance(t, dict) and t.get("k") == "index":
            # the target itself is a read too (the object is loaded)
            rroot = _ident_of(t.get("target"))
            if rroot is not None and _is_list_type(
                    (t.get("target") or {}).get("t")):
                out_r.add(rroot)
        return
    if k in ("let",):
        _iter_writes_reads(s.get("value"), out_w, out_r)
        return
    for key in ("value", "cond", "iter", "e"):
        _iter_writes_reads(s.get(key), out_w, out_r)


def _cursor_accesses(stmts, root, cursor, acc):
    """In-order scan for accesses `root[<cursor>]` / `root.get(<cursor>)`
    inside one loop body. `acc` = [updated, total, safe, blocked]: every
    access BEFORE the first cursor update counts as head-state (safe
    candidates), everything after is blocked (the cursor may sit at len
    once bumped). Conservative in one direction: an update inside a
    sibling branch still blocks later accesses."""
    if not isinstance(stmts, list):
        return
    for s in stmts:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k == "assign":
            t = s.get("target")
            if isinstance(t, dict) and t.get("k") == "index":
                r = _ident_of(t.get("target"))
                idx = t.get("idx")
                if r == root and _ident_of(idx) == cursor:
                    acc[1] += 1
                    if acc[0]:
                        acc[3] += 1
                    else:
                        acc[2] += 1
                elif r == root:
                    acc[1] += 0  # an off-cursor write: not an access claim
            if isinstance(t, dict) and t.get("k") == "ident" \
                    and t.get("name") == cursor:
                acc[0] = True
                continue  # the update's RHS cannot touch the head cursor
            _rhs_accesses(s.get("value"), root, cursor, acc)
        elif k == "let":
            _rhs_accesses(s.get("value"), root, cursor, acc)
        elif k == "expr":
            _rhs_accesses(s.get("e"), root, cursor, acc)
        elif k == "return":
            _rhs_accesses(s.get("value"), root, cursor, acc)
        elif k == "if":
            _rhs_accesses(s.get("cond"), root, cursor, acc)
            _cursor_accesses(s.get("then"), root, cursor, acc)
            _cursor_accesses(s.get("els"), root, cursor, acc)
        elif k in ("while", "for"):
            _rhs_accesses(s.get("cond"), root, cursor, acc)
            _rhs_accesses(s.get("iter"), root, cursor, acc)
            _cursor_accesses(s.get("body"), root, cursor, acc)


def _rhs_accesses(e, root, cursor, acc):
    """Head-state accesses inside an expression tree (reads and
    `set` writes at the cursor). Expression evaluation at the loop head
    happens before any statement-order cursor update."""
    if not isinstance(e, dict):
        return
    k = e.get("k")
    if k == "index":
        r = _ident_of(e.get("target"))
        if r == root and _ident_of(e.get("idx")) == cursor:
            if acc[0]:
                acc[3] += 1
            else:
                acc[2] += 1
            acc[1] += 1
    if k == "method" and e.get("name") in ("get", "set"):
        r = _ident_of(e.get("target"))
        args = e.get("args") or []
        if r == root and len(args) >= 1 \
                and _ident_of(args[0]) == cursor:
            if acc[0]:
                acc[3] += 1
            else:
                acc[2] += 1
            acc[1] += 1
    for key in ("l", "r", "e", "cond", "idx", "scrut", "target"):
        _rhs_accesses(e.get(key), root, cursor, acc)
    for a in (e.get("args") or []):
        _rhs_accesses(a, root, cursor, acc)
    for it in (e.get("items") or []):
        _rhs_accesses(it, root, cursor, acc)
    for _fn, fe in (e.get("fields") or []):
        _rhs_accesses(fe, root, cursor, acc)


def _shape_cond_text(root, cursor, op):
    return "%s %s %s.len()" % (cursor, op, root)


def _build_shape_loop(fs, stmt, kind, classes):
    """Detect and summarise one loop's shape candidate. Never raises:
    anything outside the fragment lands in frame_reasons."""
    line = stmt.get("line", 0)
    if kind == "for":
        header = "for %s in %s" % (stmt.get("var"),
                                   _shape_expr_text(stmt.get("iter")))
    else:
        header = "while %s" % _shape_expr_text(stmt.get("cond"))
    ls = ShapeLoop(fs.fn_key, kind, line, header)
    body = stmt.get("body") or []

    # -- the anchor ------------------------------------------------------
    if kind == "while":
        anchor = _len_anchor(stmt.get("cond"))
        if anchor is None:
            ls.cond_reason = ("condition is not <cursor> op <root>.len()> "
                              "— no heap anchor")
            return ls
        root, cursor, op = anchor
        ls.root, ls.cursor, ls.cond_op = root, cursor, op
        ls.range_lo = None
        if op not in ("<", "<="):
            # flipped canonical form already applied by _len_anchor
            ls.cond_reason = "unsupported comparison %s" % op
            return ls
    else:
        rng = _shape_range(stmt.get("iter"))
        if rng is None:
            ls.cond_reason = ("iterable is not range(a, <root>.len()>) — "
                              "no heap anchor")
            return ls
        (akind, aval), root = rng
        cursor = stmt.get("var")
        if not isinstance(cursor, str):
            ls.cond_reason = "loop variable is not a plain binding"
            return ls
        ls.root, ls.cursor, ls.cond_op = root, cursor, "<"
        ls.range_lo = (akind, aval)
        if akind == "lit" and aval < 0:
            ls.cond_reason = ("range start %d is negative — the cursor "
                              "starts outside the list" % aval)
            return ls

    if not _is_list_type(_binding_type(fs, stmt, root)):
        ls.cond_reason = "anchor %s is not a list binding" % root
        return ls

    # -- the transition ---------------------------------------------------
    if _has_escape(body):
        ls.refused("body escapes through return/break/continue — the "
                   "shape after the escape is unowned")
        return ls
    carried = set(_assigned_names(body))
    carried.add(ls.cursor)
    if kind == "while":
        sumr = _summarize_body(body, carried)
        if ls.cursor in sumr.unmodeled:
            ls.refused("cursor %s is unmodeled: %s"
                       % (ls.cursor, sumr.unmodeled[ls.cursor]))
            return ls
        disjs = sumr.trans.get(ls.cursor)
        if not disjs:
            ls.refused("cursor %s is never reassigned in the body — not "
                       "a walk" % ls.cursor)
            return ls
        steps = []
        for self_c, terms, const in disjs:
            if terms:
                ls.refused("cursor step references other variables — "
                           "outside the fragment")
                return ls
            if self_c not in (0, 1):
                ls.refused("cursor step is not a self-step")
                return ls
            if self_c == 0 and const != 0:
                ls.refused("cursor step rebinds to a constant")
                return ls
            if self_c == 1:
                steps.append(const)
        if not steps:
            ls.refused("cursor never moves")
            return ls
        ls.steps = steps
    else:
        # for-range: the cursor is fresh-bound per iteration, step +1.
        for name in _assigned_names(body):
            if name == ls.cursor:
                ls.refused("the for-variable %s is reassigned in the body"
                           % ls.cursor)
                return ls
        ls.steps = [1]

    # -- the frame ---------------------------------------------------------
    w_sites = []
    w_locals = []
    _stmt_writes_list(body, w_sites, w_locals)
    cid = classes.of(ls.root)
    reasons = []
    for site in w_sites:
        site_cid = classes.of(site.binding)
        if site_cid != cid:
            continue
        if site.kind in ("push", "pop"):
            reasons.append("%s.%s() at line %d changes len(%s)"
                           % (site.binding, site.kind, site.line, ls.root))
        elif site.kind == "set":
            if _ident_of(site.idx) != ls.cursor:
                reasons.append("%s.set(..) at line %d writes off the "
                               "cursor cell"
                               % (site.binding, site.line))
        elif site.kind == "index":
            if _ident_of(site.idx) != ls.cursor:
                reasons.append("%s[..] = .. at line %d writes off the "
                               "cursor cell"
                               % (site.binding, site.line))
    for name in w_locals:
        if name == ls.root or (cid != _SHAPE_WILD
                               and name in classes.members.get(cid, ())):
            reasons.append("%s is rebound inside the loop" % name)
    # a wildcard-class write may alias ANY class — including the anchor's
    if any(classes.of(w.binding) == _SHAPE_WILD for w in w_sites):
        reasons.append("a write through unknown provenance may reach %s"
                       % ls.root)
    if reasons:
        ls.frame_reasons.extend(reasons)
        return ls
    ls.frame_ok = True

    # -- accesses at the cursor cell ---------------------------------------
    strict = ls.cond_op == "<" if kind == "while" else True
    acc = [False, 0, 0, 0]  # updated, total, safe, blocked
    _cursor_accesses(body, ls.root, ls.cursor, acc)
    ls.access_total = acc[1]
    if strict:
        ls.access_safe = acc[2]
        ls.access_blocked = acc[3]
    else:
        ls.access_blocked = acc[1]
        ls.access_safe = 0
    ls.cond_ok = True
    return ls


def _binding_type(fs, stmt, name):
    """The checked type of a binding name (params first, then let/assign
    annotations anywhere in the function)."""
    for pn, pt, _rest in _fn_params(fs):
        if pn == name:
            return pt
    found = _binding_type_scan(fs.fn.get("body") or [], name)
    return found


def _fn_params(fs):
    return fs.fn.get("params") or []


def _binding_type_scan(stmts, name):
    if not isinstance(stmts, list):
        return None
    for s in stmts:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k == "let" and s.get("name") == name:
            t = s.get("t")
            if isinstance(t, str):
                return t
        if k == "assign":
            t = s.get("target")
            if isinstance(t, dict) and t.get("k") == "ident" \
                    and t.get("name") == name and isinstance(t.get("t"), str):
                return t.get("t")
        for bkey in ("body", "then", "els"):
            r = _binding_type_scan(s.get(bkey), name)
            if r is not None:
                return r
    return None


def _shape_range(it):
    """range(a, <root>.len()) -> ((akind, aval), root). a: int literal or
    plain ident (the runs-guard decides whether it is in bounds)."""
    if not isinstance(it, dict) or it.get("k") != "call" \
            or it.get("name") != "range":
        return None
    args = it.get("args") or []
    if len(args) != 2:
        return None
    a = args[0]
    if isinstance(a, dict) and a.get("k") == "int" \
            and isinstance(a.get("v"), int):
        lo = ("lit", a["v"])
    elif isinstance(a, dict) and a.get("k") == "ident" \
            and isinstance(a.get("name"), str):
        lo = ("var", a["name"])
    else:
        return None
    b = args[1]
    root = None
    if isinstance(b, dict) and b.get("k") == "method" \
            and b.get("name") == "len":
        root = _ident_of(b.get("target"))
    elif isinstance(b, dict) and b.get("k") == "call" \
            and b.get("name") == "len":
        bargs = b.get("args") or []
        if len(bargs) == 1:
            root = _ident_of(bargs[0])
    if root is None:
        return None
    return (lo, root)


def _stmt_writes_list(stmts, out_sites, out_rebinds):
    """Every heap write site and list-binding rebind inside a statement
    tree (recursing through nested loops — writes there still happen)."""
    if not isinstance(stmts, list):
        return
    for s in stmts:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k == "assign":
            t = s.get("target")
            if isinstance(t, dict) and t.get("k") == "index":
                root = _ident_of(t.get("target"))
                if root is not None and _is_list_type(
                        (t.get("target") or {}).get("t")):
                    out_sites.append(_WriteSite(root, "index", t.get("idx"),
                                                s.get("line", 0)))
            elif isinstance(t, dict) and t.get("k") == "ident" \
                    and _is_list_type(t.get("t")):
                out_rebinds.append(t.get("name"))
            _iter_writes_reads(s.get("value"), out_sites, set())
        elif k == "let":
            if _is_list_type(s.get("t")) and isinstance(s.get("name"), str):
                out_rebinds.append(s.get("name"))
            _iter_writes_reads(s.get("value"), out_sites, set())
        elif k == "expr":
            _iter_writes_reads(s.get("e"), out_sites, set())
        elif k == "return":
            _iter_writes_reads(s.get("value"), out_sites, set())
        elif k == "if":
            _iter_writes_reads(s.get("cond"), out_sites, set())
            _stmt_writes_list(s.get("then"), out_sites, out_rebinds)
            _stmt_writes_list(s.get("els"), out_sites, out_rebinds)
        elif k in ("while", "for"):
            _iter_writes_reads(s.get("cond"), out_sites, set())
            _iter_writes_reads(s.get("iter"), out_sites, set())
            _stmt_writes_list(s.get("body"), out_sites, out_rebinds)


def _shape_expr_text(e):
    """Textual rendering for shape headers (len() calls included)."""
    if not isinstance(e, dict):
        return "?"
    k = e.get("k")
    if k == "int":
        return str(e.get("v"))
    if k == "ident":
        return e.get("name", "?")
    if k == "method":
        tgt = _shape_expr_text(e.get("target"))
        if e.get("name") == "len" and not (e.get("args") or []):
            return "%s.len()" % tgt
        return "%s.%s(..)" % (tgt, e.get("name"))
    if k == "call":
        args = ", ".join(_shape_expr_text(a) for a in (e.get("args") or []))
        return "%s(%s)" % (e.get("name", "?"), args)
    if k == "bin":
        return "%s %s %s" % (_shape_expr_text(e.get("l")), e.get("op"),
                             _shape_expr_text(e.get("r")))
    return "?"


def _walk_shape_stmts(fs, stmts):
    """The main alias-tracking walk: maintain classes through
    let/assign, collect writes/reads, and hand every loop to
    _build_shape_loop with the class snapshot AT the loop head."""
    if not isinstance(stmts, list):
        return
    for s in stmts:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        if k == "let":
            val = s.get("value")
            if _is_list_type(s.get("t")) and isinstance(s.get("name"), str):
                _rebind_list(fs.classes, s["name"], val)
            _collect_wr(fs, val)
        elif k == "assign":
            t = s.get("target")
            if isinstance(t, dict) and t.get("k") == "ident" \
                    and _is_list_type(t.get("t")):
                _rebind_list(fs.classes, t.get("name"), s.get("value"))
            _collect_wr_stmt(fs, s)
        elif k == "expr":
            _collect_wr(fs, s.get("e"))
        elif k == "return":
            _collect_wr(fs, s.get("value"))
        elif k == "if":
            _collect_wr(fs, s.get("cond"))
            _walk_shape_stmts(fs, s.get("then"))
            _walk_shape_stmts(fs, s.get("els"))
        elif k in ("while", "for"):
            if k == "while":
                _collect_wr(fs, s.get("cond"))
            else:
                _collect_wr(fs, s.get("iter"))
            ls = _build_shape_loop(fs, s, k, fs.classes.snapshot())
            fs.loops.append(ls)
            _walk_shape_stmts(fs, s.get("body"))


def _collect_wr_stmt(fs, s):
    """Writes/reads of one statement through the CURRENT classes."""
    out_w = []
    out_r = set()
    _stmt_writes_reads(s, out_w, out_r)
    for site in out_w:
        fs.writes.append((fs.classes.of(site.binding), site))
    for r in out_r:
        fs.reads.add(fs.classes.of(r))


def _collect_wr(fs, e):
    """Writes/reads inside one expression tree through the CURRENT
    classes."""
    if not isinstance(e, dict):
        return
    out_w = []
    out_r = set()
    _iter_writes_reads(e, out_w, out_r)
    for site in out_w:
        fs.writes.append((fs.classes.of(site.binding), site))
    for r in out_r:
        fs.reads.add(fs.classes.of(r))


def _decide_separation(fs):
    """The separation verdicts for every pair of written classes."""
    written = {}
    for cid, site in fs.writes:
        written.setdefault(cid, []).append(site)
    reps = {}
    for cid in written:
        reps[cid] = fs.classes.repr_of(cid)
    cids = sorted(written.keys(), key=lambda c: reps[c])
    if len(cids) <= 1:
        return  # single writer (or none) — nothing to separate
    for i in range(len(cids)):
        for j in range(i + 1, len(cids)):
            a, b = cids[i], cids[j]
            ra, rb = reps[a], reps[b]
            if a == _SHAPE_WILD or b == _SHAPE_WILD:
                why = ("writes through unknown provenance may alias "
                       "anything")
            elif fs.classes.fresh.get(a) or fs.classes.fresh.get(b):
                why = ("one side is allocated inside the function "
                       "(literal / clone) — disjoint from every "
                       "pre-existing object")
            else:
                why = ("both sides are caller-owned; a caller may pass "
                       "the same list to both (f(xs, xs) aliases the "
                       "parameters) — no fresh evidence")
            proven = (a != _SHAPE_WILD and b != _SHAPE_WILD
                      and (fs.classes.fresh.get(a)
                           or fs.classes.fresh.get(b)))
            fs.separation.append((ra, rb, proven, why))


def analyze_heap(fn_key, fn):
    """The full Stage 100 heap analysis of one function."""
    fs = FnShapes(fn_key, fn)
    for p in fn.get("params") or []:
        name, ptype = p[0], p[1]
        if _is_list_type(ptype):
            fs.classes.new_class(name, False)  # caller-owned
    _walk_shape_stmts(fs, fn.get("body") or [])
    _decide_separation(fs)
    return fs


def _entry_facts_map(fn):
    """id(loop stmt) -> interval facts at the loop head (the same
    hook/keep-first convention as infer_loops)."""
    req = fn.get("requires")
    base = seed_from_requires(req, fn.get("params"), 0) \
        if req is not None else {}
    out = {}

    def hook(stmt, kind, snap, out=out):
        out.setdefault(id(stmt), snap)

    propagate_stmts(fn.get("body") or [], base, 0, hook)
    return out


def collect_shapes(program):
    """Stage 100 entry point: heap analysis of every non-extern
    function, entry facts attached to every loop candidate."""
    out = []
    for key, fn in program["fns"].items():
        if fn.get("extern", False):
            continue
        fs = analyze_heap(key, fn)
        facts = _entry_facts_map(fn)
        for ls in fs.loops:
            node = _loop_node_of(fs, ls)
            snap = facts.get(id(node), {})
            ls.entry_facts = snap
            ml = snap.get(_MINLEN)
            if isinstance(ml, dict):
                v = ml.get(ls.root)
                if isinstance(v, int):
                    ls.minlen = v
        out.append(fs)
    return out


def _loop_node_of(fs, ls):
    """The loop stmt node a ShapeLoop was built from (line + kind +
    header identify it; loops were collected in walk order)."""
    nodes = []

    def scan(stmts):
        for s in stmts or []:
            if isinstance(s, dict) and s.get("k") in ("while", "for"):
                nodes.append(s)
                scan(s.get("body"))

    scan(fs.fn.get("body") or [])
    for s in nodes:
        if s.get("line", 0) == ls.line and s.get("k") == ls.kind:
            return s
    return nodes[0] if nodes else {}


def _shape_of_loop_text(ls):
    return ("lseg(%s, 0, %s) * cell(%s, %s) * lseg(%s, %s + 1, %s.len())"
            % (ls.root, ls.cursor, ls.root, ls.cursor, ls.root, ls.cursor,
               ls.root))


def _shape_segments(ls):
    """The two SMT obligations of one shape candidate:
      initiation    — entry facts ∧ ¬shape
      preservation  — shape ∧ cond ∧ step ⊫ ¬shape'
    `xs_len` is a plain Int constant: the frame check is the SYNTACTIC
    justification for treating the list's extent as loop-invariant
    (writes stay in the cursor cell; no push/pop touches the class)."""
    xs_len = "%s_len" % ls.root
    cur = ls.cursor
    decls = [cur, xs_len]
    if ls.range_lo is not None and ls.range_lo[0] == "var":
        decls.append(ls.range_lo[1])
    shape = "(and (>= %s 0) (<= %s %s))" % (cur, cur, xs_len)
    shape_p = "(and (>= %s_p 0) (<= %s_p %s))" % (cur, cur, xs_len)

    def prelude(primed):
        lines = []
        for v in decls:
            lines.append("(declare-const %s Int)" % v)
        if primed:
            lines.append("(declare-const %s_p Int)" % cur)
        return lines

    axioms = ["; len(xs) >= 0 — a list's extent is never negative",
              "(assert (>= %s 0))" % xs_len]
    if ls.minlen is not None:
        axioms.append("; entry fact: len(%s) >= %d (seeded by requires)"
                      % (ls.root, ls.minlen))
        axioms.append("(assert (>= %s %d))" % (xs_len, ls.minlen))

    # -- initiation --------------------------------------------------------
    head = prelude(False)
    init = list(head) + axioms
    init.append("; the cursor's interval at the loop head")
    iv = ls.entry_facts.get(cur)
    if isinstance(iv, Interval):
        if iv.lo is not None and not isinstance(iv.lo, tuple):
            init.append("(assert (>= %s %d))" % (cur, iv.lo))
        if iv.hi is not None and not isinstance(iv.hi, tuple):
            init.append("(assert (<= %s %d))" % (cur, iv.hi))
    if ls.range_lo is not None:
        akind, aval = ls.range_lo
        a_s = str(aval) if akind == "lit" else aval
        # the runs-guard: a for-variable is bound only when an iteration
        # happens — an empty range vacuously satisfies every shape
        init.append("; the runs-guard: the loop body runs only when "
                    "a < len(%s)" % ls.root)
        init.append("(assert (< %s %s))" % (a_s, xs_len))
        init.append("(assert (= %s %s))" % (cur, a_s))
        if akind == "var":
            # the start's own entry interval (a `requires start >= 0`
            # seed is exactly what pins a var-start walk to the shape)
            av = ls.entry_facts.get(aval)
            if isinstance(av, Interval):
                if av.lo is not None and not isinstance(av.lo, tuple):
                    init.append("(assert (>= %s %d))" % (aval, av.lo))
                if av.hi is not None and not isinstance(av.hi, tuple):
                    init.append("(assert (<= %s %d))" % (aval, av.hi))
    init.append("; initiation: unsat => the shape holds at entry")
    init.append("(assert (not %s))" % shape)
    init_seg = _segment(head,
                        "; shape initiation of [%s]: unsat => holds on "
                        "entry" % _shape_of_loop_text(ls),
                        init[len(head):])

    # -- preservation --------------------------------------------------------
    # the next head exists only while the condition still holds, so the
    # obligation is  shape ∧ cond ∧ trans ∧ cond' ⊫ shape'  — the
    # vacuous-exit iterations are excluded, everything else must stay
    # inside the list's extent
    head_p = prelude(True)
    keep = list(head_p) + axioms
    keep.append("(assert %s)" % shape)
    if ls.range_lo is None:
        keep.append("; the loop condition")
        keep.append("(assert (%s %s %s))" % (ls.cond_op, cur, xs_len))
    else:
        akind, aval = ls.range_lo
        a_s = str(aval) if akind == "lit" else aval
        keep.append("(assert (< %s %s))" % (a_s, xs_len))
    keep.append("; the cursor's transition: i' = i + c (per disjunct)")
    eqs = ["(= %s_p (+ %s %d))" % (cur, cur, c) for c in ls.steps]
    keep.append("(assert %s)" % (eqs[0] if len(eqs) == 1
                                 else "(or %s)" % " ".join(eqs)))
    keep.append("; the condition at the NEXT head (the loop did not "
                "exit)")
    if ls.range_lo is None:
        keep.append("(assert (%s %s_p %s))" % (ls.cond_op, cur, xs_len))
    else:
        keep.append("(assert (< %s_p %s))" % (cur, xs_len))
    keep.append("; preservation: unsat => the shape is inductive")
    keep.append("(assert (not %s))" % shape_p)
    keep_seg = _segment(head_p,
                        "; shape preservation of [%s]: unsat => inductive"
                        % _shape_of_loop_text(ls),
                        keep[len(head_p):])
    return [("init: %s" % ls.header, init_seg),
            ("keep: %s" % ls.header, keep_seg)]


def verify_shapes(fs_list, decide):
    """Decide every frame-clean candidate's two obligations. Returns the
    verified segments per function for --smt dumps:
    {fn_key: [(loop, [(label, lines)])]}. With no decider, frame-clean
    candidates become "unverified" (reported, nothing claimed) — the
    Stage 97 honesty rule."""
    out = {}
    for fs in fs_list:
        for ls in fs.loops:
            if not ls.frame_ok or not ls.cond_ok:
                ls.status = "refused"
                continue
            if decide is None:
                ls.status = "unverified"
                continue
            segs = _shape_segments(ls)
            init_v = decide(segs[0][1])
            if init_v != "unsat":
                ls.status = "refused"
                ls.solver_reason = ("initiation not discharged (%s)"
                                    % init_v)
                continue
            keep_v = decide(segs[1][1])
            if keep_v != "unsat":
                ls.status = "refused"
                ls.solver_reason = ("preservation not discharged (%s)"
                                    % keep_v)
                continue
            ls.status = "proven"
            ls.shape_text = _shape_of_loop_text(ls)
            ls.segments = segs
            out.setdefault(fs.fn_key, []).append((ls, segs))
    return out

# ===========================================================================
# Stage 101 (v0.120.0-alpha): the cryptographic side-channel analysis.
#
# A taint pass whose SOURCES are the parameters a `#[secrets(...)]`
# attribute marks, and whose SINKS are the four constructs that turn a
# secret into something an attacker can measure from outside the
# process:
#
#   branch      an `if` / `while` / `match` decided by a
#               secret-derived condition — the branch predictor's
#               history follows the secret bit
#   index       a memory access whose address depends on a secret —
#               the fetched cache line follows the secret byte
#               (xs[i], s.byte_at(i), s.slice(a, b), map keys, ...)
#   loop-bound  an iteration count a secret decides (range bounds) —
#               total runtime follows the secret
#   division    a `/` or `%` with a secret operand — division latency
#               is data-dependent on several microarchitectures
#
# Everything else a secret flows through (assignment, arithmetic whose
# result is only stored or returned, a call that keeps the value
# internal) is DATA flow — invisible to a timing or cache attacker as
# long as it never reaches one of the sinks above, so it propagates
# the marking without reporting anything.
#
# The one policy decision, stated where the leaks are NOT: `.len()` on
# a secret str/list/map is PUBLIC by policy (standard constant-time
# guidance treats buffer lengths as public unless a protocol says
# otherwise). Every use is counted and printed so the audit states its
# assumptions instead of hiding them.
#
# Interprocedural by fixpoint: when a caller passes a secret argument,
# the callee's parameter becomes a secret root on the next round (and
# the flow is reported as incoming), so a leak inside a helper three
# calls deep is found at the helper's own line. Unmodeled constructs
# claim nothing — there is deliberately NO way to launder a marking
# inside the analysis (an explicit launder would be a lie the audit
# signs). Analysis-only: no compilation unit changes, interpreter and
# native binaries agree byte for byte.
# ===========================================================================

SC_KIND_ORDER = ("branch", "index", "loop-bound", "division")

# Builtin-method ops whose argument selects WHERE to read or write —
# a secret argument is an address leak, the same hazard a raw `xs[i]`
# is. Keys mirror the `rm` tags the checker puts on builtin method
# nodes; the value names the hazardous argument position(s).
SC_ADDRESS_METHODS = {
    "list.get": (0,),
    "list.set": (0,),
    "str.byte_at": (0,),
    "str.slice": (0, 1),
    "map.get": (0,),
    "map.get_or": (0,),
    "map.has": (0,),
    "map.set": (0,),
}

# The len() family — public by policy (counted, never a leak).
SC_LEN_METHODS = ("list.len", "str.len", "map.len", "chan.len")

SC_KIND_WHY = {
    "branch": "the branch predictor's state follows the secret",
    "index": "the fetched cache line follows the secret",
    "loop-bound": "the total iteration count follows the secret",
    "division": "division latency is data-dependent on some cores",
}


class SCFinding:
    """One secret-dependent sink the audit reports."""
    __slots__ = ("kind", "line", "text", "why")

    def __init__(self, kind, line, text, why):
        self.kind = kind
        self.line = line
        self.text = text
        self.why = why

    def key(self):
        return (self.kind, self.line, self.text)


class FnSideChannel:
    """The per-function side-channel report."""
    __slots__ = ("fn_key", "roots", "secret_params", "findings",
                 "length_uses", "incoming", "returns_secret")

    def __init__(self, fn_key):
        self.fn_key = fn_key
        self.roots = []           # #[secrets(...)] names (the annotation)
        self.secret_params = []   # params secret after propagation
        self.findings = []        # list[SCFinding], deduped
        self.length_uses = 0      # .len() on secret values (policy note)
        self.incoming = []        # (caller_key, param, line) secret flows
        self.returns_secret = False


def _sc_call_target(e):
    """The user-callee fn key of a call/method node, read from the
    checker's post-check tags (rc for calls, rm for methods). None for
    builtins and unresolved targets."""
    tag = e.get("rc") or e.get("rm")
    if isinstance(tag, (list, tuple)) and len(tag) == 2 and tag[0] == "user":
        return tag[1]
    return None


def _sc_builtin_op(e):
    """The builtin op of a call/method node ("str.byte_at", "range",
    ...) or None for user callees."""
    tag = e.get("rc") or e.get("rm")
    if isinstance(tag, (list, tuple)) and len(tag) == 2 and tag[0] == "builtin":
        return tag[1]
    return None


class _SCState:
    """The interprocedural fixpoint state: which fn parameters may
    carry a secret, and which fns may return one."""

    def __init__(self, program):
        self.params = {}
        self.roots = {}
        for key, fn in program["fns"].items():
            names = list((fn.get("attrs") or {}).get("secrets") or [])
            self.roots[key] = names
            self.params[key] = set(names)
        self.rets = {key: False for key in program["fns"]}
        # Stage 102: when set, _flow records EVERY secret-carrying call
        # edge (not just the first edge that marks a parameter), so the
        # verifier's chains can start at any caller. The Stage 101
        # report assembles its incoming lists with a (caller, param)
        # dedup, so the extra rows are invisible to it.
        self.always_record = False

    def snapshot(self):
        return ({k: set(v) for k, v in self.params.items()},
                dict(self.rets))


class _SCWalker:
    """One flow-sensitive pass over one function body. The `secret`
    predicate propagates the marking; `flag` reports the sinks; the
    statement walk carries the environment (var -> secret) through
    branches (join = union) and loops (two passes = widening)."""

    def __init__(self, fn_key, fn, state):
        self.fn_key = fn_key
        self.fn = fn
        self.state = state
        self.findings = {}
        self.length_uses = 0
        self.flows = []       # (callee_key, param_name, line) secret flows
        self.ret_secret = False

    def _flow(self, callee, param, line):
        """Mark the callee's parameter as a secret root for the next
        fixpoint round and record the flow for the report. Stage 102:
        under always_record every secret-carrying edge is recorded (so
        a verifier chain can be rooted at ANY caller, not just the one
        that happened to mark the parameter first); the default keeps
        the Stage 101 append-on-mark behaviour."""
        already = param in self.state.params.get(callee, ())
        if not already:
            self.state.params[callee].add(param)
            self.state.changed = True
        if already and not self.state.always_record:
            return
        self.flows.append((callee, param, line))

    def _collect_flow(self, e, env):
        """A user-call node: propagate argument secretness into the
        callee's parameters (the receiver of a method is parameter 0
        of the underlying fn)."""
        callee = _sc_call_target(e)
        if not callee or callee not in self.state.params:
            return
        cf = self.state.fns.get(callee)
        if cf is None:
            return
        params = [p[0] for p in cf.get("params", [])]
        is_method = e.get("k") == "method"
        if is_method:
            if params and self.secret(e.get("target"), env):
                self._flow(callee, params[0], e.get("line", 0))
            args = e.get("args") or []
            for i, a in enumerate(args):
                pn = params[i + 1] if i + 1 < len(params) else None
                if pn and self.secret(a, env):
                    self._flow(callee, pn, e.get("line", 0))
        else:
            args = e.get("args") or []
            for i, a in enumerate(args):
                if i < len(params) and self.secret(a, env):
                    self._flow(callee, params[i], e.get("line", 0))

    # -- reporting ---------------------------------------------------------

    def finding(self, kind, line, text):
        f = SCFinding(kind, line, text, SC_KIND_WHY[kind])
        self.findings.setdefault(f.key(), f)

    # -- the secretness predicate ----------------------------------------

    def secret(self, e, env):
        if not isinstance(e, dict):
            return False
        k = e.get("k")
        if k in ("int", "float", "bool", "str"):
            return False
        if k == "ident":
            return bool(env.get(e.get("name")))
        if k == "un":
            return self.secret(e.get("e"), env)
        if k == "bin":
            return (self.secret(e.get("l"), env)
                    or self.secret(e.get("r"), env))
        if k in ("index", "field"):
            return self.secret(e.get("target"), env)
        if k == "listlit":
            return any(self.secret(i, env)
                       for i in (e.get("items") or []))
        if k == "structlit":
            return any(self.secret(fe, env)
                       for _n, fe in (e.get("fields") or []))
        if k in ("fieldcall", "enumlit"):
            if self.secret(e.get("target"), env):
                return True
            return any(self.secret(a, env)
                       for a in (e.get("args") or []))
        if k == "method":
            op = _sc_builtin_op(e)
            if op in SC_LEN_METHODS:
                return False  # public by policy
            if self.secret(e.get("target"), env):
                return True
            if any(self.secret(a, env) for a in (e.get("args") or [])):
                return True
            key = _sc_call_target(e)
            if key:
                return bool(self.state.rets.get(key))
            return False
        if k == "call":
            key = _sc_call_target(e)
            if key:
                if any(self.secret(a, env)
                       for a in (e.get("args") or [])):
                    return True
                return bool(self.state.rets.get(key))
            if e.get("name") == "range":
                return False  # a range object only lives in a for header
            return any(self.secret(a, env)
                       for a in (e.get("args") or []))
        if k == "match":
            scrut = e.get("scrut")
            scrut_secret = self.secret(scrut, env)
            if scrut_secret:
                return True
            for arm in (e.get("arms") or []):
                binds = ((arm.get("pattern") or {}).get("bindings")
                         or [])
                aenv = dict(env)
                for b in binds:
                    if b != "_":
                        aenv[b] = False
                if self.secret(arm.get("body"), aenv):
                    return True
            return False
        if k == "qmark":
            return self.secret(e.get("e"), env)
        return False

    # -- the sink flagger --------------------------------------------------
    # Visits every subexpression of every expression the statement walk
    # touches and reports the sinks the predicate itself does not:
    # secret-decided addressing and secret divisions. Branch sinks are
    # flagged by the statement walk (it knows statement lines).

    def flag_expr(self, e, env):
        if not isinstance(e, dict):
            return
        k = e.get("k")
        if k == "bin":
            if e.get("op") in ("/", "%") and (
                    self.secret(e.get("l"), env)
                    or self.secret(e.get("r"), env)):
                self.finding("division", e.get("line", 0),
                             _sc_text(e))
            self.flag_expr(e.get("l"), env)
            self.flag_expr(e.get("r"), env)
            return
        if k == "un":
            self.flag_expr(e.get("e"), env)
            return
        if k == "index":
            if self.secret(e.get("idx"), env):
                self.finding("index", e.get("line", 0),
                             _sc_text(e))
            self.flag_expr(e.get("target"), env)
            self.flag_expr(e.get("idx"), env)
            return
        if k == "method":
            op = _sc_builtin_op(e)
            target = e.get("target")
            args = e.get("args") or []
            if op in SC_LEN_METHODS:
                if self.secret(target, env):
                    self.length_uses += 1
            elif op in SC_ADDRESS_METHODS:
                for pos in SC_ADDRESS_METHODS[op]:
                    if pos < len(args) and self.secret(args[pos], env):
                        self.finding("index", e.get("line", 0),
                                     _sc_text(e))
                        break
            else:
                # user method: the call itself is data flow, but a
                # secret ARGUMENT is a flow into the callee's
                # parameter (the fixpoint's incoming edge)
                self._collect_flow(e, env)
            self.flag_expr(target, env)
            for a in args:
                self.flag_expr(a, env)
            return
        if k == "call":
            args = e.get("args") or []
            if e.get("name") != "range":
                # Secret range bounds are flagged as LOOP-BOUND by the
                # for-statement walk; user callees only collect flows.
                self._collect_flow(e, env)
            for a in args:
                self.flag_expr(a, env)
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
        if k == "match":
            # a match in ANY position (statement, return value, let
            # value) decides control flow by its scrutinee; walk_match
            # flags the statement form, this flags the value forms —
            # the dedup key collapses the double report
            scrut = e.get("scrut")
            if self.secret(scrut, env):
                self.finding("branch", e.get("line", 0), _sc_text(scrut))
            self.flag_expr(scrut, env)
            for arm in (e.get("arms") or []):
                self.flag_expr(arm.get("body"), env)
            return
        if k == "qmark":
            self.flag_expr(e.get("e"), env)
            return
        # field / ident / literals: nothing to flag

    # -- the statement walk ------------------------------------------------

    def walk_stmts(self, stmts, env):
        for s in stmts or []:
            if not isinstance(s, dict):
                continue
            k = s.get("k")
            if k == "let":
                self.flag_expr(s.get("value"), env)
                env[s["name"]] = self.secret(s.get("value"), env)
            elif k == "assign":
                tgt = s.get("target") or {}
                self.flag_expr(tgt, env)
                self.flag_expr(s.get("value"), env)
                if tgt.get("k") == "ident":
                    env[tgt["name"]] = self.secret(s.get("value"), env)
                elif tgt.get("k") in ("field", "index"):
                    base = _sc_root_name(tgt)
                    if base is not None and self.secret(s.get("value"),
                                                        env):
                        env[base] = True
            elif k == "expr":
                e = s.get("e")
                if isinstance(e, dict) and e.get("k") == "match":
                    self.walk_match(e, env)
                else:
                    self.flag_expr(e, env)
                    self.sc_side_effects(e, env)
            elif k == "return":
                v = s.get("value")
                if v is not None:
                    self.flag_expr(v, env)
                    if self.secret(v, env):
                        self.ret_secret = True
            elif k == "if":
                cond = s.get("cond")
                self.flag_expr(cond, env)
                if self.secret(cond, env):
                    self.finding("branch", s.get("line", 0),
                                 _sc_text(cond))
                then_env = dict(env)
                self.walk_stmts(s.get("then"), then_env)
                else_env = dict(env)
                self.walk_stmts(s.get("els"), else_env)
                # join: a variable secret on EITHER path is secret
                for name, v in then_env.items():
                    env[name] = env.get(name, False) or v
                for name, v in else_env.items():
                    env[name] = env.get(name, False) or v
            elif k == "while":
                cond = s.get("cond")
                self.flag_expr(cond, env)
                if self.secret(cond, env):
                    self.finding("branch", s.get("line", 0),
                                 _sc_text(cond))
                # widening: the body runs to a fixpoint of its own
                # environment (a secret assigned inside the loop is
                # secret on every later iteration)
                self.walk_stmts(s.get("body"), env)
                self.walk_stmts(s.get("body"), env)
            elif k == "for":
                it = s.get("iter") or {}
                self.flag_expr(it, env)
                if (it.get("k") == "call" and it.get("name") == "range"):
                    args = it.get("args") or []
                    if any(self.secret(a, env) for a in args):
                        self.finding("loop-bound", s.get("line", 0),
                                     _sc_text(it))
                    env[s["var"]] = any(self.secret(a, env)
                                        for a in args)
                else:
                    env[s["var"]] = self.secret(it, env)
                self.walk_stmts(s.get("body"), env)
                self.walk_stmts(s.get("body"), env)
            elif k == "match":
                # match-as-statement arrives wrapped in an expr node;
                # a bare match node here is defensive (the parser
                # always wraps).
                m = s.get("e") or s
                self.walk_match(m, env)
            elif k in ("break", "continue", "asm"):
                pass  # escapes / hand-written asm: out of the fragment
            else:
                # unknown statement kinds: walk any expression payloads
                # defensively so nothing hides from the flagger
                for key in ("value", "cond", "iter", "e"):
                    self.flag_expr(s.get(key), env)

    def walk_match(self, m, env):
        if not isinstance(m, dict) or m.get("k") != "match":
            return
        scrut = m.get("scrut")
        self.flag_expr(scrut, env)
        scrut_secret = self.secret(scrut, env)
        if scrut_secret:
            self.finding("branch", m.get("line", 0), _sc_text(scrut))
        for arm in (m.get("arms") or []):
            binds = ((arm.get("pattern") or {}).get("bindings") or [])
            aenv = dict(env)
            for b in binds:
                if b != "_":
                    aenv[b] = scrut_secret
            body = arm.get("body")
            self.flag_expr(body, aenv)
            self.sc_side_effects(body, aenv)

    # -- side effects of expression statements -----------------------------
    # A statement-position method call can TAINT its receiver:
    # `xs.push(secret)` makes every later read of xs a secret read.

    def sc_side_effects(self, e, env):
        if not isinstance(e, dict):
            return
        if e.get("k") == "method":
            op = _sc_builtin_op(e)
            target = e.get("target")
            args = e.get("args") or []
            if op in ("list.push", "list.set", "map.set"):
                if any(self.secret(a, env) for a in args):
                    base = _sc_root_name(target)
                    if base is not None:
                        env[base] = True
            elif op is None:
                # user method: the callee may store its arguments in
                # the receiver's fields — conservatively taint the
                # receiver when any argument (or the receiver itself)
                # is secret
                if any(self.secret(a, env) for a in args) or \
                        self.secret(target, env):
                    base = _sc_root_name(target)
                    if base is not None:
                        env[base] = True
            self.flag_expr(target, env)
            for a in args:
                self.flag_expr(a, env)
        elif e.get("k") == "call":
            for a in (e.get("args") or []):
                self.flag_expr(a, env)
        elif e.get("k") == "qmark":
            self.sc_side_effects(e.get("e"), env)


def _sc_text(e):
    """Textual rendering for the side-channel findings — the same
    shapes _expr_text covers plus methods, indexes and fields (a
    branch condition like `key.byte_at(i) != msg.byte_at(i)` names the
    bytes it compares, not just `? != ?`)."""
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
        return "%s%s" % (e.get("op"), _sc_text(e.get("e")))
    if k == "bin":
        return "(%s %s %s)" % (_sc_text(e.get("l")), e.get("op"),
                               _sc_text(e.get("r")))
    if k == "call":
        args = ", ".join(_sc_text(a) for a in (e.get("args") or []))
        return "%s(%s)" % (e.get("name", "?"), args)
    if k == "method":
        args = ", ".join(_sc_text(a) for a in (e.get("args") or []))
        return "%s.%s(%s)" % (_sc_text(e.get("target")), e.get("name"),
                              args)
    if k == "index":
        return "%s[%s]" % (_sc_text(e.get("target")),
                           _sc_text(e.get("idx")))
    if k == "field":
        return "%s.%s" % (_sc_text(e.get("target")), e.get("name", "?"))
    if k == "listlit":
        return "[...]"
    return "?"


def _sc_root_name(e):
    """The variable at the base of an lvalue chain (xs[i].f -> xs)."""
    while isinstance(e, dict) and e.get("k") in ("index", "field"):
        e = e.get("target")
    if isinstance(e, dict) and e.get("k") == "ident":
        return e.get("name")
    return None


def _sc_round(program, state):
    """One fixpoint round: walk every fn with the current state. The
    walker reports the findings with the CURRENT state and mutates the
    state with the flows it observes (monotone growth). Returns the
    per-fn walkers keyed by fn."""
    walkers = {}
    for key, fn in program["fns"].items():
        w = _SCWalker(key, fn, state)
        env = {}
        for pn, _pt, _m in fn.get("params", []):
            env[pn] = pn in state.params.get(key, ())
        w.walk_stmts(fn.get("body"), env)
        walkers[key] = w
        ret_sec = w.ret_secret
        if ret_sec and not state.rets.get(key, False):
            state.rets[key] = True
            state.changed = True
    return walkers


def analyze_sidechannel(program):
    """The Stage 101 pass: returns {fn_key: FnSideChannel} for every
    function the marking or the propagation reaches. The fixpoint is
    monotone (parameter sets only grow, bounded by the param names),
    so it terminates; the findings grow monotonically with the state,
    so the LAST round carries the fullest report."""
    state = _SCState(program)
    state.fns = program["fns"]
    state.flows = {}
    reports = {}
    # Warm-up + fixpoint. Two walks per round would be wasteful; the
    # cap is a belt-and-braces guard (the state is bounded, so the
    # honest fixpoint converges long before it).
    for _round in range(64):
        state.changed = False
        walkers = _sc_round(program, state)
        for key, w in walkers.items():
            rep = reports.setdefault(key, FnSideChannel(key))
            rep.findings = list(w.findings.values())
            rep.length_uses = w.length_uses
            state.flows.setdefault(key, []).extend(w.flows)
        if not state.changed:
            break
    # Assemble: incoming flows per callee (deduped per caller+param,
    # first line wins), the secret parameters, the roots.
    incoming = {}
    for caller, flows in state.flows.items():
        for callee, param, line in flows:
            incoming.setdefault(callee, []).append((caller, param, line))
    out = {}
    for key in program["fns"]:
        rep = reports.get(key) or FnSideChannel(key)
        rep.roots = list(state.roots.get(key, []))
        rep.secret_params = sorted(state.params.get(key, ()))
        ins = []
        seen = set()
        for caller, param, line in sorted(incoming.get(key, []),
                                          key=lambda t: (t[2], t[0], t[1])):
            k = (caller, param)
            if k not in seen:
                seen.add(k)
                ins.append((caller, param, line))
        rep.incoming = ins
        rep.returns_secret = state.rets.get(key, False)
        if rep.roots or rep.incoming or rep.findings:
            out[key] = rep
    return out


# ===========================================================================
# Stage 102 (v0.121.0-alpha): the constant-time verifier — the #[ct]
# claims, proven not trusted.
#
# Stage 101 made leaks VISIBLE (an audit: every sink anywhere, exit 0
# either way). Stage 102 makes claims ENFORCEABLE. A `#[ct]` fn asserts
# "constant-time over every secret that reaches me", and the verifier
# discharges each claim by re-running the Stage 101 fixpoint in the
# claim's OWN taint universe: only that fn's #[secrets(...)] parameters
# are roots, and every other fn in the program starts public. A leak in
# an unrelated #[secrets] fn therefore cannot violate a claim it has no
# part in — the one thing a naive reading of the global report would
# get wrong, and the difference between auditing a program and
# verifying a promise.
#
# A claim is VIOLATED when a sink is reachable from the claim's secrets
# — in the claimed body itself or transitively through its callees —
# and the report prints the CHAIN (caller -> callee, the parameter each
# hop travels as, the call line) so the violation is attributable at a
# glance. Findings without a chain from the claim (the global pass's
# return-secreteness imprecision can produce sinks a param-flow cannot
# explain) are NOT charged to the claim: a violation must be a flow the
# report can actually draw. Everything else is the audit's business.
# ===========================================================================

class CtClaim:
    """One #[ct] claim's verdict."""
    __slots__ = ("fn_key", "secrets", "verified", "findings", "chains",
                 "reach", "length_uses", "returns_secret")

    def __init__(self, fn_key):
        self.fn_key = fn_key
        self.secrets = []     # the #[secrets(...)] names the claim covers
        self.verified = False
        self.findings = []    # list[(fn_key, SCFinding)] — chained only
        self.chains = {}      # (fn_key, kind, line) -> list[(fn, param, line)]
        self.reach = []       # fns the secrets touched, the claim first
        self.length_uses = 0  # .len() on secret values (the policy note)
        self.returns_secret = False


def _ct_edges(flows):
    """Dedup the accumulated caller -> [(callee, param, line)] rows
    (always_record appends across rounds) into ordered per-caller edge
    lists; first occurrence wins, so the report is deterministic."""
    edges = {}
    for caller, rows in flows.items():
        seen = set()
        ordered = []
        for callee, param, line in rows:
            k = (callee, param, line)
            if k in seen:
                continue
            seen.add(k)
            ordered.append((callee, param, line))
        if ordered:
            edges[caller] = ordered
    return edges


def _ct_chain(start, target, edges):
    """A shortest secret-flow path start -> target over the param-flow
    edges (BFS, deterministic edge order); [] when the finding sits in
    the claimed body itself, None when no path exists (the finding is
    not charged to the claim). Each hop is (callee, param, line)."""
    if start == target:
        return []
    queue = [(start, [])]
    seen = {start}
    head = 0
    while head < len(queue):
        node, path = queue[head]
        head += 1
        for callee, param, line in edges.get(node, ()):
            npath = path + [(callee, param, line)]
            if callee == target:
                return npath
            if callee in seen:
                continue
            seen.add(callee)
            queue.append((callee, npath))
    return None


def analyze_consttime(program):
    """The Stage 102 pass: one taint universe per #[ct] claim, the
    Stage 101 machinery run to its fixpoint inside each. Returns the
    CtClaim list in declaration order (only fns carrying #[ct])."""
    claims = []
    for key, fn in program["fns"].items():
        if not (fn.get("attrs") or {}).get("ct"):
            continue
        roots = list((fn.get("attrs") or {}).get("secrets") or [])
        claim = CtClaim(key)
        claim.secrets = roots
        state = _SCState(program)
        state.always_record = True
        # The claim's own universe: its #[secrets(...)] parameters are
        # the ONLY roots; every other fn starts public.
        for k in state.params:
            state.params[k] = set(roots) if k == key else set()
        state.fns = program["fns"]
        state.flows = {}
        seen_findings = set()
        findings = []
        chains = {}
        length_uses = 0
        reach = {key}
        for _round in range(64):
            state.changed = False
            walkers = _sc_round(program, state)
            # the len() policy count follows the LAST round's
            # environment (the same convention the Stage 101 report
            # uses — a widening round would otherwise recount)
            round_uses = 0
            for wkey, w in walkers.items():
                round_uses += w.length_uses
                if w.findings:
                    for f in w.findings.values():
                        fk = (wkey, f.kind, f.line, f.text)
                        if fk not in seen_findings:
                            seen_findings.add(fk)
                            findings.append((wkey, f, fk))
                state.flows.setdefault(wkey, []).extend(w.flows)
            length_uses = round_uses
            for wkey, ps in state.params.items():
                if ps:
                    reach.add(wkey)
            if not state.changed:
                break
        # Chains are computed against the CONVERGED edge set (an early
        # round can miss an edge a later one records); a finding with
        # no chain from the claim is not the claim's violation.
        chained = []
        for wkey, f, fk in findings:
            chain = _ct_chain(key, wkey, _ct_edges(state.flows))
            if chain is None:
                continue
            chained.append((wkey, f))
            chains[(wkey, f.kind, f.line)] = chain
        claim.findings = chained
        claim.chains = chains
        claim.length_uses = length_uses
        claim.returns_secret = state.rets.get(key, False)
        claim.reach = [key] + sorted(reach - {key})
        claim.verified = not chained
        claims.append(claim)
    return claims
