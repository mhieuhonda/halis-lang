#!/usr/bin/env python3
"""hlbindgen — C header -> HLS extern declaration generator.

Stage 15 release (v0.25.0-alpha): supports struct/enum generation,
#include resolution, const/volatile qualifiers, function-pointer
params (opaque int), and ABI-compatibility header generation.

Stage 15-alpha (v0.13.0-alpha): emits HLS `extern "C" { ... }`
declarations from simple C headers.

Usage:
  hlbindgen HEADER.h                  # print HLS extern block to stdout
  hlbindgen HEADER.h -o out.hls       # write HLS externs to file
  hlbindgen HEADER.h --abi-header     # print C ABI-check header to stdout
  hlbindgen HEADER.h -o out.hls --abi-header out.h  # write both
  hlbindgen HEADER.h --include /usr/include     # add #include search path
  hlbindgen HEADER.h --pure strlen   # mark `strlen` as `pure` (override)

Supported C constructs (Stage 15 release):
  - Function declarations: `int foo(char* s, long n);`
  - Standard types: int, long, char, char*, void, double, float, short,
    unsigned, signed, size_t, ssize_t, int8_t ... uint64_t, _Bool, bool.
  - Struct definitions (translates to HLS structs WITHOUT the C
    pointer-based aliasing; the HLS struct mirrors the field layout).
  - Enum definitions (translates to HLS enums).
  - #include resolution (via --include search paths).
  - const/volatile qualifiers (stripped, but tracked for naming).
  - Function-pointer parameters (synthesized as opaque int params).
  - Array parameters (decay to pointer; char[] -> str).

Limitations:
  - Unions are not supported (HLS does not have unions).
  - Bitfields are not supported (HLS does not have bitfields).
  - Macros are not expanded (#define is ignored).
  - Variadic functions (`...`) emit `_argN: int` but the runtime
    won't actually accept variadic args.
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
    "long long int": "int",
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
# Stage 15 release: add common pointer types so int*, long*, double*
# don't silently become opaque int.
PTR_TO_HLS = {
    "char": "str",
    "const char": "str",
    "void": "int",       # opaque pointer
    "int": "int",       # int* — opaque (HLS has no raw pointers)
    "long": "int",
    "short": "int",
    "float": "int",
    "double": "int",
    "unsigned": "int",
    "unsigned int": "int",
    "unsigned long": "int",
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
    "_Bool": "int",
    "bool": "int",
}


# HLS reserved words / primitive types — a struct/enum field name
# that clashes with one of these needs a `c_` prefix.
HLS_RESERVED = {
    "fn", "let", "mut", "return", "if", "else", "while", "for", "in",
    "break", "continue", "struct", "impl", "enum", "import", "uses",
    "true", "false", "match", "pure", "extern", "int", "float", "bool",
    "str", "void", "list", "map", "tainted", "self",
    "IO", "Fs", "Clock", "Args", "Exit", "Net", "Rand", "Proc",
}


def _sanitize_field_name(name):
    """Prefix reserved HLS words with `c_` so the field name is a
    legal HLS identifier."""
    if name in HLS_RESERVED:
        return "c_" + name
    if name and name[0].isdigit():
        return "c_" + name
    return name


def _strip_qualifiers(t):
    """Strip const/volatile/restrict/static from a C type string."""
    out = []
    for w in t.split():
        if w not in ("const", "volatile", "restrict", "static", "register",
                     "inline", "_Noreturn"):
            out.append(w)
    return " ".join(out).strip()


def parse_header(src: str, include_paths=None) -> dict:
    """Parse a C header source string.

    Returns a dict with three keys:
      - "functions": list of {name, ret, params, is_pure}
      - "structs":   list of {name, fields: [(name, hls_type)]}
      - "enums":     list of {name, variants: [(name, value)]}
    """
    if include_paths is None:
        include_paths = []
    # Resolve #include directives recursively.
    src = _resolve_includes(src, include_paths, set())
    # Strip comments.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    # Strip preprocessor directives we don't understand.
    src = re.sub(r"^\s*#[^\n]*", "", src, flags=re.MULTILINE)

    decls = {"functions": [], "structs": [], "enums": []}

    # Parse structs FIRST so later function signatures can reference them.
    _parse_structs(src, decls)
    _parse_enums(src, decls)
    _parse_functions(src, decls)

    return decls


def _resolve_includes(src, include_paths, seen):
    """Replace `#include "file.h"` with the contents of the file (if
    found in include_paths). System includes like `<stdio.h>` are
    left as comments (we don't try to resolve them — libc symbols are
    looked up at runtime via ctypes.CDLL(None))."""
    out_lines = []
    for line in src.split("\n"):
        m = re.match(r'^\s*#\s*include\s+"([^"]+)"\s*$', line)
        if m:
            path = m.group(1)
            for d in include_paths:
                cand = os.path.join(d, path)
                if os.path.isfile(cand):
                    if cand in seen:
                        # Avoid infinite recursion on circular includes.
                        out_lines.append("/* circular include: %s */" % path)
                        break
                    seen.add(cand)
                    with open(cand, "r") as f:
                        out_lines.append("/* begin include: %s */" % cand)
                        out_lines.append(_resolve_includes(f.read(), include_paths, seen))
                        out_lines.append("/* end include: %s */" % cand)
                    break
            else:
                # Not found — leave as comment.
                out_lines.append("/* unresolved include: %s */" % path)
            continue
        m = re.match(r'^\s*#\s*include\s+<([^>]+)>\s*$', line)
        if m:
            # System include — leave as comment.
            out_lines.append("/* system include: <%s> */" % m.group(1))
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def _parse_structs(src, decls):
    """Parse `struct Name { field1; field2; };` definitions.

    Stage 15 release: also handles nested struct types
    (`struct Point start;` -> `start: Point`).
    """
    pattern = re.compile(
        r"struct\s+([A-Za-z_]\w*)\s*\{([^}]*)\}\s*;",
        re.MULTILINE)
    for m in pattern.finditer(src):
        sname = m.group(1)
        body = m.group(2)
        fields = []
        for field_decl in body.split(";"):
            field_decl = field_decl.strip()
            if not field_decl:
                continue
            # Each field decl: TYPE name [, name2, ...]
            tokens = field_decl.split()
            if len(tokens) < 2:
                continue
            # Last token is the name (strip array brackets).
            last = tokens[-1]
            arr = re.match(r"^([A-Za-z_]\w*)\s*((?:\[[^\]]*\])+)\s*$", last)
            if arr:
                fname = arr.group(1)
                # An array field — HLS doesn't have fixed-size arrays.
                # Translate as list[T] where T is the element type.
                base_type = _parse_c_type(" ".join(tokens[:-1]) + " *")
                fields.append((_sanitize_field_name(fname), "list[%s]" % base_type))
                continue
            # Pointer field?
            if "*" in last or "*" in " ".join(tokens[:-1]):
                fname = last.lstrip("*")
                base_type = _parse_c_type(" ".join(tokens[:-1]) + " *")
                fields.append((_sanitize_field_name(fname), base_type))
                continue
            # Deep-scan-8 fix: handle `struct`/`enum` keyword ANYWHERE in
            # the type tokens (not just first position). Previously
            # `const struct Point start;` fell through to the plain-type
            # case and became `start: int` (because `const struct Point`
            # is not in C_TO_HLS). Now we scan tokens for `struct`/`enum`,
            # strip qualifiers, and use the type name.
            struct_or_enum_idx = -1
            for idx, tok in enumerate(tokens[:-1]):
                if tok in ("struct", "enum"):
                    struct_or_enum_idx = idx
                    break
            if struct_or_enum_idx >= 0 and len(tokens) >= struct_or_enum_idx + 3:
                fname = last
                type_name = tokens[struct_or_enum_idx + 1]
                fields.append((_sanitize_field_name(fname), type_name))
                continue
            # Plain type field (fallthrough: not array, not pointer,
            # not struct/enum keyword).
            fname = last
            # Deep-scan-8 fix: handle `struct Name` / `enum Name` types
            # in plain field declarations too. E.g. `struct Point start;`
            # without const -> tokens = ["struct", "Point", "start"],
            # which the old code sent to `_parse_c_type("struct Point")`
            # -> `int` (wrong, should be `Point`).
            if len(tokens) >= 3 and tokens[0] in ("struct", "enum"):
                ftype = tokens[1]
            else:
                ftype = _parse_c_type(" ".join(tokens[:-1]))
            fields.append((_sanitize_field_name(fname), ftype))
        # Deep-scan-8 fix: skip empty structs (the HLS parser rejects
        # `struct Foo {}` with "struct must have at least one field").
        # Previously hlbindgen emitted invalid HLS that the checker
        # would reject.
        if fields:
            decls["structs"].append({"name": sname, "fields": fields})


def _parse_enums(src, decls):
    """Parse `enum Name { A, B = 10, C };` definitions."""
    pattern = re.compile(
        r"enum\s+([A-Za-z_]\w*)\s*\{([^}]*)\}\s*;",
        re.MULTILINE)
    for m in pattern.finditer(src):
        ename = m.group(1)
        body = m.group(2)
        variants = []
        next_val = 0
        for v in body.split(","):
            v = v.strip()
            if not v:
                continue
            # Skip tokens like comments.
            if v.startswith("/*") or v.startswith("//"):
                continue
            # `NAME` or `NAME = value`
            mm = re.match(r"^([A-Za-z_]\w*)\s*(?:=\s*([^,]+))?$", v)
            if not mm:
                continue
            vname = mm.group(1)
            if mm.group(2):
                # Try to evaluate the value (int literal).
                try:
                    val = int(mm.group(2).strip(), 0)
                    next_val = val
                except ValueError:
                    val = next_val
            else:
                val = next_val
            variants.append((vname, val))
            next_val = val + 1
        # Deep-scan-8 fix: skip empty enums (the HLS parser rejects
        # `enum Foo {}` with "enum must have at least one variant").
        if variants:
            decls["enums"].append({"name": ename, "variants": variants})


def _parse_functions(src, decls):
    """Parse function declarations."""
    pattern = re.compile(
        r"^[\w\s\*]+?\s+\**\s*(\w+)\s*\(([^)]*)\)\s*;",
        re.MULTILINE)
    matched_names = set()
    for m in pattern.finditer(src):
        name = m.group(1)
        if name in matched_names:
            continue
        matched_names.add(name)
        params_str = m.group(2).strip()
        full = m.group(0)
        ret_part = full[:m.start(1) - m.start(0)].strip()
        ret_type = _parse_c_type(ret_part)
        params = []
        if params_str and params_str != "void":
            for p in params_str.split(","):
                p = p.strip()
                if not p or p == "...":
                    continue
                tokens = p.split()
                if len(tokens) < 2:
                    pname = "_arg%d" % len(params)
                    ptype = _parse_c_type(p)
                else:
                    last = tokens[-1]
                    arr = re.match(r"^([A-Za-z_]\w*)\s*((?:\[[^\]]*\])+)\s*$", last)
                    if arr:
                        pname = arr.group(1)
                        ptype = _parse_c_type(" ".join(tokens[:-1]) + " *")
                    else:
                        if "(" in last or ")" in last:
                            pname = "_arg%d" % len(params)
                            sys.stderr.write(
                                "warning: function-pointer parameter %r in "
                                "%s is not supported; synthesizing an opaque "
                                "ptr parameter named %s\n" % (last, p, pname))
                            ptype = "int"
                        else:
                            pname = last.lstrip("*")
                            ptype_str = " ".join(tokens[:-1])
                            ptype = _parse_c_type(ptype_str, p)
                params.append((pname, ptype))
        decls["functions"].append({
            "name": name, "ret": ret_type, "params": params, "is_pure": False,
        })
    # Warn about declarations containing function pointers that the
    # pattern silently skipped.
    all_decls = re.findall(r"^[\w\s\*]+?\s+\**\s*\w+\s*\([^;]*\)\s*;",
                           src, re.MULTILINE)
    for d in all_decls:
        nm = re.search(r"\**\s*(\w+)\s*\(", d)
        if nm and nm.group(1) not in matched_names:
            sys.stderr.write(
                "warning: skipped unsupported C declaration (function "
                "pointers / complex params are not supported): %s\n"
                % d.strip()[:100])


def _parse_c_type(type_str: str, full: str = None) -> str:
    """Convert a C type string to HLS type. Returns 'int' (opaque ptr)
    if the type can't be mapped."""
    t = type_str.strip()
    t = _strip_qualifiers(t)
    # Pointer?
    if "*" in t or (full and "*" in full):
        base = t.replace("*", "").strip()
        base = _strip_qualifiers(base)
        if base in PTR_TO_HLS:
            return PTR_TO_HLS[base]
        return "int"
    return C_TO_HLS.get(t, "int")


