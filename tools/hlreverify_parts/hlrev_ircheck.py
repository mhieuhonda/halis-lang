"""hls-reverify — the IR-level proof replay (an abstract interpreter).

Stage 108. This is the INDEPENDENT re-verification: after the optimiser
transformed the program, the replay re-derives a memory-safety verdict
for every check site the transformed IR still contains, from the SAME
seeds (the `requires` clauses), with the SAME interval arithmetic
(`boot.proof` is imported, not re-implemented — ONE definition), and
under the SAME precision discipline (straight-line propagation, joins
at merges, widening at loop headers, a post-fixpoint containment
verification that sends any still-growing variable to TOP).

The state is a 3-part abstract environment over SSA names:

    ints  — name -> Interval            (int values, tuple bounds allowed)
    lens  — name -> Interval            (container minimum lengths)
    nz    — set of names provably != 0

Seeding mirrors `run_proof_pass` exactly: `seed_from_requires` over the
requires clause, then parameter refinements (`where` predicates), with
source names mapped to their IR value names (`v_<name>`).

Verdicts (mirroring `check_bin_overflow` / `check_div_safe` /
`check_index_safe` / `check_byte_at_safe` / `check_slice_safe`):

    ovf  — binop + - * on ints: add_fits / sub_fits / mul_fits
    div  — binop / % on ints: excludes_zero (or the nz set) AND the
           INT64_MIN/-1 corner on the dividend
    bnd  — list_get / byte_at / slice: index lo >= 0 AND
           (symbolic: index hi is ("len", owner, -1) with owner the
            container; slice ends also accept delta 0)
           OR numeric: index hi < lens[container].lo

On top of the intervals, the replay accepts the ALGEBRAIC IDENTITY
certificates for overflow (x+0, x-0, x*0, x*1 — the result is the other
operand, already a legal int64, whatever its bounds). These are the
only shapes `ir.optimize._annotate_safe` grants today; the audit (the
caller) fails any `safe_overflow` annotation neither an interval proof
nor a pinned identity certifies — a forged elision.

The integrity checks (memory safety OF the transformed IR itself):
    - every ("var", n) operand resolves to a definition (an instruction
      dest, a parameter value name, or a for-counter);
    - every branch/jump label resolves to a block;
    - every block has a terminator;
    - the def of every operand PRECEDES the use (layout order + same-
      block position is a valid topological order for these reducible
      CFGs) — a use before def would read uninitialised memory natively.
"""
from __future__ import annotations

from typing import Dict, Set

from boot import proof as bp
from boot.proof import Interval, TOP

from . import hlrev_common as com

MAX_ROUNDS = 32


# ---------------------------------------------------------------------------
# The abstract state
# ---------------------------------------------------------------------------

class State:
    """ints + lens + elems + nz."""

    __slots__ = ("ints", "lens", "elems", "nz")

    def __init__(self, ints=None, lens=None, elems=None, nz=None):
        self.ints: Dict[str, Interval] = ints if ints is not None else {}
        self.lens: Dict[str, Interval] = lens if lens is not None else {}
        # Stage 108: range containers — `range(a, b)` lowers to a
        # call whose "elements" are the ints a..b-1. The for-lowering's
        # generated read pulls the loop variable out of that container,
        # so the element interval is what the AST prover's const-range
        # special case (boot.proof._const_range) knows.
        self.elems: Dict[str, Interval] = elems if elems is not None else {}
        self.nz: Set[str] = nz if nz is not None else set()

    def copy(self):
        ints = {k: Interval(v.lo, v.hi)
                for k, v in self.ints.items() if isinstance(v, Interval)}
        lens = {k: Interval(v.lo, v.hi) for k, v in self.lens.items()}
        elems = {k: Interval(v.lo, v.hi) for k, v in self.elems.items()}
        return State(ints, lens, elems, set(self.nz))

    def equivalent(self, other) -> bool:
        if self.nz != other.nz or self.lens.keys() != other.lens.keys() \
                or self.elems.keys() != other.elems.keys():
            return False
        for k, v in self.lens.items():
            if (v.lo, v.hi) != (other.lens[k].lo, other.lens[k].hi):
                return False
        for k, v in self.elems.items():
            if (v.lo, v.hi) != (other.elems[k].lo, other.elems[k].hi):
                return False
        if self.ints.keys() != other.ints.keys():
            return False
        for k, v in self.ints.items():
            if (v.lo, v.hi) != (other.ints[k].lo, other.ints[k].hi):
                return False
        return True


