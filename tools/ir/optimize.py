"""HLIR optimiser pipeline (Stage 11).

Runs the following passes on an `HLIRModule`:

  1. `constant_fold`     — fold literal arithmetic, string concatenation.
  2. `copy_propagate`    — replace `%t1 = %t0` uses with `%t0`.
  3. `dead_code_elim`    — remove instructions whose result is never used.
  4. `inline_small`      — inline calls to small `pure` functions.
  5. `licm`              — loop-invariant code motion: hoist invariant
                          expressions out of loop bodies.

`-O fast` mode (when `fast=True`) additionally:
  - Skips the integer-overflow check on `add`/`sub`/`mul` when the optimiser
    can prove the result fits in int64. (The original C codegen still emits
    the overflow check; the `-O fast` mode is a *flag* the codegen can read
    to skip it. The optimiser's job here is to *annotate* which `binop`s
    are provably safe.)

The optimiser is conservative: it only folds operations where BOTH operands
are literal constants. It does not perform inter-procedural constant
propagation. The goal is correctness and predictability, not peak
performance — the C compiler's `-O2` is still the primary optimiser.
"""
from __future__ import annotations
from typing import Dict, Set, List, Optional, Tuple
from . import (HLIRModule, HLIRFunction, Block, Instr,
               OP_CONST, OP_BINOP, OP_UNOP, OP_LOAD, OP_STORE,
               OP_CALL, OP_METHOD, OP_BUILTIN, OP_BRANCH, OP_JUMP,
               OP_RETURN, OP_PANIC)


INT64_MAX = 9223372036854775807
INT64_MIN = -9223372036854775808

# Stage 11 release: tunable for the inline_small pass. Functions with at
# most this many instructions AND marked `pure` are candidates for
# inlining at their call sites. Conservative — the goal is to remove
# call overhead for trivial helpers (e.g. `fn add(a: int, b: int) -> int
# pure { return a + b }`) without bloating the IR unreasonably.
INLINE_MAX_INSTRS = 12


def optimize(mod: HLIRModule, fast: bool = False) -> HLIRModule:
    """Run the optimisation pipeline. Returns the (mutated) module."""
    # Pass 1: constant folding.
    for fname, irf in mod.functions.items():
        _constant_fold(irf)
    # Pass 2: copy propagation.
    for fname, irf in mod.functions.items():
        _copy_propagate(irf)
    # Pass 3: dead code elimination.
    for fname, irf in mod.functions.items():
        _dead_code_elim(irf)
    # Pass 4 (Stage 11 release): inline small pure functions. After
    # inlining, re-run constant_fold + copy_propagate + DCE because
    # inlining creates new fold opportunities (e.g. `square(5)` becomes
    # `t = 5 * 5` which can fold to `t = 25`).
    _inline_small(mod)
    for fname, irf in mod.functions.items():
        _constant_fold(irf)
    for fname, irf in mod.functions.items():
        _copy_propagate(irf)
    for fname, irf in mod.functions.items():
        _dead_code_elim(irf)
    # Pass 5 (Stage 11 release): loop-invariant code motion.
    for fname, irf in mod.functions.items():
        _licm(irf)
    # Final cleanup pass: DCE again (LICM may have made the old loop-body
    # instruction dead once its value is hoisted out).
    for fname, irf in mod.functions.items():
        _dead_code_elim(irf)
    # `-O fast` annotations: mark provably-safe binops. We do not actually
    # skip the check at the IR level — we just annotate the instruction so
    # the codegen can read `ins.attrs["safe_overflow"]` and skip the C-level
    # overflow check. (Today the codegen ignores this; it's an opt-in fast
    # path for future use.)
    if fast:
        for fname, irf in mod.functions.items():
            _annotate_safe(irf)
    return mod


# ----------------------------------------------------------------------------
# Pass 1: constant folding
# ----------------------------------------------------------------------------

