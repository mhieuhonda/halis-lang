#!/usr/bin/env python3
"""Stage-0 bootstrap for Hieu Louis (HLS).

This is the SEED used to bootstrap the self-hosting cycle of Hieu Louis:
  1. boot.py can run HLS code directly (interpreted, with type + effects checking).
  2. boot.py is used to run the compiler `src/hlc.hls` (written in HLS).
  3. From there on, the native compilation cycle is self-sustaining.

Usage:
  python3 boot/boot.py [--check | --audit] <file.hls> [program args...]

Flags:
  --check    type-check + effects-check only, no execution.
  --audit    print the capability / effect tree of every function
             (declared vs computed, with a clear OK/VIOLATION status).

  --check and --audit are mutually exclusive. If neither flag is given,
  the program is type-checked, effects-checked, and executed.

Imports (Stage 6):
  `import "path/to/file.hls"` — relative to the importing file's directory.
  `import "std.str"`          — resolves to <repo>/std/str.hls
  Each file is parsed once; transitive imports are merged into one program.
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boot.lexer import tokenize, HLError          # noqa: E402
from boot.parser import Parser                     # noqa: E402
from boot.checker import check                     # noqa: E402
from boot.interp import Interp                     # noqa: E402

# Repository root = parent of the `boot/` directory.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_import(import_path, importing_file):
    """Resolve an import path to an absolute file path.

    - "std.<name>"      →  searches several candidate locations:
                              <importing_file's dir>/std/<name>.hls
                              <parent>/std/<name>.hls (walked up to 5 levels)
                              <repo>/std/<name>.hls (fallback)
    - other paths       →  relative to the importing file's directory
    Returns None if the file does not exist.
    """
    if import_path.startswith("std."):
        module_name = import_path[4:]
        rel = os.path.join("std", module_name + ".hls")
        # Walk up from the importing file's directory.
        if importing_file:
            d = os.path.dirname(os.path.abspath(importing_file))
        else:
            d = _REPO_ROOT
        for _ in range(5):
            candidate = os.path.join(d, rel)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        # Fall back to repo root.
        candidate = os.path.join(_REPO_ROOT, rel)
        return candidate if os.path.isfile(candidate) else None
    # Relative path
    p = import_path
    base_dir = os.path.dirname(importing_file) if importing_file else _REPO_ROOT
    candidate = os.path.normpath(os.path.join(base_dir, p))
    return candidate if os.path.isfile(candidate) else None


def load_program(entry_path):
    """Load the entry file plus all transitive imports; return a merged program.

    Detects cycles and duplicate declarations across files.
    """
    loaded = {}        # abs_path -> program (structs+fns+imports)
    load_order = []    # abs_paths in load order (entry last)
    visiting = set()   # for cycle detection

    def load_file(abs_path):
        if abs_path in loaded:
            return loaded[abs_path]
        if abs_path in visiting:
            raise HLError("circular import detected: %s" % abs_path, 0, 0)
        visiting.add(abs_path)
        try:
            with open(abs_path, "rb") as f:
                src = f.read()
        except OSError:
            raise HLError("cannot open file: %s" % abs_path, 0, 0)
        # BUG-20 fix: removed the redundant `except HLError: raise` block —
        # catching an exception only to re-raise it unchanged is a no-op.
        toks = tokenize(src)
        program = Parser(toks).parse_program()
        # Resolve transitive imports first
        for imp in program["imports"]:
            resolved = _resolve_import(imp["path"], abs_path)
            if resolved is None:
                raise HLError("module not found: %s" % imp["path"], imp["line"], 0)
            load_file(resolved)
        visiting.discard(abs_path)
        loaded[abs_path] = program
        load_order.append(abs_path)
        return program

    entry_abs = os.path.abspath(entry_path)
    load_file(entry_abs)

    # Merge all loaded programs into one. Earlier-loaded files (dependencies)
    # appear first; the entry file appears last.
    merged = {"structs": {}, "enums": {}, "fns": {}, "imports": [],
              "externs": []}
    for abs_path in load_order:
        prog = loaded[abs_path]
        for sname, sdef in prog["structs"].items():
            # BUG-14 fix: struct names must not collide with each other
            # OR with any enum defined in another module.
            if sname in merged["structs"] or sname in merged["enums"]:
                raise HLError("duplicate type name across modules: %s" % sname, 0, 0)
            merged["structs"][sname] = sdef
        for ename, edef in prog["enums"].items():
            if ename in merged["enums"] or ename in merged["structs"]:
                raise HLError("duplicate type name across modules: %s" % ename, 0, 0)
            merged["enums"][ename] = edef
        for fname, fdef in prog["fns"].items():
            if fname in merged["fns"]:
                raise HLError("duplicate function across modules: %s" % fname, 0, 0)
            merged["fns"][fname] = fdef
        # Stage 15: merge extern blocks.
        for ext in prog.get("externs", []):
            merged["externs"].append(ext)
    return merged


def run_cli():
    args = sys.argv[1:]
    check_only = False
    audit_only = False
    emit_ir = False        # Stage 11 (v0.9.0-alpha): print HLIR text
    emit_llvm = False      # Stage 12 (v0.10.0-alpha): print LLVM IR text
    opt_stats = False      # Stage 11: print optimiser statistics
    target_triple = None   # Stage 12: --target <triple>
    if "--check" in args:
        check_only = True
        args.remove("--check")
    if "--audit" in args:
        audit_only = True
        args.remove("--audit")
    if "--emit" in args:
        i = args.index("--emit")
        if i + 1 < len(args) and args[i + 1] == "ir":
            emit_ir = True
            del args[i:i + 2]
        elif i + 1 < len(args) and args[i + 1] == "llvm":
            emit_llvm = True
            del args[i:i + 2]
        else:
            sys.stderr.write("error: --emit expects 'ir' or 'llvm'\n")
            return 2
    if "--opt-stats" in args:
        opt_stats = True
        args.remove("--opt-stats")
    if "--target" in args:
        i = args.index("--target")
        if i + 1 < len(args):
            target_triple = args[i + 1]
            del args[i:i + 2]
        else:
            sys.stderr.write("error: --target expects a triple\n")
            return 2
    # BUG-025 fix: --check and --audit are mutually exclusive — error out
    # explicitly rather than silently preferring one over the other.
    mutually_exclusive = sum([check_only, audit_only, emit_ir, emit_llvm, opt_stats])
    if mutually_exclusive > 1:
        sys.stderr.write(
            "error: --check / --audit / --emit ir / --emit llvm / --opt-stats are mutually exclusive\n")
        return 2
    if not args:
        sys.stderr.write(
            "usage: boot.py [--check | --audit | --emit ir | --emit llvm | --opt-stats]\n"
            "               [--target <triple>] <file.hls> [program args...]\n"
            "  --check        type-check + effects-check only, no execution.\n"
            "  --audit        print the capability / effect tree of every function.\n"
            "  --emit ir      print the HLIR (Stage 11) of every function.\n"
            "  --emit llvm    print the LLVM IR (Stage 12) of every function.\n"
            "  --opt-stats    run the Stage 11 optimiser, print per-pass stats.\n"
            "  --target TRIPLE  set the LLVM target triple (e.g. aarch64-linux).\n"
        )
        return 2
    path = args[0]
    prog_args = [a.encode("utf-8") for a in args]
    try:
        program = load_program(path)
    except HLError as ex:
        sys.stderr.write("compile error: %s\n" % ex)
        return 1
    except OSError:
        sys.stderr.write("error: cannot open file %s\n" % path)
        return 2
    try:
        checker = check(program)
    except HLError as ex:
        sys.stderr.write("compile error: %s\n" % ex)
        return 1
    if audit_only:
        print_audit(program, checker)
        return 0
    if emit_ir:
        return print_ir(program)
    if emit_llvm:
        return print_llvm(program, target_triple)
    if opt_stats:
        return print_opt_stats(program)
    if check_only:
        sys.stdout.write("OK: types and effects valid\n")
        return 0
    interp = Interp(program, prog_args, sys.stdout.buffer)
    return interp.run()


def print_audit(program, checker):
    """Print the full capability / effect tree of every function in the
    program. Used by `boot.py --audit <file.hls>` (Stage 9-beta)."""
    fns = program["fns"]
    computed = getattr(checker, "computed_effects", {})
    # Compute a display name per function key.
    rows = []
    for key, fn in fns.items():
        decl = fn["effects"]
        comp = computed.get(key, set())
        # If `pure` was declared, surface it in the declared column.
        if fn.get("pure", False):
            decl_disp = "pure" if not decl else ("pure + " + ", ".join(sorted(decl)))
        else:
            decl_disp = ", ".join(sorted(decl)) if decl else "(none - pure)"
        comp_disp = ", ".join(sorted(comp)) if comp else "(none)"
        missing = comp - decl
        if missing:
            status = "VIOLATION: missing " + ", ".join(sorted(missing))
        elif fn.get("pure", False) and comp:
            status = "VIOLATION: pure but uses effects"
        else:
            status = "OK"
        rows.append((key, decl_disp, comp_disp, status))
    # Compute column widths.
    # BUG-024 fix: removed unreachable `default=4` arguments — rows is
    # guaranteed non-empty because the checker errors out if there's no
    # `main` function, so we always have at least one entry.
    name_w = max(len(r[0]) for r in rows)
    decl_w = max(len(r[1]) for r in rows)
    comp_w = max(len(r[2]) for r in rows)
    # Header.
    print("=" * (name_w + decl_w + comp_w + 22))
    print("  %-*s  %-*s  %-*s  %s" % (
        name_w, "function", decl_w, "declared", comp_w, "computed", "status"))
    print("=" * (name_w + decl_w + comp_w + 22))
    for key, decl_disp, comp_disp, status in rows:
        print("  %-*s  %-*s  %-*s  %s" % (
            name_w, key, decl_w, decl_disp, comp_w, comp_disp, status))
    print("=" * (name_w + decl_w + comp_w + 22))
    # Summary line.
    n_pure = sum(1 for _, fn in fns.items() if fn.get("pure", False))
    n_eff = sum(1 for _, fn in fns.items() if fn["effects"])
    n_total = len(fns)
    print("  %d functions: %d declared pure, %d declared with effects"
          % (n_total, n_pure, n_eff))
    # Active vs reserved effects table.
    print("")
    print("  Active effects:    IO, Fs, Clock, Args, Exit")
    print("  Reserved effects:  Net, Rand, Proc  (error if used)")
    print("  `uses IO` expands to: {IO, Fs, Clock, Args, Exit}")
    # Stage 10-alpha: taint sources / sinks / unwraps summary.
    # Sources: builtins that introduce tainted values.
    # Sinks: builtins that reject tainted values at the checker.
    # Unwraps: builtins / functions that explicitly untaint.
    print("")
    print("  Taint sources (builtins):  tainted_args, read_file_tainted")
    print("  Taint sinks (builtins):    print, println, read_file,")
    print("                            write_file, file_exists, exit")
    print("  Explicit untaint:         taint_unwrap, std.sanitize.*")
    # Scan the call graph for actual taint-source usage.
    # (BUG-21 cleanup: removed the unused `unwrap_users` and `sanitize_users`
    # lists — they were allocated but never appended to. Deeper taint-flow
    # reporting is deferred to Stage 10-beta's `--audit` extension.)
    src_users = []
    for key, fn in fns.items():
        callees = checker.edges.get(key, set())
        if "b:tainted_args" in callees:
            src_users.append(key)
    if src_users:
        print("  Functions calling tainted_args():")
        for k in src_users:
            print("    - " + k)
    else:
        print("  No function calls tainted_args() (no taint sources in this program).")

    # Stage 10-beta: taint-flow audit extension. Scan the call graph for:
    #   - functions calling read_file_tainted (second taint source)
    #   - functions calling each taint sink (print, println, read_file,
    #     write_file, file_exists, exit)
    #   - functions calling taint_unwrap (the escape hatch)
    rft_users = []
    for key, fn in fns.items():
        callees = checker.edges.get(key, set())
        if "b:read_file_tainted" in callees:
            rft_users.append(key)
    if rft_users:
        print("  Functions calling read_file_tainted():")
        for k in rft_users:
            print("    - " + k)
    else:
        print("  No function calls read_file_tainted().")

    # Taint sinks: which functions call each sink?
    SINK_BUILTIN_NAMES = ("b:print", "b:println", "b:read_file",
                          "b:write_file", "b:file_exists", "b:exit")
    sink_users = {b: [] for b in SINK_BUILTIN_NAMES}
    for key, fn in fns.items():
        callees = checker.edges.get(key, set())
        for b in SINK_BUILTIN_NAMES:
            if b in callees:
                sink_users[b].append(key)
    print("  Taint sinks called:")
    for b in SINK_BUILTIN_NAMES:
        users = sink_users[b]
        bname = b[2:]  # strip "b:" prefix
        if users:
            print("    %s (%d):" % (bname, len(users)))
            for k in users:
                print("      - " + k)
        else:
            print("    %s (0):" % bname)


def print_ir(program):
    """Stage 11 (v0.9.0-alpha): print the HLIR of a program."""
    # Local import — the IR lives under tools/ to keep boot/ clean.
    import os as _os
    _tools = _os.path.join(_REPO_ROOT, "tools")
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    try:
        from ir import build_module, dump_module  # noqa: E402
    except ImportError as ex:
        sys.stderr.write("error: cannot load HLIR module: %s\n" % ex)
        return 2
    mod = build_module(program)
    sys.stdout.write(dump_module(mod))
    return 0


def print_llvm(program, target_triple=None):
    """Stage 12 (v0.10.0-alpha): print the LLVM IR of a program."""
    import os as _os
    _tools = _os.path.join(_REPO_ROOT, "tools")
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    try:
        from llvm_emit import emit_module  # noqa: E402
    except ImportError as ex:
        sys.stderr.write("error: cannot load LLVM emitter: %s\n" % ex)
        return 2
    out = emit_module(program, target_triple=target_triple)
    sys.stdout.write(out)
    return 0


def print_opt_stats(program):
    """Stage 11 (v0.9.0-alpha): run the optimiser, print per-pass stats.

    Reports the number of instructions before/after each pass, per function.
    Useful for `make opt-stats F=examples/foo.hls`.
    """
    import os as _os
    _tools = _os.path.join(_REPO_ROOT, "tools")
    if _tools not in sys.path:
        sys.path.insert(0, _tools)
    try:
        from ir import build_module  # noqa: E402
        from ir.optimize import optimize as ir_optimize  # noqa: E402
    except ImportError as ex:
        sys.stderr.write("error: cannot load HLIR optimiser: %s\n" % ex)
        return 2
    mod = build_module(program)
    # Snapshot pre-optimisation instruction counts.
    before = {}
    for fname, irf in mod.functions.items():
        before[fname] = sum(len(b.instrs) for b in irf.blocks)
    # Run the optimiser.
    ir_optimize(mod, fast=False)
    after = {}
    for fname, irf in mod.functions.items():
        after[fname] = sum(len(b.instrs) for b in irf.blocks)
    # Report.
    name_w = max((len(n) for n in before), default=4)
    print("=" * (name_w + 30))
    print("  %-*s  %10s  %10s  %10s" % (name_w, "function", "before", "after", "removed"))
    print("=" * (name_w + 30))
    total_b, total_a = 0, 0
    for fname in before:
        b = before[fname]
        a = after[fname]
        total_b += b
        total_a += a
        print("  %-*s  %10d  %10d  %10d" % (name_w, fname, b, a, b - a))
    print("=" * (name_w + 30))
    print("  %-*s  %10d  %10d  %10d" % (
        name_w, "TOTAL", total_b, total_a, total_b - total_a))
    print("")
    print("  Passes: constant_fold, copy_propagate, dead_code_elim")
    print("  (run -O fast for additional safe-arithmetic annotations)")
    return 0


def main():
    try:
        return run_cli()
    except SystemExit as ex:
        sys.stdout.buffer.flush()
        return ex.code if ex.code is not None else 0


if __name__ == "__main__":
    _result = {}

    def _runner():
        _result["code"] = main()

    sys.setrecursionlimit(1000000)
    _stack = 512 * 1024 * 1024
    while _stack >= 8 * 1024 * 1024:
        try:
            threading.stack_size(_stack)
            break
        except (ValueError, RuntimeError, OverflowError):
            _stack //= 2
    _t = threading.Thread(target=_runner)
    _t.start()
    _t.join()
    sys.stdout.buffer.flush()
    sys.exit(_result.get("code", 1))