def join_states(a, b):
    """The join at merge points — boot.proof.iv_widen IS the interval
    join (min lo / max hi, None wins); lens take the weakest lower
    bound; nz intersects (both sides must prove non-zero); a variable
    known on one side only is unknown after the join."""
    if a is None:
        return b.copy() if b is not None else None
    if b is None:
        return a.copy()
    out = State()
    for k in set(a.ints.keys()) | set(b.ints.keys()):
        va = a.ints.get(k)
        vb = b.ints.get(k)
        if isinstance(va, Interval) and isinstance(vb, Interval):
            out.ints[k] = bp.iv_widen(va, vb)
    for k in set(a.lens.keys()) & set(b.lens.keys()):
        va, vb = a.lens[k], b.lens[k]
        lo = None
        if va.lo is not None and vb.lo is not None:
            lo = min(va.lo, vb.lo)
        elif va.lo is not None:
            lo = va.lo
        elif vb.lo is not None:
            lo = vb.lo
        out.lens[k] = Interval(lo, None)
    for k in set(a.elems.keys()) & set(b.elems.keys()):
        va, vb = a.elems[k], b.elems[k]
        if (va.lo, va.hi) == (vb.lo, vb.hi):
            out.elems[k] = Interval(va.lo, va.hi)
    out.nz = set(a.nz & b.nz)
    return out


# ---------------------------------------------------------------------------
# Seeding (mirrors boot.checking.contract.run_proof_pass)
# ---------------------------------------------------------------------------

def seed_function(fn):
    """The entry state for one function: requires + parameter
    refinements, source names mapped to IR value names. None when the
    function carries no contract (the prover never ran there)."""
    req = fn.get("requires")
    preds = fn.get("param_preds") or {}
    if (req is None and not preds) or fn.get("extern", False):
        return None
    facts = {}
    if req is not None:
        facts = bp.seed_from_requires(req, fn["params"], 0)
    for pn, ref in preds.items():
        if ref.get("base") != "int":
            continue
        pf = bp.seed_from_requires(ref["ast"], [("self", "int", False)], 0)
        for fk, fv in pf.items():
            if fk == bp._NZ:
                facts.setdefault(fk, set()).update(
                    pn if m == "self" else m for m in fv)
            elif fk == "self":
                facts[pn] = fv
            else:
                facts[fk] = fv
    st = State()
    for var, iv in facts.items():
        if not isinstance(var, str) or var.startswith("\x00"):
            continue
        if isinstance(iv, Interval):
            st.ints["v_" + var] = iv
    minlen = facts.get(bp._MINLEN)
    if isinstance(minlen, dict):
        for owner, k in minlen.items():
            st.lens["v_" + owner] = Interval(k, None)
    nzset = facts.get(bp._NZ)
    if isinstance(nzset, set):
        st.nz = {"v_" + n for n in nzset}
    return st


# ---------------------------------------------------------------------------
# Instruction classification (shared with the site harvest)
# ---------------------------------------------------------------------------

def _binop_kind(ins):
    if (ins.attrs or {}).get("ty") != "int":
        return None
    if not ins.args or ins.args[0][0] != "op":
        return None
    op = ins.args[0][1]
    if op in ("+", "-", "*"):
        return com.KIND_OVF
    if op in ("/", "%"):
        return com.KIND_DIV
    return None


def _method_kind(ins):
    if not ins.args or ins.args[0][0] != "name":
        return None
    name = ins.args[0][1]
    if name in ("byte_at", "slice"):
        return com.KIND_BND
    if name in ("get", "set"):
        return com.ADVISORY_KIND
    return None


def _is_generated_for(ins):
    """The for-lowering's element read: `v_X = list_get _, v_X__i`."""
    if ins.op != "list_get" or ins.dest is None:
        return False
    idx = ins.args[1] if len(ins.args) > 1 else None
    return (idx is not None and idx[0] == "var"
            and idx[1] == ins.dest + "__i")