def _fold_binop(op, a, b):
    """Fold a binary operation on two literal values.
    Returns (folded_value, ok). `ok` is False if the op cannot be folded
    (e.g. operands are wrong type, or operation would panic).

    Deep-scan fix (H1): use `isinstance(x, int) and not isinstance(x, bool)`
    instead of plain `isinstance(x, int)` — in Python, `bool` is a subclass
    of `int`, so `isinstance(True, int)` is True. Without this guard, the
    folder would happily fold `True + 1` to `2`, miscompiling bool-typed
    IR values (which HLS treats as a distinct type from int). The HLS
    checker rejects `bool + int` at the AST level, but the IR can contain
    such mixes after inlining or copy_propagation; folding them silently
    would change observable behaviour.
    """
    try:
        if op == "+":
            if isinstance(a, int) and not isinstance(a, bool) \
                    and isinstance(b, int) and not isinstance(b, bool):
                r = a + b
                if INT64_MIN <= r <= INT64_MAX:
                    return r, True
            if isinstance(a, (bytes, str)) and isinstance(b, (bytes, str)):
                return a + b, True
        elif op == "-":
            if isinstance(a, int) and not isinstance(a, bool) \
                    and isinstance(b, int) and not isinstance(b, bool):
                r = a - b
                if INT64_MIN <= r <= INT64_MAX:
                    return r, True
        elif op == "*":
            if isinstance(a, int) and not isinstance(a, bool) \
                    and isinstance(b, int) and not isinstance(b, bool):
                r = a * b
                if INT64_MIN <= r <= INT64_MAX:
                    return r, True
        elif op == "/":
            if isinstance(a, int) and not isinstance(a, bool) \
                    and isinstance(b, int) and not isinstance(b, bool) and b != 0:
                # BUG-DS4-13: INT64_MIN / -1 raises "integer overflow" at
                # runtime (i64_div / hl_div_i64 check for it), and the
                # mathematical result (+2^63) does not even fit in int64.
                # The old folder returned (2**63, True) — a value no backend
                # can represent. Do not fold; let the runtime panic.
                if a == INT64_MIN and b == -1:
                    return None, False
                q = abs(a) // abs(b)
                return (q if (a < 0) == (b < 0) else -q), True
        elif op == "%":
            if isinstance(a, int) and not isinstance(a, bool) \
                    and isinstance(b, int) and not isinstance(b, bool) and b != 0:
                # BUG-DS4-13: INT64_MIN % -1 panics at runtime (i64_mod /
                # hl_mod_i64), even though the mathematical result is 0.
                # Folding it to 0 would REMOVE the panic and change program
                # behaviour. Do not fold.
                if a == INT64_MIN and b == -1:
                    return None, False
                r = abs(a) % abs(b)
                return (r if a >= 0 else -r), True
        elif op == "==":
            return (a == b), True
        elif op == "!=":
            return (a != b), True
        elif op == "<":
            return (a < b), True
        elif op == "<=":
            return (a <= b), True
        elif op == ">":
            return (a > b), True
        elif op == ">=":
            return (a >= b), True
        elif op == "&&":
            if isinstance(a, bool) and isinstance(b, bool):
                return (a and b), True
        elif op == "||":
            if isinstance(a, bool) and isinstance(b, bool):
                return (a or b), True
    except Exception:
        pass
    return None, False


