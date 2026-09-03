#!/usr/bin/env python3
"""hlbindgen — C header → HLS extern declaration generator.

Stage 15 (v0.13.0-alpha): emits HLS `extern "C" { ... }` declarations
from simple C headers.

Usage:
  hlbindgen HEADER.h                # print HLS extern block to stdout
  hlbindgen HEADER.h -o out.hls     # write to file

Supported C constructs (alpha):
  - Function declarations: `int foo(char* s, long n);`
  - Standard types: int, long, char, char*, void, double, float, short.
  - One-line declarations only (no multi-line signatures).

Limitations (Stage 15 release targets):
  - No struct/enum generation.
  - No macro expansion.
  - No #include resolution.
  - No const/volatile qualifiers (silently dropped).
  - Manual `uses` annotation required: every generated function is
    marked `uses IO` (the safe default). The user edits the output
    to mark functions as `pure` if appropriate.
"""
import argparse
import os
import re
import sys


# C type -> HLS type mapping.
C_TO_HLS = {
    "int": "int",
    "long": "int",
    "long int": "int",
    "long long": "int",
    "short": "int",
    "short int": "int",
    "unsigned": "int",
    "unsigned int": "int",
    "unsigned long": "int",
    "unsigned long long": "int",
    "size_t": "int",
    "ssize_t": "int",
    "int8_t": "int",
    "int16_t": "int",
    "int32_t": "int",
    "int64_t": "int",
    "uint8_t": "int",
    "uint16_t": "int",
    "uint32_t": "int",
    "uint64_t": "int",
    "char": "int",
    "unsigned char": "int",
    "signed char": "int",
    "double": "float",
    "float": "float",
    "void": "void",
    "_Bool": "bool",
    "bool": "bool",
}

# C pointer types. `char*` -> str (HLS bytes are byte-strings).
# Other pointer types -> opaque int (HLS doesn't have raw pointers).
PTR_TO_HLS = {
    "char": "str",
    "const char": "str",
    "void": "int",       # opaque pointer
}


def parse_header(src: str) -> list:
    """Parse a C header source string; return a list of function
    declarations as dicts: {name, ret, params: [(name, type)]}."""
    decls = []
    # Strip comments.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    # Strip preprocessor directives.
    src = re.sub(r"^\s*#[^\n]*", "", src, flags=re.MULTILINE)
    # Find function declarations:
    #   RETTYPE name(params);
    pattern = re.compile(
        r"^[\w\s\*]+?\s+\**\s*(\w+)\s*\(([^)]*)\)\s*;",
        re.MULTILINE)
    for m in pattern.finditer(src):
        name = m.group(1)
        params_str = m.group(2).strip()
        # Parse the return type from the text before `name`.
        # BUG-DS4-29: the old code used full.index(name) — the FIRST
        # occurrence of the function name ANYWHERE in the match. For
        # `char* ch(char* s);` the name "ch" is found inside the return
        # type "char" itself, so the return type was parsed as "" -> int
        # instead of str. Use the match group's start position (relative
        # to the MATCH start — group offsets from finditer are absolute
        # in the subject string) instead.
        full = m.group(0)
        ret_part = full[:m.start(1) - m.start(0)].strip()
        ret_type = _parse_c_type(ret_part)
        # Parse parameters.
        params = []
        if params_str and params_str != "void":
            for p in params_str.split(","):
                p = p.strip()
                if not p or p == "...":
                    continue
                # Split into type and name. The name is the last
                # identifier; everything before is the type.
                tokens = p.split()
                if len(tokens) < 2:
                    # Just a type (no name) — e.g. `int` (anonymous).
                    pname = "_arg%d" % len(params)
                    ptype = _parse_c_type(p)
                else:
                    # BUG-DS4-30: C array parameters (`char buf[]`,
                    # `int pts[10]`) used to leak the brackets into the
                    # parameter NAME (emitting `buf[]: int`, which the
                    # HLS parser rejects). An array parameter decays to a
                    # pointer in C — strip the brackets from the name and
                    # parse the type as a pointer (char[] -> str).
                    last = tokens[-1]
                    arr = re.match(r"^([A-Za-z_]\w*)\s*((?:\[[^\]]*\])+)\s*$", last)
                    if arr:
                        pname = arr.group(1)
                        ptype = _parse_c_type(" ".join(tokens[:-1]) + " *")
                    else:
                        pname = last.lstrip("*")
                        ptype_str = " ".join(tokens[:-1])
                        ptype = _parse_c_type(ptype_str, p)
                params.append((pname, ptype))
        decls.append({"name": name, "ret": ret_type, "params": params})
    return decls


def _parse_c_type(type_str: str, full: str = None) -> str:
    """Convert a C type string to HLS type. Returns 'int' (opaque ptr)
    if the type can't be mapped."""
    t = type_str.strip()
    # Pointer?
    if "*" in t or (full and "*" in full):
        # Strip the * and any leading const.
        base = t.replace("*", "").strip()
        base = re.sub(r"^const\s+", "", base)
        # BUG-SC-BG-23 fix: the second `re.sub` was identical to the first
        # (a no-op). The intent was to also strip a TRAILING `const` (e.g.
        # `char const *` — legal C spelling). Now handles both forms.
        base = re.sub(r"\s+const$", "", base)
        if base in PTR_TO_HLS:
            return PTR_TO_HLS[base]
        # Unknown pointer type — opaque int.
        return "int"
    # Strip const.
    t = re.sub(r"^const\s+", "", t).strip()
    return C_TO_HLS.get(t, "int")


def emit_extern_block(decls: list, abi: str = "C") -> str:
    """Emit an HLS extern block from a list of declarations."""
    lines = ['extern "%s" {' % abi]
    for d in decls:
        params_str = ", ".join("%s: %s" % (n, t) for n, t in d["params"])
        ret = d["ret"]
        # Safe default: every generated function carries `uses IO`.
        # The user must edit to mark as `pure` if appropriate.
        line = "    fn %s(%s) -> %s uses IO" % (d["name"], params_str, ret)
        lines.append(line)
    lines.append("}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        prog="hlbindgen",
        description="C header -> HLS extern block generator (Stage 15-alpha).")
    parser.add_argument("header", help="C header file (.h).")
    parser.add_argument("-o", "--output", help="Write to file (default: stdout).")
    args = parser.parse_args()
    if not os.path.isfile(args.header):
        sys.stderr.write("error: file not found: %s\n" % args.header)
        return 1
    with open(args.header, "r") as f:
        src = f.read()
    decls = parse_header(src)
    if not decls:
        sys.stderr.write("warning: no function declarations found in %s\n"
                         % args.header)
    out = emit_extern_block(decls)
    if args.output:
        with open(args.output, "w") as f:
            f.write(out)
        print("Wrote %s (%d declarations)" % (args.output, len(decls)))
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