def _identity_cert(op, a_arg, b_arg, st=None):
    """The pinned algebraic identity certificate for a binop, or None.
    A literal operand may arrive as ("lit", v) or as ("var", n) whose
    interval is the constant v (const-folded temps keep their var
    spelling)."""
    def is_lit(arg, v):
        if arg is None:
            return False
        if arg[0] == "lit":
            return arg[1] == v and not isinstance(arg[1], bool)
        if arg[0] == "var" and st is not None:
            iv = st.ints.get(arg[1])
            return (isinstance(iv, Interval) and iv.is_const()
                    and iv.const() == v)
        return False
    if op == "+":
        if is_lit(b_arg, 0):
            return "alg:add-zero-right"
        if is_lit(a_arg, 0):
            return "alg:add-zero-left"
    elif op == "-":
        if is_lit(b_arg, 0):
            return "alg:sub-zero-right"
    elif op == "*":
        if is_lit(b_arg, 0):
            return "alg:mul-zero-right"
        if is_lit(a_arg, 0):
            return "alg:mul-zero-left"
        if is_lit(b_arg, 1):
            return "alg:mul-one-right"
        if is_lit(a_arg, 1):
            return "alg:mul-one-left"
    return None


# ---------------------------------------------------------------------------
# The analysis
# ---------------------------------------------------------------------------

class ReplayResult:
    """Per-function replay outcome."""

    def __init__(self):
        self.verdicts = []      # (kind, line, dest, proven, cert, detail)
        self.annotations = []   # (line, dest, certified, cert)
        self.integrity = []     # (class, text)
        self.blocks = 0
        self.widened_loops = 0
        self.iterations = 0


def _iv_of(st, arg):
    if arg is None:
        return TOP
    if arg[0] == "lit":
        v = arg[1]
        if isinstance(v, bool) or not isinstance(v, int):
            return TOP
        return Interval(v, v)
    if arg[0] == "var":
        return st.ints.get(arg[1], TOP)
    return TOP


def _bounds_proven(st, ins, owner_pos, idx_pos):
    """The two bounds routes, mirrored from check_index_safe /
    check_byte_at_safe."""
    ii = _iv_of(st, ins.args[idx_pos] if len(ins.args) > idx_pos else None)
    if ii.lo is None or isinstance(ii.lo, tuple) or ii.lo < 0:
        return False
    o = ins.args[owner_pos] if len(ins.args) > owner_pos else None
    owner = o[1] if (o is not None and o[0] == "var") else None
    hi = ii.hi
    if isinstance(hi, tuple) and hi[0] == "len" and owner is not None \
            and owner == "v_" + hi[1] and hi[2] == -1:
        return True
    if o is not None and o[0] == "var":
        ln = st.lens.get(o[1])
        if ln is not None and not isinstance(ln.lo, tuple) \
                and ln.lo is not None and isinstance(hi, int) and hi < ln.lo:
            return True
    return False


def _slice_proven(st, ins):
    """Mirrored from check_slice_safe: 0 <= a, a <= b PROVEN, and b's
    end bound valid (symbolic delta -1/0, or numeric below the
    container's known minimum length)."""
    ai = _iv_of(st, ins.args[2] if len(ins.args) > 2 else None)
    bi = _iv_of(st, ins.args[3] if len(ins.args) > 3 else None)
    if ai.lo is None or isinstance(ai.lo, tuple) or ai.lo < 0:
        return False
    if not (isinstance(ai.hi, int) and isinstance(bi.lo, int)
            and ai.hi <= bi.lo):
        return False
    o = ins.args[1] if len(ins.args) > 1 else None
    owner = o[1] if (o is not None and o[0] == "var") else None
    hi = bi.hi
    if isinstance(hi, tuple) and hi[0] == "len" and owner is not None \
            and owner == "v_" + hi[1] and hi[2] in (-1, 0):
        return True
    if o is not None and o[0] == "var":
        ln = st.lens.get(o[1])
        if ln is not None and not isinstance(ln.lo, tuple) \
                and ln.lo is not None and isinstance(hi, int) and hi < ln.lo:
            return True
    return False