def _constant_fold(irf: HLIRFunction):
    """Replace literal binops with a const instruction.

    Also tracks constants propagated through `OP_LOAD` (which is what `let`
    bindings become). When `v_x = load %t1` and `t1 = const K`, we record
    `v_x -> K` so subsequent binops on `v_x` can be folded.
    """
    for block in irf.blocks:
        # Build a map from SSA name -> literal value (if known).
        consts: Dict[str, object] = {}
        new_instrs = []
        for ins in block.instrs:
            if ins.op == OP_CONST and ins.args and ins.args[0][0] == "lit":
                consts[ins.dest] = ins.args[0][1]
                new_instrs.append(ins)
                continue
            # OP_LOAD is a copy: dest = var src. If src is a known constant,
            # record dest as the same constant (don't replace the instruction,
            # because copy_propagate will handle that — but recording the
            # constant here lets us fold downstream binops).
            if ins.op == OP_LOAD and len(ins.args) == 1 and ins.args[0][0] == "var":
                src = ins.args[0][1]
                if src in consts:
                    consts[ins.dest] = consts[src]
                new_instrs.append(ins)
                continue
            # OP_STORE: dest = var src, name. The `name` records which source
            # binding is being overwritten. Conservatively forget the dest's
            # constant value (since the new value might not be a literal).
            if ins.op == OP_STORE and len(ins.args) >= 2 and ins.args[1][0] == "name":
                # The binding name (e.g. "x") is in args[1][1]; the new value
                # is in args[0]. If args[0] is a known const, record it;
                # otherwise drop the entry.
                src_arg = ins.args[0]
                binding = "v_" + ins.args[1][1]
                if src_arg[0] == "var" and src_arg[1] in consts:
                    consts[binding] = consts[src_arg[1]]
                elif src_arg[0] == "lit":
                    consts[binding] = src_arg[1]
                else:
                    consts.pop(binding, None)
                new_instrs.append(ins)
                continue
            if ins.op == OP_BINOP and len(ins.args) >= 3:
                op_arg, a_arg, b_arg = ins.args[0], ins.args[1], ins.args[2]
                if op_arg[0] != "op":
                    new_instrs.append(ins)
                    continue
                a_val = _resolve(a_arg, consts)
                b_val = _resolve(b_arg, consts)
                if a_val is not None and b_val is not None:
                    folded, ok = _fold_binop(op_arg[1], a_val, b_val)
                    if ok:
                        new_ins = Instr(dest=ins.dest, op=OP_CONST,
                                        args=[("lit", folded)], line=ins.line)
                        new_instrs.append(new_ins)
                        consts[ins.dest] = folded
                        continue
            if ins.op == OP_UNOP and len(ins.args) >= 2:
                op_arg, a_arg = ins.args[0], ins.args[1]
                if op_arg[0] == "op" and op_arg[1] == "-":
                    a_val = _resolve(a_arg, consts)
                    # Deep-scan fix (H1): exclude bool — `isinstance(True, int)`
                    # is True in Python, but HLS treats bool as a distinct
                    # type. Folding `-True` would miscompile.
                    if isinstance(a_val, int) and not isinstance(a_val, bool) \
                            and a_val != INT64_MIN:
                        folded = -a_val
                        new_ins = Instr(dest=ins.dest, op=OP_CONST,
                                        args=[("lit", folded)], line=ins.line)
                        new_instrs.append(new_ins)
                        consts[ins.dest] = folded
                        continue
                # BUG-SC-IR-14 fix: fold the `!` (logical not) operator on
                # boolean literals. Previously only `-` was folded; `!true`
                # and `!false` stayed as runtime OP_UNOP instructions.
                if op_arg[0] == "op" and op_arg[1] == "!":
                    a_val = _resolve(a_arg, consts)
                    if isinstance(a_val, bool):
                        folded = not a_val
                        new_ins = Instr(dest=ins.dest, op=OP_CONST,
                                        args=[("lit", folded)], line=ins.line)
                        new_instrs.append(new_ins)
                        consts[ins.dest] = folded
                        continue
            new_instrs.append(ins)
        block.instrs = new_instrs


def _resolve(arg, consts):
    """Resolve an instruction argument to a literal value (if known)."""
    if arg[0] == "lit":
        return arg[1]
    if arg[0] == "var":
        return consts.get(arg[1])
    return None


# ----------------------------------------------------------------------------
# Pass 2: copy propagation
# ----------------------------------------------------------------------------

def _copy_propagate(irf: HLIRFunction):
    """Replace `%t1 = %t0` uses with `%t0`."""
    for block in irf.blocks:
        # Map: SSA name -> canonical SSA name.
        # BUG (deep-scan-5): this only recorded t1 = t0 copies, but the
        # builder emits OP_LOAD with v_* dests — the pass was a no-op.
        # Record v_x = t_k copies (temps are never store targets), and
        # invalidate the mapping when v_x is reassigned.
        canon: Dict[str, str] = {}
        for ins in block.instrs:
            # Rewrite args first.
            ins.args = [_rewrite_arg(a, canon) for a in ins.args]
            if ins.op == OP_STORE and len(ins.args) >= 2 and ins.args[1][0] == "name":
                # v_x is reassigned — later uses must see the new value.
                canon.pop("v_" + ins.args[1][1], None)
                continue
            # If this is a copy `v_x = t_k`, record the mapping.
            if ins.op == OP_LOAD and len(ins.args) == 1 and ins.args[0][0] == "var":
                src = ins.args[0][1]
                if ins.dest is not None and src.startswith("t"):
                    canon[ins.dest] = src
        if block.terminator:
            block.terminator.args = [_rewrite_arg(a, canon)
                                     for a in block.terminator.args]


