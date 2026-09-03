#!/usr/bin/env python3
"""Structural validator for the LLVM IR text emitted by tools/llvm_emit.py.

This is the guardrail that was MISSING when Stage 12-alpha shipped: the CI
only piped `--emit llvm` output to /dev/null, so the emitter could produce
structurally invalid IR (type mismatches, instructions after terminators,
calls to undeclared symbols, `ptr <integer>` literal operands, phi nodes
with dangling predecessors) and every "deep scan" still reported green.

The validator is deliberately dependency-free (pure Python) so it runs in
any CI environment. It is NOT a full LLVM parser — it checks the specific
defect classes that historically occurred:

  V1  instructions emitted after a terminator (br/ret/unreachable) with no
      intervening label
  V2  calls to @symbols that are neither declared nor defined in the module
  V3  `ptr <plain-integer>` operand (invalid literal; boxes must be created
      via hl_box_* / inttoptr)
  V4  store type disagreeing with the alloca'd slot type (tracked per slot
      register), including the old i1-vs-i64 boolean inconsistency
  V5  phi nodes whose incoming labels are not valid block labels of the
      enclosing function
  V6  block labels referenced by branches/jumps that do not exist
  V7  `define` bodies missing a terminator on their final open block
  V8  duplicate block labels or duplicate global definitions

Exit code: 0 if the module passes, 1 with a report otherwise.

Usage:
    python3 tools/ll_validate.py file.ll
    python3 tools/ll_validate.py file1.ll file2.ll ...
    cat file.ll | python3 tools/ll_validate.py -        (read stdin)
"""
import re
import sys

TERMINATORS = ("br ", "ret ", "unreachable", "switch ", "indirectbr ",
               "invoke ", "resume ", "catchret ", "cleanupret ")

# Instruction operand patterns we can check without a full parser.
# NOTE: all patterns are anchored with `^\s*` and are matched against the
# STRIPPED line, so they work with or without indentation.
CALL_RE = re.compile(r"^\s*(?:%[\w.$-]+\s*=\s*)?call\s+(?:(\w+)\s+)?@([\w.$-]+)\((.*)\)\s*$")
DEF_RE = re.compile(r"^define\s+[\w\s]*?@([\w.$-]+)\s*\(")
DECLARE_RE = re.compile(r"^declare\s+[\w\s]*?@([\w.$-]+)\s*\(")
GLOBAL_RE = re.compile(r"^@([\w.$-]+)\s*=")
LABEL_RE = re.compile(r"^([\w.$-]+):")
ALLOCA_RE = re.compile(r"^\s*%([\w.$-]+)\s*=\s*alloca\s+(\w+)")
STORE_SLOT_RE = re.compile(r"^\s*store\s+(\w+)\s+([^,]+),\s*ptr\s+%([\w.$-]+)\s*$")
PHI_RE = re.compile(r"^\s*%([\w.$-]+)\s*=\s*phi\s+(\w+)\s+(.*)$")
PHI_EDGE_RE = re.compile(r"\[\s*([^,\]]+?)\s*,\s*%([\w.$-]+)\s*\]")
BR_RE = re.compile(r"^\s*br\s+(?:i1\s+%[\w.$-]+\s*,\s*)?label\s+%([\w.$-]+)(?:\s*,\s*label\s+%([\w.$-]+))?")
# `ptr 5` / `ptr -1` as an operand — always invalid (pointers are not
# integers in LLVM).
PTR_LIT_RE = re.compile(r"(?:^|\s|,|\()ptr\s+-?\d+(?:\s|,|\)|$)")


