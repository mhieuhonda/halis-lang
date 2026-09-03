"""HLIR optimiser pipeline (Stage 11).

Runs the following passes on an `HLIRModule`:

  1. `constant_fold`     — fold literal arithmetic, string concatenation.
  2. `copy_propagate`    — replace `%t1 = %t0` uses with `%t0`.
  3. `dead_code_elim`    — remove instructions whose result is never used.

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
from typing import Dict, Set
from . import (HLIRModule, HLIRFunction, Instr,
               OP_CONST, OP_BINOP, OP_UNOP, OP_LOAD, OP_STORE,
               OP_CALL, OP_METHOD, OP_BUILTIN, OP_BRANCH, OP_JUMP,
               OP_RETURN, OP_PANIC)


INT64_MAX = 9223372036854775807
INT64_MIN = -9223372036854775808


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
    (e.g. operands are wrong type, or operation would panic)."""
    try:
        if op == "+":
            if isinstance(a, int) and isinstance(b, int):
                r = a + b
                if INT64_MIN <= r <= INT64_MAX:
                    return r, True
            if isinstance(a, (bytes, str)) and isinstance(b, (bytes, str)):
                return a + b, True
        elif op == "-":
            if isinstance(a, int) and isinstance(b, int):
                r = a - b
                if INT64_MIN <= r <= INT64_MAX:
                    return r, True
        elif op == "*":
            if isinstance(a, int) and isinstance(b, int):
                r = a * b
                if INT64_MIN <= r <= INT64_MAX:
                    return r, True
        elif op == "/":
            if isinstance(a, int) and isinstance(b, int) and b != 0:
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
            if isinstance(a, int) and isinstance(b, int) and b != 0:
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
                    if isinstance(a_val, int) and a_val != INT64_MIN:
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
    # Pure operations: const, unop (! and float negation), load,
    # list_new, struct_new, struct_get, map_get, list_len.
    # BUG (deep-scan-5): binop and list_get were previously classified as
    # pure — but checked arithmetic panics on overflow/div-zero and
    # list_get panics on out-of-bounds; DCE must never erase a required
    # panic.
    if ins.op in (OP_CONST, OP_UNOP, OP_LOAD,
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