def _rewrite_arg(arg, canon):
    if arg[0] == "var" and arg[1] in canon:
        return ("var", canon[arg[1]])
    return arg


# ----------------------------------------------------------------------------
# Pass 3: dead code elimination
# ----------------------------------------------------------------------------

def _dead_code_elim(irf: HLIRFunction):
    """Remove instructions whose result is never used.

    BUG-SC-IR-2 fix: previously `used` was computed PER BLOCK, so any SSA
    name defined in block A but consumed in block B (the normal case for
    `let` bindings flowing into `if`/`while`/`for` branches) was considered
    unused in A and was deleted — corrupting the IR. Now we compute `used`
    across ALL blocks of the function before deciding what to drop.
    """
    # Collect all referenced SSA names across the ENTIRE function.
    used: Set[str] = set()
    for block in irf.blocks:
        for ins in block.instrs:
            for a in ins.args:
                if a[0] == "var":
                    used.add(a[1])
        if block.terminator:
            for a in block.terminator.args:
                if a[0] == "var":
                    used.add(a[1])
    # Now drop dead instructions per block (using the function-wide `used` set).
    for block in irf.blocks:
        new_instrs = []
        for ins in block.instrs:
            if _has_side_effects(ins):
                new_instrs.append(ins)
                continue
            if ins.dest is not None and ins.dest in used:
                new_instrs.append(ins)
                continue
            # Otherwise, dead — drop it.
        block.instrs = new_instrs


def _has_side_effects(ins: Instr) -> bool:
    """An instruction has side effects if it can affect external state."""
    # Pure operations: const, load, list_new, struct_new, struct_get,
    # map_get, list_len, and OP_UNOP ONLY for `!` (boolean NOT, which
    # can never panic).
    # BUG (deep-scan-5): binop and list_get were previously classified as
    # pure — but checked arithmetic panics on overflow/div-zero and
    # list_get panics on out-of-bounds; DCE must never erase a required
    # panic.
    # Deep-scan-7 fix: OP_UNOP was classified as pure for ALL unary
    # operations, but `-` on int can panic on INT64_MIN (signed
    # overflow). DCE could erase a `let x: int = -y` whose only
    # consumer is the side-effectful panic — losing the panic. Now
    # we conservatively treat `-` as impure (boolean `!` is still
    # pure since booleans can't overflow).
    if ins.op == OP_UNOP:
        # The op is stored as the first arg's value.
        if ins.args and ins.args[0][0] == "op" and ins.args[0][1] == "!":
            return False  # boolean NOT — pure
        # Any other unary op (including `-`) may panic.
        return True
    if ins.op in (OP_CONST, OP_LOAD,
                  "list_new", "struct_new", "struct_get",
                  "map_get", "list_len"):
        return False
    # Impure: store, call (might be impure), method (might be impure),
    # builtin (depends), panic, branch, jump, return.
    if ins.op in (OP_STORE, OP_CALL, OP_METHOD, OP_BUILTIN,
                  OP_PANIC, OP_BRANCH, OP_JUMP, OP_RETURN):
        return True
    # Conservative: treat unknown ops as side-effectful.
    return True


# ----------------------------------------------------------------------------
# Pass 4 (-O fast): annotate provably-safe arithmetic
# ----------------------------------------------------------------------------