def validate(text, name="<module>"):
    """Validate one module; return a list of error strings."""
    errors = []
    lines = text.split("\n")

    # ---- module-level symbol tables ----
    declared = set()   # declare @foo
    defined = set()    # define @foo
    globals_ = set()   # @foo = ...
    for ln in lines:
        m = DECLARE_RE.match(ln)
        if m:
            declared.add(m.group(1))
            continue
        m = DEF_RE.match(ln)
        if m:
            if m.group(1) in defined:
                errors.append("V8 duplicate define @%s" % m.group(1))
            defined.add(m.group(1))
            continue
        m = GLOBAL_RE.match(ln)
        if m:
            if m.group(1) in globals_:
                errors.append("V8 duplicate global @%s" % m.group(1))
            globals_.add(m.group(1))

    # ---- per-function structural walk ----
    in_function = False
    fn_name = None
    cur_label = None           # label of the currently open block
    terminated = True          # True right after a terminator / at function start
    labels = set()             # labels seen in this function
    referenced_labels = []     # labels referenced by br/jump (line no, label)
    phi_edges = []             # (line no, pred label)
    slot_types = {}            # %slot reg -> alloca'd type
    line_no = 0

    def close_fn():
        nonlocal in_function, fn_name, labels, slot_types
        if in_function and not terminated and cur_label is not None:
            # Final open block without terminator (the emitter appends one,
            # so this would be an emitter bug).
            errors.append("V7 %s: block '%s' (ended at line %d) has no terminator"
                          % (fn_name, cur_label, line_no))
        # V5/V6: check label references and phi predecessors.
        for (lno, lbl) in referenced_labels:
            if lbl not in labels:
                errors.append("V6 %s (line %d): branch to unknown label '%%%s'"
                              % (fn_name, lno, lbl))
        for (lno, lbl) in phi_edges:
            if lbl not in labels:
                errors.append("V5 %s (line %d): phi predecessor '%%%s' is not a "
                              "block label of this function" % (fn_name, lno, lbl))
        in_function = False
        fn_name = None

    for raw in lines:
        line_no += 1
        ln = raw.strip()

        if not in_function:
            m = DEF_RE.match(ln)
            if m:
                in_function = True
                fn_name = m.group(1)
                cur_label = None
                terminated = True
                labels = set()
                referenced_labels = []
                phi_edges = []
                slot_types = {}
            continue

        # Function end.
        if ln == "}":
            close_fn()
            continue

        # Labels open a new block.
        m = LABEL_RE.match(ln)
        if m:
            lbl = m.group(1)
            if lbl in labels:
                errors.append("V8 %s (line %d): duplicate label '%s'"
                              % (fn_name, line_no, lbl))
            labels.add(lbl)
            if not terminated:
                errors.append(
                    "V1 %s (line %d): label '%s' opens while the previous block "
                    "'%s' is still open (missing terminator)"
                    % (fn_name, line_no, lbl, cur_label))
            cur_label = lbl
            terminated = False
            continue

        if not ln or ln.startswith(";"):
            continue

        # V1: instructions after a terminator without an intervening label.
        if terminated:
            errors.append(
                "V1 %s (line %d): instruction after terminator in block '%s': %s"
                % (fn_name, line_no, cur_label, ln[:70]))
            continue

        # Track terminators.
        if any(ln.startswith(t) for t in TERMINATORS):
            terminated = True
            m = BR_RE.match(raw)
            if m:
                for g in (m.group(1), m.group(2)):
                    if g:
                        referenced_labels.append((line_no, g))
            continue

        # Calls: check the callee exists (V2).
        m = CALL_RE.match(ln)
        if m:
            sym = m.group(2)
            if sym not in declared and sym not in defined:
                if not sym.startswith("llvm."):  # intrinsics need no declare
                    errors.append("V2 %s (line %d): call to undeclared symbol @%s"
                                  % (fn_name, line_no, sym))
            # V3: ptr <integer> literal operands inside the call.
            if PTR_LIT_RE.search(ln):
                errors.append("V3 %s (line %d): literal pointer operand "
                              "(ptr <int>) in call: %s" % (fn_name, line_no, ln[:70]))
            continue

        # V3 standalone check (e.g. store/load with ptr literal).
        if PTR_LIT_RE.search(ln) and "inttoptr" not in ln:
            errors.append("V3 %s (line %d): literal pointer operand: %s"
                          % (fn_name, line_no, ln[:70]))

        # Alloca: record the slot type.
        m = ALLOCA_RE.match(ln)
        if m:
            slot_types[m.group(1)] = m.group(2)
            continue

        # V4: store type must match the alloca'd slot type.
        m = STORE_SLOT_RE.match(ln)
        if m:
            stype, _val, slot = m.group(1), m.group(2), m.group(3)
            want = slot_types.get(slot)
            if want is not None and want != stype:
                errors.append(
                    "V4 %s (line %d): store %s into %s slot %s (type mismatch)"
                    % (fn_name, line_no, stype, want, slot))
            continue

        # V5: collect phi predecessors.
        m = PHI_RE.match(ln)
        if m:
            for _val, pred in PHI_EDGE_RE.findall(m.group(3)):
                phi_edges.append((line_no, pred))
            continue

    if in_function:
        close_fn()

    if errors:
        return ["%s: %d structural error(s)" % (name, len(errors))] + errors
    return []


def main():
    args = sys.argv[1:]
    if not args:
        sys.stderr.write(__doc__)
        return 2
    all_errors = []
    for path in args:
        if path == "-":
            text = sys.stdin.read()
            all_errors.extend(validate(text, "<stdin>"))
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError as ex:
                all_errors.append("%s: cannot read: %s" % (path, ex))
                continue
            all_errors.extend(validate(text, path))
    if all_errors:
        for err in all_errors:
            sys.stderr.write("ERROR: %s\n" % err)
        sys.stderr.write("llvm IR validation FAILED (%d error(s))\n"
                         % len(all_errors))
        return 1
    for path in args:
        if path != "-":
            sys.stderr.write("OK: %s\n" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