def analyze_function(irf, entry_state) -> ReplayResult:
    """The dataflow fixpoint + the final verdict pass. `entry_state`
    may be None (no contract): the analysis still runs — integrity and
    the annotation audit cover EVERY function; replayed ELISION claims
    only exist where the prover's seeds do."""
    res = ReplayResult()
    blocks = irf.blocks
    if not blocks:
        return res
    res.blocks = len(blocks)
    names = [b.name for b in blocks]
    name_set = set(names)
    by_name = {b.name: b for b in blocks}
    entry = names[0]
    where_fn = "function %s" % irf.name

    # ---- CFG + the structural integrity that needs no dataflow ----
    preds: Dict[str, list] = {n: [] for n in names}
    succs: Dict[str, list] = {}
    for blk in blocks:
        ss = []
        if blk.terminator is None:
            res.integrity.append(
                ("block-terminator",
                 "%s: block %s has no terminator" % (where_fn, blk.name)))
        elif blk.terminator.op in ("branch", "jump"):
            for a in blk.terminator.args:
                if a[0] == "label":
                    if a[1] in name_set:
                        ss.append(a[1])
                    else:
                        res.integrity.append(
                            ("dangling-label",
                             "%s: branch in %s targets unknown block %s"
                             % (where_fn, blk.name, a[1])))
            if blk.terminator.op == "branch" and \
                    (not blk.terminator.args or blk.terminator.args[0][0] != "var"):
                res.integrity.append(
                    ("branch-shape",
                     "%s: branch in %s lacks a condition value"
                     % (where_fn, blk.name)))
        succs[blk.name] = ss
        for l in ss:
            preds[l].append(blk.name)

    reach = set()
    work = [entry]
    while work:
        n = work.pop()
        if n in reach:
            continue
        reach.add(n)
        work.extend(succs[n])
    for n in names:
        if n not in reach:
            # A NOTE, not a failure: the LOWERING legitimately emits
            # unreachable blocks (an `if` whose both branches return
            # leaves its endif without predecessors — deep-scan-5 made
            # the builder skip the statements after a terminator, the
            # block shell remains). The optimiser never removes an
            # edge, so unreachable blocks here are inherited, not
            # created — and their sites are excluded from the harvest
            # (nothing unreachable ever executes).
            res.integrity.append(
                ("unreachable-block-note",
                 "%s: block %s is unreachable (inherited from the "
                 "lowering)" % (where_fn, n)))

    # ---- dominators (iterative, textbook) ----
    all_set = set(names)
    dom = {n: set(all_set) for n in names}
    dom[entry] = {entry}
    changed = True
    while changed:
        changed = False
        for n in names:
            if n == entry:
                continue
            ps = preds[n]
            if ps:
                new = set(all_set)
                for p in ps:
                    new &= dom[p]
                new.add(n)
                if new != dom[n]:
                    dom[n] = new
                    changed = True

    # ---- loop headers: targets of back edges (h dominates t) ----
    headers = set()
    for t in names:
        for h in succs[t]:
            if h in dom.get(t, set()):
                headers.add(h)
    res.widened_loops = len(headers)

    # ---- reverse postorder ----
    order = []
    visited = set()
    stack = [(entry, False)]
    while stack:
        n, done = stack.pop()
        if done:
            order.append(n)
            continue
        if n in visited:
            continue
        visited.add(n)
        stack.append((n, True))
        for s in succs[n]:
            if s not in visited:
                stack.append((s, False))
    rpo = list(reversed(order))

    entry_st = entry_state if entry_state is not None else State()
    state_in: Dict[str, State] = {}
    state_out: Dict[str, State] = {}
    prev_in: Dict[str, State] = {}
    rounds: Dict[str, int] = {n: 0 for n in names}

    def block_input(n):
        if n == entry:
            return entry_st.copy()
        inp = None
        for p in preds[n]:
            so = state_out.get(p)
            if so is not None:
                inp = join_states(inp, so)
        return inp

    def run_fixpoint():
        queue = list(rpo)
        seen_rounds = {n: 0 for n in names}
        while queue:
            n = queue.pop(0)
            seen_rounds[n] += 1
            res.iterations += 1
            if seen_rounds[n] > MAX_ROUNDS:
                continue
            inp = block_input(n)
            if inp is None:
                continue
            if n in headers and n in prev_in:
                inp = _widen_state(prev_in[n], inp)
            if n in state_in and state_in[n].equivalent(inp):
                continue
            prev_in[n] = inp.copy()
            state_in[n] = inp
            state_out[n] = _transfer_block(by_name[n], inp)
            for s in succs[n]:
                if seen_rounds[s] <= MAX_ROUNDS:
                    queue.append(s)

    run_fixpoint()

    # ---- post-fixpoint containment verification (the mirror of the
    # AST engine's soundness pass): re-run every block from its
    # stabilised input; anything that still grows escapes the invariant
    # and goes TOP; then the fixpoint re-runs once more. Bounded. ----
    for _verify_round in range(4):
        grew_any = False
        for n in rpo:
            sin = state_in.get(n)
            if sin is None:
                continue
            out = _transfer_block(by_name[n], sin)
            cur = state_out.get(n)
            if cur is None:
                state_out[n] = out
                continue
            for k, v in out.ints.items():
                cv = cur.ints.get(k)
                if isinstance(v, Interval) and isinstance(cv, Interval) \
                        and not bp._iv_contains(cv, v):
                    state_out[n].ints[k] = TOP
                    grew_any = True
            for k, v in out.lens.items():
                cv = cur.lens.get(k)
                if cv is not None and isinstance(v, Interval) \
                        and (v.lo is None or (isinstance(cv.lo, int)
                                              and (isinstance(v.lo, int)
                                                   and v.lo < cv.lo))):
                    state_out[n].lens[k] = Interval(None, None)
                    grew_any = True
        if not grew_any:
            break
        run_fixpoint()

    # ---- the FINAL verdict pass: every block under its stabilised
    # input, in layout order (deterministic), with the defs threaded
    # through for the referential-integrity check ----
    defined: Set[str] = set()
    for p in irf.params:
        defined.add("v_" + p[0])
    for blk in blocks:
        sin = state_in.get(blk.name)
        if sin is None:
            # unreachable: nothing here executes, no verdict to record
            continue
        st = sin.copy()
        for ins in blk.instrs:
            _check_integrity(ins, blk, defined, res, where_fn)
            _transfer_instr(ins, st, res, verdicts=True)
            if ins.dest:
                defined.add(ins.dest)
        if blk.terminator is not None:
            for a in blk.terminator.args:
                if a[0] == "var" and a[1] not in defined:
                    res.integrity.append(
                        ("dangling-var",
                         "%s: terminator of %s reads undefined value %s"
                         % (where_fn, blk.name, a[1])))
    return res


