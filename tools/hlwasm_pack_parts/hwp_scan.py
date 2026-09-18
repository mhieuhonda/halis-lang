"""hwp_scan — lightweight HLS source scanner for Stage 76.

Extracts the publish surface of a ``.hls`` program — top-level
``fn`` definitions, ``struct``/``enum`` declarations, and
``extern "js"`` imports — so ``hls-wasm-pack`` can emit ``.d.ts``
declarations and ``package.json`` metadata WITHOUT running the full
compiler (the compiler remains the single source of truth for
types; this scanner is a documented heuristic for packaging only).

Method: the source is comment-stripped and string-masked, then
whole-source regexes find each declaration class:

* ``extern "js" { ... }`` blocks are cut out first (their inner
  declarations carry no bodies, so the block always ends at the
  first ``}``); every ``fn`` inside becomes a JS import.
* ``impl Name { ... }`` blocks are cut out via brace matching
  (method fns are NOT package exports).
* ``struct`` / ``enum`` declarations have no nested braces, so
  ``{...}`` captures them whole; fields split on top-level commas
  (``list[int]`` / ``map[str, int]`` brackets nest correctly).
* Remaining ``fn NAME(params) -> RET`` headers match across lines
  (``params`` is ``[^)]*`` — newlines included); the header ends at
  the first ``{`` or end-of-line, so bodies are never swallowed.

Limitations (documented, not bugs): generic instantiations
(``Option[int]``) are kept as opaque strings for the ``.d.ts``
mapper; ``mut`` prefixes and effects clauses are ignored.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_FN_RE = re.compile(
    r'(?m)fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*'
    r'(?:\[([^\]]*)\])?\s*'
    r'\(([^)]*)\)\s*'
    r'(?:->\s*([A-Za-z_\[\],\s][A-Za-z0-9_\[\],\s]*?))?\s*'
    r'(?:pure|\buses\b[^{\n]*)?\s*(?:\{|$)')
_STRUCT_RE = re.compile(
    r'struct\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}')
_ENUM_RE = re.compile(
    r'enum\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}')
_EXTERN_JS_RE = re.compile(r'extern\s+"js"\s*\{(.*?)\}', re.DOTALL)
_IMPL_RE = re.compile(r'impl\s+[A-Za-z_][A-Za-z0-9_]*\s*\{')


def _mask_strings(line: str) -> str:
    out: List[str] = []
    i = 0
    in_str = False
    while i < len(line):
        ch = line[i]
        if in_str:
            out.append(" ")
            if ch == "\\" and i + 1 < len(line):
                out.append(" ")
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_comments(source: str) -> str:
    """Strip ``#`` comments (string-aware); string contents kept."""
    out: List[str] = []
    for raw in source.splitlines():
        masked = _mask_strings(raw)
        idx = masked.find("#")
        out.append(raw[:idx] if idx >= 0 else raw)
    return "\n".join(out)


def _mask_all(source: str) -> str:
    """Mask string contents across the whole source text."""
    return _mask_strings(source)


def _cut_impl_blocks(src: str) -> str:
    """Remove ``impl Name { ... }`` blocks via brace matching."""
    while True:
        m = _IMPL_RE.search(src)
        if not m:
            return src
        depth = 0
        i = m.end() - 1
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        src = src[:m.start()] + "\n" + src[i + 1:]


def _split_top_level(body: str, sep: str = ",") -> List[str]:
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in body:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return [p.strip() for p in parts if p.strip()]


def _parse_params(raw: str) -> List[Dict[str, str]]:
    params: List[Dict[str, str]] = []
    raw = (raw or "").strip()
    if not raw:
        return params
    for part in _split_top_level(raw):
        part = part.strip()
        if part.startswith("mut "):
            part = part[4:].strip()
        if ":" not in part:
            continue
        pname, _, ptype = part.partition(":")
        pname = pname.strip()
        ptype = ptype.strip()
        if pname and ptype:
            params.append({"name": pname, "type": ptype})
    return params


def _parse_fn_match(m: "re.Match[str]") -> Dict[str, Any]:
    return {
        "name": m.group(1),
        "generics": (m.group(2) or "").strip(),
        "params": _parse_params(m.group(3) or ""),
        "returns": (m.group(4) or "void").strip() or "void",
    }


def scan_source(source: str) -> Dict[str, Any]:
    """Scan HLS ``source`` text; return the publish surface dict."""
    # Extern blocks are extracted BEFORE string masking (masking would
    # erase the "js" marker itself: extern "js" -> extern      ).
    nocmt = _strip_comments(source)
    js_imports: List[Dict[str, Any]] = []
    for block in _EXTERN_JS_RE.findall(nocmt):
        for m in _FN_RE.finditer(block):
            js_imports.append(_parse_fn_match(m))
    src = _mask_all(_EXTERN_JS_RE.sub("\n", nocmt))
    src = _cut_impl_blocks(src)

    structs: List[Dict[str, Any]] = []
    for m in _STRUCT_RE.finditer(src):
        fields: List[Dict[str, str]] = []
        for part in _split_top_level(m.group(2) or ""):
            if ":" not in part:
                continue
            fname, _, ftype = part.partition(":")
            # Drop ``= default`` tails (Stage 7 default values).
            ftype = ftype.split("=")[0].strip()
            if fname.strip() and ftype:
                fields.append({"name": fname.strip(), "type": ftype})
        structs.append({"name": m.group(1), "fields": fields})
    src = _STRUCT_RE.sub("\n", src)

    enums: List[Dict[str, Any]] = []
    for m in _ENUM_RE.finditer(src):
        variants: List[Dict[str, Any]] = []
        for part in _split_top_level(m.group(2) or ""):
            if not part:
                continue
            vm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?$",
                          part, re.DOTALL)
            if vm:
                payloads = _split_top_level(vm.group(2) or "") \
                    if vm.group(2) else []
                variants.append({"name": vm.group(1),
                                 "payloads": payloads})
        enums.append({"name": m.group(1), "variants": variants})
    src = _ENUM_RE.sub("\n", src)

    functions = [_parse_fn_match(m) for m in _FN_RE.finditer(src)]
    return {"functions": functions, "structs": structs, "enums": enums,
            "js_imports": js_imports}


def scan_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return scan_source(f.read())


def exported_functions(surface: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Top-level fns minus ``main`` and ``jsffi_on_callback``.

    ``main`` is the wasm entry point (invoked by the loader, not by
    library consumers); ``jsffi_on_callback`` is the Stage 73
    JS->HLS dispatch hook (reached via ``Halis.callHalis``, already
    typed in the glue). Everything else is the library surface.
    """
    skip = {"main", "jsffi_on_callback"}
    return [fn for fn in surface.get("functions", [])
            if fn["name"] not in skip]


__all__ = [
    "exported_functions",
    "scan_file",
    "scan_source",
]
