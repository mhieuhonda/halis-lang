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
    if "--check" in args:
        check_only = True
        args.remove("--check")
    if not args:
        sys.stderr.write("usage: boot.py [--check] <file.hls> [program args...]\n")
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
        check(program)
    except HLError as ex:
        sys.stderr.write("compile error: %s\n" % ex)
        return 1
    if check_only:
        sys.stdout.write("OK: types and effects valid\n")
        return 0
    interp = Interp(program, prog_args, sys.stdout.buffer)
    return interp.run()


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
