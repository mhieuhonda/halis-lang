"""HLIR — Hieu Louis Intermediate Representation.

Stage 11 (v0.9.0-alpha): a mid-level IR with SSA-style single-assignment
semantics, used as the optimisation substrate for HLS programs. The IR is
produced from the existing AST (post-check) and consumed by an optimiser
pipeline before code generation.

Design:
  - Each function body becomes a `Block` of `Instr` instructions.
  - Each `Instr` has an optional `dest` (SSA name), an `op`, and a list of
    operand references (literal values or SSA names).
  - Control flow (if / while / for / break / continue / return) is lowered
    to explicit `Block`s connected by `branch` instructions. This is a
    *light* SSA form: we do not perform full phi-node construction because
    HLS already disallows shadowing and uninitialised variables — every
    binding has exactly one definition point at the source level, so we
    get "implicit SSA" for free. The IR's job is to be the optimisation
    substrate, not to be a textbook SSA construction.

The IR is consumed by `optimize.py`, which runs the following passes:
  1. `constant_fold`    — fold literal arithmetic / string concatenation.
  2. `copy_propagate`   — replace `t1 = t0` uses with `t0`.
  3. `dead_code_elim`   — remove instructions whose result is never used.
  4. `inline_small`     — (planned) inline calls to small `pure` functions.

`-O fast` mode additionally:
  - Skips the integer-overflow check on `add`/`sub`/`mul` when the optimiser
    can prove (via range analysis) that the result fits in int64.
  - Skips the bounds check on `list[i]` when the optimiser can prove `i` is
    a literal `0` or `len-1` of a literal list.

The IR is *only* a Stage-0 optimisation pass today. The self-hosted `hlc.hls`
continues to emit C directly from the AST (its codegen is already optimised
by the C compiler's `-O2`). Dogfooding the IR into `hlc.hls` itself is the
Stage 11 release target.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, List, Dict, Optional, Tuple


@dataclass
class Instr:
    """A single IR instruction.

    `dest`  — SSA name (e.g. `t1`), or None for instructions with side
              effects (calls to impure functions, stores, panics).
    `op`    — operation name (see `OP_*` constants below).
    `args`  — list of operands. Each operand is one of:
                ("lit", value)      — a literal Python value
                ("var", ssa_name)   — reference to another instruction's dest
                ("call", fname, [args]) — a function call
    `line`  — source line for position info in panics.
    `attrs` — optional annotations (e.g. `{"safe_overflow": True}` for
              `-O fast` mode).
    """
    dest: Optional[str]
    op: str
    args: List[Any]
    line: int = 0
    attrs: Optional[Dict[str, Any]] = None


@dataclass
class Block:
    """A linear sequence of instructions ending in a terminator."""
    name: str
    instrs: List[Instr] = field(default_factory=list)
    terminator: Optional[Instr] = None  # branch / return / panic / halt


@dataclass
class HLIRFunction:
    name: str
    params: List[Tuple[str, str]]  # (name, type)
    ret: str
    effects: set
    blocks: List[Block] = field(default_factory=list)


@dataclass
class HLIRModule:
    functions: Dict[str, HLIRFunction] = field(default_factory=dict)


# Op codes
OP_CONST        = "const"
OP_BINOP        = "binop"
OP_UNOP         = "unop"
OP_CALL         = "call"
OP_METHOD       = "method"        # fieldcall on a struct value
OP_BUILTIN      = "builtin"
OP_LOAD         = "load"          # dest = var (used for binding a let-target)
OP_STORE        = "store"         # var = dest  (assignment to existing binding)
OP_LIST_NEW     = "list_new"
OP_LIST_GET     = "list_get"
OP_LIST_SET     = "list_set"
OP_LIST_LEN     = "list_len"
OP_MAP_NEW      = "map_new"
OP_MAP_GET      = "map_get"
OP_MAP_SET      = "map_set"
OP_STRUCT_NEW   = "struct_new"
OP_STRUCT_GET   = "struct_get"
OP_STRUCT_SET   = "struct_set"
OP_BRANCH       = "branch"
OP_JUMP         = "jump"
OP_RETURN       = "return"
OP_PANIC        = "panic"
OP_MATCH        = "match"
OP_QMARK        = "qmark"


# ----------------------------------------------------------------------------
# Builder: AST -> HLIR.  This is a *lowering* pass; it does not optimise.
# ----------------------------------------------------------------------------

class IRBuilder:
    """Lowers a checked HLS program (AST) into an HLIRModule.

    The lowering is intentionally simple: each `let` binding becomes a `dest`
    instruction; each expression becomes either a `const`, `binop`, `unop`,
    `call`, `method`, or `builtin` instruction; control flow is lowered to
    blocks + branches.

    Because HLS already enforces single-assignment of `let` bindings, the
    resulting IR has SSA-like properties without explicit phi-node
    construction.
    """

    def __init__(self, program):
        self.program = program
        self.mod = HLIRModule()
        self._tmp = 0
        # Stack of (continue_label, break_label) for loops.
        self._loop_stack = []

    def _fresh(self):
        self._tmp += 1
        return "t%d" % self._tmp

    def build(self):
        for fname, fn in self.program["fns"].items():
            self.mod.functions[fname] = self._build_fn(fname, fn)
        return self.mod

    def _build_fn(self, fname, fn):
        params = [(p[0], p[1]) for p in fn["params"]]
        irf = HLIRFunction(
            name=fname,
            params=params,
            ret=fn["ret"],
            effects=set(fn["effects"]),
        )
        entry = Block(name="entry")
        irf.blocks.append(entry)
        self._current = entry
        self._block_counter = 0
        for stmt in fn["body"]:
            self._lower_stmt(stmt, irf)
        # If the last instruction is not a terminator, add an implicit
        # `return void` (for void functions) or `panic` (for non-void).
        if not self._current.terminator:
            if irf.ret == "void":
                self._current.terminator = Instr(None, OP_RETURN, [None], 0)
            else:
                self._current.terminator = Instr(None, OP_PANIC,
                                                [("lit", "missing return")], 0)
        return irf

    def _new_block(self, irf, prefix="bb"):
        self._block_counter += 1
        b = Block(name="%s%d" % (prefix, self._block_counter))
        irf.blocks.append(b)
        return b

    def _emit(self, op, args, line=0, dest=None):
        if dest is None:
            dest = self._fresh()
        instr = Instr(dest=dest, op=op, args=args, line=line)
        self._current.instrs.append(instr)
        return dest

    # ---------- statement lowering ----------
    def _lower_stmt(self, stmt, irf):
        k = stmt["k"]
        if k == "let":
            val = self._lower_expr(stmt["value"], irf)
            dest = "v_%s" % stmt["name"]
            self._emit(OP_LOAD, [("var", val)], stmt.get("line", 0), dest=dest)
        elif k == "assign":
            target = stmt["target"]
            val = self._lower_expr(stmt["value"], irf)
            if target["k"] == "ident":
                dest = "v_%s" % target["name"]
                self._emit(OP_STORE, [("var", val), ("name", target["name"])],
                           stmt.get("line", 0), dest=dest)
            elif target["k"] == "field":
                recv = self._lower_expr(target["target"], irf)
                self._emit(OP_STRUCT_SET,
                           [("var", recv), ("name", target["name"]),
                            ("var", val)],
                           stmt.get("line", 0))
            elif target["k"] == "index":
                lst = self._lower_expr(target["target"], irf)
                idx = self._lower_expr(target["idx"], irf)
                self._emit(OP_LIST_SET,
                           [("var", lst), ("var", idx), ("var", val)],
                           stmt.get("line", 0))
        elif k == "return":
            val = None
            if stmt.get("value") is not None:
                val = ("var", self._lower_expr(stmt["value"], irf))
            self._current.terminator = Instr(None, OP_RETURN, [val],
                                             stmt.get("line", 0))
        elif k == "if":
            cond = self._lower_expr(stmt["cond"], irf)
            then_block = self._new_block(irf, "then")
            else_block = self._new_block(irf, "else")
            end_block = self._new_block(irf, "endif")
            self._current.terminator = Instr(None, OP_BRANCH,
                                            [("var", cond),
                                             ("label", then_block.name),
                                             ("label", else_block.name)],
                                            stmt.get("line", 0))
            self._current = then_block
            for s in stmt["then"]:
                self._lower_stmt(s, irf)
            if not self._current.terminator:
                self._current.terminator = Instr(None, OP_JUMP,
                                                 [("label", end_block.name)], 0)
            self._current = else_block
            els = stmt.get("els")
            if els:
                for s in els:
                    self._lower_stmt(s, irf)
            if not self._current.terminator:
                self._current.terminator = Instr(None, OP_JUMP,
                                                 [("label", end_block.name)], 0)
            self._current = end_block
        elif k == "while":
            cond_block = self._new_block(irf, "while_cond")
            body_block = self._new_block(irf, "while_body")
            end_block = self._new_block(irf, "while_end")
            self._current.terminator = Instr(None, OP_JUMP,
                                             [("label", cond_block.name)], 0)
            self._current = cond_block
            cond = self._lower_expr(stmt["cond"], irf)
            self._current.terminator = Instr(None, OP_BRANCH,
                                             [("var", cond),
                                              ("label", body_block.name),
                                              ("label", end_block.name)],
                                             stmt.get("line", 0))
            self._current = body_block
            self._loop_stack.append((cond_block.name, end_block.name))
            for s in stmt["body"]:
                self._lower_stmt(s, irf)
            self._loop_stack.pop()
            if not self._current.terminator:
                self._current.terminator = Instr(None, OP_JUMP,
                                                 [("label", cond_block.name)], 0)
            self._current = end_block
        elif k == "for":
            # Lower `for v: T in iter { body }` as:
            #   t_iter = <iter>
            #   t_len = list_len(t_iter)
            #   t_i = 0
            #   while t_i < t_len:
            #     v = list_get(t_iter, t_i)
            #     body
            #     t_i = t_i + 1
            iter_name = self._lower_expr(stmt["iter"], irf)
            len_name = self._emit(OP_LIST_LEN, [("var", iter_name)],
                                  stmt.get("line", 0))
            i_name = "v_%s__i" % stmt["var"]
            self._emit(OP_CONST, [("lit", 0)], stmt.get("line", 0), dest=i_name)
            cond_block = self._new_block(irf, "for_cond")
            body_block = self._new_block(irf, "for_body")
            end_block = self._new_block(irf, "for_end")
            self._current.terminator = Instr(None, OP_JUMP,
                                             [("label", cond_block.name)], 0)
            self._current = cond_block
            cond_tmp = self._emit(OP_BINOP,
                                  [("op", "<"), ("var", i_name),
                                   ("var", len_name)],
                                  stmt.get("line", 0))
            self._current.terminator = Instr(None, OP_BRANCH,
                                             [("var", cond_tmp),
                                              ("label", body_block.name),
                                              ("label", end_block.name)],
                                             stmt.get("line", 0))
            self._current = body_block
            v_name = "v_%s" % stmt["var"]
            self._emit(OP_LIST_GET, [("var", iter_name), ("var", i_name)],
                       stmt.get("line", 0), dest=v_name)
            self._loop_stack.append((cond_block.name, end_block.name))
            for s in stmt["body"]:
                self._lower_stmt(s, irf)
            self._loop_stack.pop()
            # Increment loop counter.
            inc = self._emit(OP_BINOP,
                             [("op", "+"), ("var", i_name), ("lit", 1)],
                             stmt.get("line", 0))
            # BUG-SC-IR-1 fix: the store target must be the binding name
            # WITHOUT the "v_" prefix (the constant-folder / store handler
            # prepends "v_"). Previously this used `i_name + "__i"` which
            # produced `v_x__i__i` (double-prefixed), so the loop counter
            # was never updated — representing an infinite loop in the IR.
            self._emit(OP_STORE, [("var", inc), ("name", stmt["var"] + "__i")],
                       stmt.get("line", 0), dest=i_name)
            if not self._current.terminator:
                self._current.terminator = Instr(None, OP_JUMP,
                                                 [("label", cond_block.name)], 0)
            self._current = end_block
        elif k == "break":
            if not self._loop_stack:
                # The checker already rejects this; defensively skip.
                return
            self._current.terminator = Instr(None, OP_JUMP,
                                             [("label", self._loop_stack[-1][1])],
                                             stmt.get("line", 0))
        elif k == "continue":
            if not self._loop_stack:
                return
            self._current.terminator = Instr(None, OP_JUMP,
                                             [("label", self._loop_stack[-1][0])],
                                             stmt.get("line", 0))
        elif k == "expr":
            self._lower_expr(stmt["e"], irf)
        else:
            # Unknown statement kind — emit a comment marker so the
            # optimiser can drop it after DCE.
            self._emit("comment", [("text", k)], stmt.get("line", 0))

    # ---------- expression lowering ----------
    def _lower_expr(self, e, irf):
        k = e["k"]
        if k == "int":
            return self._emit(OP_CONST, [("lit", e["v"])], e.get("line", 0))
        if k == "float":
            return self._emit(OP_CONST, [("lit", e["v"])], e.get("line", 0))
        if k == "bool":
            return self._emit(OP_CONST, [("lit", e["v"])], e.get("line", 0))
        if k == "str":
            return self._emit(OP_CONST, [("lit", e["v"])], e.get("line", 0))
        if k == "ident":
            return "v_%s" % e["name"]
        if k == "bin":
            a = self._lower_expr(e["l"], irf)
            b = self._lower_expr(e["r"], irf)
            return self._emit(OP_BINOP,
                             [("op", e["op"]), ("var", a), ("var", b)],
                             e.get("line", 0))
        if k == "un":
            a = self._lower_expr(e["e"], irf)
            return self._emit(OP_UNOP, [("op", e["op"]), ("var", a)],
                              e.get("line", 0))
        if k == "call":
            args = [self._lower_expr(a, irf) for a in e["args"]]
            arg_refs = [("var", a) for a in args]
            return self._emit(OP_CALL,
                              [("fname", e["name"])] + arg_refs,
                              e.get("line", 0))
        if k == "fieldcall" or k == "method":
            recv = self._lower_expr(e["target"], irf)
            args = [self._lower_expr(a, irf) for a in e["args"]]
            return self._emit(OP_METHOD,
                              [("name", e["name"]), ("var", recv)]
                              + [("var", a) for a in args],
                              e.get("line", 0))
        if k == "field":
            recv = self._lower_expr(e["target"], irf)
            return self._emit(OP_STRUCT_GET,
                              [("var", recv), ("name", e["name"])],
                              e.get("line", 0))
        if k == "index":
            lst = self._lower_expr(e["target"], irf)
            idx = self._lower_expr(e["idx"], irf)
            return self._emit(OP_LIST_GET, [("var", lst), ("var", idx)],
                              e.get("line", 0))
        if k == "qmark":
            # The `?` operator propagates an Err variant out of the
            # enclosing function. Lower as a match + return.
            v = self._lower_expr(e["e"], irf)
            return self._emit(OP_QMARK, [("var", v)], e.get("line", 0))
        if k == "match":
            scrut = self._lower_expr(e["scrut"], irf)
            return self._emit(OP_MATCH,
                              [("var", scrut)] +
                              [("arm", {"pattern": arm["pattern"],
                                        "body": arm["body"]})
                               for arm in e["arms"]],
                              e.get("line", 0))
        if k == "listlit":
            elems = [self._lower_expr(a, irf) for a in e["items"]]
            return self._emit(OP_LIST_NEW, [("var", a) for a in elems],
                              e.get("line", 0))
        if k == "structlit":
            args = [self._lower_expr(v, irf) for _, v in e["fields"]]
            return self._emit(OP_STRUCT_NEW,
                              [("type", e["name"])]
                              + [("name", fname) for fname, _ in e["fields"]]
                              + [("var", a) for a in args],
                              e.get("line", 0))
        # Fallback: emit a const None so we don't crash the optimiser.
        return self._emit(OP_CONST, [("lit", None)], e.get("line", 0))


# ----------------------------------------------------------------------------
# Pretty-printer for debugging / `--emit ir` flag.
# ----------------------------------------------------------------------------

def _fmt_arg(a):
    if a is None:
        return "void"
    if isinstance(a, tuple):
        if a[0] == "lit":
            return repr(a[1])
        if a[0] == "var":
            return "%" + str(a[1])
        if a[0] == "name":
            return a[1]
        if a[0] == "op":
            return a[1]
        if a[0] == "label":
            return "@" + a[1]
        if a[0] == "fname":
            return a[1]
        if a[0] == "type":
            return a[1]
        if a[0] == "text":
            return repr(a[1])
        if a[0] == "arm":
            return "<arm>"
        return str(a)
    return str(a)


def dump_module(mod):
    """Render an HLIRModule as human-readable text (for `--emit ir`)."""
    lines = []
    for fname, irf in mod.functions.items():
        eff_str = ", ".join(sorted(irf.effects)) if irf.effects else "pure"
        lines.append("fn %s(%s) -> %s uses %s {" % (
            fname,
            ", ".join("%s: %s" % (n, t) for n, t in irf.params),
            irf.ret, eff_str))
        for block in irf.blocks:
            lines.append("  %s:" % block.name)
            for ins in block.instrs:
                args = ", ".join(_fmt_arg(a) for a in ins.args)
                dest = "%%%s =" % ins.dest if ins.dest else "  "
                lines.append("    %s %s %s" % (dest, ins.op, args))
            if block.terminator:
                t = block.terminator
                args = ", ".join(_fmt_arg(a) for a in t.args)
                lines.append("      %s %s" % (t.op, args))
        lines.append("}")
        lines.append("")
    return "\n".join(lines)


def build_module(program):
    """Convenience: build an HLIRModule from a parsed program."""
    return IRBuilder(program).build()
