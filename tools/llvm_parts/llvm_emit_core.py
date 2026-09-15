"""emit_core - verbatim segment of the original tools/llvm_emit.py(split for maintainability; behavior unchanged)."""
from __future__ import annotations
import os as _os
import sys as _sys
_TOOLS_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _TOOLS_DIR not in _sys.path:
    _sys.path.insert(0, _TOOLS_DIR)
from llvm_common import (
    ATTRIBUTES, Dict, INTRINSIC_DECLS, List, Optional, RUNTIME_DECLS, Tuple, hls_type_to_llvm,
)

class LLVMEmitterCore(object):
    """Walks a checked HLS program AST and emits LLVM IR text."""

    def __init__(self, program, target_triple: Optional[str] = None):
        self.program = program
        self.target_triple = target_triple
        self.lines: List[str] = []
        self._tmp = 0
        self._label = 0
        self._ov_counter = 0
        self._str_counter = 0
        self._sc_counter = 0
        self._string_consts: List[Tuple[str, bytes, str]] = []
        # Stack of (continue_label, break_label) for loops.
        self._loop_stack: List[Tuple[str, str]] = []
        # Per-function local variable map: name -> (LLVM register, llvm_type).
        self._locals: Dict[str, Tuple[str, str]] = {}
        # The current function's return type (HLS type string).
        self._current_ret_type_value: str = "void"
        # The label of the currently open basic block, or None right after
        # a terminator was emitted. Used to (a) never emit instructions
        # after a terminator and (b) build phi nodes for && / ||.
        self._cur_block: Optional[str] = None
        # Deep-scan-12 fix (DSS-T-02): deferred allocas to be flushed into
        # the entry block when the function's body has been lowered. The
        # `match` result slot is added here when a match is encountered
        # mid-body; it is then emitted into the entry block at function-
        # close time. (The entry block was already opened at line 430 with
        # `entry:`; we append the deferred allocas right before `}`.)
        self._entry_allocas: List[Tuple[str, str]] = []

    def _fresh(self, prefix="t"):
        self._tmp += 1
        return "%%%s%d" % (prefix, self._tmp)

    def _fresh_label(self, prefix="bb"):
        self._label += 1
        return "%s%d" % (prefix, self._label)

    def _emit(self, line: str):
        """Emit one line and maintain current-block tracking.

        A line ending in ':' opens a new basic block. A `br`/`ret`/
        `unreachable` line closes the current block (no further
        instructions may be emitted until the next label).
        """
        self.lines.append(line)
        s = line.strip()
        if s.endswith(":") and not s.startswith(";"):
            self._cur_block = s[:-1]
        elif (s.startswith("br ") or s.startswith("ret ")
              or s == "unreachable" or s.startswith("switch ")):
            self._cur_block = None

    def _to_i1(self, val_ty: str, val: str) -> str:
        """Coerce a value to i1 for use as a branch condition."""
        if val_ty == "i1":
            return val
        if val_ty == "i64":
            tmp = self._fresh("b")
            self._emit("  %s = trunc i64 %s to i1" % (tmp, val))
            return tmp
        if val_ty == "ptr":
            tmp = self._fresh("b")
            self._emit("  %s = icmp ne ptr %s, null" % (tmp, val))
            return tmp
        # Fallback: treat any other type as truthy (non-zero).
        tmp = self._fresh("b")
        self._emit("  %s = or i1 1, 0" % tmp)
        return tmp

    # ---------- public API ----------
    def emit(self) -> str:
        """Emit the complete module (prelude + functions)."""
        # Reset ALL emitter state (idempotent — safe to call twice).
        self.lines = []
        self._string_consts = []
        self._tmp = 0
        self._label = 0
        self._ov_counter = 0
        self._sc_counter = 0
        self._str_counter = 0
        self._locals = {}
        self._loop_stack = []
        self._cur_block = None
        if self.target_triple:
            self._emit("target triple = \"%s\"" % self.target_triple)
            self._emit("")
        for line in RUNTIME_DECLS.strip().split("\n"):
            self._emit(line)
        self._emit("")
        for line in INTRINSIC_DECLS.strip().split("\n"):
            self._emit(line)
        self._emit("")
        # Emit panic message constants (used by the with.overflow paths).
        # "integer overflow" = 16 chars + 1 NUL = 17 bytes.
        self._emit('@.panic_overflow_msg = private unnamed_addr constant [17 x i8] c"integer overflow\\00"')
        self._emit("")
        # Extern "C" declarations become `declare` lines (BUG-DS4-6: they
        # used to be emitted as broken `define`s with `unreachable` bodies
        # that shadowed the real libc symbols).
        extern_decl_lines = []
        for fname, fn in self.program["fns"].items():
            if not fn.get("extern", False):
                continue
            ret_llvm = hls_type_to_llvm(fn["ret"])
            param_tys = [hls_type_to_llvm(p[1]) for p in fn["params"]]
            extern_decl_lines.append(
                "declare %s @%s(%s)" % (
                    ret_llvm, fname, ", ".join(param_tys)))
        for ln in extern_decl_lines:
            self._emit(ln)
        if extern_decl_lines:
            self._emit("")
        # Emit functions (extern decls are skipped — they have no body).
        for fname, fn in self.program["fns"].items():
            if fn.get("extern", False):
                continue
            self._emit_function(fname, fn)
        # Emit collected string constants just before the final join.
        # Insert them right after the panic constants (the second blank
        # line region) so they precede all functions.
        if self._string_consts:
            insert_at = None
            blank_count = 0
            for idx, ln in enumerate(self.lines):
                if ln == "":
                    blank_count += 1
                    if blank_count == 4:  # after decls + intrinsics + panic const
                        insert_at = idx + 1
                        break
            if insert_at is None:
                insert_at = len(self.lines)
            sc_lines = []
            for name, data, bytes_str in self._string_consts:
                sc_lines.append('@%s = private unnamed_addr constant [%d x i8] [%s]' % (
                    name, len(data) + 1, bytes_str))
            sc_lines.append("")
            self.lines[insert_at:insert_at] = sc_lines
        # Stage 12 release: emit the attributes block at the end of the
        # module (LLVM requires `attributes #N = { ... }` after the
        # declarations that reference it).
        for line in ATTRIBUTES.strip().split("\n"):
            self._emit(line)
        self._emit("")
        return "\n".join(self.lines) + "\n"

    # ---------- function emission ----------
    def _emit_function(self, fname: str, fn: Dict):
        ret_llvm = hls_type_to_llvm(fn["ret"])
        self._current_ret_type_value = fn["ret"]
        self._locals = {}
        self._cur_block = None
        # Deep-scan-12 fix (DSS-T-02): per-function reset of the deferred
        # alloca list. The list is appended to by `_lower_match_typed`
        # (and any other construct that needs an entry-block alloca
        # discovered after pre-collection).
        self._entry_allocas = []
        params = []
        for (pname, ptype, _) in fn["params"]:
            params.append("%s %%v_%s" % (hls_type_to_llvm(ptype), pname))
        param_str = ", ".join(params)
        self._emit("define %s @%s(%s) {" % (ret_llvm, fname, param_str))
        self._emit("entry:")
        # Allocate stack slots for parameters so we can re-assign them
        # (HLS allows `let mut` reassignment). Track the LLVM type per
        # slot so load/store use the right type.
        for (pname, ptype, _) in fn["params"]:
            slot = self._fresh("p")
            pty = hls_type_to_llvm(ptype)
            self._emit("  %s = alloca %s" % (slot, pty))
            self._emit("  store %s %%v_%s, ptr %s" % (pty, pname, slot))
            self._locals[pname] = (slot, pty)
        # BUG-DS4-7: pre-allocate slots for ALL `let` bindings and `for`
        # loop variables in the ENTRY block. Allocating inside loop bodies
        # grows the stack on every iteration (LLVM allocas are not released
        # until function return), so a `while` loop with a `let` inside
        # would eventually exhaust the stack. Slots are keyed by
        # (name, hls-type); the same name in disjoint sibling scopes reuses
        # the same slot safely (HLS forbids shadowing, and each `let`
        # stores before any load on every path that reaches a use).
        self._slot_pool: Dict[Tuple[str, str], Tuple[str, str]] = {}
        collected: List[Tuple[str, str]] = []
        self._collect_bindings(fn["body"], collected)
        for (bname, btype) in collected:
            key = (bname, btype)
            if key in self._slot_pool:
                continue
            slot = self._fresh("l")
            bty = hls_type_to_llvm(btype)
            self._emit("  %s = alloca %s" % (slot, bty))
            self._slot_pool[key] = (slot, bty)
        # Deep-scan-12 (DSS-T-02): placeholder for deferred allocas (those
        # discovered DURING body lowering — currently only the match
        # result slot, but extensible). We splice them in here at function
        # close so they live in the entry block (LLVM's mem2reg pass
        # hoists entry-block allocas, and a loop-body alloca would grow
        # the stack on every iteration).
        marker_idx = len(self.lines)
        self._emit("  ; __deferred_allocas_placeholder__")
        # Lower each statement.
        for stmt in fn["body"]:
            self._lower_stmt(stmt)
        # Implicit return void for void functions; unreachable for non-void
        # (the type checker already rejects missing returns).
        if self._cur_block is not None:
            if fn["ret"] == "void":
                self._emit("  ret void")
            else:
                # Defensive unreachable — the checker should have rejected
                # this function for not returning on all paths.
                self._emit("  unreachable")
        # Splice the deferred allocas into the placeholder line.
        if self._entry_allocas:
            new_lines = []
            for (slot_name, slot_ty) in self._entry_allocas:
                new_lines.append("  %s = alloca %s" % (slot_name, slot_ty))
            # Replace the placeholder line (at marker_idx) with the alloca
            # lines. self.lines is only ever appended to during body
            # lowering, so marker_idx remains valid.
            self.lines[marker_idx:marker_idx + 1] = new_lines
        else:
            # No deferred allocas — drop the placeholder comment line.
            del self.lines[marker_idx]
        self._emit("}")
        self._emit("")

    def _collect_bindings(self, stmts, acc: List[Tuple[str, str]]):
        """Collect every (name, type) bound by `let` / `for` anywhere in
        this statement list, including nested block scopes."""
        for s in stmts:
            k = s["k"]
            if k == "let":
                acc.append((s["name"], s["t"]))
            elif k == "if":
                self._collect_bindings(s["then"], acc)
                if s.get("els"):
                    self._collect_bindings(s["els"], acc)
            elif k == "while":
                self._collect_bindings(s["body"], acc)
            elif k == "for":
                acc.append((s["var"], s["vtype"]))
                # BUG (deep-scan-5): the loop-index slot was NOT collected
                # — a `for` nested in a loop re-executed its alloca every
                # outer iteration; allocas are reclaimed only at function
                # return, so the stack grew without bound. Pre-allocate it
                # in the entry block like every other binding. The '#idx'
                # suffix cannot collide with user identifiers.
                acc.append((s["var"] + "#idx", "int"))
                self._collect_bindings(s["body"], acc)

    def _get_slot(self, name: str, hls_ty: str) -> Tuple[str, str]:
        """Return (slot, llvm_type) for a binding, allocating lazily if the
        pre-collection pass missed it (defensive)."""
        key = (name, hls_ty)
        entry = self._slot_pool.get(key)
        if entry is None:
            slot = self._fresh("l")
            ty = hls_type_to_llvm(hls_ty)
            # Deep-scan-20 fix: allocate lazily-discovered slots in the
            # ENTRY block via the deferred-splice mechanism — an alloca
            # emitted at the current point (e.g. a match-arm payload
            # bind inside a while body) re-executes on every iteration
            # and grows the stack (allocas are reclaimed only at
            # function return). Same rule the result-slot fix
            # (deep-scan-12) applies.
            self._entry_allocas.append((slot, ty))
            entry = (slot, ty)
            self._slot_pool[key] = entry
        return entry

    # ---------- statement lowering ----------


__all__ = [
]