def _annotate_safe(irf: HLIRFunction):
    """Mark binops whose overflow is provably impossible.

    For a `t = a + b` where both a and b are literals, we already folded it
    in pass 1. For non-literal operands, we conservatively cannot prove
    safety. The annotation is stored as `ins.attrs["safe_overflow"] = True`
    so the codegen can opt to skip the C-level overflow check.

    The only safe pattern we annotate today:
      `t = a * 0`  -> always 0 (no overflow possible)
      `t = a + 0` / `t = a - 0`  -> no overflow possible
    These are trivial but they exercise the fast-path codegen plumbing.
    """
    for block in irf.blocks:
        for ins in block.instrs:
            if ins.op != OP_BINOP or len(ins.args) < 3:
                continue
            op_arg, a_arg, b_arg = ins.args[0], ins.args[1], ins.args[2]
            if op_arg[0] != "op":
                continue
            op = op_arg[1]
            # Try to identify a literal-0 operand.
            a_is_zero = a_arg[0] == "lit" and a_arg[1] == 0
            b_is_zero = b_arg[0] == "lit" and b_arg[1] == 0
            # BUG-DS4-14: `0 - x` is NOT overflow-safe — 0 - INT64_MIN
            # overflows to +2^63. Only `x - 0` (and `x + 0`, `x * 0`) are
            # genuinely safe. The old code annotated `(0 - x)` as safe.
            if op == "+" and (a_is_zero or b_is_zero):
                # BUG-SC-IR-5 fix: Instr is a @dataclass with attrs: Optional[Dict]
                # defaulting to None. `hasattr(ins, "attrs")` is always True
                # (the attribute exists), so the guard never initialized it,
                # causing `ins.attrs["safe_overflow"] = True` to crash with
                # TypeError when attrs is None. Check for None instead.
                if ins.attrs is None:
                    ins.attrs = {}
                ins.attrs["safe_overflow"] = True
            elif op == "-" and b_is_zero:
                if ins.attrs is None:
                    ins.attrs = {}
                ins.attrs["safe_overflow"] = True
            # Stage 11 release: also annotate multiplications by 0 or 1
            # (the result is provably safe — 0 or the other operand, both
            # of which fit in int64 since the operand already did).
            elif op == "*":
                a_is_one = a_arg[0] == "lit" and a_arg[1] == 1
                b_is_one = b_arg[0] == "lit" and b_arg[1] == 1
                if a_is_zero or b_is_zero or a_is_one or b_is_one:
                    if ins.attrs is None:
                        ins.attrs = {}
                    ins.attrs["safe_overflow"] = True


# ----------------------------------------------------------------------------
# Pass 4 (Stage 11 release): inline small `pure` functions
# ----------------------------------------------------------------------------