def _widen_state(prev, inp):
    """Widening at a loop head: interval bounds that GREW escape to
    infinity (boot.proof._widen_growth); lens keep a lower bound only
    while it did not shrink; elems are loop-invariant (kept when both
    sides agree); nz intersects."""
    out = State()
    for k in set(prev.ints.keys()) | set(inp.ints.keys()):
        va = prev.ints.get(k, TOP)
        vb = inp.ints.get(k, TOP)
        if isinstance(va, Interval) and isinstance(vb, Interval):
            out.ints[k] = bp._widen_growth(va, vb)
    for k in set(prev.lens.keys()) & set(inp.lens.keys()):
        va, vb = prev.lens[k], inp.lens[k]
        lo = None
        if va.lo is not None and vb.lo is not None and vb.lo >= va.lo:
            lo = va.lo
        out.lens[k] = Interval(lo, None)
    for k in set(prev.elems.keys()) & set(inp.elems.keys()):
        va, vb = prev.elems[k], inp.elems[k]
        if (va.lo, va.hi) == (vb.lo, vb.hi):
            out.elems[k] = Interval(va.lo, va.hi)
    out.nz = set(prev.nz & inp.nz)
    return out


def _transfer_block(blk, st):
    """One block's transfer function (no verdict recording — the
    fixpoint phases never record; the final pass re-walks)."""
    st = st.copy()
    for ins in blk.instrs:
        _transfer_instr(ins, st, None, verdicts=False)
    return st


