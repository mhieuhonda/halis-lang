"""hwp_build — compile + assemble a publish-ready ``pkg/`` dir.

Pipeline (per target):

  1. ``hlwasm.compile_program`` into a temp dir (``.wasm`` + compact
     ``.js`` glue; ``--wasm-opt auto`` + ``--opt-level`` flow through).
  2. ``hwp_scan.scan_file`` the ``.hls`` input for the publish surface.
  3. ``hwp_glue.render_js`` wraps the compact glue per target.
  4. ``hwp_types.render_dts`` emits the ``.d.ts``.
  5. ``hwp_manifest.build_package_json`` + README + ``.pack-manifest.json``
     (sha256 per file, sizes, target, toolchain) complete ``pkg/``.

``build()`` returns the manifest dict. All writes stay inside
``out_dir`` (realpath containment enforced on every filename).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional

from hwp_common import (
    _REPO_ROOT, _TOOLS_DIR, DEFAULT_OPT_LEVEL, DEFAULT_TARGET,
    PACK_FORMAT_VERSION, PACK_TARGETS, WARN_JS_BYTES, WARN_WASM_BYTES,
    ensure_inside, log_info, log_warn,
)


def _ensure_tools_on_path() -> None:
    if _TOOLS_DIR not in sys.path:
        sys.path.insert(0, _TOOLS_DIR)
    _pkg_dir = os.path.join(_TOOLS_DIR, "hlwasm_parts")
    if _pkg_dir not in sys.path:
        sys.path.insert(0, _pkg_dir)
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _readme_md(opts: Dict[str, Any], surface: Dict[str, Any],
               wasm_size: int, js_size: int) -> str:
    from hwp_manifest import file_stem
    from hwp_scan import exported_functions
    name = str(opts.get("name", "halis-app"))
    version = str(opts.get("version", "0.1.0"))
    target = str(opts.get("target", DEFAULT_TARGET))
    stem = file_stem(name)
    lines = ["# %s" % name, "",
             str(opts.get("description",
                          "WebAssembly package built with hls-wasm-pack")),
             "",
             "Version %s · target `%s` · built with Halis Stage 76 "
             "(`hls-wasm-pack`)." % (version, target),
             "",
             "## Install", "",
             "```sh",
             "npm install %s" % name,
             "```", "",
             "## Use (%s)" % target]
    if target == "nodejs":
        lines += ["", "```js",
                  "const { init } = require('%s');" % name,
                  "const inst = await init(); // runs main()",
                  "```"]
    elif target == "no-modules":
        lines += ["", "```html",
                  '<script src="./%s.js"></script>' % stem,
                  "<script>HalisPack.init().then(() => {});</script>",
                  "```"]
    else:
        lines += ["", "```js",
                  "import init from '%s';" % name,
                  "const inst = await init(); // runs main()",
                  "```"]
    fns = exported_functions(surface)
    lines += ["", "## Halis API", ""]
    if fns:
        for fn in fns:
            params = ", ".join("%s: %s" % (p["name"], p["type"])
                               for p in fn.get("params", []))
            lines.append("- `%s(%s) -> %s`" % (fn["name"], params,
                                               fn.get("returns", "void")))
    else:
        lines.append("No exported functions (the program exposes `main` only).")
    structs = surface.get("structs", [])
    if structs:
        lines += ["", "## Structs", ""]
        for st in structs:
            fields = ", ".join("%s: %s" % (f["name"], f["type"])
                               for f in st.get("fields", []))
            lines.append("- `%s { %s }`" % (st["name"], fields))
    lines += ["",
              "## Sizes", "",
              "- `%s.wasm`: %d bytes" % (stem, wasm_size),
              "- `%s.js`: %d bytes" % (stem, js_size),
              "",
              "> Built from Halis source with `hls-wasm-pack build`. "
              "See `.pack-manifest.json` for per-file SHA-256 hashes.",
              ""]
    return "\n".join(lines)


def build(input_hls: str, out_dir: str,
          opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the publish-ready package; return the manifest dict."""
    from hwp_manifest import (build_package_json, file_stem, validate_name,
                              validate_version, write_package_json)
    from hwp_scan import scan_file
    import hwp_glue
    import hwp_types

    opts = dict(opts or {})
    target = str(opts.get("target", DEFAULT_TARGET))
    if target not in PACK_TARGETS:
        raise ValueError(
            "unknown pack target %r (expected one of: %s)"
            % (target, ", ".join(PACK_TARGETS)))
    name = validate_name(str(opts.get("name", "halis-app")))
    version = validate_version(str(opts.get("version", "0.1.0")))
    wasm_opt = str(opts.get("wasm_opt", "auto"))
    opt_level = str(opts.get("opt_level", DEFAULT_OPT_LEVEL))
    if wasm_opt not in ("auto", "on", "off"):
        raise ValueError("wasm_opt must be auto|on|off, got %r" % wasm_opt)
    if opt_level not in ("O1", "O2", "O3", "Os"):
        raise ValueError("opt_level must be O1|O2|O3|Os, got %r" % opt_level)

    if not os.path.isfile(input_hls):
        raise ValueError("input file not found: %r" % input_hls)
    stem = file_stem(name)
    os.makedirs(out_dir, exist_ok=True)

    _ensure_tools_on_path()
    from hlwasm_parts.hwasm_glue_cli import compile_program  # type: ignore

    tmp = tempfile.mkdtemp(prefix="hwp_build_")
    try:
        tmp_base = os.path.join(tmp, "bundle")
        rc = compile_program(input_hls, tmp_base,
                             target="wasm32-unknown-unknown",
                             emit_wasm=True, emit_js=True, emit_html=False,
                             run=False, wasm_opt=wasm_opt,
                             opt_level=opt_level, glue_style="compact")
        if rc != 0:
            raise ValueError("hlwasm compile failed (rc=%d)" % rc)
        with open(tmp_base + ".wasm", "rb") as f:
            wasm_bytes = f.read()
        # NOTE: the Stage 73 compact glue carries one non-UTF8 comment
        # byte (a cp1252 em-dash in the readStruct fix comment — a
        # pre-existing upstream mojibake, harmless inside a JS comment).
        # Normalize to valid UTF-8 on pack so the published .js is
        # decodable everywhere (browsers assume UTF-8 for ESM).
        with open(tmp_base + ".js", "rb") as f:
            compact_glue = f.read().decode("utf-8", errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if wasm_bytes[:4] != b"\x00asm":
        raise ValueError("hlwasm did not produce a valid wasm module")

    surface = scan_file(input_hls)

    # Write pkg/ files (every filename is containment-checked).
    wasm_file = stem + ".wasm"
    js_file = stem + ".js"
    dts_file = stem + ".d.ts"
    for fname in (wasm_file, js_file, dts_file, "package.json",
                  "README.md", ".pack-manifest.json"):
        ensure_inside(os.path.join(out_dir, fname), out_dir, "output file")

    with open(os.path.join(out_dir, wasm_file), "wb") as f:
        f.write(wasm_bytes)
    js_src = hwp_glue.render_js(target, stem, compact_glue)
    with open(os.path.join(out_dir, js_file), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(js_src)
    dts_src = hwp_types.render_dts(surface, name)
    with open(os.path.join(out_dir, dts_file), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(dts_src)

    pkg = build_package_json(opts, js_file, dts_file, wasm_file)
    write_package_json(out_dir, pkg)

    wasm_size = len(wasm_bytes)
    js_size = len(js_src.encode("utf-8"))
    with open(os.path.join(out_dir, "README.md"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(_readme_md(opts, surface, wasm_size, js_size))
    if target == "deno":
        with open(os.path.join(out_dir, "deno.json"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(hwp_glue.deno_json(name))

    files: List[str] = [wasm_file, js_file, dts_file, "package.json",
                        "README.md"]
    if target == "deno":
        files.append("deno.json")
    manifest: Dict[str, Any] = {
        "packVersion": PACK_FORMAT_VERSION,
        "name": name,
        "version": version,
        "target": target,
        "toolchain": "hls-wasm-pack v0.95.0-alpha (Stage 76)",
        "source": os.path.basename(input_hls),
        "files": {},
    }
    for fname in files:
        p = os.path.join(out_dir, fname)
        manifest["files"][fname] = {
            "size": os.path.getsize(p),
            "sha256": _sha256_file(p),
        }
    with open(os.path.join(out_dir, ".pack-manifest.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    if wasm_size >= WARN_WASM_BYTES:
        log_warn("wasm is %d bytes (>= %d): consider --wasm-opt on "
                 "+ --opt-level Os" % (wasm_size, WARN_WASM_BYTES))
    if js_size >= WARN_JS_BYTES:
        log_warn("js glue is %d bytes (>= %d)" % (js_size, WARN_JS_BYTES))
    log_info("built %s@%s [%s] -> %s (%d wasm bytes)"
             % (name, version, target, out_dir, wasm_size))
    return manifest


__all__ = [
    "build",
]