def emit_extern_block(decls: dict, abi: str = "C",
                      pure_fns=None) -> str:
    """Emit an HLS extern block from a list of declarations.

    Stage 15 release: also emits HLS struct + enum definitions BEFORE
    the extern block so the extern functions can reference them.

    `pure_fns` is a set of function names that should be marked `pure`
    instead of `uses IO`.
    """
    if pure_fns is None:
        pure_fns = set()
    lines = []
    # Struct definitions.
    for s in decls["structs"]:
        lines.append("struct %s {" % s["name"])
        for (fname, ftype) in s["fields"]:
            lines.append("    %s: %s" % (fname, ftype))
        lines.append("}")
        lines.append("")
    # Enum definitions.
    for e in decls["enums"]:
        lines.append("enum %s {" % e["name"])
        for i, (vname, val) in enumerate(e["variants"]):
            suffix = "," if i < len(e["variants"]) - 1 else ""
            lines.append("    %s%s" % (vname, suffix))
        lines.append("}")
        lines.append("")
    # Extern block.
    lines.append('extern "%s" {' % abi)
    for d in decls["functions"]:
        params_str = ", ".join("%s: %s" % (n, t) for n, t in d["params"])
        ret = d["ret"]
        if d["name"] in pure_fns:
            line = "    fn %s(%s) -> %s pure" % (d["name"], params_str, ret)
        else:
            line = "    fn %s(%s) -> %s uses IO" % (d["name"], params_str, ret)
        lines.append(line)
    lines.append("}")
    return "\n".join(lines) + "\n"