def _check_integrity(ins, blk, defined, res, where_fn):
    """Referential integrity of one instruction against the defs seen
    so far (layout order + position = a valid topological order for
    these reducible CFGs)."""
    where = "%s: %s in block %s (line %s)" % (
        where_fn, ins.op, blk.name, ins.line)
    for a in ins.args:
        if a[0] == "var" and a[1] not in defined:
            res.integrity.append(
                ("dangling-var", where + " reads undefined value "
                 + str(a[1])))


def _transfer_instr(ins, st, res, verdicts):
    """Apply one instruction to the state; when `verdicts`, record the
    check-site verdict and the fast-annotation audit. Mirrors
    boot.proof's precision EXACTLY: an interval result is tracked only
    where expr_interval computes one."""
    op = ins.op
    dest = ins.dest

    if op == "const":
        a = ins.args[0] if ins.args else ("lit", None)
        v = a[1]
        if isinstance(v, bool):
            pass
        elif isinstance(v, int):
            st.ints[dest] = Interval(v, v)
        return

    if op == "load":
        src = ins.args[0] if ins.args else ("var", "?")
        if src[0] == "var":
            if src[1] in st.ints:
                st.ints[dest] = st.ints[src[1]]
            else:
                st.ints.pop(dest, None)
            if src[1] in st.nz:
                st.nz.add(dest)
            if src[1] in st.lens:
                st.lens[dest] = st.lens[src[1]]
            if src[1] in st.elems:
                st.elems[dest] = st.elems[src[1]]
        return

    if op == "store":
        src = ins.args[0] if ins.args else ("var", "?")
        binding = dest
        if len(ins.args) > 1 and ins.args[1][0] == "name":
            binding = "v_" + ins.args[1][1]
        # value-derived facts about the reassigned binding die
        # (mirror of _drop_facts_for_owner + the nz discard)
        st.nz.discard(binding)
        st.lens.pop(binding, None)
        st.elems.pop(binding, None)
        if src[0] == "var":
            if src[1] in st.ints:
                st.ints[binding] = st.ints[src[1]]
            else:
                st.ints.pop(binding, None)
            if src[1] in st.nz:
                st.nz.add(binding)
            if src[1] in st.lens:
                st.lens[binding] = st.lens[src[1]]
            if src[1] in st.elems:
                st.elems[binding] = st.elems[src[1]]
        elif src[0] == "lit":
            v = src[1]
            if isinstance(v, bool) or not isinstance(v, int):
                st.ints.pop(binding, None)
            else:
                st.ints[binding] = Interval(v, v)
        return

    if op == "binop":
        kind = _binop_kind(ins)
        proven = False
        cert = None
        if len(ins.args) >= 3 and ins.args[0][0] == "op":
            opname = ins.args[0][1]
            a_arg, b_arg = ins.args[1], ins.args[2]
            ai = _iv_of(st, a_arg)
            bi = _iv_of(st, b_arg)
            if kind == com.KIND_OVF:
                if opname == "+":
                    proven = bp.add_fits(ai, bi)
                    iv = bp.iv_add(ai, bi)
                elif opname == "-":
                    proven = bp.sub_fits(ai, bi)
                    iv = bp.iv_sub(ai, bi)
                else:
                    proven = bp.mul_fits(ai, bi)
                    iv = bp.iv_mul(ai, bi)
                cert = _identity_cert(opname, a_arg, b_arg, st)
                if (iv.lo is not None or iv.hi is not None) and dest:
                    st.ints[dest] = iv
            elif kind == com.KIND_DIV:
                nz = bp.excludes_zero(bi) or _var_in(st.nz, b_arg)
                if nz:
                    can_be_min = ai.lo is None or (
                        not isinstance(ai.lo, tuple) and ai.lo <= bp.INT64_MIN)
                    if can_be_min and (
                            (bi.lo is None or (not isinstance(bi.lo, tuple)
                                               and bi.lo <= -1))
                            and (bi.hi is None or (not isinstance(bi.hi, tuple)
                                                   and bi.hi >= -1))):
                        nz = False
                proven = nz
            if verdicts and kind is not None and res is not None:
                res.verdicts.append((kind, ins.line, dest, proven, cert,
                                     "%s %s" % (opname, ins.line)))
                if (ins.attrs or {}).get("safe_overflow"):
                    # the fast-annotation audit: certified by an interval
                    # proof at THIS program point, or by a pinned identity
                    certified = bool(proven) or cert is not None
                    res.annotations.append(
                        (ins.line, dest, certified,
                         cert if cert else ("interval" if proven else None)))
        return

    if op == "unop":
        if len(ins.args) >= 2 and ins.args[0][0] == "op" \
                and ins.args[0][1] == "-" and dest:
            iv = bp.iv_neg(_iv_of(st, ins.args[1]))
            if iv.lo is not None or iv.hi is not None:
                st.ints[dest] = iv
        return

    if op == "list_new":
        st.lens[dest] = Interval(len(ins.args), len(ins.args))
        return

    if op == "list_len":
        src = ins.args[0] if ins.args else ("var", "?")
        ln = st.lens.get(src[1]) if src[0] == "var" else None
        if ln is not None and ln.lo is not None and dest:
            st.ints[dest] = Interval(ln.lo, ln.hi)
        return

    if op == "call":
        # `range(a, b)` with constant bounds (literals or const temps —
        # the folded IR keeps the operands as var references to const
        # instructions): the container's length is b-a and its elements
        # are a..b-1 (the IR lowering treats range as a list-like
        # container; the AST prover's const-range case seeds the loop
        # variable from exactly these numbers).
        if ins.args and ins.args[0] == ("fname", "range") \
                and len(ins.args) == 3:
            a_iv = _iv_of(st, ins.args[1])
            b_iv = _iv_of(st, ins.args[2])
            if a_iv.is_const() and b_iv.is_const():
                a_v, b_v = a_iv.const(), b_iv.const()
                span = max(0, b_v - a_v)
                st.lens[dest] = Interval(span, span)
                st.elems[dest] = Interval(a_v, b_v - 1)
        # every other call: results unknown (TOP)
        return

    if op == "list_get":
        src = ins.args[0] if ins.args else ("var", "?")
        generated = _is_generated_for(ins)
        if not generated:
            proven = _bounds_proven(st, ins, 0, 1)
            if verdicts and res is not None:
                res.verdicts.append((com.KIND_BND, ins.line, dest, proven,
                                     None, "list_get"))
        # the loop variable read out of a range container carries the
        # container's element interval (mirrors boot.proof's
        # const-range seeding); out of a list it is TOP
        if src[0] == "var" and src[1] in st.elems and dest:
            st.ints[dest] = st.elems[src[1]]
        return

    if op == "list_set":
        if verdicts and res is not None:
            proven = _bounds_proven(st, ins, 0, 1)
            res.verdicts.append((com.ADVISORY_KIND, ins.line, dest, proven,
                                 None, "list_set"))
        return

    if op == "method":
        kind = _method_kind(ins)
        name = ins.args[0][1] if ins.args and ins.args[0][0] == "name" else ""
        if kind == com.KIND_BND:
            if name == "byte_at":
                proven = _bounds_proven(st, ins, 1, 2)
            else:
                proven = _slice_proven(st, ins)
            if verdicts and res is not None:
                res.verdicts.append((kind, ins.line, dest, proven, None,
                                     name))
        elif name == "len" and len(ins.args) > 1:
            recv = ins.args[1]
            ln = st.lens.get(recv[1]) if recv[0] == "var" else None
            if ln is not None and ln.lo is not None and dest:
                st.ints[dest] = Interval(ln.lo, ln.hi)
        elif name in ("push", "pop") and len(ins.args) > 1:
            recv = ins.args[1]
            if recv[0] == "var":
                # the receiver's length facts die (push/pop mutate it —
                # mirror of _collect_len_mutators)
                st.lens.pop(recv[1], None)
        return

    # call / qmark / struct_* / map_* / panic / comment: results unknown
    # (TOP) — the IR is post-check; nothing tracked mutates here.
    if op == "qmark" and dest:
        src = ins.args[0] if ins.args else ("var", "?")
        if src[0] == "var" and src[1] in st.ints:
            st.ints[dest] = st.ints[src[1]]
    return


def _var_in(nzset, arg):
    return arg is not None and arg[0] == "var" and arg[1] in nzset
