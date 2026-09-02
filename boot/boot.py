#!/usr/bin/env python3
"""Stage-0 bootstrap for Hieu Louis (HLS).

This is the SEED used to bootstrap the self-hosting cycle of Hieu Louis:
  1. boot.py can run HLS code directly (interpreted, with type + effects checking).
  2. boot.py is used to run the compiler `src/hlc.hls` (written in HLS).
  3. From there on, the native compilation cycle is self-sustaining.

Usage:
  python3 boot/boot.py [--check] <file.hls> [program args...]

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
        try:
            toks = tokenize(src)
            program = Parser(toks).parse_program()
        except HLError:
            raise
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
    merged = {"structs": {}, "enums": {}, "fns": {}, "imports": []}
    for abs_path in load_order:
        prog = loaded[abs_path]
        for sname, sdef in prog["structs"].items():
            if sname in merged["structs"]:
                raise HLError("duplicate struct across modules: %s" % sname, 0, 0)
            merged["structs"][sname] = sdef
        for ename, edef in prog["enums"].items():
            if ename in merged["enums"] or ename in merged["structs"]:
                raise HLError("duplicate enum across modules: %s" % ename, 0, 0)
            merged["enums"][ename] = edef
        for fname, fdef in prog["fns"].items():
            if fname in merged["fns"]:
                raise HLError("duplicate function across modules: %s" % fname, 0, 0)
            merged["fns"][fname] = fdef
    return merged


def run_cli():
    args = sys.argv[1:]
    check_only = False
    audit_only = False
    if "--check" in args:
        check_only = True
        args.remove("--check")
    if "--audit" in args:
        audit_only = True
        args.remove("--audit")
    if not args:
        sys.stderr.write(
            "usage: boot.py [--check | --audit] <file.hls> [program args...]\n"
            "  --check    type-check + effects-check only, no execution.\n"
            "  --audit    print the capability / effect tree of every function.\n"
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
            decl_disp = ", ".join(sorted(decl)) if decl else "(none — pure)"
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
    name_w = max((len(r[0]) for r in rows), default=4)
    decl_w = max((len(r[1]) for r in rows), default=8)
    comp_w = max((len(r[2]) for r in rows), default=8)
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