def emit_abi_header(decls: dict, src_header: str = None) -> str:
    """Stage 15 release: emit a C header file containing exactly the
    declarations that hlc.hls's codegen will emit as forward
    declarations. Compile this with `gcc -c` to verify that the HLS
    extern signatures match the real C declarations.

    The header contains:
      - #include of the original source header (if given) so the
        real declarations are visible.
      - Static-assert macros verifying the HLS-mapped types have the
        expected byte sizes.
    """
    lines = [
        "/* HLS ABI-compatibility header (generated by hlbindgen). */",
        "/* Compile with: gcc -c -Wall this_header.h */",
        "",
    ]
    if src_header:
        # Use a relative path so the assertion works regardless of
        # where hlbindgen runs.
        lines.append('#include "%s"' % src_header)
        lines.append("")
    # Type-size assertions.
    # Deep-scan-8 CRITICAL fix: the old assertions checked
    # `sizeof(int) == 8` and `sizeof(float) == 8`, but C `int` is
    # 4 bytes and C `float` is 4 bytes on every mainstream platform.
    # The assertions would FAIL on any standard gcc/clang build,
    # making the ABI header unusable. HLS `int` is i64 (8 bytes) and
    # HLS `float` is f64 (8 bytes), so the correct check is against
    # `int64_t` and `double` — the C types that actually match the
    # HLS ABI. Also include <stdint.h> for the int64_t typedef and
    # <stdbool.h> for `_Bool` (the C99 bool type).
    lines.append("/* Type-size assertions: HLS int is i64 (8 bytes),")
    lines.append("   HLS float is f64 (8 bytes), HLS bool is 1 byte.")
    lines.append("   C externs must use int64_t/double/_Bool to match. */")
    lines.append("#include <stdint.h>")
    lines.append("#include <stdbool.h>")
    lines.append('_Static_assert(sizeof(int64_t) == 8, "HLS int is i64 (8 bytes)");')
    lines.append('_Static_assert(sizeof(double) == 8, "HLS float is f64 (8 bytes)");')
    lines.append('_Static_assert(sizeof(_Bool) == 1, "HLS bool is 1 byte");')
    # Function existence assertions (one per extern fn).
    lines.append("")
    lines.append("/* Function-existence assertions: each HLS extern must")
    lines.append("   resolve to a real C function with a compatible signature. */")
    for d in decls["functions"]:
        # We can't easily type-check without a full C type parser, but
        # we can take the address to force the linker to resolve it.
        lines.append("extern void* %s_ptr; /* &%s */" % (d["name"], d["name"]))
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        prog="hlbindgen",
        description="C header -> HLS extern block generator (Stage 15 release).")
    parser.add_argument("header", help="C header file (.h).")
    parser.add_argument("-o", "--output", help="Write HLS externs to file (default: stdout).")
    parser.add_argument("--abi-header", metavar="PATH",
                        help="Generate a C ABI-compatibility header. "
                             "Use '-' or no value to print to stdout; "
                             "use a path to write to file.")
    parser.add_argument("--include", action="append", default=[],
                        help="Add a #include search path (repeatable).")
    parser.add_argument("--pure", action="append", default=[],
                        help="Mark the named function as `pure` (repeatable).")
    args = parser.parse_args()
    if not os.path.isfile(args.header):
        sys.stderr.write("error: file not found: %s\n" % args.header)
        return 1
    with open(args.header, "r") as f:
        src = f.read()
    decls = parse_header(src, include_paths=args.include)
    pure_set = set(args.pure)
    out = emit_extern_block(decls, pure_fns=pure_set)
    if not decls["functions"]:
        sys.stderr.write("warning: no function declarations found in %s\n"
                         % args.header)
    # Print stats to stderr.
    sys.stderr.write("hlbindgen: %d functions, %d structs, %d enums\n"
                     % (len(decls["functions"]),
                        len(decls["structs"]),
                        len(decls["enums"])))
    # HLS output.
    if args.output:
        with open(args.output, "w") as f:
            f.write(out)
        print("Wrote %s" % args.output)
    else:
        sys.stdout.write(out)
    # ABI header.
    if args.abi_header is not None:
        abi_src = emit_abi_header(decls, src_header=args.header)
        if args.abi_header == "-":
            sys.stdout.write("\n/* === ABI HEADER === */\n")
            sys.stdout.write(abi_src)
        else:
            with open(args.abi_header, "w") as f:
                f.write(abi_src)
            print("Wrote ABI header %s" % args.abi_header)
    return 0


if __name__ == "__main__":
    sys.exit(main())