def _inline_small(mod: HLIRModule):
    """Inline calls to small `pure` functions at their call sites.

    A function is inlinable if ALL of:
      - It is declared `pure` (no effects).
      - Its body has at most INLINE_MAX_INSTRS instructions (excluding the
        implicit terminator).
      - It has exactly one block (no control flow). This keeps the inliner
        trivial: it just clones the body into the caller.
      - It is not recursive (directly or transitively). We approximate this
        by skipping any function whose body contains a self-call (direct
        recursion only — transitive is too expensive to detect here, and
        is also rare for `pure` helpers).

    For each call site `%r = call @f(%a, %b)` where `f` is inlinable:
      - Allocate fresh SSA names for every instruction in `f`'s body.
      - Replace `f`'s parameter SSA names with the call's argument SSA names
        (or copy them via OP_LOAD if the argument is a literal).
      - Replace `%r` (the call's dest) with the inlined `return`'s operand
        (the last instruction's dest after rename).

    The pass is conservative: it does not handle control flow, recursion,
    or `?`/match expressions inside the inlined body (those would already
    have raised HLError during IR construction).

    Side effects: the call instruction is REMOVED; the inlined instructions
    are INSERTED in its place. Subsequent DCE removes any leftover
    instructions whose dest is now unused.
    """
    # Step 1: identify inlinable functions.
    inlinable: Dict[str, HLIRFunction] = {}
    for fname, irf in mod.functions.items():
        # Must be pure (no effects).
        if irf.effects:
            continue
        # Must have exactly one block (no control flow).
        if len(irf.blocks) != 1:
            continue
        block = irf.blocks[0]
        # Count instructions (excluding the terminator).
        n_instrs = len(block.instrs)
        if n_instrs > INLINE_MAX_INSTRS:
            continue
        # Skip recursive functions (direct self-call).
        is_recursive = False
        for ins in block.instrs:
            if ins.op == OP_CALL and ins.args and ins.args[0][0] == "fname" \
                    and ins.args[0][1] == fname:
                is_recursive = True
                break
        if is_recursive:
            continue
        # Skip functions with no terminator or non-return terminator
        # (defensive — IRBuilder always sets a terminator).
        if block.terminator is None or block.terminator.op != OP_RETURN:
            continue
        inlinable[fname] = irf
    if not inlinable:
        return  # nothing to inline

    # Step 2: walk every function body and rewrite call sites.
    for caller_name, caller in mod.functions.items():
        # Renumbering: we need fresh SSA names that don't collide with
        # existing names in this function. Use a per-caller counter that
        # starts above the highest existing temp id.
        max_t = 0
        for blk in caller.blocks:
            for ins in blk.instrs:
                if ins.dest and ins.dest.startswith("t"):
                    try:
                        v = int(ins.dest[1:])
                        if v > max_t:
                            max_t = v
                    except ValueError:
                        pass
        counter = [max_t + 1]

        def fresh():
            n = counter[0]
            counter[0] += 1
            return "t%d" % n

        for blk in caller.blocks:
            new_instrs: List[Instr] = []
            for ins in blk.instrs:
                if ins.op != OP_CALL or not ins.args or ins.args[0][0] != "fname":
                    new_instrs.append(ins)
                    continue
                target = ins.args[0][1]
                if target not in inlinable:
                    new_instrs.append(ins)
                    continue
                callee = inlinable[target]
                # The callee is a single-block pure function ending in OP_RETURN.
                # Map callee param SSA names -> caller argument SSA names.
                # callee.params is List[(name, type)] — the SSA name in the IR
                # is "v_<name>" (per IRBuilder._build_fn).
                rename: Dict[str, Tuple] = {}
                # ins.args[1:] are the call's argument SSA references.
                call_args = ins.args[1:]
                for (pname, _ptype), arg in zip(callee.params, call_args):
                    rename["v_" + pname] = arg
                # Rename every dest in the callee body to a fresh name, and
                # rewrite every var reference via the rename map (chained:
                # if v_a was renamed and v_b's source uses v_a, the rewrite
                # follows the chain).
                callee_block = callee.blocks[0]
                last_dest: Optional[str] = None
                for cin in callee_block.instrs:
                    # Allocate a fresh dest for this instruction.
                    new_dest = fresh() if cin.dest else None
                    if cin.dest is not None:
                        rename[cin.dest] = ("var", new_dest)
                    # Rewrite args: each ("var", name) -> rename[name] if present.
                    new_args = []
                    for a in cin.args:
                        if a[0] == "var" and a[1] in rename:
                            new_args.append(rename[a[1]])
                        else:
                            new_args.append(a)
                    new_instrs.append(Instr(
                        dest=new_dest, op=cin.op, args=new_args,
                        line=cin.line, attrs=dict(cin.attrs) if cin.attrs else None,
                    ))
                    if new_dest is not None:
                        last_dest = new_dest
                # The callee's terminator is OP_RETURN with the returned
                # value. The CALL instruction's dest was %r — replace it
                # with the inlined return value via a copy.
                # We rewrite ins.dest references in subsequent instructions
                # via OP_LOAD (a copy). For simplicity, allocate a fresh
                # copy %r = load %last_dest.
                ret_val = None
                if callee_block.terminator and callee_block.terminator.args:
                    ret_val = callee_block.terminator.args[0]
                if ret_val is not None and ret_val[0] == "var":
                    # Resolve through rename.
                    if ret_val[1] in rename:
                        ret_val = rename[ret_val[1]]
                    # Emit a copy: dest = load ret_val.
                    new_instrs.append(Instr(
                        dest=ins.dest, op=OP_LOAD, args=[ret_val], line=ins.line,
                    ))
                else:
                    # Void return or no return value: emit a const None
                    # (will be DCE'd if the dest is unused).
                    new_instrs.append(Instr(
                        dest=ins.dest, op=OP_CONST, args=[("lit", None)],
                        line=ins.line,
                    ))
            blk.instrs = new_instrs


# ----------------------------------------------------------------------------
# Pass 5 (Stage 11 release): loop-invariant code motion (LICM)
# ----------------------------------------------------------------------------

def _licm(irf: HLIRFunction):
    """Hoist loop-invariant instructions out of loop bodies.

    A loop is identified by the Block control-flow structure: a back-edge
    from a body block to a cond block (named "*_cond" by IRBuilder).

    For each instruction inside the loop body that:
      - Is "pure" (no side effects: const, unop, load, list_len,
        struct_get, map_get, list_new, struct_new).
      - All its operand SSA names are defined OUTSIDE the loop (in a block
        that is not part of the loop), OR are themselves hoistable.
    we MOVE that instruction from the loop body to the block that
    immediately precedes the loop's cond block (the preheader).

    This is conservative — we do not perform full dominator analysis or
    SSA renaming. The pass only hoists when the SSA name's ONLY definition
    is outside the loop, which is the common case for `let c = a + b`
    inside `while ... { ... }` where `a` and `b` are not mutated by the
    loop body.

    Deep-scan fix (O5): we ONLY hoist instructions from the loop's
    IMMEDIATE body block (the one whose terminator is the back-edge to
    cond). Nested control-flow inside the loop (e.g. an `if` with its
    own then/else/endif blocks) is NOT considered for hoisting — those
    blocks may not execute on every iteration, so hoisting their
    instructions out would be unsafe.
    """
    # First, identify loops. A loop is a sequence of blocks where the
    # last block jumps back to the first (the cond). The cond block's
    # name ends with "_cond" (set by IRBuilder._new_block with prefix
    # "while_cond" or "for_cond").
    for caller_block_idx in range(len(irf.blocks)):
        cond = irf.blocks[caller_block_idx]
        if not cond.name.endswith("_cond"):
            continue
        # Find the body of this loop: blocks from cond_index+1 until the
        # block whose terminator jumps back to cond.
        loop_blocks: Set[str] = {cond.name}
        end_idx = caller_block_idx
        for j in range(caller_block_idx + 1, len(irf.blocks)):
            blk = irf.blocks[j]
            loop_blocks.add(blk.name)
            end_idx = j
            if blk.terminator and blk.terminator.op == OP_JUMP \
                    and blk.terminator.args \
                    and blk.terminator.args[0][0] == "label" \
                    and blk.terminator.args[0][1] == cond.name:
                break
        # The preheader is the block immediately before the cond block.
        preheader = irf.blocks[caller_block_idx - 1] if caller_block_idx > 0 else None
        if preheader is None:
            continue  # no preheader available (shouldn't happen for real loops)

        # Collect SSA names defined INSIDE the loop. Any name in this set
        # is NOT loop-invariant by definition.
        loop_defined: Set[str] = set()
        for j in range(caller_block_idx, end_idx + 1):
            blk = irf.blocks[j]
            for ins in blk.instrs:
                if ins.dest:
                    loop_defined.add(ins.dest)

        # Deep-scan fix (O5): ONLY hoist from the loop's IMMEDIATE body
        # block (the last one in the loop, whose terminator is the back-
        # edge to cond). Hoisting from nested if/else/endif blocks would
        # be unsafe because those blocks may not execute on every iter.
        # The immediate body block is irf.blocks[end_idx].
        body = irf.blocks[end_idx]
        hoisted: List[Instr] = []
        remaining: List[Instr] = []
        for ins in body.instrs:
            if not _is_hoistable(ins, loop_defined):
                remaining.append(ins)
                continue
            # All operands are defined outside the loop. Hoist.
            hoisted.append(ins)
        if hoisted:
            # Append the hoisted instructions to the preheader. The
            # preheader's terminator is a separate field, so appending
            # to .instrs is safe — it doesn't displace the terminator.
            preheader.instrs.extend(hoisted)
            body.instrs = remaining


def _is_hoistable(ins: Instr, loop_defined: Set[str]) -> bool:
    """True if `ins` is a pure op whose operands are all defined outside
    the loop (so the result is loop-invariant)."""
    # Only hoist pure operations.
    PURE_OPS = {OP_CONST, OP_UNOP, OP_LOAD, "list_new", "struct_new",
                "struct_get", "map_get", "list_len"}
    # OP_BINOP is pure in the sense of "doesn't write memory", but it can
    # PANIC on overflow/div-zero — hoisting it out of a loop that never
    # executes would panic on a program that should have run cleanly.
    # So we DO NOT hoist OP_BINOP (matches the DCE classification).
    if ins.op not in PURE_OPS:
        return False
    # OP_STORE inside a loop is never invariant (it mutates state).
    # (Deep-scan note H7: this check is dead code — OP_STORE is not in
    # PURE_OPS, so the earlier guard already returned False. Kept for
    # clarity / defensive programming.)
    if ins.op == OP_STORE:
        return False
    # All var-typed operands must be defined outside the loop.
    for a in ins.args:
        if a[0] == "var" and a[1] in loop_defined:
            return False
    return True
